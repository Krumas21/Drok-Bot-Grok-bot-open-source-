"""A scripted OpenAI-compatible server for end-to-end tests without a real API key.

Send an agent one of these messages:
  e2e shell: <command>        -> calls shell
  e2e browse: <url>           -> calls browser goto
  e2e screen                  -> calls computer screenshot
  e2e push                    -> calls shell "git push" (needs approval by default rules)
  e2e handoff                 -> asks forge to run `uname -a`
  anything else               -> plain reply
After a tool result it replies with a summary of that result. Reviewer prompts always get "allow".

Run:  uv run python tests/e2e/mock_llm.py   (listens on :9999, use DROK_PROVIDER_MOCK_BASE_URL=http://host:9999/v1)
"""

import json
import time
import uuid

import uvicorn
from fastapi import FastAPI, Request

app = FastAPI()


def reply(content="", tool=None, args=None):
    msg = {"role": "assistant", "content": content}
    if tool:
        msg["tool_calls"] = [{"id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
                              "function": {"name": tool, "arguments": json.dumps(args or {})}}]
    return {"id": "mock", "object": "chat.completion", "created": int(time.time()), "model": "mock",
            "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if tool else "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


def text_of(m):
    c = m.get("content")
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c if p.get("type") == "text")
    return c or ""


@app.post("/v1/chat/completions")
async def completions(request: Request):
    body = await request.json()
    msgs = body["messages"]
    system = text_of(msgs[0]) if msgs and msgs[0]["role"] == "system" else ""
    if "safety reviewer" in system:
        return reply('{"decision": "allow", "reason": "mock reviewer"}')
    if "running summary" in system or "recorded browser demonstration" in system:
        return reply("DESCRIPTION: mock skill\n\n## Steps\n1. mock")
    last = msgs[-1]
    if last["role"] == "tool" or text_of(last).startswith("[screen image"):
        result = next(text_of(m) for m in reversed(msgs) if m["role"] == "tool")
        return reply(f"Done. Tool said:\n```\n{result[:600]}\n```")
    said = text_of(last)
    cmd = said.split("]: ", 1)[-1].strip()
    if cmd.startswith("e2e shell:"):
        return reply("Running it.", "shell", {"command": cmd.split(":", 1)[1].strip()})
    if cmd.startswith("e2e browse:"):
        return reply("", "browser", {"action": "goto", "url": cmd.split(":", 1)[1].strip()})
    if cmd.startswith("e2e screen"):
        return reply("", "computer", {"action": "screenshot"})
    if cmd.startswith("e2e push"):
        return reply("", "shell", {"command": "cd /workspace && git push origin main"})
    if cmd.startswith("e2e handoff"):
        return reply("", "message_agent", {"agent": "forge", "message": "e2e shell: uname -a"})
    if said.startswith("[Reply from"):
        return reply(f"Teammate reported back: {said[:300]}")
    return reply(f"Mock reply to: {cmd[:200]}")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9999, log_level="warning")
