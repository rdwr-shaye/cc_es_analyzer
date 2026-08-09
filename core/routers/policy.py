"""Product-level policy endpoint.

Lives in core rather than in a module: what this deployment may do spans every
module, and the frontend reads it once to decide which parts of the nav and
which affordances to render at all.
"""

from fastapi import APIRouter

from core import policy, updater

router = APIRouter(prefix="/api", tags=["policy"])


@router.get("/policy")
def deployment_policy():
    """Which capabilities this instance carries, so the UI can avoid offering
    what it cannot do. This is a convenience for the frontend, NOT the control:
    a disabled capability has no route to call in the first place."""
    snap = policy.snapshot()
    # The installed version travels with the policy, not with the updater.
    # Embedded, there IS no updater — the tool ships and upgrades with the CC —
    # but that is precisely where knowing which build is running matters most,
    # because it has to be matched against the CC release it arrived with.
    snap["version"] = updater.local_version()
    return snap
