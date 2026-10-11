"""The web: search and extraction (scraping) without spending a browser turn per page."""

import asyncio
import hashlib
import re
import shlex
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx

from . import Tool, ToolContext, i, params, s

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0 Safari/537.36 Drok"
SKIP = {"script", "style", "noscript", "svg", "head", "template", "iframe", "nav", "footer", "aside"}
BLOCK = {"p", "div", "section", "article", "main", "header", "li", "tr", "br", "table", "ul", "ol", "blockquote",
         "pre", "h1", "h2", "h3", "h4", "h5", "h6", "dt", "dd", "figure", "figcaption"}


class _Markdown(HTMLParser):
    """Readable markdown-ish text from HTML: headings, paragraphs, list items, links. Drops chrome (nav, footers)."""

    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base, self.out, self.skip, self.href, self.title, self.in_title = base, [], 0, None, "", False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.in_title = True
            return
        if tag in SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        if tag in BLOCK:
            self.out.append("\n")
        if tag in ("h1", "h2", "h3", "h4"):
            self.out.append("#" * int(tag[1]) + " ")
        elif tag == "li":
            self.out.append("- ")
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            if href and not href.startswith(("javascript:", "#", "mailto:")):
                self.href = urljoin(self.base, href)
                self.out.append("[")

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
            return
        if tag in SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == "a" and self.href:
            self.out.append(f"]({self.href})")
            self.href = None
        if tag in BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif not self.skip:
            self.out.append(data)

    def text(self) -> str:
        raw = "".join(self.out)
        raw = re.sub(r"\[\s*\]\([^)]*\)", "", raw)  # empty links (icons)
        raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
        raw = re.sub(r" *\n *", "\n", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return re.sub(r"\n\n(?=- )", "\n", raw).strip()


def html_to_markdown(html: str, base: str = "") -> tuple[str, str]:
    p = _Markdown(base)
    try:
        p.feed(html)
    except Exception:  # malformed HTML: fall back to tag stripping
        return "", unescape(re.sub(r"<[^>]+>", " ", html))
    return p.title.strip(), p.text()


def html_to_text(raw: str) -> str:
    return html_to_markdown(raw)[1]


async def _fetch(ctx: ToolContext, client: httpx.AsyncClient, url: str, max_chars: int) -> str:
    try:
        r = await client.get(url)
    except httpx.HTTPError as e:
        return f"## {url}\nError: {e.__class__.__name__}: {e}"
    ctype = r.headers.get("content-type", "")
    head = f"## {r.url} (HTTP {r.status_code})"
    if "pdf" in ctype or str(r.url).lower().endswith(".pdf"):
        cache = ctx.app.settings.workspace / ".drok" / "web"
        cache.mkdir(parents=True, exist_ok=True)
        f = cache / f"{hashlib.sha1(str(r.url).encode()).hexdigest()[:16]}.pdf"
        f.write_bytes(r.content)
        out = await ctx.app.computer.exec(f"pdftotext -layout {shlex.quote('/workspace/.drok/web/' + f.name)} - "
                                          f"| head -c {max_chars}", timeout=90, screen=ctx.agent["screen"])
        return f"{head} [PDF]\n{out['output']}"
    if "html" in ctype or "<html" in r.text[:500].lower():
        title, body = html_to_markdown(r.text, str(r.url))
        return f"{head}\nTitle: {title}\n\n{body[:max_chars]}"
    return f"{head}\n{r.text[:max_chars]}"


async def _render(ctx: ToolContext, url: str, selector: str, max_chars: int) -> str:
    """Load the page in the agent's real browser (JS, logins) in a background tab and extract it."""
    app, screen = ctx.app, ctx.agent["screen"]
    context = await app.browsers.context(screen)
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass  # long-polling sites never go idle; the DOM is good enough
        if selector:
            items = await page.eval_on_selector_all(selector, "els => els.map(e => e.innerText.trim()).filter(Boolean)")
            return f"## {page.url} - {len(items)} matches for {selector}\n" + "\n---\n".join(items)[:max_chars]
        html = await page.content()
        title, body = html_to_markdown(html, page.url)
        if len(body) < 200:  # heavy client-side apps: fall back to what is rendered
            body = await page.inner_text("body")
        return f"## {page.url} (rendered)\nTitle: {title}\n\n{body[:max_chars]}"
    finally:
        await page.close()


async def web_extract(ctx: ToolContext, urls: list, render: bool = False, selector: str = "",
                      max_chars: int = 15000) -> str:
    urls = [u if re.match(r"^https?://", u) else f"https://{u}" for u in (urls if isinstance(urls, list) else [urls])][:5]
    max_chars = min(max(max_chars, 1000), 60000)
    if render or selector:
        results = []
        for u in urls:  # one browser screen: sequential
            try:
                results.append(await _render(ctx, u, selector, max_chars))
            except Exception as e:
                results.append(f"## {u}\nError rendering: {e.__class__.__name__}: {e}")
        return "\n\n".join(results)
    async with httpx.AsyncClient(follow_redirects=True, timeout=30, headers={"User-Agent": UA}) as client:
        return "\n\n".join(await asyncio.gather(*(_fetch(ctx, client, u, max_chars) for u in urls)))


async def web_search(ctx: ToolContext, query: str, count: int = 8) -> str:
    st = ctx.app.settings
    count = min(max(count, 1), 20)
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": UA}, follow_redirects=True) as c:
        if st.brave_api_key:
            r = await c.get("https://api.search.brave.com/res/v1/web/search", params={"q": query, "count": count},
                            headers={"X-Subscription-Token": st.brave_api_key, "Accept": "application/json"})
            items = [(x["title"], x["url"], x.get("description", "")) for x in r.json().get("web", {}).get("results", [])]
        elif st.searxng_url:
            r = await c.get(f"{st.searxng_url}/search", params={"q": query, "format": "json"})
            items = [(x["title"], x["url"], x.get("content", "")) for x in r.json().get("results", [])[:count]]
        else:
            r = await c.post("https://html.duckduckgo.com/html/", data={"q": query})
            items = _parse_ddg(r.text)[:count]
    if not items:
        return "No results (or the search provider blocked the request). Try other words, or search in the browser."
    return "\n\n".join(f"{n}. {html_to_text(t)}\n{u}\n{html_to_text(d)}" for n, (t, u, d) in enumerate(items, 1))


def _parse_ddg(html: str) -> list[tuple[str, str, str]]:
    # ponytail: scrapes DuckDuckGo's HTML endpoint - zero setup, but brittle; set BRAVE_API_KEY or SEARXNG_URL for real use.
    out = []
    for m in re.finditer(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>(.*?)(?=class="result__a"|$)', html, re.S):
        href, title, rest = m.groups()
        q = parse_qs(urlparse(unescape(href)).query)
        url = unquote(q["uddg"][0]) if "uddg" in q else unescape(href)
        if "duckduckgo.com/y.js" in url:  # ads
            continue
        snip = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', rest, re.S)
        out.append((title, url, snip.group(1) if snip else ""))
    return out


def tools(app) -> list[Tool]:
    return [
        Tool("web_search", "Search the web. Returns titles, URLs and snippets - then read pages with web_extract.",
             params(["query"], query=s("Search query"), count=i("Results, default 8, max 20")),
             web_search, risk="safe", subject=lambda a: a.get("query", "")),
        Tool("web_extract", "Scrape 1-5 URLs in parallel into clean markdown (headings, lists, links) - fast, no "
             "browser. PDFs are converted to text. render=true loads them in your real browser (JavaScript, logged-in "
             "sites). selector='CSS' returns the text of each matching element (implies render) - great for tables, "
             "listings and prices. For crawling many pages, write a script and run it with shell.",
             params(["urls"], urls={"type": "array", "items": {"type": "string"}}, render={"type": "boolean"},
                    selector=s("CSS selector, e.g. '.product .price'"),
                    max_chars=i("Per page, default 15000, max 60000")),
             web_extract, risk="safe", timeout=240, subject=lambda a: " ".join(a.get("urls") or [])[:300]),
    ]
