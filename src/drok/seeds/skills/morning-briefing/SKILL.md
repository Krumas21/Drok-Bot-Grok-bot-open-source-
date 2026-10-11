---
name: morning-briefing
description: Compile a short morning briefing (calendar, inbox highlights, task status, news on watched topics) and send it to the owner on Telegram.
---

## When to use
A routine fires asking for the morning briefing, or the owner asks "what's on today?".

## Inputs and access
- /workspace/chief/tasks.md (open tasks)
- /workspace/chief/watchlist.md (topics to watch, one per line) - optional
- Calendar/email only if a connector (MCP) or a logged-in browser session exists. If not, skip that section and say so.

## Steps
1. Read open tasks; list anything overdue or blocked.
2. For each watchlist topic, search for news from the last 24h; keep at most 2 items per topic, with links.
3. If calendar access exists, list today's events with times.
4. If email access exists, list up to 5 messages that need a reply.

## Validate
Every item has a source or a file path. No item older than 24h in the news section.

## Return
A message under 200 words sent with notify_owner. Sections with no data say "nothing" - never fill gaps with guesses.

## Needs owner approval
Nothing - this skill only reads and reports.
