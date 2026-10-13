"""Agent runtime: one worker per agent consuming a durable, DB-backed inbox of turns.

Durability rules (the reason this survives crashes and restarts):
- Every unit of work is a row in `turns` before anything runs.
- The LLM transcript of a turn is persisted after every step, so a restart resumes mid-turn.
- Waiting for a human (approval) is a state, not a blocked coroutine: the turn is parked as 'waiting' and re-queued
  when the decision lands, then picks up exactly at the unanswered tool calls.
- Long turns compress their own middle into a structured checkpoint instead of running out of context.
"""

import asyncio
import json
import logging
import re
import time

from . import context
from .db import now
from .llm import LLMError
from .tools import Tool, ToolContext, ToolResult

log = logging.getLogger("drok.runtime")

MAX_HOPS = 8  # agent-to-agent message chain limit, stops two agents ping-ponging forever
REFLECT_AFTER_CALLS = 5
CORRECTION = re.compile(r"\b(no[,.!]|don'?t|stop|wrong|instead|actually|prefer|always|never|remember|next time)\b",
                        re.I)


def _unanswered(msgs: list[dict]) -> list[dict]:
    """Tool calls of the last assistant message that have no tool result yet."""
    for idx in range(len(msgs) - 1, -1, -1):
        m = msgs[idx]
        if m["role"] == "assistant":
            answered = {x.get("tool_call_id") for x in msgs[idx + 1:] if x["role"] == "tool"}
            return [c for c in m.get("tool_calls") or [] if c["id"] not in answered]
    return []


def _count_calls(msgs: list[dict]) -> int:
    return sum(len(m.get("tool_calls") or []) for m in msgs if m["role"] == "assistant")


class Runtime:
    def __init__(self, app):
        self.app = app
        self.db = app.db
        self.wake: dict[str, asyncio.Event] = {}
        self.current: dict[str, asyncio.Task] = {}
        self.workers: dict[str, asyncio.Task] = {}
        self._background: set[asyncio.Task] = set()

    # ---------- messages ----------
    def post(self, conversation_id: int, author: str, content: str, kind: str = "text", meta: dict | None = None) -> dict:
        mid = self.db.insert("messages", conversation_id=conversation_id, author=author, kind=kind,
                             content=content, meta=meta or {}, created_at=now())
        msg = self.db.one("SELECT * FROM messages WHERE id = ?", mid)
        self.app.bus.publish({"type": "message", "message": msg})
        return msg

    def update_message(self, mid: int, content: str | None = None, **meta_changes) -> None:
        msg = self.db.one("SELECT * FROM messages WHERE id = ?", mid)
        if not msg:
            return
        meta = {**msg["meta"], **meta_changes}
        values = {"meta": meta}
        if content is not None:
            values["content"] = content
        self.db.update("messages", mid, **values)
        self.app.bus.publish({"type": "message_updated", "message": {**msg, **values}})

    def update_message_meta(self, mid: int, **changes) -> None:
        self.update_message(mid, None, **changes)

    def set_status(self, agent_id: str, status: str) -> None:
        agent = self.app.agent(agent_id)
        if not agent or agent["status"] == status:
            return
        self.db.update("agents", agent_id, status=status)
        self.app.bus.publish({"type": "status", "agent_id": agent_id, "status": status})

    def spawn(self, coro) -> None:
        """Fire-and-forget background work that must not be garbage-collected mid-flight."""
        task = asyncio.get_running_loop().create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # ---------- inbox ----------
    def enqueue(self, agent_id: str, conversation_id: int, trigger: str, source: str = "user",
                meta: dict | None = None) -> int:
        tid = self.db.insert("turns", agent_id=agent_id, conversation_id=conversation_id, trigger=trigger,
                             source=source, meta=meta or {}, created_at=now(), updated_at=now())
        self._wake(agent_id)
        return tid

    def _wake(self, agent_id: str) -> None:
        try:
            self.wake.setdefault(agent_id, asyncio.Event()).set()
        except RuntimeError:  # called outside the event loop (e.g. a sync test); the worker polls anyway
            pass

    def user_message(self, conversation_id: int, text: str, source: str = "web", meta: dict | None = None) -> dict:
        """Entry point for everything the owner says, from any channel."""
        conv = self.app.conversation(conversation_id)
        if not conv:
            raise ValueError("Unknown conversation")
        self.app.touch_user()
        msg = self.post(conversation_id, "user", text, meta={"source": source, **(meta or {})})
        targets = conv["members"] if conv["kind"] == "dm" else self._route_group(conv, text)
        for agent_id in targets:
            self.enqueue(agent_id, conversation_id, text, source, {"message_id": msg["id"], **(meta or {})})
        return msg

    def _mentions(self, conv: dict, text: str) -> list[str]:
        if "@everyone" in text:
            return list(conv["members"])
        found = []
        for ref in re.findall(r"@([\w\-]+)", text):
            a = self.app.find_agent(ref)
            if a and a["id"] in conv["members"] and a["id"] not in found:
                found.append(a["id"])
        return found

    def _route_group(self, conv: dict, text: str) -> list[str]:
        mentioned = self._mentions(conv, text)
        if mentioned:
            return mentioned
        orch = self.app.orchestrator()
        if orch and orch["id"] in conv["members"]:
            return [orch["id"]]
        # ponytail: no LLM router - first member owns unaddressed group messages; add a router call if groups get big.
        return [conv["members"][0]]

    def handoff(self, from_agent: dict, to_agent: dict, text: str, conversation_id: int, hops: int) -> str:
        if hops >= MAX_HOPS:
            return "Not sent: this request has bounced between agents too many times. Finish it yourself or ask the user."
        conv = self.app.conversation(conversation_id)
        if conv and conv["kind"] == "group" and to_agent["id"] in conv["members"]:
            target_conv = conversation_id
        else:
            target_conv = self.app.dm(to_agent["id"])["id"]
        self.post(conversation_id, from_agent["id"], text, kind="handoff", meta={"to": to_agent["id"]})
        if target_conv != conversation_id:
            self.post(target_conv, from_agent["id"], text, kind="handoff", meta={"to": to_agent["id"]})
        self.enqueue(
            to_agent["id"], target_conv, f"[Handoff from {from_agent['name']}]: {text}", "handoff",
            {"from_agent": from_agent["id"], "reply_conversation": conversation_id, "hops": hops + 1},
        )
        return f"Sent to {to_agent['name']}. Their reply will arrive as a new message; continue with other work or end your turn."

    def stop(self, agent_id: str) -> bool:
        task = self.current.get(agent_id)
        if task and not task.done():
            task.cancel()
            return True
        return False

    # ---------- approvals ----------
    def resolve_approval(self, approval_id: int, approved: bool, args: dict | None = None,
                         always: bool = False) -> dict | None:
        ap = self.db.one("SELECT * FROM approvals WHERE id = ?", approval_id)
        if not ap or ap["status"] != "pending":
            return ap
        self.app.touch_user()
        status = "approved" if approved else "denied"
        self.db.update("approvals", approval_id, status=status, decided_at=now(), args=args or ap["args"])
        if approved and always:
            tool = self.app.all_tools().get(ap["tool"])
            if tool:
                self.app.reviewer.always_allow(tool, args or ap["args"])
        for m in self.db.all("SELECT id, meta FROM messages WHERE kind = 'approval'"):
            if m["meta"].get("approval_id") == approval_id:
                self.update_message_meta(m["id"], status=status)
        self.app.bus.publish({"type": "approval_resolved", "approval_id": approval_id, "status": status})
        if not self.db.one("SELECT id FROM approvals WHERE turn_id = ? AND status = 'pending'", ap["turn_id"]):
            self.db.run("UPDATE turns SET status = 'queued', updated_at = ? WHERE id = ? AND status = 'waiting'",
                        now(), ap["turn_id"])
            self._wake(ap["agent_id"])
        return self.db.one("SELECT * FROM approvals WHERE id = ?", approval_id)

    # ---------- workers ----------
    async def run(self) -> None:
        # Anything that was mid-flight when the process died resumes from its persisted transcript.
        self.db.run("UPDATE turns SET status = 'queued' WHERE status = 'running'")
        self.db.run("UPDATE agents SET status = 'idle'")
        while True:
            for agent in self.app.agents():
                w = self.workers.get(agent["id"])
                if not w or w.done():
                    self.workers[agent["id"]] = asyncio.create_task(self._worker(agent["id"]), name=agent["id"])
            for agent_id, w in list(self.workers.items()):
                if not self.app.agent(agent_id):
                    w.cancel()
                    del self.workers[agent_id]
            await asyncio.sleep(2)

    async def _worker(self, agent_id: str) -> None:
        event = self.wake.setdefault(agent_id, asyncio.Event())
        while True:
            turn = self.db.one(
                "SELECT * FROM turns WHERE agent_id = ? AND status = 'queued' ORDER BY id LIMIT 1", agent_id
            )
            if not turn:
                waiting = self.db.one("SELECT id FROM turns WHERE agent_id = ? AND status = 'waiting'", agent_id)
                self.set_status(agent_id, "waiting" if waiting else "idle")
                event.clear()
                try:
                    await asyncio.wait_for(event.wait(), timeout=30)
                except TimeoutError:
                    pass
                continue
            task = asyncio.create_task(self.run_turn(turn))
            self.current[agent_id] = task
            try:
                await task
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise  # the worker itself is shutting down; a user "stop" only cancels the turn
            except Exception:
                log.exception("turn %s crashed", turn["id"])
                self.db.update("turns", turn["id"], status="failed", updated_at=now())
                self._after(turn, "failed", "crashed")
            finally:
                self.current.pop(agent_id, None)

    # ---------- the turn loop ----------
    async def run_turn(self, turn: dict) -> None:
        agent = self.app.agent(turn["agent_id"])
        if not agent:
            self.db.update("turns", turn["id"], status="cancelled")
            return
        st = self.app.settings
        self.db.update("turns", turn["id"], status="running", updated_at=now())
        self.set_status(agent["id"], "working")
        msgs = turn["llm_messages"] or context.build(self.app, agent, turn)
        tools = self.app.tools_for(agent)
        schemas = [t.schema() for t in tools.values()]
        steps = turn["steps"]
        try:
            if _unanswered(msgs):
                outcome = await self._execute(agent, turn, msgs, _unanswered(msgs), tools, resumed=True)
                if outcome != "ok":
                    return self._settle(agent, turn, msgs, steps, outcome)
            while steps < st.max_steps:
                self._absorb(turn, msgs)
                compressed = await context.compress(self.app, agent, turn, msgs)
                if compressed:
                    msgs[:] = compressed
                    self._save(turn, msgs, steps)
                    self.post(turn["conversation_id"], agent["id"], "Compressed earlier steps into a checkpoint to "
                              "keep working within my context window.", kind="status")
                reply, _ = await self.app.llm.chat(agent["model"], context.prepare(msgs, st.context_chars),
                                                   schemas or None, agent["fallbacks"], agent["id"],
                                                   reasoning=agent.get("reasoning", ""))
                msgs.append(reply)
                steps += 1
                self._save(turn, msgs, steps)
                if not reply.get("tool_calls"):
                    return self._finish(agent, turn, msgs, steps, reply["content"])
                if reply["content"].strip():
                    self.post(turn["conversation_id"], agent["id"], reply["content"], kind="progress")
                outcome = await self._execute(agent, turn, msgs, reply["tool_calls"], tools)
                if outcome != "ok":
                    return self._settle(agent, turn, msgs, steps, outcome)
            msgs.append({"role": "user", "content": "[harness] Step budget for this turn reached. Stop using tools. "
                         "Reply with what is done, what is left, and anything blocking."
                         + (" (Your goal continues in the next turn - call nothing now.)"
                            if turn["meta"].get("goal_id") else "")})
            reply, _ = await self.app.llm.chat(agent["model"], context.prepare(msgs, st.context_chars), None,
                                               agent["fallbacks"], agent["id"])
            msgs.append(reply)
            return self._finish(agent, turn, msgs, steps, reply["content"])
        except asyncio.CancelledError:
            self._save(turn, msgs, steps, status="cancelled")
            self.post(turn["conversation_id"], agent["id"], "Stopped.", kind="status")
            self._after(turn, "cancelled", "Stopped by user")
            raise
        except LLMError as e:
            self._save(turn, msgs, steps, status="failed")
            self.post(turn["conversation_id"], agent["id"], f"I couldn't reach my model: {e}", kind="status",
                      meta={"error": True})
            self.set_status(agent["id"], "error")
            self._after(turn, "failed", str(e))

    def _settle(self, agent, turn, msgs, steps, outcome: str):
        if outcome == "wait":
            return self._park(turn, msgs, steps)
        # A tool ended the turn on purpose (e.g. clarify posted a question); its message is already visible.
        return self._finish(agent, turn, msgs, steps, "", post=False)

    def _absorb(self, turn: dict, msgs: list[dict]) -> None:
        """A new owner message to the same conversation redirects the running turn instead of waiting behind it."""
        for t in self.db.all(
            "SELECT * FROM turns WHERE agent_id = ? AND conversation_id = ? AND status = 'queued' "
            "AND source IN ('user','web','telegram') AND id > ? ORDER BY id",
            turn["agent_id"], turn["conversation_id"], turn["id"],
        ):
            msgs.append({"role": "user", "content": f"[New message from the owner while you were working]: {t['trigger']}"})
            self.db.update("turns", t["id"], status="done", result=f"merged into turn {turn['id']}", updated_at=now())
            if t["meta"].get("voice_reply"):
                turn["meta"]["voice_reply"] = True

    async def _execute(self, agent: dict, turn: dict, msgs: list[dict], calls: list[dict],
                       tools: dict[str, Tool], resumed: bool = False) -> str:
        """Run tool calls. Returns 'ok', 'wait' (needs a human decision) or 'stop' (a tool ended the turn)."""
        waiting = stop = False
        images: list[str] = []
        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"]["arguments"] or "{}")
                if not isinstance(args, dict):
                    raise ValueError("arguments must be an object")
            except ValueError as e:
                msgs.append(self._tool_msg(call, f"Error: invalid JSON arguments ({e}). Retry with valid JSON."))
                continue
            tool = tools.get(name)
            if not tool:
                msgs.append(self._tool_msg(call, f"Error: unknown tool '{name}'."))
                continue
            if resumed:
                ap = self.db.one("SELECT * FROM approvals WHERE turn_id = ? AND tool_call_id = ? ORDER BY id DESC",
                                 turn["id"], call["id"])
                decision, reason = ("ask", "") if not ap else (
                    ("allow", "approved by owner") if ap["status"] == "approved" else
                    ("deny", "denied by owner") if ap["status"] == "denied" else ("pending", ""))
                if decision == "pending":
                    waiting = True
                    continue
                if ap and ap["status"] == "approved":
                    args = ap["args"]
                if decision == "ask":  # no approval row (crash between decide and insert): decide again
                    decision, reason = await self.app.reviewer.decide(tool, args, agent, turn["trigger"])
            else:
                decision, reason = await self.app.reviewer.decide(tool, args, agent, turn["trigger"])

            if decision == "deny":
                self._audit(agent, turn, name, args, "deny", reason)
                text = ("The owner denied this action. Do not retry it or work around it."
                        if reason == "denied by owner" else
                        f"Blocked by safety policy ({reason}). Do not work around this; tell the owner if it blocks you.")
                msgs.append(self._tool_msg(call, text))
                continue
            if decision == "ask":
                self._request_approval(agent, turn, call, tool, args, reason)
                waiting = True
                continue
            result = await self.run_tool(agent, turn, tool, args, reason)
            msgs.append(self._tool_msg(call, result.text))
            stop = stop or result.stop
            if result.image:
                images.append(result.image)
        if images and not waiting:
            msgs.append({"role": "user", "content": [
                {"type": "text", "text": "[screen image returned by the tool call above]"},
                *({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img}"}} for img in images[-1:]),
            ]})
        return "wait" if waiting else "stop" if stop else "ok"

    @staticmethod
    def _tool_msg(call: dict, text: str) -> dict:
        return {"role": "tool", "tool_call_id": call["id"], "content": text}

    async def run_tool(self, agent: dict, turn: dict, tool: Tool, args: dict, reason: str,
                       worker: int | None = None) -> ToolResult:
        ctx = ToolContext(self.app, agent, turn)
        started = time.monotonic()
        try:
            out = await asyncio.wait_for(tool.fn(ctx, **args), timeout=tool.timeout)
            result = out if isinstance(out, ToolResult) else ToolResult(str(out))
        except TimeoutError:
            result = ToolResult(f"Error: {tool.name} timed out after {tool.timeout:.0f}s.")
        except TypeError as e:
            result = ToolResult(f"Error: bad arguments for {tool.name}: {e}")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # tool failures are reported to the model, which decides what to do next
            log.warning("tool %s failed: %s", tool.name, e)
            result = ToolResult(f"Error: {e.__class__.__name__}: {e}")
        result.text = self.app.vault.redact(result.text)
        if not tool.extra.get("trusted"):
            result.text = f'<untrusted source="{tool.name}">\n{result.text}\n</untrusted>'
        ms = int((time.monotonic() - started) * 1000)
        self._audit(agent, turn, tool.name, args, "allow", reason, result.text[:500], ms)
        if not tool.extra.get("quiet"):
            meta = {"tool": tool.name, "reason": reason, "ms": ms, "preview": result.text[:600]}
            if worker:
                meta["worker"] = worker
            self.post(turn["conversation_id"], agent["id"], tool.subject_of(args)[:300], kind="tool", meta=meta)
        return result

    def _audit(self, agent, turn, tool, args, decision, reason, result="", ms=0):
        self.db.insert("audit", agent_id=agent["id"], turn_id=turn["id"], tool=tool, args=args, decision=decision,
                       reason=reason, result=result, duration_ms=ms, created_at=now())

    def _request_approval(self, agent, turn, call, tool: Tool, args: dict, reason: str) -> None:
        if self.db.one("SELECT id FROM approvals WHERE turn_id = ? AND tool_call_id = ? AND status = 'pending'",
                       turn["id"], call["id"]):
            return
        aid = self.db.insert("approvals", turn_id=turn["id"], agent_id=agent["id"], tool_call_id=call["id"],
                             tool=tool.name, args=args, reason=reason, created_at=now())
        self._audit(agent, turn, tool.name, args, "ask", reason)
        self.post(turn["conversation_id"], agent["id"], tool.subject_of(args)[:1000], kind="approval",
                  meta={"approval_id": aid, "tool": tool.name, "args": args, "reason": reason, "status": "pending"})
        self.app.bus.publish({"type": "approval", "approval_id": aid, "agent_id": agent["id"], "tool": tool.name,
                              "subject": tool.subject_of(args)[:500], "reason": reason})

    def _save(self, turn, msgs, steps, status=None):
        values = {"llm_messages": msgs, "steps": steps, "updated_at": now()}
        if status:
            values["status"] = status
        self.db.update("turns", turn["id"], **values)

    def _park(self, turn, msgs, steps):
        self._save(turn, msgs, steps, status="waiting")
        self.set_status(turn["agent_id"], "waiting")

    def _finish(self, agent, turn, msgs, steps, text: str, post: bool = True) -> None:
        text = text.strip() or ("(done)" if post else "(waiting for the owner's answer)")
        self._save(turn, msgs, steps, status="done")
        self.db.update("turns", turn["id"], result=text)
        meta = turn["meta"]
        if post:
            self.post(turn["conversation_id"], agent["id"], text,
                      meta={"turn_id": turn["id"], "source": turn["source"], **{
                          k: v for k, v in meta.items() if k in ("voice_reply", "routine_id", "goal_id")}})
        if meta.get("from_agent"):  # reply flows back to the agent that delegated
            sender = self.app.agent(meta["from_agent"])
            if sender:
                self.enqueue(sender["id"], meta["reply_conversation"], f"[Reply from {agent['name']}]: {text}",
                             "handoff", {"hops": meta.get("hops", 0) + 1})
        conv = self.app.conversation(turn["conversation_id"])
        if conv and conv["kind"] == "group" and meta.get("hops", 0) < MAX_HOPS and post:
            for other in self._mentions(conv, text):
                if other != agent["id"]:
                    self.enqueue(other, conv["id"], f"[{agent['name']} in the group]: {text}", "group",
                                 {"hops": meta.get("hops", 0) + 1})
        self._after(turn, "done", text)
        self.spawn(self.summarize(turn["conversation_id"], agent))
        calls = _count_calls(msgs)
        # Goal turns are long by nature; reflecting after each one would mostly re-learn the same thing.
        threshold = REFLECT_AFTER_CALLS * (3 if turn["source"] == "goal" else 1)
        if self.app.settings.learning and (calls >= threshold or (
                turn["source"] in ("web", "telegram", "user") and CORRECTION.search(turn["trigger"]))):
            self.spawn(self.app.memory.reflect(agent, turn, msgs, calls))

    def _after(self, turn: dict, status: str, result: str) -> None:
        self.app.scheduler.turn_finished(turn, status, result)
        fresh = self.db.one("SELECT * FROM turns WHERE id = ?", turn["id"]) or turn
        self.spawn(self.app.goals.turn_finished(fresh, status))
        self.app.bus.publish({"type": "turn_done", "turn_id": turn["id"], "agent_id": turn["agent_id"],
                              "status": status})

    async def summarize(self, conversation_id: int, agent: dict, keep: int = context.HISTORY,
                        minimum: int = context.HISTORY) -> int:
        """Fold old messages into a running summary so long-lived conversations keep fitting in context. Keeps the
        last `keep` messages verbatim; does nothing unless at least `minimum` would be folded. Returns how many were."""
        conv = self.app.conversation(conversation_id)
        if not conv:
            return 0
        rows = self.db.all("SELECT id, author, content FROM messages WHERE conversation_id = ? AND id > ? "
                           "AND kind IN ('text','handoff') ORDER BY id", conversation_id, conv["summarized_upto"])
        old = rows[:-keep] if keep else rows
        if len(old) < max(minimum, 1):
            return 0
        transcript = "\n".join(f"{r['author']}: {r['content'][:2000]}" for r in old)
        try:
            summary = await self.app.llm.text(
                self.app.aux_model(agent),
                "Update the running summary of a conversation between an owner and their AI agents. Keep decisions, "
                "facts, open tasks, preferences and file paths. Drop chit-chat. Max 400 words.",
                f"Current summary:\n{conv['summary'] or '(none)'}\n\nNew messages:\n{transcript}",
                agent_id=agent["id"],
            )
        except LLMError as e:
            log.warning("summary failed: %s", e)
            return 0
        self.db.update("conversations", conversation_id, summary=summary, summarized_upto=old[-1]["id"])
        return len(old)
