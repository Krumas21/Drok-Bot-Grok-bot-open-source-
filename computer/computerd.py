"""computerd - the agents' shared computer.

Runs N virtual screens (Xvfb + fluxbox + x11vnc + Chromium each), restarts anything that dies, and exposes a tiny
token-protected HTTP API for the Drok core: exec, screenshot, input, recover. Standard library only.
"""

import base64
import glob
import hmac
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ.get("COMPUTER_TOKEN", "")
SCREENS = int(os.environ.get("DROK_SCREENS", "4"))
WIDTH, HEIGHT = 1280, 800
HOME = os.path.expanduser("~")
PROFILES = os.path.join(HOME, ".browser")

procs: dict[tuple[int, str], subprocess.Popen] = {}
lock = threading.Lock()


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def screen_env(n: int) -> dict:
    return {**os.environ, "DISPLAY": f":{n}", "HOME": HOME}


def commands(n: int) -> list[tuple[str, list[str]]]:
    profile = os.path.join(PROFILES, f"screen-{n}")
    return [
        ("xvfb", ["Xvfb", f":{n}", "-screen", "0", f"{WIDTH}x{HEIGHT}x24", "-nolisten", "tcp"]),
        ("wm", ["fluxbox"]),
        ("vnc", ["x11vnc", "-display", f":{n}", "-rfbport", str(5900 + n), "-forever", "-shared", "-nopw",
                 "-quiet", "-noxdamage"]),
        ("browser", ["chromium", "--no-sandbox", "--disable-dev-shm-usage", "--no-first-run",
                     "--no-default-browser-check", "--test-type", "--password-store=basic", "--disable-features=Translate",
                     f"--user-data-dir={profile}", f"--remote-debugging-port={9222 + n}",
                     "--window-position=0,0", f"--window-size={WIDTH},{HEIGHT}", "--start-maximized",
                     "about:blank"]),
        # Chromium only binds DevTools to localhost; expose it to the core container on 9300+N.
        ("cdp", ["socat", f"TCP-LISTEN:{9300 + n},fork,reuseaddr,bind=0.0.0.0", f"TCP:127.0.0.1:{9222 + n}"]),
    ]


def start(n: int, name: str, argv: list[str]) -> None:
    if name == "xvfb":
        for f in (f"/tmp/.X{n}-lock", f"/tmp/.X11-unix/X{n}"):
            if os.path.exists(f):
                os.remove(f)
    if name == "browser":
        for f in glob.glob(os.path.join(PROFILES, f"screen-{n}", "Singleton*")):
            os.remove(f)  # stale lock after a crash would stop Chromium from starting
    procs[(n, name)] = subprocess.Popen(argv, env=screen_env(n), stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, start_new_session=True)
    log(f"screen {n}: started {name}")
    if name == "xvfb":
        time.sleep(1)


def stop_all() -> None:
    for p in procs.values():
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    procs.clear()


def supervise() -> None:
    """Restart anything that died. If a screen's X server died, everything on that screen restarts."""
    while True:
        with lock:
            for n in range(1, SCREENS + 1):
                x = procs.get((n, "xvfb"))
                x_dead = x is None or x.poll() is not None
                for name, argv in commands(n):
                    p = procs.get((n, name))
                    if x_dead or p is None or p.poll() is not None:
                        if p and p.poll() is None:
                            os.killpg(p.pid, signal.SIGKILL)
                        start(n, name, argv)
        time.sleep(5)


def run_exec(body: dict) -> dict:
    timeout = min(float(body.get("timeout_s", 120)), 1800)
    env = screen_env(int(body.get("screen", 1)))
    p = subprocess.Popen(["bash", "-lc", body["command"]], cwd=body.get("cwd") or "/workspace", env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        out, _ = p.communicate(timeout=timeout)
        code = p.returncode
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        out, _ = p.communicate()
        out += f"\n[killed after {timeout:.0f}s timeout]".encode()
        code = -9
    return {"exit_code": code, "output": out.decode(errors="replace")}


def screenshot(body: dict) -> dict:
    n = int(body["screen"])
    path = f"/tmp/shot-{n}.png"
    subprocess.run(["scrot", "-o", "-p", path], env=screen_env(n), check=True, timeout=15)
    with open(path, "rb") as f:
        return {"png": base64.b64encode(f.read()).decode()}


def xdo(n: int, *args: str) -> None:
    subprocess.run(["xdotool", *args], env=screen_env(n), check=True, timeout=30)


def do_input(body: dict) -> dict:
    n, action = int(body["screen"]), body["action"]
    x, y = str(int(body.get("x", 0))), str(int(body.get("y", 0)))
    if action in ("click", "double_click", "right_click", "move", "scroll"):
        xdo(n, "mousemove", x, y)
    if action == "click":
        xdo(n, "click", "1")
    elif action == "double_click":
        xdo(n, "click", "--repeat", "2", "1")
    elif action == "right_click":
        xdo(n, "click", "3")
    elif action == "type":
        xdo(n, "type", "--delay", "15", "--", body.get("text", ""))
    elif action == "key":
        xdo(n, "key", "--", body.get("key", "Return"))
    elif action == "scroll":
        button = {"up": "4", "down": "5", "left": "6", "right": "7"}.get(body.get("direction", "down"), "5")
        xdo(n, "click", "--repeat", str(max(1, min(int(body.get("amount", 3)), 20))), button)
    elif action != "move":
        raise ValueError(f"unknown action {action}")
    return {"ok": True}


def recover(body: dict) -> dict:
    with lock:
        stop_all()
        if body.get("reset"):
            shutil.rmtree(PROFILES, ignore_errors=True)  # browser state only; /workspace is never touched
    return {"ok": True, "reset": bool(body.get("reset"))}


def health() -> dict:
    alive = {f"{n}:{name}": p.poll() is None for (n, name), p in procs.items()}
    return {"ok": bool(alive) and all(alive.values()), "screens": SCREENS, "processes": alive}


ROUTES = {"/exec": run_exec, "/screenshot": screenshot, "/input": do_input, "/recover": recover}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, health())
        self._send(404, {"error": "not found"})

    def do_POST(self):
        auth = self.headers.get("Authorization", "")
        if not TOKEN or not hmac.compare_digest(auth, f"Bearer {TOKEN}"):
            return self._send(401, {"error": "bad token (is COMPUTER_TOKEN set on both containers?)"})
        fn = ROUTES.get(self.path)
        if not fn:
            return self._send(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            self._send(200, fn(body))
        except Exception as e:
            self._send(500, {"error": f"{e.__class__.__name__}: {e}"})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    os.makedirs(PROFILES, exist_ok=True)
    threading.Thread(target=supervise, daemon=True).start()
    log(f"computerd: {SCREENS} screens, API on :7000")
    ThreadingHTTPServer(("0.0.0.0", 7000), Handler).serve_forever()
