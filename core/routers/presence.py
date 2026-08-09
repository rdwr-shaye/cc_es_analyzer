"""
Who else is on my CC right now, and what did they just change?

The UI polls /api/presence a few times a minute: it draws the co-user badge
from `peers`, drains queued `notifications` into toasts, and calls /peers
before any destructive action so the confirmation can name the people who
would be affected.
"""
from fastapi import APIRouter, Request
from pydantic import BaseModel

from core import sessions
from modules.es.client import session_target

router = APIRouter(prefix="/api/presence", tags=["presence"])


@router.get("")
def presence(request: Request):
    """Current session, everyone else on the same ES target, and any pending
    notifications (draining them — each is delivered once)."""
    sid = request.state.sid
    snap = sessions.snapshot(sid)
    return {**snap, "notifications": sessions.drain(sid),
            "es_target": session_target(sid)}


@router.get("/peers")
def peers(request: Request):
    """Just the co-users — polled right before a destructive action so the
    warning reflects who is on the CC at that moment."""
    sid = request.state.sid
    return {"peers": sessions.peers_on_target(session_target(sid), sid),
            "es_target": session_target(sid)}


class NameRequest(BaseModel):
    name: str = ""


@router.post("/name")
def set_name(req: NameRequest, request: Request):
    """Set the display name others see instead of this session's IP."""
    sessions.set_name(request.state.sid, req.name)
    return {"ok": True, **sessions.snapshot(request.state.sid)}
