"""The locked half: the live device probe (Check 3, Tier B).

Its own router file, not a second endpoint bolted onto routers/debug.py —
that is what makes "absent from /openapi.json unless widget.device_probe is
unlocked" a real guarantee rather than an in-handler `if`. See
core/policy.py's registration-gating rule and modules/widgets/__init__.py.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter
from pydantic import BaseModel

from core import policy
from modules.widgets import catalog, client, device_check, device_probe, diagnosis, index_check

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/widgets", tags=["widgets"])


class ProbeRequest(BaseModel):
    time_from_ms: int
    time_to_ms: int
    device_ip: str
    last_update_iso: str | None = None
    device_version: str = ""


@router.post("/{widget_id}/probe")
def probe_widget_device(widget_id: str, req: ProbeRequest):
    """Recomputes the SAME shape /debug returns, with this one device's live
    probe folded into the diagnosis — one assembled report, not two responses
    the frontend has to stitch together, matching the "tiles and drilldown
    render from the same object" rule AGENTS.md documents for System Health.

    Belt and braces: this route only exists at all once widget.device_probe
    is unlocked (route registration in modules/widgets/__init__.py), but the
    explicit check below means a stale reference to this function — a test, a
    future refactor — could never reach the device call either.
    """
    if not policy.enabled("widget.device_probe"):
        return {"error": "widget.device_probe is not enabled"}

    widget = catalog.describe(widget_id)
    if widget is None:
        return {"error": f"no such widget: {widget_id}"}
    if not widget.executor_type:
        return {"error": f"{widget_id} has no executor type — nothing to probe"}

    started = time.time()
    api = client.call_widget_api(widget, req.time_from_ms, req.time_to_ms, [req.device_ip])
    index_results = index_check.check_indices(widget, req.time_from_ms, req.time_to_ms)
    device_results = device_check.polling_status([req.device_ip], widget.executor_type)
    probe_result = device_probe.probe_device(
        req.device_ip, widget.executor_type, req.last_update_iso, req.device_version)

    diag = diagnosis.diagnose(api, index_results, device_results, probe_result)
    return {
        "widget": catalog.as_dict(widget),
        "api": api,
        "index": index_results,
        "device": device_results,
        "probe": probe_result,
        "diagnosis": diag,
        "checked_at": time.time(),
        "took_ms": int((time.time() - started) * 1000),
    }
