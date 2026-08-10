"""MariaDB module — the CC's relational store.

Declares its own capabilities and routers, so enabling it is one line in
modules/__init__.py and nothing in main.py or core/policy.py changes.

The read capabilities ship on. The write capability does not: `maria.write`
belongs to NO profile and is reachable only by creating the property file, so
by default this component cannot modify a customer's CC database and the route
that would do so is not registered at all. That is a deliberately conservative
default for a capability that Phase 1 has not yet put identity, authorization
and a durable audit trail behind — the roadmap allows edits to existing data on
a customer appliance, but "allowed" there means gated, and the gate is not
built. Unlocking it before then is a decision an operator takes knowingly, on
an instance where a log line is accountability enough.
"""

from __future__ import annotations

from core.policy import Capability, Module, EMBEDDED, STANDALONE

_BOTH = (STANDALONE, EMBEDDED)
_NEITHER: tuple[str, ...] = ()


def _module() -> Module:
    # Imported inside the function for the same reason as modules/es: the
    # routers import this package's siblings, so importing them at module scope
    # would be a cycle.
    from modules.maria.routers import browse, edit, query

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
            Capability(
                id="maria.write",
                title="Edit a single cell in the CC's MariaDB",
                profiles=_NEITHER,
                unlockable=True,
                note="One column of one row, addressed by its full primary "
                     "key, refused on key/binary/generated columns and on any "
                     "row that changed since it was read. Off everywhere until "
                     "an operator creates the property file, because the "
                     "identity and audit machinery it should sit behind is "
                     "Phase 1 work that does not exist yet.",
            ),
        ),
        routers=(
            (browse.router, "maria.read"),
            (query.router, "maria.query.raw"),
            (edit.router, "maria.write"),
        ),
    )


MODULE = _module
