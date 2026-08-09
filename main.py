from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import Response
import json
import re
import uvicorn
import logging
import logging.handlers
import os
from config import settings
import modules
from core import policy, sessions, updater
from core.routers import policy as policy_router, presence, update
from modules.es.client import POOL_SIZE, reset_session, set_session

# ── Logging setup ─────────────────────────────────────────────────────────────
LOG_DIR  = os.path.join(os.path.dirname(__file__), "logs")
LOG_FILE = os.path.join(LOG_DIR, "cc_es_analyzer.log")
os.makedirs(LOG_DIR, exist_ok=True)

_fmt     = logging.Formatter(
    fmt="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Console handler
_console = logging.StreamHandler()
_console.setFormatter(_fmt)

# Rotating file handler — 5 MB per file, keep last 5 files
_file_h  = logging.handlers.RotatingFileHandler(
    LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_file_h.setFormatter(_fmt)

logging.basicConfig(level=logging.INFO, handlers=[_console, _file_h])
logger = logging.getLogger("cc_es_analyzer")
logger.info("Log file: %s", LOG_FILE)

app = FastAPI(
    title="CC Elasticsearch Analyzer",
    description="Analyze CyberController Elasticsearch data",
    # Single source of truth: the VERSION file at the repo root. Bumping it is
    # what tells deployed instances that a new version exists.
    version=updater.local_version(),
)

# ── Concurrency ───────────────────────────────────────────────────────────────
# Almost every endpoint is a sync `def`, so FastAPI runs it in AnyIO's
# threadpool — 40 threads by default, which ~20 users (each firing several
# parallel XHRs per screen) can exhaust. Raise it, and keep the ES connection
# pool (services/es_client.POOL_SIZE) the same size so requests don't queue in
# one place only to churn TCP connections in the other.
REQUEST_THREADS = POOL_SIZE


@app.on_event("startup")
async def _widen_threadpool() -> None:
    import anyio.to_thread
    anyio.to_thread.current_default_thread_limiter().total_tokens = REQUEST_THREADS
    logger.info("Request threadpool: %s threads · ES connection pool: %s",
                REQUEST_THREADS, POOL_SIZE)
    logger.info("CC ES Analyzer %s (update mode: %s)",
                updater.local_version(), updater.mode())
    # Look for a newer version off the request path, now and every few hours.
    # An appliance follows the CC release train, so the poll would only spend
    # itself failing to reach git from inside a customer network.
    if policy.enabled("app.self_update"):
        updater.start_background_checks()


# ── Session / presence middleware ─────────────────────────────────────────────
# Requests that CHANGE data on the connected CC. Matched on method + path so a
# new UI path can never bypass the peer notification.
_MANIPULATIONS: list[tuple[str, re.Pattern, str]] = [
    ("POST",   re.compile(r"^/api/indices/create$"),                "created an index"),
    ("DELETE", re.compile(r"^/api/indices/([^/]+)$"),               "DELETED index"),
    ("POST",   re.compile(r"^/api/indices/([^/]+)/duplicate$"),     "duplicated index"),
    ("POST",   re.compile(r"^/api/indices/([^/]+)/import$"),        "imported CSV into"),
    ("POST",   re.compile(r"^/api/doc/update$"),                    "edited a document"),
    ("POST",   re.compile(r"^/api/docs/bulk-delete$"),              "DELETED documents"),
    ("POST",   re.compile(r"^/api/docs/bulk-field$"),               "bulk-edited a field"),
    ("POST",   re.compile(r"^/api/docs/bulk-update$"),              "modified query results"),
    ("POST",   re.compile(r"^/api/artificial$"),                    "generated artificial data"),
    ("POST",   re.compile(r"^/api/exports/restore$"),               "restored an archive"),
]


def _manipulation(method: str, path: str):
    """(action, target) when this request changes CC data, else None."""
    for verb, pattern, action in _MANIPULATIONS:
        if method != verb:
            continue
        m = pattern.match(path)
        if m:
            return action, (m.group(1) if m.groups() else "")
    return None


@app.middleware("http")
async def session_middleware(request: Request, call_next):
    """Identify the browser, bind its own ES connection for the duration of the
    request, and tell the other users on that CC when it changes data."""
    sid_in = request.cookies.get(sessions.COOKIE_NAME)
    sid = sessions.touch(sid_in, sessions.client_ip(request),
                         request.headers.get("user-agent", ""))
    request.state.sid = sid
    token = set_session(sid)
    try:
        hit = _manipulation(request.method, request.url.path)
        response = await call_next(request)

        if hit is not None:
            action, target = hit
            # These endpoints all answer with a small JSON body and report
            # failures as {"error": ...} at HTTP 200 — read it so we only warn
            # the others about changes that actually happened.
            body = b""
            async for chunk in response.body_iterator:
                body += chunk
            failed = response.status_code >= 400
            if not failed:
                try:
                    failed = "error" in json.loads(body or b"{}")
                except Exception:
                    failed = False
            if not failed:
                sessions.notify_peers(sid, action, target)
            response = Response(content=body, status_code=response.status_code,
                                headers=dict(response.headers),
                                media_type=response.media_type)
    finally:
        reset_session(token)

    if sid_in != sid:
        response.set_cookie(sessions.COOKIE_NAME, sid, httponly=True,
                            samesite="lax", path="/", max_age=30 * 86400,
                            secure=request.url.scheme == "https")
    return response


# ── API Routers ───────────────────────────────────────────────────────────────
# Assembled from whatever modules are enabled — main.py deliberately names no
# datastore, so adding PostgreSQL is a new package under modules/ and nothing
# here changes.
#
# Gating happens by REGISTRATION, not by a runtime check: in a profile without
# the capability the path does not exist at all — no OpenAPI entry, 404 to a
# direct call — so there is nothing to bypass. See core/policy.py.
app.include_router(policy_router.router)
app.include_router(presence.router)

for module in modules.discover():
    for router, gating_capability in module.routers:
        if gating_capability is None or policy.enabled(gating_capability):
            app.include_router(router)

if policy.enabled("app.self_update"):
    app.include_router(update.router)

policy.log_startup()

# ── Unknown API paths ────────────────────────────────────────────────────────
# Registered after every router and before the SPA catch-all: without it, a
# path whose capability is switched off would fall through to the catch-all
# below and answer 200 with the SPA's HTML, which reads as "it worked" to a
# caller and as an odd finding to a reviewer. A gated endpoint must 404, and
# so should any mistyped API path.
@app.api_route("/api/{full_path:path}", include_in_schema=False,
               methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
def api_not_found(full_path: str):
    return Response(
        content=json.dumps({"error": f"no such endpoint: /api/{full_path}"}),
        status_code=404, media_type="application/json")


# ── Static files + SPA catch-all ─────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="frontend/static"), name="static")

_SPA_HTML = os.path.join(os.path.dirname(__file__), "frontend", "index.html")
_SPA_ASSETS = [
    os.path.join(os.path.dirname(__file__), "frontend", "static", "js", "app.js"),
    os.path.join(os.path.dirname(__file__), "frontend", "static", "css", "style.css"),
]
_spa_cache: dict = {}     # {"key": <stamp>, "html": <rendered>}


def _asset_version() -> str:
    """Short stamp that changes whenever the app version or an asset changes."""
    parts = [updater.local_version()]
    for path in _SPA_ASSETS:
        try:
            parts.append(str(int(os.path.getmtime(path))))
        except OSError:
            parts.append("0")
    import hashlib
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:10]


def _spa_page() -> str:
    """index.html with the asset version stamped in, cached until it changes.

    Without this, browsers keep serving the previous app.js from cache after an
    update — the page looks updated (new HTML) but runs old JavaScript, which
    presents as "the new features aren't there" until a hard reload.
    """
    stamp = _asset_version()
    try:
        stamp += "-" + str(int(os.path.getmtime(_SPA_HTML)))
    except OSError:
        pass
    if _spa_cache.get("key") != stamp:
        with open(_SPA_HTML, "r", encoding="utf-8") as fh:
            _spa_cache["html"] = fh.read().replace("__ASSET_V__", stamp)
        _spa_cache["key"] = stamp
    return _spa_cache["html"]


@app.get("/", include_in_schema=False)
@app.get("/{full_path:path}", include_in_schema=False)
def serve_spa(full_path: str = ""):
    # The HTML itself must never be cached — it is what carries the new asset
    # URLs. The assets under /static are immutable per stamp, so they cache.
    return Response(content=_spa_page(), media_type="text/html",
                    headers={"Cache-Control": "no-cache, must-revalidate"})


def _port_in_use(host: str, port: int) -> bool:
    """Return True if something is already listening on host:port.

    Guards against a stale/orphaned server instance silently double-binding the
    port (on Windows two processes can bind the same port without an error,
    which makes the browser hang as connections get raced between them).
    """
    import socket
    # 0.0.0.0 is a bind address, not connectable — probe the loopback instead.
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "", "::") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex((probe_host, port)) == 0


if __name__ == "__main__":
    if _port_in_use(settings.service_host, settings.service_port):
        logger.error(
            "Port %s is already in use — another CC ES Analyzer instance is "
            "probably still running. Stop it first (Ctrl+C in its terminal, or "
            "kill the process listening on port %s) and try again.",
            settings.service_port, settings.service_port,
        )
        raise SystemExit(1)

    # Hot-reload is great for development but spawns a file-watcher subprocess,
    # which is undesirable for a hidden auto-start run. Pass --no-reload (used by
    # the Windows scheduled task) to run a single, stable process.
    import sys
    reload = "--no-reload" not in sys.argv

    ssl_kwargs = {}
    if settings.service_ssl:
        from core.tls import ensure_cert
        cert, key = ensure_cert(settings.ssl_certfile, settings.ssl_keyfile)
        ssl_kwargs = {"ssl_certfile": cert, "ssl_keyfile": key}
        logger.info("Serving HTTPS on https://%s:%s (cert=%s)",
                    settings.service_host, settings.service_port, cert)

    uvicorn.run(
        "main:app",
        host=settings.service_host,
        port=settings.service_port,
        reload=reload,
        **ssl_kwargs,
    )

