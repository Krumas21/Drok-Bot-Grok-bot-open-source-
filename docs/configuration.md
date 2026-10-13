# Configuration

Everything that needs a restart lives in `.env` (start from `.env.example`, or run `scripts/setup.sh`). Agents, rules, routines, skills, secrets and MCP servers are managed live in the web UI.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `DROK_ADMIN_PASSWORD` | - (required) | Web UI password. |
| `COMPUTER_TOKEN` | - (required) | Shared secret between core and computer. `setup.sh` generates it. |
| `DROK_DEFAULT_MODEL` | `openai/gpt-5-mini` | Model for new agents (`provider/model`). |
| `<PROVIDER>_API_KEY` | - | See the provider table in the README. |
| `<PROVIDER>_BASE_URL` | preset | Override a preset's endpoint (e.g. an Azure/OpenAI proxy). |
| `OLLAMA_BASE_URL` | - | e.g. `http://host.docker.internal:11434/v1` |
| `DROK_PROVIDER_<NAME>_BASE_URL` / `_API_KEY` | - | Any other OpenAI-compatible server, used as `<name>/<model>`. |
| `TELEGRAM_BOT_TOKEN` | - | From @BotFather. |
| `TELEGRAM_ALLOWED_USER_IDS` | - | Comma-separated user ids. Empty = the bot only tells people their id. |
| `DROK_AUTO_REVIEW` | `on` | Reviewer model for risky calls not covered by a rule. `off` = allow them. |
| `DROK_REVIEWER_MODEL` | agent's model | A fast, cheap model is a good choice. |
| `DROK_ABSENCE_DAYS` | `14` | Pause all routines after this many days without owner activity (warns a day before). `0` = never. |
| `DROK_MAX_STEPS` | `40` | Model calls per turn before the agent must report status. |
| `DROK_SCREENS` | `4` | Virtual screens on the computer. Agents are assigned round-robin; change per agent in the UI. Budget ~400 MB RAM per screen. |
| `DROK_OWNER_NAME` | `the owner` | How agents refer to you. |
| `TZ` | `UTC` | Default timezone (prompts, routines). |
| `DROK_PORT` / `DROK_BIND` | `8080` / `0.0.0.0` | Where the UI listens on the host. |
| `DROK_PUBLIC_URL` | `http://localhost:8080` | Base for links sent on Telegram. |
| `BRAVE_API_KEY` or `SEARXNG_URL` | - | Search backend for `web_search`. Without it a keyless DuckDuckGo fallback is used (fine for light use, can get rate-limited). |
| `DROK_STT_MODEL` | - | Speech-to-text for Telegram voice notes, e.g. `openai/whisper-1` or `groq/whisper-large-v3`. |
| `DROK_TTS_MODEL` / `DROK_TTS_VOICE` | - / `alloy` | Voice replies to voice notes, e.g. `openai/gpt-4o-mini-tts`. |
| `DROK_SECRET_KEY` | generated | Fernet key for secrets. Generated into `data/.secret.key` if unset. |
| `DROK_AUX_MODEL` | reviewer or agent model | Background work: summaries, compression, reflection, goal verification. A fast cheap model is ideal. |
| `DROK_LEARNING` | `on` | Post-turn reflection that saves memory and skills. |
| `DROK_CONTEXT_CHARS` | `320000` | Compress long turns beyond this (~4 chars per token). Lower it for small-context local models. |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | - | OAuth client (type Desktop app) for Gmail, Drive and Calendar. |
| `GOOGLE_REDIRECT_URI` | `http://localhost:<DROK_PORT>/oauth/google/callback` | Only if you use a Web-application client with HTTPS. |

## Google (Gmail, Drive, Calendar)

1. [Google Cloud Console](https://console.cloud.google.com/): create a project; under *APIs & Services → Library* enable **Gmail API**, **Google Drive API** and **Google Calendar API**.
2. *OAuth consent screen*: External; add your Google account as a test user. Apps left in *Testing* get refresh tokens that expire after 7 days - for an always-on setup, press **Publish app** (it's for your own use; Google shows an "unverified app" warning you can continue past).
3. *Credentials → Create credentials → OAuth client ID → Desktop app*. Copy the client ID and secret into `.env` as `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`, then `docker compose up -d`.
4. Settings → Google → **Connect Google**, approve. Google then redirects to `http://localhost:8080/oauth/google/callback?...`. On the server itself that completes on its own; from another device the page fails to load - copy the address from the address bar and paste it into the box on the Settings page.

Scopes requested: Gmail modify (read, label, draft, send - sending always needs your approval), Drive (full), Calendar events. Tokens are stored encrypted and refreshed automatically; **Disconnect** revokes them.

## Telegram, step by step

1. Talk to [@BotFather](https://t.me/BotFather), `/newbot`, copy the token into `TELEGRAM_BOT_TOKEN`.
2. `docker compose up -d` and send your bot any message. It answers with your user id.
3. Put the id in `TELEGRAM_ALLOWED_USER_IDS` and `docker compose up -d` again.
4. Set `DROK_PUBLIC_URL` to the address you open the UI at, so links in messages work.

Commands: `/agents`, `/ask <agent> <message>`, `/approvals`, `/goal`, `/goals`, `/routines`, `/stop [agent]`, `/help`, plus the chat commands below. Plain messages, photos, files (saved to `/workspace/inbox`) and voice notes go to your lead agent.

## Remote access

Don't port-forward the UI to the internet: it controls agents with shell access. Use [Tailscale](https://tailscale.com) or WireGuard and open `http://<tailnet-name>:8080`. If you put a reverse proxy with TLS in front, proxy WebSockets for `/vnc/*` and disable buffering for `/events` (Server-Sent Events).

## MCP connectors

Settings → Connectors. Give a name and either a Streamable HTTP URL or a command (Node is installed in the core image, so `npx -y …` servers work). Environment lines may reference secrets: `GITHUB_PERSONAL_ACCESS_TOKEN={{secret:GITHUB_TOKEN}}`. Tools appear to all agents as `mcp_<server>_<tool>` and go through review.

## Chat commands

Type these in any web chat (or on Telegram, where they act on your lead agent's conversation). Anything else starting with `/` runs the skill of that name.

| Command | Does |
|---|---|
| `/new` | Fresh start: the agent stops seeing this chat's earlier messages. Memory, skills and files stay. |
| `/compress` | Folds the chat's history into its running summary now, keeping the last few messages. |
| `/model` | Shows models. `/model <provider/model>` switches this chat's agent, `/model <agent> <provider/model>` a named one, `/model all <provider/model>` everyone. A tiny test call runs first, so a typo in a model name is refused instead of breaking the agent. |
| `/retry` | Runs your last message again (e.g. after a model error). |
| `/stop` | Stops the current task. |
| `/usage` | Tokens per agent and model, today and over 7 days. |
| `/help` | The list. |

## Files

Press **Attach** in a chat or drop files onto it (on Telegram, just send them). Files land in `/workspace/inbox/<time>_<name>` and the agent is told the path; it reads PDFs, Office files, spreadsheets, images and text with `read_file`. Any `/workspace/...` path in a message is a link to that file on the Files page, so deliverables are one click away.

## Agents

Each agent has: name, title, description (teammates read it to decide who gets a task), role instructions, model + fallbacks, a tool allowlist (`*`, or patterns like `browser, memory_*, message_agent`), and a screen. Mark one as **team lead** - it's the one on Telegram, and the only one that can hire teammates itself (`agent_create`, asks you first by default; a new agent starts with the lead's model).

Starter agents are created from `src/drok/seeds/agents/*.md` on first run only. Edit or delete them freely in the UI.

## Upgrading

```sh
git pull
docker compose up -d --build
```

The database schema is created with `IF NOT EXISTS`; files in `./data` and `./workspace` are kept.

## Running without Docker

For development: run `computer/` in Docker (it needs X11 and Chromium), and the core with `uv run drok`, pointing `COMPUTER_URL` at it. Publish the computer's ports 7000, 5901+ and 9301+ to localhost for that setup only. On macOS, port 7000 is taken by AirPlay - map it elsewhere (`-p 17000:7000`).
