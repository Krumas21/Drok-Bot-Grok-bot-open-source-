"""Telegram: the owner's pocket line to the orchestrator, plus approvals and alerts from every agent.

Long polling, so it works behind home NAT with no public URL. Only TELEGRAM_ALLOWED_USER_IDS are served.
"""

import logging
import time
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

log = logging.getLogger("drok.telegram")

HELP = """Talk to me normally - I'm your orchestrator and I'll hand work to the team.
/agents - who's on the team and what they're doing
/ask <agent> <message> - talk to one agent directly
/approvals - pending approvals
/routines - scheduled work
/stop [agent] - stop current work"""


def chunks(text: str, size: int = 4000):
    for i in range(0, max(len(text), 1), size):
        yield text[i:i + size]


class Telegram:
    def __init__(self, app):
        self.app = app
        self.allowed = app.settings.telegram_allowed
        self.tg = Application.builder().token(app.settings.telegram_token).build()
        self.sent_approvals: dict[int, list[tuple[int, int]]] = {}  # approval id -> [(chat, message id)]

    def is_allowed(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.allowed

    async def start(self) -> None:
        h = self.tg.add_handler
        h(CommandHandler(["start", "help"], self.cmd_help))
        h(CommandHandler("agents", self.cmd_agents))
        h(CommandHandler("ask", self.cmd_ask))
        h(CommandHandler("approvals", self.cmd_approvals))
        h(CommandHandler("routines", self.cmd_routines))
        h(CommandHandler("stop", self.cmd_stop))
        h(CallbackQueryHandler(self.on_callback))
        h(MessageHandler(filters.VOICE | filters.AUDIO, self.on_voice))
        h(MessageHandler(filters.PHOTO | filters.Document.ALL, self.on_file))
        h(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        await self.tg.initialize()
        await self.tg.start()
        await self.tg.updater.start_polling()
        self.app.bus.on(self.on_event)
        if not self.allowed:
            log.warning("TELEGRAM_ALLOWED_USER_IDS is empty: the bot will only tell people their user id.")
        log.info("Telegram polling as @%s", self.tg.bot.username)

    async def stop(self) -> None:
        await self.tg.updater.stop()
        await self.tg.stop()
        await self.tg.shutdown()

    # ---------- outbound ----------
    async def send(self, text: str, file: Path | None = None, buttons=None, voice: bytes | None = None) -> list:
        sent = []
        for chat in self.allowed:
            try:
                if voice:
                    await self.tg.bot.send_voice(chat, voice)
                parts = list(chunks(text))
                for n, part in enumerate(parts):
                    markup = buttons if n == len(parts) - 1 else None
                    m = await self.tg.bot.send_message(chat, part, reply_markup=markup)
                    sent.append((chat, m.message_id))
                if file:
                    with file.open("rb") as f:
                        await self.tg.bot.send_document(chat, f, filename=file.name)
            except Exception as e:
                log.warning("telegram send to %s failed: %s", chat, e)
        return sent

    def _link(self, path: str) -> str:
        return f"{self.app.settings.public_url}{path}"

    async def on_event(self, ev: dict) -> None:
        kind = ev["type"]
        if kind == "approval":
            agent = self.app.agent(ev["agent_id"]) or {"name": ev["agent_id"]}
            aid = ev["approval_id"]
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("Allow once", callback_data=f"ap:{aid}:once"),
                InlineKeyboardButton("Always", callback_data=f"ap:{aid}:always"),
                InlineKeyboardButton("Deny", callback_data=f"ap:{aid}:deny"),
            ]])
            text = f"{agent['name']} wants approval for {ev['tool']}:\n\n{ev['subject']}\n\n({ev['reason']})"
            self.sent_approvals[aid] = await self.send(text, buttons=markup)
        elif kind == "approval_resolved":
            for chat, mid in self.sent_approvals.pop(ev["approval_id"], []):
                try:
                    await self.tg.bot.edit_message_reply_markup(chat, mid, reply_markup=None)
                    await self.tg.bot.send_message(chat, f"Approval #{ev['approval_id']}: {ev['status']}",
                                                   reply_to_message_id=mid)
                except Exception as e:
                    log.debug("approval edit failed: %s", e)
        elif kind == "notice":
            await self.send(ev["text"])
        elif kind == "message":
            await self._forward(ev["message"])

    async def _forward(self, msg: dict) -> None:
        author = self.app.agent(msg["author"])
        if not author:
            return
        conv = self.app.conversation(msg["conversation_id"])
        orch = self.app.orchestrator()
        in_orch_dm = conv and conv["kind"] == "dm" and orch and conv["members"] == [orch["id"]]
        source = msg["meta"].get("source")
        if msg["kind"] == "request":
            what = "store a secret" if msg["meta"].get("request") == "secret" else "take over its screen"
            path = "/settings#secrets" if msg["meta"].get("request") == "secret" else "/computer"
            await self.send(f"{author['name']} needs you to {what}:\n{msg['content']}\n\n{self._link(path)}")
            return
        if msg["kind"] not in ("text", "status"):
            return
        if not ((in_orch_dm and source != "web") or source == "telegram"):
            return
        prefix = "" if in_orch_dm else f"{author['name']}: "
        voice = None
        st = self.app.settings
        if msg["meta"].get("voice_reply") and st.tts_model:
            try:
                voice = await self.app.llm.speak(st.tts_model, msg["content"], st.tts_voice)
            except Exception as e:
                log.warning("tts failed: %s", e)
        await self.send(prefix + msg["content"], voice=voice)

    # ---------- inbound ----------
    async def _guard(self, update: Update) -> bool:
        user = update.effective_user
        if self.is_allowed(user and user.id):
            return True
        if not self.allowed and update.effective_message:
            await update.effective_message.reply_text(
                f"Your Telegram user id is {user.id}. Add it to TELEGRAM_ALLOWED_USER_IDS in .env and restart Drok."
            )
        log.info("ignored telegram update from user %s", user and user.id)
        return False

    def _orch_conv(self) -> int | None:
        orch = self.app.orchestrator()
        return self.app.dm(orch["id"])["id"] if orch else None

    async def on_text(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        conv = self._orch_conv()
        if conv:
            self.app.runtime.user_message(conv, update.message.text, source="telegram")

    async def on_voice(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        st = self.app.settings
        if not st.stt_model:
            await update.message.reply_text("Voice notes need DROK_STT_MODEL set (e.g. openai/whisper-1).")
            return
        media = update.message.voice or update.message.audio
        data = bytes(await (await media.get_file()).download_as_bytearray())
        try:
            text = await self.app.llm.transcribe(st.stt_model, "voice.ogg", data)
        except Exception as e:
            await update.message.reply_text(f"Couldn't transcribe: {e}")
            return
        await update.message.reply_text(f"Heard: {text}")
        conv = self._orch_conv()
        if conv:
            self.app.runtime.user_message(conv, text, source="telegram", meta={"voice_reply": True})

    async def on_file(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        m = update.message
        if m.photo:
            tg_file, name = await m.photo[-1].get_file(), "photo.jpg"
        else:
            tg_file, name = await m.document.get_file(), Path(m.document.file_name or "file").name
        inbox = self.app.settings.workspace / "inbox"
        inbox.mkdir(exist_ok=True)
        dest = inbox / f"{int(time.time())}_{name}"
        await tg_file.download_to_drive(dest)
        conv = self._orch_conv()
        if conv:
            note = f"[Owner sent a file via Telegram: /workspace/inbox/{dest.name}]"
            self.app.runtime.user_message(conv, f"{m.caption or ''}\n{note}".strip(), source="telegram")

    async def on_callback(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if not self.is_allowed(q.from_user.id):
            await q.answer("Not allowed")
            return
        _, aid, choice = q.data.split(":")
        ap = self.app.runtime.resolve_approval(int(aid), choice != "deny", always=choice == "always")
        await q.answer(f"{ap['status'] if ap else 'gone'}")

    # ---------- commands ----------
    async def cmd_help(self, update: Update, _) -> None:
        if await self._guard(update):
            await update.message.reply_text(HELP)

    async def cmd_agents(self, update: Update, _) -> None:
        if not await self._guard(update):
            return
        lines = [f"{'*' if a['is_orchestrator'] else '-'} {a['name']} ({a['id']}) - {a['title']} [{a['status']}]"
                 for a in self.app.agents()]
        await update.message.reply_text("\n".join(lines) or "No agents.")

    async def cmd_ask(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        if len(ctx.args) < 2:
            await update.message.reply_text("Usage: /ask <agent> <message>")
            return
        agent = self.app.find_agent(ctx.args[0])
        if not agent:
            await update.message.reply_text(f"No agent '{ctx.args[0]}'. Try /agents.")
            return
        self.app.runtime.user_message(self.app.dm(agent["id"])["id"], " ".join(ctx.args[1:]), source="telegram")

    async def cmd_approvals(self, update: Update, _) -> None:
        if not await self._guard(update):
            return
        pending = self.app.db.all("SELECT * FROM approvals WHERE status = 'pending' ORDER BY id")
        if not pending:
            await update.message.reply_text("Nothing waiting for you.")
        tools = self.app.all_tools()
        for ap in pending:
            tool = tools.get(ap["tool"])
            await self.on_event({"type": "approval", "approval_id": ap["id"], "agent_id": ap["agent_id"],
                                 "tool": ap["tool"], "reason": ap["reason"],
                                 "subject": tool.subject_of(ap["args"]) if tool else str(ap["args"])})

    async def cmd_routines(self, update: Update, _) -> None:
        if not await self._guard(update):
            return
        sch = self.app.scheduler
        rows = self.app.db.all("SELECT * FROM routines ORDER BY agent_id")
        await update.message.reply_text("\n".join(
            f"#{r['id']} {r['name']} ({r['agent_id']}) {r['cron'] or 'webhook'} - "
            f"{'next ' + sch.fmt(r['next_run_at'], r['tz']) if r['enabled'] else 'paused'}" for r in rows
        ) or "No routines.")

    async def cmd_stop(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        agent = self.app.find_agent(ctx.args[0]) if ctx.args else self.app.orchestrator()
        if not agent:
            await update.message.reply_text("No such agent.")
            return
        stopped = self.app.runtime.stop(agent["id"])
        await update.message.reply_text(f"Stopping {agent['name']}." if stopped else f"{agent['name']} is idle.")
