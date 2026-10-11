# Contributing to Drok

Thanks for helping. Drok aims to stay small enough that one person can read all of it in an afternoon - please keep that in mind.

## Setup

```sh
uv sync                      # Python 3.12+, installs dev tools too
uv run pytest                # fast: fake LLM, fake computer, real MCP stdio server
uv run ruff check src tests
```

The computer needs Docker (X11 + Chromium). For the full stack without an API key, use the mock model - see "Development" in the README. `tests/e2e/smoke.py` must pass before a PR touching the runtime, tools, web or computer is merged; CI runs it on every push.

## Where things live

Start with [docs/architecture.md](docs/architecture.md). The core loop is `src/drok/runtime.py`; tools are in `src/drok/tools/`.

**Adding a tool:** write an async function `(ctx, **args)` returning `str` or `ToolResult`, register it in that module's `tools(app)` list with a JSON schema, a `risk` (`safe` = never reviewed, `risky` = rules + reviewer) and a `subject` (the text rules match against). Mark it `extra={"trusted": True}` only if its output can't contain outside content. Add a test.

**Adding a provider:** if it speaks the OpenAI chat-completions API, it's one line in `PRESETS` in `src/drok/llm.py`.

## Style

- Read the surrounding code and match it. Comments explain *why*, not what.
- Standard library first; new dependencies need a good reason in the PR.
- No silent failures: report errors to the model or the user, never swallow them.
- Anything that sends, publishes, buys, deletes or deploys must be reviewable - never mark such a tool `safe`.
- UI: server-rendered Jinja + small vanilla JS, no build step. Keep it that way.

## Pull requests

- One change per PR, with a test that fails without it.
- Describe what you verified (tests, smoke run, screenshots for UI changes).
- By contributing you agree your work is released under the MIT license.

## Reporting bugs

Use the issue templates. Include `docker compose logs drok --tail 200` (check it for secrets first) and the steps to reproduce. Security issues: see [SECURITY.md](SECURITY.md), not public issues.
