"""Check 2 — does the widget's Elasticsearch index exist for the selected range.

No new query logic: built entirely on modules/es/discovery.py, which already
answers "what index families can this cluster produce" and "what time window
does this index name cover" for the System Health and index-browsing screens.
This module's only job is to point that machinery at a widget's catalog rows
and turn the result into the MISSING / EXISTS_EMPTY / EXISTS vocabulary a
diagnosis needs.

The IO (fetching the live index list and the discovery catalog) is kept
separate from the classification (`_covers`, `classify_prefix`), the same
split every check-parsing module in this codebase uses — the rules that decide
what an engineer is told are pure functions, testable without a cluster.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

MISSING, EXISTS_EMPTY, EXISTS, UNKNOWN = "MISSING", "EXISTS_EMPTY", "EXISTS", "UNKNOWN"


def _covers(window: tuple[int, int] | None, from_ms: int, to_ms: int) -> bool:
    """True when an index's covered time window overlaps [from_ms, to_ms).

    `window` is None whenever the family's slice length could not be measured
    (discovery.slice_window's own "no guess" rule) — that counts as covering,
    because excluding an index CC Admin cannot date is worse than including
    one it should have: a widget's index family being reported MISSING when it
    is actually just un-dateable would be a false, and confident, root cause.
    """
    if window is None:
        return True
    start, end = window
    return start < to_ms and end > from_ms


def classify_prefix(matches: list[dict], covering: list[dict]) -> tuple[str, int]:
    """(status, doc_count) from already-filtered `_cat/indices` rows.

    Pure: `matches`/`covering` are plain {"index", "health", "docs.count"}
    dicts, so this is exercised with synthetic rows and needs no ES client.
    """
    if not matches:
        return MISSING, 0
    if not covering:
        return EXISTS_EMPTY, 0
    doc_count = sum(int(r.get("docs.count") or 0) for r in covering)
    return (EXISTS if doc_count else EXISTS_EMPTY), doc_count


def check_indices(widget, from_ms: int, to_ms: int) -> list[dict]:
    """One entry per `widget.index_prefixes`. A catalog row with no confirmed
    prefix (see catalog.py's `confirmed=False`) reports UNKNOWN rather than
    MISSING — an unverified prefix is not evidence of a missing index.
    """
    if not widget.confirmed or not widget.index_prefixes:
        return [{"prefix": None, "status": UNKNOWN, "doc_count": 0, "indices": [],
                 "detail": widget.notes or
                           "this widget's index prefix was not confirmed against "
                           "source — see modules/widgets/catalog.py"}]

    from modules.es.client import get_client
    from modules.es import discovery

    es = get_client()
    try:
        family_catalog = discovery.discover(es)["families"]
        live = es.cat_indices()
    except Exception as exc:                                   # noqa: BLE001
        logger.info("[widgets] index check unavailable: %s", exc)
        return [{"prefix": p, "status": UNKNOWN, "doc_count": 0, "indices": [],
                 "detail": str(exc)} for p in widget.index_prefixes]

    results = []
    for prefix in widget.index_prefixes:
        slice_minutes = family_catalog.get(prefix, {}).get("slice_minutes")
        matches = [row for row in live
                  if discovery.family_of(row.get("index", "")) == prefix]
        covering = [row for row in matches
                   if _covers(discovery.slice_window(row["index"], slice_minutes),
                              from_ms, to_ms)]
        status, doc_count = classify_prefix(matches, covering)
        shown = covering or matches
        results.append({
            "prefix": prefix, "status": status, "doc_count": doc_count,
            "indices": [{"name": r["index"], "health": r.get("health"),
                        "docs": int(r.get("docs.count") or 0)} for r in shown],
        })
    return results
