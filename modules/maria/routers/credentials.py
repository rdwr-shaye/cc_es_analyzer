"""Letting an operator tell the tool which account reaches THIS CC's MariaDB.

Exists because discovery (modules/maria/credentials.py) cannot promise to be
right on every appliance CC Admin will ever meet — confirmed on a second lab
CC whose account convention differs entirely from the first. When discovery
gets it wrong, the fix has to be something an operator can do from the UI in
the moment, not a redeploy or an environment variable that needs a restart.

Nothing here touches the CC. It only changes which account THIS TOOL uses to
open a connection — there is no route in _MANIPULATIONS for that reason, the
same as /api/connect itself carries none.

Gated by maria.read rather than a capability of its own: it manages how the
module reaches a datastore it can already read, not a new kind of access,
and modules/maria/__init__.py already documents maria.read as governing the
whole module.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from pydantic import BaseModel

from modules.maria import credentials
from modules.maria.client import resolve_host

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/maria", tags=["mariadb"])


def _current_host() -> str:
    host, _why = resolve_host()
    return host


@router.get("/credentials")
def maria_credentials_state():
    """What account is in effect for the connected CC, and where it came
    from — never the password. No CC connected is not an error here; the
    screen simply has nothing to report yet."""
    host = _current_host()
    if not host:
        return {"host": "", "override": {"set": False}}
    override = credentials.override_state(host)
    effective = credentials.resolve(host)
    return {
        "host": host,
        "override": override,
        "effective_user": effective["user"],
        "effective_source": effective["source"],
    }


class CredentialOverride(BaseModel):
    user: str
    password: str


@router.post("/credentials")
def maria_credentials_set(body: CredentialOverride):
    """Save an operator-supplied account for the connected CC. Takes effect
    on the very next connection — modules/maria/credentials.py::set_override
    drops the cache."""
    host = _current_host()
    if not host:
        return {"error": "no CC is connected — connect to one first"}
    user = body.user.strip()
    password = body.password
    if not user or not password:
        return {"error": "both a username and a password are required"}
    credentials.set_override(host, user, password)
    logger.info("[maria] operator set a credential override for %s (user %r)",
                host, user)
    return {"ok": True, "host": host, "user": user}


@router.delete("/credentials")
def maria_credentials_clear():
    """Drop the override for the connected CC, returning to whatever
    discovery finds on its own."""
    host = _current_host()
    if not host:
        return {"error": "no CC is connected"}
    removed = credentials.clear_override(host)
    logger.info("[maria] operator cleared the credential override for %s", host)
    return {"ok": True, "removed": removed, "host": host}
