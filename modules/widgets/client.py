"""Check 1 — replay a widget's own backend REST call.

Every widget in the catalog is served by a controller under kvision_vrm's
`/mgmt/vrm` path space — the same REST API the CC's own browser UI calls when
that widget renders. So the most direct way to answer "does the widget's API
actually return data" is to send it the same request and look at what comes
back, rather than re-deriving the answer from Elasticsearch ourselves (that is
Check 2's job, and a second, independent measurement, not a restatement).

WHERE THIS GOES. `resolve_mgmt_root()` follows the exact shape of
modules/maria/client.py::resolve_host(): a `VRM_BASE_URL` env override first,
else the connected CC's own `cc_host` over HTTPS. No SSH-tunnel fallback, on
purpose — port 443 is the one port a standalone engineer is guaranteed to
reach, because it is how they reach the CC's own UI to begin with.

AUTHENTICATION — confirmed live, not guessed. `/mgmt/vrm/*` answers 401/403
to an anonymous request (confirmed against a real CC, 2026-09). It is NOT
HTTP Basic auth (an earlier pass here assumed that and was wrong). Captured
directly from the CC's own login flow via Postman:

    POST {mgmt_root}/system/user/login   {"username": ..., "password": ...}
      -> {"status": "ok", "jsessionid": "...", "visionsessionid": "..."}

and every subsequent call — VRM included — carries that value as a plain
cookie: `Cookie: JSESSIONID=<jsessionid>`. So this module needs an actual
login step, not a per-request credential: `login()` performs it once,
`routers/debug.py`'s `/vrm/login` endpoint stores the resulting session on
the browser session's `ESHttpClient` (`client.vrm_session`, alongside the
existing `.ssh` — same one-place-secrets-live rule, same
never-written-to-disk, never-logged discipline), and `call_widget_api()`
sends the cookie on every call from there. The password itself is used once,
for the login call, and is never retained afterward — only the resulting
session token is held.

A SYS_ADMIN login is a real, scoped credential (a person's operator account,
not a service account this tool owns), so it is never assumed still valid:
`call_widget_api()` treats a 401/403 while a session was believed active as
that session having EXPIRED, clears it, and reports that distinctly from
"never logged in" — the UI's job is to ask again in either case, not to
retry silently with a stored password.

SHUTDOWN. `logout_all_sessions()` (called from main.py's shutdown event) logs
out every live VRM session this process is holding — a login left open on the
CC after this tool exits is a stray, forgotten session on someone's account.
"""

from __future__ import annotations

import logging
import os
import time

import requests
import urllib3

from config import settings

logger = logging.getLogger(__name__)

PASS, EMPTY, ERROR = "PASS", "EMPTY", "ERROR"


class VrmError(Exception):
    """Could not even attempt the call — no CC connected, no base URL."""


def _verify_certs() -> bool:
    verify = settings.vrm_verify_certs
    if not verify:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return verify


def resolve_mgmt_root() -> tuple[str, str]:
    """(root_url, why) for this session's `/mgmt` root — the login endpoint
    and every `/mgmt/vrm/...` call both hang off this same root. ("", why)
    when there is nothing to point at yet."""
    explicit = os.environ.get("VRM_BASE_URL", "").strip() or settings.vrm_base_url
    if explicit:
        # VRM_BASE_URL is documented as the full override including /mgmt/vrm
        # (the escape hatch for an install where the default assumption is
        # wrong) — strip a trailing /vrm so login and vrm calls share one root.
        root = explicit.rstrip("/")
        if root.endswith("/vrm"):
            root = root[: -len("/vrm")]
        return root, "VRM_BASE_URL"

    from modules.es.client import get_client
    host = (getattr(get_client(), "cc_host", "") or "").strip()
    if not host or host in ("localhost", "127.0.0.1"):
        return "", "no CC connected"
    port = f":{settings.vrm_port}" if settings.vrm_port else ""
    return f"{settings.vrm_scheme}://{host}{port}/mgmt", "the connected CC"


def resolve_base_url() -> tuple[str, str]:
    """(base_url, why) for this session's `/mgmt/vrm` root specifically."""
    root, why = resolve_mgmt_root()
    return (f"{root}/vrm" if root else ""), why


def login(username: str, password: str) -> dict:
    """POST {mgmt_root}/system/user/login. Never raises.

    Returns {"ok": True, "jsessionid", "visionsessionid"} or
    {"ok": False, "error"}. The password is used only for this one call —
    the caller (routers/debug.py) stores the returned session, never this.
    """
    root, why = resolve_mgmt_root()
    if not root:
        return {"ok": False, "error": why}
    try:
        r = requests.post(f"{root}/system/user/login",
                          json={"username": username, "password": password},
                          verify=_verify_certs(), timeout=settings.vrm_timeout_s)
    except Exception as exc:                                   # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    if r.status_code != 200:
        return {"ok": False, "error": f"HTTP {r.status_code}"}
    try:
        payload = r.json()
    except ValueError:
        return {"ok": False, "error": "login response was not JSON"}

    jsessionid = payload.get("jsessionid")
    if payload.get("status") != "ok" or not jsessionid:
        return {"ok": False, "error": payload.get("status") or "login refused"}
    return {"ok": True, "jsessionid": jsessionid,
            "visionsessionid": payload.get("visionsessionid")}


def logout(jsessionid: str) -> None:
    """Best-effort — never raises, and the caller never needs to check the
    result. The logout PATH itself is an inference (mirrors login's shape;
    not independently captured the way login was), so a failure here is
    logged and otherwise ignored rather than surfaced as an error anywhere —
    worst case a session outlives this process by its own natural timeout,
    which is not a new risk this tool introduces."""
    root, _why = resolve_mgmt_root()
    if not root:
        return
    try:
        requests.post(f"{root}/system/user/logout",
                      cookies={"JSESSIONID": jsessionid},
                      verify=_verify_certs(), timeout=5)
    except Exception as exc:                                   # noqa: BLE001
        logger.info("[widgets] VRM logout failed (best-effort): %s", exc)


def logout_all_sessions() -> None:
    """Log out every VRM session any browser session's client is holding.
    Called once, from main.py's shutdown event — see the module docstring."""
    from modules.es.client import all_session_clients
    for c in all_session_clients():
        session = getattr(c, "vrm_session", None)
        if not session:
            continue
        try:
            logout(session["jsessionid"])
        except Exception:                                       # noqa: BLE001
            logger.warning("[widgets] logout at shutdown failed", exc_info=True)
        c.vrm_session = None


def _sample(payload, limit: int = 5):
    """A truncated, JSON-safe slice of a response body — the point of Check 1
    is not just PASS/EMPTY/ERROR, it is letting an engineer SEE what the
    widget's own API actually said. Shown even on ERROR: a 400/500 from this
    CC's own controller very often carries a message naming exactly what was
    wrong with the request (this is how the DP Traffic / WebDDoS request-
    shape gap below Check 1's docstring was found in the first place — the
    error body is evidence, not noise to discard).

    Less need to be defensive about exposure here than device_probe.py's own
    truncation: this is the SAME data the widget's own dashboard already
    shows the logged-in user, not a device's raw payload reaching a new
    audience — so this can afford to be more generous, capped only to keep
    the report readable and the response size sane.
    """
    if isinstance(payload, list):
        return payload[:limit]
    if isinstance(payload, dict):
        return payload
    return payload


def classify_response(http_status: int | None, payload,
                      exc: Exception | None) -> tuple[str, int | None, str, object]:
    """(status, row_count, detail, sample) — pure, no network, unit-testable.

    PASS = a 2xx response carrying a non-empty `data` array (the confirmed
    `MetadataResponse` shape: `{"metaData": {...}, "data": [...]}`). EMPTY =
    2xx with `data` present but empty — a real answer, just nothing in range.
    ERROR covers everything else, connection failures included, because a
    widget that could not even be asked is not "empty", it is unknown.
    """
    if exc is not None:
        return ERROR, None, f"{type(exc).__name__}: {exc}", None
    if http_status is None or not (200 <= http_status < 300):
        return ERROR, None, f"HTTP {http_status}", _sample(payload)
    if not isinstance(payload, dict):
        return ERROR, None, "response was not a JSON object", _sample(payload)
    data = payload.get("data")
    if data is None:
        return ERROR, None, "response carried no 'data' field", _sample(payload)
    count = len(data) if isinstance(data, list) else 1
    return (PASS if count else EMPTY), count, f"HTTP {http_status}", _sample(data)


def call_widget_api(widget, time_from_ms: int, time_to_ms: int,
                    device_ips: list[str] | None = None) -> dict:
    """POST widget.api_path against this session's CC. Never raises — every
    failure mode (no CC connected, connection refused, timeout, non-2xx,
    malformed body, no/expired VRM session) comes back through
    classify_response() plus the auth_required/session_expired flags below,
    so a caller building a report never needs a second error path.
    """
    started = time.time()

    def _done(status, http_status, count, detail, sample=None, **extra):
        return {"status": status, "http_status": http_status, "row_count": count,
                "detail": detail, "sample": sample,
                "took_ms": int((time.time() - started) * 1000), **extra}

    base_url, why = resolve_base_url()
    if not base_url:
        status, count, detail, sample = classify_response(None, None, VrmError(why))
        return _done(status, None, count, detail, sample)

    from modules.es.client import get_client
    client = get_client()
    session = getattr(client, "vrm_session", None)
    cookies = {"JSESSIONID": session["jsessionid"]} if session else None

    body = {
        "timeInterval": {"from": time_from_ms, "to": time_to_ms},
        "selectedDevices": [
            {"deviceId": ip, "networkPolicies": [], "ports": []}
            for ip in (device_ips or [])
        ],
        "include": "ACTIVE_ONLY",
    }

    http_status, payload, exc = None, None, None
    try:
        r = requests.post(f"{base_url}{widget.api_path}", json=body,
                          cookies=cookies, verify=_verify_certs(),
                          timeout=settings.vrm_timeout_s)
        http_status = r.status_code
        try:
            payload = r.json()
        except ValueError:
            payload = None
    except Exception as e:                                     # noqa: BLE001
        exc = e

    if http_status in (401, 403):
        if session:
            # Believed logged in, refused anyway — the session died under us
            # (its own idle timeout, or an admin action). Clear it so the
            # NEXT call does not present it as still valid, and tell the UI
            # to ask again rather than to just show a bare error.
            client.vrm_session = None
            status, count, detail, sample = classify_response(http_status, payload, exc)
            return _done(status, http_status, count, detail, sample,
                        session_expired=True)
        status, count, detail, sample = classify_response(http_status, payload, exc)
        return _done(status, http_status, count, detail, sample, auth_required=True)

    status, count, detail, sample = classify_response(http_status, payload, exc)
    return _done(status, http_status, count, detail, sample)
