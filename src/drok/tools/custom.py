"""Self-built tools: an agent turns a script on the computer into a tool every agent can call.

A custom tool is a shell command run in /workspace with the call's arguments as JSON on stdin (and in $DROK_ARGS).
Typical flow: write /workspace/.tools/<name>/run.py, test it with shell, then tool_create with test_args. Every call
of a custom tool goes through safety review like any other risky tool.
"""

import json
import re
import shlex

from ..db import now
from . import Tool, ToolContext, params, s

NAME = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
RESERVED = ("gmail_", "drive_", "calendar_", "mcp_")


def _schema(parameters) -> dict:
    if isinstance(parameters, str):
        parameters = json.loads(parameters or "{}")
    parameters = parameters or {}
    if parameters.get("type") == "object":
        return parameters
    # Shorthand {"arg": "description"} -> required string arguments.
    return {"type": "object", "properties": {k: {"type": "string", "description": str(v)} for k, v in parameters.items()},
            "required": list(parameters)}


async def _run(app, command: str, args: dict, screen: int) -> str:
    payload = app.vault.fill(json.dumps(args, ensure_ascii=False))
    line = f"export DROK_ARGS={shlex.quote(payload)}; printf '%s' \"$DROK_ARGS\" | ( {command} )"
    r = await app.computer.exec(line, timeout=300, screen=screen)
    out = r["output"]
    if len(out) > 20000:
        out = out[:4000] + "\n[...]\n" + out[-14000:]
    return f"exit code: {r['exit_code']}\n{out}"


def make(app, row: dict) -> Tool:
    async def fn(ctx: ToolContext, **args) -> str:
        app.db.run("UPDATE custom_tools SET uses = uses + 1 WHERE name = ?", row["name"])
        return await _run(app, row["command"], args, ctx.agent["screen"])

    return Tool(row["name"], f"[custom tool by {row['created_by']}] {row['description']}", _schema(row["parameters"]),
                fn, risk="risky", timeout=330, extra={"custom": True},
                subject=lambda a: f"{row['name']} {json.dumps(a, ensure_ascii=False)[:300]}")


async def tool_create(ctx: ToolContext, name: str, description: str, command: str, parameters: dict | None = None,
                      test_args: dict | None = None) -> str:
    app = ctx.app
    if not NAME.match(name):
        return "Error: name must be snake_case, 3-41 chars, starting with a letter."
    if name in {t.name for t in app.builtin_tools()} or name.startswith(RESERVED):
        return f"Error: '{name}' is taken by a built-in or connector tool. Pick another name."
    try:
        schema = _schema(parameters)
    except ValueError as e:
        return f"Error: parameters must be JSON: {e}"
    test = ""
    if test_args is not None:
        test = await _run(app, command, test_args, ctx.agent["screen"])
        if not test.startswith("exit code: 0"):
            return f"Not registered - the test run failed:\n{test}\nFix the script and try again."
    app.db.run(
        "INSERT INTO custom_tools (name, description, parameters, command, created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET description = excluded.description, "
        "parameters = excluded.parameters, command = excluded.command, enabled = 1, updated_at = excluded.updated_at",
        name, description.strip(), schema, command.strip(), ctx.agent["id"], now(), now())
    app.bus.publish({"type": "tools"})
    return (f"Tool '{name}' is registered for the whole team (available from the next turn)."
            + (f"\nTest run:\n{test}" if test else " Tip: pass test_args next time to verify it on creation."))


async def tool_update(ctx: ToolContext, name: str, enabled: bool | None = None, description: str = "",
                      command: str = "", parameters: dict | None = None) -> str:
    row = ctx.app.db.one("SELECT * FROM custom_tools WHERE name = ?", name)
    if not row:
        return f"No custom tool '{name}'."
    changes = {"updated_at": now()}
    if enabled is not None:
        changes["enabled"] = int(enabled)
    if description:
        changes["description"] = description
    if command:
        changes["command"] = command
    if parameters is not None:
        changes["parameters"] = _schema(parameters)
    ctx.app.db.update("custom_tools", name, key_col="name", **changes)
    ctx.app.bus.publish({"type": "tools"})
    return f"Updated '{name}'."


def registered(app) -> list[Tool]:
    return [make(app, row) for row in app.db.all("SELECT * FROM custom_tools WHERE enabled = 1 ORDER BY name")]


def management_tools(app) -> list[Tool]:
    t = {"trusted": True}
    return [
        Tool("tool_create", "Build a new tool for the whole team from a script on the computer. The command runs in "
             "/workspace and receives the call's arguments as JSON on stdin and in $DROK_ARGS; whatever it prints is "
             "the result. Write and test the script first (e.g. /workspace/.tools/<name>/run.py), then register it "
             "with test_args to prove it works.",
             params(["name", "description", "command"], name=s("snake_case tool name"),
                    description=s("What it does, when to use it, what it returns"),
                    command=s("Shell command, e.g. python3 /workspace/.tools/fx_rates/run.py"),
                    parameters={"type": "object", "description": "JSON schema of the arguments, or a shorthand map "
                                "{\"arg\": \"description\"}"},
                    test_args={"type": "object", "description": "Arguments for a verification run"}),
             tool_create, risk="risky", extra=t, subject=lambda a: f"{a.get('name')}: {a.get('command', '')}"),
        Tool("tool_update", "Change, disable or re-enable a custom tool.",
             params(["name"], name=s("Tool name"), enabled={"type": "boolean"}, description=s("New description"),
                    command=s("New command"), parameters={"type": "object"}),
             tool_update, risk="risky", extra=t, subject=lambda a: f"{a.get('name')}: {a.get('command', '')}"),
    ]
