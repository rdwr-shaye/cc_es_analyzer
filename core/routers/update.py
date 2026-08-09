"""
Version banner and one-click update.

The UI polls /api/update/status (cheap — it reads a cached result) and shows a
badge when the deployment is behind the repository. Pressing Update calls
/api/update/apply, which either asks the host agent to pull+rebuild or does the
fast-forward itself, depending on how this instance is deployed. See
core/updater.py for the three modes.

Because an update restarts the app for EVERYONE, the other connected users are
told before it happens.
"""
import logging

from fastapi import APIRouter, Request

from core import sessions, updater

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/update", tags=["update"])


@router.get("/status")
def update_status():
    """Cached view: current version, latest known version, what changed."""
    return updater.status()


@router.post("/check")
def update_check():
    """Force a fresh check now (the 'Check again' button)."""
    return updater.check()


@router.post("/apply")
def update_apply(request: Request):
    """Pull the new version and restart. Everyone else gets a heads-up first."""
    sid = request.state.sid
    who = sessions.describe_session(sid)
    st = updater.status(max_age_s=60)
    result = updater.apply(requested_by=who)
    if "error" in result:
        return result
    target = (st.get("latest") or {}).get("version") or "the latest version"
    sessions.broadcast("is updating CC ES Analyzer", f"to {target} — the app will "
                       "restart in a moment", exclude_sid=sid)
    return result


@router.get("/job")
def update_job():
    """Progress of the update that is running (or the last one)."""
    return updater.job()
