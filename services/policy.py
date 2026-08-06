"""Deployment profile and capability policy.

One image ships to a lab machine and to a customer's CyberController, so what
an instance may do cannot be decided by which build it is. It is decided at
startup from two inputs:

  1. the PROFILE — ``lab`` for a developer/lab run, ``embedded`` for the copy
     that rides the CC's monitoring compose. Comes from ANALYZER_PROFILE, so
     the compose file is what pins an appliance to ``embedded``;
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
means one entry in CAPABILITIES plus a conditional include_router — the shape
that keeps the later phases of the roadmap cheap.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from config import settings

logger = logging.getLogger(__name__)

# ── Profiles ─────────────────────────────────────────────────────────────────
LAB = "lab"
EMBEDDED = "embedded"
PROFILES = (LAB, EMBEDDED)

_BOTH = (LAB, EMBEDDED)
_LAB_ONLY = (LAB,)


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


# ── The registry ─────────────────────────────────────────────────────────────
# Read access and edits to data that ALREADY EXISTS are part of debugging a
# live system, so they ship enabled. What does not ship enabled is anything
# that FABRICATES data — on a customer's production CC, synthetic documents
# are indistinguishable from real ones once written, which is exactly the
# outcome support must never cause.
CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        id="es.read",
        title="Browse and query Elasticsearch/OpenSearch",
        profiles=_BOTH,
    ),
    Capability(
        id="es.connect",
        title="Choose which Elasticsearch to connect to",
        profiles=_LAB_ONLY,
        note="Embedded on a CC, the datastore is the one running beside it — "
             "ES_HOST is set by the compose file and there is nothing to pick. "
             "A connection screen there would only invite pointing a CC's "
             "console at someone else's cluster.",
    ),
    Capability(
        id="es.doc.write",
        title="Edit, bulk-update, bulk-delete and import documents",
        profiles=_BOTH,
        note="Changes existing data; gated by authorisation and audit once "
             "those land, not by profile.",
    ),
    Capability(
        id="es.index.admin",
        title="Create and delete indices",
        profiles=_BOTH,
    ),
    Capability(
        id="es.index.duplicate",
        title="Duplicate an index, optionally shifting its dates",
        profiles=_LAB_ONLY,
        unlockable=True,
        note="Produces a synthetic copy of real data — lab/reproduction tool.",
    ),
    Capability(
        id="es.artificial",
        title="Generate artificial documents",
        profiles=_LAB_ONLY,
        unlockable=True,
        note="Fabricates data outright. Never on by default at a customer.",
    ),
    Capability(
        id="archive.export",
        title="Export and archive index data",
        profiles=_BOTH,
        note="Moves customer data off the box — needs a data-residency policy "
             "and an audit record in the embedded profile.",
    ),
    Capability(
        id="archive.restore",
        title="Restore an archive into an index",
        profiles=_BOTH,
    ),
    Capability(
        id="app.self_update",
        title="Check for and apply in-app updates",
        profiles=_LAB_ONLY,
        unlockable=False,
        note="An appliance follows the CC release train; the updater cannot "
             "reach git from a customer network.",
    ),
)

_BY_ID = {c.id: c for c in CAPABILITIES}


def capability(cap_id: str) -> Capability:
    try:
        return _BY_ID[cap_id]
    except KeyError:
        raise KeyError(f"unknown capability {cap_id!r} — add it to "
                       f"services/policy.CAPABILITIES") from None


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
    profile = (settings.profile or LAB).strip().lower()
    if profile not in PROFILES:
        logger.warning("[policy] unknown profile %r — falling back to %r",
                       profile, LAB)
        profile = LAB

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
        c.id for c in CAPABILITIES
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
            for c in CAPABILITIES
        },
    }


def log_startup() -> None:
    st = state()
    off = sorted(c.id for c in CAPABILITIES if c.id not in st["enabled"])
    logger.info("[policy] profile=%s · capabilities on: %s",
                st["profile"], len(st["enabled"]))
    if st["unlocked"]:
        logger.info("[policy] unlocked by %s: %s",
                    os.path.basename(st["property_file"]),
                    ", ".join(sorted(st["unlocked"])))
    if off:
        logger.info("[policy] not registered: %s", ", ".join(off))
