"""Claude through Anthropic's native Messages API: prompt caching, effort, and thinking kept across steps.

Drok's transcripts are OpenAI-shaped, the format every other provider speaks. This module converts them on the way in
and converts replies back, keeping each reply's raw content blocks under "blocks" so they are replayed byte for byte on
the next step: Claude's thinking is bound to the exact history it was produced in.
"""

import json
import re

from anthropic import AsyncAnthropic, BadRequestError

from .llm import LLMError

MAX_TOKENS = 64000  # thinking counts toward it; streaming keeps long replies clear of HTTP timeouts
BINDING_BETA = "thinking-binding-controls-2026-08-01"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models that take server-side refusal fallback in its "default" form (Claude API only).
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
# A rejected request that names one of these is about our tuning, not the conversation: retry it plain.
TUNING_ERROR = re.compile(r"thinking|effort|output_config|block_binding|fallback", re.I)


def _image(url: str) -> dict:
    if url.startswith("data:"):
        head, _, data = url.partition(",")
        return {"type": "image", "source": {"type": "base64", "media_type": head[5:].split(";")[0], "data": data}}
    return {"type": "image", "source": {"type": "url", "url": url}}


def _parts(content) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content.strip() else []
    out = []
    for p in content or []:
        if p.get("type") == "text" and (p.get("text") or "").strip():
            out.append({"type": "text", "text": p["text"]})
        elif p.get("type") == "image_url":
            out.append(_image(p["image_url"]["url"]))
    return out


def _assistant(m: dict) -> list[dict]:
    if m.get("blocks"):
        return m["blocks"]
    blocks = _parts(m.get("content") or "")
    for c in m.get("tool_calls") or []:
        try:
            args = json.loads(c["function"]["arguments"] or "{}")
        except ValueError:
            args = {}
        blocks.append({"type": "tool_use", "id": c["id"], "name": c["function"]["name"],
                       "input": args if isinstance(args, dict) else {}})
    return blocks


def to_anthropic(messages: list[dict]) -> tuple[str, list[dict]]:
    """OpenAI-shaped transcript -> (system, Messages API messages). Tool results become tool_result blocks at the start
    of the next user turn, as the API requires; consecutive same-role entries merge into one turn."""
    system, out = "", []
    for m in messages:
        role = m["role"]
        if role == "system" and not out and not system:
            system = m["content"]
            continue
        if role == "assistant":
            blocks = _assistant(m)
        elif role == "tool":
            role, blocks = "user", [{"type": "tool_result", "tool_use_id": m["tool_call_id"],
                                     "content": m["content"] or "(no output)"}]
        else:
            role, blocks = "user", _parts(m["content"])
        if not blocks:
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"] = out[-1]["content"] + blocks
        else:
            out.append({"role": role, "content": list(blocks)})
    if out and out[-1]["role"] == "assistant":  # current models reject a trailing assistant turn (prefill)
        out.append({"role": "user", "content": [{"type": "text", "text": "Continue."}]})
    return system, out


def from_anthropic(msg) -> dict:
    """A Messages API reply -> Drok's assistant message, with the raw blocks kept for replay."""
    if msg.stop_reason == "refusal":
        d = msg.stop_details
        raise LLMError(f"the model declined ({(d and d.category) or 'policy'})"
                       + (f": {d.explanation}" if d and d.explanation else ""))
    # to_dict keeps the API's own key names (e.g. "from", not the SDK's from_) and only what the API sent.
    blocks = [b.to_dict(mode="json", exclude_none=True) for b in msg.content]
    # After a mid-reply fallback only the declined attempt's text may be echoed back (Anthropic's rule); the
    # fallback block itself is just a marker.
    cut = max((i for i, b in enumerate(blocks) if b["type"] == "fallback"), default=-1)
    if cut >= 0:
        blocks = [b for b in blocks[:cut] if b["type"] == "text"] + blocks[cut + 1:]
    text = "\n".join(b["text"] for b in blocks if b["type"] == "text").strip()
    calls = [{"id": b["id"], "type": "function", "function": {"name": b["name"], "arguments": json.dumps(b["input"])}}
             for b in blocks if b["type"] == "tool_use"]
    if msg.stop_reason == "max_tokens" and calls:
        # A call cut off half-way can't run, and a tool_use sent back without its result is rejected.
        return {"role": "assistant", "content": (text + "\n\n[reply cut off: it ran past the length limit]").strip()}
    out = {"role": "assistant", "content": text, "blocks": blocks}
    if calls:
        out["tool_calls"] = calls
    return out


def tool_defs(tools: list[dict]) -> list[dict]:
    return [{"name": t["function"]["name"], "description": t["function"].get("description", ""),
             "input_schema": t["function"].get("parameters") or {"type": "object", "properties": {}}} for t in tools]


class Claude:
    def __init__(self, api_key: str, base_url: str):
        # Drok's ANTHROPIC_BASE_URL points at the OpenAI-compatible /v1/ path; the SDK wants the bare host.
        self.client = AsyncAnthropic(api_key=api_key, base_url=re.sub(r"/v1/?$", "", base_url.rstrip("/")),
                                     max_retries=4)
        self.plain: set[str] = set()  # models that rejected adaptive thinking / effort / fallbacks

    async def create(self, model: str, messages: list[dict], tools: list[dict] | None = None, effort: str = "",
                     cache: bool = True, max_tokens: int = MAX_TOKENS):
        system, msgs = to_anthropic(messages)
        base: dict = {"model": model, "max_tokens": max_tokens, "messages": msgs}
        if system:
            base["system"] = system
        if tools:
            base["tools"] = tool_defs(tools)
        if cache:  # moves forward with the transcript, so every step re-reads the previous one at ~10% of the price
            base["cache_control"] = {"type": "ephemeral"}
        tuned, betas = {}, []
        if model not in self.plain:
            # Drok compresses long transcripts and prunes old screenshots: drop the thinking those edits invalidate
            # instead of failing the request.
            tuned["thinking"] = {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
            betas.append(BINDING_BETA)
            if effort:
                tuned["output_config"] = {"effort": effort}
            if model in FALLBACK_MODELS:
                tuned["fallbacks"] = "default"
                betas.append(FALLBACK_BETA)
        try:
            return await self._send({**base, **tuned}, betas)
        except BadRequestError as e:
            if not tuned or not TUNING_ERROR.search(str(e)):
                raise
            msg = await self._send(base, [])  # older Claude models take none of the tuning
            self.plain.add(model)
            return msg

    async def _send(self, kwargs: dict, betas: list[str]):
        async with self.client.beta.messages.stream(**kwargs, **({"betas": betas} if betas else {})) as stream:
            return await stream.get_final_message()
