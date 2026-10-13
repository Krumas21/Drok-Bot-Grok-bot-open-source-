"""Any LLM provider through its OpenAI-compatible endpoint.

Model specs look like "provider/model", e.g. "anthropic/claude-sonnet-5-5", "openrouter/x-ai/grok-4",
"ollama/qwen3:32b". Only the first slash splits provider from model.
"""

import logging
import os
import re
from dataclasses import dataclass

from openai import AsyncOpenAI, BadRequestError

from .db import DB, now

log = logging.getLogger("drok.llm")

# ponytail: Anthropic/Gemini via their OpenAI-compat layers (no prompt caching); add native adapters if cost matters.
PRESETS: dict[str, tuple[str, str]] = {
    "openai": ("OPENAI_API_KEY", "https://api.openai.com/v1"),
    "anthropic": ("ANTHROPIC_API_KEY", "https://api.anthropic.com/v1/"),
    "gemini": ("GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai/"),
    "xai": ("XAI_API_KEY", "https://api.x.ai/v1"),
    "openrouter": ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1"),
    "groq": ("GROQ_API_KEY", "https://api.groq.com/openai/v1"),
    "deepseek": ("DEEPSEEK_API_KEY", "https://api.deepseek.com/v1"),
    "mistral": ("MISTRAL_API_KEY", "https://api.mistral.ai/v1"),
    "together": ("TOGETHER_API_KEY", "https://api.together.xyz/v1"),
}


@dataclass
class Provider:
    name: str
    base_url: str
    api_key: str


class LLMError(Exception):
    pass


def discover_providers(env: dict[str, str] | None = None) -> dict[str, Provider]:
    env = dict(os.environ if env is None else env)
    found: dict[str, Provider] = {}
    for name, (key_var, url) in PRESETS.items():
        if env.get(key_var):
            found[name] = Provider(name, env.get(f"{name.upper()}_BASE_URL", url), env[key_var])
    if env.get("OLLAMA_BASE_URL"):
        found["ollama"] = Provider("ollama", env["OLLAMA_BASE_URL"], "ollama")
    # Any OpenAI-compatible server: DROK_PROVIDER_<NAME>_BASE_URL (+ optional _API_KEY)
    for var, value in env.items():
        m = re.fullmatch(r"DROK_PROVIDER_([A-Z0-9_]+)_BASE_URL", var)
        if m and value:
            name = m.group(1).lower()
            found[name] = Provider(name, value, env.get(f"DROK_PROVIDER_{m.group(1)}_API_KEY") or "none")
    return found


def split_model(spec: str) -> tuple[str, str]:
    provider, _, model = spec.partition("/")
    if not model:
        raise LLMError(f"Model '{spec}' must look like provider/model, e.g. openai/gpt-5-mini")
    return provider, model


def _clean(message) -> dict:
    """Keep only fields every provider accepts back as input (drops reasoning blobs etc)."""
    out: dict = {"role": "assistant", "content": message.content or ""}
    if message.tool_calls:
        out["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"},
            }
            for tc in message.tool_calls
        ]
    return out


class LLM:
    def __init__(self, db: DB | None = None, providers: dict[str, Provider] | None = None):
        self.db = db
        self.providers = discover_providers() if providers is None else providers
        self._clients: dict[str, AsyncOpenAI] = {}

    def client(self, provider: str) -> AsyncOpenAI:
        if provider not in self.providers:
            raise LLMError(
                f"Provider '{provider}' is not configured. Set its API key in .env "
                f"(configured: {', '.join(self.providers) or 'none'})."
            )
        if provider not in self._clients:
            p = self.providers[provider]
            # SDK retries 429/5xx/connection errors with exponential backoff.
            self._clients[provider] = AsyncOpenAI(api_key=p.api_key, base_url=p.base_url, max_retries=4, timeout=300)
        return self._clients[provider]

    async def chat(
        self, model: str, messages: list[dict], tools: list[dict] | None = None,
        fallbacks: list[str] = (), agent_id: str = "", reasoning: str = "",
    ) -> tuple[dict, str]:
        """Returns (assistant message dict, model actually used). Tries fallbacks in order."""
        errors = []
        for spec in [model, *fallbacks]:
            try:
                provider, name = split_model(spec)
                kwargs = {"model": name, "messages": messages}
                if tools:
                    kwargs["tools"] = tools
                if reasoning in ("low", "medium", "high"):
                    kwargs["reasoning_effort"] = reasoning
                try:
                    resp = await self.client(provider).chat.completions.create(**kwargs)
                except BadRequestError:
                    if "reasoning_effort" not in kwargs:
                        raise
                    # Not every model takes a reasoning effort; retry plainly rather than failing the turn.
                    kwargs.pop("reasoning_effort")
                    resp = await self.client(provider).chat.completions.create(**kwargs)
                if not resp.choices:
                    raise LLMError("empty response")
                self._record(agent_id, spec, resp.usage)
                return _clean(resp.choices[0].message), spec
            except Exception as e:  # noqa: BLE001 - any provider failure moves on to the next fallback
                log.warning("model %s failed: %s", spec, e)
                errors.append(f"{spec}: {e}")
        raise LLMError("All models failed. " + " | ".join(errors))

    async def text(self, model: str, system: str, prompt: str, agent_id: str = "") -> str:
        msg, _ = await self.chat(
            model, [{"role": "system", "content": system}, {"role": "user", "content": prompt}], agent_id=agent_id
        )
        return msg["content"].strip()

    async def check(self, spec: str) -> str:
        """Why `spec` can't be used, or "" when it answers. One tiny real call: a typo in a model name only shows
        up as a provider 404, so checking the format alone isn't enough."""
        try:
            await self.text(spec, "Reply with: ok", "ping")
        except LLMError as e:
            return str(e).removeprefix("All models failed. ")
        return ""

    async def transcribe(self, spec: str, filename: str, data: bytes) -> str:
        provider, name = split_model(spec)
        r = await self.client(provider).audio.transcriptions.create(model=name, file=(filename, data))
        return r.text

    async def speak(self, spec: str, text: str, voice: str) -> bytes:
        provider, name = split_model(spec)
        r = await self.client(provider).audio.speech.create(
            model=name, voice=voice, input=text[:4000], response_format="opus"
        )
        return r.content

    def _record(self, agent_id: str, spec: str, usage) -> None:
        if not (self.db and usage):
            return
        self.db.insert(
            "usage", agent_id=agent_id or "-", model=spec, created_at=now(),
            prompt_tokens=usage.prompt_tokens or 0, completion_tokens=usage.completion_tokens or 0,
        )
