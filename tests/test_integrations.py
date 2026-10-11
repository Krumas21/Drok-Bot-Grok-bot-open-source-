import asyncio
import shlex
import sys
from pathlib import Path

from conftest import call, say

from drok.db import now


async def test_mcp_stdio_server_becomes_a_tool(app):
    server = Path(__file__).parent / "mcp_echo_server.py"
    app.db.insert("mcp_servers", name="echo", target=f"{shlex.quote(sys.executable)} {shlex.quote(str(server))}", env={}, created_at=now())
    await app.mcp.start()
    try:
        for _ in range(100):
            if "mcp_echo_shout" in app.all_tools():
                break
            await asyncio.sleep(0.1)
        assert "mcp_echo_shout" in app.all_tools(), app.mcp.errors
        app.llm.script = [call("mcp_echo_shout", {"text": "hello"}), say("ok")]
        app.runtime.user_message(app.dm("forge")["id"], "shout")
        t = app.db.one("SELECT * FROM turns WHERE status = 'queued'")
        await app.runtime.run_turn(t)
        result = [m for m in app.db.one("SELECT llm_messages FROM turns WHERE id = ?", t["id"])["llm_messages"]
                  if m["role"] == "tool"][0]["content"]
        assert "HELLO" in result
    finally:
        await app.mcp.stop()


async def test_telegram_routing(app):
    from drok.channels.telegram import Telegram

    app.settings.telegram_token = "123456:TEST"
    app.settings.telegram_allowed = {42}
    tg = Telegram(app)
    sent = []

    async def fake_send(text, file=None, buttons=None, voice=None):
        sent.append((text, buttons))
        return [(42, len(sent))]

    tg.send = fake_send
    chief_dm, forge_dm = app.dm("chief")["id"], app.dm("forge")["id"]
    post = app.runtime.post

    await tg.on_event({"type": "message", "message": post(chief_dm, "chief", "from routine", meta={"source": "routine"})})
    await tg.on_event({"type": "message", "message": post(chief_dm, "chief", "web reply", meta={"source": "web"})})
    await tg.on_event({"type": "message", "message": post(forge_dm, "forge", "asked via /ask", meta={"source": "telegram"})})
    await tg.on_event({"type": "message", "message": post(forge_dm, "forge", "web only", meta={"source": "web"})})
    await tg.on_event({"type": "message", "message": post(chief_dm, "user", "owner text", meta={"source": "telegram"})})
    await tg.on_event({"type": "approval", "approval_id": 7, "agent_id": "forge", "tool": "shell",
                       "subject": "git push", "reason": "rule"})

    texts = [t for t, _ in sent]
    assert texts[0] == "from routine"
    assert texts[1] == "Forge: asked via /ask"
    assert "web reply" not in texts and "web only" not in texts and "owner text" not in texts
    assert "git push" in texts[2] and sent[2][1] is not None  # approval comes with buttons
