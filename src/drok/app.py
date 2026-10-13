"""Wires every subsystem together and owns agent/conversation bookkeeping."""

import asyncio
import logging
import re
import time
from fnmatch import fnmatchcase
from pathlib import Path

import yaml

from .bus import Bus
from .computer import Computer
from .config import Settings
from .db import DB, now
from .llm import LLM
from .review import Reviewer
from .tools import Tool
from .vault import Vault

log = logging.getLogger("drok")

SEEDS = Path(__file__).parent / "seeds"
COLORS = ["#ff6b35", "#3a86ff", "#06d6a0", "#ffbe0b", "#c77dff", "#ef476f", "#4cc9f0", "#90be6d"]


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "agent"


def parse_frontmatter(text: str) -> tuple[dict, str]:
    if text.startswith("---"):
        _, fm, body = text.split("---", 2)
        return yaml.safe_load(fm) or {}, body.strip()
    return {}, text.strip()


class App:
    def __init__(self, settings: Settings, llm: LLM | None = None, computer: Computer | None = None):
        from .goals import Goals
        from .mcp_client import MCPManager
        from .memory import Memory
        from .runtime import Runtime
        from .scheduler import Scheduler
        from .skills import Skills
        from .teach import Teacher
        from .tools.computer import Browsers
        from .tools.google import Google

        self.settings = settings
        self.db = DB(settings.data_dir / "drok.db")
        self.bus = Bus()
        self.llm = llm or LLM(self.db)
        self.vault = Vault(self.db, settings.secret_key)
        self.reviewer = Reviewer(self.db, self.llm, settings.reviewer_model, settings.auto_review)
        self.computer = computer or Computer(settings.computer_url, settings.computer_token, settings.screens)
        self.browsers = Browsers(self.computer)
        self.skills = Skills(settings.workspace / ".skills", SEEDS / "skills", self.db,
                             legacy_root=settings.data_dir / "skills")
        self.memory = Memory(self)
        self.goals = Goals(self)
        self.google = Google(self)
        self.mcp = MCPManager(self)
        self.runtime = Runtime(self)
        self.scheduler = Scheduler(self)
        self.teacher = Teacher(self)
        self.telegram = None  # set by channels.telegram when configured
        self._tasks: list[asyncio.Task] = []
        self.seed_agents()

    # ---------- lifecycle ----------
    async def start(self) -> None:
        from .channels.telegram import Telegram

        await self.mcp.start()
        self._tasks.append(asyncio.create_task(self.runtime.run(), name="runtime"))
        self._tasks.append(asyncio.create_task(self.scheduler.run(), name="scheduler"))
        if self.settings.telegram_token:
            self.telegram = Telegram(self)
            await self.telegram.start()
        log.info("Drok started with %d agents", len(self.agents()))

    async def stop(self) -> None:
        if self.telegram:
            await self.telegram.stop()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.mcp.stop()
        await self.browsers.close()

    # ---------- agents ----------
    def agents(self) -> list[dict]:
        return self.db.all("SELECT * FROM agents ORDER BY is_orchestrator DESC, created_at")

    def agent(self, agent_id: str) -> dict | None:
        return self.db.one("SELECT * FROM agents WHERE id = ?", agent_id)

    def find_agent(self, ref: str) -> dict | None:
        ref = ref.strip().lstrip("@").lower()
        for a in self.agents():
            if ref in (a["id"], a["name"].lower(), slugify(a["name"])):
                return a
        return None

    def orchestrator(self) -> dict | None:
        return self.db.one("SELECT * FROM agents WHERE is_orchestrator = 1 ORDER BY created_at LIMIT 1") or (
            self.agents()[0] if self.agents() else None
        )

    def create_agent(self, name: str, title: str = "", description: str = "", system_prompt: str = "",
                     model: str = "", fallbacks: list[str] | None = None, tools: list[str] | None = None,
                     is_orchestrator: bool = False, agent_id: str = "", color: str = "") -> dict:
        agent_id = agent_id or slugify(name)
        base, n = agent_id, 2
        while self.agent(agent_id):
            agent_id, n = f"{base}-{n}", n + 1
        count = len(self.agents())
        if is_orchestrator:
            self.db.run("UPDATE agents SET is_orchestrator = 0")
        self.db.insert(
            "agents", id=agent_id, name=name, title=title, description=description, system_prompt=system_prompt,
            model=model or self.settings.default_model, fallbacks=fallbacks or [], tools=tools or ["*"],
            screen=count % max(self.settings.screens, 1) + 1, is_orchestrator=int(is_orchestrator),
            color=color or COLORS[count % len(COLORS)], created_at=now(),
        )
        self.dm(agent_id)
        self.bus.publish({"type": "agents"})
        return self.agent(agent_id)

    def delete_agent(self, agent_id: str) -> None:
        self.runtime.stop(agent_id)
        self.db.run("DELETE FROM routines WHERE agent_id = ?", agent_id)
        self.db.run("DELETE FROM agents WHERE id = ?", agent_id)
        self.db.run("UPDATE turns SET status = 'cancelled' WHERE agent_id = ? AND status IN ('queued','waiting')",
                    agent_id)
        self.bus.publish({"type": "agents"})

    def seed_agents(self) -> None:
        if self.agents() or self.db.get_kv("agents_seeded"):
            return
        for path in sorted((SEEDS / "agents").glob("*.md")):
            meta, body = parse_frontmatter(path.read_text())
            self.create_agent(
                name=meta["name"], title=meta.get("title", ""), description=meta.get("description", ""),
                system_prompt=body, model=meta.get("model", ""), is_orchestrator=bool(meta.get("orchestrator")),
                agent_id=meta.get("id", ""), color=meta.get("color", ""),
            )
        self.db.set_kv("agents_seeded", True)

    # ---------- conversations ----------
    def dm(self, agent_id: str) -> dict:
        conv = self.db.one(
            "SELECT * FROM conversations WHERE kind = 'dm' AND members = ?", f'["{agent_id}"]'
        )
        if conv:
            return conv
        cid = self.db.insert("conversations", kind="dm", members=[agent_id], created_at=now())
        return self.conversation(cid)

    def conversation(self, cid: int) -> dict | None:
        return self.db.one("SELECT * FROM conversations WHERE id = ?", cid)

    def create_group(self, title: str, members: list[str]) -> dict:
        members = [m for m in dict.fromkeys(members) if self.agent(m)]
        if not 2 <= len(members) <= 6:
            raise ValueError("A group needs 2 to 6 agents")
        cid = self.db.insert("conversations", kind="group", title=title or "Group", members=members,
                             created_at=now())
        self.bus.publish({"type": "agents"})
        return self.conversation(cid)

    def groups(self) -> list[dict]:
        return self.db.all("SELECT * FROM conversations WHERE kind = 'group' ORDER BY created_at")

    # ---------- tools ----------
    def builtin_tools(self) -> list[Tool]:
        from .tools import computer, custom, google, mind, system, team, web

        return [*mind.tools(self), *system.tools(self), *web.tools(self), *computer.tools(self), *team.tools(self),
                *custom.management_tools(self), *google.tools(self)]

    def all_tools(self) -> dict[str, Tool]:
        from .tools import custom

        tools = [*self.builtin_tools(), *custom.registered(self), *self.mcp.tools()]
        return {t.name: t for t in tools}

    def tools_for(self, agent: dict) -> dict[str, Tool]:
        patterns = agent["tools"] or ["*"]
        allowed = {n: t for n, t in self.all_tools().items() if any(fnmatchcase(n, p) for p in patterns)
                   and (agent["is_orchestrator"] or not t.extra.get("lead_only"))}
        return allowed

    def aux_model(self, agent: dict | None) -> str:
        """Model for background work (summaries, compression, reflection, verification)."""
        return self.settings.aux_model or self.settings.reviewer_model or (agent or {}).get("model") \
            or self.settings.default_model

    def connections(self) -> list[str]:
        out = []
        email = self.google.email()
        if email:
            out.append(f"Google as {email} (gmail_*, drive_*, calendar_* tools)")
        for name, defs in self.mcp.tool_defs.items():
            out.append(f"MCP '{name}' ({len(defs)} tools, named mcp_{name}_*)")
        return out

    def inbox_file(self, name: str) -> Path:
        """Where a file the owner sends lands: /workspace/inbox/<time>_<name>, with a name that is safe to paste
        into a shell command or a link."""
        inbox = self.settings.workspace / "inbox"
        inbox.mkdir(exist_ok=True)
        safe = re.sub(r"[^\w.\-]+", "_", Path(name).name).strip("._") or "file"
        return inbox / f"{int(time.time())}_{safe}"

    def touch_user(self) -> None:
        """User was here: keeps routines from auto-pausing for absence."""
        self.db.set_kv("last_user_activity", now())
        self.db.set_kv("absence_warned", False)
