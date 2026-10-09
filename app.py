"""
Catch-up: "What did I miss?" for WhatsApp chats.
Flask + OpenRouter (or any OpenAI-compatible endpoint, incl. local Ollama).

Run:  pip install -r requirements.txt && python app.py
"""
import io
import json
import os
import re
import secrets
import sqlite3
import time
import zipfile
from collections import Counter
from functools import wraps

import requests
from dotenv import load_dotenv
from flask import Flask, flash, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MB upload cap
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config["DATABASE"] = os.path.join(app.instance_path, "catchup.sqlite3")

API_KEY = os.getenv("OPENROUTER_API_KEY", "")
BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
DEFAULT_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
CHUNK_CHARS = int(os.getenv("CHUNK_CHARS", "24000"))   # size of one part sent to the model
MAX_CHUNKS = int(os.getenv("MAX_CHUNKS", "10"))         # newest parts kept if chat is huge
MAX_MSG_CHARS = 600


# --------------------------------------------------------------------------
# Accounts: only account data is stored. Chat exports are never persisted.
# --------------------------------------------------------------------------
def get_db():
    if "db" not in g:
        os.makedirs(app.instance_path, exist_ok=True)
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
    return g.db


def init_db():
    get_db().execute(
        """CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    get_db().commit()


@app.teardown_appcontext
def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            if request.path == "/analyze":
                return jsonify(error="Please sign in to analyse a chat."), 401
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def current_user():
    if "user_id" not in session:
        return None
    return get_db().execute("SELECT id, name, email FROM users WHERE id = ?", (session["user_id"],)).fetchone()


with app.app_context():
    init_db()

# --------------------------------------------------------------------------
# 1. WhatsApp export parsing (runs fully on your machine)
# --------------------------------------------------------------------------
_TIME = r"(\d{1,2}:\d{2}(?::\d{2})?(?:\s?[APap]\.?[Mm]\.?)?)"
_DATE = r"(\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4})"
ANDROID = re.compile(rf"^{_DATE},?\s+{_TIME}\s+-\s+(.*)$")   # 12/03/24, 10:45 - Name: msg
IOS = re.compile(rf"^\[{_DATE},?\s+{_TIME}\]\s+(.*)$")        # [12/03/24, 10:45:12 AM] Name: msg
STRIP = re.compile("[\u200e\u200f\u202a-\u202e]")
MEDIA = re.compile(r"<?(media omitted|image omitted|video omitted|sticker omitted|audio omitted)>?", re.I)


def normalise(text: str) -> str:
    text = STRIP.sub("", text)
    return text.replace("\u202f", " ").replace("\u00a0", " ")


def parse_chat(raw: str):
    msgs = []
    for line in normalise(raw).splitlines():
        m = ANDROID.match(line) or IOS.match(line)
        if m:
            date, time, rest = m.groups()
            if ": " not in rest:      # system line (joined, left, encryption notice...)
                continue
            sender, body = rest.split(": ", 1)
            msgs.append({"date": date, "time": time, "sender": sender.strip(), "text": body.strip()})
        elif msgs and line.strip():   # continuation of a multi-line message
            msgs[-1]["text"] += "\n" + line.strip()

    if not msgs:  # fallback: plain pasted text, one message per line
        for line in raw.splitlines():
            if line.strip():
                msgs.append({"date": "", "time": "", "sender": "?", "text": line.strip()})

    for i, m in enumerate(msgs, 1):
        m["id"] = i
        if MEDIA.search(m["text"]) and len(m["text"]) < 40:
            m["text"] = "[media]"
        m["text"] = m["text"][:MAX_MSG_CHARS]
    return msgs


# --------------------------------------------------------------------------
# 2. Privacy helpers: mask phone numbers / emails before anything is sent out
# --------------------------------------------------------------------------
PHONE = re.compile(r"(?<![\w])\+?\d[\d\s\-]{8,}\d(?![\w])")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def redact(msgs):
    alias = {}
    for m in msgs:
        if PHONE.fullmatch(m["sender"]):
            alias.setdefault(m["sender"], f"Person {len(alias) + 1}")
            m["sender"] = alias[m["sender"]]
        m["text"] = EMAIL.sub("[email]", PHONE.sub("[phone]", m["text"]))
    return msgs


# --------------------------------------------------------------------------
# 3. Local signals (cheap, no LLM) – used as hints + stats strip
# --------------------------------------------------------------------------
DEADLINE_WORDS = re.compile(
    r"\b(today|tonight|tomorrow|eod|asap|urgent|deadline|due|by (mon|tue|wed|thu|fri|sat|sun)\w*|"
    r"by \d{1,2}(:\d{2})?\s?(am|pm)?|\d{1,2}(st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*|"
    r"submit|submission|last date|meeting at|call at)\b",
    re.I,
)
GROUP_MENTION = re.compile(r"(?<!\w)@(all|everyone)(?!\w)", re.I)


def parse_names(value: str):
    """Accept comma or newline separated names without changing WhatsApp sender labels."""
    return [name.strip() for name in re.split(r"[,\n]", value or "") if name.strip()]


def local_signals(msgs, aliases, group_members=None):
    keys = [a.lower() for a in aliases if a]
    names = set(keys) | {k.split()[0] for k in keys if k.split()}
    names = {n.lstrip("@") for n in names if len(n.lstrip("@")) >= 3}
    pattern = re.compile(r"(?<!\w)@?(" + "|".join(re.escape(n) for n in names) + r")(?!\w)", re.I) if names else None
    # A supplied roster is the explicit group indicator.  Three or more unique
    # senders is the practical fallback for exports where no roster was entered.
    participants = {m["sender"].lower() for m in msgs}
    is_group = bool(group_members) or len(participants) >= 3
    mention_ids, deadline_ids = [], []
    for m in msgs:
        named_mention = pattern and m["sender"].lower() not in keys and pattern.search(m["text"])
        group_mention = is_group and GROUP_MENTION.search(m["text"])
        # In a WhatsApp group @all and @everyone address every member, including
        # the signed-in reader.  In a one-to-one chat they are ordinary words.
        if named_mention or group_mention:
            mention_ids.append(m["id"])
        if DEADLINE_WORDS.search(m["text"]):
            deadline_ids.append(m["id"])
    people = Counter(m["sender"] for m in msgs)
    return {
        "messages": len(msgs),
        "participants": len(people),
        "top_talkers": people.most_common(3),
        "first_date": msgs[0]["date"] if msgs else "",
        "last_date": msgs[-1]["date"] if msgs else "",
        "mention_ids": mention_ids,
        "deadline_ids": deadline_ids,
        "is_group": is_group,
        "group_members": group_members or [],
    }


# --------------------------------------------------------------------------
# 4. LLM call
# --------------------------------------------------------------------------
SYSTEM_PROMPT = """You are a chat triage assistant that answers "What did I miss?".
You receive a WhatsApp conversation, one message per line: [id] sender (date time): text
The reader is: {me}
This may be only one part of a longer chat; ids are global, so keep them as given.

Return ONLY valid JSON (no markdown, no commentary) with exactly this shape:
{{
  "tldr": "2-4 sentence plain-language summary of what happened",
  "topics": [{{"title": "...", "summary": "1-2 sentences"}}],
  "priority_items": [{{"title": "short", "detail": "1 sentence", "urgency": "high|medium|low", "ref": <message id>}}],
  "decisions": [{{"decision": "...", "made_by": "name or group", "ref": <id>}}],
  "action_items": [{{"task": "...", "owner": "name or Unassigned", "deadline": "as stated or null", "is_for_reader": true|false, "ref": <id>}}],
  "mentions_for_reader": [{{"from": "name", "what": "what they want/said", "needs_reply": true|false, "ref": <id>}}],
  "deadlines": [{{"what": "...", "when": "as stated, resolve 'tomorrow' etc. relative to the message date", "ref": <id>}}]
}}

Rules:
- Use only what is in the chat. Never invent tasks, names, dates or decisions.
- "ref" must be an id that exists in the chat.
- urgency "high" = needs action within ~24h, blocks others, or is a hard deadline; "medium" = needs action this week; "low" = FYI.
- Set is_for_reader / mentions_for_reader only when the reader is addressed by name, @mention, or clearly implied. If the reader is unknown, use empty mention list.
- This is a group chat: {is_group}. Known group members: {group_members}. When it is a group chat, @all or @everyone includes the reader and should count as a mention for the reader when the message is relevant. Do not apply that rule to a one-to-one chat.
- Ignore small talk, greetings, memes and forwarded spam unless they hold information.
- Sort priority_items from most to least urgent. Keep every field short. Use empty arrays when nothing applies.
- Hints from a keyword scan (may be incomplete or wrong): messages that may mention the reader: {mentions}; messages with possible deadlines: {deadlines}.
"""


def extract_json(text: str):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("Model did not return JSON")
    return json.loads(text[start : end + 1])


def fmt(m):
    return f"[{m['id']}] {m['sender']} ({m['date']} {m['time']}): {m['text']}".strip()


def make_chunks(msgs):
    """Split the whole chat into parts that fit the model; keep newest parts if huge."""
    chunks, cur, size = [], [], 0
    for m in msgs:
        n = len(fmt(m)) + 1
        if cur and size + n > CHUNK_CHARS:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(m)
        size += n
    if cur:
        chunks.append(cur)
    dropped = 0
    if len(chunks) > MAX_CHUNKS:
        dropped = sum(len(c) for c in chunks[:-MAX_CHUNKS])
        chunks = chunks[-MAX_CHUNKS:]
    return chunks, dropped


def call_llm(system, user, model):
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}",
        "HTTP-Referer": "http://localhost:5000",
        "X-Title": "Catch-up",
    }
    payload = {
        "model": model,
        "temperature": 0.2,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    r = requests.post(f"{BASE_URL}/chat/completions", headers=headers, json=payload, timeout=180)
    if r.status_code != 200:
        try:
            detail = r.json().get("error", {}).get("message", r.text)
        except Exception:
            detail = r.text
        raise RuntimeError(f"LLM API error {r.status_code}: {detail}")
    return r.json()["choices"][0]["message"]["content"]


def llm_json(system, user, model, tries=2):
    """Call the model and parse JSON; retry once on bad JSON or a rate limit."""
    for attempt in range(tries):
        try:
            return extract_json(call_llm(system, user, model))
        except (ValueError, json.JSONDecodeError):
            if attempt == tries - 1:
                raise
        except RuntimeError as e:
            if "429" in str(e) and attempt < tries - 1:
                time.sleep(6)
                continue
            raise


def analyse_chunk(chunk, me_label, stats, model):
    ids = {m["id"] for m in chunk}
    system = SYSTEM_PROMPT.format(
        me=me_label or "unknown (do not guess)",
        mentions=[i for i in stats["mention_ids"] if i in ids][:60] or "none",
        deadlines=[i for i in stats["deadline_ids"] if i in ids][:60] or "none",
        is_group="yes" if stats["is_group"] else "no",
        group_members=", ".join(stats["group_members"]) or "not supplied",
    )
    return llm_json(system, "\n".join(fmt(m) for m in chunk), model)


LIST_KEYS = {
    "topics": "title",
    "priority_items": "title",
    "decisions": "decision",
    "action_items": "task",
    "mentions_for_reader": "what",
    "deadlines": "what",
}
URGENCY = {"high": 0, "medium": 1, "low": 2}


def merge_results(results, model):
    out = {k: [] for k in LIST_KEYS}
    for r in results:
        for k in LIST_KEYS:
            out[k].extend(x for x in (r.get(k) or []) if isinstance(x, dict))
    for k, field in LIST_KEYS.items():  # drop duplicates that appear across parts
        seen, unique = set(), []
        for x in out[k]:
            key = str(x.get(field, "")).strip().lower()
            if key and key not in seen:
                seen.add(key)
                unique.append(x)
        out[k] = unique
    out["priority_items"].sort(key=lambda x: URGENCY.get(x.get("urgency"), 3))

    tldrs = [r.get("tldr", "") for r in results if r.get("tldr")]
    if len(tldrs) <= 1:
        out["tldr"] = tldrs[0] if tldrs else ""
    else:
        try:
            joined = "\n".join(f"Part {i + 1}: {t}" for i, t in enumerate(tldrs))
            out["tldr"] = call_llm(
                "Combine these partial summaries of one WhatsApp chat (oldest to newest) into one clear "
                "summary of 3-5 sentences. Plain text only, no markdown. Do not add facts.",
                joined, model,
            ).strip()
        except Exception:
            out["tldr"] = " ".join(tldrs)
    return out


def attach_quotes(result, by_id):
    """Link every item back to the original message so the user can verify it."""
    for key in ("priority_items", "decisions", "action_items", "mentions_for_reader", "deadlines"):
        for item in result.get(key, []) or []:
            try:
                msg = by_id.get(int(item.get("ref")))
            except (TypeError, ValueError):
                msg = None
            if msg:
                item["source"] = {
                    "sender": msg["sender"],
                    "when": f"{msg['date']} {msg['time']}".strip(),
                    "text": msg["text"][:220],
                }
    return result


# --------------------------------------------------------------------------
# 5. Routes
# --------------------------------------------------------------------------
@app.route("/")
@login_required
def index():
    return render_template(
        "index.html",
        default_model=DEFAULT_MODEL,
        local_mode="localhost" in BASE_URL,
        user=current_user(),
    )


@app.route("/register", methods=["GET", "POST"])
def register():
    if "user_id" in session:
        return redirect(url_for("index"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not name or not email or not password:
            flash("Please complete every field.", "danger")
        elif len(password) < 8:
            flash("Use a password with at least 8 characters.", "danger")
        else:
            try:
                db = get_db()
                cur = db.execute(
                    "INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
                    (name, email, generate_password_hash(password)),
                )
                db.commit()
                session.clear()
                session["user_id"] = cur.lastrowid
                return redirect(url_for("index"))
            except sqlite3.IntegrityError:
                flash("An account with that email already exists. Please sign in.", "danger")
    return render_template("auth.html", mode="register")


@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("index"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Incorrect email or password.", "danger")
        else:
            session.clear()
            session["user_id"] = user["id"]
            return redirect(url_for("index"))
    return render_template("auth.html", mode="login")


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def read_upload(upload):
    data = upload.read()
    if upload.filename.lower().endswith(".zip"):  # iPhone exports (or "with media") come as a zip
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".txt")]
            if not names:
                raise ValueError("No .txt chat file found inside that zip.")
            names.sort(key=lambda n: ("_chat" not in n.lower(), len(n)))
            data = z.read(names[0])
    return data.decode("utf-8", errors="ignore")


@app.route("/analyze", methods=["POST"])
@login_required
def analyze():
    if not API_KEY and "openrouter.ai" in BASE_URL:
        return jsonify(error="No API key found. Add OPENROUTER_API_KEY to your .env file and restart."), 400

    raw = request.form.get("chat_text", "")
    upload = request.files.get("chat_file")
    if upload and upload.filename:
        try:
            raw = read_upload(upload)
        except (ValueError, zipfile.BadZipFile) as e:
            return jsonify(error=str(e) if isinstance(e, ValueError) else "That zip file is damaged."), 400
    if not raw.strip():
        return jsonify(error="Paste a chat or upload a WhatsApp .txt export first."), 400

    # "Rahul, Rahul K, 919876543210" -> every variant counts as you
    aliases = parse_names(request.form.get("user_name", ""))
    me_label = ", ".join(aliases)
    group_members = parse_names(request.form.get("group_members", ""))
    model = request.form.get("model", "").strip() or DEFAULT_MODEL
    do_redact = request.form.get("redact") == "on"
    try:
        last_n = int(request.form.get("last_n") or 0)
    except ValueError:
        last_n = 0

    msgs = parse_chat(raw)
    if last_n > 0:
        msgs = msgs[-last_n:]
    if do_redact:
        msgs = redact(msgs)
    if not msgs:
        return jsonify(error="Couldn't find any messages in that text."), 400

    stats = local_signals(msgs, aliases, group_members)
    chunks, dropped = make_chunks(msgs)

    results, failed = [], 0
    for chunk in chunks:
        try:
            results.append(analyse_chunk(chunk, me_label, stats, model))
        except (RuntimeError, requests.RequestException) as e:
            if not results:          # first part failed: usually key, credits or rate limit
                return jsonify(error=str(e)), 502
            failed += 1
        except (ValueError, json.JSONDecodeError):
            failed += 1
    if not results:
        return jsonify(error="The model replied in an unexpected format. Try again or pick a stronger model."), 502

    result = merge_results(results, model)
    result = attach_quotes(result, {m["id"]: m for m in msgs})
    result["stats"] = {k: stats[k] for k in ("messages", "participants", "top_talkers", "first_date", "last_date")}
    result["stats"].update(
        mentions=len(stats["mention_ids"]), dropped=dropped, model=model,
        parts=len(chunks), failed_parts=failed,
    )
    return jsonify(result)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
