"""The free half: Check 1 (API replay), Check 2 (index), Check 3 Tier A
(polling status) — everything widget.debug covers. No credential, no call to
a customer device; see routers/probe.py for the separately-gated live probe.
"""

from __future__ import annotations

import contextvars
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter
from pydantic import BaseModel

from modules.widgets import catalog, client, device_check, diagnosis, index_check

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/widgets", tags=["widgets"])


def _in_context(fn):
    """Same discipline as modules/system/routers/dashboard.py::_in_context and
    modules/diag/routers/connectivity.py's own copy: the ES and MariaDB
    clients this module reaches through are resolved off a ContextVar
    session_middleware sets on THIS request's thread, which a bare worker
    thread does not inherit. One copy per task, taken on this thread before
    any of them reach the pool — contextvars.Context.run() is not safe to
    call concurrently on the SAME Context from more than one thread."""
    ctx = contextvars.copy_context()
    return lambda: ctx.run(fn)


@router.get("/catalog")
def widget_catalog():
    """Every known widget, grouped for the picker's <optgroup>s."""
    return {
        "domains": [
            {"domain": domain, "widgets": [catalog.as_dict(w) for w in widgets]}
            for domain, widgets in catalog.by_domain().items()
        ],
    }


@router.get("/devices")
def widget_devices():
    """Device IPs for the scope picker — vision_ng.device_access.mgt_ip only,
    never a credential column. See modules/widgets/device_check.py."""
    return {"devices": device_check.list_devices()}


# ── VRM login (Check 1's real auth) ──────────────────────────────────────────
# A SYS_ADMIN session on the CC, not a credential this tool owns — see the
# module docstring in client.py for why this is an explicit login/logout
# pair rather than a connect-time field. Gated by widget.debug like the rest
# of this router: meaningless without it.

class VrmLoginRequest(BaseModel):
    username: str
    password: str


@router.post("/vrm/login")
def vrm_login(req: VrmLoginRequest):
    result = client.login(req.username, req.password)
    if not result.get("ok"):
        return {"error": result.get("error") or "login failed"}
    from modules.es.client import get_client
    get_client().vrm_session = {
        "jsessionid": result["jsessionid"],
        "visionsessionid": result.get("visionsessionid"),
        "username": req.username,
        "logged_in_at": time.time(),
    }
    logger.info("[widgets] VRM login established for %r", req.username)
    return {"status": "ok", "username": req.username}


@router.post("/vrm/logout")
def vrm_logout():
    from modules.es.client import get_client
    c = get_client()
    session = getattr(c, "vrm_session", None)
    if session:
        client.logout(session["jsessionid"])
        c.vrm_session = None
    return {"status": "ok"}


@router.get("/vrm/status")
def vrm_status():
    from modules.es.client import get_client
    session = getattr(get_client(), "vrm_session", None)
    return {"logged_in": bool(session),
            "username": (session or {}).get("username")}


class DebugRequest(BaseModel):
    time_from_ms: int
    time_to_ms: int
    device_ips: list[str] = []


@router.post("/{widget_id}/debug")
def debug_widget(widget_id: str, req: DebugRequest):
    """The three-check report for one widget. The three checks are
    independent and two of them cost a round trip (VRM, MariaDB), so they run
    in parallel — same reasoning as System Health's /summary."""
    widget = catalog.describe(widget_id)
    if widget is None:
        return {"error": f"no such widget: {widget_id}",
                "known": sorted(catalog.WIDGET_CATALOG)}

    started = time.time()
    with ThreadPoolExecutor(max_workers=3) as pool:
        api_future = pool.submit(_in_context(lambda: client.call_widget_api(
            widget, req.time_from_ms, req.time_to_ms, req.device_ips)))
        index_future = pool.submit(_in_context(lambda: index_check.check_indices(
            widget, req.time_from_ms, req.time_to_ms)))
        device_future = pool.submit(_in_context(lambda: device_check.polling_status(
            req.device_ips, widget.executor_type) if widget.executor_type else []))

        try:
            api = api_future.result()
        except Exception as exc:                                # noqa: BLE001
            logger.exception("[widgets] API check failed for %s", widget_id)
            api = {"status": "ERROR", "detail": str(exc), "took_ms": 0}
        try:
            index_results = index_future.result()
        except Exception as exc:                                # noqa: BLE001
            logger.exception("[widgets] index check failed for %s", widget_id)
            index_results = [{"prefix": p, "status": "UNKNOWN", "doc_count": 0,
                             "indices": [], "detail": str(exc)}
                            for p in widget.index_prefixes] or [
                {"prefix": None, "status": "UNKNOWN", "doc_count": 0,
                 "indices": [], "detail": str(exc)}]
        try:
            device_results = device_future.result()
        except Exception as exc:                                # noqa: BLE001
            logger.exception("[widgets] device check failed for %s", widget_id)
            device_results = [{"device_ip": ip, "status": "UNKNOWN",
                              "task_status": None, "task_info": str(exc)}
                             for ip in req.device_ips]

    diag = diagnosis.diagnose(api, index_results, device_results)
    return {
        "widget": catalog.as_dict(widget),
        "api": api,
        "index": index_results,
        "device": device_results,
        "diagnosis": diag,
        "checked_at": time.time(),
        "took_ms": int((time.time() - started) * 1000),
    }
