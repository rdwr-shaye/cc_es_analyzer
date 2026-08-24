"""Login, logout and password change.

These are the only API paths that answer before a caller has authenticated, so
they are also the only ones an unauthenticated caller can probe. They are
written accordingly: no endpoint here reveals whether a username exists, what
the stored hash is, where it lives, or whether the appliance is still on the
password it shipped with — that last one is told only to a caller who has
already logged in and therefore already knows.
"""

from __future__ import annotations

import logging
import threading
import time

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from core import auth, sessions

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = ""
    password: str = ""


class PasswordRequest(BaseModel):
    current: str = ""
    new: str = ""


# ── Rate limiting ────────────────────────────────────────────────────────────
# One shared password on a box reachable from a customer's network is exactly
# the thing worth guessing at, and scrypt only makes each attempt cost ~40ms —
# plenty for one login, nothing for a script. Failures are counted per client
# IP and the lockout is deliberately dumb: a fixed window, no cleverness, no
# per-account state to get wrong.
_FAIL_LIMIT = 5
_FAIL_WINDOW_S = 300
_fails: dict[str, list] = {}
_fail_lock = threading.Lock()


def _record_failure(ip: str) -> None:
    now = time.time()
    with _fail_lock:
        hits = [t for t in _fails.get(ip, []) if now - t < _FAIL_WINDOW_S]
        hits.append(now)
        _fails[ip] = hits


def _locked_out(ip: str) -> int:
    """Seconds remaining before *ip* may try again, or 0."""
    now = time.time()
    with _fail_lock:
        hits = [t for t in _fails.get(ip, []) if now - t < _FAIL_WINDOW_S]
        _fails[ip] = hits
        if len(hits) < _FAIL_LIMIT:
            return 0
        return int(_FAIL_WINDOW_S - (now - hits[0])) + 1


def _clear_failures(ip: str) -> None:
    with _fail_lock:
        _fails.pop(ip, None)


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/state")
def auth_state(request: Request):
    """What the login screen needs before anyone has logged in.

    `using_default` is withheld from unauthenticated callers: telling the world
    that a reachable appliance is still on its shipped password would be
    handing over half the credential.
    """
    sid = getattr(request.state, "sid", "")
    authed = sessions.is_authenticated(sid)
    base = auth.state()
    if not authed:
        base.pop("using_default", None)
        base.pop("change_offered", None)
    return {**base, "authenticated": authed}


@router.post("/login")
def login(req: LoginRequest, request: Request, response: Response):
    sid = getattr(request.state, "sid", "")
    ip = sessions.client_ip(request)

    wait = _locked_out(ip)
    if wait:
        response.status_code = 429
        logger.warning("[auth] %s is locked out for another %ss", ip, wait)
        return {"error": f"Too many failed attempts. Try again in {wait} seconds.",
                "retry_after": wait}

    # Username and password are checked together and reported together. A
    # response that distinguished "no such user" from "wrong password" would
    # confirm the account name for free.
    ok = (req.username or "").strip().lower() == auth.username().lower()
    ok = auth.check_password(req.password or "") and ok
    if not ok:
        _record_failure(ip)
        logger.warning("[auth] failed login from %s", ip)
        response.status_code = 401
        return {"error": "Incorrect username or password."}

    _clear_failures(ip)
    sessions.set_authenticated(sid, True)
    logger.info("[auth] %s logged in from %s", auth.username(), ip)
    return {
        "ok": True,
        "username": auth.username(),
        # Drives the one-time offer to change it. Only ever sent to a caller
        # who has just proved they know the password.
        "using_default": auth.is_default(),
        "change_offered": bool(auth.state().get("change_offered")),
    }


@router.post("/logout")
def logout(request: Request):
    sid = getattr(request.state, "sid", "")
    sessions.set_authenticated(sid, False)
    return {"ok": True}


@router.post("/password")
def change_password(req: PasswordRequest, request: Request, response: Response):
    """Change the appliance password. Requires the current one.

    Requiring `current` even though the caller is already authenticated is not
    ceremony: the session is a cookie on a shared appliance credential, so
    "someone left this tab open" is a realistic way for a stranger to arrive at
    this endpoint, and it must not be enough to lock the real operator out.
    """
    sid = getattr(request.state, "sid", "")
    if not sessions.is_authenticated(sid):
        response.status_code = 401
        return {"error": "Not logged in."}

    if not auth.check_password(req.current or ""):
        logger.warning("[auth] password change refused: current password wrong")
        response.status_code = 403
        return {"error": "Current password is incorrect."}

    new = req.new or ""
    problem = _password_problem(new)
    if problem:
        response.status_code = 400
        return {"error": problem}

    auth.set_password(new)
    return {"ok": True, "using_default": False}


@router.post("/keep-default")
def keep_default(request: Request, response: Response):
    """Record that the offer to change the shipped password was declined."""
    sid = getattr(request.state, "sid", "")
    if not sessions.is_authenticated(sid):
        response.status_code = 401
        return {"error": "Not logged in."}
    auth.mark_default_kept()
    return {"ok": True}


def _password_problem(pw: str) -> str:
    """Why *pw* is unacceptable, or "" if it is fine.

    Length only, and a low bar at that. A shared operational credential that
    engineers have to type during an incident is the wrong place for a
    composition policy — rules of that kind reliably produce one predictable
    password with a digit on the end. Length is the property that actually
    costs an attacker anything.
    """
    if len(pw) < 8:
        return "Password must be at least 8 characters."
    if len(pw) > 200:
        return "Password must be at most 200 characters."
    if pw.strip() != pw:
        return "Password cannot start or end with a space."
    return ""
