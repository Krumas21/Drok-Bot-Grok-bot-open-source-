"""decide: the team's decision driver. Main decisions go to TypeSafe's Jev - a "System One" model that answers typed
questions with probabilities instead of prose - through OpenRouter's Decisions API, using OPENROUTER_API_KEY."""

import httpx

from . import Tool, ToolContext, params, s

URL = "https://openrouter.ai/api/alpha/decisions"
MAX_OPTIONS = 8
TRANSPORT: httpx.AsyncBaseTransport | None = None  # tests swap in a MockTransport


async def ask(api_key: str, model: str, state: dict, questions: dict) -> dict[str, float]:
    """One forward pass: every question is answered at once. Returns {question_id: P(yes)}."""
    async with httpx.AsyncClient(timeout=30, transport=TRANSPORT) as http:
        r = await http.post(URL, json={"model": model, "state": state, "questions": questions},
                            headers={"Authorization": f"Bearer {api_key}"})
    if r.status_code != 200:
        raise ValueError(f"HTTP {r.status_code}: {r.text[:300]}")
    answers = r.json().get("answers")
    if not isinstance(answers, dict):
        raise ValueError("response has no answers")
    out = {}
    for qid in questions:
        p = (answers.get(qid) or {}).get("noul")
        if not isinstance(p, (int, float)) or not 0 <= p <= 1:  # NaN fails the range check too
            raise ValueError(f"bad answer for {qid}: {answers.get(qid)!r}")
        out[qid] = float(p)
    return out


async def decide(ctx: ToolContext, decision: str, options: list, success: str, context: str = "") -> str:
    opts = [str(o).strip() for o in options if str(o).strip()]
    if not 2 <= len(opts) <= MAX_OPTIONS:
        return f"Error: give 2-{MAX_OPTIONS} options."
    state = {"decision": decision, "success_means": success, "context": context}
    gid = ctx.turn["meta"].get("goal_id")
    goal = ctx.app.goals.get(gid) if gid else None
    if goal:
        state["goal"] = {"objective": goal["objective"], "done_when": goal["done_when"]}
    # One yes/no forecast per option, not a single choice question: absolute odds also tell us when every option
    # is bad, which a normalized choice distribution hides.
    questions = {f"option_{n}": {
        "type": "noul",
        "instructions": f"Going with this option will succeed: {opt}",
        "criteria": {"true": f"It achieves this: {success}",
                     "false": "It falls short, backfires, or costs more than it gains."},
    } for n, opt in enumerate(opts, 1)}
    try:
        odds = await ask(ctx.app.llm.providers["openrouter"].api_key, ctx.app.settings.jev_model, state, questions)
    except (httpx.HTTPError, ValueError) as e:
        return f"Jev unavailable ({e}). Decide yourself with think, and tell the owner Jev wasn't consulted."
    ranked = sorted(zip(odds.values(), opts, strict=True), reverse=True)
    (best_p, best), (second_p, _) = ranked[0], ranked[1]
    lines = [f"Jev, P(success) per option for: {decision}", *(f"- {p:.0%}  {o}" for p, o in ranked), "",
             f"Verdict: {best}. Act on it; override only for a hard fact Jev didn't have, and say what it was. "
             "On a goal, record it with goal_update(decisions=...)."]
    if best_p < 0.5:
        lines.append("Warning: even the best option is more likely to fail than succeed. Look for a better option "
                     "or ask the owner before committing.")
    elif best_p - second_p < 0.05:
        lines.append("Near tie: break it on cost and reversibility.")
    return "\n".join(lines)


def tools(app) -> list[Tool]:
    if "openrouter" not in app.llm.providers:
        return []
    return [
        Tool("decide", "Decision driver: settle a main decision with Jev, a calibrated forecasting model. Use it for "
             "strategy, irreversible or costly choices, and picking between approaches - not for trivia. Give 2-8 "
             "concrete options and what success means; it returns each option's probability of success and the "
             "verdict to act on. Jev sees only what you pass, so put the facts that matter in `context`.",
             params(["decision", "options", "success"], decision=s("The decision, as a question"),
                    options={"type": "array", "items": s("One concrete option")},
                    success=s("What a good outcome looks like, verifiable"),
                    context=s("Facts, constraints, numbers and risks Jev should weigh")),
             decide, risk="safe", extra={"trusted": True}, subject=lambda a: a.get("decision", "")[:300]),
    ]
