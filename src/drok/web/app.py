"""Web UI + API. Server-rendered Jinja pages, vanilla JS, Server-Sent Events for live updates."""

import asyncio
import hmac
import json
import logging
import mimetypes
import shutil
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .. import commands
from ..app import App
from ..db import now
from ..llm import split_model

log = logging.getLogger("drok.web")
HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
PUBLIC = ("/login", "/static/", "/healthz", "/hooks/")


def ago(ts: float | None) -> str:
    if not ts:
        return "-"
    d = time.time() - ts
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)}m ago"
    if d < 86400:
        return f"{int(d // 3600)}h ago"
    return datetime.fromtimestamp(ts).strftime("%b %d")


templates.env.filters["ago"] = ago
templates.env.filters["clock"] = lambda ts: datetime.fromtimestamp(ts).strftime("%H:%M") if ts else ""
templates.env.filters["tojson_pretty"] = lambda v: json.dumps(v, indent=2, ensure_ascii=False)
# Jinja's tojson escapes & < > for <script> contexts; these values only go into autoescaped HTML text.
templates.env.filters["json"] = lambda v: json.dumps(v, ensure_ascii=False)


def create_web(drok: App) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_):
        await drok.start()
        yield
        await drok.stop()

    web = FastAPI(title="Drok", lifespan=lifespan, docs_url=None, redoc_url=None)
    web.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @web.middleware("http")
    async def auth(request: Request, call_next):
        path = request.url.path
        if not path.startswith(PUBLIC) and not request.session.get("auth"):
            if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
                return RedirectResponse("/login")
            return JSONResponse({"error": "login required"}, status_code=401)
        return await call_next(request)

    # Session middleware must wrap the auth middleware, so it is added last (outermost).
    web.add_middleware(SessionMiddleware, secret_key=drok.settings.session_key, session_cookie="drok_session",
                       max_age=60 * 60 * 24 * 30, same_site="lax")

    def page(request: Request, name: str, **ctx) -> HTMLResponse:
        base = {
            "agents": drok.agents(), "groups": drok.groups(), "path": request.url.path,
            "pending": drok.db.one("SELECT COUNT(*) n FROM approvals WHERE status = 'pending'")["n"],
            "orchestrator": drok.orchestrator(), "agent_map": {a["id"]: a for a in drok.agents()},
            "dm_ids": {a["id"]: drok.dm(a["id"])["id"] for a in drok.agents()},
            "open_goals": drok.db.one("SELECT COUNT(*) n FROM goals WHERE status IN ('active','blocked')")["n"],
        }
        return templates.TemplateResponse(request, name, {**base, **ctx})

    def back(request: Request, fallback: str = "/") -> RedirectResponse:
        return RedirectResponse(request.headers.get("referer") or fallback, status_code=303)

    # ---------------- auth ----------------
    @web.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, error: str = ""):
        return templates.TemplateResponse(request, "login.html", {"error": error})

    @web.post("/login")
    async def login(request: Request, password: str = Form(...)):
        if hmac.compare_digest(password.encode(), drok.settings.admin_password.encode()):
            request.session["auth"] = True
            drok.touch_user()
            return RedirectResponse("/", status_code=303)
        await asyncio.sleep(1.5)  # slows brute force from the LAN
        return RedirectResponse("/login?error=1", status_code=303)

    @web.post("/logout")
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @web.get("/healthz")
    async def healthz():
        return {"ok": True}

    # ---------------- conversations ----------------
    @web.get("/")
    async def home():
        orch = drok.orchestrator()
        return RedirectResponse(f"/c/{drok.dm(orch['id'])['id']}" if orch else "/agents")

    def render_messages(request: Request, rows: list[dict]) -> str:
        return "".join(
            templates.get_template("_message.html").render(m=m, agent_map={a["id"]: a for a in drok.agents()},
                                                           request=request)
            for m in rows
        )

    @web.get("/c/{cid}", response_class=HTMLResponse)
    async def chat(request: Request, cid: int):
        conv = drok.conversation(cid)
        if not conv:
            raise HTTPException(404)
        rows = drok.db.all("SELECT * FROM (SELECT * FROM messages WHERE conversation_id = ? ORDER BY id DESC "
                           "LIMIT 150) ORDER BY id", cid)
        members = [drok.agent(m) for m in conv["members"] if drok.agent(m)]
        agent = members[0] if conv["kind"] == "dm" and members else None
        if agent:
            drok.db.set_kv(f"read:{cid}", now())
        return page(request, "chat.html", conv=conv, members=members, agent=agent,
                    messages_html=render_messages(request, rows), last_id=rows[-1]["id"] if rows else 0,
                    skills=drok.skills.list(), goals=drok.goals.active_for(agent["id"]) if agent else [])

    @web.get("/c/{cid}/messages", response_class=HTMLResponse)
    async def messages_after(request: Request, cid: int, after: int = 0):
        rows = drok.db.all("SELECT * FROM messages WHERE conversation_id = ? AND id > ? ORDER BY id LIMIT 200",
                           cid, after)
        return HTMLResponse(render_messages(request, rows))

    @web.get("/m/{mid}", response_class=HTMLResponse)
    async def one_message(request: Request, mid: int):
        m = drok.db.one("SELECT * FROM messages WHERE id = ?", mid)
        return HTMLResponse(render_messages(request, [m] if m else []))

    def _save_upload(f: UploadFile) -> str:
        dest = drok.inbox_file(f.filename or "file")
        with dest.open("wb") as out:
            shutil.copyfileobj(f.file, out)
        return f"[Owner uploaded a file: /workspace/inbox/{dest.name}]"

    @web.post("/c/{cid}/send")
    async def send(cid: int, text: str = Form(""), files: list[UploadFile] = File(default=[])):
        if not drok.conversation(cid):
            raise HTTPException(404)
        files = [f for f in files if f.filename]
        if not files:
            if await commands.handle(drok, cid, text, "web") is not None:
                return {"ok": True}
        notes = [await asyncio.to_thread(_save_upload, f) for f in files]
        body = "\n".join([text.strip(), *notes]).strip()
        if not body:
            raise HTTPException(400, "empty")
        msg = drok.runtime.user_message(cid, body, source="web")
        return {"id": msg["id"]}

    @web.post("/groups")
    async def create_group(request: Request, title: str = Form(""), members: list[str] = Form(...)):
        try:
            g = drok.create_group(title, members)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return RedirectResponse(f"/c/{g['id']}", status_code=303)

    @web.post("/groups/{cid}/delete")
    async def delete_group(cid: int):
        drok.db.run("DELETE FROM conversations WHERE id = ? AND kind = 'group'", cid)
        drok.db.run("DELETE FROM messages WHERE conversation_id = ?", cid)
        return RedirectResponse("/", status_code=303)

    # ---------------- agents ----------------
    @web.get("/agents", response_class=HTMLResponse)
    async def agents_page(request: Request):
        return page(request, "agents.html", providers=drok.llm.providers,
                    default_model=drok.settings.default_model)

    @web.get("/agents/{agent_id}", response_class=HTMLResponse)
    async def agent_page(request: Request, agent_id: str):
        agent = drok.agent(agent_id)
        if not agent:
            raise HTTPException(404)
        mem = drok.memory
        routines = drok.db.all("SELECT * FROM routines WHERE agent_id = ?", agent_id)
        goals = drok.db.all("SELECT * FROM goals WHERE agent_id = ? ORDER BY id DESC LIMIT 10", agent_id)
        learned = drok.db.all("SELECT * FROM messages WHERE author = ? AND kind = 'learned' ORDER BY id DESC LIMIT 12",
                              agent_id)
        tool_names = sorted(drok.all_tools())
        return page(request, "agent.html", a=agent, own=mem.entries(agent_id), shared=mem.entries(None),
                    own_usage=mem.usage(agent_id), shared_usage=mem.usage(None), routines=routines, goals=goals,
                    learned=learned, tool_names=tool_names, providers=drok.llm.providers,
                    error=request.query_params.get("error", ""))

    def _agent_fields(name, title, description, system_prompt, model, fallbacks, tools, reasoning="") -> dict:
        model = model.strip() or drok.settings.default_model
        split_model(model)
        return {"name": name.strip(), "title": title.strip(), "description": description.strip(),
                "system_prompt": system_prompt.strip(), "model": model,
                "reasoning": reasoning if reasoning in ("low", "medium", "high") else "",
                "fallbacks": [f.strip() for f in fallbacks.split(",") if f.strip()],
                "tools": [t.strip() for t in tools.split(",") if t.strip()] or ["*"]}

    @web.post("/agents")
    async def create_agent(name: str = Form(...), title: str = Form(""), description: str = Form(""),
                           system_prompt: str = Form(""), model: str = Form(""), fallbacks: str = Form(""),
                           tools: str = Form("*"), reasoning: str = Form("")):
        try:
            f = _agent_fields(name, title, description, system_prompt, model, fallbacks, tools, reasoning)
        except Exception as e:
            raise HTTPException(400, str(e)) from e
        effort = f.pop("reasoning")
        a = drok.create_agent(**f)
        drok.db.update("agents", a["id"], reasoning=effort)
        return RedirectResponse(f"/c/{drok.dm(a['id'])['id']}", status_code=303)

    @web.post("/agents/{agent_id}")
    async def update_agent(agent_id: str, name: str = Form(...), title: str = Form(""),
                           description: str = Form(""), system_prompt: str = Form(""), model: str = Form(""),
                           fallbacks: str = Form(""), tools: str = Form("*"), screen: int = Form(1),
                           is_orchestrator: str = Form(""), reasoning: str = Form("")):
        try:
            f = _agent_fields(name, title, description, system_prompt, model, fallbacks, tools, reasoning)
        except Exception as e:
            raise HTTPException(400, str(e)) from e
        if is_orchestrator:
            drok.db.run("UPDATE agents SET is_orchestrator = 0")
        drok.db.update("agents", agent_id, **f, screen=max(1, min(screen, drok.settings.screens)),
                       **({"is_orchestrator": 1} if is_orchestrator else {}))
        drok.bus.publish({"type": "agents"})
        return RedirectResponse(f"/agents/{agent_id}", status_code=303)

    @web.post("/agents/{agent_id}/delete")
    async def delete_agent(agent_id: str):
        drok.delete_agent(agent_id)
        return RedirectResponse("/agents", status_code=303)

    @web.post("/agents/{agent_id}/stop")
    async def stop_agent(agent_id: str):
        return {"stopped": drok.runtime.stop(agent_id)}

    @web.post("/agents/{agent_id}/handback")
    async def handback(request: Request, agent_id: str):
        drok.touch_user()
        drok.runtime.enqueue(agent_id, drok.dm(agent_id)["id"],
                             "[system] The owner finished the manual step on your screen and handed it back. "
                             "Take a screenshot or snapshot to see the current state, then continue.", "system")
        return back(request, "/computer")

    @web.post("/agents/{agent_id}/memory")
    async def add_memory(request: Request, agent_id: str, content: str = Form(...), shared: str = Form("")):
        from ..memory import MemoryRefused
        try:
            drok.memory.apply(agent_id, "add", "shared" if shared else "agent", content)
        except MemoryRefused as e:
            return RedirectResponse(f"/agents/{agent_id}?error={quote(str(e)[:300])}", status_code=303)
        return back(request)

    @web.post("/memories/{mid}/delete")
    async def delete_memory(request: Request, mid: int):
        drok.db.run("DELETE FROM memories WHERE id = ?", mid)
        return back(request)

    # ---------------- approvals ----------------
    @web.get("/approvals", response_class=HTMLResponse)
    async def approvals_page(request: Request):
        pending = drok.db.all("SELECT * FROM approvals WHERE status = 'pending' ORDER BY id")
        recent = drok.db.all("SELECT * FROM approvals WHERE status != 'pending' ORDER BY id DESC LIMIT 30")
        return page(request, "approvals.html", pending_list=pending, recent=recent)

    @web.post("/approvals/{aid}")
    async def decide(request: Request, aid: int, decision: str = Form(...), args: str = Form("")):
        edited = None
        if args.strip():
            try:
                edited = json.loads(args)
            except ValueError as e:
                raise HTTPException(400, f"Arguments must be valid JSON: {e}") from e
        drok.runtime.resolve_approval(aid, decision != "deny", edited, always=decision == "always")
        if request.headers.get("x-requested-with") == "fetch":
            return {"ok": True}
        return back(request, "/approvals")

    # ---------------- computer ----------------
    @web.get("/computer", response_class=HTMLResponse)
    async def computer_page(request: Request):
        health = await drok.computer.health()
        by_screen: dict[int, list] = {}
        for a in drok.agents():
            by_screen.setdefault(a["screen"], []).append(a)
        teaching = {n: drok.teacher.active(n) for n in range(1, drok.settings.screens + 1)}
        return page(request, "computer.html", health=health, screens=range(1, drok.settings.screens + 1),
                    by_screen=by_screen, teaching=teaching)

    @web.post("/computer/recover")
    async def recover(request: Request, reset: str = Form("")):
        try:
            await drok.computer.recover(reset=bool(reset))
        except Exception as e:
            raise HTTPException(502, str(e)) from e
        await drok.browsers.close()
        return back(request, "/computer")

    @web.post("/teach/start")
    async def teach_start(request: Request, screen: int = Form(...), agent_id: str = Form(...),
                          goal: str = Form(...)):
        try:
            await drok.teacher.start(screen, agent_id, goal)
        except Exception as e:
            raise HTTPException(400, f"Couldn't start recording: {e}") from e
        return back(request, "/computer")

    @web.post("/teach/stop")
    async def teach_stop(request: Request, screen: int = Form(...)):
        slug = await drok.teacher.stop(screen)
        return RedirectResponse(f"/skills/{slug}" if slug else "/computer", status_code=303)

    @web.websocket("/vnc/{screen}")
    async def vnc(ws: WebSocket, screen: int):
        # Bridge the browser's WebSocket to x11vnc's raw TCP port (what websockify does), behind our login.
        if not ws.session.get("auth") or not 1 <= screen <= drok.settings.screens:
            await ws.close(code=4401)
            return
        offered = ws.headers.get("sec-websocket-protocol", "")
        await ws.accept(subprotocol="binary" if "binary" in offered else None)
        host, port = drok.computer.vnc_addr(screen)
        try:
            reader, writer = await asyncio.open_connection(host, port)
        except OSError:
            await ws.close(code=1011)
            return

        async def up():
            try:
                while True:
                    writer.write(await ws.receive_bytes())
                    await writer.drain()
            except (WebSocketDisconnect, RuntimeError, KeyError):
                pass

        async def down():
            try:
                while data := await reader.read(65536):
                    await ws.send_bytes(data)
            except (WebSocketDisconnect, RuntimeError, OSError):
                pass

        tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            writer.close()
            try:
                await ws.close()
            except (RuntimeError, WebSocketDisconnect):
                pass  # browser already went away

    # ---------------- routines ----------------
    @web.get("/routines", response_class=HTMLResponse)
    async def routines_page(request: Request):
        rows = drok.db.all("SELECT * FROM routines ORDER BY agent_id, id")
        runs = {r["id"]: drok.db.all("SELECT * FROM routine_runs WHERE routine_id = ? ORDER BY id DESC", r["id"])
                for r in rows}
        return page(request, "routines.html", routines=rows, runs=runs, fmt=drok.scheduler.fmt,
                    tz=drok.settings.timezone, public_url=drok.settings.public_url)

    @web.post("/routines")
    async def create_routine(agent_id: str = Form(...), name: str = Form(...), instructions: str = Form(...),
                             kind: str = Form("schedule"), cron: str = Form(""), tz: str = Form("UTC"),
                             match: str = Form("")):
        try:
            drok.scheduler.create(agent_id, name, instructions, cron.strip(), tz.strip() or "UTC", kind, match)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        drok.touch_user()
        return RedirectResponse("/routines", status_code=303)

    @web.post("/routines/{rid}/{action}")
    async def routine_action(request: Request, rid: int, action: str):
        r = drok.db.one("SELECT * FROM routines WHERE id = ?", rid)
        if not r:
            raise HTTPException(404)
        try:
            if action == "toggle":
                drok.scheduler.update(rid, enabled=not r["enabled"])
            elif action == "test":
                drok.scheduler.fire(r, test=True)
            elif action == "delete":
                drok.scheduler.delete(rid)
            elif action == "edit":
                form = await request.form()
                changes = {k: form[k].strip() for k in ("name", "instructions", "cron", "tz", "match") if k in form}
                drok.scheduler.update(rid, **changes)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return back(request, "/routines")

    @web.post("/hooks/{rid}")
    async def webhook(request: Request, rid: int):
        r = drok.db.one("SELECT * FROM routines WHERE id = ? AND kind = 'webhook'", rid)
        given = request.headers.get("x-drok-secret") or request.query_params.get("secret", "")
        if not r or not hmac.compare_digest(given.encode(), r["secret"].encode()):
            raise HTTPException(404)
        if not r["enabled"]:
            return {"ok": False, "reason": "paused"}
        payload = (await request.body()).decode(errors="replace")
        if r["match"] and r["match"].lower() not in payload.lower():
            return {"ok": True, "fired": False}
        drok.scheduler.fire(r, payload=payload)
        return {"ok": True, "fired": True}

    # ---------------- skills ----------------
    @web.get("/skills", response_class=HTMLResponse)
    async def skills_page(request: Request):
        return page(request, "skills.html", skills=drok.skills.list(), archived=drok.skills.list(archived=True))

    @web.post("/skills/{slug}/{action}")
    async def skill_action(request: Request, slug: str, action: str):
        sk = drok.skills
        if action == "pin":
            sk.pin(slug, True)
        elif action == "unpin":
            sk.pin(slug, False)
        elif action == "archive":
            sk.archive(slug)
        elif action == "restore":
            sk.restore(slug)
        elif action == "delete":
            sk.delete(slug)
            return RedirectResponse("/skills", status_code=303)
        return back(request, "/skills")

    @web.get("/skills/{slug}", response_class=HTMLResponse)
    async def skill_page(request: Request, slug: str):
        skill = drok.skills.read(slug) if slug != "new" else None
        if slug != "new" and not skill:
            raise HTTPException(404)
        raw = skill["raw"] if skill else "---\nname: new-skill\ndescription: What it does and when to use it.\n---\n\n## When to use\n\n## Steps\n1. \n\n## Validate\n\n## Return\n\n## Needs owner approval\n"
        return page(request, "skill.html", skill=skill, raw=raw, slug=slug)

    @web.post("/skills/{slug}")
    async def save_skill(slug: str, raw: str = Form(...)):
        from ..app import parse_frontmatter, slugify
        meta, _ = parse_frontmatter(raw)
        new_slug = slugify(meta.get("name") or slug)
        if new_slug != slugify(slug) and slug != "new":
            drok.skills.delete(slug)
        drok.skills.write_raw(new_slug, raw.replace("\r\n", "\n"))
        return RedirectResponse(f"/skills/{new_slug}", status_code=303)

    # ---------------- settings ----------------
    @web.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        st = drok.settings
        return page(request, "settings.html", st=st, providers=drok.llm.providers,
                    custom_tools=drok.db.all("SELECT * FROM custom_tools ORDER BY name"),
                    google_email=drok.google.email(), google_configured=drok.google.configured,
                    google_redirect=drok.google.redirect_uri(), google_error=request.query_params.get("google_error", ""),
                    rules=drok.db.all("SELECT * FROM rules ORDER BY tool, id"), secrets=drok.vault.names(),
                    mcp=drok.db.all("SELECT * FROM mcp_servers ORDER BY name"), mcp_errors=drok.mcp.errors,
                    mcp_tools={k: len(v) for k, v in drok.mcp.tool_defs.items()},
                    telegram=bool(drok.telegram), computer=await drok.computer.health())

    @web.post("/settings/rules")
    async def add_rule(request: Request, tool: str = Form(...), subject: str = Form("*"), action: str = Form(...),
                       note: str = Form("")):
        if action not in ("allow", "ask", "deny"):
            raise HTTPException(400)
        drok.db.insert("rules", tool=tool.strip(), subject=subject.strip() or "*", action=action, note=note,
                       created_at=now())
        return back(request, "/settings")

    @web.post("/settings/rules/{rid}/delete")
    async def delete_rule(request: Request, rid: int):
        drok.db.run("DELETE FROM rules WHERE id = ?", rid)
        return back(request, "/settings")

    @web.post("/settings/secrets")
    async def set_secret(request: Request, name: str = Form(...), value: str = Form(...),
                         description: str = Form("")):
        name = name.strip().upper()
        try:
            drok.vault.set(name, value, description)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        drok.touch_user()
        # Wake every agent that asked for this secret.
        asked = {m["author"] for m in drok.db.all("SELECT author, meta FROM messages WHERE kind = 'request'")
                 if m["meta"].get("request") == "secret" and m["meta"].get("name") == name}
        for agent_id in asked:
            if drok.agent(agent_id):
                drok.runtime.enqueue(agent_id, drok.dm(agent_id)["id"],
                                     f"[system] The owner stored secret {name}. Use it as {{{{secret:{name}}}}} "
                                     "and continue the task that needed it.", "system")
        return back(request, "/settings")

    @web.post("/settings/secrets/{name}/delete")
    async def delete_secret(request: Request, name: str):
        drok.vault.delete(name)
        return back(request, "/settings")

    @web.post("/settings/mcp")
    async def add_mcp(request: Request, name: str = Form(...), target: str = Form(...), env: str = Form("")):
        env_map = {}
        for line in env.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                env_map[k.strip()] = v.strip()
        try:
            drok.db.insert("mcp_servers", name=name.strip().lower().replace(" ", "_"), target=target.strip(),
                           env=env_map, created_at=now())
        except Exception as e:
            raise HTTPException(400, f"Couldn't add server: {e}") from e
        await drok.mcp.reload()
        return back(request, "/settings")

    @web.post("/settings/mcp/{sid}/{action}")
    async def mcp_action(request: Request, sid: int, action: str):
        if action == "delete":
            drok.db.run("DELETE FROM mcp_servers WHERE id = ?", sid)
        elif action == "toggle":
            drok.db.run("UPDATE mcp_servers SET enabled = 1 - enabled WHERE id = ?", sid)
        await drok.mcp.reload()
        return back(request, "/settings")

    # ---------------- activity + files ----------------
    @web.get("/activity", response_class=HTMLResponse)
    async def activity(request: Request):
        audit = drok.db.all("SELECT * FROM audit ORDER BY id DESC LIMIT 200")
        usage = drok.db.all(
            "SELECT agent_id, model, SUM(prompt_tokens) p, SUM(completion_tokens) c, COUNT(*) n FROM usage "
            "WHERE created_at > ? GROUP BY agent_id, model ORDER BY p + c DESC", now() - 7 * 86400)
        return page(request, "activity.html", audit=audit, usage=usage)

    @web.get("/files/{path:path}")
    async def files(request: Request, path: str = ""):
        root = drok.settings.workspace
        full = (root / path).resolve()
        if full != root and root not in full.parents:
            raise HTTPException(404)
        if full.is_file():
            if request.query_params.get("raw") or not (mimetypes.guess_type(full.name)[0] or "").startswith("text"):
                return FileResponse(full)
            text = full.read_text(errors="replace")[:500_000]
            return page(request, "files.html", path=path, entries=None, text=text)
        if not full.is_dir():
            raise HTTPException(404)
        entries = sorted(full.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        rows = [{"name": e.name, "dir": e.is_dir(), "size": e.stat().st_size if e.is_file() else 0,
                 "mtime": e.stat().st_mtime, "rel": str(e.relative_to(root))}
                for e in entries if not e.name.startswith(".")]
        return page(request, "files.html", path=path, entries=rows, text=None)

    @web.get("/files")
    async def files_root(request: Request):
        return await files(request, "")

    # ---------------- goals ----------------
    @web.get("/goals", response_class=HTMLResponse)
    async def goals_page(request: Request):
        rows = drok.db.all("SELECT * FROM goals ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'blocked' THEN 1 "
                           "WHEN 'paused' THEN 2 ELSE 3 END, updated_at DESC LIMIT 200")
        return page(request, "goals.html", goals=rows, events={g["id"]: drok.goals.events(g["id"], 3) for g in rows})

    @web.get("/goals/{gid}", response_class=HTMLResponse)
    async def goal_page(request: Request, gid: int):
        g = drok.goals.get(gid)
        if not g:
            raise HTTPException(404)
        turns = drok.db.all("SELECT id, status, steps, created_at, result FROM turns WHERE "
                            "json_extract(meta, '$.goal_id') = ? ORDER BY id DESC LIMIT 30", gid)
        return page(request, "goal.html", g=g, events=drok.goals.events(gid, 200), turns=turns)

    @web.post("/goals")
    async def create_goal(agent_id: str = Form(...), title: str = Form(...), objective: str = Form(...),
                          done_when: str = Form(""), max_turns: int = Form(30), check_every_minutes: int = Form(0)):
        try:
            g = drok.goals.create(agent_id, title, objective, done_when, max_turns, check_every_minutes, "owner")
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        drok.touch_user()
        return RedirectResponse(f"/goals/{g['id']}", status_code=303)

    @web.post("/goals/{gid}/{action}")
    async def goal_action(request: Request, gid: int, action: str, turns: int = Form(10)):
        g = drok.goals.get(gid)
        if not g:
            raise HTTPException(404)
        drok.touch_user()
        if action == "pause":
            drok.goals.set_status(gid, "paused", "paused by the owner")
            if drok.db.one("SELECT id FROM turns WHERE status = 'running' AND json_extract(meta, '$.goal_id') = ?", gid):
                drok.runtime.stop(g["agent_id"])
        elif action == "resume":
            drok.goals.set_status(gid, "active", "resumed by the owner")
        elif action == "cancel":
            drok.goals.set_status(gid, "cancelled", "cancelled by the owner")
        elif action == "extend":
            drok.goals.extend(gid, turns)
        return back(request, f"/goals/{gid}")

    # ---------------- self-built tools ----------------
    @web.post("/tools/{name}/{action}")
    async def custom_tool_action(request: Request, name: str, action: str):
        if action == "delete":
            drok.db.run("DELETE FROM custom_tools WHERE name = ?", name)
        elif action == "toggle":
            drok.db.run("UPDATE custom_tools SET enabled = 1 - enabled WHERE name = ?", name)
        return back(request, "/settings#tools")

    # ---------------- Google ----------------
    @web.get("/integrations/google/connect")
    async def google_connect(request: Request):
        if not drok.google.configured:
            return RedirectResponse("/settings?google_error=Set+GOOGLE_CLIENT_ID+and+GOOGLE_CLIENT_SECRET+first#google")
        url, state, verifier = drok.google.auth_url()
        request.session["google_oauth"] = {"state": state, "verifier": verifier}
        return RedirectResponse(url)

    async def _google_finish(request: Request, code: str, state: str) -> RedirectResponse:
        pending = request.session.pop("google_oauth", None)
        if not pending or (state and state != pending["state"]):
            return RedirectResponse("/settings?google_error=That+link+expired+-+press+Connect+again#google",
                                    status_code=303)
        try:
            await drok.google.exchange(code, pending["verifier"])
        except Exception as e:
            return RedirectResponse(f"/settings?google_error={quote(str(e)[:200])}#google", status_code=303)
        drok.bus.publish({"type": "notice", "text": f"Google connected as {drok.google.email()}."})
        return RedirectResponse("/settings#google", status_code=303)

    @web.get("/oauth/google/callback")
    async def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
        if error:
            return RedirectResponse(f"/settings?google_error={quote(error)}#google")
        return await _google_finish(request, code, state)

    @web.post("/integrations/google/paste")
    async def google_paste(request: Request, redirect: str = Form(...)):
        from ..tools.google import Google
        code, state = Google.parse_redirect(redirect)
        return await _google_finish(request, code, state)

    @web.post("/integrations/google/disconnect")
    async def google_disconnect(request: Request):
        await drok.google.disconnect()
        return RedirectResponse("/settings#google", status_code=303)

    # ---------------- live events ----------------
    @web.get("/events")
    async def events(request: Request):
        q = drok.bus.subscribe()

        async def stream():
            try:
                yield "retry: 3000\n\n"
                while True:
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=15)
                    except TimeoutError:
                        yield ": ping\n\n"
                        continue
                    if ev["type"] == "message":
                        m = ev["message"]
                        ev = {"type": "message", "id": m["id"], "conversation_id": m["conversation_id"],
                              "author": m["author"], "kind": m["kind"]}
                    elif ev["type"] == "message_updated":
                        ev = {"type": "message_updated", "id": ev["message"]["id"]}
                    yield f"data: {json.dumps(ev, default=str)}\n\n"
            finally:
                drok.bus.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return web
