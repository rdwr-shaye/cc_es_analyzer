"""Roll up the three checks into one severity and one sentence.

Pure — no I/O, no ES/MariaDB/HTTP calls — unit-testable the same way
modules/system/checks.py's parsers are, and for the same reason: these are the
rules that decide what a support engineer is told about a customer's data
pipeline, so they belong where a CC is not required to exercise them.

Severity vocabulary matches the rest of the app (modules/system/checks.py,
modules/diag/probes.py): ok < unknown < warn < crit, and `unknown` outranks
`ok` — a check that could not run must never look like a clean bill of health.

The three probe-aware branches (near the bottom) encode a specific reading:
when the live device probe (Tier B, optional — see device_probe.py) proves the
DefensePro device itself has real data for this window, an otherwise-ambiguous
"nothing in the index" stops being ambiguous. It narrows to WHICH side of the
KVision pipeline lost the data:

  * Tier A (the polling-task status) says the last poll SUCCEEDED -> the data
    reached the product and was lost after collection. Suspect the
    data-persist-service failing to write from its RabbitMQ queue.
  * Tier A says FAIL, or the device was never polled at all -> KVision never
    got the data even though the device has it. Suspect data-polling-
    scheduler's fetch for this device/executor, not persistence — this probe
    succeeded where the product's own polling apparently did not.

And when the probe instead shows the device has NOTHING, an empty widget was
correct all along: the root cause traces back to the source, not to a bug in
this product's pipeline. These three rows only ever fire when Tier B was
explicitly run for a device — `/debug` alone never reaches them.
"""

from __future__ import annotations

OK, UNKNOWN, WARN, CRIT = "ok", "unknown", "warn", "crit"

_INDEX_RANK = {"MISSING": 3, "EXISTS_EMPTY": 2, "UNKNOWN": 1, "EXISTS": 0}
_DEVICE_RANK = {"FAIL": 3, "NO_DATA": 2, "UNKNOWN": 1, "SUCCESS": 0}


def _rollup_index(index_results: list[dict]) -> tuple[str, int]:
    """(worst status, total docs) across a widget's index prefixes. A widget
    with several prefixes is only as healthy as its worst one — the same
    "worst of them" rule modules/system/checks.py::worst() applies to panes."""
    if not index_results:
        return "UNKNOWN", 0
    worst = max(index_results, key=lambda r: _INDEX_RANK.get(r.get("status"), 1))
    return worst.get("status", "UNKNOWN"), sum(r.get("doc_count", 0) for r in index_results)


def _rollup_device(device_results: list[dict]) -> str | None:
    if not device_results:
        return None
    worst = max(device_results, key=lambda r: _DEVICE_RANK.get(r.get("status"), 1))
    return worst.get("status")


def diagnose(api: dict, index_results: list[dict], device_results: list[dict],
            probe_result: dict | None = None) -> dict:
    """{"severity", "headline"}. `probe_result` is None whenever Tier B has
    not been run for this report (the common case — it's opt-in)."""
    api_status = api.get("status")
    index_status, _index_docs = _rollup_index(index_results)
    device_status = _rollup_device(device_results)
    probe_status = probe_result.get("status") if probe_result else None

    if api_status == "PASS":
        # A PASS does not, by itself, mean the picture is consistent — if the
        # index this tool checked shows nothing for the same window, that is
        # a real contradiction worth naming, not a detail to drop. Confirmed
        # live: EAAF total returned a real row while eaaf-attack-data showed
        # EXISTS_EMPTY for the identical range — meaning either this widget's
        # real data source differs from the index family the catalog names,
        # or the API result reflects a wider scope/cache than the picked
        # time range. Either way "check its own UI filters" would have been
        # the wrong thing to tell the engineer.
        if index_status in ("MISSING", "EXISTS_EMPTY"):
            return {"severity": WARN, "headline":
                    "the API returned data, but the index this tool checked "
                    "shows nothing for this window — either this widget's "
                    "real data source differs from the index family in the "
                    "catalog (worth confirming against source), or the API "
                    "result reflects a wider scope or a cache rather than "
                    "exactly this time range"}
        return {"severity": OK, "headline":
                "the API returns data — if the widget still looks empty, "
                "check its own UI filters"}
    if api_status == "ERROR":
        return {"severity": CRIT, "headline":
                f"the widget's own API call failed: {api.get('detail', '') or 'see detail'}"}
    if api_status is None:
        return {"severity": UNKNOWN, "headline": "the API check did not run"}

    # From here, api_status == "EMPTY": the widget's own call returned
    # nothing. Everything below narrows down WHY.
    index_empty = index_status in ("MISSING", "EXISTS_EMPTY")

    if probe_status == "DATA" and index_empty:
        if device_status == "SUCCESS":
            return {"severity": CRIT, "headline":
                    "the device has real data and the last poll reported "
                    "success, but it never reached the index — suspect the "
                    "data-persist-service failing to write from its RabbitMQ "
                    "queue, not collection"}
        return {"severity": CRIT, "headline":
                "the device has real data but data-polling never "
                "(successfully) fetched it — this probe succeeded where the "
                "product's own polling didn't, so suspect data-polling-"
                "scheduler's fetch for this device/executor, not persistence"}

    if probe_status in ("EMPTY", "HTTP_ERROR", "UNSUPPORTED_VERSION") and index_empty:
        return {"severity": WARN, "headline":
                "the device itself has nothing (or can't be reached, or "
                "doesn't support this executor at its version) — the empty "
                "widget traces to the source, not the KVision pipeline"}

    if index_status == "MISSING":
        return {"severity": CRIT, "headline": "index family never created"}

    if index_status == "EXISTS_EMPTY":
        if device_status == "FAIL":
            return {"severity": CRIT, "headline": "last poll failed — see task_info"}
        if device_status == "NO_DATA":
            return {"severity": WARN, "headline":
                    "never polled — check DP version against the executor's "
                    "minimum, feature enabled? Run the live probe to confirm "
                    "the device itself has data before chasing the pipeline."}
        if device_status == "SUCCESS":
            return {"severity": WARN, "headline":
                    "polling succeeded but nothing was written — check the "
                    "persist-service consumer, or genuinely no traffic in "
                    "this window"}
        return {"severity": UNKNOWN, "headline":
                "index exists but is empty, and the last polling status "
                "could not be determined"}

    if index_status == "EXISTS":
        return {"severity": WARN, "headline":
                "data exists end-to-end but the API found none — the query/"
                "filter/scope in this replay likely differs from the "
                "widget's real request"}

    return {"severity": UNKNOWN, "headline":
            "could not determine a diagnosis — the index check did not complete"}
