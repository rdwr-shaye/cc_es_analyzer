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
            # Creating and deleting an index were one capability until it
            # became clear they are not the same risk. Creating one on a
            # customer's appliance is a reproduction activity — the CC makes
            # its own from its index templates, and an engineer who wants a
            # scratch index wants it on their own machine. Deleting one is
            # something support genuinely has to do on a customer box, because
            # a corrupted index is a real thing to meet there and dropping it
            # is often the fastest way back to a green cluster.
            Capability(
                id="es.index.create",
                title="Create an index",
                profiles=_STANDALONE_ONLY,
                unlockable=True,
                note="Embedded, GET /api/indices/possible still lists every "
                     "index family this CC's templates could produce — looking "
                     "at the catalog is diagnosis, creating from it is not.",
            ),
            Capability(
                id="es.index.delete",
                title="Delete an index",
                profiles=_BOTH,
                note="Always a data loss, and deliberately available on a "
                     "customer's CC: a corrupted index has to be removable by "
                     "the engineer who found it.",
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
        #
        # The ungated routers are registered FIRST, and that is load-bearing
        # rather than tidy: FastAPI matches in registration order, and
        # exports.router holds the specific paths (/jobs/{job_id},
        # /ssh-creds/{host}) that would otherwise be captured by
        # export_router's DELETE /{name}. See the comment in exports.py.
        routers=(
            (health.router, None),
            (indices.router, None),
            (query.router, None),
            (exports.router, None),

            # Every capability below controls a router. A capability that names
            # no router is decorative — /api/policy would report a boundary
            # that route registration does not keep, and a reviewer reading the
            # registry would place the boundary somewhere it is not.
            (query.write_router, "es.doc.write"),
            (indices.import_router, "es.doc.write"),
            (indices.create_router, "es.index.create"),
            (indices.delete_router, "es.index.delete"),
            (exports.export_router, "es.archive.export"),
            (exports.restore_router, "es.archive.restore"),
            (indices.gated_router, "es.index.duplicate"),
            (artificial.router, "es.artificial"),
        ),
    )


MODULE = _module
