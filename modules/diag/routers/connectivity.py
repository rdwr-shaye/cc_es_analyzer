"""Connectivity check endpoints.

Note what these do NOT accept: a host, a port, a URL. Every endpoint takes a
target ID resolved against the fixed table in modules/diag/targets.py. That is
the whole security design — an endpoint taking a free-form address would be a
network scanner running inside a customer's data centre, reachable by anyone
who reaches this app, and its results would map addresses the customer never
agreed to expose.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Query

from config import settings
from modules.diag import probes, targets

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/diag", tags=["diagnostics"])


@router.get("/targets")
def list_targets():
    """What this CC is expected to reach, and why each one matters."""
    return {
        "targets": [
            {
                "id": t.id, "label": t.label, "host": t.host, "port": t.port,
                "purpose": t.purpose, "caveat": t.caveat,
                "critical": t.critical, "sources": list(t.sources),
            }
            for t in targets.TARGETS
        ],
        # Said plainly on screen rather than buried: the probe runs from this
        # container, which is strong evidence about the appliance but is not
        # the same network path as the service that fetches the feed.
        "vantage_point": "the CC Admin container",
    }


@router.get("/proxy")
def proxy_state():
    """The proxy configuration this container would use, per destination.

    Worth its own endpoint because "there is no proxy configured" is itself a
    finding on an appliance in a data centre that requires one — the checks
    would then fail for a reason that has nothing to do with the firewall.
    """
    return {
        "targets": {t.id: probes.proxy_for(t.host) for t in targets.TARGETS},
    }


@router.get("/connectivity")
def check_connectivity(target: str = Query(default=""),
                       timeout: float = Query(default=5.0, ge=1.0, le=20.0)):
    """Probe one target, or every target when none is named.

    Targets are probed in parallel: done serially, a data centre that black-holes
    outbound traffic would make this screen take five timeouts end to end, and
    an engineer would give up before it answered.
    """
    if target:
        chosen = [targets.get(target)]
        if chosen[0] is None:
            return {"error": f"no such target: {target}",
                    "known": targets.all_ids()}
    else:
        chosen = list(targets.TARGETS)

    with ThreadPoolExecutor(max_workers=min(8, len(chosen))) as pool:
        results = list(pool.map(lambda t: probes.run_probe(t, timeout), chosen))

    summary = probes.roll_up(results)
    # WHOSE connectivity this describes. Embedded the answer is "this CC", and
    # the result means what an engineer assumes it means. STANDALONE it is the
    # engineer's own laptop — which can sit on the open internet while the CC
    # they are debugging cannot reach anything at all, and reporting "reachable"
    # then is worse than reporting nothing. The distinction is returned as data
    # so the UI can put it where it cannot be skimmed past.
    embedded = settings.profile.strip().lower() in ("embedded",)
    vantage = {
        "embedded": embedded,
        "label": ("this CC — the container runs on the appliance being reported on"
                  if embedded else
                  "THIS MACHINE, not the CC you are connected to"),
        "warning": ("" if embedded else
                    "CC Admin is running standalone, so these results describe "
                    "the machine it runs on. They say nothing about whether the "
                    "connected CC can reach these services — that box has its "
                    "own DNS, its own routes and its own firewall."),
    }
    for r in results:
        if r["severity"] in (probes.WARN, probes.CRIT):
            logger.info("[diag] %s: %s (%s)", r["id"], r["headline"],
                        r.get("failed_stage"))
    return {
        "results": results,
        "vantage_point": vantage["label"],
        "vantage": vantage,
        "profile": settings.profile,
        **summary,
    }
