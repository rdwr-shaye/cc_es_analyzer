"""Which dashboard widgets exist, and what each one depends on.

No canonical registry of this exists anywhere in the product — checked
kvision_vrm's controllers and webui_components' widget-repository UI, neither
names a stable widget id. So `widget_catalog.json`, next to this file, IS the
catalog: hand-built by reading each `*Controller` in kvision_vrm and tracing
it to the `*QueryHandler` / `AbstractAdaptiveReportIndexNameResolver2` that
resolves its Elasticsearch index, and to the `ExecutorType` (kvision_libs'
datapolling DTO) whose RabbitMQ-published task ultimately fills that index.

Every row is provenance, not guesswork — `controller` names the exact class
this was read from. A handful of rows carry `confirmed: false`: their endpoint
is real (the controller exists) but the index prefix could not be pinned down
to one constant in the source available when the row was written. Those still
work for Check 1 (the API replay does not need an index prefix), report
`UNKNOWN` rather than `MISSING` for Check 2, and are visibly badged in the
picker rather than presented as solid as the rest.

DESIGN FOR AUTOMATION. This catalog is hand-built today because nothing else
exists to build it from — that is not meant to stay true forever. A later
pass could plausibly walk kvision_vrm's controllers and regenerate this table
automatically, so the ROWS live in `widget_catalog.json` — a plain data file —
rather than as Python dataclass literals. This module's only job is to load
that file, validate each row, and wrap it as a `WidgetCheck`. A future
generator's whole job is then to overwrite `widget_catalog.json` wholesale: no
Python syntax to emit correctly, nothing that risks touching the loader below
or the capabilities/routers this package wires up, and a plain `git diff` on
the JSON is exactly what a reviewer would want to read before trusting a
re-scan. The schema a generator must emit is: a top-level `schema_version`
(int), `generated_by` (a provenance string — who/what produced this file and
when), `category_order` (the picker's <optgroup> order), and `widgets` (an
object keyed by widget id, each value matching WidgetCheck's fields below,
`id` itself excluded since it is the key).

A malformed or missing row is skipped with a logged warning, never a startup
crash: this catalog backs a capability that ships on in both profiles, so one
bad entry in a future auto-generated file must degrade to "one fewer widget in
the picker," not take the app down.

Deliberately NOT modeled here (checked, no confirmed VRM controller and/or no
confirmed Elasticsearch persistence path — i.e. status/config polling rather
than an ES-backed dashboard widget): DP Traffic Baseline, Flow Detector
Hostgroup Counters, Flow Detector attacks, Network Element Monitoring, cDDoS
Connector Status.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_CATALOG_FILE = os.path.join(os.path.dirname(__file__), "widget_catalog.json")

# Fields a row MUST carry to be usable at all. Everything else in WidgetCheck
# has a default, so a future generator that omits an optional field degrades
# gracefully rather than losing the whole row.
_REQUIRED_FIELDS = ("label", "domain", "controller", "api_path")


@dataclass(frozen=True)
class WidgetCheck:
    id: str                              # picker value, e.g. "eaaf-total"
    label: str                           # shown in the UI, e.g. "EAAF total"
    domain: str                          # picker grouping, e.g. "EAAF"
    controller: str                      # provenance — the kvision_vrm class this came from
    api_path: str                        # appended to the VRM base + "/mgmt/vrm"
    index_prefixes: tuple[str, ...] = field(default_factory=tuple)
    executor_type: str = ""              # a kvision_libs ExecutorType name, or "" if none maps
    min_dp_version: str = ""             # static fact — no live per-device version check
    confirmed: bool = True               # False = index_prefixes not pinned to source; badge it
    notes: str = ""


def _load() -> tuple[dict[str, WidgetCheck], tuple[str, ...]]:
    """(widgets, category_order). Never raises — a broken catalog file must
    degrade the picker, not the app; see the module docstring."""
    try:
        with open(_CATALOG_FILE, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        logger.error("[widgets] %s is missing — the widget picker will be "
                     "empty", _CATALOG_FILE)
        return {}, ()
    except (OSError, ValueError) as exc:
        logger.error("[widgets] %s is not readable/valid JSON — the widget "
                     "picker will be empty: %s", _CATALOG_FILE, exc)
        return {}, ()

    widgets: dict[str, WidgetCheck] = {}
    for widget_id, row in (raw.get("widgets") or {}).items():
        if not isinstance(row, dict):
            logger.warning("[widgets] %s: row for %r is not an object — "
                           "skipped", os.path.basename(_CATALOG_FILE), widget_id)
            continue
        missing = [k for k in _REQUIRED_FIELDS if not row.get(k)]
        if missing:
            logger.warning("[widgets] %s: %r is missing %s — skipped",
                           os.path.basename(_CATALOG_FILE), widget_id, missing)
            continue
        widgets[widget_id] = WidgetCheck(
            id=widget_id,
            label=row["label"], domain=row["domain"],
            controller=row["controller"], api_path=row["api_path"],
            index_prefixes=tuple(row.get("index_prefixes") or ()),
            executor_type=row.get("executor_type", ""),
            min_dp_version=row.get("min_dp_version", ""),
            confirmed=bool(row.get("confirmed", True)),
            notes=row.get("notes", ""),
        )

    category_order = tuple(raw.get("category_order") or ())
    return widgets, category_order


WIDGET_CATALOG, CATEGORIES = _load()


def describe(widget_id: str) -> WidgetCheck | None:
    return WIDGET_CATALOG.get(widget_id)


def by_domain() -> dict[str, list[WidgetCheck]]:
    """CATEGORIES order first, then any domain the catalog file introduced
    that CATEGORIES does not name yet — alphabetically, so a future automated
    scan that finds a genuinely new widget domain shows up in the picker
    rather than silently vanishing until someone remembers to extend
    category_order."""
    grouped: dict[str, list[WidgetCheck]] = {}
    for w in WIDGET_CATALOG.values():
        grouped.setdefault(w.domain, []).append(w)
    ordered = [d for d in CATEGORIES if d in grouped]
    ordered += sorted(d for d in grouped if d not in CATEGORIES)
    return {domain: grouped[domain] for domain in ordered}


def as_dict(w: WidgetCheck) -> dict:
    """The catalog row shape both routers/debug.py and routers/probe.py embed
    in their responses — one place to add a field rather than two to keep in
    sync."""
    return {
        "id": w.id, "label": w.label, "domain": w.domain,
        "controller": w.controller, "api_path": w.api_path,
        "index_prefixes": list(w.index_prefixes),
        "executor_type": w.executor_type, "min_dp_version": w.min_dp_version,
        "confirmed": w.confirmed, "notes": w.notes,
    }
