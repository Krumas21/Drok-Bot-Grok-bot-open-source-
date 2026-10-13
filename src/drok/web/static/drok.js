// Drok web client: live updates over SSE, markdown rendering, composer, approvals. No framework, no build.
(() => {
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const page = document.body.dataset;

  // ---- theme ----
  const root = document.documentElement;
  try { const t = localStorage.getItem("drok-theme"); if (t) root.dataset.theme = t; } catch (e) {}
  $$("[data-theme-toggle]").forEach(b => b.addEventListener("click", () => {
    const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("drok-theme", root.dataset.theme); } catch (e) {}
  }));
  $$("[data-rail-toggle]").forEach(b => b.addEventListener("click", () => document.body.classList.toggle("rail-open")));

  // ---- markdown ----
  // Every /workspace/... path an agent mentions becomes a link to the file (view or download).
  const WS_PATH = /\/workspace\/[^\s`'"<>()[\]{}]*[^\s`'"<>()[\]{}.,;:!?]/g;
  function linkPaths(el) {
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    const hits = [];
    while (walker.nextNode()) {
      const n = walker.currentNode;
      if (n.data.includes("/workspace/") && !n.parentElement.closest("a")) hits.push(n);
    }
    hits.forEach(n => {
      const frag = document.createDocumentFragment();
      let last = 0;
      for (const m of n.data.matchAll(WS_PATH)) {
        frag.append(n.data.slice(last, m.index));
        const a = document.createElement("a");
        a.href = "/files/" + m[0].slice("/workspace/".length).split("/").map(encodeURIComponent).join("/");
        a.target = "_blank"; a.textContent = m[0];
        frag.append(a);
        last = m.index + m[0].length;
      }
      frag.append(n.data.slice(last));
      n.replaceWith(frag);
    });
  }
  function renderMd(scope) {
    $$(".md[data-raw]", scope).forEach(el => {
      const raw = el.textContent;
      el.removeAttribute("data-raw");
      if (window.marked && window.DOMPurify) {
        el.innerHTML = DOMPurify.sanitize(marked.parse(raw, { breaks: true }));
        $$("a", el).forEach(a => { a.target = "_blank"; a.rel = "noopener"; });
      }
      linkPaths(el);
    });
  }
  renderMd(document);

  // ---- toasts ----
  function toast(html, ms = 6000) {
    let box = $(".toasts");
    if (!box) { box = document.createElement("div"); box.className = "toasts"; document.body.append(box); }
    const t = document.createElement("div");
    t.className = "toast"; t.innerHTML = html; box.append(t);
    setTimeout(() => t.remove(), ms);
  }
  const esc = s => String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // ---- timeline ----
  const timeline = $(".timeline");
  const inner = $(".timeline-inner");
  const convId = Number(page.conversation || 0);
  let lastId = Number(page.lastId || 0);
  const nearBottom = () => !timeline || timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 160;
  const toBottom = () => { if (timeline) timeline.scrollTop = timeline.scrollHeight; };
  toBottom();

  function markSameAuthor(scope) {
    let prev = null;
    $$(".msg", scope || inner).forEach(m => {
      m.classList.toggle("same-author", !!prev && prev.dataset.author === m.dataset.author && prev.classList.contains("text") && m.classList.contains("text"));
      prev = m;
    });
  }
  if (inner) markSameAuthor();

  let fetching = false, again = false;
  async function pull() {
    if (fetching) { again = true; return; }
    fetching = true;
    try {
      const stick = nearBottom();
      const r = await fetch(`/c/${convId}/messages?after=${lastId}`);
      const html = await r.text();
      if (html.trim()) {
        const tpl = document.createElement("template");
        tpl.innerHTML = html;
        const nodes = [...tpl.content.children];
        nodes.forEach(n => { if (!document.getElementById(n.id)) { n.classList.add("fresh"); inner.append(n); } });
        lastId = Math.max(lastId, ...nodes.map(n => Number(n.dataset.id)));
        renderMd(inner); bindApprovals(inner); markSameAuthor();
        $(".empty", inner)?.remove();
        if (stick) toBottom();
      }
    } finally {
      fetching = false;
      if (again) { again = false; pull(); }
    }
  }

  async function refreshMessage(id) {
    const el = document.getElementById(`m${id}`);
    if (!el) return;
    const html = await (await fetch(`/m/${id}`)).text();
    const tpl = document.createElement("template");
    tpl.innerHTML = html;
    const n = tpl.content.firstElementChild;
    if (n) { el.replaceWith(n); renderMd(n.parentElement); bindApprovals(n.parentElement); }
  }

  // ---- composer ----
  const form = $("#composer");
  if (form) {
    const ta = $("textarea", form);
    const grow = () => { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 260) + "px"; };
    ta.addEventListener("input", grow);
    ta.addEventListener("keydown", e => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
    });
    const picker = $("input[type=file]", form);
    const attached = $("[data-attached]");
    const showFiles = (note) => {
      const names = [...picker.files].map(f => f.name).join(", ");
      attached.textContent = note || (names ? `${names} · ` : "");
    };
    $("[data-attach]", form).addEventListener("click", () => picker.click());
    picker.addEventListener("change", () => showFiles());
    const convo = $(".convo");
    convo.addEventListener("dragover", e => {
      if (![...e.dataTransfer.types].includes("Files")) return;
      e.preventDefault(); convo.classList.add("dropping");
    });
    convo.addEventListener("dragleave", e => { if (!convo.contains(e.relatedTarget)) convo.classList.remove("dropping"); });
    convo.addEventListener("drop", e => {
      convo.classList.remove("dropping");
      if (!e.dataTransfer.files.length) return;
      e.preventDefault(); picker.files = e.dataTransfer.files; showFiles(); ta.focus();
    });
    form.addEventListener("submit", async e => {
      e.preventDefault();
      const text = ta.value.trim();
      const uploading = picker.files.length > 0;
      if (!text && !uploading) return;
      const body = new FormData(form);
      ta.value = ""; grow();
      if (uploading) showFiles("Uploading… ");
      const r = await fetch(form.action, { method: "POST", body });
      if (!r.ok) { toast("Couldn't send: " + esc(await r.text())); ta.value = text; showFiles(); return; }
      picker.value = ""; showFiles();
      pull(); toBottom();
    });
    ta.focus();
  }

  $$("[data-stop]").forEach(b => b.addEventListener("click", async () => {
    await fetch(`/agents/${b.dataset.stop}/stop`, { method: "POST" });
    toast("Stopping…", 2000);
  }));

  // ---- approvals (inline) ----
  function bindApprovals(scope) {
    $$("form[data-approval]", scope).forEach(f => {
      if (f.dataset.bound) return;
      f.dataset.bound = "1";
      f.addEventListener("submit", async e => {
        e.preventDefault();
        const btn = e.submitter;
        const body = new FormData(f);
        body.set("decision", btn.value);
        $$("button", f).forEach(b => b.disabled = true);
        const r = await fetch(f.action, { method: "POST", body, headers: { "x-requested-with": "fetch" } });
        if (!r.ok) { toast(esc(await r.text())); $$("button", f).forEach(b => b.disabled = false); return; }
        if (f.dataset.remove !== undefined) f.closest("[data-approval-row]")?.remove();
      });
    });
  }
  bindApprovals(document);

  // ---- answers to agent questions (option buttons) ----
  document.addEventListener("submit", async e => {
    const f = e.target.closest("form[data-answer]");
    if (!f) return;
    e.preventDefault();
    const r = await fetch(f.action, { method: "POST", body: new URLSearchParams(new FormData(f)) });
    if (!r.ok) { toast("Couldn't send: " + esc(await r.text())); return; }
    f.closest(".question")?.querySelectorAll("button").forEach(b => b.disabled = true);
    pull();
  });

  // ---- live events ----
  const statusWords = { idle: "idle", working: "working", waiting: "waiting on you", error: "error" };
  function setStatus(agentId, status) {
    $$(`[data-agent="${agentId}"]`).forEach(el => el.dataset.status = status);
    $$(`[data-status-text="${agentId}"]`).forEach(el => el.textContent = statusWords[status] || status);
  }
  function bumpBadge(delta) {
    $$("[data-pending]").forEach(b => {
      const n = Math.max(0, Number(b.dataset.count || 0) + delta);
      b.dataset.count = n; b.textContent = n;
    });
  }

  const es = new EventSource("/events");
  // Pages kept in the back/forward cache would otherwise each hold an SSE connection; six of them exhaust the
  // browser's per-host connection limit and the next page load hangs.
  addEventListener("pagehide", () => es.close());
  addEventListener("pageshow", e => { if (e.persisted) location.reload(); });
  es.onmessage = e => {
    const ev = JSON.parse(e.data);
    switch (ev.type) {
      case "message":
        if (ev.conversation_id === convId) pull();
        else if (ev.author !== "user" && ev.kind === "text") {
          $(`.station[data-conv="${ev.conversation_id}"]`)?.classList.add("has-unread");
        }
        break;
      case "message_updated":
        if (inner) refreshMessage(ev.id);
        break;
      case "status":
        setStatus(ev.agent_id, ev.status);
        break;
      case "approval":
        bumpBadge(1);
        if (!document.querySelector(`[data-approval-id="${ev.approval_id}"]`))
          toast(`<b>${esc(ev.agent_id)}</b> needs approval for <b>${esc(ev.tool)}</b>. <a href="/approvals">Review</a>`, 9000);
        if (page.page === "approvals") location.reload();
        break;
      case "approval_resolved":
        bumpBadge(-1);
        break;
      case "notice":
        toast(esc(ev.text), 12000);
        break;
      case "request":
        toast(`<b>${esc(ev.agent_id)}</b> needs you: ${esc(ev.reason)}`, 12000);
        break;
      case "teach":
        if (page.page === "computer") location.reload();
        break;
      case "goal":
        if (page.page === "goals" || page.page === "goal") location.reload();
        break;
      case "question":
        if (ev.conversation_id !== convId) toast(`<b>${esc(ev.agent_id)}</b> asks: ${esc(ev.question)}`, 12000);
        break;
    }
  };
})();
