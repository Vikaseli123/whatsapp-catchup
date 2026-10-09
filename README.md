# Catch-up — WhatsApp briefing app

Catch-up is a Flask application that converts a WhatsApp chat export into a focused briefing: what needs your reply, tasks, urgency, deadlines, decisions, and main topics. It uses the included Spark Bootstrap 5 design system and works with OpenRouter or an OpenAI-compatible local endpoint such as Ollama.

## What it does

- Creates local, password-protected user accounts with SQLite.
- Accepts pasted WhatsApp messages or a `.txt` / `.zip` export.
- Reads sender names from every export; for groups, you can also enter the member roster.
- Treats `@all` and `@everyone` as a mention of you in a group chat, including when you are one of the members.
- Identifies direct mentions, reply-needed messages, deadlines, decisions, tasks, priorities, and topics.
- Optionally masks phone numbers and email addresses before the chat is sent to the selected model.
- Never saves chat text, uploaded exports, or generated summaries. Only account records are stored locally in `instance/catchup.sqlite3`.

## Run locally

```powershell
cd whatsapp-catchup
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Open `.env` and set `OPENROUTER_API_KEY`. You should also set a long random `FLASK_SECRET_KEY` for stable login sessions. Then run:

```powershell
python app.py
```

Open http://127.0.0.1:5000, create an account, and sign in.

## Export a WhatsApp chat

1. Open the chat or group in WhatsApp.
2. Open the chat details and choose **Export chat**.
3. Choose **Without media**.
4. Upload the exported `.txt` file, or the `.zip` file created by iPhone, in Catch-up.

For a group, enter member names (comma or line separated) in **Group member names**. This explicitly enables group-wide `@all` / `@everyone` detection. If the field is blank, Catch-up continues to work normally for direct chats; it also recognizes exports with three or more distinct senders as group conversations.

## Configuration

| Variable | Purpose | Default |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | OpenRouter API key | required for OpenRouter |
| `OPENROUTER_BASE_URL` | OpenAI-compatible endpoint | `https://openrouter.ai/api/v1` |
| `OPENROUTER_MODEL` | Initial model shown in the form | `openai/gpt-4o-mini` |
| `FLASK_SECRET_KEY` | Signs authenticated user sessions | temporary random value if omitted |
| `CHUNK_CHARS` / `MAX_CHUNKS` | Limits very long exports | `24000` / `10` |

For Ollama, run a local model and set `OPENROUTER_BASE_URL=http://localhost:11434/v1`; the app then does not require an OpenRouter key.

## Project structure

```text
app.py                         Flask routes, auth, parsing and LLM orchestration
instance/catchup.sqlite3       Local account database (created automatically; ignored by Git)
templates/auth.html            Sign in and registration screen
templates/index.html           Authenticated Catch-up workspace
static/vendor/spark/           Spark Bootstrap 5 assets bundled from the supplied template
static/css/style.css           Catch-up custom design layer
static/js/app.js               Upload, request and result rendering logic
```

## Privacy and security notes

Passwords are stored as Werkzeug password hashes, never plain text. Account storage is local SQLite. Chat content is processed in memory for the request and is not written to the database or filesystem. When using a remote model provider, it still receives the chat text needed to generate the briefing—leave masking enabled if you want phone numbers and email addresses redacted first.
