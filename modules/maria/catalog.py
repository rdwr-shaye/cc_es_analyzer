"""What the CC's MariaDB holds, and which parts of it matter when debugging.

The same idea as modules/es/catalog.py, and for the same reason. A raw list of
schemas and 175 tables hands the engineer back the question they came with —
"which of these matters?" — so the curated half is the product and the browse
screens on top of it are generic.

Deliberately NOT a schema tree: DBeaver exists. What does not exist is "the
table that explains this attack", which is what this file is for.

Table-level curation is filled in as the debugging scenarios are written down
with R&D; the schema level below is what has been confirmed on a live CC
(OpenSearch-era CC, MariaDB 10.x, `vision_ng` at 175 tables / 46 MB).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SchemaInfo:
    name: str
    title: str
    description: str
    category: str
    # System schemas exist on every MariaDB and say nothing about the CC. Kept
    # reachable (a DBA debugging replication wants them) but out of the way, so
    # the default view is the four that are actually about this product.
    system: bool = False
    tables: tuple[str, ...] = field(default_factory=tuple)


CC_SCHEMA_CATALOG: dict[str, SchemaInfo] = {
    "vision_ng": SchemaInfo(
        name="vision_ng",
        title="Vision NG",
        description="The current CyberController datastore — configuration, "
                    "device inventory, policies and operational state. The "
                    "largest schema on the box and the first place to look.",
        category="Core",
    ),
    "vision": SchemaInfo(
        name="vision",
        title="Vision (legacy)",
        description="The previous generation of the Vision schema. Still "
                    "present and still written to by some flows, so a value "
                    "missing from vision_ng is often here.",
        category="Core",
    ),
    "kvision_auto_engine_db": SchemaInfo(
        name="kvision_auto_engine_db",
        title="Auto Engine",
        description="State for the automation engine — its templates, runs "
                    "and results.",
        category="Automation",
    ),
    "quartz": SchemaInfo(
        name="quartz",
        title="Quartz scheduler",
        description="Scheduled job definitions, triggers and fire history. "
                    "Where to look when a periodic task did not run.",
        category="Scheduling",
    ),
    "mysql": SchemaInfo(
        name="mysql", title="mysql",
        description="Server catalog: accounts, grants, plugins.",
        category="System", system=True),
    "sys": SchemaInfo(
        name="sys", title="sys",
        description="Performance-schema helper views.",
        category="System", system=True),
    "performance_schema": SchemaInfo(
        name="performance_schema", title="performance_schema",
        description="Server instrumentation.",
        category="System", system=True),
    "information_schema": SchemaInfo(
        name="information_schema", title="information_schema",
        description="Server metadata. This module reads it to discover what "
                    "a given CC actually has.",
        category="System", system=True),
}


def describe(schema: str) -> SchemaInfo | None:
    """Curated metadata for a schema, or None when this CC has one we have not
    catalogued — which is information in itself, so callers surface it rather
    than hiding the schema."""
    return CC_SCHEMA_CATALOG.get(schema)


def is_system(schema: str) -> bool:
    info = CC_SCHEMA_CATALOG.get(schema)
    return bool(info and info.system)
