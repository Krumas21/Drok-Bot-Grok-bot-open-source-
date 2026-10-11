"""In-process pub/sub so the web UI (SSE) and Telegram see agent activity as it happens."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger("drok.bus")


class Bus:
    def __init__(self):
        self._queues: set[asyncio.Queue] = set()
        self._handlers: list[Callable[[dict], Awaitable[None]]] = []

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._queues.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._queues.discard(q)

    def on(self, handler: Callable[[dict], Awaitable[None]]) -> None:
        self._handlers.append(handler)

    def publish(self, event: dict) -> None:
        for q in list(self._queues):
            if q.full():  # slow browser tab: drop it rather than grow memory forever
                self._queues.discard(q)
                continue
            q.put_nowait(event)
        for h in self._handlers:
            asyncio.get_running_loop().create_task(self._safe(h, event))

    @staticmethod
    async def _safe(handler, event):
        try:
            await handler(event)
        except Exception:
            log.exception("bus handler failed for %s", event.get("type"))
