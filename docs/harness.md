# The harness: how Drok agents think, work and improve

Drok combines two designs:

- **Grok Bot** (xAI): a *team* of named agents with roles, a shared persistent computer with a screen per agent, handoffs, routines, approvals and an independent reviewer.
- **Hermes Agent** (Nous Research): a *self-improving* single agent - bounded curated memory, session search, skills with progressive disclosure that it writes and patches itself, delegation to isolated sub-agents, structured context compression.

Every Drok agent is a Hermes-grade agent; together they are a Grok Bot-style team.

## One turn

```
trigger (owner message · handoff · goal continuation · routine · wake-up · webhook)
  └─ context: system prompt (role, team, operating protocol, handbook, connections,
     skills index, memory, open goals) + recent conversation (or the goal brief)
       └─ loop: model → tool calls → safety review → execute → results (untrusted-fenced)
            ├─ compress the middle into a checkpoint when the transcript grows too large
            ├─ owner writes mid-task → folded into the running turn
            └─ needs approval → turn parks, resumes exactly there after the decision
  └─ finish: reply · route handoff replies · goal hooks · summary · reflection (learning)
```

## Operating protocol

Every agent's system prompt carries the same working method:

1. **Understand** - check memory, the skills index and `recall` first; `clarify` only when an ambiguity changes the outcome.
2. **Plan** - `plan` writes a live checklist the owner watches update; `think` is a scratchpad for hard decisions and failures.
3. **Execute with the most reliable tool** - connectors/APIs > browser DOM > pixel control; a script over a long chain of tool calls; background processes for long jobs.
4. **Verify** - evidence, not claims.
5. **Report** - done / partial / blocked, with paths and links.

## Memory

| Layer | What | How it's used |
|---|---|---|
| Agent notes | Bounded (2,200 chars) per agent: environment facts, tool quirks, conventions | Always in that agent's prompt |
| Shared memory | Bounded (1,600 chars): the owner's preferences and team-wide facts | Always in every agent's prompt |
| Recall | Full-text index of every message in every conversation | `recall(query)` → hits → `recall(message_id=…)` for context |
| Conversation summary | Rolling summary of long conversations | In the prompt for that conversation |
| Goal state | Plan + progress + decisions + files per goal | The brief of each goal continuation |

Memory is bounded on purpose (as in Hermes): when it's full the agent has to consolidate - merge, shorten, drop - which keeps it dense. Entries that look like instructions or credentials are refused, because memory is injected into every future prompt.

## Learning (self-improvement)

After a turn with 5+ tool calls, or when the owner's message reads like a correction ("no, always…", "next time…"), a background **reflection** reviews what happened and may:

- add/replace memory entries (owner preferences → shared; environment facts → agent notes),
- create a skill for a non-trivial workflow worth repeating,
- patch an existing skill with a lesson (`Pitfall: X - do Y`),
- suggest a change to the agent's own instructions (shown to the owner).

Agents also improve themselves deliberately:

- `skill_manage` - create, patch, rewrite, attach scripts/references/templates.
- `tool_create` - turn a script on the computer into a tool **every** agent can call (owner approves creation; every call is reviewed).
- `update_my_instructions` - propose a rewrite of their own role (owner approves, can edit before approving).

The **curator** archives agent-made skills nobody has used for 60 days (never deletes; pinned and owner/seed skills are exempt). Usage, patches and last use are tracked per skill.

## Skills (progressive disclosure)

- Level 0 - name + one-line description of every skill, in every prompt.
- Level 1 - `skill_view(name)` loads `SKILL.md`.
- Level 2 - `skill_view(name, file)` loads a supporting file from `references/`, `scripts/`, `templates/`, `assets/`.

Skills live in `/workspace/.skills/<name>/` so scripts run directly on the computer. The owner can invoke one in chat with `/skill-name …`. Format follows the Agent Skills layout (frontmatter `name`, `description`).

## Goals - long-horizon work

`goal_set(title, objective, done_when, max_turns, check_every_minutes)` (or the Goals page, or `/goal` on Telegram) starts work that continues across turns:

- **Externalized state.** Each continuation starts fresh from the goal brief - objective, definition of done, plan, progress, next steps, decisions, files, recent events - not from a growing transcript. That is what lets a goal run for hundreds of steps.
- **Progress protocol.** Before a goal turn ends, the agent calls `goal_update` with progress and next steps (and can wait: `check_again_in_minutes`).
- **Verification.** `goal_update(status="done", evidence=…)` sends the evidence to an independent verifier that checks it against `done_when` - and reads the files the evidence cites. Rejected → the goal continues with the verifier's feedback.
- **Guards.** Turn budgets, exponential backoff after failures (blocked after 3), blocked state when the agent needs the owner (owner is notified), pause/resume/cancel/extend from the UI.
- **Monitoring loops.** `check_every_minutes` turns a goal into "keep checking until the condition holds".

Smaller loops: `wake_me(minutes, note)` (one-shot timer) and routines (cron or webhook).

## Delegation

- `message_agent` - hand off to a persistent teammate (their reply comes back to you).
- `delegate(tasks=[…])` - spawn up to 6 isolated workers (4 at a time) that use your tools and return only their final reports. Workers can't ask the owner, schedule work, or touch memory/skills; anything that needs approval is refused and reported back so the parent can do it.

## Context compression

When a turn's transcript passes `DROK_CONTEXT_CHARS`, the middle is folded into a structured checkpoint (Goal · Constraints · Progress done/in progress/blocked · Key decisions · Files/URLs/IDs · Next steps · Critical context). The head (prompt + request) and the last messages stay verbatim; tool calls are never separated from their results. If a compression saves under 10% twice, it stops trying (and older tool output is trimmed instead).

## Tools at a glance

| Area | Tools |
|---|---|
| Thinking & planning | `think`, `plan`, `clarify`, `decide`, `goal_set`, `goal_update`, `goal_list`, `wake_me` |
| Memory & learning | `memory`, `recall`, `skill_view`, `skill_manage`, `tool_create`, `tool_update`, `update_my_instructions` |
| Team | `message_agent`, `delegate`, `notify_owner`, `routine_*`, `request_secret`, `request_takeover` |
| Computer | `shell` (+ background), `process`, `read_file` (text, images, PDF/DOCX/XLSX…), `write_file`, `edit_file`, `list_files`, `search_files`, `make_file` (docx, pdf, pptx, xlsx, html, epub) |
| Web | `web_search` (Brave, SearXNG, or a keyless fallback), `web_extract` (5 URLs in parallel, PDFs, JS rendering, CSS selectors), `browser` (DOM), `computer` (pixels) |
| Google | `gmail_search/read/attachment/draft/send/modify`, `drive_search/read/download/upload/create_folder`, `calendar_events/create_event` |
| Anything else | MCP servers and agent-built custom tools |
