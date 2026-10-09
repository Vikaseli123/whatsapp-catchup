const SAMPLE = `12/03/25, 09:02 - Priya: Good morning everyone! Reminder that the hackathon project report is due Friday 5 PM
12/03/25, 09:05 - Arjun: lol who is even awake
12/03/25, 09:07 - Priya: @Rahul can you finish the API section by tomorrow night? Karthik is waiting on it for the demo
12/03/25, 09:16 - Meera: sharing the logo options, vote by tonight pls
12/03/25, 09:30 - Karthik: I think we should go with Option 2
12/03/25, 10:12 - Priya: ok decision made, we go with Option 2 for the logo
12/03/25, 14:05 - Karthik: @everyone the demo is now in Room 304 at 3 PM on Saturday, not Room 201
12/03/25, 14:06 - Karthik: Rahul please confirm you can bring the projector
12/03/25, 16:20 - Priya: Also everyone needs to upload their slides to the shared drive by Thursday EOD`;

const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));

$("#sample").addEventListener("click", () => {
  $("#chat_text").value = SAMPLE;
  $("#user_name").value = "Rahul";
  $("#group_members").value = "Priya, Arjun, Meera, Karthik, Rahul";
});

$("#form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("#go");
  button.disabled = true;
  button.innerHTML = "Reading your chat…";
  $("#out").innerHTML = `<div class="card app-card empty-state"><div class="card-body"><div class="loader"><span></span><span></span><span></span></div><p class="mt-3 mb-0">Sorting what’s urgent from what’s noise…</p></div></div>`;
  try {
    const response = await fetch("/analyze", { method: "POST", body: new FormData(event.target) });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Something went wrong");
    render(data);
  } catch (error) {
    $("#out").innerHTML = `<div class="error-box"><strong>Couldn’t summarise that.</strong><br>${esc(error.message)}</div>`;
  } finally {
    button.disabled = false;
    button.innerHTML = `<i class="bi bi-stars"></i> Create my catch-up`;
  }
});

const source = (item) => item.source ? `<div class="src"><strong>${esc(item.source.sender)}</strong> · ${esc(item.source.when)}<br>${esc(item.source.text)}</div>` : "";
const section = (icon, title, items, renderer) => !items?.length ? "" : `<h3 class="sec-title"><i class="bi ${icon}"></i>${title}<span class="sec-count">${items.length}</span></h3>${items.map(renderer).join("")}`;

function render(data) {
  const stats = data.stats || {};
  const forYou = (data.action_items || []).filter((item) => item.is_for_reader);
  const others = (data.action_items || []).filter((item) => !item.is_for_reader);
  const talkers = (stats.top_talkers || []).map((talker) => talker[0]).join(", ");
  let html = `<div class="d-flex flex-wrap gap-2 mb-4"><div class="stat"><b>${stats.messages ?? 0}</b><span>messages</span></div><div class="stat"><b>${stats.participants ?? 0}</b><span>people</span></div><div class="stat"><b>${stats.mentions ?? 0}</b><span>mentions you</span></div><div class="stat"><b>${(data.deadlines || []).length}</b><span>deadlines</span></div></div><div class="tldr"><span class="who">Catch-up briefing</span>${esc(data.tldr)}</div><div class="small text-secondary mt-2 ms-1">${esc(stats.first_date)} to ${esc(stats.last_date)}${talkers ? " · most active: " + esc(talkers) : ""} · ${esc(stats.model)}${stats.parts > 1 ? ` · read in ${stats.parts} parts` : ""}${stats.failed_parts ? ` · ${stats.failed_parts} part(s) failed and were skipped` : ""}${stats.dropped ? ` · ${stats.dropped} oldest messages skipped` : ""}</div>`;
  html += section("bi-person-raised-hand", "Needs you", data.mentions_for_reader, (item) => `<div class="item you"><div class="item-title">${item.needs_reply ? '<span class="tag hot">Reply needed</span>' : ""}${esc(item.from)}</div><div class="item-detail">${esc(item.what)}</div>${source(item)}</div>`);
  html += section("bi-check2-square", "Your tasks", forYou, (item) => `<div class="item you"><div class="item-title">${esc(item.task)}</div>${item.deadline ? `<div><span class="tag hot">${esc(item.deadline)}</span></div>` : ""}${source(item)}</div>`);
  html += section("bi-exclamation-triangle", "Most urgent first", data.priority_items, (item) => { const urgency = ["high", "medium", "low"].includes(item.urgency) ? item.urgency : "low"; return `<div class="item ${urgency}"><div class="item-title"><span class="pill ${urgency}">${urgency}</span>${esc(item.title)}</div><div class="item-detail">${esc(item.detail)}</div>${source(item)}</div>`; });
  html += section("bi-calendar-event", "Deadlines and dates", data.deadlines, (item) => `<div class="item plain"><div class="item-title">${esc(item.what)}</div><div><span class="tag hot">${esc(item.when)}</span></div>${source(item)}</div>`);
  html += section("bi-hammer", "Decisions made", data.decisions, (item) => `<div class="item plain"><div class="item-title">${esc(item.decision)}</div><div class="item-detail">By ${esc(item.made_by)}</div>${source(item)}</div>`);
  html += section("bi-list-task", "Other people’s tasks", others, (item) => `<div class="item plain"><div class="item-title">${esc(item.task)}</div><div><span class="tag">${esc(item.owner)}</span>${item.deadline ? `<span class="tag hot">${esc(item.deadline)}</span>` : ""}</div>${source(item)}</div>`);
  html += section("bi-chat-square-text", "What people talked about", data.topics, (item) => `<div class="item plain"><div class="item-title">${esc(item.title)}</div><div class="item-detail">${esc(item.summary)}</div></div>`);
  $("#out").innerHTML = html;
  $("#out").scrollIntoView({ behavior: "smooth", block: "start" });
}
