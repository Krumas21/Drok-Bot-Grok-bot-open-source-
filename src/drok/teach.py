"""Teach a task: the owner performs a browser workflow once on an agent's screen; Drok records the DOM-level steps
(clicks, inputs, keys, navigations - password fields masked) and drafts a reusable skill from them."""

import asyncio
import json
import logging
import time

from .llm import LLMError

log = logging.getLogger("drok.teach")

MAX_SECONDS = 600

RECORDER_JS = r"""(() => {
  if (window.__drokRecInstalled) return; window.__drokRecInstalled = true;
  const send = ev => { try { window.__drokRec && window.__drokRec(ev); } catch (e) {} };
  const desc = el => {
    if (!el || !el.tagName) return '';
    const tag = el.tagName.toLowerCase();
    const label = (el.getAttribute('aria-label') || el.innerText || el.placeholder || el.title || el.name || '')
      .trim().replace(/\s+/g, ' ').slice(0, 60);
    const id = el.id ? `#${el.id}` : '';
    const type = el.type ? `[type=${el.type}]` : '';
    return `${tag}${id}${type} "${label}"`;
  };
  document.addEventListener('click', e => {
    const el = e.target.closest('a,button,input,select,textarea,label,[role]') || e.target;
    send({type: 'click', target: desc(el), url: location.href});
  }, true);
  document.addEventListener('change', e => {
    const el = e.target; const secret = el.type === 'password' || /otp|code|pin|cvc|card/i.test(el.name || '');
    send({type: 'input', target: desc(el), value: secret ? '<secret - ask the owner>' : String(el.value).slice(0, 200),
          url: location.href});
  }, true);
  document.addEventListener('keydown', e => {
    if (['Enter', 'Tab', 'Escape'].includes(e.key)) send({type: 'key', key: e.key, url: location.href});
  }, true);
})();"""

DRAFT_SYSTEM = """You turn a recorded browser demonstration into a reusable skill for an AI agent that controls a \
browser through a DOM tool (goto, snapshot, click/fill by element, press). Write Markdown with sections: When to use, \
Inputs and access needed, Steps (numbered; describe elements by visible label, not CSS ids), How to validate, What to \
return, Needs owner approval (anything that sends, buys, publishes or deletes), If something breaks (layout changed, \
logged out, missing data -> stop and report, never guess). Generalize values the owner typed into named inputs where \
they look task-specific. First line: a one-sentence description starting with 'DESCRIPTION:'."""


class Teacher:
    def __init__(self, app):
        self.app = app
        self.sessions: dict[int, dict] = {}
        self._bound: set[int] = set()

    def active(self, screen: int) -> dict | None:
        return self.sessions.get(screen)

    async def start(self, screen: int, agent_id: str, goal: str) -> None:
        if screen in self.sessions:
            raise ValueError("Already recording on this screen")
        ctx = await self.app.browsers.context(screen)
        if screen not in self._bound:
            await ctx.expose_binding("__drokRec", lambda source, ev: self._record(screen, ev))
            await ctx.add_init_script(RECORDER_JS)
            ctx.on("page", lambda p: self._watch_page(screen, p))
            for p in ctx.pages:
                self._watch_page(screen, p)
            self._bound.add(screen)
        for p in ctx.pages:
            try:
                await p.evaluate(RECORDER_JS)
            except Exception as e:  # e.g. chrome:// pages refuse scripts
                log.debug("recorder inject skipped: %s", e)
        self.sessions[screen] = {"agent_id": agent_id, "goal": goal, "events": [], "started": time.time(),
                                 "timer": asyncio.get_running_loop().call_later(
                                     MAX_SECONDS, lambda: asyncio.ensure_future(self.stop(screen)))}
        self.app.bus.publish({"type": "teach", "screen": screen, "recording": True})

    def _watch_page(self, screen: int, page) -> None:
        page.on("framenavigated", lambda f: f == page.main_frame and self._record(screen, {"type": "navigate",
                                                                                       "url": f.url}))

    def _record(self, screen: int, ev: dict) -> None:
        s = self.sessions.get(screen)
        if not s:
            return
        ev["t"] = round(time.time() - s["started"], 1)
        if s["events"] and ev.get("type") == "navigate" and s["events"][-1].get("url") == ev.get("url"):
            return
        s["events"].append(ev)

    async def stop(self, screen: int) -> str | None:
        s = self.sessions.pop(screen, None)
        if not s:
            return None
        s["timer"].cancel()
        self.app.bus.publish({"type": "teach", "screen": screen, "recording": False})
        agent = self.app.agent(s["agent_id"]) or self.app.orchestrator()
        if not s["events"]:
            return None
        log_lines = "\n".join(json.dumps(e, ensure_ascii=False) for e in s["events"][:400])
        try:
            draft = await self.app.llm.text(agent["model"], DRAFT_SYSTEM,
                                            f"Goal stated by the owner: {s['goal']}\n\nRecorded events:\n{log_lines}",
                                            agent_id=agent["id"])
        except LLMError as e:
            self.app.runtime.post(self.app.dm(agent["id"])["id"], "system", f"Couldn't draft the skill: {e}",
                                  kind="status")
            return None
        first, _, rest = draft.partition("\n")
        description = first.removeprefix("DESCRIPTION:").strip() if first.startswith("DESCRIPTION:") else s["goal"]
        body = rest if first.startswith("DESCRIPTION:") else draft
        slug = self.app.skills.save(s["goal"][:50] or "taught-task", description, body, draft=True)
        conv = self.app.dm(agent["id"])["id"]
        self.app.runtime.post(conv, "system", f"Drafted skill '{slug}' from your demonstration "
                              f"({len(s['events'])} steps). Review it on the Skills page, then remove draft: true.",
                              kind="status", meta={"skill": slug})
        return slug
