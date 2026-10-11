"""Routines: scheduled (cron) or webhook-triggered work owned by one agent.

next_run_at lives in the DB, so restarts never lose or double-fire a schedule. Runs missed while the server was off
collapse into a single catch-up run.
"""

import asyncio
import logging
import secrets as pysecrets
from datetime import datetime
from zoneinfo import ZoneInfo

from croniter import croniter

from .db import now

log = logging.getLogger("drok.scheduler")

MIN_GAP = 300
MAX_PER_AGENT = 50
KEEP_RUNS = 20
TICK = 15


def next_run(cron: str, tz: str, after: float) -> float:
    return croniter(cron, datetime.fromtimestamp(after, ZoneInfo(tz))).get_next(float)


def validate(cron: str, tz: str) -> None:
    try:
        ZoneInfo(tz)
    except Exception as e:
        raise ValueError(f"Unknown timezone '{tz}'") from e
    if not croniter.is_valid(cron):
        raise ValueError(f"Invalid cron expression '{cron}'")
    it = croniter(cron, datetime.now(ZoneInfo(tz)))
    times = [it.get_next(float) for _ in range(30)]
    if min(b - a for a, b in zip(times, times[1:], strict=False)) < MIN_GAP:
        raise ValueError("Routines must be at least 5 minutes apart")


class Scheduler:
    def __init__(self, app):
        self.app = app
        self.db = app.db

    @staticmethod
    def fmt(ts: float | None, tz: str) -> str:
        if not ts:
            return "-"
        return datetime.fromtimestamp(ts, ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M %Z")

    def create(self, agent_id: str, name: str, instructions: str, cron: str = "", tz: str = "UTC",
               kind: str = "schedule", match: str = "") -> int:
        if self.db.one("SELECT COUNT(*) AS n FROM routines WHERE agent_id = ?", agent_id)["n"] >= MAX_PER_AGENT:
            raise ValueError(f"An agent can own at most {MAX_PER_AGENT} routines")
        values = {"agent_id": agent_id, "name": name.strip(), "instructions": instructions.strip(), "kind": kind,
                  "tz": tz, "match": match, "created_at": now()}
        if kind == "schedule":
            validate(cron, tz)
            values |= {"cron": cron, "next_run_at": next_run(cron, tz, now())}
        else:
            values["secret"] = pysecrets.token_urlsafe(24)
        rid = self.db.insert("routines", **values)
        self.app.bus.publish({"type": "routines"})
        return rid

    def update(self, rid: int, **changes) -> None:
        r = self.db.one("SELECT * FROM routines WHERE id = ?", rid)
        if not r:
            raise ValueError(f"No routine #{rid}")
        if "enabled" in changes:
            changes["enabled"] = int(bool(changes["enabled"]))
            changes["paused_reason"] = "" if changes["enabled"] else "by owner"
        cron, tz = changes.get("cron", r["cron"]), changes.get("tz", r["tz"])
        if r["kind"] == "schedule" and ("cron" in changes or "tz" in changes or changes.get("enabled")):
            validate(cron, tz)
            changes["next_run_at"] = next_run(cron, tz, now())
        if changes:
            self.db.update("routines", rid, **changes)
        self.app.bus.publish({"type": "routines"})

    def delete(self, rid: int) -> None:
        self.db.run("DELETE FROM routine_runs WHERE routine_id = ?", rid)
        self.db.run("DELETE FROM routines WHERE id = ?", rid)
        self.app.bus.publish({"type": "routines"})

    def fire(self, routine: dict, payload: str = "", test: bool = False) -> int:
        agent = self.app.agent(routine["agent_id"])
        if not agent:
            raise ValueError("Routine owner no longer exists")
        trigger = (f"[Routine '{routine['name']}' {'test run' if test else 'fired'} at "
                   f"{self.fmt(now(), routine['tz'])}]\n{routine['instructions']}")
        if payload:
            trigger += f"\n\nEvent payload:\n<untrusted source=\"webhook\">\n{payload[:8000]}\n</untrusted>"
        run_id = self.db.insert("routine_runs", routine_id=routine["id"], started_at=now())
        turn_id = self.app.runtime.enqueue(agent["id"], self.app.dm(agent["id"])["id"], trigger, "routine",
                                           {"routine_id": routine["id"], "run_id": run_id})
        self.db.update("routine_runs", run_id, turn_id=turn_id)
        self.db.update("routines", routine["id"], last_run_at=now())
        old = self.db.all("SELECT id FROM routine_runs WHERE routine_id = ? ORDER BY id DESC LIMIT -1 OFFSET ?",
                          routine["id"], KEEP_RUNS)
        for o in old:
            self.db.run("DELETE FROM routine_runs WHERE id = ?", o["id"])
        return run_id

    def turn_finished(self, turn: dict, status: str, result: str) -> None:
        run_id = turn["meta"].get("run_id")
        if run_id:
            self.db.update("routine_runs", run_id, status=status, result=result[:4000], finished_at=now())
            self.app.bus.publish({"type": "routines"})

    async def run(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("scheduler tick failed")
            await asyncio.sleep(TICK)

    def tick(self) -> None:
        t = now()
        for r in self.db.all("SELECT * FROM routines WHERE kind = 'schedule' AND enabled = 1 AND next_run_at <= ?", t):
            # Advance first: if firing crashes we skip one run instead of firing in a tight loop.
            self.db.update("routines", r["id"], next_run_at=next_run(r["cron"], r["tz"], t))
            try:
                self.fire(r)
            except ValueError as e:
                log.warning("routine %s not fired: %s", r["id"], e)
        self.check_absence(t)

    def check_absence(self, t: float) -> None:
        """Like Grok Bot: if the owner disappears, routines pause instead of running unattended forever."""
        days = self.app.settings.absence_days
        if days <= 0:
            return
        last = self.db.get_kv("last_user_activity")
        if last is None:
            self.db.set_kv("last_user_activity", t)
            return
        idle_days = (t - last) / 86400
        active = self.db.all("SELECT id FROM routines WHERE enabled = 1")
        if not active:
            return
        if idle_days >= days:
            self.db.run("UPDATE routines SET enabled = 0, paused_reason = 'owner absent' WHERE enabled = 1")
            self.app.bus.publish({"type": "notice", "text": f"Paused {len(active)} routines: no activity from you for "
                                  f"{days} days. Resume them on the Routines page."})
            self.app.bus.publish({"type": "routines"})
        elif idle_days >= days - 1 and not self.db.get_kv("absence_warned"):
            self.db.set_kv("absence_warned", True)
            self.app.bus.publish({"type": "notice", "text": f"Haven't heard from you in {int(idle_days)} days. Reply "
                                  "anything within a day to keep your routines running."})
