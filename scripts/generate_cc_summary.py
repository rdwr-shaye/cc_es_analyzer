#!/usr/bin/env python3
"""
Standalone CC Summary Analytics export — techSupport edition.

Produces the SAME JSON the analyzer's "Summary Analytics → Download JSON" button
returns (GET /api/cc/summary/export): attack aggregations, per-category duration
stats, inter-attack gaps, DP traffic, and a per-device breakdown with reverse-DNS
resolution.

It talks straight to Elasticsearch over plain HTTP with ONLY the Python standard
library (no requests / elasticsearch-py / fastapi) so it can run on any CC server
as part of techSupport generation. Defaults to the local ES (localhost:9200);
override with flags or env vars.

    python3 generate_cc_summary.py                 # -> cc_summary_<UTCstamp>.json
    python3 generate_cc_summary.py -o -            # -> stdout
    python3 generate_cc_summary.py --host 10.1.2.3 --port 9200 --output /tmp/s.json
    ES_HOST=... ES_PORT=... ES_USER=... ES_PASSWORD=... python3 generate_cc_summary.py

This is a faithful port of routers/query.py::cc_summary + cc_summary_export; the
query bodies, rounding, and output key order match the service byte-for-byte
(aside from the live `generated_at` timestamp and the `elasticsearch.host` value).
"""
import argparse
import base64
import json
import os
import socket
import ssl
import sys
import urllib.request
from datetime import datetime, timezone


# ── Tiny ES HTTP client (stdlib only) ────────────────────────────────────────

class ES:
    def __init__(self, host, port, scheme="http", user="", password="",
                 verify_certs=False, timeout=60):
        self.base_url = f"{scheme}://{host}:{port}"
        self.timeout = timeout
        self._headers = {"Content-Type": "application/json"}
        if user:
            tok = base64.b64encode(f"{user}:{password}".encode()).decode()
            self._headers["Authorization"] = f"Basic {tok}"
        self._ctx = None
        if scheme == "https" and not verify_certs:
            self._ctx = ssl.create_default_context()
            self._ctx.check_hostname = False
            self._ctx.verify_mode = ssl.CERT_NONE

    def _req(self, method, path, body=None, params=None):
        url = self.base_url + path
        if params:
            from urllib.parse import urlencode
            url += "?" + urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=self._headers)
        with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as r:
            return json.loads(r.read().decode("utf-8"))

    def info(self):
        return self._req("GET", "/")

    def get(self, path, params=None):
        return self._req("GET", path, params=params)

    def search(self, index, body):
        # Mirror the service: always request an exact total-hit count.
        if isinstance(body, dict) and "track_total_hits" not in body:
            body = {**body, "track_total_hits": True}
        return self._req("POST", f"/{index}/_search", body)


# ── Helpers (verbatim from routers/query.py) ─────────────────────────────────

def _val(v):
    if v is None or str(v).strip() in ("N_A", "null", ""):
        return None
    return v


def _round(v, ndigits=2):
    if v is None:
        return None
    try:
        return round(float(v), ndigits)
    except (TypeError, ValueError):
        return None


def _first_bucket(agg_bucket, key):
    for b in agg_bucket.get(key, {}).get("buckets", []):
        v = b.get("key")
        if v and str(v).strip() not in ("N_A", "null", ""):
            return v
    return None


def _ms_to_s(ms):
    if ms is None:
        return None
    try:
        return round(float(ms) / 1000, 1)
    except (TypeError, ValueError):
        return None


def _epoch_to_iso(ms):
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(float(ms) / 1000, tz=timezone.utc).isoformat()
    except Exception:
        return None


def _fmt_stats(stats):
    return {
        "count": stats.get("count"),
        "min_s": _ms_to_s(stats.get("min")),
        "max_s": _ms_to_s(stats.get("max")),
        "avg_s": _ms_to_s(stats.get("avg")),
    }


def _hist_buckets(aggs, key):
    return [
        {"date": b.get("key_as_string"), "ts": b.get("key"), "count": b.get("doc_count")}
        for b in aggs.get(key, {}).get("buckets", [])
        if b.get("doc_count", 0) > 0
    ]


def _compute_gaps(sorted_timestamps):
    if len(sorted_timestamps) < 2:
        return {"attack_count": len(sorted_timestamps), "gap_count": 0,
                "min_s": None, "max_s": None, "avg_s": None}
    gaps_s = [
        (sorted_timestamps[i + 1] - sorted_timestamps[i]) / 1000
        for i in range(len(sorted_timestamps) - 1)
        if sorted_timestamps[i + 1] > sorted_timestamps[i]
    ]
    if not gaps_s:
        return {"attack_count": len(sorted_timestamps), "gap_count": 0,
                "min_s": None, "max_s": None, "avg_s": None}
    return {
        "attack_count": len(sorted_timestamps),
        "gap_count":    len(gaps_s),
        "min_s":  round(min(gaps_s), 1),
        "max_s":  round(max(gaps_s), 1),
        "avg_s":  round(sum(gaps_s) / len(gaps_s), 1),
    }


# ── Summary (port of cc_summary) ─────────────────────────────────────────────

def cc_summary(es):
    ar = es.search("dp-attack-raw-*", {
        "size": 0,
        "aggs": {
            "by_day":   {"date_histogram": {"field": "startTime", "interval": "1d"}},
            "by_week":  {"date_histogram": {"field": "startTime", "interval": "1w"}},
            "by_month": {"date_histogram": {"field": "startTime", "interval": "1M"}},
            "by_category": {"terms": {"field": "category", "size": 20}},
            "by_status":   {"terms": {"field": "status",   "size": 10}},
            "by_risk":     {"terms": {"field": "risk",     "size": 10}},
            "duration_overall": {"stats": {"field": "duration"}},
            "duration_by_cat": {
                "terms": {"field": "category", "size": 20},
                "aggs": {
                    "dur":     {"stats": {"field": "duration"}},
                    "avg_bps": {"avg":  {"field": "averageAttackRateBps"}},
                    "max_bps": {"max":  {"field": "maxAttackRateBps"}},
                },
            },
            "total_packets": {"sum": {"field": "packetCount"}},
            "avg_bw_bps":    {"avg": {"field": "averageAttackRateBps"}},
            "max_bw_bps":    {"max": {"field": "maxAttackRateBps"}},
        },
    })
    a_aggs = ar.get("aggregations", {})
    total_raw = ar.get("hits", {}).get("total")
    total_attacks = total_raw.get("value") if isinstance(total_raw, dict) else total_raw

    gap_resp = es.search("dp-attack-raw-*", {
        "size": 2000,
        "sort": [{"startTime": {"order": "asc"}}],
        "_source": ["startTime", "category"],
        "query": {"match_all": {}},
    })
    gap_data = [
        (h["_source"].get("startTime"), h["_source"].get("category", "Unknown"))
        for h in gap_resp.get("hits", {}).get("hits", [])
        if h.get("_source", {}).get("startTime")
    ]
    overall_gaps = _compute_gaps([t for t, _ in gap_data])

    from collections import defaultdict
    cat_times = defaultdict(list)
    for t, cat in gap_data:
        cat_times[cat].append(t)
    gaps_by_cat = {cat: _compute_gaps(sorted(ts)) for cat, ts in cat_times.items()}

    traffic_result = {}
    try:
        traffic_idxs_raw = es.get("/_cat/indices/dp-traffic-agg-*",
                                  params={"format": "json", "h": "index"})
        traffic_idx_names = [i.get("index", "") for i in traffic_idxs_raw if i.get("index")]
        if traffic_idx_names:
            tr = es.search(",".join(traffic_idx_names), {
                "size": 0,
                "aggs": {
                    "total_bps": {"sum": {"field": "trafficValue"}},
                    "avg_bps":   {"avg": {"field": "trafficValue"}},
                    "max_bps":   {"max": {"field": "trafficValue"}},
                    "by_day": {
                        "date_histogram": {"field": "timeStamp", "interval": "1d"},
                        "aggs": {
                            "avg_bps": {"avg": {"field": "trafficValue"}},
                            "max_bps": {"max": {"field": "trafficValue"}},
                        },
                    },
                    "by_direction": {"terms": {"field": "direction", "size": 5}},
                    "by_device":    {"terms": {"field": "deviceIp",  "size": 10}},
                },
            })
            t_aggs = tr.get("aggregations", {})
            t_total = tr.get("hits", {}).get("total")
            traffic_result = {
                "total_records": t_total.get("value") if isinstance(t_total, dict) else t_total,
                "total_bps":     _round(t_aggs.get("total_bps", {}).get("value")),
                "avg_bps":       _round(t_aggs.get("avg_bps",   {}).get("value")),
                "max_bps":       _round(t_aggs.get("max_bps",   {}).get("value")),
                "by_day": [
                    {
                        # key_as_string when ES provides it; else epoch ms → ISO
                        "date":    b.get("key_as_string") or _epoch_to_iso(b.get("key")),
                        "avg_bps": _round(b.get("avg_bps", {}).get("value")),
                        "max_bps": _round(b.get("max_bps", {}).get("value")),
                    }
                    for b in t_aggs.get("by_day", {}).get("buckets", [])
                    if b.get("doc_count", 0) > 0
                ],
                "by_direction": [
                    {"key": b.get("key"), "count": b.get("doc_count")}
                    for b in t_aggs.get("by_direction", {}).get("buckets", [])
                ],
                "by_device": [
                    {"key": b.get("key"), "count": b.get("doc_count")}
                    for b in t_aggs.get("by_device", {}).get("buckets", [])
                ],
            }
    except Exception:
        pass

    return {
        "total_attacks":    total_attacks,
        "total_packets":    _round(a_aggs.get("total_packets", {}).get("value")),
        "avg_attack_bps":   _round(a_aggs.get("avg_bw_bps",   {}).get("value")),
        "max_attack_bps":   _round(a_aggs.get("max_bw_bps",   {}).get("value")),

        "attacks_by_day":   _hist_buckets(a_aggs, "by_day"),
        "attacks_by_week":  _hist_buckets(a_aggs, "by_week"),
        "attacks_by_month": _hist_buckets(a_aggs, "by_month"),

        "by_category": [{"key": b.get("key"), "count": b.get("doc_count")}
                        for b in a_aggs.get("by_category", {}).get("buckets", [])],
        "by_status":   [{"key": b.get("key"), "count": b.get("doc_count")}
                        for b in a_aggs.get("by_status",   {}).get("buckets", [])],
        "by_risk":     [{"key": b.get("key"), "count": b.get("doc_count")}
                        for b in a_aggs.get("by_risk",     {}).get("buckets", [])],

        "duration_overall": _fmt_stats(a_aggs.get("duration_overall", {})),
        "duration_by_cat": [
            {
                "category": b.get("key"),
                "count":    b.get("doc_count"),
                "min_s":    _ms_to_s(b.get("dur", {}).get("min")),
                "max_s":    _ms_to_s(b.get("dur", {}).get("max")),
                "avg_s":    _ms_to_s(b.get("dur", {}).get("avg")),
                "avg_bps":  _round(b.get("avg_bps", {}).get("value")),
                "max_bps":  _round(b.get("max_bps", {}).get("value")),
            }
            for b in a_aggs.get("duration_by_cat", {}).get("buckets", [])
        ],

        "inter_attack_gaps": {
            "overall":     overall_gaps,
            "by_category": gaps_by_cat,
        },

        "traffic": traffic_result,
    }


# ── Full export (port of cc_summary_export) ──────────────────────────────────

def cc_summary_export(es):
    export = cc_summary(es)

    raw_dev = es.search("dp-attack-raw-*", {
        "size": 0,
        "aggs": {
            "by_device": {
                "terms": {"field": "deviceIp", "size": 100},
                "aggs": {
                    "versions":    {"terms": {"field": "deviceVersion",   "size": 5}},
                    "detectors":   {"terms": {"field": "detectorName",    "size": 5}},
                    "det_sources": {"terms": {"field": "detectionSource", "size": 5}},
                    "by_category": {"terms": {"field": "category",        "size": 20}},
                    "by_risk":     {"terms": {"field": "risk",            "size": 10}},
                    "by_status":   {"terms": {"field": "status",          "size": 5}},
                    "first_seen":  {"min":   {"field": "startTime"}},
                    "last_seen":   {"max":   {"field": "startTime"}},
                    "dur_stats":   {"stats": {"field": "duration"}},
                    "avg_bps":     {"avg":   {"field": "averageAttackRateBps"}},
                    "max_bps":     {"max":   {"field": "maxAttackRateBps"}},
                    "total_pkts":  {"sum":   {"field": "packetCount"}},
                },
            }
        },
    })

    devices = []
    for b in raw_dev.get("aggregations", {}).get("by_device", {}).get("buckets", []):
        ip  = b.get("key")
        dur = b.get("dur_stats", {})

        hostname = None
        resolved = False
        try:
            name = socket.gethostbyaddr(ip)[0]
            if name and name != ip:
                hostname = name
                resolved = True
        except Exception:
            pass

        devices.append({
            "ip":               ip,
            "hostname":         hostname,
            "hostname_resolved": resolved,
            "software_version": _first_bucket(b, "versions"),
            "detector_name":    _first_bucket(b, "detectors"),
            "detection_source": _first_bucket(b, "det_sources"),
            "total_attacks":    b.get("doc_count"),
            "first_attack":     _epoch_to_iso(b.get("first_seen", {}).get("value")),
            "last_attack":      _epoch_to_iso(b.get("last_seen",  {}).get("value")),
            "min_duration_s":   _ms_to_s(dur.get("min")),
            "avg_duration_s":   _ms_to_s(dur.get("avg")),
            "max_duration_s":   _ms_to_s(dur.get("max")),
            "avg_attack_bps":   _round(b.get("avg_bps", {}).get("value")),
            "max_attack_bps":   _round(b.get("max_bps", {}).get("value")),
            "total_packets":    _round(b.get("total_pkts", {}).get("value")),
            "attack_categories": [
                {"category": bk.get("key"), "count": bk.get("doc_count")}
                for bk in b.get("by_category", {}).get("buckets", [])
            ],
            "by_risk": [
                {"risk": bk.get("key"), "count": bk.get("doc_count")}
                for bk in b.get("by_risk", {}).get("buckets", [])
            ],
            "by_status": [
                {"status": bk.get("key"), "count": bk.get("doc_count")}
                for bk in b.get("by_status", {}).get("buckets", [])
            ],
        })

    now = datetime.now(timezone.utc)
    try:
        info = es.info()
        export["elasticsearch"] = {
            "host":         es.base_url,
            "es_version":   info.get("version", {}).get("number"),
            "cluster_name": info.get("cluster_name"),
        }
    except Exception:
        export["elasticsearch"] = {"host": es.base_url}

    export["generated_at"] = now.isoformat()
    export["devices"]      = sorted(devices, key=lambda d: d.get("total_attacks") or 0, reverse=True)
    return export


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="Generate the CC Summary Analytics JSON (same as the analyzer "
                    "'Download JSON'), talking directly to Elasticsearch.")
    p.add_argument("--host", default=os.getenv("ES_HOST", "localhost"),
                   help="ES host (default: localhost, or $ES_HOST)")
    p.add_argument("--port", type=int, default=int(os.getenv("ES_PORT", "9200")),
                   help="ES port (default: 9200, or $ES_PORT)")
    p.add_argument("--scheme", default=os.getenv("ES_SCHEME", "http"),
                   choices=["http", "https"], help="http (default) or https")
    p.add_argument("--user", default=os.getenv("ES_USER", ""), help="basic-auth user")
    p.add_argument("--password", default=os.getenv("ES_PASSWORD", ""), help="basic-auth password")
    p.add_argument("--verify-certs", action="store_true",
                   help="verify TLS certs (default: off, matching the service)")
    p.add_argument("-o", "--output", default="",
                   help="output file; '-' for stdout; default cc_summary_<UTCstamp>.json")
    args = p.parse_args()

    es = ES(args.host, args.port, args.scheme, args.user, args.password,
            verify_certs=args.verify_certs)
    try:
        export = cc_summary_export(es)
    except Exception as exc:
        sys.stderr.write(f"ERROR: failed to build summary from {es.base_url}: {exc}\n")
        return 2

    text = json.dumps(export, indent=2, default=str)
    if args.output == "-":
        sys.stdout.write(text + "\n")
    else:
        out = args.output or ("cc_summary_"
                              + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                              + ".json")
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text)
        sys.stderr.write(f"wrote {out} ({len(text):,} bytes)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
