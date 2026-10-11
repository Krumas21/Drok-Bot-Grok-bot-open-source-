# Architecture

Drok is two containers and one SQLite file.

| Piece | Where | What it does |
|---|---|---|
| `drok` (core) | `src/drok/` | Web UI + API, agent runtime, scheduler, Telegram, MCP clients. One asyncio process. |
| `computer` | `computer/` | Debian with Xvfb, fluxbox, x11vnc and Chromium per screen, plus a shell with sudo. `computerd.py` supervises it and exposes a small token-protected HTTP API. |
| `data/drok.db` | volume | Agents, conversations, turns, approvals, rules, memory (FTS5), routines, secrets (encrypted), audit, usage. |
| `data/skills/` | volume | One `SKILL.md` per folder, shared by every agent. |
| `workspace/` | volume, mounted in both | The agents' durable files. |
| `computer-home` | named volume | `/home/drok` on the computer: browser profiles (logins), CLI credentials, installed tools. |

## Modules

```
src/drok/
  app.py          wiring, agents, conversations, tool registry
  runtime.py      the durable turn loop (read this first)
  context.py      system prompt + operating protocol, history, goal briefs, structured compression
  memory.py       curated memory, recall (FTS over all messages), post-turn reflection
  goals.py        long-horizon goals, continuation engine, verifier, wake-ups
  delegate.py     parallel isolated workers
  skills.py       progressive-disclosure skill library, usage tracking, curator
  review.py       rules + reviewer model -> allow / ask / deny
  llm.py          provider registry, retries, fallbacks, reasoning effort (OpenAI-compatible endpoints)
  scheduler.py    routines (cron, webhooks), goal/wake-up ticks, curator, absence auto-pause
  tools/          mind (think, plan, memory, recall, goals, clarify), system (shell, files, documents),
                  web (search, extract), computer (browser, pixels), team (handoffs, delegate, skills, routines,
                  secrets, notify), custom (self-built tools), google (Gmail, Drive, Calendar)
  mcp_client.py   long-lived MCP sessions -> tools
  teach.py        record a browser demo -> draft skill
  vault.py        Fernet-encrypted secrets, placeholder fill + redaction
  channels/telegram.py
  web/            FastAPI app, Jinja templates, static CSS/JS
  seeds/          starter agents and skills, copied on first run
```

## A turn, step by step

A **turn** is one unit of work for one agent: a user message, a handoff from a teammate, a routine firing, or a system notice ("the owner stored the secret you asked for").

1. Something calls `runtime.enqueue(...)`. A row is written to `turns` with status `queued`. Nothing runs yet.
2. The agent's worker (one per agent, so an agent does one thing at a time; agents run in parallel) picks the oldest queued turn and marks it `running`.
3. Context is built: harness prompt + role + team roster + skills index + memory + conversation summary + the last 30 messages.
4. Loop until done or the step budget (`DROK_MAX_STEPS`) runs out:
   - New owner messages for this conversation are folded into the running turn.
   - Call the model (with fallbacks). Save the transcript.
   - No tool calls -> post the reply, mark `done`.
   - For each tool call: **review** it (below). `allow` runs it with a timeout; output is redacted for secrets and wrapped in `<untrusted>` unless the tool is internal. `deny` returns a refusal to the model. `ask` creates an approval.
   - If any call needs approval, the turn is saved as `waiting` and the worker moves on.
5. When every approval for a turn is decided, the turn goes back to `queued`. On resume, the runtime finds the tool calls in the last assistant message that have no result yet and runs (or refuses) exactly those.
6. On finish: replies to handoffs are routed back to the sender, group `@mentions` wake the mentioned agents, routine runs are recorded, old messages get summarized in the background.

On boot, any turn still marked `running` is put back to `queued`: the persisted transcript lets it continue where it stopped.

## Review pipeline

`review.Reviewer.decide(tool, args, agent, task)`:

1. **Rules** (Settings page): fnmatch patterns on the tool name and on the call's *subject* (the shell command, URL, element description…). Any `deny` hit wins, then `ask`, then `allow`.
2. Tools marked `safe` (reading files, memory, skills, handoffs) are allowed.
3. Otherwise the **reviewer model** (`DROK_REVIEWER_MODEL`, default: the agent's own) judges the call against the task. Errors or unclear answers become `ask`.

*Always allow* on an approval saves an exact-match rule. Widen it on the Settings page if you want (`git status*`).

## The computer

`computerd.py` (standard library only) starts, per screen *N*: `Xvfb :N`, `fluxbox`, `x11vnc` on port 5900+N, Chromium with DevTools on 9222+N, and `socat` exposing DevTools on 9300+N to the core. A watchdog restarts dead processes every 5 seconds; if the X server dies, everything on that screen restarts.

- `browser` tool: Playwright connects over CDP to the screen's existing Chromium (`connect_over_cdp`), so what the agent does is what you see on the screen. `snapshot` tags visible interactive elements with numbered refs for reliable clicking with any model.
- `computer` tool: `xdotool` + `scrot` for pixel-level control of anything on the screen.
- Live view: the browser opens `/vnc/<n>` on the core, which checks your session and bridges the WebSocket to x11vnc's TCP port (what websockify does), so the computer never needs a published port.

## Telegram

Long polling (works behind NAT). Only `TELEGRAM_ALLOWED_USER_IDS` are served. What gets sent to you:

- everything the lead agent says in its DM that wasn't a reply to the web UI (Telegram conversations, routine results, handoff reports),
- replies to `/ask <agent>`,
- approval requests from any agent (with buttons), secret/takeover requests (with links), system notices,
- anything an agent sends with `notify_owner`.

## Data you should back up

`./data` (database, skills, generated keys - **`.secret.key` decrypts your secrets**), `./workspace`, and the `computer-home` volume if you want to keep browser logins.
