"""
Direct HTTP client for Elasticsearch — bypasses the elasticsearch-py
product-check that rejects older or proxied ES servers.
All calls are plain REST HTTP requests (exactly what `curl` does).
"""
import urllib3
import requests
from requests.auth import HTTPBasicAuth
from config import settings

# ── Singleton ─────────────────────────────────────────────────────────────────
_client: "ESHttpClient | None" = None


class ESHttpClient:
    """Thin wrapper around `requests.Session` that speaks the ES REST API."""

    def __init__(self, host: str, port: int, scheme: str = "http",
                 user: str = "", password: str = "", verify_certs: bool = False):
        self.base_url = f"{scheme}://{host}:{port}"
        self.auth     = HTTPBasicAuth(user, password) if user else None
        self.verify   = verify_certs
        self.session  = requests.Session()
        if not verify_certs:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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

def get_client() -> ESHttpClient:
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


def update_client(host: str, port: int, scheme: str = "http",
                  user: str = "", password: str = "",
                  verify_certs: bool = False) -> ESHttpClient:
    """Replace the singleton (e.g. when user changes settings in the UI)."""
    global _client
    _client = ESHttpClient(host, port, scheme, user, password, verify_certs)
    return _client


def ping() -> bool:
    try:
        return get_client().ping()
    except Exception:
        return False
