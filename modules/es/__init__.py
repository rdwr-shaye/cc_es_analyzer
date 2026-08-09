"""Elasticsearch / OpenSearch module.

The original CC ES Analyzer, now one module of CC Admin. It declares its own
capabilities and routers so neither main.py nor core/policy.py has to know
Elasticsearch exists — which is what makes PostgreSQL and MariaDB a new package
rather than an edit to everything.

Capability ids are namespaced `es.*`, matching the nav hierarchy the frontend
renders (Databases > Elasticsearch > screens). Keeping those two shapes the
same is deliberate: UI gating falls out of the namespace instead of needing a
hand-maintained map of which button belongs to which capability.
"""

from core.policy import Capability, Module, EMBEDDED, STANDALONE

_BOTH = (STANDALONE, EMBEDDED)
_STANDALONE_ONLY = (STANDALONE,)


def _module() -> Module:
    # Imported inside the function: the routers import this package's siblings
    # (client, catalog, discovery), so importing them at module scope would be
    # a cycle. main.py calls discover() once, at startup.
    from modules.es.routers import artificial, exports, health, indices, query

    return Module(
        id="es",
        title="Elasticsearch / OpenSearch",
        capabilities=(
            Capability(
                id="es.read",
                title="Browse and query Elasticsearch/OpenSearch",
                profiles=_BOTH,
            ),
            Capability(
                id="es.connect",
                title="Choose which Elasticsearch to connect to",
                profiles=_STANDALONE_ONLY,
                note="Embedded on a CC the datastore is the one running beside "
                     "it — ES_HOST comes from the compose file and there is "
                     "nothing to pick. A connection screen there would only "
                     "invite pointing a CC's console at someone else's cluster. "
                     "Standalone this is the whole point of the product.",
            ),
            Capability(
                id="es.doc.write",
                title="Edit, bulk-update, bulk-delete and import documents",
                profiles=_BOTH,
                note="Changes existing data; gated by authorisation and audit "
                     "once those land, not by profile.",
            ),
            Capability(
                id="es.index.admin",
                title="Create and delete indices",
                profiles=_BOTH,
            ),
            Capability(
                id="es.index.duplicate",
                title="Duplicate an index, optionally shifting its dates",
                profiles=_STANDALONE_ONLY,
                unlockable=True,
                note="Produces a synthetic copy of real data — a reproduction "
                     "tool.",
            ),
            Capability(
                id="es.artificial",
                title="Generate artificial documents",
                profiles=_STANDALONE_ONLY,
                unlockable=True,
                note="Fabricates data outright. Never on by default at a "
                     "customer.",
            ),
            Capability(
                id="es.archive.export",
                title="Export and archive index data",
                profiles=_BOTH,
                note="Moves customer data off the box — needs a data-residency "
                     "policy and an audit record in the embedded profile.",
            ),
            Capability(
                id="es.archive.restore",
                title="Restore an archive into an index",
                profiles=_BOTH,
            ),
        ),
        # (router, gating capability or None). Ungated routers still sit behind
        # the module itself: if es.read is ever off, discover() drops the whole
        # module rather than registering a console for a store you cannot read.
        routers=(
            (health.router, None),
            (indices.router, None),
            (query.router, None),
            (exports.router, None),
            (indices.gated_router, "es.index.duplicate"),
            (artificial.router, "es.artificial"),
        ),
    )


MODULE = _module
