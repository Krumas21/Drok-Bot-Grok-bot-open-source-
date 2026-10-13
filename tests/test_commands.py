"""Owner commands (/new, /compress, /model, /retry), hiring agents, and file uploads from the web chat."""

import asyncio

from conftest import call, messages, say, turn
from fastapi.testclient import TestClient

from drok import commands, context
from drok.llm import LLM
from drok.web.app import create_web


async def run_next(app, agent_id):
    t = app.db.one("SELECT * FROM turns WHERE agent_id = ? AND status = 'queued' ORDER BY id LIMIT 1", agent_id)
    await app.runtime.run_turn(t)
    await asyncio.sleep(0)
    for task in list(app.runtime._background):
        await task
    return turn(app, t["id"])


def latest_turn(app):
    return app.db.one("SELECT * FROM turns ORDER BY id DESC LIMIT 1")


async def test_new_hides_earlier_messages_from_the_agent(app):
    conv = app.dm("forge")["id"]
    app.runtime.user_message(conv, "old topic")
    app.db.update("conversations", conv, summary="talked about the old topic")
    reply = await commands.handle(app, conv, "/new")
    assert "won't see" in reply and messages(app, conv, "status")[-1]["content"] == reply
    app.runtime.user_message(conv, "new topic")
    msgs = context.build(app, app.agent("forge"), latest_turn(app))
    seen = " ".join(str(m["content"]) for m in msgs[1:])
    assert "new topic" in seen and "old topic" not in seen


async def test_compress_folds_history_into_the_summary(app):
    conv = app.dm("forge")["id"]
    for n in range(8):
        app.runtime.post(conv, "user" if n % 2 == 0 else "forge", f"message {n}")
    reply = await commands.handle(app, conv, "/compress")
    assert reply.startswith("Compressed 4 messages")
    assert app.conversation(conv)["summary"] == "summary"
    app.runtime.user_message(conv, "next")
    history = [m["content"] for m in context.build(app, app.agent("forge"), latest_turn(app))[1:]]
    assert "summary" in history[0] and "message 3" not in " ".join(history) and "message 4" in " ".join(history)
    assert await commands.handle(app, app.dm("scout")["id"], "/compress") == "Nothing to compress yet."


async def test_model_command_tests_the_model_before_switching(app):
    conv = app.dm("forge")["id"]
    assert "Forge: fake/model" in await commands.handle(app, conv, "/model")
    assert await commands.handle(app, conv, "/model fake/better") == "Forge now on fake/better."
    assert app.agent("forge")["model"] == "fake/better"

    async def broken(spec):
        return "404 model: sonnet-5.5"
    app.llm.check = broken
    assert (await commands.handle(app, conv, "/model anthropic/sonnet-5.5")).startswith("Not changed")
    assert app.agent("forge")["model"] == "fake/better"

    del app.llm.check
    await commands.handle(app, app.dm("chief")["id"], "/model all fake/cheap")
    assert {a["model"] for a in app.agents()} == {"fake/cheap"}
    group = app.create_group("g", ["chief", "forge"])["id"]
    assert "name one" in await commands.handle(app, group, "/model fake/x")
    assert "No agent 'nobody'" in await commands.handle(app, conv, "/model nobody")


async def test_model_check_reports_unconfigured_providers():
    assert "not configured" in await LLM(providers={}).check("anthropic/claude-sonnet-5-5")


async def test_retry_runs_the_last_message_again(app):
    conv = app.dm("forge")["id"]
    assert await commands.handle(app, conv, "/retry") == "Nothing to retry yet."
    msg = app.runtime.user_message(conv, "build it")
    assert "Still working" in await commands.handle(app, conv, "/retry")
    app.llm.script = [say("failed somehow")]
    await run_next(app, "forge")
    assert (await commands.handle(app, conv, "/retry")).startswith("Retrying")
    t = latest_turn(app)
    assert t["status"] == "queued" and t["meta"]["message_id"] == msg["id"]
    assert context.build(app, app.agent("forge"), t)[-1]["content"] == "build it"


async def test_commands_stay_out_of_the_agents_view_and_skills_fall_through(app):
    conv = app.dm("scout")["id"]
    await commands.handle(app, conv, "/usage")
    assert await commands.handle(app, conv, "/web-research NAS prices") is None
    assert await commands.handle(app, conv, "just text") is None
    app.runtime.user_message(conv, "hello")
    seen = " ".join(str(m["content"]) for m in context.build(app, app.agent("scout"), latest_turn(app))[1:])
    assert "/usage" not in seen and "No model calls" not in seen


async def test_lead_hires_a_new_agent_after_approval(app):
    assert "agent_create" in app.tools_for(app.agent("chief"))
    assert "agent_create" not in app.tools_for(app.agent("forge"))
    conv = app.dm("chief")["id"]
    app.llm.script = [call("agent_create", {"name": "Ledger", "title": "Bookkeeper", "description": "Invoices",
                                            "instructions": "Keep the books in /workspace/books."})]
    app.runtime.user_message(conv, "hire a bookkeeper")
    t = await run_next(app, "chief")
    assert t["status"] == "waiting" and not app.find_agent("ledger")  # default rule: the owner confirms
    app.runtime.resolve_approval(app.db.one("SELECT id FROM approvals")["id"], True)
    app.llm.script = [say("Hired Ledger.")]
    await run_next(app, "chief")
    ledger = app.find_agent("ledger")
    assert ledger["title"] == "Bookkeeper" and ledger["model"] == app.agent("chief")["model"]
    assert ledger["tools"] == ["*"] and app.dm("ledger")


def test_web_upload_lands_in_the_inbox_and_reaches_the_agent(app):
    client = TestClient(create_web(app))
    assert client.post("/login", data={"password": "pw"}, follow_redirects=False).status_code == 303
    conv = app.dm("forge")["id"]
    r = client.post(f"/c/{conv}/send", data={"text": "summarize this"},
                    files=[("files", ("Q3 report (final).csv", b"a,b\n1,2\n", "text/csv"))])
    assert r.status_code == 200
    saved = list((app.settings.workspace / "inbox").iterdir())
    assert len(saved) == 1 and saved[0].name.endswith("_Q3_report_final_.csv") and saved[0].read_bytes() == b"a,b\n1,2\n"
    text = messages(app, conv, "text")[-1]["content"]
    assert text.startswith("summarize this") and f"/workspace/inbox/{saved[0].name}" in text
    assert latest_turn(app)["agent_id"] == "forge"

    assert client.post(f"/c/{conv}/send", data={"text": "/help"}).json() == {"ok": True}
    assert "/compress" in messages(app, conv, "status")[-1]["content"]
    assert client.post(f"/c/{conv}/send", data={"text": " "}).status_code == 400
