"""The time box, as seen by the browser.

Two endpoints. The browser polls the first to run its countdown and to know
when to raise the prompt, and calls the second when someone answers "keep it
open". Deciding `warning` on the SERVER rather than in the browser is
deliberate: every open tab then agrees about when the prompt is due, and a
laptop with a skewed clock cannot sail past the warning window and be
surprised by the shutdown.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from core import lifecycle, sessions

router = APIRouter(prefix="/api/session", tags=["session"])


@router.get("/lifetime")
def lifetime():
    """Remaining time, and whether the prompt is due.

    Left readable without a login on purpose: it carries no information about
    the appliance beyond how long this container has left, and the login screen
    itself needs to be able to say "this window closes in four minutes" rather
    than letting someone type a password into a page that is about to vanish.
    """
    return lifecycle.state()


@router.post("/extend")
def extend(request: Request, response: Response):
    """Grant another window. Requires a login when one is required at all.

    Anyone logged in may extend, and an extension applies to the container
    rather than to the caller — there is one shared credential and one shared
    box, so a per-user window would be a fiction.
    """
    from core import auth
    sid = getattr(request.state, "sid", "")
    if auth.required() and not sessions.is_authenticated(sid):
        response.status_code = 401
        return {"error": "Not logged in."}

    if not lifecycle.enabled():
        return {"enabled": False, "extended": False,
                "error": "this deployment has no time box"}

    result = lifecycle.extend()
    if result.get("extended"):
        # The other tabs are told, so a colleague watching the same CC sees the
        # countdown jump rather than wondering whether their own click worked.
        sessions.broadcast("extended this session", "", exclude_sid=sid)
    return result
