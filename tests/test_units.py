import pytest

from drok.llm import LLMError, discover_providers, split_model
from drok.scheduler import KEEP_RUNS, validate


def test_rule_precedence(app):
    r = app.reviewer
    assert r.match("shell", "git push origin")["action"] == "ask"
    assert r.match("shell", "sudo mkfs /dev/sda")["action"] == "deny"
    assert r.match("browser", "goto https://x.com")["action"] == "allow"
    assert r.match("shell", "ls") is None


async def test_always_allow_is_exact(app):
    tool = app.all_tools()["shell"]
    app.reviewer.always_allow(tool, {"command": "npm test *"})
    assert app.reviewer.match("shell", "npm test *")["action"] == "allow"
    assert app.reviewer.match("shell", "npm test && rm x") is None


def test_cron_validation():
    validate("0 8 * * 1-5", "Europe/Vilnius")
    with pytest.raises(ValueError):
        validate("* * * * *", "UTC")
    with pytest.raises(ValueError):
        validate("0 8 * * *", "Mars/Base")


def test_routine_fire_and_history_cap(app):
    rid = app.scheduler.create("chief", "Brief", "Do the briefing", "0 8 * * *", "UTC")
    r = app.db.one("SELECT * FROM routines WHERE id = ?", rid)
    for _ in range(KEEP_RUNS + 5):
        app.scheduler.fire(r)
    assert app.db.one("SELECT COUNT(*) n FROM routine_runs")["n"] == KEEP_RUNS
    t = app.db.one("SELECT * FROM turns ORDER BY id DESC LIMIT 1")
    assert t["source"] == "routine" and "Do the briefing" in t["trigger"]


def test_due_routine_fires_once_and_advances(app):
    rid = app.scheduler.create("chief", "Brief", "x", "0 8 * * *", "UTC")
    app.db.update("routines", rid, next_run_at=1.0)  # long overdue: server was off
    app.scheduler.tick()
    app.scheduler.tick()
    assert app.db.one("SELECT COUNT(*) n FROM turns")["n"] == 1


def test_absence_pauses_routines(app):
    app.scheduler.create("chief", "Brief", "x", "0 8 * * *", "UTC")
    app.db.set_kv("last_user_activity", 0)
    app.scheduler.check_absence(30 * 86400)
    assert app.db.one("SELECT * FROM routines")["enabled"] == 0


def test_vault_roundtrip(app):
    app.vault.set("PW", "hunter22")
    assert app.vault.fill("login {{secret:PW}} {{secret:NOPE}}") == "login hunter22 {{secret:NOPE}}"
    assert app.vault.redact("echo hunter22") == "echo {{secret:PW}}"


def test_providers_and_models():
    found = discover_providers({"OPENAI_API_KEY": "k", "DROK_PROVIDER_LOCAL_BASE_URL": "http://x/v1",
                                "OLLAMA_BASE_URL": "http://o/v1"})
    assert set(found) == {"openai", "local", "ollama"}
    assert split_model("openrouter/x-ai/grok-4") == ("openrouter", "x-ai/grok-4")
    with pytest.raises(LLMError):
        split_model("gpt-5")


def test_workspace_jail(app, tmp_path):
    from drok.tools import ToolContext
    from drok.tools.system import _ws_path
    ctx = ToolContext(app, app.agent("forge"), {})
    assert _ws_path(ctx, "/workspace/a/b.txt") == app.settings.workspace / "a" / "b.txt"
    with pytest.raises(ValueError):
        _ws_path(ctx, "/workspace/../../etc/passwd")


def test_telegram_allowlist(app):
    from drok.channels.telegram import Telegram
    app.settings.telegram_token = "123456:TEST"
    app.settings.telegram_allowed = {42}
    tg = Telegram(app)
    assert tg.is_allowed(42) and not tg.is_allowed(7) and not tg.is_allowed(None)
