"""CC Admin feature modules.

Each module is a package exporting ``MODULE`` — a zero-arg callable returning a
``core.policy.Module``. Adding PostgreSQL means writing ``modules/pg/`` and
adding one line to ENABLED below; main.py does not change, and neither does
core/policy.py.

Deferred as a callable rather than a plain attribute because a module's routers
import its own siblings, so building the Module at import time would create a
cycle. discover() is called once, at startup.
"""

from __future__ import annotations

import importlib
import logging

from core import policy
from core.policy import Module

logger = logging.getLogger(__name__)

# Order matters only for route registration; keep it readable.
ENABLED: tuple[str, ...] = (
    # First, because it owns the landing page: "is this CC healthy" is the
    # question asked before any datastore is opened.
    "modules.system",
    "modules.es",
    "modules.maria",
    # Read-only diagnostics: can this CC reach the services it depends on?
    # The first half of the corrective-actions work — the half that changes
    # nothing and is therefore safe on a customer's production appliance.
    "modules.diag",
    # Phase 2: "modules.pg"
    # Phase 3: "modules.kb"
)


def discover() -> list[Module]:
    """Import every enabled module, register its capabilities, return them.

    Must run before anything calls policy.enabled(), since that is what
    populates the registry.
    """
    found: list[Module] = []
    for path in ENABLED:
        mod = importlib.import_module(path)
        factory = getattr(mod, "MODULE", None)
        if factory is None:
            logger.warning("[modules] %s exports no MODULE — skipped", path)
            continue
        m = factory() if callable(factory) else factory
        policy.register(m)
        found.append(m)
    logger.info("[modules] loaded: %s", ", ".join(m.id for m in found) or "none")
    return found
