"""MariaDB module — the CC's relational store.

Declares its own capabilities and routers, so enabling it is one line in
modules/__init__.py and nothing in main.py or core/policy.py changes.

Only read capabilities exist so far, and that is a deliberate starting point
rather than an unfinished one: reads are what a debugging session needs first,
and shipping the write path later means the security review can be had about a
component that provably cannot modify a customer's CC database today.
"""

from __future__ import annotations

from core.policy import Capability, Module, EMBEDDED, STANDALONE

_BOTH = (STANDALONE, EMBEDDED)


def _module() -> Module:
    # Imported inside the function for the same reason as modules/es: the
    # routers import this package's siblings, so importing them at module scope
    # would be a cycle.
    from modules.maria.routers import browse, query

    return Module(
        id="maria",
        title="MariaDB",
        capabilities=(
            Capability(
                id="maria.read",
                title="Browse the CC's MariaDB schemas and tables",
                profiles=_BOTH,
                note="Read-only at the transaction, not just in our SQL layer.",
            ),
            Capability(
                id="maria.query.raw",
                title="Run read-only SQL against the CC's MariaDB",
                profiles=_BOTH,
                note="The escape hatch for joins the curated screens do not "
                     "cover. Still read-only, still capped and timed out.",
            ),
        ),
        routers=(
            (browse.router, "maria.read"),
            (query.router, "maria.query.raw"),
        ),
    )


MODULE = _module
