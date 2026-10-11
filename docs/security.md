# Security model

Drok gives language models a shell, a browser and your accounts. Read this before connecting anything that matters.

## What protects you

| Layer | What it does | What it doesn't do |
|---|---|---|
| **Container boundary** | Agents act inside the `computer` container. It has no Docker socket and no published ports. `/workspace` is the only folder shared with the host. | It's a container, not a VM. Agents have sudo *inside* it and full outbound internet. |
| **Rules** | Pattern rules on every tool call: deny beats ask beats allow. Defaults ask before `git push`, `npm publish`, `rm -rf` and any routine change, and deny `mkfs`. | Patterns match text; a determined model can phrase a command differently. That's what the reviewer is for. |
| **Reviewer model** | An independent model call judges risky calls (shell, browser clicks/typing, MCP tools, routine writes) against the task: allow, ask, or deny. If it errors, the call waits for you. | It's a model. It can be wrong. |
| **Approvals** | You see the exact call and can edit its arguments before allowing it. Denials are final for that call. | An approval covers what the agent proposed, not what it already did. |
| **Untrusted fencing** | Web pages, files, command output and MCP results are wrapped in `<untrusted>` and the agents are told never to follow instructions inside. | Prompt injection is reduced, not eliminated. |
| **Secrets vault** | Secrets are Fernet-encrypted in the DB. Agents use `{{secret:NAME}}`; values are substituted only when a command runs and are redacted from everything that returns to the model, transcripts and logs. Entry is only possible in the web UI (not in Telegram, whose history isn't private). | Code running on the computer sees the real value while it runs. Don't give agents secrets they don't need. |
| **Human takeover** | Logins, 2FA, CAPTCHAs and payments are handed to you on the live screen instead of being worked around. | Once you log a site in, agents on that screen can use the session. |
| **Owner-only access** | Web UI behind a password (session cookie, SameSite=Lax, slow failed logins). VNC bridged through the same session. Telegram restricted to an allowlist of user ids. Webhooks need a per-routine secret. | Single admin. Use a strong password and keep the UI off the public internet. |
| **Audit** | Every tool call, decision, reason, duration and result preview is logged (Activity page). | Logs live in the same database; they aren't tamper-proof. |
| **Absence pause** | Routines pause after `DROK_ABSENCE_DAYS` without hearing from you. | - |

## Recommendations

- Run Drok on a machine (or VM) dedicated to it, not your daily laptop.
- Give agents **their own accounts** (a separate email, a GitHub bot account, scoped API tokens) rather than your personal ones.
- Keep consequential actions behind approval: add `ask` rules for anything that sends, pays, deletes or deploys in your setup (e.g. `mcp_gmail_*` → ask).
- Use a different, cheap and fast model as `DROK_REVIEWER_MODEL` so reviewing doesn't share the acting model's blind spots.
- Access the UI over Tailscale/WireGuard. Don't expose port 8080.
- Back up `data/.secret.key` separately - it decrypts your stored secrets.

## Known gaps

- `web_fetch` and the browser can reach any URL, so a prompt-injected agent could leak data it has seen through a URL. Secrets are never filled into `web_fetch`, but other data could be.
- Chromium runs with `--no-sandbox` inside the container (the container is the sandbox).
- x11vnc on the internal Docker network has no password; only the core container can reach it.

Report vulnerabilities privately - see [SECURITY.md](../SECURITY.md).
