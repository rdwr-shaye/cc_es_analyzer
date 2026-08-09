"""Product-level policy endpoint.

Lives in core rather than in a module: what this deployment may do spans every
module, and the frontend reads it once to decide which parts of the nav and
which affordances to render at all.
"""

from fastapi import APIRouter

from core import policy

router = APIRouter(prefix="/api", tags=["policy"])


@router.get("/policy")
def deployment_policy():
    """Which capabilities this instance carries, so the UI can avoid offering
    what it cannot do. This is a convenience for the frontend, NOT the control:
    a disabled capability has no route to call in the first place."""
    return policy.snapshot()
