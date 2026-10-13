"""Claude's native API path: transcript conversion, replies, request tuning, and usage with cache tokens."""

import httpx2
import pytest
from anthropic import BadRequestError
from anthropic.types.beta import BetaMessage
from conftest import call, say

from drok.claude import BINDING_BETA, FALLBACK_BETA, Claude, from_anthropic, to_anthropic
from drok.db import DB
from drok.llm import LLM, LLMError, Provider


def reply(*content, stop="end_turn", usage=None, stop_details=None) -> BetaMessage:
    return BetaMessage.model_validate({
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5", "content": list(content),
        "stop_reason": stop, "stop_details": stop_details, "stop_sequence": None,
        "usage": usage or {"input_tokens": 10, "output_tokens": 5}})


THINK = {"type": "thinking", "thinking": "", "signature": "sig-1"}
USE = {"type": "tool_use", "id": "toolu_1", "name": "shell", "input": {"command": "ls"}}


def test_transcript_converts_to_messages_api_shape():
    tool_turn = call("shell", {"command": "ls"}, "toolu_a")
    tool_turn["tool_calls"].append(call("read_file", {"path": "x"}, "toolu_b")["tool_calls"][0])
    system, msgs = to_anthropic([
        {"role": "system", "content": "be useful"},
        {"role": "user", "content": "hi"},
        {"role": "user", "content": "and also this"},
        tool_turn,
        {"role": "tool", "tool_call_id": "toolu_a", "content": "a.txt"},
        {"role": "tool", "tool_call_id": "toolu_b", "content": ""},
        {"role": "user", "content": [{"type": "text", "text": "[screen image]"},
                                     {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBOR"}}]},
        {"role": "assistant", "content": "done", "blocks": [THINK, {"type": "text", "text": "done"}]},
    ])
    assert system == "be useful"
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant", "user"]
    assert [b["text"] for b in msgs[0]["content"]] == ["hi", "and also this"]
    assert [b["type"] for b in msgs[1]["content"]] == ["tool_use", "tool_use"] and msgs[1]["content"][0]["input"] == {"command": "ls"}
    results = msgs[2]["content"]
    assert [b["type"] for b in results] == ["tool_result", "tool_result", "text", "image"]  # results first, together
    assert results[1]["content"] == "(no output)"
    assert results[3]["source"] == {"type": "base64", "media_type": "image/png", "data": "iVBOR"}
    assert msgs[3]["content"][0] is THINK  # replayed byte for byte
    assert msgs[4]["content"] == [{"type": "text", "text": "Continue."}]  # never end on an assistant turn


def test_reply_keeps_raw_blocks_and_maps_tool_calls():
    out = from_anthropic(reply(THINK, {"type": "text", "text": "Listing."}, USE, stop="tool_use"))
    assert out["content"] == "Listing." and out["blocks"][0] == THINK
    assert out["tool_calls"] == [{"id": "toolu_1", "type": "function",
                                  "function": {"name": "shell", "arguments": '{"command": "ls"}'}}]
    _, msgs = to_anthropic([{"role": "user", "content": "go"}, out])
    assert msgs[1]["content"] == out["blocks"]


def test_refusal_cutoff_and_fallback_echo_rules():
    with pytest.raises(LLMError, match="declined \\(cyber\\)"):
        from_anthropic(reply(stop="refusal", stop_details={"type": "refusal", "category": "cyber", "explanation": None}))
    cut = from_anthropic(reply({"type": "text", "text": "Writing"}, USE, stop="max_tokens"))
    assert "tool_calls" not in cut and "blocks" not in cut and "cut off" in cut["content"]
    fell = from_anthropic(reply(THINK, {"type": "text", "text": "partial"}, USE,
                                {"type": "fallback", "from": {"model": "claude-sonnet-5-5"}, "to": {"model": "claude-sonnet-5"},
                                 "trigger": {"type": "refusal", "category": "cyber"}},
                                {"type": "text", "text": "rest"}))
    assert [b.get("text") for b in fell["blocks"]] == ["partial", "rest"]  # only text before the switch is echoed
    assert "tool_calls" not in fell
    from anthropic.types.beta import BetaFallbackBlock
    fb = BetaFallbackBlock.model_validate({"type": "fallback", "from": {"model": "a"}, "to": {"model": "b"},
                                           "trigger": {"type": "refusal"}})
    assert "from" in fb.to_dict(mode="json")  # API key names, the shape the API accepts back


class Recorder(Claude):
    def __init__(self, fail_tuned: str = ""):
        super().__init__("key", "https://api.anthropic.com/v1/")
        self.sent, self.fail_tuned = [], fail_tuned

    async def _send(self, kwargs, betas):
        self.sent.append((kwargs, betas))
        if self.fail_tuned and "thinking" in kwargs:
            raise BadRequestError(self.fail_tuned, body=None,
                                  response=httpx2.Response(400, request=httpx2.Request("POST", "https://x")))
        return reply({"type": "text", "text": "ok"})


async def test_requests_cache_keep_thinking_and_fall_back_server_side():
    c = Recorder()
    assert str(c.client.base_url).rstrip("/") == "https://api.anthropic.com"
    tools = [{"type": "function", "function": {"name": "shell", "description": "Run", "parameters": {"type": "object"}}}]
    await c.create("claude-sonnet-5-5", [{"role": "user", "content": "hi"}], tools, effort="medium")
    kw, betas = c.sent[-1]
    assert kw["cache_control"] == {"type": "ephemeral"} and kw["output_config"] == {"effort": "medium"}
    assert kw["thinking"]["block_binding"] == {"prefix_mismatch_behavior": "drop_block"}
    assert kw["fallbacks"] == "default" and set(betas) == {BINDING_BETA, FALLBACK_BETA}
    assert kw["tools"] == [{"name": "shell", "description": "Run", "input_schema": {"type": "object"}}]
    await c.create("claude-haiku-5-5", [{"role": "user", "content": "hi"}], cache=False)
    kw, betas = c.sent[-1]
    assert "fallbacks" not in kw and "cache_control" not in kw and "output_config" not in kw and betas == [BINDING_BETA]


async def test_older_models_get_a_plain_request_once_they_reject_the_tuning():
    c = Recorder(fail_tuned="thinking.type.adaptive is not supported on this model")
    await c.create("claude-old", [{"role": "user", "content": "hi"}], effort="low")
    assert "thinking" not in c.sent[-1][0] and c.sent[-1][1] == [] and "claude-old" in c.plain
    await c.create("claude-old", [{"role": "user", "content": "again"}])
    assert len(c.sent) == 3  # no second failing attempt
    other = Recorder(fail_tuned="messages.1: tool_use ids must be unique")
    with pytest.raises(BadRequestError):
        await other.create("claude-sonnet-5-5", [{"role": "user", "content": "hi"}])
    assert not other.plain  # a broken conversation is not a reason to drop the tuning


async def test_llm_routes_anthropic_natively_and_records_cache_usage(tmp_path):
    db = DB(tmp_path / "t.db")
    llm = LLM(db, providers={"anthropic": Provider("anthropic", "https://api.anthropic.com/v1/", "k")})
    seen = {}

    class Fake:
        async def create(self, name, messages, tools, effort, cache):
            seen.update(name=name, effort=effort, cache=cache)
            return reply({"type": "text", "text": "hi"}, usage={
                "input_tokens": 100, "output_tokens": 7, "cache_read_input_tokens": 9000,
                "cache_creation_input_tokens": 400})

    llm._claude = Fake()
    msg, used = await llm.chat("anthropic/claude-sonnet-5-5", [say("x")], reasoning="medium", agent_id="forge")
    assert used == "anthropic/claude-sonnet-5-5" and msg["content"] == "hi"
    assert seen == {"name": "claude-sonnet-5-5", "effort": "medium", "cache": True}
    row = db.one("SELECT * FROM usage")
    assert (row["prompt_tokens"], row["cache_read_tokens"], row["cache_write_tokens"]) == (9500, 9000, 400)
    await llm.text("anthropic/claude-haiku-5-5", "system", "prompt")
    assert seen["cache"] is False  # one-off prompts never pay for a cache write
