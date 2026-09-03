"""PostgreSQL module — the CC's other relational store.

Same shape as modules/maria: declares its own capabilities and routers, so
enabling it is one line in modules/__init__.py and nothing in main.py or
core/policy.py changes.

The read capabilities ship on. The write capability does not: `pg.write`
belongs to NO profile and is reachable only by creating the property file, for
the identical reason modules/maria/__init__.py gives for `maria.write` — Phase
1 has not yet put identity, authorization and a durable audit trail behind it,
so the conservative default is off, and unlocking it is a decision an operator
takes knowingly.
"""

from __future__ import annotations

from core.policy import Capability, Module, EMBEDDED, STANDALONE

_BOTH = (STANDALONE, EMBEDDED)
_NEITHER: tuple[str, ...] = ()


def _module() -> Module:
    # Imported inside the function for the same reason as modules/maria: the
    # routers import this package's siblings, so importing them at module scope
    # would be a cycle.
    from modules.pg.routers import browse, edit, query

    return Module(
        id="pg",
        title="PostgreSQL",
        capabilities=(
            Capability(
                id="pg.read",
                title="Browse the CC's PostgreSQL databases and tables",
                profiles=_BOTH,
                note="Read-only at the transaction, not just in our SQL layer.",
            ),
            Capability(
                id="pg.query.raw",
                title="Run read-only SQL against the CC's PostgreSQL",
                profiles=_BOTH,
                note="The escape hatch for joins the curated screens do not "
                     "cover. Still read-only, still capped and timed out.",
            ),
            Capability(
                id="pg.write",
                title="Edit a single cell in the CC's PostgreSQL",
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
            (browse.router, "pg.read"),
            (query.router, "pg.query.raw"),
            (edit.router, "pg.write"),
        ),
    )


MODULE = _module
