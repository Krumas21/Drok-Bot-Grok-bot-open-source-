"""Thinking, planning, memory, goals and loops - the tools an agent uses to run long work well."""

from datetime import datetime

from ..goals import _norm_plan
from ..memory import MemoryRefused
from . import Tool, ToolContext, ToolResult, i, params, s

T = {"trusted": True}
MARK = {"done": "x", "in_progress": "~", "blocked": "!"}


async def think(ctx: ToolContext, thought: str) -> str:
    ctx.app.runtime.post(ctx.conversation_id, ctx.agent["id"], thought, kind="thought")
    return "Noted. Continue."


def _render(items: list[dict]) -> str:
    return "\n".join(f"- [{MARK.get(p['status'], ' ')}] {p['text']}" for p in items)


async def plan(ctx: ToolContext, items: list, note: str = "") -> str:
    app, turn = ctx.app, ctx.turn
    norm = _norm_plan(items)
    if not norm:
        return "Plan is empty."
    gid = turn["meta"].get("goal_id")
    goal = app.goals.get(gid) if gid else None
    holder = goal["state"] if goal else turn["meta"]
    body = _render(norm) + (f"\n\n{note}" if note else "")
    mid = holder.get("plan_message_id")
    if mid and app.db.one("SELECT id FROM messages WHERE id = ?", mid):
        app.runtime.update_message(mid, content=body, items=norm)
    else:
        holder["plan_message_id"] = app.runtime.post(ctx.conversation_id, ctx.agent["id"], body, kind="plan",
                                                     meta={"items": norm, "goal_id": gid})["id"]
    if goal:
        app.db.update("goals", gid, plan=norm, state=holder)
    else:
        app.db.update("turns", turn["id"], meta=holder)
    done = sum(p["status"] == "done" for p in norm)
    return f"Plan saved: {done}/{len(norm)} done. Keep it current as you work."


async def memory(ctx: ToolContext, action: str, content: str = "", target: str = "agent", old_text: str = "") -> str:
    try:
        return ctx.app.memory.apply(ctx.agent["id"], action, target, content, old_text)
    except MemoryRefused as e:
        return f"Error: {e}"


async def recall(ctx: ToolContext, query: str = "", message_id: int = 0) -> str:
    if message_id:
        return ctx.app.memory.around(message_id)
    return ctx.app.memory.recall(query)


async def goal_set(ctx: ToolContext, title: str, objective: str, done_when: str, max_turns: int = 30,
                   check_every_minutes: int = 0, agent: str = "") -> str:
    owner = ctx.app.find_agent(agent) if agent else ctx.agent
    if not owner:
        return f"No agent '{agent}'."
    g = ctx.app.goals.create(owner["id"], title, objective, done_when, max_turns, check_every_minutes,
                             created_by=ctx.agent["id"])
    when = "after this turn" if owner["id"] == ctx.agent["id"] else f"by {owner['name']} now"
    return (f"Goal #{g['id']} created ({g['max_turns']}-turn budget). Work on it starts {when}; each continuation "
            "starts from the goal's plan and progress record, so keep those current with goal_update.")


async def goal_update(ctx: ToolContext, goal_id: int = 0, progress: str = "", next_steps: str = "",
                      decisions: str = "", files: str = "", plan: list | None = None, status: str = "",
                      evidence: str = "", blocker: str = "", done_when: str = "",
                      check_again_in_minutes: int = 0) -> str:
    gid = goal_id or ctx.turn["meta"].get("goal_id")
    if not gid:
        return "No goal_id given and this turn isn't working on a goal. Use goal_list."
    try:
        return ctx.app.goals.update(int(gid), ctx.agent["id"], progress=progress, next_steps=next_steps,
                                    decisions=decisions, files=files, plan=plan, status=status, evidence=evidence,
                                    blocker=blocker, done_when=done_when,
                                    check_again_in_minutes=check_again_in_minutes)
    except ValueError as e:
        return f"Error: {e}"


async def goal_list(ctx: ToolContext, everyone: bool = False) -> str:
    sql = "SELECT * FROM goals WHERE status NOT IN ('done','cancelled','failed')"
    rows = ctx.app.db.all(sql + ("" if everyone else " AND agent_id = ?") + " ORDER BY id",
                          *([] if everyone else [ctx.agent["id"]]))
    return "\n".join(f"#{g['id']} [{g['status']}] {g['title']} - {g['agent_id']}, {g['turns_used']}/{g['max_turns']} "
                     f"turns. Progress: {g['state'].get('progress', '-')[:200]}" for g in rows) or "No open goals."


async def wake_me(ctx: ToolContext, minutes: float, note: str) -> str:
    wid = ctx.app.goals.wake(ctx.agent["id"], ctx.conversation_id, minutes, note)
    due = ctx.app.db.one("SELECT due_at FROM wakeups WHERE id = ?", wid)["due_at"]
    return f"Wake-up #{wid} set for {datetime.fromtimestamp(due).strftime('%H:%M')}. End your turn; you'll get the note back."


async def clarify(ctx: ToolContext, question: str, options: list | None = None, why: str = "") -> ToolResult:
    opts = [str(o)[:100] for o in (options or [])][:6]
    app = ctx.app
    app.runtime.post(ctx.conversation_id, ctx.agent["id"], question, kind="question",
                     meta={"options": opts, "why": why})
    app.bus.publish({"type": "question", "agent_id": ctx.agent["id"], "conversation_id": ctx.conversation_id,
                     "question": question, "options": opts})
    gid = ctx.turn["meta"].get("goal_id")
    if gid:
        app.goals.set_status(gid, "blocked", f"waiting for the owner: {question[:300]}")
    return ToolResult("Question sent to the owner. Their answer will arrive as a new message.", stop=True)


async def update_my_instructions(ctx: ToolContext, new_instructions: str, reason: str) -> str:
    ctx.app.db.update("agents", ctx.agent["id"], system_prompt=new_instructions.strip())
    ctx.app.bus.publish({"type": "agents"})
    return "Your role instructions are updated; they apply from your next turn."


def tools(app) -> list[Tool]:
    item = {"type": "object", "properties": {"text": s("Step"), "status": s("pending|in_progress|done|blocked")},
            "required": ["text"]}
    return [
        Tool("think", "Reason step by step before acting: weigh options, interpret surprising results, debug a "
             "failure, check your plan against the request. Shown to the owner as your thinking.",
             params(["thought"], thought=s("Your reasoning")), think, risk="safe", extra={**T, "quiet": True}),
        Tool("plan", "Write or update your task plan (a checklist the owner sees live). Send the full list each time "
             "with statuses.",
             params(["items"], items={"type": "array", "items": item}, note=s("Optional note")),
             plan, risk="safe", extra={**T, "quiet": True}),
        Tool("memory", "Curated long-term memory, always in your prompt. action=add|replace|remove. target='agent' "
             "(your notes: environment, tools, conventions) or 'shared' (the owner's preferences and team-wide facts). "
             "replace/remove find the entry by old_text. Bounded: consolidate when full.",
             params(["action"], action=s("add|replace|remove"), content=s("The fact (dense, one fact)"),
                    target=s("agent|shared"), old_text=s("Unique substring of the entry to replace/remove")),
             memory, risk="safe", extra=T),
        Tool("recall", "Search everything ever said in any conversation with the owner and the team (full-text). "
             "Pass message_id to read a hit with its surrounding messages.",
             params([], query=s("Words to search"), message_id=i("Open this message in context")),
             recall, risk="safe"),
        Tool("goal_set", "Start a long-horizon goal: work that continues across many turns (you or a teammate) until "
             "an independent verifier confirms `done_when`. Use for multi-hour/multi-day objectives, monitoring "
             "('until X happens'), or anything too big for one turn.",
             params(["title", "objective", "done_when"], title=s("Short title"),
                    objective=s("What to achieve and why, with constraints"),
                    done_when=s("Verifiable definition of done"), max_turns=i("Turn budget (default 30)"),
                    check_every_minutes=i("Pause between continuations, for monitoring loops (0 = keep going)"),
                    agent=s("Who owns it (default you)")),
             goal_set, risk="risky", extra=T, subject=lambda a: f"{a.get('title')}: {a.get('done_when', '')[:200]}"),
        Tool("goal_update", "Record goal progress before ending a goal turn: progress, next_steps, decisions, files, "
             "plan. status=blocked (+blocker) when you need the owner; status=done (+evidence) when finished - "
             "verified independently; status=active to resume a goal you blocked once the owner answered. "
             "check_again_in_minutes to wait for something external.",
             params([], goal_id=i("Defaults to the goal of this turn"), progress=s("What is done so far (cumulative)"),
                    next_steps=s("What to do next"), decisions=s("Decisions and why"),
                    files=s("Relevant files/URLs/IDs"), plan={"type": "array", "items": item},
                    status=s("blocked|done|active"), evidence=s("Proof of completion"),
                    blocker=s("What you need from the owner"), done_when=s("Set the definition of done if missing"),
                    check_again_in_minutes=i("Continue after this delay")),
             goal_update, risk="safe", extra=T),
        Tool("goal_list", "List open goals (yours, or everyone's).", params([], everyone={"type": "boolean"}),
             goal_list, risk="safe", extra=T),
        Tool("wake_me", "Set a one-shot timer: you'll get `note` back as a new message after `minutes` - for checking "
             "on something later (a build, a reply, a price).",
             params(["minutes", "note"], minutes={"type": "number"}, note=s("What to check and why")),
             wake_me, risk="safe", extra=T),
        Tool("clarify", "Ask the owner a question when an ambiguity would change the outcome. Offer options when you "
             "can (shown as buttons). Ends your turn; the answer arrives as a new message.",
             params(["question"], question=s("One clear question"), options={"type": "array", "items": s("Option")},
                    why=s("Why you need to know")),
             clarify, risk="safe", extra=T),
        Tool("update_my_instructions", "Propose a full rewrite of your own role instructions (the owner approves the "
             "change). Use when the owner corrected how you work in a lasting way.",
             params(["new_instructions", "reason"], new_instructions=s("Complete new instructions"),
                    reason=s("What changed and why")),
             update_my_instructions, risk="risky", extra=T, subject=lambda a: a.get("reason", "")[:300]),
    ]
