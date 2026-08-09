"""Deployment profile and capability policy.

CC Admin has TWO SUPPORTED DEPLOYMENT MODES, and both are products — neither
is a stepping stone to the other:

  * ``standalone`` — the original remote tool. An engineer runs it on their own
    machine and connects it to a CC over the network. This is how support
    reaches CyberControllers that do not carry the embedded build, which
    includes every CC already in the field. It is not going away.
  * ``embedded`` — the copy that rides a modern CC's monitoring compose and
    talks to the datastores running beside it.

One image serves both, so what an instance may do cannot be decided by which
build it is. It is decided at startup from two inputs:

  1. the PROFILE, from ANALYZER_PROFILE — the compose file is what pins an
     appliance to ``embedded``; anything else defaults to ``standalone``;
  2. an optional PROPERTY FILE on the system filesystem, following the
     product's existing convention. Its presence unlocks the capabilities a
     customer instance does not carry by default. Only personnel who know it
     is there can create one, and it never travels in the image.

The important part is *how* a disabled capability is disabled: main.py never
registers its routes, so it is absent from the OpenAPI schema and answers 404
to a direct call. A hidden menu item is not a control — a route that does not
exist is. That distinction is what this module exists to make possible, and it
is what a product security review will actually be checking.

Adding a capability later (SQL browsing, log collection, guided remediation)
means one Module declaring its own capabilities and routers — the shape
that keeps the later phases of the roadmap cheap.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from config import settings

logger = logging.getLogger(__name__)

# ── Profiles ─────────────────────────────────────────────────────────────────
STANDALONE = "standalone"
EMBEDDED = "embedded"
PROFILES = (STANDALONE, EMBEDDED)

# "lab" was this profile's name before it was clear that the remote tool is a
# shipping product in its own right rather than developer scaffolding. Accepted
# so an existing ANALYZER_PROFILE=lab keeps working instead of silently
# falling back; drop it once nothing sets it.
_PROFILE_ALIASES = {"lab": STANDALONE}

_BOTH = (STANDALONE, EMBEDDED)
_STANDALONE_ONLY = (STANDALONE,)


@dataclass(frozen=True)
class Capability:
    """One switchable feature of the app."""

    id: str
    title: str
    # Profiles that carry this capability with nothing further required.
    profiles: tuple[str, ...]
    # Whether the property file may switch it on in a profile that does not
    # carry it. Some capabilities are never unlockable on an appliance — the
    # self-updater cannot reach git from a customer network, so offering it
    # would only produce confusing failures.
    unlockable: bool = False
    note: str = ""


@dataclass(frozen=True)
class Module:
    """One feature area of CC Admin — a datastore, or a cross-cutting concern
    such as log collection or the knowledge base.

    A module owns its capabilities and its routers, so adding PostgreSQL means
    one new package plus one entry in modules/__init__.py: neither main.py nor
    this file has to learn about it. `routers` pairs each router with the
    capability that gates it (None = always registered)."""

    id: str
    title: str
    capabilities: tuple[Capability, ...] = ()
    routers: tuple = ()


# ── The registry ─────────────────────────────────────────────────────────────
# Product-level capabilities only. Anything belonging to a datastore or feature
# lives with that module and arrives via register() — see modules/es/__init__.py.
#
# The governing rule wherever they are declared: read access and edits to data
# that ALREADY EXISTS are part of debugging a live system, so they ship enabled.
# What does not is anything that FABRICATES data — on a customer's production
# CC, synthetic documents are indistinguishable from real ones once written,
# which is exactly the outcome support must never cause.
CORE_CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        id="app.self_update",
        title="Check for and apply in-app updates",
        profiles=_STANDALONE_ONLY,
        unlockable=False,
        note="An appliance follows the CC release train; the updater cannot "
             "reach git from a customer network. Standalone keeps it.",
    ),
)

_BY_ID: dict[str, Capability] = {c.id: c for c in CORE_CAPABILITIES}


def register(module: "Module") -> None:
    """Add a module's capabilities to the registry. Called before any route is
    registered; a duplicate id is a programming error, not a merge of two."""
    global _state
    for cap in module.capabilities:
        if cap.id in _BY_ID and _BY_ID[cap.id] != cap:
            raise ValueError(f"capability {cap.id!r} declared twice — "
                             f"module {module.id!r} clashes with an existing one")
        _BY_ID[cap.id] = cap
    _state = None       # force re-resolution now the registry has grown


def capabilities() -> tuple[Capability, ...]:
    return tuple(_BY_ID.values())


def capability(cap_id: str) -> Capability:
    try:
        return _BY_ID[cap_id]
    except KeyError:
        raise KeyError(f"unknown capability {cap_id!r} — declare it in its "
                       f"module's MODULE.capabilities, or in "
                       f"core.policy.CORE_CAPABILITIES if it is product-level"
                       ) from None


# ── Resolution ───────────────────────────────────────────────────────────────
# Resolved once. Routes are registered at import time, so a capability that
# changed after startup could not take effect anyway; re-reading per request
# would only invite the two halves to disagree. Changing the property file
# requires a container restart, which is both honest and easy to audit.
_state: dict | None = None


def _read_property_file(path: str) -> dict[str, str]:
    """Parse a java-style ``key=value`` property file. Missing file is normal
    and silent — most instances will never have one."""
    props: dict[str, str] = {}
    if not path:
        return props
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith(("#", "!")):
                    continue
                key, sep, value = line.partition("=")
                if sep:
                    props[key.strip()] = value.strip()
    except FileNotFoundError:
        return props
    except OSError as exc:
        # Worth a warning: the file exists but we cannot read it, which
        # presents to the operator as "the unlock did not work".
        logger.warning("[policy] cannot read property file %s: %s", path, exc)
    return props


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on", "enabled")


def _resolve() -> dict:
    profile = (settings.profile or STANDALONE).strip().lower()
    profile = _PROFILE_ALIASES.get(profile, profile)
    if profile not in PROFILES:
        logger.warning("[policy] unknown profile %r — falling back to %r",
                       profile, STANDALONE)
        profile = STANDALONE

    path = settings.policy_file
    props = _read_property_file(path)
    unlocked: set[str] = set()
    for key, value in props.items():
        if not key.startswith("capability."):
            continue
        cap_id = key[len("capability."):]
        if cap_id not in _BY_ID:
            logger.warning("[policy] property file names unknown capability "
                           "%r — ignored", cap_id)
            continue
        if not _truthy(value):
            continue
        if not _BY_ID[cap_id].unlockable:
            logger.warning("[policy] capability %r cannot be unlocked by the "
                           "property file — ignored", cap_id)
            continue
        unlocked.add(cap_id)

    enabled = {
        c.id for c in capabilities()
        if profile in c.profiles or c.id in unlocked
    }
    return {
        "profile": profile,
        "property_file": path,
        "property_file_present": bool(props),
        "unlocked": unlocked,
        "enabled": enabled,
    }


def state() -> dict:
    global _state
    if _state is None:
        _state = _resolve()
    return _state


def reset() -> None:
    """Drop the cached resolution. For tests — production re-reads on restart."""
    global _state
    _state = None


def profile() -> str:
    return state()["profile"]


def enabled(cap_id: str) -> bool:
    """True when this instance carries the capability. Unknown ids raise, so a
    typo fails loudly at startup instead of silently disabling a feature."""
    capability(cap_id)
    return cap_id in state()["enabled"]


def snapshot() -> dict:
    """What the UI needs to hide affordances it must not offer. Deliberately
    does NOT leak the property file's path — knowing a capability is off is
    fine, knowing where to create the file to turn it on is not."""
    st = state()
    return {
        "profile": st["profile"],
        "capabilities": {
            c.id: {
                "enabled": c.id in st["enabled"],
                "title": c.title,
            }
            for c in capabilities()
        },
    }


def log_startup() -> None:
    st = state()
    off = sorted(c.id for c in capabilities() if c.id not in st["enabled"])
    logger.info("[policy] profile=%s · capabilities on: %s",
                st["profile"], len(st["enabled"]))
    if st["unlocked"]:
        logger.info("[policy] unlocked by %s: %s",
                    os.path.basename(st["property_file"]),
                    ", ".join(sorted(st["unlocked"])))
    if off:
        logger.info("[policy] not registered: %s", ", ".join(off))
