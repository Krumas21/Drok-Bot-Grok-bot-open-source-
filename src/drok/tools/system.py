"""Shell, workspace files and the web."""

import base64
import html
import re
from pathlib import Path

import httpx

from . import Tool, ToolContext, ToolResult, i, params, s

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def _clip(text: str, head: int = 4000, tail: int = 14000) -> str:
    if len(text) <= head + tail:
        return text
    return f"{text[:head]}\n[... {len(text) - head - tail} chars cut ...]\n{text[-tail:]}"


def _ws_path(ctx: ToolContext, path: str) -> Path:
    """Map an agent path (/workspace/x or x) to the real workspace dir, refusing escapes."""
    root = ctx.app.settings.workspace
    rel = path.removeprefix("/workspace").lstrip("/") if path.startswith("/workspace") else path.lstrip("/")
    full = (root / rel).resolve()
    if full != root and root not in full.parents:
        raise ValueError("Path is outside /workspace")
    return full


async def shell(ctx: ToolContext, command: str, timeout: int = 120) -> str:
    r = await ctx.app.computer.exec(ctx.app.vault.fill(command), timeout=min(max(timeout, 1), 1800))
    return f"exit code: {r['exit_code']}\n{_clip(r['output'])}"


async def read_file(ctx: ToolContext, path: str, offset: int = 0) -> str | ToolResult:
    p = _ws_path(ctx, path)
    if p.is_dir():
        return "That is a directory; use list_files."
    if p.suffix.lower() in IMAGE_EXT:
        return ToolResult(f"Image {path} ({p.stat().st_size} bytes)", image=base64.b64encode(p.read_bytes()).decode())
    text = p.read_text(errors="replace")
    chunk = text[offset:offset + 30000]
    more = f"\n[... {len(text) - offset - len(chunk)} more chars; read again with offset={offset + len(chunk)}]" \
        if offset + len(chunk) < len(text) else ""
    return chunk + more


async def write_file(ctx: ToolContext, path: str, content: str, append: bool = False) -> str:
    p = _ws_path(ctx, path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a" if append else "w") as f:
        f.write(content)
    return f"{'Appended' if append else 'Wrote'} {len(content)} chars to /workspace/{p.relative_to(ctx.app.settings.workspace)}"


async def list_files(ctx: ToolContext, path: str = "/workspace") -> str:
    p = _ws_path(ctx, path)
    if not p.exists():
        return "Not found."
    rows = []
    for child in sorted(p.iterdir())[:500]:
        if child.name.startswith(".") and child.name not in (".env.example",):
            continue
        rows.append(f"{child.name}/" if child.is_dir() else f"{child.name}  ({child.stat().st_size} B)")
    return "\n".join(rows) or "(empty)"


def html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr)>", "\n", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = html.unescape(raw)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", raw)).strip()


async def web_fetch(ctx: ToolContext, url: str) -> str:
    async with httpx.AsyncClient(follow_redirects=True, timeout=30,
                                 headers={"User-Agent": "Mozilla/5.0 (Drok agent)"}) as c:
        r = await c.get(url)
    body = html_to_text(r.text) if "html" in r.headers.get("content-type", "") else r.text
    return f"HTTP {r.status_code} {r.url}\n\n{_clip(body, 20000, 0)}"


async def web_search(ctx: ToolContext, query: str, count: int = 8) -> str:
    st = ctx.app.settings
    async with httpx.AsyncClient(timeout=30) as c:
        if st.brave_api_key:
            r = await c.get("https://api.search.brave.com/res/v1/web/search", params={"q": query, "count": count},
                            headers={"X-Subscription-Token": st.brave_api_key, "Accept": "application/json"})
            items = [(x["title"], x["url"], x.get("description", "")) for x in r.json().get("web", {}).get("results", [])]
        else:
            r = await c.get(f"{st.searxng_url}/search", params={"q": query, "format": "json"})
            items = [(x["title"], x["url"], x.get("content", "")) for x in r.json().get("results", [])[:count]]
    return "\n\n".join(f"{t}\n{u}\n{html_to_text(d)}" for t, u, d in items) or "No results."


def tools(app) -> list[Tool]:
    out = [
        Tool("shell", "Run a bash command on the shared Linux computer (cwd /workspace). Use for git, builds, "
             "scripts, package installs (sudo works). Reference stored secrets as {{secret:NAME}}.",
             params(["command"], command=s("Bash command"), timeout=i("Seconds before kill, default 120, max 1800")),
             shell, risk="risky", subject=lambda a: a.get("command", ""), timeout=1830),
        Tool("read_file", "Read a text file (or view an image) from /workspace.",
             params(["path"], path=s("Path, e.g. /workspace/notes.md"), offset=i("Char offset for long files")),
             read_file, risk="safe"),
        Tool("write_file", "Create or overwrite a file in /workspace (set append=true to append).",
             params(["path", "content"], path=s("Path inside /workspace"), content=s("Full file content"),
                    append={"type": "boolean"}),
             write_file, risk="safe", subject=lambda a: a.get("path", "")),
        Tool("list_files", "List a directory in /workspace.", params([], path=s("Directory, default /workspace")),
             list_files, risk="safe"),
        Tool("web_fetch", "Fetch a URL and return readable text (no JavaScript). For interactive sites use browser.",
             params(["url"], url=s("http(s) URL")), web_fetch, risk="safe", subject=lambda a: a.get("url", "")),
    ]
    if app.settings.brave_api_key or app.settings.searxng_url:
        out.append(Tool("web_search", "Search the web. Returns titles, URLs and snippets.",
                        params(["query"], query=s("Search query"), count=i("Results, default 8")),
                        web_search, risk="safe", subject=lambda a: a.get("query", "")))
    return out
