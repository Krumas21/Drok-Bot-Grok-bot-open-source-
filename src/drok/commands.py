"""Owner slash commands, the same in the web chat and on Telegram. Anything else starting with / is a skill."""

import re

from .db import now

HELP = """/new - fresh start: the agent stops seeing this chat's history (memory, skills and files stay)
/compress - fold this chat's history into its summary now, keeping the last few messages
/model [agent|all] [provider/model] - show models, or switch one (tested with a tiny call first)
/retry - run your last message again
/stop - stop the current task
/usage - tokens used today and this week
/help - this list
/<skill> - run a skill, e.g. /web-research"""

COMPRESS_KEEP = 4  # messages left verbatim after /compress


async def handle(app, conversation_id: int, text: str, source: str = "web") -> str | None:
    """Run an owner command and post it with its reply into the conversation. None when `text` isn't a command."""
    m = re.match(r"\s*/([a-z]+)(?:@\S+)?(?:\s+(.*))?$", text or "", re.S)
    if not m or m.group(1) not in COMMANDS:
        return None
    conv = app.conversation(conversation_id)
    if not conv:
        raise ValueError("Unknown conversation")
    app.touch_user()
    app.runtime.post(conversation_id, "user", text.strip(), kind="status", meta={"source": source})
    reply = await COMMANDS[m.group(1)](app, conv, (m.group(2) or "").split(), source)
    app.runtime.post(conversation_id, "system", reply, kind="status")
    return reply


def _members(app, conv: dict) -> list[dict]:
    return [a for a in (app.agent(i) for i in conv["members"]) if a]


async def cmd_help(app, conv, args, source) -> str:
    return HELP


async def cmd_new(app, conv, args, source) -> str:
    last = app.db.one("SELECT MAX(id) m FROM messages WHERE conversation_id = ?", conv["id"])["m"] or 0
    # The agent's history starts after summarized_upto (see context.build), so this hides everything so far.
    app.db.update("conversations", conv["id"], summary="", summarized_upto=last)
    who = ", ".join(a["name"] for a in _members(app, conv))
    return f"New conversation. {who} won't see the messages above; memory, skills and files are kept."


async def cmd_compress(app, conv, args, source) -> str:
    members = _members(app, conv)
    if not members:
        return "No agent in this conversation."
    folded = await app.runtime.summarize(conv["id"], members[0], keep=COMPRESS_KEEP, minimum=1)
    if not folded:
        return "Nothing to compress yet."
    words = len((app.conversation(conv["id"])["summary"] or "").split())
    return f"Compressed {folded} messages into the summary ({words} words); the last {COMPRESS_KEEP} stay as they are."


async def cmd_model(app, conv, args, source) -> str:
    members = _members(app, conv)
    targets, spec = members if len(members) == 1 else [], ""
    if args and "/" not in args[0]:
        ref = args.pop(0)
        targets = app.agents() if ref == "all" else [a for a in [app.find_agent(ref)] if a]
        if not targets:
            return (f"No agent '{ref}'. Team: {', '.join(a['id'] for a in app.agents())}. "
                    "Models are written provider/model, e.g. anthropic/claude-sonnet-5-5.")
    if args:
        spec = args[0]
    if not spec:
        shown = targets or members
        lines = [f"{a['name']}: {a['model']}" + (f" (fallbacks: {', '.join(a['fallbacks'])})" if a["fallbacks"] else "")
                 for a in shown]
        lines.append(f"Background work (summaries, compression, reflection): {app.aux_model(shown[0] if shown else None)}")
        return "\n".join(lines) + "\nChange with /model <provider/model>, /model <agent> <provider/model> or /model all …"
    if not targets:
        return "This chat has several agents - name one: /model <agent> <provider/model>"
    problem = await app.llm.check(spec)
    if problem:
        return f"Not changed: {spec} didn't answer ({problem[:400]})"
    for a in targets:
        app.db.update("agents", a["id"], model=spec)
    app.bus.publish({"type": "agents"})
    return f"{', '.join(a['name'] for a in targets)} now on {spec}."


async def cmd_retry(app, conv, args, source) -> str:
    last = app.db.one("SELECT * FROM messages WHERE conversation_id = ? AND author = 'user' AND kind = 'text' "
                      "ORDER BY id DESC LIMIT 1", conv["id"])
    if not last:
        return "Nothing to retry yet."
    if app.db.one("SELECT id FROM turns WHERE conversation_id = ? AND status IN ('queued','running')", conv["id"]):
        return "Still working on it - /stop first if you want a fresh attempt."
    targets = conv["members"] if conv["kind"] == "dm" else app.runtime._route_group(conv, last["content"])
    for agent_id in targets:
        app.runtime.enqueue(agent_id, conv["id"], last["content"], source, {"message_id": last["id"]})
    return f"Retrying: {last['content'][:200]}"


async def cmd_stop(app, conv, args, source) -> str:
    stopped = [a["name"] for a in _members(app, conv) if app.runtime.stop(a["id"])]
    return f"Stopping {', '.join(stopped)}." if stopped else "Nothing is running."


async def cmd_usage(app, conv, args, source) -> str:
    def totals(since: float) -> dict:
        rows = app.db.all("SELECT agent_id, model, SUM(prompt_tokens) p, SUM(completion_tokens) c FROM usage "
                          "WHERE created_at > ? GROUP BY agent_id, model", since)
        return {(r["agent_id"], r["model"]): (r["p"], r["c"]) for r in rows}

    def k(n: int) -> str:
        return f"{n / 1000:.1f}k" if n >= 1000 else str(n)

    day, week = totals(now() - 86400), totals(now() - 7 * 86400)
    if not week:
        return "No model calls in the last 7 days."
    lines = []
    for (agent, model), (p, c) in sorted(week.items(), key=lambda kv: -sum(kv[1])):
        dp, dc = day.get((agent, model), (0, 0))
        lines.append(f"{agent} · {model}: today {k(dp)} in / {k(dc)} out · 7 days {k(p)} in / {k(c)} out")
    return "\n".join(lines)


COMMANDS = {"help": cmd_help, "new": cmd_new, "compress": cmd_compress, "model": cmd_model, "retry": cmd_retry,
            "stop": cmd_stop, "usage": cmd_usage}
