"""NetDevices web app."""
import asyncio
import io
import json
import tarfile
import time
from contextlib import asynccontextmanager
from functools import wraps
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket

import html as _html

from . import auth, backups, config, db, scan, sshbridge, status

PAGE_HEADERS = {
    "Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    # Scripts only from our own files: an injected <script> or onclick=... won't run.
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                               "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
                               "form-action 'self'; frame-ancestors 'none'",
}


def page(name: str) -> HTMLResponse:
    # __VERSION__ is stamped into each page (footer text and ?v= on scripts/styles),
    # so a browser can never mix a new page with cached old JavaScript after an update.
    text = (config.STATIC_DIR / name).read_text(encoding="utf-8").replace("__VERSION__", config.VERSION)
    text = text.replace("__TITLE__", _html.escape(page_title()))
    return HTMLResponse(text, headers=PAGE_HEADERS)


def page_title() -> str:
    return (db.get_meta("title") or "").strip() or "Homelab"


def err(msg: str, code: int = 400, **extra) -> JSONResponse:
    return JSONResponse({"error": msg, **extra}, status_code=code)


def authed(request) -> bool:
    return auth.valid(request.cookies.get(auth.COOKIE))


def api(fn):
    """Require a session; for state-changing calls also require the X-ND header
    (a cross-site page can't send custom headers, so this blocks CSRF)."""
    @wraps(fn)
    async def wrapper(request: Request):
        if not authed(request):
            return err("not logged in", 401)
        if request.method not in ("GET", "HEAD") and request.headers.get("x-nd") != "1":
            return err("missing X-ND header", 403)
        return await fn(request)
    return wrapper


async def body(request: Request) -> dict:
    try:
        b = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise ValueError("invalid JSON")
    if not isinstance(b, dict):
        raise ValueError("expected a JSON object")
    return b


# ------------------------------------------------------------------ pages
async def index(request):
    if not auth.is_configured():
        return RedirectResponse("/setup", 303)
    if not authed(request):
        return RedirectResponse("/login", 303)
    return page("index.html")


async def login_page(request):
    if not auth.is_configured():
        return RedirectResponse("/setup", 303)
    return page("login.html")


async def setup_page(request):
    if auth.is_configured():
        return RedirectResponse("/", 303)
    return page("setup.html")


async def term_page(request):
    if not authed(request):
        return RedirectResponse("/login", 303)
    return page("terminal.html")


async def healthz(request):
    return PlainTextResponse("ok")


# ------------------------------------------------------------------ auth api
async def api_setup(request):
    if auth.is_configured():
        return err("already set up", 409)
    if request.headers.get("x-nd") != "1":
        return err("missing X-ND header", 403)
    ip = request.client.host if request.client else "?"
    if (w := auth.throttled(ip)) > 0:
        return err(f"Too many attempts. Wait {int(w) + 1} seconds.", 429)
    try:
        b = await body(request)
    except ValueError as e:
        return err(str(e))
    code = str(b.get("code", "")).strip().upper()
    if not auth.setup_code or code != auth.setup_code:
        auth._failures.setdefault(ip, []).append(time.time())
        return err("That setup code is wrong. It's printed in the container log (setup.sh shows it).", 403)
    pw = str(b.get("password", ""))
    if (m := auth.check_password_rules(pw)):
        return err(m)
    auth.set_password(pw)
    r = JSONResponse({"ok": True})
    r.set_cookie(value=auth.new_session(), **auth.cookie_args())
    return r


async def api_login(request):
    if request.headers.get("x-nd") != "1":
        return err("missing X-ND header", 403)
    ip = request.client.host if request.client else "?"
    if (w := auth.throttled(ip)) > 0:
        return err(f"Too many attempts. Wait {int(w) + 1} seconds.", 429)
    try:
        b = await body(request)
    except ValueError as e:
        return err(str(e))
    await asyncio.sleep(0.3)
    token = await asyncio.to_thread(auth.login, ip, str(b.get("password", "")))
    if not token:
        return err("Wrong password.", 401)
    r = JSONResponse({"ok": True})
    r.set_cookie(value=token, **auth.cookie_args())
    return r


async def api_logout(request):
    auth.end_session(request.cookies.get(auth.COOKIE))
    r = JSONResponse({"ok": True})
    r.delete_cookie(auth.COOKIE, path="/")
    return r


@api
async def api_password(request):
    try:
        b = await body(request)
    except ValueError as e:
        return err(str(e))
    if not await asyncio.to_thread(auth.verify_current, str(b.get("current", ""))):
        return err("Current password is wrong.", 403)
    new = str(b.get("new", ""))
    if (m := auth.check_password_rules(new)):
        return err(m)
    await asyncio.to_thread(auth.set_password, new)
    r = JSONResponse({"ok": True})
    r.set_cookie(value=auth.new_session(), **auth.cookie_args())
    return r


# ------------------------------------------------------------------ data api
@api
async def api_data(request):
    if request.method == "GET":
        return JSONResponse(db.load_all(), headers={"Cache-Control": "no-store"})
    try:
        b = await body(request)
        sites = db.normalize(b)
        rev = b.get("rev")
        rev = int(rev) if rev is not None else None
        new_rev = db.save_all(sites, rev)
    except db.RevisionConflict as e:
        return err("The list was changed in another tab or window. Reloading.", 409, rev=e.rev)
    except (ValueError, TypeError) as e:
        return err(str(e))
    status.trigger()
    return JSONResponse({"ok": True, "rev": new_rev})


@api
async def api_export(request):
    return Response(db.export_json(), media_type="application/json", headers={
        "Content-Disposition": f'attachment; filename="netdevices-{time.strftime("%Y-%m-%d")}.json"'})


@api
async def api_status(request):
    return JSONResponse({"version": config.VERSION, "devices": status.results, "ping_available": status.ping_available,
                         "last_sweep": status.last_sweep, "interval": config.STATUS_INTERVAL,
                         "history": status.summaries(), "cert_warn_days": status.CERT_WARN_DAYS},
                        headers={"Cache-Control": "no-store"})


@api
async def api_status_refresh(request):
    status.trigger()
    return JSONResponse({"ok": True})


@api
async def api_info(request):
    return JSONResponse({"version": config.VERSION, "ssh_keys": sshbridge.key_names(),
                         "web_ssh": sshbridge.asyncssh is not None, "status_interval": config.STATUS_INTERVAL})


@api
async def api_settings(request):
    if request.method == "GET":
        return JSONResponse({"title": page_title(), "types": db.DEVICE_TYPES})
    try:
        b = await body(request)
    except ValueError as e:
        return err(str(e))
    title = str(b.get("title", "")).strip()
    if len(title) > 60:
        return err("Title is too long (60 characters max).")
    db.set_meta("title", title)
    return JSONResponse({"ok": True, "title": page_title()})


@api
async def api_scan(request):
    if request.method == "GET":
        j = scan.job
        return JSONResponse({"job": j, "suggest": scan.suggest_range(), "ports": scan.PORTS}, headers={"Cache-Control": "no-store"})
    try:
        b = await body(request)
    except ValueError as e:
        return err(str(e))
    if b.get("action") == "cancel":
        scan.cancel()
        return JSONResponse({"ok": True})
    try:
        scan.start(str(b.get("cidr", "")))
    except ValueError as e:
        return err(str(e))
    return JSONResponse({"ok": True})


@api
async def api_backups(request):
    if request.method == "POST":
        try:
            name = await asyncio.to_thread(backups.make, True)
        except OSError as e:
            return err(f"Couldn't write the backup: {e}", 500)
        return JSONResponse({"ok": True, "name": name})
    return JSONResponse({"backups": backups.listing(), "folder": str(config.DATA_DIR / "backups")})


@api
async def api_backup_file(request):
    p = backups.path_of(request.path_params["name"])
    if not p:
        return err("no such backup", 404)
    return FileResponse(p, media_type="application/json", filename=p.name)


@api
async def api_forget_hostkey(request):
    try:
        b = await body(request)
    except ValueError as e:
        return err(str(e))
    dev = db.get_device(str(b.get("device_id", "")))
    if not dev:
        return err("unknown device", 404)
    h, p = status.ssh_target(dev)
    return JSONResponse({"ok": True, "removed": db.forget_host_key(f"{h}:{p}")})


@api
async def client_linux(request):
    buf = io.BytesIO()
    src = config.CLIENT_DIR / "linux"
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for p in sorted(src.iterdir()):
            info = t.gettarinfo(str(p), arcname=f"netdevices-putty/{p.name}")
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o755 if p.suffix == ".sh" else 0o644
            with open(p, "rb") as f:
                t.addfile(info, f)
    return Response(buf.getvalue(), media_type="application/gzip",
                    headers={"Content-Disposition": 'attachment; filename="netdevices-putty-linux.tar.gz"'})


# ------------------------------------------------------------------ web ssh
async def ws_ssh(ws: WebSocket):
    origin = ws.headers.get("origin", "")
    host = ws.headers.get("host", "")
    if not auth.valid(ws.cookies.get(auth.COOKIE)) or not origin or urlsplit(origin).netloc != host:
        await ws.close(code=4401)
        return
    dev = db.get_device(ws.query_params.get("d", ""))
    await ws.accept()
    if not dev:
        await ws.send_text(json.dumps({"type": "closed", "reason": "That device no longer exists."}))
        await ws.close()
        return
    await sshbridge.handle(ws, dev)


# ------------------------------------------------------------------ app
@asynccontextmanager
async def lifespan(app):
    db.init()
    auth.init_setup_code()
    status.mark_gap()
    task = asyncio.create_task(status.run_forever())
    btask = asyncio.create_task(backups.run_forever())
    print(f"NetDevices {config.VERSION} - data in {config.DATA_DIR}, status every {config.STATUS_INTERVAL}s, "
          f"web ssh {'on' if sshbridge.asyncssh else 'OFF (asyncssh missing)'}", flush=True)
    yield
    task.cancel()
    btask.cancel()


routes = [
    Route("/", index),
    Route("/login", login_page),
    Route("/setup", setup_page),
    Route("/term", term_page),
    Route("/healthz", healthz),
    Route("/api/setup", api_setup, methods=["POST"]),
    Route("/api/login", api_login, methods=["POST"]),
    Route("/api/logout", api_logout, methods=["POST"]),
    Route("/api/password", api_password, methods=["POST"]),
    Route("/api/data", api_data, methods=["GET", "PUT"]),
    Route("/api/export", api_export),
    Route("/api/status", api_status),
    Route("/api/status/refresh", api_status_refresh, methods=["POST"]),
    Route("/api/info", api_info),
    Route("/api/hostkey/forget", api_forget_hostkey, methods=["POST"]),
    Route("/api/settings", api_settings, methods=["GET", "PUT"]),
    Route("/api/scan", api_scan, methods=["GET", "POST"]),
    Route("/api/backups", api_backups, methods=["GET", "POST"]),
    Route("/api/backups/{name}", api_backup_file),
    Route("/client/netdevices-putty-linux.tar.gz", client_linux),
    WebSocketRoute("/ws/ssh", ws_ssh),
    Mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static"),
]

app = Starlette(routes=routes, lifespan=lifespan)
