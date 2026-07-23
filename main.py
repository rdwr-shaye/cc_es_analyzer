from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from routers import artificial, exports, health, indices, query
import uvicorn
import logging
import logging.handlers
import os
from config import settings

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
    version="1.0.0",
)

# ── API Routers ───────────────────────────────────────────────────────────────
app.include_router(health.router)
app.include_router(indices.router)
app.include_router(query.router)
app.include_router(exports.router)
app.include_router(artificial.router)

# ── Static files + SPA catch-all ─────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="frontend/static"), name="static")


@app.get("/", include_in_schema=False)
@app.get("/{full_path:path}", include_in_schema=False)
def serve_spa(full_path: str = ""):
    return FileResponse("frontend/index.html")


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
        from services.tls import ensure_cert
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

