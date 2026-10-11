"""Tool primitives. A tool is a JSON-schema'd async function plus a risk level that drives review."""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..app import App


@dataclass
class ToolResult:
    text: str
    image: str | None = None  # base64 PNG, shown to vision models


@dataclass
class ToolContext:
    app: "App"
    agent: dict
    turn: dict

    @property
    def conversation_id(self) -> int:
        return self.turn["conversation_id"]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., Awaitable[str | ToolResult]]
    risk: str = "risky"  # safe: never reviewed | risky: rules, then auto-review
    subject: Callable[[dict], str] | None = None  # what rules match against (command, url, ...)
    timeout: float = 300
    extra: dict = field(default_factory=dict)

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }

    def subject_of(self, args: dict) -> str:
        if self.subject:
            return self.subject(args)
        return json.dumps(args, sort_keys=True, ensure_ascii=False)


def params(required: list[str] | None = None, **props: Any) -> dict:
    """Shorthand for a JSON-schema object: params(["q"], q={"type": "string", "description": ...})."""
    return {"type": "object", "properties": props, "required": required or list(props)}


def s(description: str, **extra) -> dict:
    return {"type": "string", "description": description, **extra}


def i(description: str, **extra) -> dict:
    return {"type": "integer", "description": description, **extra}
