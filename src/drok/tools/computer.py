"""Each agent's screen: pixel-level `computer` control and DOM-level `browser` control of the same Chromium."""

import asyncio
import base64
import logging

from . import Tool, ToolContext, ToolResult, i, params, s

log = logging.getLogger("drok.browser")

SNAPSHOT_JS = r"""() => {
  document.querySelectorAll('[data-drok-ref]').forEach(e => e.removeAttribute('data-drok-ref'));
  const sel = 'a[href],button,input,select,textarea,summary,[role=button],[role=link],[role=checkbox],[role=tab],' +
              '[role=menuitem],[role=option],[role=switch],[contenteditable=true]';
  const out = []; let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none') continue;
    n++; el.setAttribute('data-drok-ref', n);
    const tag = el.tagName.toLowerCase();
    const label = (el.getAttribute('aria-label') || el.innerText || el.placeholder || el.title || el.name || el.id || '')
      .trim().replace(/\s+/g, ' ').slice(0, 80);
    const type = el.type && tag === 'input' ? `[${el.type}]` : '';
    const val = (tag === 'input' || tag === 'textarea') && el.type !== 'password' && el.value
      ? ` = "${String(el.value).slice(0, 40)}"` : '';
    const href = tag === 'a' ? ` -> ${(el.getAttribute('href') || '').slice(0, 80)}` : '';
    out.push(`[${n}] ${tag}${type} "${label}"${val}${href}`);
    if (n >= 250) break;
  }
  return out.join('\n');
}"""


class Browsers:
    """Lazily connects Playwright to each screen's already-running Chromium over CDP."""

    def __init__(self, computer):
        self.computer = computer
        self._pw = None
        self._browsers: dict[int, object] = {}
        self._pages: dict[int, object] = {}

    async def context(self, screen: int):
        b = self._browsers.get(screen)
        if b is None or not b.is_connected():
            if self._pw is None:
                from playwright.async_api import async_playwright
                self._pw = await async_playwright().start()
            b = await self._pw.chromium.connect_over_cdp(self.computer.cdp_url(screen), timeout=15000)
            self._browsers[screen] = b
            self._pages.pop(screen, None)
        return b.contexts[0] if b.contexts else await b.new_context()

    async def page(self, screen: int):
        ctx = await self.context(screen)
        p = self._pages.get(screen)
        if p is None or p.is_closed():
            p = ctx.pages[-1] if ctx.pages else await ctx.new_page()
            self._pages[screen] = p
        await p.bring_to_front()
        return p

    def use(self, screen: int, page) -> None:
        self._pages[screen] = page

    async def close(self) -> None:
        for b in self._browsers.values():
            try:
                await b.close()  # for CDP connections this only disconnects; Chromium keeps running
            except Exception as e:
                log.debug("browser close: %s", e)
        if self._pw:
            await self._pw.stop()


async def computer(ctx: ToolContext, action: str, x: int = 0, y: int = 0, text: str = "", key: str = "",
                   direction: str = "down", amount: int = 3, seconds: float = 1) -> ToolResult:
    app, screen = ctx.app, ctx.agent["screen"]
    async with app.computer.locks.setdefault(screen, asyncio.Lock()):
        if action == "wait":
            await asyncio.sleep(min(seconds, 30))
        elif action != "screenshot":
            payload = {"action": action, "x": x, "y": y, "key": key, "direction": direction, "amount": amount}
            if action == "type":
                payload["text"] = app.vault.fill(text)
            await app.computer.input(screen, **payload)
            await asyncio.sleep(0.6)  # let the UI settle before looking
        png = await app.computer.screenshot(screen)
    return ToolResult(f"Screen {screen} after {action}. Coordinates are pixels on a 1280x800 screen.", image=png)


async def browser(ctx: ToolContext, action: str, url: str = "", ref: int = 0, element: str = "", text: str = "",
                  key: str = "", index: int = 0) -> str | ToolResult:
    app, screen = ctx.app, ctx.agent["screen"]
    async with app.computer.locks.setdefault(screen, asyncio.Lock()):
        page = await app.browsers.page(screen)
        target = f'[data-drok-ref="{ref}"]'
        if action == "goto":
            await page.goto(url if "://" in url else f"https://{url}", wait_until="domcontentloaded", timeout=45000)
        elif action == "click":
            await page.click(target, timeout=10000)
        elif action == "fill":
            await page.fill(target, app.vault.fill(text), timeout=10000)
        elif action == "select":
            await page.select_option(target, label=text, timeout=10000)
        elif action == "press":
            await page.keyboard.press(key or "Enter")
        elif action == "back":
            await page.go_back(timeout=20000)
        elif action == "scroll":
            await page.mouse.wheel(0, 700 if (text or "down") != "up" else -700)
        elif action == "new_tab":
            page = await page.context.new_page()
            app.browsers.use(screen, page)
            if url:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        elif action in ("tabs", "switch_tab"):
            pages = page.context.pages
            if action == "switch_tab":
                page = pages[index]
                app.browsers.use(screen, page)
                await page.bring_to_front()
            else:
                return "\n".join(f"[{n}] {p.url}" for n, p in enumerate(pages))
        elif action == "screenshot":
            png = await page.screenshot()
            return ToolResult(f"{page.url}", image=base64.b64encode(png).decode())
        elif action == "read":
            body = await page.inner_text("body", timeout=10000)
            return f"URL: {page.url}\nTitle: {await page.title()}\n\n{body[:30000]}"
        elif action != "snapshot":
            return f"Unknown action '{action}'."
        if action in ("click", "press", "fill", "goto", "back"):
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass  # many SPAs never fire it after a click; the snapshot below shows the real state
        refs = await page.evaluate(SNAPSHOT_JS)
        body = (await page.inner_text("body", timeout=10000))[:5000]
        return f"URL: {page.url}\nTitle: {await page.title()}\n\nInteractive elements:\n{refs}\n\nPage text:\n{body}"


def tools(app) -> list[Tool]:
    return [
        Tool("browser",
             "Control the Chromium browser on your screen through the DOM. 'snapshot' lists interactive elements as "
             "[ref] - use those refs for click/fill/select. Actions: goto(url), snapshot, read (full text), "
             "click(ref, element), fill(ref, element, text), select(ref, element, text=option label), press(key), "
             "back, scroll(text=up|down), tabs, switch_tab(index), new_tab(url), screenshot. Every click/fill/select "
             "must include `element`: a plain description of what it is (shown to the safety reviewer). Fill "
             "credentials with {{secret:NAME}}.",
             params(["action"], action=s("Action name", enum=["goto", "snapshot", "read", "click", "fill", "select",
                                                              "press", "back", "scroll", "tabs", "switch_tab",
                                                              "new_tab", "screenshot"]),
                    url=s("URL for goto/new_tab"), ref=i("Element ref from the last snapshot"),
                    element=s("What the element is, e.g. 'Submit order button'"), text=s("Text to fill / option"),
                    key=s("Key for press, e.g. Enter, Tab, Control+A"), index=i("Tab index for switch_tab")),
             browser, risk="risky", timeout=90,
             subject=lambda a: f"{a.get('action', '')} {a.get('url') or a.get('element') or a.get('key') or ''}".strip()),
        Tool("computer",
             "Pixel-level control of your 1280x800 screen (any app, not just the browser). Actions: screenshot, "
             "click(x,y), double_click(x,y), right_click(x,y), move(x,y), type(text), key(key, e.g. ctrl+l, Return), "
             "scroll(x,y,direction,amount), wait(seconds). Returns a screenshot after each action.",
             params(["action"], action=s("Action", enum=["screenshot", "click", "double_click", "right_click", "move",
                                                        "type", "key", "scroll", "wait"]),
                    x=i("X pixel"), y=i("Y pixel"), text=s("Text to type"), key=s("xdotool key name"),
                    direction=s("up|down|left|right"), amount=i("Scroll clicks"), seconds={"type": "number"}),
             computer, risk="risky", timeout=60,
             subject=lambda a: f"{a.get('action', '')} {a.get('text') or a.get('key') or ''}".strip()),
    ]
