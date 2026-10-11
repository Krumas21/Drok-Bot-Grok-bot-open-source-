# Security policy

## Reporting a vulnerability

Please **don't open a public issue**. Report it privately through GitHub: **Security → Report a vulnerability** on this repository (private vulnerability reporting).

Include what an attacker can do, the steps to reproduce, and the version or commit. You'll get an acknowledgement within a few days. Fixes are released as soon as they're ready, and you'll be credited unless you prefer otherwise.

## Scope

In scope: authentication and session bypass, ways around the approval/review pipeline, secret leakage (to the model, logs, transcripts or Telegram), access to the computer container from outside, webhook secret bypass, Telegram allowlist bypass.

Prompt injection that leads an agent to misbehave *within* what its rules and approvals allow is a known limitation (see [docs/security.md](docs/security.md)) - but reports showing a concrete bypass of those safeguards are very welcome.

## Supported versions

The latest release and `main`.
