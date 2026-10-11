"""Tools that touch Drok itself: memory, teammates, skills, routines, secrets and the owner."""

import re

from ..db import now
from . import Tool, ToolContext, i, params, s


async def memory_save(ctx: ToolContext, content: str, shared: bool = False) -> str:
    mid = ctx.app.db.insert("memories", agent_id=None if shared else ctx.agent["id"], content=content.strip(),
                            created_at=now())
    return f"Saved memory #{mid}{' (shared with the team)' if shared else ''}."


async def memory_search(ctx: ToolContext, query: str) -> str:
    words = re.findall(r"\w+", query)
    if not words:
        return "Empty query."
    fts = " OR ".join(f'"{w}"' for w in words)
    rows = ctx.app.db.all(
        "SELECT m.id, m.agent_id, m.content FROM memories_fts f JOIN memories m ON m.id = f.rowid "
        "WHERE memories_fts MATCH ? AND (m.agent_id = ? OR m.agent_id IS NULL) ORDER BY rank LIMIT 15",
        fts, ctx.agent["id"],
    )
    return "\n".join(f"[{r['id']}] {r['content']}" for r in rows) or "Nothing found."


async def memory_forget(ctx: ToolContext, memory_id: int) -> str:
    ctx.app.db.run("DELETE FROM memories WHERE id = ? AND (agent_id = ? OR agent_id IS NULL)", memory_id,
                   ctx.agent["id"])
    return f"Forgot #{memory_id}."


async def message_agent(ctx: ToolContext, agent: str, message: str) -> str:
    target = ctx.app.find_agent(agent)
    if not target:
        return f"No agent '{agent}'. Team: {', '.join(a['id'] for a in ctx.app.agents())}"
    if target["id"] == ctx.agent["id"]:
        return "That is you."
    return ctx.app.runtime.handoff(ctx.agent, target, message, ctx.conversation_id, ctx.turn["meta"].get("hops", 0))


async def skill_read(ctx: ToolContext, name: str) -> str:
    skill = ctx.app.skills.read(name)
    return skill["body"] if skill else f"No skill '{name}'."


async def skill_save(ctx: ToolContext, name: str, description: str, instructions: str) -> str:
    slug = ctx.app.skills.save(name, description, instructions)
    return f"Saved skill '{slug}'. The whole team can use it."


async def routine_create(ctx: ToolContext, name: str, instructions: str, schedule: str, timezone: str = "",
                         agent: str = "") -> str:
    owner = ctx.app.find_agent(agent) if agent else ctx.agent
    if not owner:
        return f"No agent '{agent}'."
    rid = ctx.app.scheduler.create(owner["id"], name, instructions, schedule,
                                   timezone or ctx.app.settings.timezone)
    r = ctx.app.db.one("SELECT * FROM routines WHERE id = ?", rid)
    return f"Routine #{rid} '{name}' created for {owner['name']}; next run {ctx.app.scheduler.fmt(r['next_run_at'], r['tz'])}."


async def routine_list(ctx: ToolContext) -> str:
    rows = ctx.app.db.all("SELECT * FROM routines ORDER BY agent_id, id")
    sch = ctx.app.scheduler
    return "\n".join(
        f"#{r['id']} [{r['agent_id']}] {r['name']} - {r['cron'] or 'webhook'} {r['tz']} - "
        f"{'enabled' if r['enabled'] else 'paused ' + r['paused_reason']} - next {sch.fmt(r['next_run_at'], r['tz'])}"
        for r in rows
    ) or "No routines."


async def routine_update(ctx: ToolContext, routine_id: int, enabled: bool | None = None, schedule: str = "",
                         instructions: str = "", delete: bool = False) -> str:
    sch = ctx.app.scheduler
    if delete:
        sch.delete(routine_id)
        return f"Deleted routine #{routine_id}."
    changes = {}
    if enabled is not None:
        changes["enabled"] = enabled
    if schedule:
        changes["cron"] = schedule
    if instructions:
        changes["instructions"] = instructions
    sch.update(routine_id, **changes)
    return f"Updated routine #{routine_id}."


async def request_secret(ctx: ToolContext, name: str, reason: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_\-]", "_", name).upper()
    ctx.app.runtime.post(ctx.conversation_id, ctx.agent["id"], reason, kind="request",
                         meta={"request": "secret", "name": name})
    ctx.app.bus.publish({"type": "request", "request": "secret", "agent_id": ctx.agent["id"], "name": name,
                         "reason": reason})
    return (f"Asked the owner to store secret {name}. You'll get a message when it is saved; then use "
            f"{{{{secret:{name}}}}}. End your turn or continue with other work.")


async def request_takeover(ctx: ToolContext, reason: str) -> str:
    screen = ctx.agent["screen"]
    ctx.app.runtime.post(ctx.conversation_id, ctx.agent["id"], reason, kind="request",
                         meta={"request": "takeover", "screen": screen})
    ctx.app.bus.publish({"type": "request", "request": "takeover", "agent_id": ctx.agent["id"], "screen": screen,
                         "reason": reason})
    return "Asked the owner to take over your screen. You'll get a message when they hand it back. End your turn now."


async def notify_owner(ctx: ToolContext, text: str, file: str = "") -> str:
    tg = ctx.app.telegram
    if not tg:
        ctx.app.runtime.post(ctx.conversation_id, ctx.agent["id"], text, kind="text")
        return "Telegram is not configured; posted in the web UI instead."
    path = None
    if file:
        from .system import _ws_path
        path = _ws_path(ctx, file)
        if not path.is_file():
            return f"File not found: {file}"
    await tg.send(f"{ctx.agent['name']}: {text}", file=path)
    return "Sent to the owner on Telegram."


def tools(app) -> list[Tool]:
    t = {"trusted": True}
    return [
        Tool("memory_save", "Save a durable fact (owner preference, decision, account detail, lesson learned). "
             "shared=true makes it visible to the whole team.",
             params(["content"], content=s("The fact, one or two sentences"), shared={"type": "boolean"}),
             memory_save, risk="safe", extra=t),
        Tool("memory_search", "Search your memory and the team's shared memory.", params(["query"], query=s("Words")),
             memory_search, risk="safe", extra=t),
        Tool("memory_forget", "Delete an outdated or wrong memory by id.", params(["memory_id"], memory_id=i("Id")),
             memory_forget, risk="safe", extra=t),
        Tool("message_agent", "Hand a task or question to a teammate (async). Give full context: goal, inputs, "
             "file paths, definition of done. Their reply comes back to you as a new message.",
             params(["agent", "message"], agent=s("Teammate id or name"), message=s("Self-contained request")),
             message_agent, risk="safe", extra=t, subject=lambda a: f"{a.get('agent')}: {a.get('message', '')[:200]}"),
        Tool("skill_read", "Read a skill (saved procedure) from the library.", params(["name"], name=s("Skill slug")),
             skill_read, risk="safe", extra=t),
        Tool("skill_save", "Save a reusable procedure to the team's skill library. Include: when to use, inputs and "
             "access needed, steps, how to validate, what to return, and what needs owner approval.",
             params(["name", "description", "instructions"], name=s("Short name"),
                    description=s("One line: what it does and when to use it"), instructions=s("Markdown body")),
             skill_save, risk="safe", extra=t),
        Tool("routine_create", "Schedule recurring work (cron, min 5 minutes apart). Confirm with the owner: owner "
             "agent, schedule + timezone, inputs, expected result, approval boundary, what to do if a source is missing.",
             params(["name", "instructions", "schedule"], name=s("Name"),
                    instructions=s("What to do each run, self-contained"),
                    schedule=s("Cron expression, e.g. '0 8 * * 1-5' for weekdays 08:00"),
                    timezone=s("IANA timezone, e.g. Europe/Vilnius"), agent=s("Owner agent (default: you)")),
             routine_create, risk="risky", extra=t, subject=lambda a: f"{a.get('schedule')} {a.get('name')}"),
        Tool("routine_list", "List all routines.", params([]), routine_list, risk="safe", extra=t),
        Tool("routine_update", "Pause/resume, reschedule, rewrite or delete a routine.",
             params(["routine_id"], routine_id=i("Routine id"), enabled={"type": "boolean"},
                    schedule=s("New cron"), instructions=s("New instructions"), delete={"type": "boolean"}),
             routine_update, risk="risky", extra=t),
        Tool("request_secret", "Ask the owner to securely store a credential (password, API key, token). The value "
             "never enters the chat.",
             params(["name", "reason"], name=s("UPPER_SNAKE name, e.g. GITHUB_TOKEN"), reason=s("Why you need it")),
             request_secret, risk="safe", extra=t),
        Tool("request_takeover", "Ask the owner to take control of your screen for a step only a human should do "
             "(login, 2FA, CAPTCHA, payment). Leave the page ready first.",
             params(["reason"], reason=s("Exactly what they need to do")), request_takeover, risk="safe", extra=t),
        Tool("notify_owner", "Message the owner on Telegram right now (progress, results, alerts), optionally with a "
             "file from /workspace.",
             params(["text"], text=s("Message"), file=s("Optional /workspace path to attach")),
             notify_owner, risk="safe", extra=t),
    ]
