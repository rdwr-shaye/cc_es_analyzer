"""
Live "possible indices" discovery — the API port of scripts/live_index_discovery.py
(see docs/LIVE_INDEX_DISCOVERY.md for the method and its validation).

Builds the complete catalog of index families a CC machine can ever create —
including families with no live index yet — with the correct slice duration,
doc types, and field mappings, from four REST calls:

  1. /_template                     -> families + field mappings (the schema)
  2. /<appconfig*>/_search          -> configured slice sizes (defaults + overrides)
  3. /_cat/indices (+creation.date) -> empirical slice measurement + live examples
  4. /                              -> cluster identity for the _meta block

All calls go through the app's ESHttpClient so the auth/TLS/connection settings
from /api/connect apply (the standalone script uses raw urllib instead).

Gotchas honoured (docs/LIVE_INDEX_DISCOVERY.md §8):
  * template NAMES are not a reliable docType source (product wiring bug) —
    constructed names use the pattern's -ty- token, else docType = prefix;
  * live index names always win over anything constructed;
  * "-pt-<n>" partition suffixes are stripped before parsing "sl";
  * slice sizes are runtime-overridable — the catalog is cached PER CLUSTER
    (base_url) with a short TTL, never reused across machines.
"""
import fnmatch
import logging
import re
import threading
import time
from collections import defaultdict

logger = logging.getLogger(__name__)

STD_SLICES_MIN = [5, 20, 60, 720, 1440, 10080, 20160, 43200, 129600, 259200, 525600]

UNSLICED = {
    "dp-https-server", "dp-auth-table-status", "alert", "audit", "appconfig",
    "snapshot-definition", "forensics-definition", "rt-alert-def", "rt-alert-def-vrm",
    "user-activity-log", "vrm-scheduled-report-definition", "vrm-scheduled-report-result",
}

# appconfig section -> (key-role -> family prefix) for families that may have no
# live index. Key roles: raw / five_min / hourly / daily (see _config_slices).
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

_LIVE_NAME_RE = re.compile(r"(.+?)-ty-(.+?)-sid-(\w+)(?:-open)?-sl-(\d+)(?:-pt-(\d+))?$")

# ── Per-cluster cache ────────────────────────────────────────────────────────
# Slice sizes are runtime-overridable and examples embed "now", so entries age
# out quickly; a refresh=True call bypasses the cache entirely.
_CACHE_TTL_S = 300
_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def _nearest_std(minutes: float) -> int:
    return min(STD_SLICES_MIN, key=lambda s: abs(s - minutes) / s)


def _flatten(props: dict, parent: str = "") -> dict:
    out: dict = {}
    for name, spec in props.items():
        if not isinstance(spec, dict):
            continue
        path = f"{parent}.{name}" if parent else name
        if "properties" in spec:
            out.update(_flatten(spec["properties"], path))
        else:
            out[path] = spec.get("type", "object")
    return out


def _empirical_slices(es) -> dict:
    """prefix -> (slice_minutes, latest_example) measured from live creation dates."""
    rows = es.get("/_cat/indices", params={"format": "json", "h": "index,creation.date"})
    by_prefix: dict[str, list] = defaultdict(list)
    for row in rows:
        name, created = row.get("index", ""), row.get("creation.date")
        if not name or name.startswith(".") or not created:
            continue
        m = _LIVE_NAME_RE.match(name)
        if m and int(m.group(4)) > 0:
            by_prefix[m.group(1)].append((int(m.group(4)), int(created), name))
    result = {}
    for prefix, entries in by_prefix.items():
        est = min(created / (sl * 60000) for sl, created, _n in entries)
        result[prefix] = (_nearest_std(est), sorted(entries)[-1][2])
    return result


def _config_slices(es) -> dict:
    """family prefix -> (slice_minutes, 'config <section>.<key>') from appconfig."""
    appconfig_idx = None
    try:
        for row in es.get("/_cat/indices", params={"format": "json", "h": "index"}):
            if "appconfig" in row.get("index", ""):
                appconfig_idx = row["index"]
                break
        if not appconfig_idx:
            return {}
        docs = es.search(appconfig_idx, {"size": 100})["hits"]["hits"]
    except Exception as exc:
        logger.warning("[discovery] appconfig read failed: %s", exc)
        return {}
    out: dict = {}
    for hit in docs:
        for sname, section in (hit.get("_source", {})
                               .get("configurationSections", {}) or {}).items():
            fam = SECTION_FAMILIES.get(sname)
            if not fam or not isinstance(section, dict):
                continue
            for key, rec in (section.get("configurationRecords", {}) or {}).items():
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


def discover(es, refresh: bool = False) -> dict:
    """Full possible-indices catalog for the cluster behind *es*.

    Returns {"_meta": {...}, "families": {family: entry}} where each entry has
    index_pattern, doc_types, slice_minutes, slice_source, live_example,
    example_now, fields, field_count, template_names (+ optional doc_type_note).
    Cached per base_url for a short TTL; refresh=True re-runs the discovery.
    """
    key = getattr(es, "base_url", "?")
    if not refresh:
        with _cache_lock:
            hit = _cache.get(key)
        if hit and time.time() - hit[0] < _CACHE_TTL_S:
            return hit[1]

    info = es.info()
    emp = _empirical_slices(es)
    cfg = _config_slices(es)
    templates = es.get("/_template", params={})

    catalog: dict = {}
    for tname, t in sorted(templates.items()):
        if not isinstance(t, dict):
            continue
        patterns = t.get("index_patterns") or ([t["template"]] if "template" in t else [])
        pattern = patterns[0] if patterns else ""
        doc_type = tname.split("-ty-", 1)[1] if "-ty-" in tname else None
        prefix = re.sub(r"-?\*$", "", pattern)
        family = re.sub(r"-ty-.*$", "", prefix)
        fields = _flatten(t.get("mappings", {}).get("properties", {}))

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

    # Live prefixes whose template pattern has a mid-name wildcard (e.g.
    # "dp*-baseline-portion-*") don't join by family key — add them as
    # first-class entries, matching fields via the pattern.
    for prefix, (slice_min, example) in emp.items():
        if prefix in catalog:
            continue
        fields, tnames, pattern = {}, [], ""
        probe = f"{prefix}-ty-x-sid-0-sl-1"
        for tname, t in templates.items():
            if not isinstance(t, dict):
                continue
            pats = t.get("index_patterns") or ([t["template"]] if "template" in t else [])
            if any(fnmatch.fnmatch(probe, p)
                   or fnmatch.fnmatch(prefix, p.rstrip("*").rstrip("-") + "*")
                   for p in pats):
                cand = _flatten(t.get("mappings", {}).get("properties", {}))
                if len(cand) > len(fields):
                    fields, pattern = cand, pats[0]
                tnames.append(tname)
        doc_type = re.match(r".+-ty-(.+?)-sid-", example)
        catalog[prefix] = {
            "index_pattern": pattern or f"{prefix}-*",
            "doc_types": [doc_type.group(1)] if doc_type else [prefix],
            "slice_minutes": slice_min, "slice_source": "empirical (live indices)",
            "live_example": example, "fields": fields, "field_count": len(fields),
            "template_names": tnames}

    # Every family gets an example name usable today: the latest LIVE index when
    # one exists, otherwise a CONSTRUCTED name (what ES will create on first write).
    now_ms = int(time.time() * 1000)
    for family, entry in catalog.items():
        entry["example_now"] = entry.get("live_example")
        if entry["example_now"]:
            continue
        stripped = re.sub(r"-?\*$", "", entry["index_pattern"])
        prefix = re.sub(r"-ty-.*$", "", stripped)
        if "*" in prefix:
            continue  # mid-name wildcard: cannot construct reliably
        # Doc type for construction: the pattern's own -ty- token (per-category
        # attack templates) or the prefix (writer default). NEVER the template
        # NAME's -ty- token — a product wiring bug registers some templates
        # under an unrelated type while real indices use the prefix.
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

    # Per-template pattern -> fields, for doc-type-specific lookups: sibling
    # templates of one family DIFFER (e.g. dp-attack-raw-ty-https__flood has 20
    # staticData.* fields that -ty-intrusions lacks), while the family entry
    # above keeps only the LARGEST set. Internal — not part of the API payload.
    pattern_fields = []
    for _tname, t in templates.items():
        if not isinstance(t, dict):
            continue
        pats = t.get("index_patterns") or ([t["template"]] if "template" in t else [])
        fields = _flatten(t.get("mappings", {}).get("properties", {}))
        for p in pats:
            if fields:
                pattern_fields.append((p, fields))

    result = {
        "_meta": {
            "source": f"{key} cluster {info.get('cluster_name')}",
            "es_version": info.get("version", {}).get("number"),
            "generated": time.strftime("%Y-%m-%d %H:%M"),
            "name_formula": ("<prefix>[-<tenant>]-ty-<docType>-sid-<serverId>"
                             "-sl-<floor(timestampMs/(slice_minutes*60000))>[-pt-<n>]"),
            "families": len(catalog),
        },
        "families": catalog,
        "_pattern_fields": pattern_fields,
    }
    with _cache_lock:
        _cache[key] = (time.time(), result)
    logger.info("[discovery] %s: %s families, %s with slice size", key, len(catalog),
                sum(1 for v in catalog.values() if v["slice_minutes"]))
    return result


def slice_for_index(es, index_name: str) -> tuple[int | None, str | None]:
    """(slice_minutes, slice_source) for a concrete index name via the catalog.

    Family = the part before "-ty-" (the name grammar), looked up exactly and
    then against wildcard family keys. Returns (None, None) when the family is
    not in the catalog at all — callers should surface a guess, not hide it.
    """
    family = index_name.split("-ty-")[0] if "-ty-" in index_name else index_name
    try:
        catalog = discover(es)["families"]
    except Exception as exc:
        logger.warning("[discovery] catalog unavailable for %r: %s", index_name, exc)
        return None, None
    entry = catalog.get(family)
    if entry is None:
        for fam_key, cand in catalog.items():
            if "*" in fam_key and fnmatch.fnmatch(family, fam_key):
                entry = cand
                break
    if entry is None:
        return None, None
    return entry.get("slice_minutes"), entry.get("slice_source")


def catalog_entry_for_index(es, index_name: str) -> dict | None:
    """The catalog entry whose family matches *index_name*, or None.

    The family entry carries the LARGEST field set across the family's sibling
    templates, but per-doc-type templates differ (intrusions: 46 fields,
    https__flood: 65). So when a template pattern matches the concrete index
    name, the returned entry's fields are replaced with that template's own —
    the most specific (longest) matching pattern wins, exactly the template ES
    itself would apply when the index is first written.
    """
    family = index_name.split("-ty-")[0] if "-ty-" in index_name else index_name
    try:
        cat = discover(es)
    except Exception:
        return None
    catalog = cat["families"]
    entry = catalog.get(family)
    if entry is None:
        for fam_key, cand in catalog.items():
            if "*" in fam_key and fnmatch.fnmatch(family, fam_key):
                entry = cand
                break
    if entry is None:
        return None
    best = None
    for pattern, fields in cat.get("_pattern_fields", []):
        if fnmatch.fnmatch(index_name, pattern):
            if best is None or len(pattern) > len(best[0]):
                best = (pattern, fields)
    if best:
        entry = {**entry, "fields": best[1], "field_count": len(best[1]),
                 "fields_template_pattern": best[0]}
    return entry
