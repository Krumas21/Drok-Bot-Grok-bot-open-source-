import json

import pytest

from drok.app import App
from drok.config import Settings
from drok.llm import LLM


def call(name: str, args: dict, cid: str = "c1") -> dict:
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def say(text: str) -> dict:
    return {"role": "assistant", "content": text}


class FakeLLM(LLM):
    """Scripted model: pops one reply per chat() call; reviewer verdict is configurable."""

    def __init__(self):
        super().__init__(providers={})
        self.script: list[dict] = []
        self.seen: list[list[dict]] = []
        self.verdict = '{"decision": "allow", "reason": "fine"}'

    async def chat(self, model, messages, tools=None, fallbacks=(), agent_id=""):
        self.seen.append(messages)
        if not self.script:
            return say("(script exhausted)"), model
        return self.script.pop(0), model

    async def text(self, model, system, prompt, agent_id=""):
        if isinstance(self.verdict, Exception):
            raise self.verdict
        return self.verdict


class FakeComputer:
    screens = 2

    def __init__(self):
        self.commands: list[str] = []
        self.locks = {}

    async def exec(self, command, timeout=120, cwd="/workspace", screen=1):
        self.commands.append(command)
        return {"exit_code": 0, "output": f"ran: {command}"}

    async def screenshot(self, screen):
        return "iVBORw0KGgo="

    async def input(self, screen, **kw):
        return {"ok": True}

    async def health(self):
        return {"ok": True}


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "ws", secret_key=_key(),
                        session_key="x", admin_password="pw", screens=2, default_model="fake/model")
    settings.data_dir.mkdir()
    settings.workspace.mkdir()
    return App(settings, llm=FakeLLM(), computer=FakeComputer())


def _key():
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


def turn(app, tid):
    return app.db.one("SELECT * FROM turns WHERE id = ?", tid)


def messages(app, conv_id, kind=None):
    rows = app.db.all("SELECT * FROM messages WHERE conversation_id = ? ORDER BY id", conv_id)
    return [m for m in rows if kind is None or m["kind"] == kind]
