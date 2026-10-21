// The studio agent's widget: a chat with the studio's AI agent, in two sizes. Collapsed, it is a labelled button in
// the corner, always there; a message the agent sends while it is collapsed pops up above the button for 20 seconds
// (a preview), then waits behind the button's count. Expanded, it is the whole conversation, every visit, with a
// composer and, under the agent's open offer, one form. Everything drawn comes from the server's thread and one tab
// flag (expanded). Each page of the site loads the widget anew, so the tab keeps a copy of the last thread, the draft
// and the scroll: the next page draws from them at once, without replaying the entrances, then catches up.
(() => {
  // --- who, and what they do on the page ---------------------------------------------------------------------------
  const store = (s, k, v) => { try { if (v === undefined) return s.getItem(k); if (v === null) s.removeItem(k); else s.setItem(k, v); } catch (_) { return null; } };
  const newId = () => (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2) + Date.now());
  let visitor = store(localStorage, "fw_visitor"); if (!visitor) { visitor = newId(); store(localStorage, "fw_visitor", visitor); }
  const opened = Date.now(); let deepest = 0;
  const params = new URLSearchParams(location.search);
  const base = {
    page: location.pathname, title: document.title.split(" — ")[0],
    device: /Mobi/.test(navigator.userAgent) ? "mobile" : "desktop",
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
  };
  const post = (path, body, beacon = false) => {
    const json = JSON.stringify(body);
    if (beacon && navigator.sendBeacon) { navigator.sendBeacon(path, new Blob([json], { type: "application/json" })); return Promise.resolve(null); }
    return fetch(path, { method: "POST", headers: { "content-type": "application/json" }, body: json });
  };
  const touch = (kind, extra = {}, who = {}, beacon = false) =>
    post("/api/events", { visitor, ...who, touch: { ...base, kind, at: new Date().toISOString(), ...extra } }, beacon);

  window.fwSignal = (label, text = "", website = "") => touch("signal", { label, text }, website ? { website } : {});
  // the page's own field asks the agent for something ("Roast my site"): a message to it, answered in the chat
  window.fwAsk = (label, text = "", website = "") => say(text, label, website);

  const ref = document.referrer && !document.referrer.startsWith(location.origin) ? new URL(document.referrer).hostname : "";
  touch("view", { referrer: ref, campaign: params.get("utm_campaign") || "" });
  addEventListener("scroll", () => {
    const h = document.documentElement; deepest = Math.max(deepest, Math.round(100 * (h.scrollTop + innerHeight) / h.scrollHeight));
  }, { passive: true });
  let readSent = false;
  addEventListener("pagehide", () => { if (readSent) return; readSent = true;
    touch("read", { seconds: Math.round((Date.now() - opened) / 1000), scroll: deepest }, {}, true); });
  let leavingSent = 0;
  document.addEventListener("mouseout", e => {
    if (!e.relatedTarget && e.clientY < 8 && Date.now() - leavingSent > 30000) { leavingSent = Date.now(); touch("leaving", { label: "pointer to the tab bar" }); }
  });
  setInterval(() => { if (document.visibilityState === "visible") touch("presence"); }, 20000);
  const lingered = new Set();  // staying on one thing for a few seconds: once per thing per page
  document.querySelectorAll("[data-track]:not(button)").forEach(t => {
    let timer = 0;
    t.addEventListener("pointerenter", () => { if (!lingered.has(t)) timer = setTimeout(() => { lingered.add(t); touch("linger", { label: t.dataset.track }); }, 3500); });
    t.addEventListener("pointerleave", () => clearTimeout(timer));
  });
  document.addEventListener("click", e => {
    const el = e.target.closest("[data-track],a"); if (!el || e.composedPath().some(n => n.id === "fw-agent")) return;
    touch("click", { label: el.dataset.track || el.textContent.trim().slice(0, 60) });
  });

  // --- state ---------------------------------------------------------------------------------------------------------
  const PREVIEW_MS = 20000, MAX_PREVIEWS = 3;
  let thread = { messages: [], read_through: -1, activity: null, open_offer: null, now: new Date().toISOString() };
  let fetchedAt = Date.now();
  try {  // the last page's copy: the server's thread as this tab last had it
    const kept = JSON.parse(store(sessionStorage, "fw_thread") || "null");
    if (kept && kept.visitor === visitor) { thread = kept.thread; fetchedAt = kept.fetchedAt; }
  } catch (_) {}
  let expanded = store(sessionStorage, "fw_open") === "1";
  let pending = [];  // what they sent, shown at once until the thread has it: {key, text, id, failed}
  const sentForms = new Set();  // offers whose form was just sent from this tab, before the thread says so

  const serverNow = () => Date.parse(thread.now) + (Date.now() - fetchedAt);
  const unread = () => thread.messages.filter(m => m.sender === "agent" && m.id > thread.read_through && !m.dismissed);
  const previews = () => expanded ? [] : unread().filter(m => serverNow() - Date.parse(m.at) < PREVIEW_MS).slice(-MAX_PREVIEWS);

  // --- the DOM: built once, redrawn from state ---------------------------------------------------------------------
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  // one icon per state, drawn on a 20-unit grid in the text colour: what the agent is doing, at a glance
  const PATHS = {
    talk: ["M4 4.5h12A1.5 1.5 0 0 1 17.5 6v7a1.5 1.5 0 0 1-1.5 1.5H9.5L6 17.5v-3H4A1.5 1.5 0 0 1 2.5 13V6A1.5 1.5 0 0 1 4 4.5z"],
    writing: ["M12.5 3.5l3 3-8.5 8.5-3.5.5.5-3.5z", "M11 5l3 3", "M3 18h6"],
    thinking: ["M2.5 10s3-5.5 7.5-5.5S17.5 10 17.5 10s-3 5.5-7.5 5.5S2.5 10 2.5 10z", "M10 8a2 2 0 1 1 0 4 2 2 0 0 1 0-4z"],
    researching: ["M8.5 3.5a5 5 0 1 1 0 10 5 5 0 0 1 0-10z", "M12.2 12.2l4.3 4.3"],
    close: ["M5 8l5 5 5-5"],
  };
  const icon = name => {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 20 20"); svg.setAttribute("aria-hidden", "true"); svg.classList.add("fw-icon", `fw-icon-${name}`);
    for (const d of PATHS[name]) { const path = document.createElementNS(svg.namespaceURI, "path"); path.setAttribute("d", d); svg.append(path); }
    return svg;
  };
  const DOING = { thinking: "Thinking…", writing: "Writing…", researching: "Looking things up…" };
  const root = el("div"); root.id = "fw-agent";
  const stack = el("div", "fw-previews"); stack.setAttribute("aria-live", "polite");
  const panel = el("section", "fw-panel"); panel.setAttribute("aria-label", "Chat with the studio's agent");
  const head = el("header", "fw-head"); const who = el("div", "fw-who"); const names = el("div");
  const status = el("small", "fw-status", "AI agent · Fieldwork Studio");
  names.append(el("b", "", "The studio's agent"), status);
  who.append(el("span", "fw-avatar", "F"), names); head.append(who);
  const lines = el("div", "fw-lines");
  const compose = el("form", "fw-compose");
  const input = el("textarea"); input.rows = 1; input.placeholder = "Ask anything about your marketing"; input.id = "fw-input";
  input.setAttribute("aria-label", "Message the studio's agent");
  const sendBtn = el("button", "fw-send", "Send"); sendBtn.type = "submit";
  compose.append(input, sendBtn);
  panel.append(head, lines, compose);
  const button = el("button", "fw-button"); button.type = "button";
  const glyph = el("span", "fw-glyph"); const label = el("span", "fw-label"); const badge = el("span", "fw-badge");
  button.append(glyph, label, badge);
  root.append(stack, panel, button);
  let restoring = true;  // the first draw on a page: what was on screen on the last page arrives already there
  if (expanded) panel.classList.add("fw-kept");
  document.body.appendChild(root);
  input.value = store(sessionStorage, "fw_draft") || "";

  function preview(m) {
    const p = el("aside", `fw-preview${restoring ? " fw-kept" : ""}`); p.dataset.id = m.id;
    const text = el("p", "fw-said", m.text); text.tabIndex = 0;
    const actions = el("div", "fw-actions");
    const reply = el("button", "fw-reply", "Reply"); reply.type = "button";
    const x = el("button", "fw-x", "×"); x.type = "button"; x.setAttribute("aria-label", "Delete this message");
    actions.append(reply, x); p.append(text, actions);
    const go = () => expand(m.id);
    text.onclick = go; text.onkeydown = e => { if (e.key === "Enter") go(); }; reply.onclick = go;
    x.onclick = () => dismiss(m.id);
    return p;
  }

  function form(m) {
    const taken = m.offer_taken || sentForms.has(m.id), person = m.offer.kind === "person";
    if (taken) return el("div", "fw-sent", person ? "Sent. Someone at the studio will be in touch." : "Sent. Check your inbox.");
    const f = el("form", "fw-form");
    f.append(el("div", "fw-offer", person ? `Want someone at the studio to get in touch about ${m.offer.what}?` : `Want ${m.offer.what}?`));
    for (const [name, type, place, auto, req] of [["name", "text", "Your name", "name", true], ["email", "email", "Work email", "email", true], ["website", "text", "Your website (optional)", "url", false]]) {
      const i = el("input"); i.name = name; i.type = type; i.placeholder = place; i.autocomplete = auto; i.required = req; i.id = `fw-${name}-${m.id}`;
      f.append(i);
    }
    const b = el("button", "", person ? "Have them get in touch" : "Email it to me"); b.type = "submit"; f.append(b);
    f.onsubmit = e => { e.preventDefault(); const d = new FormData(f);
      touch("identify", { label: "introduced themselves" }, { name: d.get("name"), email: d.get("email"), website: d.get("website") || "" });
      sentForms.add(m.id); render(); };
    return f;
  }

  let drawn = "";  // what the panel's lines were last drawn from: redrawn only when it changes, so a form keeps its typing
  function drawLines() {
    const seen = new Set(thread.messages.map(m => m.id));
    const key = JSON.stringify([thread.messages.map(m => [m.id, m.offer_taken, m.dismissed]), thread.open_offer, thread.activity,
      pending.map(p => [p.key, p.failed, p.id != null && seen.has(p.id)]), [...sentForms]]);
    if (key === drawn) return; drawn = key;
    const nearEnd = lines.scrollHeight - lines.scrollTop - lines.clientHeight < 60;
    lines.replaceChildren();
    if (!thread.messages.length && !pending.length) lines.append(el("p", "fw-intro", "Ask anything about your marketing, or about the studio's work. I'm the studio's AI agent, and I'll answer here."));
    for (const m of thread.messages) {
      const line = el("div", `fw-line ${m.sender === "agent" ? "agent" : "you"}`, m.asked_through ? `${m.asked_through}: ${m.text}` : m.text);
      line.dataset.id = m.id; lines.append(line);
      if (m.offer && (m.id === thread.open_offer || m.offer_taken)) lines.append(form(m));
    }
    for (const p of pending) {
      if (p.id != null && seen.has(p.id)) continue;
      const line = el("div", `fw-line you pending${p.failed ? " failed" : ""}`, p.text);
      if (p.failed) { const r = el("button", "fw-retry", "Not sent. Retry"); r.type = "button"; r.onclick = () => { pending = pending.filter(x => x !== p); say(p.raw, p.asked, p.website); }; line.append(r); }
      lines.append(line);
    }
    if (thread.activity) { const t = el("div", "fw-working"); t.setAttribute("role", "status"); t.append(icon(thread.activity), el("span", "", DOING[thread.activity])); lines.append(t); }
    if (nearEnd) lines.scrollTop = lines.scrollHeight;
  }

  function render() {
    const shown = previews();
    const ids = shown.map(m => m.id);
    [...stack.children].forEach(c => { if (!ids.includes(+c.dataset.id)) c.remove(); });
    shown.forEach(m => { if (!stack.querySelector(`[data-id="${m.id}"]`)) stack.append(preview(m)); });  // newest nearest the button
    stack.hidden = expanded || !shown.length;
    panel.hidden = !expanded;
    if (expanded) drawLines();
    const count = expanded ? 0 : unread().length;
    const state = expanded ? "close" : thread.activity || "talk";
    if (glyph.dataset.state !== state) { glyph.dataset.state = state; glyph.replaceChildren(icon(state)); }
    label.textContent = expanded ? "Close" : thread.activity ? DOING[thread.activity] : "Ask the studio's agent";
    status.textContent = thread.activity ? DOING[thread.activity] : "AI agent · Fieldwork Studio";
    badge.textContent = count; badge.hidden = !count;
    button.classList.toggle("busy", !!thread.activity && !expanded);
    button.setAttribute("aria-expanded", String(expanded));
    button.setAttribute("aria-label", expanded ? "Close the chat" : count ? `Open the chat, ${count} unread` : "Open the chat with the studio's agent");
    if (expanded) markRead();
  }

  // --- what they do with it -----------------------------------------------------------------------------------------
  function markRead() {
    if (document.visibilityState !== "visible") return;
    const last = thread.messages.length ? thread.messages[thread.messages.length - 1].id : -1;
    if (last > thread.read_through) { thread.read_through = last; post("/api/thread/read", { visitor, through: last }); }
  }
  function expand(at) {
    const first = at != null ? at : (unread()[0] || {}).id;
    if (!expanded) { expanded = true; store(sessionStorage, "fw_open", "1"); touch("chat_opened"); }
    drawn = ""; render();
    const target = first != null && lines.querySelector(`[data-id="${first}"]`);
    if (target) target.scrollIntoView({ block: "center" }); else lines.scrollTop = lines.scrollHeight;
    input.focus();
  }
  function collapse() { panel.classList.remove("fw-kept"); expanded = false; store(sessionStorage, "fw_open", null); touch("chat_closed"); render(); button.focus(); }
  button.onclick = () => (expanded ? collapse() : expand());
  document.addEventListener("keydown", e => { if (e.key === "Escape" && expanded) collapse(); });
  function dismiss(id) {
    const m = thread.messages.find(x => x.id === id); if (m) m.dismissed = true;
    post("/api/thread/dismiss", { visitor, id }); render();
  }
  async function say(text, asked = "", website = "") {
    text = text.trim(); if (!text) return;
    const p = { key: newId(), raw: text, text: asked ? `${asked}: ${text}` : text, asked, website, id: null, failed: false };
    pending.push(p); render();
    try {
      const r = await post("/api/thread/send", { visitor, text, asked_through: asked, website });
      if (!r.ok) throw new Error(String(r.status));
      p.id = (await r.json()).id;
      refresh();
    } catch (_) { p.failed = true; }
    render();
  }
  const fit = () => { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 120) + "px"; };
  input.oninput = fit;
  input.onkeydown = e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); compose.requestSubmit(); } };
  compose.onsubmit = e => { e.preventDefault(); const t = input.value; input.value = ""; fit(); store(sessionStorage, "fw_draft", null); say(t); };
  input.addEventListener("input", () => store(sessionStorage, "fw_draft", input.value || null));
  addEventListener("pagehide", () => store(sessionStorage, "fw_scroll", expanded ? String(lines.scrollTop) : null));
  document.addEventListener("visibilitychange", () => { if (expanded) markRead(); });

  // --- the thread, from the server ----------------------------------------------------------------------------------
  async function refresh() {
    try {
      const r = await fetch(`/api/thread?v=${encodeURIComponent(visitor)}`, { cache: "no-store" });
      const t = await r.json();
      t.read_through = Math.max(t.read_through, thread.read_through);  // a read this tab just sent may not be back yet
      thread = t; fetchedAt = Date.now();
      store(sessionStorage, "fw_thread", JSON.stringify({ visitor, thread, fetchedAt }));
      pending = pending.filter(p => p.failed || p.id == null || !thread.messages.some(m => m.id === p.id));
    } catch (_) {}
    render();
  }
  (async function poll() { await refresh(); setTimeout(poll, thread.activity ? 800 : 1500); })();
  setInterval(() => { if (!expanded) render(); }, 1000);  // previews leave at 20 seconds
  render(); fit();
  const scroll = store(sessionStorage, "fw_scroll");
  if (expanded && scroll != null) lines.scrollTop = +scroll;
  restoring = false;
})();
