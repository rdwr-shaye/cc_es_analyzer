"""What the CC's PostgreSQL holds, and which parts of it matter when debugging.

The same idea as modules/maria/catalog.py, for the same reason: a raw list of
databases and hundreds of tables hands the engineer back the question they
came with — "which of these matters?" — so the curated half is the product.

PostgreSQL splits differently to MariaDB, and that shapes what "curated" means
here. One MariaDB connection sees every schema on the server; one PostgreSQL
connection sees exactly one DATABASE (modules/pg/client.py has to reconnect to
browse a second one). So the unit this catalog describes is the database, not
a schema within it — every database on this CC keeps its own tables in the
`public` schema, and `pg_catalog`/`information_schema` are PostgreSQL's own
system catalogs in every database, not something specific to one of them.

Descriptions below are read off the table names actually observed on a lab CC
(10.205.189.20, PostgreSQL 18), not from product documentation — there is no
per-database README on the box. Treat them as an engineer's working guess from
the schema, not a specification; a table doing something unexpected is grounds
to fix this file, not to distrust it.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DatabaseInfo:
    name: str
    title: str
    description: str
    category: str
    # `postgres` itself is the server's always-present maintenance database —
    # never a CC datastore. Kept reachable (useful for a raw diagnostic query)
    # but out of the way, matching how modules/maria/catalog.py treats mysql/
    # sys/performance_schema/information_schema.
    system: bool = False
    tables: tuple[str, ...] = field(default_factory=tuple)


CC_DATABASE_CATALOG: dict[str, DatabaseInfo] = {
    "dfc": DatabaseInfo(
        name="dfc",
        title="DefensePro configuration",
        description="The largest database on the box by table count. Holds "
                    "DefensePro's own policy and attack model — active_policy, "
                    "active_signature, attack, bdos_config, bgp_flow_spec — "
                    "so a mitigation decision that does not match what the UI "
                    "shows is often explained by a row here.",
        category="DefensePro",
    ),
    "policy_editor": DatabaseInfo(
        name="policy_editor",
        title="Policy Editor",
        description="Protection policy authoring: policy_template, "
                    "protection_configuration, pulse_template, plus the feed "
                    "tables a policy can reference (geo_location_feed, "
                    "asn_feed, dns_allow_list). Where a template looks wrong "
                    "before it is even applied.",
        category="DefensePro",
    ),
    "dpinlineconfig": DatabaseInfo(
        name="dpinlineconfig",
        title="DefensePro inline configuration",
        description="Inline deployment state — managed_policy, network, "
                    "packet_capture, migration_request/migration_operation. "
                    "The migration_* tables are the first place to look when "
                    "an inline deployment change did not take.",
        category="DefensePro",
    ),
    "anomalydetectionengine": DatabaseInfo(
        name="anomalydetectionengine",
        title="Anomaly Detection Engine",
        description="Baseline and anomaly state — anomaly, "
                    "moving_average_snapshot, network_citizen, and the "
                    "socx_* tables behind SOC-facing recommendations "
                    "(socx_recommendation, socx_positive_recommendation, "
                    "socx_event).",
        category="Detection",
    ),
    "definitions": DatabaseInfo(
        name="definitions",
        title="Definitions",
        description="Small and cross-cutting: forensics-definition(-history), "
                    "rt-alert-def-vrm and vrm-scheduled-report-definition. "
                    "Alert and report DEFINITIONS, not the alerts or reports "
                    "themselves — those live in Elasticsearch.",
        category="Definitions",
    ),
    "postgres": DatabaseInfo(
        name="postgres", title="postgres",
        description="PostgreSQL's own maintenance database. No CC tables of "
                    "its own; useful mainly for a server-wide diagnostic query.",
        category="System", system=True),
}


def describe(database: str) -> DatabaseInfo | None:
    """Curated metadata for a database, or None when this CC has one we have
    not catalogued — which is information in itself, so callers surface it
    rather than hiding the database."""
    return CC_DATABASE_CATALOG.get(database)


def is_system(database: str) -> bool:
    info = CC_DATABASE_CATALOG.get(database)
    return bool(info and info.system)
