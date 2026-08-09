"""
Direct HTTP client for Elasticsearch — bypasses the elasticsearch-py
product-check that rejects older or proxied ES servers.
All calls are plain REST HTTP requests (exactly what `curl` does).

Multi-user: the client is resolved PER BROWSER SESSION. `get_client()` returns
the client bound to the session in the current request context (set by the
session middleware in main.py), so two people can work against two different
CC machines at the same time. Callers with no session — background job threads
(which capture their client up front), the standalone scripts, curl — fall
back to the process-wide default built from .env settings.
"""
import contextvars
import threading

import urllib3
import requests
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth
from config import settings

# Concurrent HTTP connections kept alive per ES host. Sized to the server's
# request threadpool (see main.py) so ~20 simultaneous users don't fall off the
# end of the pool and pay a new TCP+TLS handshake on every call.
POOL_SIZE = 80

# ── Default (no-session) client ──────────────────────────────────────────────
_client: "ESHttpClient | None" = None

# ── Per-session clients ──────────────────────────────────────────────────────
# The middleware sets _current_sid for the duration of each request; FastAPI
# copies the context into the threadpool worker that runs sync endpoints, so
# plain `def` handlers see it too.
_current_sid: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "cc_session_id", default=None)
_session_clients: dict[str, "ESHttpClient"] = {}
_session_lock = threading.Lock()


class ESHttpClient:
    """Thin wrapper around `requests.Session` that speaks the ES REST API."""

    def __init__(self, host: str, port: int, scheme: str = "http",
                 user: str = "", password: str = "", verify_certs: bool = False):
        self.base_url = f"{scheme}://{host}:{port}"
        self.auth     = HTTPBasicAuth(user, password) if user else None
        self.verify   = verify_certs
        self.session  = requests.Session()
        # requests' default pool holds only 10 connections per host; past that
        # it opens and discards sockets on every call.
        adapter = HTTPAdapter(pool_connections=POOL_SIZE, pool_maxsize=POOL_SIZE)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        if not verify_certs:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass

    # ── Low-level HTTP ────────────────────────────────────────────────────────

    def get(self, path: str, params: dict = None) -> dict:
        r = self.session.get(
            f"{self.base_url}{path}",
            auth=self.auth, params=params,
            verify=self.verify, timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: dict = None) -> dict:
        r = self.session.post(
            f"{self.base_url}{path}",
            json=body, auth=self.auth,
            verify=self.verify, timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def put(self, path: str, body: dict = None) -> dict:
        r = self.session.put(
            f"{self.base_url}{path}",
            json=body, auth=self.auth,
            verify=self.verify, timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def delete(self, path: str) -> dict:
        r = self.session.delete(
            f"{self.base_url}{path}",
            auth=self.auth, verify=self.verify, timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def bulk(self, ndjson_text: str, refresh: bool = False) -> dict:
        """POST newline-delimited bulk actions to the _bulk endpoint."""
        path = "/_bulk" + ("?refresh=true" if refresh else "")
        r = self.session.post(
            f"{self.base_url}{path}",
            data=ndjson_text.encode("utf-8"),
            headers={"Content-Type": "application/x-ndjson"},
            auth=self.auth, verify=self.verify, timeout=120,
        )
        r.raise_for_status()
        return r.json()

    # ── Connectivity ──────────────────────────────────────────────────────────

    def ping(self) -> bool:
        try:
            r = self.session.get(
                f"{self.base_url}/",
                auth=self.auth, verify=self.verify, timeout=10,
            )
            return r.status_code == 200
        except Exception:
            return False

    def info(self) -> dict:
        return self.get("/")

    # ── Cluster ───────────────────────────────────────────────────────────────

    def cluster_health(self) -> dict:
        return self.get("/_cluster/health")

    def nodes_stats(self) -> dict:
        return self.get("/_nodes/stats/jvm,os,fs")

    # ── Indices ───────────────────────────────────────────────────────────────

    def cat_indices(self) -> list:
        """Return list of index stat rows (same shape as es.cat.indices)."""
        return self.get("/_cat/indices", params={
            "format": "json",
            "h": "index,health,status,docs.count,store.size,pri,rep",
            "s": "index",
        })

    def index_stats(self, index: str) -> dict:
        return self.get(f"/{index}/_stats")

    def index_mapping(self, index: str) -> dict:
        return self.get(f"/{index}/_mapping")

    # ── Search ────────────────────────────────────────────────────────────────

    def search(self, index: str, body: dict) -> dict:
        # Always request an EXACT total-hit count so every screen's
        # SHOWING / TOTAL widget shows the real number of matching docs.
        # Elasticsearch 7+ caps `hits.total` at 10,000 unless track_total_hits
        # is set — that cap is what made large indices report "… / 10,000".
        # A shallow copy avoids mutating the caller's query body.
        if isinstance(body, dict) and "track_total_hits" not in body:
            body = {**body, "track_total_hits": True}
        return self.post(f"/{index}/_search", body)


# ── Module-level helpers ────────────────────────────────────────────────��─────

def set_session(sid: str | None):
    """Bind the current request context to a browser session. Returns the token
    to reset with (the middleware does this per request)."""
    return _current_sid.set(sid)


def reset_session(token) -> None:
    _current_sid.reset(token)


def current_session_id() -> str | None:
    return _current_sid.get()


def _default_client() -> ESHttpClient:
    global _client
    if _client is None:
        _client = ESHttpClient(
            host=settings.es_host,
            port=settings.es_port,
            scheme=settings.es_scheme,
            user=settings.es_user,
            password=settings.es_password,
            verify_certs=settings.es_verify_certs,
        )
    return _client


def get_client() -> ESHttpClient:
    """The ES client for the caller: the current session's own connection when
    one exists, otherwise the process default from .env."""
    sid = _current_sid.get()
    if sid:
        with _session_lock:
            client = _session_clients.get(sid)
        if client is not None:
            return client
    return _default_client()


def update_client(host: str, port: int, scheme: str = "http",
                  user: str = "", password: str = "",
                  verify_certs: bool = False) -> ESHttpClient:
    """Point the CALLER at a different ES. Inside a request this rebinds only
    that browser session — other users keep the cluster they connected to.
    Without a session (scripts, tests) it replaces the process default."""
    client = ESHttpClient(host, port, scheme, user, password, verify_certs)
    sid = _current_sid.get()
    if sid:
        with _session_lock:
            old = _session_clients.get(sid)
            _session_clients[sid] = client
        if old is not None:
            old.close()
        return client
    global _client
    old, _client = _client, client
    if old is not None:
        old.close()
    return client


def session_target(sid: str) -> str | None:
    """base_url of a session's ES connection, or None if it never connected."""
    with _session_lock:
        client = _session_clients.get(sid)
    return client.base_url if client is not None else None


def drop_session(sid: str) -> None:
    """Release a session's ES connection (called when the session is evicted)."""
    with _session_lock:
        client = _session_clients.pop(sid, None)
    if client is not None:
        client.close()


def ping() -> bool:
    try:
        return get_client().ping()
    except Exception:
        return False
