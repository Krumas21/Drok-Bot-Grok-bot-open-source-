"""Tools that touch Drok itself: teammates, workers, skills, routines, secrets and the owner."""

import re

from .. import delegate as delegation
from . import Tool, ToolContext, i, params, s


async def message_agent(ctx: ToolContext, agent: str, message: str) -> str:
    target = ctx.app.find_agent(agent)
    if not target:
        return f"No agent '{agent}'. Team: {', '.join(a['id'] for a in ctx.app.agents())}"
    if target["id"] == ctx.agent["id"]:
        return "That is you."
    return ctx.app.runtime.handoff(ctx.agent, target, message, ctx.conversation_id, ctx.turn["meta"].get("hops", 0))


async def agent_create(ctx: ToolContext, name: str, title: str, description: str, instructions: str,
                       model: str = "", tools: list | None = None) -> str:
    app = ctx.app
    if app.find_agent(name):
        return f"There is already an agent called '{name}'. Use message_agent, or pick another name."
    model = model.strip()
    if model:
        problem = await app.llm.check(model)
        if problem:
            return f"Not created: model {model} didn't answer ({problem[:300]}). Omit model to use yours."
    a = app.create_agent(name.strip(), title.strip(), description.strip(), instructions.strip(),
                         model=model or ctx.agent["model"], fallbacks=list(ctx.agent["fallbacks"]),
                         tools=[str(t).strip() for t in tools or [] if str(t).strip()] or ["*"])
    app.db.update("agents", a["id"], reasoning=ctx.agent.get("reasoning", ""))  # same effort as the lead
    return (f"Created {a['name']} (id {a['id']}, {a['title']}) on screen {a['screen']} with {a['model']}. "
            "Hand them work with message_agent; the owner can edit them on the Agents page.")


async def delegate(ctx: ToolContext, tasks: list) -> str:
    return await delegation.run(ctx, [t if isinstance(t, dict) else {"task": str(t)} for t in tasks])


async def skill_view(ctx: ToolContext, name: str, file: str = "") -> str:
    try:
        return ctx.app.skills.view(name, file)
    except ValueError as e:
        return f"Error: {e}"


async def skill_manage(ctx: ToolContext, action: str, name: str, description: str = "", content: str = "",
                       old_string: str = "", new_string: str = "", file_path: str = "", file_content: str = "") -> str:
    sk = ctx.app.skills
    try:
        if action == "create":
            if sk.read(name):
                return f"Skill '{name}' exists. Use action=patch to improve it."
            if not (description and content):
                return "create needs description and content."
            slug = sk.save(name, description, content, created_by=ctx.agent["id"])
            warn = sk.lint(sk.read(slug)["raw"])
            return f"Created /{slug} for the whole team." + (f" Warnings: {'; '.join(warn)}" if warn else "")
        if action == "patch":
            diff = sk.patch(name, old_string, new_string)
            warn = sk.lint(sk.read(name)["raw"])
            return f"Patched {name}.\n{diff}" + (f"\nWarnings: {'; '.join(warn)}" if warn else "")
        if action == "rewrite":
            skill = sk.read(name)
            if not skill:
                return f"No skill '{name}'."
            sk.save(name, description or skill.get("description", ""), content,
                    created_by=skill.get("created_by", ctx.agent["id"]))
            return f"Rewrote {name}."
        if action == "write_file":
            sk.write_file(name, file_path, file_content)
            return f"Wrote {file_path} in skill {name}."
        if action == "remove_file":
            sk.remove_file(name, file_path)
            return f"Removed {file_path} from skill {name}."
        if action == "archive":
            sk.archive(name)
            return f"Archived {name} (restorable from the Skills page)."
    except ValueError as e:
        return f"Error: {e}"
    return f"Unknown action '{action}'. Use create, patch, rewrite, write_file, remove_file or archive."


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
    path = None
    if file:
        from .system import _ws_path
        path = _ws_path(ctx, file)
        if not path.is_file():
            return f"File not found: {file}"
    ctx.app.runtime.post(ctx.conversation_id, ctx.agent["id"], text + (f"\n\n📎 {file}" if file else ""),
                         meta={"source": "notify", "telegram_sent": bool(tg)})
    if not tg:
        return "Telegram is not configured; posted in the web UI instead."
    await tg.send(f"{ctx.agent['name']}: {text}", file=path)
    return "Sent to the owner on Telegram."


def tools(app) -> list[Tool]:
    t = {"trusted": True}
    task = {"type": "object", "properties": {
        "task": s("Self-contained task for one worker"),
        "context": s("Everything the worker needs: inputs, paths, constraints, definition of done"),
        "output_format": s("Optional shape of the report, e.g. 'JSON list of {name, url, price}'")},
        "required": ["task"]}
    return [
        Tool("message_agent", "Hand a task or question to a teammate (async). Give full context: goal, inputs, "
             "file paths, definition of done. Their reply comes back to you as a new message.",
             params(["agent", "message"], agent=s("Teammate id or name"), message=s("Self-contained request")),
             message_agent, risk="safe", extra=t, subject=lambda a: f"{a.get('agent')}: {a.get('message', '')[:200]}"),
        Tool("agent_create", "Add a permanent teammate when recurring work fits no one on the team (team lead only). "
             "They get their own chat, screen and memory and work like any other agent. Write the instructions as "
             "their role: what they own, how they work, where they keep files, how they report.",
             params(["name", "title", "description", "instructions"], name=s("Short first name, e.g. Ledger"),
                    title=s("Role, e.g. Bookkeeper"),
                    description=s("One or two lines teammates read to decide what to send them"),
                    instructions=s("Their role instructions (Markdown)"),
                    model=s("provider/model; omit to use yours"),
                    tools={"type": "array", "items": {"type": "string"},
                           "description": "Tool name patterns they may use, e.g. ['browser', 'web_*']; omit for all"}),
             agent_create, risk="risky", extra={**t, "lead_only": True},
             subject=lambda a: f"{a.get('name')}: {a.get('title')}"),
        Tool("delegate", "Run 1-6 sub-tasks in parallel on isolated workers that use your tools and return only "
             "their final reports. Use for parallel research, bulk processing, or keeping long side-quests out of "
             "your context. Workers start with zero context - brief them completely. They can't ask the owner or get "
             "approvals.",
             params(["tasks"], tasks={"type": "array", "items": task}),
             delegate, risk="safe", extra=t, timeout=1800,
             subject=lambda a: f"{len(a.get('tasks') or [])} workers: "
                               f"{'; '.join(str((x or {}).get('task', x))[:60] for x in (a.get('tasks') or []))}"),
        Tool("skill_view", "Load a skill from the library (its procedure), or one of its supporting files.",
             params(["name"], name=s("Skill slug"), file=s("Optional file, e.g. references/api.md")),
             skill_view, risk="safe", extra=t),
        Tool("skill_manage", "Grow the team's skill library. action=create (name, description, content) for a new "
             "procedure; patch (old_string -> new_string) to fix or add a lesson; rewrite (content); write_file / "
             "remove_file (file_path under references/, scripts/, templates/, assets/); archive. Skills hold: When to "
             "use, Inputs, Steps, Validate, Return, Needs approval, Pitfalls.",
             params(["action", "name"], action=s("create|patch|rewrite|write_file|remove_file|archive"),
                    name=s("Skill name (kebab-case)"), description=s("One line: what + when"),
                    content=s("Markdown body"), old_string=s("Exact text to replace"), new_string=s("Replacement"),
                    file_path=s("Supporting file path"), file_content=s("Supporting file content")),
             skill_manage, risk="safe", extra=t, subject=lambda a: f"{a.get('action')} {a.get('name')}"),
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
        Tool("notify_owner", "Message the owner right now on Telegram (and in the web UI): progress, results, alerts - "
             "optionally with a file from /workspace.",
             params(["text"], text=s("Message"), file=s("Optional /workspace path to attach")),
             notify_owner, risk="safe", extra=t),
    ]
