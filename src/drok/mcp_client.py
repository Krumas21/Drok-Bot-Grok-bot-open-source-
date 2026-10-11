"""MCP servers configured in the UI become tools for every agent (account-wide, like Grok Bot connectors)."""

import asyncio
import logging
import os
import re
import shlex

from mcp import Client, StdioServerParameters

from .tools import Tool, ToolContext

log = logging.getLogger("drok.mcp")


class MCPManager:
    def __init__(self, app):
        self.app = app
        self.clients: dict[str, Client] = {}
        self.tool_defs: dict[str, list] = {}
        self.errors: dict[str, str] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._stops: dict[str, asyncio.Event] = {}

    async def start(self) -> None:
        for row in self.app.db.all("SELECT * FROM mcp_servers WHERE enabled = 1"):
            self._launch(row)

    async def reload(self) -> None:
        await self.stop()
        await self.start()

    async def stop(self) -> None:
        for ev in self._stops.values():
            ev.set()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        self._stops.clear()
        self.clients.clear()
        self.tool_defs.clear()

    def _launch(self, row: dict) -> None:
        stop = asyncio.Event()
        self._stops[row["name"]] = stop
        self._tasks[row["name"]] = asyncio.create_task(self._serve(row, stop), name=f"mcp:{row['name']}")

    def _target(self, row: dict):
        target = self.app.vault.fill(row["target"].strip())
        if re.match(r"https?://", target):
            return target
        parts = shlex.split(target)
        env = {**os.environ, **{k: self.app.vault.fill(str(v)) for k, v in (row["env"] or {}).items()}}
        return StdioServerParameters(command=parts[0], args=parts[1:], env=env)

    async def _serve(self, row: dict, stop: asyncio.Event) -> None:
        """Keep one session open per server; reconnect with backoff if it drops."""
        name, delay = row["name"], 5
        while not stop.is_set():
            try:
                async with Client(self._target(row), read_timeout_seconds=120) as client:
                    tools, cursor = [], None
                    while True:
                        page = await client.list_tools(cursor=cursor)
                        tools += page.tools
                        cursor = page.next_cursor
                        if not cursor:
                            break
                    self.clients[name], self.tool_defs[name] = client, tools
                    self.errors.pop(name, None)
                    log.info("MCP %s connected with %d tools", name, len(tools))
                    delay = 5
                    await stop.wait()
            except Exception as e:
                self.errors[name] = f"{e.__class__.__name__}: {e}"
                log.warning("MCP %s failed: %s", name, e)
            finally:
                self.clients.pop(name, None)
                self.tool_defs.pop(name, None)
            if not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except TimeoutError:
                    delay = min(delay * 2, 300)

    def tools(self) -> list[Tool]:
        out = []
        for server, defs in self.tool_defs.items():
            for d in defs:
                tool_name = re.sub(r"[^a-zA-Z0-9_-]", "_", f"mcp_{server}_{d.name}")[:64]

                async def call(ctx: ToolContext, _server=server, _tool=d.name, **args) -> str:
                    client = self.clients.get(_server)
                    if not client:
                        return f"MCP server '{_server}' is disconnected: {self.errors.get(_server, 'reconnecting')}"
                    res = await client.call_tool(_tool, {k: ctx.app.vault.fill(v) if isinstance(v, str) else v
                                                         for k, v in args.items()})
                    text = "\n".join(getattr(c, "text", f"[{c.type} content]") for c in res.content)
                    return f"Error from {_tool}: {text}" if res.is_error else text or "(no content)"

                out.append(Tool(tool_name, (d.description or d.name)[:1000],
                                d.input_schema or {"type": "object", "properties": {}}, call, risk="risky"))
        return out
