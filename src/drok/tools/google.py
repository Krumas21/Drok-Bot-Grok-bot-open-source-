"""Google: Gmail, Drive and Calendar through Google's REST APIs, with the owner's OAuth consent.

Setup (docs/configuration.md): create an OAuth client of type "Desktop app", put GOOGLE_CLIENT_ID/SECRET in .env,
press Connect on the Settings page. Desktop clients may redirect to any localhost port, so the consent screen sends
the browser to http://localhost:<port>/oauth/google/callback. Opened on the server itself, that completes on its own;
from another device the page fails to load and the owner pastes that address into Drok instead. Tokens are stored
encrypted in the vault and refreshed automatically; agents never see them.
"""

import asyncio
import base64
import hashlib
import json
import mimetypes
import secrets as pysecrets
import shlex
import time
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from . import Tool, ToolContext, i, params, s
from .system import _ws_path
from .web import html_to_text

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
DRIVE = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
CAL = "https://www.googleapis.com/calendar/v3/calendars"
SCOPES = ["openid", "email", "https://www.googleapis.com/auth/gmail.modify", "https://www.googleapis.com/auth/drive",
          "https://www.googleapis.com/auth/calendar.events"]
VAULT_KEY = "_GOOGLE_TOKEN"
EXPORTS = {  # Google-native file -> (mime for reading as text, extension -> mime for downloads)
    "application/vnd.google-apps.document": ("text/plain", {
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "pdf": "application/pdf",
        "md": "text/markdown", "txt": "text/plain", "html": "text/html"}),
    "application/vnd.google-apps.spreadsheet": ("text/csv", {
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "csv": "text/csv",
        "pdf": "application/pdf"}),
    "application/vnd.google-apps.presentation": ("text/plain", {
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "pdf": "application/pdf", "txt": "text/plain"}),
}
NATIVE = {".docx": "document", ".doc": "document", ".md": "document", ".txt": "document", ".html": "document",
          ".xlsx": "spreadsheet", ".csv": "spreadsheet", ".pptx": "presentation"}


class GoogleError(Exception):
    pass


class Google:
    def __init__(self, app, transport: httpx.AsyncBaseTransport | None = None):
        self.app = app
        self.transport = transport
        self._lock = asyncio.Lock()

    # ---------- connection ----------
    @property
    def configured(self) -> bool:
        st = self.app.settings
        return bool(st.google_client_id and st.google_client_secret)

    def token(self) -> dict | None:
        raw = self.app.vault.get(VAULT_KEY)
        return json.loads(raw) if raw else None

    def email(self) -> str | None:
        t = self.token()
        return t.get("email") if t else None

    def redirect_uri(self) -> str:
        return self.app.settings.google_redirect_uri or f"http://localhost:{self.app.settings.port}/oauth/google/callback"

    def auth_url(self) -> tuple[str, str, str]:
        """Returns (url, state, code_verifier)."""
        state, verifier = pysecrets.token_urlsafe(16), pysecrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        q = {"client_id": self.app.settings.google_client_id, "redirect_uri": self.redirect_uri(),
             "response_type": "code", "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent",
             "state": state, "code_challenge": challenge, "code_challenge_method": "S256"}
        return f"{AUTH_URL}?{urlencode(q)}", state, verifier

    @staticmethod
    def parse_redirect(text: str) -> tuple[str, str]:
        """Accepts the full redirect URL the browser landed on, or a bare code."""
        text = text.strip()
        if "code=" in text:
            q = parse_qs(urlparse(text).query)
            return q.get("code", [""])[0], q.get("state", [""])[0]
        return text, ""

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=60, transport=self.transport)

    async def exchange(self, code: str, verifier: str) -> str:
        st = self.app.settings
        async with self._client() as c:
            r = await c.post(TOKEN_URL, data={
                "code": code, "client_id": st.google_client_id, "client_secret": st.google_client_secret,
                "redirect_uri": self.redirect_uri(), "grant_type": "authorization_code", "code_verifier": verifier})
            if r.status_code != 200:
                raise GoogleError(f"Google refused the code: {r.text[:300]}")
            tok = r.json()
            if not tok.get("refresh_token"):
                raise GoogleError("No refresh token returned - remove Drok's access at myaccount.google.com and retry.")
            info = await c.get("https://openidconnect.googleapis.com/v1/userinfo",
                               headers={"Authorization": f"Bearer {tok['access_token']}"})
        email = info.json().get("email", "") if info.status_code == 200 else ""
        self._store({"access_token": tok["access_token"], "refresh_token": tok["refresh_token"],
                     "expires_at": time.time() + tok.get("expires_in", 3600), "scope": tok.get("scope", ""),
                     "email": email})
        return email

    def _store(self, tok: dict) -> None:
        self.app.vault.set(VAULT_KEY, json.dumps(tok), "Google OAuth token (managed by Drok)")

    async def disconnect(self) -> None:
        tok = self.token()
        if tok:
            try:
                async with self._client() as c:
                    await c.post("https://oauth2.googleapis.com/revoke", data={"token": tok["refresh_token"]})
            except httpx.HTTPError:
                pass  # revoking is best effort; the local token is deleted regardless
        self.app.vault.delete(VAULT_KEY)

    async def _access_token(self) -> str:
        async with self._lock:
            tok = self.token()
            if not tok:
                raise GoogleError("Google isn't connected. Ask the owner to connect it on the Settings page.")
            if tok["expires_at"] - 60 > time.time():
                return tok["access_token"]
            st = self.app.settings
            async with self._client() as c:
                r = await c.post(TOKEN_URL, data={"client_id": st.google_client_id, "client_secret":
                                                  st.google_client_secret, "refresh_token": tok["refresh_token"],
                                                  "grant_type": "refresh_token"})
            if r.status_code != 200:
                raise GoogleError(f"Google token refresh failed ({r.status_code}): {r.text[:200]}. The owner may need "
                                  "to reconnect Google on the Settings page.")
            fresh = r.json()
            tok |= {"access_token": fresh["access_token"], "expires_at": time.time() + fresh.get("expires_in", 3600)}
            self._store(tok)
            return tok["access_token"]

    async def call(self, method: str, url: str, **kw) -> httpx.Response:
        extra = kw.pop("headers", {})
        for attempt in range(2):
            headers = {**extra, "Authorization": f"Bearer {await self._access_token()}"}
            async with self._client() as c:
                r = await c.request(method, url, headers=headers, **kw)
            if r.status_code == 401 and attempt == 0:
                tok = self.token()
                tok["expires_at"] = 0  # force a refresh and retry once
                self._store(tok)
                continue
            if r.status_code >= 400:
                try:
                    msg = r.json().get("error", {}).get("message", r.text)
                except ValueError:
                    msg = r.text
                raise GoogleError(f"Google API {r.status_code}: {str(msg)[:400]}")
            return r
        raise GoogleError("Google authorization failed twice")

    async def json(self, method: str, url: str, **kw) -> dict:
        return (await self.call(method, url, **kw)).json()


# ---------------- Gmail ----------------
def _headers(msg: dict) -> dict:
    return {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}


def _body(part: dict) -> tuple[str, str, list]:
    """(plain text, html, attachments) from a Gmail message payload, walking multipart trees."""
    plain, html, files = "", "", []
    stack = [part]
    while stack:
        p = stack.pop(0)
        stack.extend(p.get("parts") or [])
        body = p.get("body", {})
        if p.get("filename") and body.get("attachmentId"):
            files.append({"filename": p["filename"], "size": body.get("size", 0), "attachment_id": body["attachmentId"]})
            continue
        data = body.get("data")
        if not data:
            continue
        text = base64.urlsafe_b64decode(data + "===").decode(errors="replace")
        if p.get("mimeType") == "text/plain" and not plain:
            plain = text
        elif p.get("mimeType") == "text/html" and not html:
            html = text
    return plain, html, files


async def gmail_search(ctx: ToolContext, query: str, max_results: int = 10) -> str:
    g = ctx.app.google
    res = await g.json("GET", f"{GMAIL}/messages", params={"q": query, "maxResults": min(max(max_results, 1), 50)})
    ids = [m["id"] for m in res.get("messages", [])]
    if not ids:
        return "No messages match."
    metas = await asyncio.gather(*(g.json("GET", f"{GMAIL}/messages/{mid}", params=[
        ("format", "metadata"), ("metadataHeaders", "From"), ("metadataHeaders", "Subject"),
        ("metadataHeaders", "Date")]) for mid in ids))
    lines = []
    for m in metas:
        h = _headers(m)
        unread = " (unread)" if "UNREAD" in m.get("labelIds", []) else ""
        lines.append(f"[{m['id']}]{unread} {h.get('date', '')} | {h.get('from', '')} | {h.get('subject', '(no subject)')}"
                     f"\n    {m.get('snippet', '')[:200]}")
    return "\n".join(lines)


async def gmail_read(ctx: ToolContext, message_id: str) -> str:
    m = await ctx.app.google.json("GET", f"{GMAIL}/messages/{message_id}", params={"format": "full"})
    h = _headers(m)
    plain, html, files = _body(m.get("payload", {}))
    text = plain or html_to_text(html)
    att = "\n".join(f"- {f['filename']} ({f['size']} B) attachment_id={f['attachment_id']}" for f in files)
    return (f"From: {h.get('from', '')}\nTo: {h.get('to', '')}\nCc: {h.get('cc', '')}\nDate: {h.get('date', '')}\n"
            f"Subject: {h.get('subject', '')}\nThread: {m.get('threadId')}\nLabels: {', '.join(m.get('labelIds', []))}\n\n"
            f"{text[:20000]}" + (f"\n\nAttachments:\n{att}" if att else ""))


async def gmail_attachment(ctx: ToolContext, message_id: str, attachment_id: str, filename: str) -> str:
    r = await ctx.app.google.json("GET", f"{GMAIL}/messages/{message_id}/attachments/{attachment_id}")
    dest = _ws_path(ctx, f"/workspace/inbox/{Path(filename).name}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(base64.urlsafe_b64decode(r["data"] + "==="))
    return f"Saved /workspace/inbox/{dest.name} ({dest.stat().st_size} bytes)."


async def _mime(ctx: ToolContext, to: str, subject: str, body: str, cc: str, reply_to: str,
                attachments: list | None) -> dict:
    msg = EmailMessage()
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    thread = None
    if reply_to:
        orig = await ctx.app.google.json("GET", f"{GMAIL}/messages/{reply_to}", params=[
            ("format", "metadata"), ("metadataHeaders", "Message-ID"), ("metadataHeaders", "References"),
            ("metadataHeaders", "Subject")])
        h = _headers(orig)
        thread = orig.get("threadId")
        if h.get("message-id"):
            msg["In-Reply-To"] = h["message-id"]
            msg["References"] = f"{h.get('references', '')} {h['message-id']}".strip()
        if not subject:
            subject = h.get("subject", "")
            subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    msg["Subject"] = subject
    msg.set_content(body)
    for path in attachments or []:
        p = _ws_path(ctx, path)
        ctype = (mimetypes.guess_type(p.name)[0] or "application/octet-stream").split("/")
        msg.add_attachment(p.read_bytes(), maintype=ctype[0], subtype=ctype[1], filename=p.name)
    out = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
    if thread:
        out["threadId"] = thread
    return out


async def gmail_draft(ctx: ToolContext, to: str, subject: str = "", body: str = "", cc: str = "",
                      reply_to_message_id: str = "", attachments: list | None = None) -> str:
    message = await _mime(ctx, to, subject, body, cc, reply_to_message_id, attachments)
    d = await ctx.app.google.json("POST", f"{GMAIL}/drafts", json={"message": message})
    return f"Draft saved in Gmail (draft id {d['id']}). The owner can review and send it from Gmail."


async def gmail_send(ctx: ToolContext, to: str, subject: str = "", body: str = "", cc: str = "",
                     reply_to_message_id: str = "", attachments: list | None = None) -> str:
    message = await _mime(ctx, to, subject, body, cc, reply_to_message_id, attachments)
    r = await ctx.app.google.json("POST", f"{GMAIL}/messages/send", json=message)
    return f"Sent (message id {r['id']})."


async def gmail_modify(ctx: ToolContext, message_id: str, add_labels: list | None = None,
                       remove_labels: list | None = None, archive: bool = False, mark_read: bool = False) -> str:
    g = ctx.app.google
    add, remove = list(add_labels or []), list(remove_labels or [])
    if archive:
        remove.append("INBOX")
    if mark_read:
        remove.append("UNREAD")
    if any(not lbl.isupper() for lbl in add + remove):
        labels = {lbl["name"].lower(): lbl["id"] for lbl in (await g.json("GET", f"{GMAIL}/labels"))["labels"]}
        missing = [lbl for lbl in add if not lbl.isupper() and lbl.lower() not in labels]
        for name in missing:  # create user labels on demand
            labels[name.lower()] = (await g.json("POST", f"{GMAIL}/labels", json={"name": name}))["id"]
        add = [lbl if lbl.isupper() else labels[lbl.lower()] for lbl in add]
        remove = [lbl if lbl.isupper() else labels.get(lbl.lower(), lbl) for lbl in remove]
    await g.json("POST", f"{GMAIL}/messages/{message_id}/modify", json={"addLabelIds": add, "removeLabelIds": remove})
    return "Updated."


# ---------------- Drive ----------------
async def drive_search(ctx: ToolContext, query: str = "", max_results: int = 20) -> str:
    if any(op in query for op in ("=", " contains ", " in parents", "mimeType")):
        q = query
    elif query:
        safe = query.replace("\\", "\\\\").replace("'", "\\'")
        q = f"(name contains '{safe}' or fullText contains '{safe}') and trashed = false"
    else:
        q = "trashed = false"
    res = await ctx.app.google.json("GET", f"{DRIVE}/files", params={
        "q": q, "pageSize": min(max(max_results, 1), 100), "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true", "fields": "files(id,name,mimeType,modifiedTime,size,webViewLink)"})
    files = res.get("files", [])
    return "\n".join(f"[{f['id']}] {f['name']} ({f['mimeType'].rsplit('.', 1)[-1]}, {f.get('size', '-')} B, "
                     f"modified {f.get('modifiedTime', '')[:10]}) {f.get('webViewLink', '')}" for f in files) or "No files."


async def drive_read(ctx: ToolContext, file_id: str, max_chars: int = 30000) -> str:
    g = ctx.app.google
    meta = await g.json("GET", f"{DRIVE}/files/{file_id}", params={"fields": "name,mimeType,size",
                                                                     "supportsAllDrives": "true"})
    mime = meta["mimeType"]
    if mime in EXPORTS:
        r = await g.call("GET", f"{DRIVE}/files/{file_id}/export", params={"mimeType": EXPORTS[mime][0]})
        return f"# {meta['name']}\n\n{r.text[:max_chars]}"
    if mime == "application/pdf":
        r = await g.call("GET", f"{DRIVE}/files/{file_id}", params={"alt": "media", "supportsAllDrives": "true"})
        cache = ctx.app.settings.workspace / ".drok" / "drive"
        cache.mkdir(parents=True, exist_ok=True)
        (cache / f"{file_id}.pdf").write_bytes(r.content)
        out = await ctx.app.computer.exec(f"pdftotext -layout {shlex.quote(f'/workspace/.drok/drive/{file_id}.pdf')} - "
                                          f"| head -c {max_chars}", timeout=90, screen=ctx.agent["screen"])
        return f"# {meta['name']} [PDF]\n\n{out['output']}"
    if mime.startswith("text/") or mime in ("application/json", "application/xml"):
        r = await g.call("GET", f"{DRIVE}/files/{file_id}", params={"alt": "media", "supportsAllDrives": "true"})
        return f"# {meta['name']}\n\n{r.text[:max_chars]}"
    return f"{meta['name']} is {mime}; download it with drive_download and open it with read_file or a script."


async def drive_download(ctx: ToolContext, file_id: str, path: str = "", format: str = "") -> str:
    g = ctx.app.google
    meta = await g.json("GET", f"{DRIVE}/files/{file_id}", params={"fields": "name,mimeType",
                                                                     "supportsAllDrives": "true"})
    name, mime = meta["name"], meta["mimeType"]
    if mime in EXPORTS:
        formats = EXPORTS[mime][1]
        fmt = format or next(iter(formats))
        if fmt not in formats:
            return f"Can't export {mime} as {fmt}; options: {', '.join(formats)}"
        r = await g.call("GET", f"{DRIVE}/files/{file_id}/export", params={"mimeType": formats[fmt]})
        name = f"{name}.{fmt}"
    else:
        r = await g.call("GET", f"{DRIVE}/files/{file_id}", params={"alt": "media", "supportsAllDrives": "true"})
    dest = _ws_path(ctx, path or f"/workspace/drive/{Path(name).name}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(r.content)
    return f"Saved /workspace/{dest.relative_to(ctx.app.settings.workspace)} ({dest.stat().st_size} bytes)."


async def drive_upload(ctx: ToolContext, path: str, name: str = "", folder_id: str = "",
                       convert: bool = False) -> str:
    src = _ws_path(ctx, path)
    if not src.is_file():
        return f"Not found: {path}"
    meta = {"name": name or src.name}
    if folder_id:
        meta["parents"] = [folder_id]
    if convert and src.suffix.lower() in NATIVE:
        meta["mimeType"] = f"application/vnd.google-apps.{NATIVE[src.suffix.lower()]}"
        if not name:
            meta["name"] = src.stem
    boundary = f"drok{pysecrets.token_hex(8)}"
    ctype = mimetypes.guess_type(src.name)[0] or "application/octet-stream"
    body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(meta)}\r\n"
            f"--{boundary}\r\nContent-Type: {ctype}\r\n\r\n").encode() + src.read_bytes() + f"\r\n--{boundary}--".encode()
    f = await ctx.app.google.json("POST", UPLOAD, params={"uploadType": "multipart", "supportsAllDrives": "true",
                                                          "fields": "id,name,webViewLink"},
                                  content=body, headers={"Content-Type": f"multipart/related; boundary={boundary}"})
    return f"Uploaded '{f['name']}' (id {f['id']}): {f.get('webViewLink', '')}"


async def drive_create_folder(ctx: ToolContext, name: str, parent_id: str = "") -> str:
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        meta["parents"] = [parent_id]
    f = await ctx.app.google.json("POST", f"{DRIVE}/files", json=meta, params={"fields": "id,name,webViewLink",
                                                                                "supportsAllDrives": "true"})
    return f"Folder '{f['name']}' (id {f['id']}): {f.get('webViewLink', '')}"


# ---------------- Calendar ----------------
async def calendar_events(ctx: ToolContext, days: int = 7, query: str = "", calendar: str = "primary") -> str:
    start = datetime.now(UTC)
    params_ = {"timeMin": start.isoformat(), "timeMax": (start + timedelta(days=min(max(days, 1), 90))).isoformat(),
               "singleEvents": "true", "orderBy": "startTime", "maxResults": 100}
    if query:
        params_["q"] = query
    res = await ctx.app.google.json("GET", f"{CAL}/{calendar}/events", params=params_)
    lines = []
    for e in res.get("items", []):
        when = e.get("start", {}).get("dateTime") or e.get("start", {}).get("date", "")
        who = ", ".join(a.get("email", "") for a in e.get("attendees", [])[:6])
        lines.append(f"[{e['id']}] {when} - {e.get('summary', '(no title)')}"
                     + (f" @ {e['location']}" if e.get("location") else "") + (f" with {who}" if who else ""))
    return "\n".join(lines) or "No events in that window."


async def calendar_create_event(ctx: ToolContext, summary: str, start: str, end: str, description: str = "",
                                location: str = "", attendees: list | None = None, timezone: str = "") -> str:
    tz = timezone or ctx.app.settings.timezone

    def when(v: str) -> dict:
        return {"date": v} if len(v) == 10 else {"dateTime": v, "timeZone": tz}

    body = {"summary": summary, "start": when(start), "end": when(end), "description": description,
            "location": location, "attendees": [{"email": a} for a in attendees or []]}
    e = await ctx.app.google.json("POST", f"{CAL}/primary/events", json=body,
                                  params={"sendUpdates": "all" if attendees else "none"})
    return f"Created '{e.get('summary')}' ({e['id']}): {e.get('htmlLink', '')}"


def tools(app) -> list[Tool]:
    if not app.google.token():
        return []
    return [
        Tool("gmail_search", "Search the owner's Gmail with Gmail query syntax (from:, to:, subject:, is:unread, "
             "newer_than:2d, has:attachment, label:...). Returns ids, senders, subjects, snippets.",
             params(["query"], query=s("Gmail search query"), max_results=i("Default 10, max 50")),
             gmail_search, risk="safe", subject=lambda a: a.get("query", "")),
        Tool("gmail_read", "Read one email in full (headers, body, attachment list).",
             params(["message_id"], message_id=s("Message id from gmail_search")), gmail_read, risk="safe"),
        Tool("gmail_attachment", "Save an email attachment to /workspace/inbox.",
             params(["message_id", "attachment_id", "filename"], message_id=s("Message id"),
                    attachment_id=s("From gmail_read"), filename=s("File name")),
             gmail_attachment, risk="safe"),
        Tool("gmail_draft", "Create a Gmail draft (nothing is sent). Set reply_to_message_id to reply in-thread. "
             "Attachments are /workspace paths. Prefer drafting unless the owner asked you to send.",
             params(["to"], to=s("Recipients, comma-separated"), subject=s("Subject (auto 'Re:' for replies)"),
                    body=s("Plain-text body"), cc=s("Cc"), reply_to_message_id=s("Reply in this message's thread"),
                    attachments={"type": "array", "items": {"type": "string"}}),
             gmail_draft, risk="risky", subject=lambda a: f"draft to {a.get('to')}: {a.get('subject', '')}"),
        Tool("gmail_send", "Send an email from the owner's Gmail (always needs the owner's approval).",
             params(["to", "body"], to=s("Recipients, comma-separated"), subject=s("Subject"), body=s("Plain-text body"),
                    cc=s("Cc"), reply_to_message_id=s("Reply in this message's thread"),
                    attachments={"type": "array", "items": {"type": "string"}}),
             gmail_send, risk="risky", subject=lambda a: f"to {a.get('to')}: {a.get('subject', '')}\n\n{a.get('body', '')[:600]}"),
        Tool("gmail_modify", "Organize an email: add/remove labels (created if missing), archive, mark read.",
             params(["message_id"], message_id=s("Message id"), add_labels={"type": "array", "items": {"type": "string"}},
                    remove_labels={"type": "array", "items": {"type": "string"}}, archive={"type": "boolean"},
                    mark_read={"type": "boolean"}),
             gmail_modify, risk="risky"),
        Tool("drive_search", "Find files in the owner's Google Drive by name/content words (or a raw Drive query).",
             params([], query=s("Words, or a Drive query like \"'FOLDER_ID' in parents\""),
                    max_results=i("Default 20")),
             drive_search, risk="safe", subject=lambda a: a.get("query", "")),
        Tool("drive_read", "Read a Drive file as text: Google Docs, Sheets (CSV), Slides, PDFs and text files.",
             params(["file_id"], file_id=s("File id"), max_chars=i("Default 30000")), drive_read, risk="safe"),
        Tool("drive_download", "Download a Drive file into /workspace (Google Docs/Sheets/Slides exported to "
             "docx/xlsx/pptx/pdf/csv/md).",
             params(["file_id"], file_id=s("File id"), path=s("Destination (default /workspace/drive/<name>)"),
                    format=s("Export format for Google-native files")),
             drive_download, risk="safe"),
        Tool("drive_upload", "Upload a /workspace file to the owner's Drive; convert=true turns docx/md/csv/xlsx/pptx "
             "into editable Google Docs/Sheets/Slides. Returns the link.",
             params(["path"], path=s("/workspace path"), name=s("Name in Drive"), folder_id=s("Target folder id"),
                    convert={"type": "boolean"}),
             drive_upload, risk="risky", subject=lambda a: a.get("path", "")),
        Tool("drive_create_folder", "Create a folder in Drive.",
             params(["name"], name=s("Folder name"), parent_id=s("Parent folder id")),
             drive_create_folder, risk="risky", subject=lambda a: a.get("name", "")),
        Tool("calendar_events", "List upcoming events from the owner's Google Calendar.",
             params([], days=i("Days ahead, default 7"), query=s("Filter text"), calendar=s("Calendar id")),
             calendar_events, risk="safe"),
        Tool("calendar_create_event", "Create a calendar event (invites attendees). Needs the owner's approval.",
             params(["summary", "start", "end"], summary=s("Title"),
                    start=s("ISO datetime 2026-10-12T14:00:00 or date 2026-10-12"), end=s("ISO datetime or date"),
                    description=s("Details"), location=s("Where"),
                    attendees={"type": "array", "items": {"type": "string"}}, timezone=s("IANA tz")),
             calendar_create_event, risk="risky", subject=lambda a: f"{a.get('start')} {a.get('summary')}"),
    ]
