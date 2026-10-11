"""Goals: long-horizon work that keeps going across many turns until it is verifiably done.

How a goal runs:
- Its state lives outside any transcript: objective, definition of done, a plan (checklist), and a structured progress
  record (done so far, next steps, decisions, files, blocker). Every continuation turn starts fresh from that state,
  so a goal can run for hundreds of steps without drowning in its own context.
- After each turn the engine schedules the next one: immediately, or after `check_every` / a requested delay (that is
  how monitoring loops work: "check the deploy every 10 minutes until it's green").
- The agent can't just declare victory. Marking a goal done sends the evidence to an independent verifier that
  checks it against the definition of done (and peeks at the files it cites). Rejected -> the goal continues with
  the verifier's feedback.
- Budgets (max turns), failure backoff and blocked states stop runaway loops; the owner is told when a goal finishes,
  blocks, or runs out of budget.

Wake-ups are the lightweight sibling: "wake me in 30 minutes to check X", one-shot.
"""

import json
import logging
import re

from .db import now
from .llm import LLMError

log = logging.getLogger("drok.goals")

MAX_FAILURES = 3
EVENTS_IN_BRIEF = 8

VERIFIER_SYSTEM = """You verify whether an AI agent has really completed a goal. Be strict but fair: judge only \
against the definition of done, using the evidence and file excerpts provided. Claims without evidence don't count. \
Reply with JSON only: {"verdict": "done" | "not_done", "reason": "<one or two sentences; if not done, say exactly \
what is missing>"}."""


class Goals:
    def __init__(self, app):
        self.app = app
        self.db = app.db

    def get(self, gid: int) -> dict | None:
        return self.db.one("SELECT * FROM goals WHERE id = ?", gid)

    def events(self, gid: int, limit: int = 50) -> list[dict]:
        return list(reversed(self.db.all("SELECT * FROM goal_events WHERE goal_id = ? ORDER BY id DESC LIMIT ?",
                                          gid, limit)))

    def event(self, gid: int, kind: str, text: str) -> None:
        self.db.insert("goal_events", goal_id=gid, kind=kind, text=text[:4000], created_at=now())
        self.app.bus.publish({"type": "goal", "goal_id": gid, "kind": kind})

    def active_for(self, agent_id: str) -> list[dict]:
        return self.db.all("SELECT * FROM goals WHERE agent_id = ? AND status IN ('active','blocked','paused') "
                           "ORDER BY id", agent_id)

    # ---------- lifecycle ----------
    def create(self, agent_id: str, title: str, objective: str, done_when: str = "", max_turns: int = 30,
               check_every_minutes: int = 0, created_by: str = "owner") -> dict:
        if not self.app.agent(agent_id):
            raise ValueError(f"No agent '{agent_id}'")
        conv = self.app.dm(agent_id)["id"]
        gid = self.db.insert(
            "goals", agent_id=agent_id, conversation_id=conv, title=title.strip()[:200], objective=objective.strip(),
            done_when=done_when.strip(), max_turns=max(1, min(int(max_turns or 30), 500)),
            check_every=max(0, int(check_every_minutes or 0)) * 60, next_run_at=now(), created_by=created_by,
            created_at=now(), updated_at=now(),
        )
        self.event(gid, "created", f"Goal set by {created_by}: {objective[:500]}")
        self.app.runtime.post(conv, agent_id, f"**Goal #{gid}: {title}**\n\n{objective}", kind="goal",
                              meta={"goal_id": gid, "event": "created"})
        self.tick()
        return self.get(gid)

    def set_status(self, gid: int, status: str, note: str = "") -> None:
        goal = self.get(gid)
        if not goal:
            raise ValueError(f"No goal #{gid}")
        values = {"status": status, "updated_at": now()}
        if status in ("done", "failed", "cancelled"):
            values["finished_at"] = now()
        if status == "active":
            values |= {"next_run_at": now(), "failures": 0}
        self.db.update("goals", gid, **values)
        self.event(gid, "status", f"{status}{': ' + note if note else ''}")
        if status in ("done", "blocked", "failed"):
            agent = self.app.agent(goal["agent_id"]) or {"name": goal["agent_id"]}
            self.app.bus.publish({"type": "notice", "text": f"Goal #{gid} '{goal['title']}' ({agent['name']}) is "
                                  f"{status}{': ' + note[:600] if note else ''}"})
        if status == "active":
            self.tick()

    def extend(self, gid: int, turns: int) -> None:
        goal = self.get(gid)
        self.db.update("goals", gid, max_turns=goal["max_turns"] + max(1, turns), updated_at=now())
        self.set_status(gid, "active", f"budget +{turns} turns")

    def update(self, gid: int, agent_id: str, **fields) -> str:
        """The agent's progress report. Returns what the agent should know next."""
        goal = self.get(gid)
        if not goal or goal["agent_id"] != agent_id:
            raise ValueError(f"Goal #{gid} isn't yours")
        state = dict(goal["state"])
        for key in ("progress", "next_steps", "decisions", "files", "blocker"):
            if fields.get(key):
                state[key] = fields[key]
        values = {"state": state, "updated_at": now()}
        if fields.get("plan"):
            values["plan"] = _norm_plan(fields["plan"])
        if fields.get("done_when") and not goal["done_when"]:
            values["done_when"] = fields["done_when"]
        if fields.get("check_again_in_minutes"):
            values["next_run_at"] = now() + int(fields["check_again_in_minutes"]) * 60
            state["wait_reason"] = fields.get("progress", "")[:200]
        self.db.update("goals", gid, **values)
        if fields.get("progress"):
            self.event(gid, "progress", fields["progress"])
        status = fields.get("status")
        if status == "blocked":
            reason = fields.get("blocker") or fields.get("progress") or "needs the owner"
            self.set_status(gid, "blocked", reason)
            return "Goal marked blocked; the owner has been told. End your turn."
        if status == "done":
            return "Completion claimed - the verifier will check your evidence after this turn ends."
        if status == "active" and goal["status"] == "blocked":
            self.set_status(gid, "active", "unblocked by the agent")
            return "Goal reactivated; work continues in the next turn."
        return "Goal state saved."

    # ---------- engine ----------
    def tick(self) -> None:
        """Start the next continuation of every active goal that is due and not already being worked on."""
        for g in self.db.all("SELECT * FROM goals WHERE status = 'active' AND (next_run_at IS NULL OR next_run_at <= ?)",
                             now()):
            open_turn = self.db.one(
                "SELECT id FROM turns WHERE agent_id = ? AND status IN ('queued','running','waiting') "
                "AND json_extract(meta, '$.goal_id') = ?", g["agent_id"], g["id"])
            if open_turn:
                continue
            if g["turns_used"] >= g["max_turns"]:
                self.set_status(g["id"], "blocked", f"used its budget of {g['max_turns']} turns. "
                                f"Progress: {g['state'].get('progress', 'none recorded')[:400]}")
                continue
            self.db.update("goals", g["id"], next_run_at=None)
            self.app.runtime.enqueue(g["agent_id"], g["conversation_id"], self.brief(g), "goal",
                                     {"goal_id": g["id"]})
        for w in self.db.all("SELECT * FROM wakeups WHERE fired = 0 AND due_at <= ?", now()):
            self.db.update("wakeups", w["id"], fired=1)
            if self.app.agent(w["agent_id"]):
                self.app.runtime.enqueue(w["agent_id"], w["conversation_id"],
                                         f"[Wake-up you scheduled]: {w['note']}", "wakeup", {"wakeup_id": w["id"]})

    def brief(self, g: dict) -> str:
        plan = "\n".join(f"- [{ {'done': 'x', 'in_progress': '~', 'blocked': '!'}.get(p['status'], ' ') }] "
                         f"{p['id']}. {p['text']}" for p in g["plan"]) or "(no plan yet - make one with `plan`)"
        st = g["state"]
        recent = "\n".join(f"- {e['kind']}: {e['text'][:400]}" for e in self.events(g["id"], EVENTS_IN_BRIEF))
        return (
            f"[Goal #{g['id']}: {g['title']} — turn {g['turns_used'] + 1} of {g['max_turns']}]\n\n"
            f"OBJECTIVE:\n{g['objective']}\n\nDONE WHEN:\n{g['done_when'] or '(not defined yet - define it with goal_update)'}\n\n"
            f"PLAN:\n{plan}\n\n"
            f"PROGRESS SO FAR:\n{st.get('progress', '(nothing yet)')}\n\nNEXT STEPS:\n{st.get('next_steps', '(decide)')}\n\n"
            f"DECISIONS:\n{st.get('decisions', '-')}\n\nFILES:\n{st.get('files', '-')}\n\nRECENT EVENTS:\n{recent}\n\n"
            "Continue the goal from where it stands. Work in solid steps and verify as you go. Before this turn "
            "ends, call goal_update with progress, next_steps and anything decided. If you need the owner, set "
            "status=blocked with the exact question. If you must wait for something external, set "
            "check_again_in_minutes. When the definition of done is met, set status=done with concrete evidence "
            "(paths, outputs, links) - an independent verifier will check it."
        )

    async def turn_finished(self, turn: dict, status: str) -> None:
        gid = turn["meta"].get("goal_id")
        goal = self.get(gid) if gid else None
        if not goal:
            return
        values = {"turns_used": goal["turns_used"] + 1, "updated_at": now()}
        if status == "cancelled":
            self.db.update("goals", gid, **values)
            if goal["status"] == "active":
                self.set_status(gid, "paused", "stopped by the owner")
            return
        if status == "failed":
            failures = goal["failures"] + 1
            self.db.update("goals", gid, **values, failures=failures, next_run_at=now() + 60 * 2 ** failures)
            if failures >= MAX_FAILURES:
                self.set_status(gid, "blocked", f"{failures} turns in a row failed (model/provider errors)")
            return
        values["failures"] = 0
        claimed = _claimed_done(turn)
        self.db.update("goals", gid, **values)
        goal = self.get(gid)
        if goal["status"] != "active":
            return
        if claimed is not None:
            ok, reason = await self.verify(goal, claimed)
            self.event(gid, "verify", f"{'PASSED' if ok else 'REJECTED'}: {reason}")
            if ok:
                self.set_status(gid, "done", claimed[:600])
                self.app.runtime.post(goal["conversation_id"], goal["agent_id"],
                                      f"Goal #{gid} verified done. {reason}", kind="goal",
                                      meta={"goal_id": gid, "event": "done"})
                return
            self.app.runtime.post(goal["conversation_id"], goal["agent_id"],
                                  f"Verifier: goal #{gid} isn't done yet - {reason}", kind="goal",
                                  meta={"goal_id": gid, "event": "rejected"})
        if goal["next_run_at"] is None:  # the agent didn't ask to wait: continue now, or at the goal's cadence
            self.db.update("goals", gid, next_run_at=now() + goal["check_every"])
        self.tick()

    async def verify(self, goal: dict, evidence: str) -> tuple[bool, str]:
        if not goal["done_when"]:
            return False, "No definition of done was set. Set done_when with goal_update, then claim done again."
        excerpts = []
        root = self.app.settings.workspace
        for path in list(dict.fromkeys(re.findall(r"/workspace/[\w./\-]+", evidence)))[:3]:
            real = (root / path.removeprefix("/workspace/")).resolve()
            if root not in real.parents:
                continue
            if real.is_file():
                excerpts.append(f"--- {path} ({real.stat().st_size} bytes) ---\n{real.read_text(errors='replace')[:1500]}")
            else:
                excerpts.append(f"--- {path}: DOES NOT EXIST ---")
        prompt = (f"OBJECTIVE:\n{goal['objective']}\n\nDEFINITION OF DONE:\n{goal['done_when']}\n\n"
                  f"AGENT'S EVIDENCE:\n{evidence[:4000]}\n\nRECORDED PROGRESS:\n"
                  f"{goal['state'].get('progress', '')[:2000]}\n\nFILE EXCERPTS:\n" + ("\n".join(excerpts) or "(none cited)"))
        agent = self.app.agent(goal["agent_id"])
        try:
            reply = await self.app.llm.text(self.app.aux_model(agent), VERIFIER_SYSTEM, prompt, agent_id=agent["id"])
            m = re.search(r"\{.*\}", reply, re.S)
            verdict = json.loads(m.group(0)) if m else {}
        except (LLMError, ValueError) as e:
            return False, f"verifier unavailable ({e.__class__.__name__}); will re-check"
        return verdict.get("verdict") == "done", str(verdict.get("reason", ""))[:600]

    def wake(self, agent_id: str, conversation_id: int, minutes: float, note: str) -> int:
        return self.db.insert("wakeups", agent_id=agent_id, conversation_id=conversation_id,
                              due_at=now() + max(1.0, float(minutes)) * 60, note=note[:1000], created_at=now())


def _norm_plan(plan) -> list[dict]:
    if isinstance(plan, str):
        plan = [line.strip("-*• ").strip() for line in plan.splitlines() if line.strip()]
    out = []
    for n, p in enumerate(plan, 1):
        if isinstance(p, str):
            p = {"text": p}
        status = p.get("status", "pending")
        out.append({"id": int(p.get("id", n)), "text": str(p.get("text", ""))[:300],
                    "status": status if status in ("pending", "in_progress", "done", "blocked") else "pending"})
    return out[:40]


def _claimed_done(turn: dict) -> str | None:
    """The evidence from a goal_update(status=done) call in this turn, if the agent claimed completion."""
    msgs = json.loads(json.dumps(turn.get("llm_messages") or []))
    for m in msgs:
        for c in m.get("tool_calls") or []:
            if c["function"]["name"] != "goal_update":
                continue
            try:
                args = json.loads(c["function"]["arguments"] or "{}")
            except ValueError:
                continue
            if args.get("status") == "done":
                return str(args.get("evidence") or args.get("progress") or "")
    return None
