#!/usr/bin/env python3
"""Build the possible-indices catalog from a live CC Elasticsearch/OpenSearch machine.

Implements docs/LIVE_INDEX_DISCOVERY.md: templates -> families + field mappings,
appconfig -> slice sizes, live indices -> empirical slice validation + examples.

Usage:
    python scripts/live_index_discovery.py [host] [port] [outfile]
    python scripts/live_index_discovery.py 10.205.189.20 9200 possible_indices_catalog.json

Stdlib only — no dependencies.
"""
import json
import re
import sys
import time
import urllib.request
from collections import defaultdict

STD_SLICES_MIN = [5, 20, 60, 720, 1440, 10080, 20160, 43200, 129600, 259200, 525600]

UNSLICED = {
    "dp-https-server", "dp-auth-table-status", "alert", "audit", "appconfig",
    "snapshot-definition", "forensics-definition", "rt-alert-def", "rt-alert-def-vrm",
    "user-activity-log", "vrm-scheduled-report-definition", "vrm-scheduled-report-result",
}

# appconfig section -> (family prefix, key-role -> prefix) mapping for families that may
# have no live index. Key roles: raw / five_min / hourly / daily (see resolve_config_slices).
SECTION_FAMILIES = {
    "dpTraffic": {"raw": "dp-traffic-raw", "five_min": "dp-traffic-five-min-agg",
                  "hourly": "dp-traffic-agg", "daily": "dp-traffic-dailyagg"},
    "dfTraffic": {"raw": "df-traffic-raw", "five_min": "df-traffic-five-min-agg",
                  "hourly": "df-traffic-agg", "daily": "df-traffic-dailyagg"},
    "dpAttack": {"raw": "dp-attack-raw"},
    "dfAttack": {"raw": "df-attack-raw"},
    "genericAttackData": {"raw": "attack-data", "five_min": "attack-five-min-data",
                          "hourly": "attack-hourly-data", "daily": "attack-daily-data"},
    "trafficData": {"raw": "traffic-data", "five_min": "traffic-five-min-data",
                    "hourly": "traffic-hourly-data", "daily": "traffic-daily-data"},
    "eaafAttackData": {"raw": "eaaf-attack-data", "hourly": "eaaf-attack-hourly-data",
                       "daily": "eaaf-attack-daily-data"},
    "connectionstatistics": {"raw": "dp-connection-statistics",
                             "five_min": "dp-five-min-connection-statistics",
                             "hourly": "dp-hourly-connection-statistics",
                             "daily": "dp-daily-connection-statistics"},
    "ConcurrentConnectionsConfigurationSection": {
        "raw": "dp-concurrent-connections", "five_min": "dp-five-min-concurrent-connections",
        "hourly": "dp-hourly-concurrent-connections", "daily": "dp-daily-concurrent-connections"},
    "DPHttpsDataConfigurationSection": {"raw": "dp-https-rt", "five_min": "dp-five-min-https-rt",
                                        "hourly": "dp-hourly-https-rt", "daily": "dp-daily-https-rt"},
    "outOfState": {"raw": "dp-out-of-state-ts", "five_min": "dp-five-min-out-of-state-ts",
                   "hourly": "dp-hourly-out-of-state-ts", "daily": "dp-daily-out-of-state-ts"},
    "topTalkers": {"raw": "top-talkers"},
    "qdosStatus": {"raw": "dp-qdos-status-raw"},
    "qdosAttackCharacteristics": {"raw": "dp-qdos-attack-characteristics-raw"},
    "timeSeriesDpAttack": {"raw": "dp-ts-attack-raw"},
    "attacksExtraInfo": {"raw": "dp-attack-extra-info-ts-raw"},
    "DataInsight": {"raw": "data-insight-raw"},
    "DpAutoEscalation": {"raw": "dp-auto-escalation-ts-raw"},
    "webDdosRealTimeAttack": {"raw": "web-ddos-real-time-attack-ts-raw"},
    "socxEvent": {"raw": "socx-event"},
    "socxRecommendation": {"raw": "socx-recommendation"},
    "dpHttpTrafficStatistics": {"raw": "dp-http-traffic"},
    "detectionEngineBaseline": {"raw": "detection-engine-baseline-hourly"},
    "appWallLearning": {"raw": "appwall-learning"},
    "appWallTraffic": {"raw": "appwall-raw"},
    "AppWallWebApplicationConfigurationSection": {"raw": "aw-web-application"},
    "DFProtectedObjectConfigurationSection": {"raw": "df-protected-object"},
    "dfActivation": {"raw": "df-activation", "daily": "df-daily-activation"},
    "dfAttackStoryActivation": {"raw": "df-attackstory-activation"},
    "dfAttackStoryProtection": {"raw": "df-attackstory-protection"},
    "systemAttack": {"raw": "system-attack"},
    "systemAttackV2": {"raw": "system-v2-attack"},
    "systemTraffic": {"raw": "system-traffic"},
    "systemTrafficV2": {"raw": "system-v2-traffic"},
    "nfDump": {"raw": "nfdump"},
    "malwareFileScanning": {"raw": "malware-file-scanning"},
    "RtAlertJournaling": {"raw": "rt-alert-journaling"},
    "alteonStatistics": {"raw": "alteon-statistics"},
}


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=30) as r:
        return r.read().decode()


def nearest_std(minutes):
    return min(STD_SLICES_MIN, key=lambda s: abs(s - minutes) / s)


def flatten(props, parent=""):
    out = {}
    for name, spec in props.items():
        path = f"{parent}.{name}" if parent else name
        if "properties" in spec:
            out.update(flatten(spec["properties"], path))
        else:
            out[path] = spec.get("type", "object")
    return out


def empirical_slices(base):
    """prefix -> (slice_minutes, latest_example) measured from live index creation dates."""
    by_prefix = defaultdict(list)
    for line in get(base, "/_cat/indices?h=index,creation.date").splitlines():
        parts = line.split()
        if len(parts) != 2 or parts[0].startswith("."):
            continue
        m = re.match(r"(.+?)-ty-(.+?)-sid-(\w+)(?:-open)?-sl-(\d+)(?:-pt-(\d+))?$", parts[0])
        if m and int(m.group(4)) > 0:
            by_prefix[m.group(1)].append((int(m.group(4)), int(parts[1]), parts[0]))
    result = {}
    for prefix, entries in by_prefix.items():
        est = min(created / (sl * 60000) for sl, created, _name in entries)
        result[prefix] = (nearest_std(est), sorted(entries)[-1][2])
    return result


def resolve_config_slices(base):
    """family prefix -> (slice_minutes, 'config <section>.<key>') from the appconfig index."""
    appconfig_idx = None
    for line in get(base, "/_cat/indices?h=index").splitlines():
        if "appconfig" in line:
            appconfig_idx = line.strip()
            break
    if not appconfig_idx:
        return {}
    docs = json.loads(get(base, f"/{appconfig_idx}/_search?size=100"))["hits"]["hits"]
    out = {}
    for hit in docs:
        for sname, section in hit["_source"].get("configurationSections", {}).items():
            fam = SECTION_FAMILIES.get(sname)
            if not fam:
                continue
            for key, rec in section.get("configurationRecords", {}).items():
                if "index" not in key.lower() or "max" in key.lower():
                    continue
                if not re.search(r"size|days|min", key, re.I):
                    continue
                k = key.lower()
                role = ("daily" if "daily" in k else "five_min" if "five_min" in k
                        else "hourly" if "hourly" in k or "aggregation" in k else "raw")
                prefix = fam.get(role)
                if not prefix or not isinstance(rec.get("value"), int):
                    continue
                minutes = rec["value"] * (1440 if k.endswith("days") else 1)
                out.setdefault(prefix, (minutes, f"config {sname}.{key}"))
    return out


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "localhost"
    port = sys.argv[2] if len(sys.argv) > 2 else "9200"
    outfile = sys.argv[3] if len(sys.argv) > 3 else "possible_indices_catalog.json"
    base = f"http://{host}:{port}"

    info = json.loads(get(base, "/"))
    emp = empirical_slices(base)
    cfg = resolve_config_slices(base)
    templates = json.loads(get(base, "/_template"))

    catalog = {}
    for tname, t in sorted(templates.items()):
        patterns = t.get("index_patterns", [])
        pattern = patterns[0] if patterns else ""
        doc_type = tname.split("-ty-", 1)[1] if "-ty-" in tname else None
        prefix = re.sub(r"-?\*$", "", pattern)
        family = re.sub(r"-ty-.*$", "", prefix)
        fields = flatten(t.get("mappings", {}).get("properties", {}))

        if family in emp:
            slice_min, src = emp[family][0], "empirical (live indices)"
        elif family in cfg:
            slice_min, src = cfg[family]
        elif family in UNSLICED or prefix in UNSLICED:
            slice_min, src = None, "unsliced (single definition-style index)"
        else:
            slice_min, src = None, "unknown — no live index, no config record"

        entry = catalog.setdefault(family or tname, {
            "index_pattern": pattern, "doc_types": [], "slice_minutes": slice_min,
            "slice_source": src, "live_example": emp.get(family, (None, None))[1],
            "fields": fields, "field_count": len(fields), "template_names": []})
        if doc_type and doc_type not in entry["doc_types"]:
            entry["doc_types"].append(doc_type)
        entry["template_names"].append(tname)
        if len(fields) > entry["field_count"]:
            entry["fields"], entry["field_count"] = fields, len(fields)

    # Live prefixes whose template pattern has a mid-name wildcard (e.g. "dp*-baseline-portion-*")
    # don't join by family key — add them as first-class entries, matching fields via the pattern.
    import fnmatch
    for prefix, (slice_min, example) in emp.items():
        if prefix in catalog:
            continue
        fields, tnames, pattern = {}, [], ""
        probe = f"{prefix}-ty-x-sid-0-sl-1"
        for tname, t in templates.items():
            pats = t.get("index_patterns", [])
            if any(fnmatch.fnmatch(probe, p) or fnmatch.fnmatch(prefix, p.rstrip("*").rstrip("-") + "*")
                   for p in pats):
                cand = flatten(t.get("mappings", {}).get("properties", {}))
                if len(cand) > len(fields):
                    fields, pattern = cand, pats[0]
                tnames.append(tname)
        doc_type = re.match(r".+-ty-(.+?)-sid-", example)
        catalog[prefix] = {
            "index_pattern": pattern or f"{prefix}-*", "doc_types":
                [doc_type.group(1)] if doc_type else [prefix],
            "slice_minutes": slice_min, "slice_source": "empirical (live indices)",
            "live_example": example, "fields": fields, "field_count": len(fields),
            "template_names": tnames}

    # Every family gets an example name usable today: the latest LIVE index when one exists,
    # otherwise a CONSTRUCTED name from doc type + slice size (what ES will create on first write).
    now_ms = int(time.time() * 1000)
    for family, entry in catalog.items():
        entry["example_now"] = entry.get("live_example")
        if entry["example_now"]:
            continue
        stripped = re.sub(r"-?\*$", "", entry["index_pattern"])
        prefix = re.sub(r"-ty-.*$", "", stripped)
        if "*" in prefix:
            continue  # mid-name wildcard: cannot construct reliably
        # Doc type for construction: the pattern's own -ty- token (per-category attack
        # templates) or the prefix (writer default). NEVER the template NAME's -ty- token —
        # a product wiring bug registers some templates under an unrelated type (e.g. the
        # out-of-state agg templates carry "-ty-dp-attack-raw") while real indices use the prefix.
        m = re.search(r"-ty-(.+)$", stripped)
        doc_type = m.group(1) if m else prefix
        name_ty = (entry.get("doc_types") or [None])[0]
        if name_ty and name_ty not in (doc_type, prefix):
            entry["doc_type_note"] = (f"template name says -ty-{name_ty} but writers use "
                                      f"-ty-{doc_type}; template-name token is unreliable")
        if entry["slice_minutes"]:
            sl = now_ms // (entry["slice_minutes"] * 60000)
            entry["example_now"] = f"{prefix}-ty-{doc_type}-sid-0-sl-{sl}"
        elif "unsliced" in (entry.get("slice_source") or ""):
            entry["example_now"] = prefix

    meta = {"_meta": {
        "source": f"{host}:{port} cluster {info.get('cluster_name')}",
        "es_version": info.get("version", {}).get("number"),
        "generated": time.strftime("%Y-%m-%d %H:%M"),
        "name_formula": ("<prefix>[-<tenant>]-ty-<docType>-sid-<serverId>"
                         "-sl-<floor(timestampMs/(slice_minutes*60000))>[-pt-<n>]"),
        "families": len(catalog),
    }}
    with open(outfile, "w") as f:
        json.dump({**meta, **catalog}, f, indent=1)

    resolved = sum(1 for v in catalog.values() if v["slice_minutes"])
    print(f"{outfile}: {len(catalog)} families, {resolved} with slice size, "
          f"{sum(v['field_count'] for v in catalog.values())} total fields")


if __name__ == "__main__":
    main()
