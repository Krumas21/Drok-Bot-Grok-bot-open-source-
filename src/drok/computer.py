"""Client for computerd, the small daemon inside the shared `computer` container."""

import asyncio
import socket
from urllib.parse import urlparse

import httpx

CDP_PORT_BASE = 9300  # computerd forwards screen N's Chromium DevTools to 9300+N
VNC_PORT_BASE = 5900  # x11vnc for screen N listens on 5900+N


class ComputerError(Exception):
    pass


class Computer:
    def __init__(self, url: str, token: str, screens: int):
        self.url = url
        self.token = token
        self.screens = screens
        self.host = urlparse(url).hostname or "localhost"
        self.locks = {n: asyncio.Lock() for n in range(1, screens + 1)}
        self.http = httpx.AsyncClient(base_url=url, headers={"Authorization": f"Bearer {token}"}, timeout=60)

    async def _post(self, path: str, timeout: float = 60, **body) -> dict:
        try:
            r = await self.http.post(path, json=body, timeout=timeout)
        except httpx.HTTPError as e:
            raise ComputerError(f"computer unreachable ({e.__class__.__name__}). Is the computer container running?") from e
        if r.status_code != 200:
            raise ComputerError(f"computer error {r.status_code}: {r.text[:300]}")
        return r.json()

    async def exec(self, command: str, timeout: float = 120, cwd: str = "/workspace", screen: int = 1) -> dict:
        return await self._post("/exec", timeout=timeout + 15, command=command, timeout_s=timeout, cwd=cwd,
                                screen=screen)

    async def bg_start(self, command: str, screen: int = 1, cwd: str = "/workspace") -> dict:
        return await self._post("/bg/start", command=command, screen=screen, cwd=cwd)

    async def bg(self, action: str, **body) -> dict:
        return await self._post(f"/bg/{action}", **body)

    async def screenshot(self, screen: int) -> str:
        return (await self._post("/screenshot", screen=screen))["png"]

    async def input(self, screen: int, **action) -> dict:
        return await self._post("/input", screen=screen, **action)

    async def health(self) -> dict:
        try:
            r = await self.http.get("/health", timeout=5)
            return r.json()
        except (httpx.HTTPError, ValueError) as e:
            return {"ok": False, "error": str(e)}

    async def recover(self, reset: bool = False) -> dict:
        return await self._post("/recover", timeout=90, reset=reset)

    def cdp_url(self, screen: int) -> str:
        # Chromium's DevTools rejects Host headers that are not an IP or "localhost", so resolve the name first.
        return f"http://{socket.gethostbyname(self.host)}:{CDP_PORT_BASE + screen}"

    def vnc_addr(self, screen: int) -> tuple[str, int]:
        return self.host, VNC_PORT_BASE + screen
