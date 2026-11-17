"""Tests for modules/widgets — the rules that decide what a support engineer
is told about why a dashboard widget is showing no data.

Same split as every other check-parsing module in this codebase
(modules/system/checks.py, modules/diag/probes.py): the IO (HTTP calls to the
CC's VRM API and to a DefensePro device, ES/MariaDB queries) cannot be tested
without those things, and does not need to be — those clients are not ours to
verify. What IS ours, and what this file covers, is the INTERPRETATION: the
pure functions that turn a response into PASS/EMPTY/ERROR, an index into
MISSING/EXISTS_EMPTY/EXISTS, a polling row into SUCCESS/FAIL/NO_DATA, and all
of it into one severity and one sentence.

The catalog is a fixed table like modules/diag/targets.py's — checked for
self-consistency here rather than trusted by inspection. The decrypt round
trip is exercised against a LOCALLY GENERATED key and password, never a real
device credential — this file never touches vision_ng or a live CC.

    python tests/test_widget_debug.py
    pytest tests/test_widget_debug.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.widgets import catalog, client, device_check, device_probe, diagnosis, index_check  # noqa: E402

_FAILURES: list[str] = []


def check(label, got, want):
    if got == want:
        print(f"  ok  {label}")
    else:
        _FAILURES.append(f"{label}: expected {want!r}, got {got!r}")
        print(f"  BAD {label} — expected {want!r}, got {got!r}")


# ── Check 1 — client.classify_response ──────────────────────────────────────

def test_classify_response():
    print("\nclassify_response turns a VRM reply into PASS/EMPTY/ERROR")
    check("a connection exception is ERROR",
          client.classify_response(None, None, ConnectionError("refused"))[0], client.ERROR)
    check("a non-2xx status is ERROR",
          client.classify_response(500, {"data": []}, None)[0], client.ERROR)
    check("a non-dict payload is ERROR",
          client.classify_response(200, ["not", "a", "dict"], None)[0], client.ERROR)
    check("a body with no 'data' field is ERROR",
          client.classify_response(200, {"metaData": {}}, None)[0], client.ERROR)
    check("2xx with an empty data list is EMPTY",
          client.classify_response(200, {"metaData": {}, "data": []}, None)[0], client.EMPTY)
    check("2xx with a non-empty data list is PASS",
          client.classify_response(200, {"metaData": {}, "data": [1, 2]}, None)[0], client.PASS)
    _status, count, _detail, _sample = client.classify_response(
        200, {"metaData": {}, "data": [1, 2, 3]}, None)
    check("row_count reflects the data length", count, 3)

    print("\n...and the sample is the real response, not just a verdict")
    sample = client.classify_response(
        200, {"metaData": {}, "data": [{"a": 1}, {"a": 2}]}, None)[3]
    check("a PASS sample is the actual data rows", sample, [{"a": 1}, {"a": 2}])
    big = [{"i": i} for i in range(20)]
    truncated = client.classify_response(200, {"metaData": {}, "data": big}, None)[3]
    check("a long sample is truncated so the report stays readable",
          len(truncated), 5)
    error_sample = client.classify_response(
        500, {"error": "missing required field 'scope'"}, None)[3]
    check("an ERROR sample still carries the body — often names the real cause",
          error_sample, {"error": "missing required field 'scope'"})


# ── Check 2 — index_check ────────────────────────────────────────────────────

def test_covers():
    print("\n_covers checks whether an index's window overlaps the requested range")
    check("unknown slicing always counts as covering — never wrongly exclude",
          index_check._covers(None, 100, 200), True)
    check("a window fully inside the range covers",
          index_check._covers((120, 180), 100, 200), True)
    check("a window overlapping the start covers",
          index_check._covers((50, 150), 100, 200), True)
    check("a window overlapping the end covers",
          index_check._covers((150, 250), 100, 200), True)
    check("a window entirely before the range does not cover",
          index_check._covers((0, 50), 100, 200), False)
    check("a window entirely after the range does not cover",
          index_check._covers((250, 300), 100, 200), False)
    check("a window that only touches the boundary does not cover",
          index_check._covers((0, 100), 100, 200), False)


def test_classify_prefix():
    print("\nclassify_prefix turns filtered ES rows into MISSING/EXISTS_EMPTY/EXISTS")
    check("no matching indices at all is MISSING",
          index_check.classify_prefix([], [])[0], index_check.MISSING)
    matches = [{"index": "x", "docs.count": 0}]
    check("matches exist but none cover the window is EXISTS_EMPTY",
          index_check.classify_prefix(matches, [])[0], index_check.EXISTS_EMPTY)
    covering_empty = [{"index": "x", "docs.count": 0}]
    check("a covering index with zero docs is EXISTS_EMPTY",
          index_check.classify_prefix(matches, covering_empty)[0], index_check.EXISTS_EMPTY)
    covering_with_docs = [{"index": "x", "docs.count": 42}]
    status, count = index_check.classify_prefix(matches, covering_with_docs)
    check("a covering index with docs is EXISTS", status, index_check.EXISTS)
    check("doc_count is summed across covering indices", count, 42)


# ── Check 3, Tier A — device_check ───────────────────────────────────────────

def test_classify_task_status():
    print("\nclassify_task_status collapses TaskStatus to what a diagnosis needs")
    check("no row at all is NO_DATA — a distinct finding from FAIL",
          device_check.classify_task_status(None), device_check.NO_DATA)
    check("SUCCESS", device_check.classify_task_status({"task_status": "SUCCESS"}),
          device_check.SUCCESS)
    check("FAIL", device_check.classify_task_status({"task_status": "FAIL"}),
          device_check.FAIL)
    for mid in ("ENQUEUED", "STARTED", "IN_PROGRESS"):
        check(f"{mid} is UNKNOWN — not yet resolved, not a guess",
              device_check.classify_task_status({"task_status": mid}), device_check.UNKNOWN)
    check("an unrecognised status string is UNKNOWN, never a guess",
          device_check.classify_task_status({"task_status": "SOMETHING_NEW"}),
          device_check.UNKNOWN)


# ── Check 3, Tier B — device_probe ───────────────────────────────────────────

def _aes_encrypt(plaintext: str, key: bytes) -> str:
    """Test-only encryptor — the exact reverse of decrypt_password — so this
    file can prove the decrypt path round-trips without ever touching a real
    device credential or the real AES key."""
    from cryptography.hazmat.primitives import padding as _padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    padder = _padding.PKCS7(128).padder()
    padded = padder.update(plaintext.encode("utf-8")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(ciphertext).decode()


def test_decrypt_password():
    print("\ndecrypt_password reverses the ENC<digit><millis>_<password> scheme")
    key = os.urandom(32)

    wrapped = _aes_encrypt("ENC1700000000000_hunter2", key)
    check("round-trips a wrapped password",
          device_probe.decrypt_password(wrapped, key), "hunter2")

    unwrapped = _aes_encrypt("plain-text-account", key)
    check("a decrypted value without the ENC prefix passes through unchanged",
          device_probe.decrypt_password(unwrapped, key), "plain-text-account")

    check("non-base64 input passes through unchanged rather than raising",
          device_probe.decrypt_password("not base64 at all !!", key),
          "not base64 at all !!")
    check("empty input passes through unchanged",
          device_probe.decrypt_password("", key), "")

    wrong_key = os.urandom(32)
    result = device_probe.decrypt_password(wrapped, wrong_key)
    check("the wrong key never raises — falls back to the original value",
          isinstance(result, str), True)


def test_classify_probe():
    print("\nclassify_probe turns a device response into HTTP_ERROR/EMPTY/DATA")
    check("no HTTP status is HTTP_ERROR",
          device_probe.classify_probe(None, None)[0], device_probe.HTTP_ERROR)
    check("a non-2xx status is HTTP_ERROR",
          device_probe.classify_probe(500, {"a": 1})[0], device_probe.HTTP_ERROR)
    check("2xx with no body is EMPTY",
          device_probe.classify_probe(200, None)[0], device_probe.EMPTY)
    check("2xx with an empty list is EMPTY",
          device_probe.classify_probe(200, [])[0], device_probe.EMPTY)
    check("2xx with a non-empty list is DATA",
          device_probe.classify_probe(200, [{"x": 1}])[0], device_probe.DATA)
    check("2xx with an empty dict is EMPTY",
          device_probe.classify_probe(200, {})[0], device_probe.EMPTY)
    check("2xx with a non-empty dict is DATA",
          device_probe.classify_probe(200, {"records": [1]})[0], device_probe.DATA)
    _status, sample = device_probe.classify_probe(200, [1, 2, 3, 4, 5, 6, 7, 8])
    check("a list sample is truncated rather than returning the full body",
          len(sample), 5)


def test_version_at_least():
    print("\n_version_at_least compares dotted version strings, or skips honestly")
    check("equal versions", device_probe._version_at_least("10.1.0.0", "10.1.0.0"), True)
    check("newer is at least", device_probe._version_at_least("10.12.0.0", "10.1.0.0"), True)
    check("older is not at least", device_probe._version_at_least("9.0.0.0", "10.1.0.0"), False)
    check("an unparseable version skips the check (None), never a guess",
          device_probe._version_at_least("not-a-version", "10.1.0.0"), None)


def test_executor_api_map_is_a_fixed_table():
    print("\nEXECUTOR_API_MAP matches the confirmed DefensePro device REST contract")
    expected = {
        "DP_ATTACK_DATA":            "/v1/attack-data",
        "DP_APPLICATIONS":           "/v2/traffic/application",
        "DP_TRAFFIC_DATA":           "/v1/traffic/policy",
        "DP_TRAFFIC_BASELINE":       "/v1/traffic/application/characteristics",
        "DP_TRAFFIC_STATISTICS":     "/v2/traffic/statistics",
        "WEB_DDOS_REAL_TIME_ATTACK": "/v1/traffic/attack/characteristics",
    }
    for executor, path in expected.items():
        check(f"{executor} -> {path}", device_probe.EXECUTOR_API_MAP[executor][0], path)


# ── The rollup ────────────────────────────────────────────────────────────────

def test_diagnose():
    print("\ndiagnose() rolls the checks into one severity and one sentence")

    def api(status, detail=""):
        return {"status": status, "detail": detail}

    def idx(status, doc_count=0):
        return [{"prefix": "x", "status": status, "doc_count": doc_count, "indices": []}]

    def dev(status):
        return [{"device_ip": "1.2.3.4", "status": status}]

    check("PASS is ok",
          diagnosis.diagnose(api("PASS"), idx("EXISTS", 5), dev("SUCCESS"))["severity"],
          diagnosis.OK)
    check("PASS is ok with no index rows to check either",
          diagnosis.diagnose(api("PASS"), [], dev("SUCCESS"))["severity"],
          diagnosis.OK)
    result = diagnosis.diagnose(api("PASS"), idx("EXISTS_EMPTY"), dev("SUCCESS"))
    check("PASS + an empty index is a contradiction, not a clean ok — confirmed live",
          result["severity"], diagnosis.WARN)
    check("...and the headline names the contradiction",
          "index" in result["headline"], True)
    check("PASS + MISSING index is the same contradiction",
          diagnosis.diagnose(api("PASS"), idx("MISSING"), dev("SUCCESS"))["severity"],
          diagnosis.WARN)
    check("ERROR is crit",
          diagnosis.diagnose(api("ERROR"), [], [])["severity"], diagnosis.CRIT)
    check("no api result at all is unknown",
          diagnosis.diagnose({}, [], [])["severity"], diagnosis.UNKNOWN)
    check("EMPTY + MISSING is crit",
          diagnosis.diagnose(api("EMPTY"), idx("MISSING"), [])["severity"], diagnosis.CRIT)
    check("EMPTY + EXISTS_EMPTY + FAIL is crit",
          diagnosis.diagnose(api("EMPTY"), idx("EXISTS_EMPTY"), dev("FAIL"))["severity"],
          diagnosis.CRIT)
    check("EMPTY + EXISTS_EMPTY + NO_DATA is warn",
          diagnosis.diagnose(api("EMPTY"), idx("EXISTS_EMPTY"), dev("NO_DATA"))["severity"],
          diagnosis.WARN)
    check("EMPTY + EXISTS_EMPTY + SUCCESS is warn",
          diagnosis.diagnose(api("EMPTY"), idx("EXISTS_EMPTY"), dev("SUCCESS"))["severity"],
          diagnosis.WARN)
    check("EMPTY + EXISTS(docs) is warn — query/scope suspected",
          diagnosis.diagnose(api("EMPTY"), idx("EXISTS", 10), [])["severity"], diagnosis.WARN)

    print("\n...and the probe-aware rows only fire once Tier B was explicitly run")
    probe_data = {"status": "DATA"}
    result = diagnosis.diagnose(api("EMPTY"), idx("EXISTS_EMPTY"), dev("SUCCESS"), probe_data)
    check("probe DATA + index empty + Tier A success is crit", result["severity"], diagnosis.CRIT)
    check("...and the headline suspects the persist-service, not collection",
          "persist-service" in result["headline"], True)

    result = diagnosis.diagnose(api("EMPTY"), idx("EXISTS_EMPTY"), dev("FAIL"), probe_data)
    check("probe DATA + index empty + Tier A fail is crit", result["severity"], diagnosis.CRIT)
    check("...and the headline suspects data-polling-scheduler, not persistence",
          "data-polling" in result["headline"], True)

    probe_empty = {"status": "EMPTY"}
    result = diagnosis.diagnose(api("EMPTY"), idx("EXISTS_EMPTY"), dev("SUCCESS"), probe_empty)
    check("probe EMPTY traces the empty widget back to the source, not the pipeline",
          result["severity"], diagnosis.WARN)

    without_probe = diagnosis.diagnose(api("EMPTY"), idx("EXISTS_EMPTY"), dev("SUCCESS"))
    check("without a probe result, the plain Tier-A-only branch still applies",
          without_probe["severity"], diagnosis.WARN)


# ── The catalog ───────────────────────────────────────────────────────────────

# From kvision_libs' ExecutorType enum (see WIDGET_DEBUG_HELPER.md and
# kvision_data_polling_scheduler's ExecutorType.java) — every non-empty
# executor_type in the catalog must be one of these, or a typo would silently
# make Check 3 (and any future Tier B probe) look for a device that never runs.
_KNOWN_EXECUTOR_TYPES = {
    "DP_TRAFFIC_DATA", "DP_ATTACK_DATA", "DP_APPLICATIONS",
    "FLOW_DETECTOR_ATTACK_DATA", "FLOW_DETECTOR_TRAFFIC_DATA_NETWORK_ANALYTICS",
    "FLOW_DETECTOR_TRAFFIC_DATA_TOP_TALKERS", "NETWORK_ELEMENT_MONITORING_DATA",
    "CLEANUP", "CDDOS_CONNECTOR_STATUS_DATA", "WEB_DDOS_REAL_TIME_ATTACK",
    "DP_TRAFFIC_BASELINE", "DP_TRAFFIC_STATISTICS",
    "FLOW_DETECTOR_TRAFFIC_DATA_HOSTGROUP_COUNTERS",
}


def test_catalog_is_a_fixed_table():
    print("\nthe widget catalog is a self-describing, internally consistent table")
    check("there are widgets", len(catalog.WIDGET_CATALOG) > 0, True)
    check("an unknown id resolves to nothing", catalog.describe("no-such-widget"), None)

    for w in catalog.WIDGET_CATALOG.values():
        check(f"{w.id} has a real api_path", w.api_path.startswith("/"), True)
        check(f"{w.id} names its controller", bool(w.controller.strip()), True)
        if w.executor_type:
            check(f"{w.id}'s executor_type is a real ExecutorType",
                  w.executor_type in _KNOWN_EXECUTOR_TYPES, True)
        if not w.confirmed:
            check(f"{w.id} explains what is unconfirmed", bool(w.notes.strip()), True)

    print("\nby_domain() follows category_order, appending unlisted domains alphabetically")
    grouped = catalog.by_domain()
    known_prefix = list(grouped.keys())[:len(catalog.CATEGORIES)]
    check("known domains keep the declared order", known_prefix, list(catalog.CATEGORIES))


def test_load_skips_malformed_rows():
    print("\na malformed catalog row is skipped, never a startup crash "
          "(this catalog backs a capability that ships on in both profiles)")
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "widget_catalog.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({
                "schema_version": 1,
                "category_order": ["X"],
                "widgets": {
                    "good": {"label": "Good", "domain": "X", "controller": "C", "api_path": "/x"},
                    "missing-label": {"domain": "X", "controller": "C", "api_path": "/y"},
                    "not-an-object": "oops",
                },
            }, fh)
        original_path = catalog._CATALOG_FILE
        catalog._CATALOG_FILE = path
        try:
            widgets, order = catalog._load()
        finally:
            catalog._CATALOG_FILE = original_path

    check("the well-formed row loads", "good" in widgets, True)
    check("a row missing a required field is skipped, not crashing the load",
          "missing-label" in widgets, False)
    check("a non-object row is skipped too", "not-an-object" in widgets, False)
    check("category_order still comes through", order, ("X",))


def main() -> int:
    test_classify_response()
    test_covers()
    test_classify_prefix()
    test_classify_task_status()
    test_decrypt_password()
    test_classify_probe()
    test_version_at_least()
    test_executor_api_map_is_a_fixed_table()
    test_diagnose()
    test_catalog_is_a_fixed_table()
    test_load_skips_malformed_rows()

    print("\n" + "=" * 66)
    if _FAILURES:
        print("FAILURES:")
        for f in _FAILURES:
            print(" - " + f)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
