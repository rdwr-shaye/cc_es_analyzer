from fastapi import APIRouter
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel
from services.es_client import get_client, update_client
import json
import logging
import re
import socket
import time
from datetime import datetime, timezone
from typing import Any

router = APIRouter(prefix="/api", tags=["query"])
logger = logging.getLogger(__name__)


# ── Connection update ─────────────────────────────────────────────────────────

class ConnectionRequest(BaseModel):
    host: str
    port: int = 9200
    scheme: str = "http"
    user: str = ""
    password: str = ""
    verify_certs: bool = False
    # ── SSH fallback (used only if the ES port cannot be reached directly) ──
    ssh_enabled: bool = False
    ssh_user: str = ""
    ssh_password: str = ""
    ssh_port: int = 22


def _try_connect(req: "ConnectionRequest"):
    """Update the ES client and attempt a ping. Returns (ok, client, info)."""
    client = update_client(
        host=req.host, port=req.port, scheme=req.scheme,
        user=req.user, password=req.password, verify_certs=req.verify_certs,
    )
    if client.ping():
        return True, client, client.info()
    return False, client, None


@router.post("/connect")
def connect(req: ConnectionRequest):
    """
    Update the active Elasticsearch connection, trying up to three ways:

      1. Direct ES connection.
      2. If that fails and SSH is enabled: SSH in and OPEN the ES port on the
         target box's firewall, then retry the direct connection (works when the
         port was simply closed on that box).
      3. If it's still unreachable: open an SSH TUNNEL and forward ES traffic
         through the SSH connection (works when a firewall between us and the
         box blocks the ES port but permits SSH, or ES only listens on the
         remote loopback).
    """
    from services.ssh_tunnel import start_tunnel, stop_tunnel

    def _connected(info, **extra):
        return {"connected": True,
                "es_version": info.get("version", {}).get("number"),
                "cluster_name": info.get("cluster_name"), **extra}

    # ── 1) Direct ES connection ───────────────────────────────────────────────
    try:
        ok, client, info = _try_connect(req)
        if ok:
            stop_tunnel()   # direct works — drop any stale tunnel
            return _connected(info)
    except Exception as e:
        logger.warning("[connect] direct ES connection error: %s", e)

    if not req.ssh_enabled:
        return {"connected": False,
                "error": "Ping failed — check host/port. "
                         "Enable SSH to reach ES through the firewall."}
    if not req.ssh_user:
        return {"connected": False,
                "error": "SSH is enabled but no SSH username was provided."}

    # ── 2) SSH-open the ES port on the box (only if not already open), then
    #       retry the direct connection. If it was already open, opening again
    #       won't help — skip straight to the tunnel.
    from services.ssh_opener import ensure_port_open_via_ssh
    logger.info("[connect] direct failed; checking/opening port %s on %s via SSH",
                req.port, req.host)
    open_res = ensure_port_open_via_ssh(
        host=req.host, ssh_user=req.ssh_user, ssh_password=req.ssh_password,
        port=req.port, ssh_port=req.ssh_port,
    )
    if open_res.get("already_open"):
        logger.info("[connect] port %s already open in firewall — going to SSH tunnel", req.port)
    else:
        if not open_res.get("ok"):
            logger.info("[connect] SSH port-open did not confirm (%s) — will try tunnel",
                        open_res.get("error"))
        try:
            ok, client, info = _try_connect(req)
            if ok:
                stop_tunnel()
                return _connected(info, ssh_opened_port=True,
                                  ssh_command=open_res.get("command"))
        except Exception as e:
            logger.warning("[connect] ES retry after SSH port-open failed: %s", e)

    # ── 3) SSH tunnel bypass — forward ES traffic through the SSH connection ──
    logger.info("[connect] opening SSH tunnel %s:22 -> 127.0.0.1:%s", req.host, req.port)
    tun = start_tunnel(
        ssh_host=req.host, ssh_user=req.ssh_user, ssh_password=req.ssh_password,
        ssh_port=req.ssh_port, remote_host="127.0.0.1", remote_port=req.port,
    )
    if not tun["ok"]:
        return {
            "connected": False,
            "error": f"Direct connection and SSH port-open both failed, and the SSH "
                     f"tunnel could not be opened: {tun.get('error') or 'unknown error'}",
            "ssh": tun,
        }
    try:
        client = update_client(
            host=tun["local_host"], port=tun["local_port"],
            scheme=req.scheme or "http",
            user=req.user, password=req.password, verify_certs=req.verify_certs,
        )
        if client.ping():
            info = client.info()
            return _connected(
                info, ssh_tunnel=True,
                tunnel=f"127.0.0.1:{tun['local_port']} → (ssh) {req.host} → 127.0.0.1:{req.port}",
            )
    except Exception as e:
        logger.warning("[connect] ES via SSH tunnel failed: %s", e)

    stop_tunnel()
    return {
        "connected": False,
        "error": f"Could not reach Elasticsearch directly, after opening the port via "
                 f"SSH, or through an SSH tunnel — check ES is running on 127.0.0.1:{req.port} "
                 f"on {req.host}.",
    }


# ── Generic query ─────────────────────────────────────────────────────────────

class QueryRequest(BaseModel):
    index: str
    body: dict
    size: int = 20


def _validate_sort_clause(es, index: str, body: dict) -> str | None:
    """Ensure every field in body["sort"] exists in *index*'s mapping.

    The default editor template sorts on "startTime", which not every index
    has — running it unmodified would error out. A missing sort field is
    replaced with the index's best sortable date field; if the index has no
    date fields at all, the sort clause is dropped. Returns a human-readable
    note describing what changed (or None when the sort was left untouched).
    """
    sort_clauses = body.get("sort")
    if not sort_clauses:
        return None
    if not isinstance(sort_clauses, list):
        sort_clauses = [sort_clauses]

    known = set(_collect_all_fields(es, index))
    if not known:
        return None      # mapping unavailable — don't second-guess the query

    def _sort_field(clause):
        if isinstance(clause, str):
            return clause
        if isinstance(clause, dict) and clause:
            return next(iter(clause))
        return None

    missing = [f for f in (_sort_field(c) for c in sort_clauses)
               if f and f != "_score" and f not in known]
    if not missing:
        return None

    substitute, reason = _resolve_sort_field(es, index, "start")
    if substitute in known:
        kept = [c for c in sort_clauses if _sort_field(c) not in missing]
        # Preserve the order direction of the first replaced clause if present.
        order = "desc"
        for c in sort_clauses:
            f = _sort_field(c)
            if f in missing and isinstance(c, dict) and isinstance(c.get(f), dict):
                order = c[f].get("order", "desc")
                break
        body["sort"] = kept + [{substitute: {"order": order}}]
        note = (f"sort field(s) {', '.join(missing)} not in {index!r} — "
                f"sorted by {substitute!r} instead")
    else:
        body.pop("sort", None)
        note = (f"sort field(s) {', '.join(missing)} not in {index!r} and no "
                f"date field found — sort removed")
    logger.info("[query] %s (%s)", note, reason)
    return note


@router.post("/query")
def generic_query(req: QueryRequest):
    """Execute any Elasticsearch query against the specified index."""
    try:
        es = get_client()
        body = req.body
        if "size" not in body:
            body["size"] = req.size
        try:
            sort_note = _validate_sort_clause(es, req.index, body)
        except Exception as exc:                     # validation is best-effort
            logger.warning("[query] sort validation failed: %s", exc)
            sort_note = None
        resp = es.search(req.index, body)
        hits = resp.get("hits", {})
        total = hits.get("total")
        result = {
            "total": total.get("value") if isinstance(total, dict) else total,
            "hits": [{"_id": h["_id"], "_index": h["_index"], **h.get("_source", {})}
                     for h in hits.get("hits", [])],
            "took_ms": resp.get("took"),
            "aggregations": resp.get("aggregations"),
        }
        if sort_note:
            result["sort_note"] = sort_note
        return result
    except Exception as e:
        return {"error": str(e)}


# ── CC-specific convenience endpoints ────────────────────────────────────────

def _log_es_query(api: str, index: str, body: dict) -> None:
    """Log the exact ES query an API endpoint sends — the API name comes first
    so log lines are easy to attribute when debugging."""
    logger.info("[%s] ES query → POST /%s/_search\n%s",
                api, index, json.dumps(body, indent=2, ensure_ascii=False))


@router.get("/cc/attacks")
def latest_attacks(size: int = 50, status: str = ""):
    """
    Two-phase join:
      Phase 1 — fetch unique attack records from dp-attack-raw-* (one doc per attack,
                 attackIpsId uses dash delimiter e.g. '3-1781601338').
      Phase 2 — enrich with aggregated traffic stats from attack-data-*
                 (attackIpsId uses underscore delimiter e.g. '3_1781601338').
    """
    try:
        es = get_client()

        # ── Phase 1: canonical attack list from dp-attack-raw-* ──────────────
        raw_body = {
            "size": size,
            "sort": [{"startTime": {"order": "desc"}}],
            "query": {"match_all": {}},
        }
        _log_es_query("/api/cc/attacks — phase 1 (attack list)", "dp-attack-raw-*", raw_body)
        raw_resp = es.search("dp-attack-raw-*", raw_body)
        raw_hits = raw_resp.get("hits", {}).get("hits", [])
        total_raw = raw_resp.get("hits", {}).get("total")

        if not raw_hits:
            return {"total_records": 0, "total_unique_attacks": 0, "attacks": []}

        # Build lookup: underscore_id → dp-attack-raw base record
        raw_by_under = {}
        for h in raw_hits:
            src = h.get("_source", {})
            aid_dash  = src.get("attackIpsId") or h.get("_id", "")
            aid_under = aid_dash.replace("-", "_", 1)   # first dash only
            chars     = src.get("characteristics") or {}
            raw_by_under[aid_under] = {
                "attackIpsId":   aid_dash,                          # keep original dash format
                "startTime":     src.get("startTime"),
                "endTime":       src.get("endTime"),
                "duration":      src.get("duration"),
                "attackType":    _extract_attack_type(h.get("_index", "")),
                "status":        _val(src.get("status")),
                "deviceIp":      _val(src.get("deviceIp")),
                "blockingState": _val(chars.get("blockingState") or src.get("latestBlockingState")),
                "actionType":    _val(chars.get("actionType")),
            }

        # ── Phase 2: aggregated stats from attack-data-* ─────────────────────
        under_ids = list(raw_by_under.keys())
        stats_body = {
            "size": 0,
            "query": {"terms": {"attackIpsId": under_ids}},
            "aggs": {
                "by_attack": {
                    "terms": {"field": "attackIpsId", "size": len(under_ids) + 10},
                    "aggs": {
                        "total_bw":       {"sum": {"field": "bandwidth"}},
                        "total_packets":  {"sum": {"field": "trafficPacketCount"}},
                        "avg_pkt_rate":   {"avg": {"field": "packetRate"}},
                        "top_device":     {"terms": {"field": "deviceIp",      "size": 3}},
                        "top_protection": {"terms": {"field": "protection",    "size": 5}},
                        "top_dest":       {"terms": {"field": "destinationIp", "size": 3}},
                        "top_policy":     {"terms": {"field": "policyName",    "size": 3}},
                    },
                }
            },
        }
        _log_es_query("/api/cc/attacks — phase 2 (traffic stats)", "attack-data-*", stats_body)
        stats_resp = es.search("attack-data-*", stats_body)
        stats_buckets = stats_resp.get("aggregations", {}).get("by_attack", {}).get("buckets", [])

        stats_by_under = {}
        for b in stats_buckets:
            aid = b.get("key")
            stats_by_under[aid] = {
                "recordCount":   b.get("doc_count"),
                "totalBw":       _round(b.get("total_bw",      {}).get("value")),
                "totalPackets":  _round(b.get("total_packets", {}).get("value")),
                "avgPacketRate": _round(b.get("avg_pkt_rate",  {}).get("value")),
                "deviceIp":      _first_bucket(b, "top_device"),
                "protection":    _first_bucket(b, "top_protection"),
                "destinationIp": _first_bucket(b, "top_dest"),
                "policyName":    _first_bucket(b, "top_policy"),
            }

        # ── Merge ─────────────────────────────────────────────────────────────
        attacks = []
        for aid_under, base in raw_by_under.items():
            stats = stats_by_under.get(aid_under, {})
            merged = {**base, **stats}
            if base.get("deviceIp"):          # raw doc beats aggregated top-device
                merged["deviceIp"] = base["deviceIp"]
            attacks.append(merged)

        return {
            "total_records":        total_raw.get("value") if isinstance(total_raw, dict) else total_raw,
            "total_unique_attacks": len(attacks),
            "attacks":              attacks,
        }
    except Exception as e:
        return {"error": str(e)}


@router.get("/cc/attacks/{attack_id}/details")
def attack_details(attack_id: str, size: int = 300):
    """
    Everything ES knows about one attack: search every index whose name
    contains "dp-" or "attack-data" for the attack ID, in BOTH delimiter
    forms (dp-attack-raw stores '3-1781601338', attack-data '3_1781601338'),
    and return the matching documents grouped per index.
    """
    try:
        es = get_client()
        dash  = attack_id.replace("_", "-", 1)
        under = attack_id.replace("-", "_", 1)
        pattern = "*dp-*,*attack-data*"
        body = {
            "size": max(1, min(size, 1000)),
            "query": {"bool": {
                "should": [{"match": {"attackIpsId": v}} for v in {dash, under}],
                "minimum_should_match": 1,
            }},
        }
        _log_es_query(f"/api/cc/attacks/{attack_id}/details", pattern, body)
        resp = es.post(f"/{pattern}/_search?ignore_unavailable=true&allow_no_indices=true",
                       body)
        hits  = resp.get("hits", {}).get("hits", [])
        total = resp.get("hits", {}).get("total")
        by_index: dict = {}
        for h in hits:
            by_index.setdefault(h.get("_index", "?"), []).append(
                {"_id": h.get("_id"), **h.get("_source", {})})
        return {
            "attack_id": attack_id,
            "id_forms":  sorted({dash, under}),
            "total":     total.get("value") if isinstance(total, dict) else total,
            "returned":  len(hits),
            "indices":   [{"index": k, "count": len(v), "docs": v}
                          for k, v in sorted(by_index.items())],
        }
    except Exception as e:
        return {"error": str(e)}


@router.get("/cc/attacks/summary")
def attacks_summary():
    """Aggregated attack count by category and status."""
    try:
        es = get_client()
        body = {
            "size": 0,
            "aggs": {
                "by_category": {"terms": {"field": "category.keyword", "size": 20}},
                "by_status":   {"terms": {"field": "status.keyword",   "size": 10}},
                "attacks_over_time": {
                    "date_histogram": {"field": "startTime", "interval": "1d"}
                },
            },
        }
        _log_es_query("/api/cc/attacks/summary", "attack-data-*", body)
        resp = es.search("attack-data-*", body)
        aggs = resp.get("aggregations", {})
        hits_total = resp.get("hits", {}).get("total")
        return {
            "total_attacks":     hits_total.get("value") if isinstance(hits_total, dict) else hits_total,
            "by_category":        _buckets(aggs, "by_category"),
            "by_status":          _buckets(aggs, "by_status"),
            "attacks_over_time":  _date_buckets(aggs, "attacks_over_time"),
        }
    except Exception as e:
        return {"error": str(e)}


@router.get("/cc/summary")
def cc_summary():
    """
    Full analytics summary:
    - Attack counts per day / week / month
    - Breakdown by category, status, risk
    - Duration stats (min/max/avg) per category
    - Inter-attack gap stats (min/max/avg time between attacks) overall + per category
    - Overall traffic from dp-traffic-agg-*
    """
    try:
        es = get_client()

        # ── 1. Attack aggregations from dp-attack-raw-* ───────────────────────
        attack_body = {
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
        }
        _log_es_query("/api/cc/summary — 1 (attack aggregations)", "dp-attack-raw-*", attack_body)
        ar = es.search("dp-attack-raw-*", attack_body)
        a_aggs = ar.get("aggregations", {})
        total_raw = ar.get("hits", {}).get("total")
        total_attacks = total_raw.get("value") if isinstance(total_raw, dict) else total_raw

        # ── 2. Inter-attack gap calculation (fetch up to 2000 sorted timestamps) ─
        gap_body = {
            "size": 2000,
            "sort": [{"startTime": {"order": "asc"}}],
            "_source": ["startTime", "category"],
            "query": {"match_all": {}},
        }
        _log_es_query("/api/cc/summary — 2 (inter-attack gaps)", "dp-attack-raw-*", gap_body)
        gap_resp = es.search("dp-attack-raw-*", gap_body)
        gap_data = [
            (h["_source"].get("startTime"), h["_source"].get("category", "Unknown"))
            for h in gap_resp.get("hits", {}).get("hits", [])
            if h.get("_source", {}).get("startTime")
        ]
        overall_gaps = _compute_gaps([t for t, _ in gap_data])

        from collections import defaultdict
        cat_times: dict = defaultdict(list)
        for t, cat in gap_data:
            cat_times[cat].append(t)
        gaps_by_cat = {cat: _compute_gaps(sorted(ts)) for cat, ts in cat_times.items()}

        # ── 3. Traffic aggregation from dp-traffic-agg-* ──────────────────────
        traffic_result: dict = {}
        try:
            traffic_idxs_raw = es.get("/_cat/indices/dp-traffic-agg-*",
                                      params={"format": "json", "h": "index"})
            traffic_idx_names = [i.get("index", "") for i in traffic_idxs_raw if i.get("index")]
            if traffic_idx_names:
                traffic_body = {
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
                }
                _log_es_query("/api/cc/summary — 3 (traffic aggregation)",
                              ",".join(traffic_idx_names), traffic_body)
                tr = es.search(",".join(traffic_idx_names), traffic_body)
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
                            # (a raw number here crashed the frontend's .slice()).
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
            pass  # traffic data is best-effort

        # ── Assemble ──────────────────────────────────────────────────────────
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
    except Exception as e:
        return {"error": str(e)}


@router.get("/cc/summary/export")
def cc_summary_export():
    """
    Download the /api/cc/summary response as a JSON file,
    with an additional 'devices' field containing per-device breakdown
    and reverse-DNS resolution for each device IP.
    """
    try:
        es = get_client()

        # ── 1. Get the base summary (same as /api/cc/summary) ─────────────────
        export = cc_summary()
        if "error" in export:
            return Response(
                content=json.dumps(export, indent=2),
                media_type="application/json", status_code=500,
            )

        # ── 2. Build per-device breakdown from dp-attack-raw-* ────────────────
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

            # Reverse-DNS lookup
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

        # ── 3. Inject metadata + devices into the summary object ──────────────
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

        ts = now.strftime("%Y%m%d_%H%M%S")
        filename = f"cc_summary_{ts}.json"
        return Response(
            content=json.dumps(export, indent=2, default=str),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as e:
        return Response(
            content=json.dumps({"error": str(e)}, indent=2),
            media_type="application/json", status_code=500,
        )


class NLQueryRequest(BaseModel):
    text: str
    index: str = "dp-attack-raw-*"
    attack_types: list[str] = []     # pre-selected attack categories from UI picker

    # Explicit date-range bounds from the UI time-range picker. Each of the
    # start/end times can carry BOTH a lower ("after") and an upper ("before")
    # bound, e.g. started after 09:00 AND before 10:00.
    start_after:  str = ""           # ISO datetime, e.g. "2026-06-28T10:46"
    start_before: str = ""
    end_after:    str = ""
    end_before:   str = ""

    # Sort
    sort_hint:      str = "start"    # "start" | "end" | "" (no sort)
    sort_direction: str = "desc"     # "asc" | "desc"


class MultiRunRequest(BaseModel):
    per_index_queries: list[dict]    # [{index, query_body}, ...]
    size: int = 20
    sort_direction: str = "desc"     # "asc" | "desc" — used to re-sort merged hits


class ExportRequest(BaseModel):
    # Either a multi-index plan or a single index+body. format: "csv" | "json".
    per_index_queries: list[dict] = []
    index: str = ""
    query_body: dict = {}
    format: str = "csv"
    max_rows: int = 100_000          # safety cap to avoid unbounded exports


@router.get("/cc/attack-types")
def get_attack_types():
    """Return distinct attack categories present in dp-attack-raw-* indices."""
    FALLBACK = ["DNS", "WebDDoS", "BehavioralDOS", "SynFlood", "Intrusions",
                "Anomalies", "AntiScanning", "ACL", "StatefulACL", "DOSShield",
                "TrafficFilters"]
    try:
        es = get_client()
        resp = es.search("dp-attack-raw-*", {
            "size": 0,
            "aggs": {
                "categories": {"terms": {"field": "category", "size": 100}}
            },
        })
        buckets = resp.get("aggregations", {}).get("categories", {}).get("buckets", [])
        types = [b.get("key") for b in buckets if b.get("key")]
        # Merge with fallback so known types always appear
        merged = list(dict.fromkeys(types + [t for t in FALLBACK if t not in types]))
        return {"types": merged}
    except Exception as e:
        return {"types": FALLBACK, "warning": str(e)}


@router.post("/query/translate")
def translate_nl_query(req: NLQueryRequest):
    """
    Translate NL text + UI picker state into per-index ES query bodies.

    Workflow:
      1. Parse common filters (category, IP, status, risk, blocking)
      2. Discover distinct index groups from the pattern (dp-attack-* →
         ["dp-attack-raw-*", "dp-attack-extra-*"])
      3. For each group: fetch mapping, resolve correct date fields,
         build a group-specific query + sort clause
      4. Return per_index_queries[] + interpreted[]
    Logs the final per-group queries at INFO level.
    """
    import re

    text_lower = req.text.lower()
    interpreted: list = []

    # ── ES client ─────────────────────────────────────────────────────────────
    try:
        es = get_client()
    except Exception as exc:
        return {"error": str(exc)}

    # ── Common (non-date) must clauses ────────────────────────────────────────
    common_must: list = []

    # ── Enum-like fields (category / status / risk) ───────────────────────────
    # Resolve real values + exact casing from the index data, so typed words
    # like "terminated" map to the stored value "Terminated", and new values
    # are picked up automatically (built-in fallback if ES is unavailable).
    enum_maps = _get_enum_maps(es, req.index, ["status", "risk", "category"])

    # Clauses on fixed/known fields are gated per-group: only applied to an index
    # group whose mapping actually has the field (e.g. dp-attack-extra-* has no
    # "status" field). Each entry is (gate_field, clause).
    gated_clauses: list = []

    if req.attack_types:                       # UI picker overrides text category
        clean = [t.strip() for t in req.attack_types if t.strip()]
        if len(clean) == 1:
            gated_clauses.append(("category", {"match": {"category": clean[0]}}))
            interpreted.append(f"category = {clean[0]}")
        elif len(clean) > 1:
            gated_clauses.append(("category", {"bool": {"should": [{"match": {"category": t}} for t in clean],
                                                        "minimum_should_match": 1}}))
            interpreted.append(f"category IN [{', '.join(clean)}]")
        enum_maps.pop("category", None)

    # Match enum values appearing in the text, longest term first, consuming the
    # matched span so "StatefulACL" wins over "ACL". `consumed` (text with those
    # spans blanked) is then used for the generic field/existence extraction so
    # an enum value isn't also treated as a free-text field value.
    from collections import defaultdict
    # Group values that look the same case/separator-insensitively, so a typed
    # word that matches several stored variants (e.g. "Terminated" AND
    # "TerminaTed") pulls in ALL of them rather than just the first.
    _groups: dict = {}   # (field, norm) -> {"rep": term, "canons": [canonical...]}
    for field, vmap in enum_maps.items():
        for term, canon in vmap.items():
            norm = _norm_field(term)
            g = _groups.setdefault((field, norm), {"rep": term, "canons": []})
            if canon not in g["canons"]:
                g["canons"].append(canon)
            if len(_enum_tokens(term)) > len(_enum_tokens(g["rep"])):
                g["rep"] = term          # prefer a multi-word representative

    consumed = req.text
    _matched: dict = defaultdict(list)
    for (field, norm), g in sorted(_groups.items(), key=lambda kv: -len(kv[0][1])):
        pat = _enum_pattern(g["rep"])
        if pat.search(consumed):
            for c in g["canons"]:
                if c not in _matched[field]:
                    _matched[field].append(c)
            consumed = pat.sub(" ", consumed)
    for field, vals in _matched.items():
        if len(vals) == 1:
            gated_clauses.append((field, {"match": {field: vals[0]}}))
            interpreted.append(f"{field} = {vals[0]}")
        else:
            gated_clauses.append((field, {"bool": {"should": [{"match": {field: v}} for v in vals],
                                                   "minimum_should_match": 1}}))
            interpreted.append(f"{field} IN [{', '.join(vals)}]")

    # Free-text "<descriptor words> <value>" references (IPs, attack IDs, numbers,
    # quoted strings). The real ES field is resolved per-index from the mapping
    # in the per-group loop below. Runs on `consumed` so enum values are skipped.
    field_refs = _extract_field_refs(consumed)
    _OP_LABEL = {"eq": "=", "contains": "contains", "neq": "≠", "ncontains": "not-contains"}
    for ref in field_refs:
        label = " ".join(ref["words"]) or ref["kind"]
        vals  = ref["values"]
        shown = vals[0] if len(vals) == 1 else "[" + ", ".join(map(str, vals)) + "]"
        interpreted.append(f"{label} {_OP_LABEL[ref['op']]} {shown}")

    # Field-existence predicates ("footprint field exists", "no vlan", ...)
    existence_refs = _extract_existence_refs(consumed)
    for ref in existence_refs:
        label = " ".join(ref["words"])
        interpreted.append(f"{label} {'exists' if ref['present'] else 'missing'}")

    # Blocking state (nested field — gated on the "blockingState" leaf)
    if any(w in text_lower for w in ("blocking", "blocked")):
        gated_clauses.append(("blockingState", {"match": {"characteristics.blockingState": "Blocking"}}))
        interpreted.append("blockingState = Blocking")

    # ── Explicit date values from the UI picker ───────────────────────────────
    # Each of start/end carries optional "after" (gte) and "before" (lte) bounds.
    start_bounds = {"gte": _parse_datetime_to_ms(req.start_after),
                    "lte": _parse_datetime_to_ms(req.start_before)}
    end_bounds   = {"gte": _parse_datetime_to_ms(req.end_after),
                    "lte": _parse_datetime_to_ms(req.end_before)}
    start_bounds = {k: v for k, v in start_bounds.items() if v is not None}
    end_bounds   = {k: v for k, v in end_bounds.items() if v is not None}

    for label, bounds in (("start", start_bounds), ("end", end_bounds)):
        if "gte" in bounds and "lte" in bounds and bounds["gte"] > bounds["lte"]:
            return {"error": f"illegal {label}-time range — 'after' is later than 'before'"}

    def _fmt_bound(v: str) -> str:
        return v.replace("T", " ")
    if req.start_after:
        interpreted.append(f"started after {_fmt_bound(req.start_after)}")
    if req.start_before:
        interpreted.append(f"started before {_fmt_bound(req.start_before)}")
    if req.end_after:
        interpreted.append(f"ended after {_fmt_bound(req.end_after)}")
    if req.end_before:
        interpreted.append(f"ended before {_fmt_bound(req.end_before)}")

    # ── NL relative-time phrases (fallback — only when picker is empty) ───────
    nl_time = _parse_nl_time(req.text) if not (start_bounds or end_bounds) else None
    if nl_time:
        interpreted.append(f"start >= {nl_time['label']}")

    # ── Discover index groups → build one query per group ─────────────────────
    groups = _discover_index_groups(es, req.index)
    per_index_queries: list = []

    suggestions: list = []   # partial matches the user must confirm before querying

    for group_pattern in groups:
        date_fields = _get_date_fields(es, group_pattern)
        group_must     = list(common_must)    # copy — do NOT modify common_must
        group_must_not: list = []

        # Fetch the group's real field names once for field/existence resolution.
        all_fields = (_collect_all_fields(es, group_pattern)
                      if (field_refs or existence_refs or gated_clauses) else [])
        all_fields_set = set(all_fields)

        # Gated clauses (category/status/risk/blocking) — only applied when the
        # field actually exists in THIS group's mapping.
        for gate_field, clause in gated_clauses:
            if gate_field in all_fields_set:
                group_must.append(clause)
            else:
                logger.info("[translate] %s  SKIP clause on %r — field absent from mapping",
                            group_pattern, gate_field)

        # Resolve free-text field references against THIS group's real mapping.
        # Supports operators: eq (match), contains (wildcard), neq / ncontains (must_not).
        # A *confident* match (all descriptor words found) is applied directly; a
        # *partial* match becomes a suggestion the user confirms before it's used.
        if field_refs:
            for ref in field_refs:
                words = ref["words"] or (["ip"] if ref["kind"] == "ip" else [])
                fld, score, total, why = _resolve_field_scored(words, all_fields)
                vals = ref["values"]
                if not fld:
                    logger.info("[translate] %s  FIELD  words=%s values=%r -> UNRESOLVED (%s)",
                                group_pattern, ref["words"], vals, why)
                    continue
                op = ref["op"]
                where = "must_not" if op in ("neq", "ncontains") else "must"
                # eq → match (1 value) / terms (many); contains → wildcard,
                # OR-combined via a should for multi-value refs.
                if op in ("contains", "ncontains"):
                    if len(vals) == 1:
                        clause = {"wildcard": {fld: f"*{vals[0]}*"}}
                    else:
                        clause = {"bool": {"should": [{"wildcard": {fld: f"*{v}*"}} for v in vals],
                                           "minimum_should_match": 1}}
                elif len(vals) == 1:
                    clause = {"match": {fld: vals[0]}}
                else:
                    clause = {"terms": {fld: vals}}

                if score >= total:                      # confident — apply
                    (group_must_not if where == "must_not" else group_must).append(clause)
                    logger.info("[translate] %s  FIELD  words=%s op=%s values=%r -> %r (%s)",
                                group_pattern, ref["words"], op, vals, fld, why)
                else:                                   # partial — suggest candidates
                    cands = _candidate_fields(words, all_fields)
                    suggestions.append({
                        "index": group_pattern, "kind": "field",
                        "label": " ".join(ref["words"]) or ref["kind"],
                        "op": op, "value": vals[0], "values": vals, "where": where,
                        "total": total, "candidates": cands,
                    })
                    logger.info("[translate] %s  FIELD  words=%s values=%r -> SUGGEST %s (%s)",
                                group_pattern, ref["words"], vals,
                                [c["field"] for c in cands], why)

        # Field-existence predicates → bare `exists` clause routed to must / must_not.
        # ES 1.3.14 rejects constant_score+missing here, so "missing" is expressed
        # as must_not exists (and "present" as must exists).
        if existence_refs:
            for ref in existence_refs:
                fld, score, total, why = _resolve_field_scored(ref["words"], all_fields)
                if not fld:
                    logger.info("[translate] %s  EXISTS words=%s -> UNRESOLVED (%s)",
                                group_pattern, ref["words"], why)
                    continue
                clause = {"exists": {"field": fld}}
                where  = "must" if ref["present"] else "must_not"
                if score >= total:                      # confident — apply
                    (group_must if ref["present"] else group_must_not).append(clause)
                    logger.info("[translate] %s  EXISTS words=%s present=%s -> %s exists %r (%s)",
                                group_pattern, ref["words"], ref["present"], where, fld, why)
                else:                                   # partial — suggest candidates
                    cands = _candidate_fields(ref["words"], all_fields)
                    suggestions.append({
                        "index": group_pattern, "kind": "exists",
                        "label": " ".join(ref["words"]),
                        "present": ref["present"], "value": None, "where": where,
                        "total": total, "candidates": cands,
                    })
                    logger.info("[translate] %s  EXISTS words=%s -> SUGGEST %s (%s)",
                                group_pattern, ref["words"],
                                [c["field"] for c in cands], why)

        # Started-at → field with "start"
        if start_bounds:
            sf, sf_reason = _pick_date_field_verbose(date_fields, "start")
            group_must.append({"range": {sf: dict(start_bounds)}})
            logger.info("[translate] %s  START  chosen=%r  reason=%s  bounds=%s",
                        group_pattern, sf, sf_reason, start_bounds)

        # Ended-at → field with "end" (or non-"start" fallback)
        if end_bounds:
            ef, ef_reason = _pick_date_field_verbose(date_fields, "end")
            group_must.append({"range": {ef: dict(end_bounds)}})
            logger.info("[translate] %s  END    chosen=%r  reason=%s  bounds=%s",
                        group_pattern, ef, ef_reason, end_bounds)

        # NL relative time → applied on start field
        if nl_time:
            tf = _pick_date_field(date_fields, "start")
            group_must.append({"range": {tf: {"gte": nl_time["value"]}}})

        # Build query — bool whenever there are negations or multiple clauses.
        if group_must_not:
            bool_q: dict = {"must_not": group_must_not}
            bool_q["must"] = group_must or [{"match_all": {}}]
            query: dict = {"bool": bool_q}
        elif not group_must:
            query = {"match_all": {}}
        elif len(group_must) == 1:
            query = group_must[0]
        else:
            query = {"bool": {"must": group_must}}

        body: dict = {"query": query}

        # Sort clause — use _resolve_sort_field so fields with doc_values:False
        # (e.g. endTime in dp-attack-extra-*) are filtered out before picking.
        if req.sort_hint and req.sort_direction in ("asc", "desc"):
            sort_field, sort_reason = _resolve_sort_field(es, group_pattern, req.sort_hint)
            body["sort"] = [{sort_field: {"order": req.sort_direction}}]
            logger.info("[translate] %s  SORT  hint=%r  chosen=%r  reason=%s  order=%s",
                        group_pattern, req.sort_hint, sort_field, sort_reason, req.sort_direction)

        per_index_queries.append({
            "index":       group_pattern,
            "date_fields": date_fields,
            "query_body":  body,
        })

    # ── Info log — full per-group query plan ──────────────────────────────────
    logger.info(
        "[translate] pattern=%r → groups=%s | interpreted=%s\nFINAL QUERY PLAN:\n%s",
        req.index,
        [q["index"] for q in per_index_queries],
        interpreted,
        json.dumps(per_index_queries, indent=2, ensure_ascii=False),
    )

    if suggestions:
        logger.info("[translate] %s field suggestion(s) need confirmation: %s",
                    len(suggestions),
                    [(s["index"], s["label"], [c["field"] for c in s.get("candidates", [])])
                     for s in suggestions])

    return {
        "per_index_queries": per_index_queries,
        "interpreted":       interpreted,
        "is_multi_index":    len(groups) > 1,
        "suggestions":       suggestions,
    }


@router.post("/query/multi-run")
def multi_run_query(req: MultiRunRequest):
    """
    Execute a list of per-index queries (produced by /query/translate) and
    return merged hits with per-index metadata.
    """
    try:
        es = get_client()
        all_hits: list = []
        meta: list = []

        # Collect hits and remember which sort field each index used
        sort_field_by_index: dict[str, str] = {}
        for item in req.per_index_queries:
            index      = item.get("index", "")
            query_body = dict(item.get("query_body", {}))
            if "size" not in query_body:
                query_body["size"] = req.size

            # Extract sort field from the embedded sort clause (set by translate)
            sort_clauses = query_body.get("sort", [])
            if sort_clauses and isinstance(sort_clauses, list) and sort_clauses[0]:
                sort_field_by_index[index] = next(iter(sort_clauses[0]))

            try:
                resp      = es.search(index, query_body)
                hits      = resp.get("hits", {}).get("hits", [])
                total_raw = resp.get("hits", {}).get("total")
                total_val = total_raw.get("value") if isinstance(total_raw, dict) else total_raw
                all_hits.extend([
                    {"_id": h["_id"], "_index": h["_index"], **h.get("_source", {})}
                    for h in hits
                ])
                meta.append({
                    "index": index, "total": total_val,
                    "returned": len(hits), "took_ms": resp.get("took"),
                })
                logger.info("[multi-run] %-40s total=%-8s returned=%s  took=%sms",
                            index, total_val, len(hits), resp.get("took"))
            except Exception as exc:
                logger.error("[multi-run] %s → ERROR: %s", index, exc)
                meta.append({"index": index, "error": str(exc)})

        # Re-sort merged hits globally.
        # Each hit carries its _index; use the sort field that index was queried with.
        # Fall back through common time-field names if the hit's sort field is absent.
        _FALLBACK_TIME_FIELDS = ["startTime", "endTime", "timeStamp", "timestamp", "@timestamp"]

        def _sort_key(hit: dict):
            idx = hit.get("_index", "")
            # Match stored sort field by prefix (wildcard patterns like dp-attack-raw-*)
            sf = None
            for pattern, field in sort_field_by_index.items():
                prefix = pattern.rstrip("*").rstrip("-")
                if idx.startswith(prefix):
                    sf = field
                    break
            # Try the chosen field, then fallbacks
            candidates = ([sf] if sf else []) + _FALLBACK_TIME_FIELDS
            for f in candidates:
                v = hit.get(f)
                if v is not None:
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        pass
            return 0

        reverse = (req.sort_direction == "desc")
        all_hits.sort(key=_sort_key, reverse=reverse)

        logger.info("[multi-run] merged total_hits=%s across %s groups (sort_direction=%s)",
                    len(all_hits), len(req.per_index_queries), req.sort_direction)
        return {"total_hits": len(all_hits), "hits": all_hits, "per_index_meta": meta}
    except Exception as e:
        return {"error": str(e)}


class AggregateRequest(BaseModel):
    """Group-by aggregation over one or more per-index queries."""
    per_index_queries: list[dict]     # [{index, query_body}, ...] — same shape as multi-run
    group_by: list[str]               # 1+ fields; nested terms aggs in this order
    metric_field: str = ""            # optional numeric field → stats per group
    size: int = 100                   # max buckets per group level


def _build_nested_terms_aggs(fields: list[str], metric_field: str, size: int) -> dict:
    """Innermost-out: terms agg per group field, optional stats on the metric."""
    inner: dict = {}
    if metric_field:
        inner = {"m": {"stats": {"field": metric_field}}}
    aggs = inner
    for i in range(len(fields) - 1, -1, -1):
        level = {"terms": {"field": fields[i], "size": size}}
        if aggs:
            level = {"terms": level["terms"], "aggs": aggs}
        aggs = {f"g{i}": level}
    return aggs


def _flatten_agg_buckets(node: dict, fields: list[str], level: int,
                         key_prefix: tuple, out: dict, size: int) -> bool:
    """Walk nested terms buckets, accumulating rows into *out* keyed by the
    group-value tuple. Returns True if any level looked truncated."""
    truncated = False
    container = node.get(f"g{level}", {})
    buckets = container.get("buckets", [])
    if len(buckets) >= size or container.get("sum_other_doc_count"):
        truncated = True
    for b in buckets:
        key = key_prefix + (str(b.get("key_as_string", b.get("key"))),)
        if level + 1 < len(fields):
            if _flatten_agg_buckets(b, fields, level + 1, key, out, size):
                truncated = True
            continue
        row = out.setdefault(key, {"count": 0, "stats": None})
        row["count"] += b.get("doc_count", 0)
        st = b.get("m")
        if st and st.get("count"):
            # Merge stats exactly: sums/counts add, min/max compare, avg derived.
            cur = row["stats"] or {"count": 0, "sum": 0.0, "min": None, "max": None}
            cur["count"] += st.get("count", 0)
            cur["sum"]   += st.get("sum") or 0.0
            for k, fn in (("min", min), ("max", max)):
                v = st.get(k)
                if v is not None:
                    cur[k] = v if cur[k] is None else fn(cur[k], v)
            row["stats"] = cur
    return truncated


@router.post("/query/aggregate")
def aggregate_query(req: AggregateRequest):
    """
    Aggregate the documents matching each per-index query, grouped by one or
    more fields (nested terms aggs), optionally with numeric stats per group.
    Buckets from different index groups are merged exactly: counts and sums
    add up, min/max compare, avg is re-derived from the merged sum/count.
    """
    fields = [f.strip() for f in req.group_by if f and f.strip()]
    if not fields:
        return {"error": "select at least one field to group by"}
    items = [it for it in req.per_index_queries if it.get("index")]
    if not items:
        return {"error": "no query provided"}
    size = max(1, min(int(req.size or 100), 1000))

    try:
        es = get_client()
        merged: dict = {}
        truncated = False

        for it in items:
            index = it["index"]
            query = (it.get("query_body") or {}).get("query", {"match_all": {}})

            def _run(group_fields: list[str]) -> dict:
                body = {"size": 0, "query": query,
                        "aggs": _build_nested_terms_aggs(group_fields, req.metric_field, size)}
                return es.search(index, body)

            # ES 7+ text fields can't be aggregated directly — retry with the
            # conventional .keyword sub-fields (same trick as /indices/field-values).
            try:
                resp = _run(fields)
            except Exception as first_err:
                try:
                    resp = _run([f if f.endswith(".keyword") or f == "_index"
                                 else f + ".keyword" for f in fields])
                except Exception:
                    raise first_err

            if _flatten_agg_buckets(resp.get("aggregations", {}), fields, 0,
                                    (), merged, size):
                truncated = True

        rows = []
        for key, data in merged.items():
            row = {fields[i]: key[i] for i in range(len(fields))}
            row["count"] = data["count"]
            st = data.get("stats")
            if st and st.get("count"):
                row["sum"] = _round(st["sum"])
                row["avg"] = _round(st["sum"] / st["count"]) if st["count"] else None
                row["min"] = _round(st.get("min"))
                row["max"] = _round(st.get("max"))
            rows.append(row)
        rows.sort(key=lambda r: -(r.get("count") or 0))

        logger.info("[aggregate] group_by=%s metric=%r groups=%s rows=%s truncated=%s",
                    fields, req.metric_field, [it["index"] for it in items],
                    len(rows), truncated)
        return {"rows": rows[:10_000], "group_by": fields,
                "metric_field": req.metric_field, "truncated": truncated}
    except Exception as e:
        logger.error("[aggregate] error: %s", e)
        return {"error": str(e)}


@router.post("/query/export")
def export_results(req: ExportRequest):
    """
    Export ALL documents matching a query (single or multi-index) by scrolling
    Elasticsearch — not limited by the on-screen page size.

    The response is *streamed*: rows are flushed to the client as each scroll
    batch arrives, so memory stays flat and any rows sent before a mid-stream
    failure are preserved in the downloaded file (the stream just ends early).
    """
    try:
        es = get_client()
    except Exception as exc:
        return Response(json.dumps({"error": str(exc)}),
                        media_type="application/json", status_code=500)

    items = req.per_index_queries or (
        [{"index": req.index, "query_body": req.query_body}] if req.index else [])
    items = [it for it in items if it.get("index")]
    if not items:
        return Response(json.dumps({"error": "no query provided"}),
                        media_type="application/json", status_code=400)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    if req.format == "json":
        gen = _stream_json(es, items, req.max_rows)
        return StreamingResponse(
            gen, media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="cc_export_{ts}.json"'})

    # CSV: derive a stable column header from the indices' mappings up front
    # (cheap, one _mapping call per group) so we can stream rows without buffering.
    # Use TOP-LEVEL field names — nested objects (e.g. "geo") stay one column and
    # are JSON-stringified, matching the document's source shape.
    cols = ["_id", "_index"]
    seen = set(cols)
    for it in items:
        for f in _collect_top_fields(es, it["index"]):
            if f not in seen:
                seen.add(f); cols.append(f)

    gen = _stream_csv(es, items, req.max_rows, cols)
    return StreamingResponse(
        gen, media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="cc_export_{ts}.csv"'})


def _collect_top_fields(es, index: str) -> list[str]:
    """Return TOP-LEVEL field names from the mapping of *index* (no recursion),
    so nested objects remain a single CSV column rather than exploding into leaves."""
    names: list[str] = []
    seen: set[str] = set()
    try:
        mapping_resp = es.get(f"/{index}/_mapping", params={})
    except Exception as exc:
        logger.warning("[_collect_top_fields] index=%r error=%s", index, exc)
        return names

    def _top_props(mappings) -> dict:
        if not isinstance(mappings, dict):
            return {}
        if isinstance(mappings.get("properties"), dict):     # ES 5+ form
            return mappings["properties"]
        props: dict = {}                                     # ES 1.x: type -> properties
        for _type, tdata in mappings.items():
            if isinstance(tdata, dict) and isinstance(tdata.get("properties"), dict):
                props.update(tdata["properties"])
        return props

    for _idx, idx_data in mapping_resp.items():
        if not isinstance(idx_data, dict):
            continue
        for fname in _top_props(idx_data.get("mappings", {})):
            if fname not in seen:
                seen.add(fname); names.append(fname)
    return names


def _scroll_hits(es, index: str, query: dict, page: int = 1000):
    """Yield raw ES hits (with _id/_type/_index/_source) matching *query* in
    *index* via a plain scroll, one at a time (flat memory).

    Uses a regular scroll (not search_type=scan, which some ES builds reject):
    the initial search already returns the first batch + a scroll_id, then we
    page through /_search/scroll until exhausted.
    """
    resp = es.post(f"/{index}/_search?scroll=1m", {"query": query, "size": page})
    while True:
        hits = resp.get("hits", {}).get("hits", [])
        if not hits:
            break
        for h in hits:
            yield h
        sid = resp.get("_scroll_id")
        if not sid:
            break
        resp = es.post("/_search/scroll", {"scroll": "1m", "scroll_id": sid})


def _scroll_iter(es, index: str, query: dict, page: int = 1000):
    """Flattened {_id,_index,**_source} rows — used by the CSV/JSON export."""
    for h in _scroll_hits(es, index, query, page):
        yield {"_id": h.get("_id"), "_index": h.get("_index"), **h.get("_source", {})}


def _csv_cell(v):
    """Stringify a CSV cell — nested objects/arrays become compact JSON."""
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return v


def _csv_line(values: list) -> str:
    """Render one RFC-4180 CSV line (CRLF-terminated)."""
    import csv, io
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\r\n").writerow(values)
    return buf.getvalue()


def _stream_csv(es, items: list, max_rows: int, cols: list):
    """Generator yielding CSV text: header first, then a line per scrolled row.
    On a scroll error it stops cleanly — rows already yielded are kept."""
    yield _csv_line(cols)
    count = 0
    for it in items:
        index = it["index"]
        query = (it.get("query_body") or {}).get("query", {"match_all": {}})
        try:
            for row in _scroll_iter(es, index, query):
                yield _csv_line([_csv_cell(row.get(c)) for c in cols])
                count += 1
                if count >= max_rows:
                    logger.info("[export-csv] row cap %s reached", max_rows)
                    return
        except Exception as exc:
            logger.error("[export-csv] %s scroll error after %s rows: %s", index, count, exc)
            return   # partial CSV already flushed to the client
    logger.info("[export-csv] streamed %s rows from %s group(s)", count, len(items))


def _stream_json(es, items: list, max_rows: int):
    """Generator yielding a JSON object: {"hits":[...],"total":N,"truncated":bool}.
    A mid-stream error is reported in the trailing "error" field; rows already
    emitted stay valid."""
    yield '{"hits":['
    count = 0
    first = True
    truncated = False
    err = None
    for it in items:
        index = it["index"]
        query = (it.get("query_body") or {}).get("query", {"match_all": {}})
        try:
            for row in _scroll_iter(es, index, query):
                yield ("" if first else ",") + json.dumps(row, default=str)
                first = False
                count += 1
                if count >= max_rows:
                    truncated = True
                    break
        except Exception as exc:
            err = f"{index}: {exc}"
            logger.error("[export-json] scroll error after %s rows: %s", count, exc)
            break
        if truncated:
            break
    tail = f'],"total":{count},"truncated":{"true" if truncated else "false"}'
    if err:
        tail += ',"error":' + json.dumps(err)
    tail += '}'
    yield tail
    logger.info("[export-json] streamed %s rows  truncated=%s  err=%s", count, truncated, bool(err))


class DocUpdateRequest(BaseModel):
    index: str                 # concrete index (the row's _index)
    id:    str                 # document _id
    field: str                 # top-level field name (the table column)
    op:    str = "set"         # "set" | "delete"
    value: Any = None          # new value (for "set")


def _coerce_value(value, old):
    """Coerce the user's input to the existing field's JSON type when possible."""
    if isinstance(old, bool):
        return value if isinstance(value, bool) else str(value).strip().lower() in ("true", "1", "yes")
    if isinstance(old, int) and not isinstance(old, bool):
        try:    return int(value)
        except (TypeError, ValueError):
            try: return float(value)
            except (TypeError, ValueError): return value
    if isinstance(old, float):
        try:    return float(value)
        except (TypeError, ValueError): return value
    return value


@router.post("/doc/update")
def doc_update(req: DocUpdateRequest):
    """
    Edit or delete a single field on one document (by _id) and re-index it.

      op="set"    → set `field` to `value` (type-coerced to the old field's type)
      op="delete" → remove `field` from the document
    """
    try:
        es = get_client()
        # Look the doc up by _id to discover its _type and current _source.
        resp = es.search(req.index, {"query": {"ids": {"values": [req.id]}}, "size": 1})
        hits = resp.get("hits", {}).get("hits", [])
        if not hits:
            return {"error": f"document _id={req.id!r} not found in {req.index!r}"}

        hit   = hits[0]
        dtype = hit.get("_type")
        idx   = hit.get("_index", req.index)
        src   = hit.get("_source", {}) or {}

        if req.op == "delete":
            if req.field not in src:
                return {"error": f"field {req.field!r} not present on document"}
            src.pop(req.field, None)
            new_value = None
        elif req.op == "set":
            new_value = _coerce_value(req.value, src.get(req.field))
            src[req.field] = new_value
        else:
            return {"error": f"unknown op {req.op!r}"}

        es.put(f"/{idx}/{dtype}/{req.id}?refresh=true", src)
        logger.info("[doc-update] %s/%s/%s  op=%s  field=%s -> %r",
                    idx, dtype, req.id, req.op, req.field, new_value)
        return {"ok": True, "op": req.op, "field": req.field, "value": new_value}
    except Exception as e:
        logger.error("[doc-update] error: %s", e)
        return {"error": str(e)}


class BulkDeleteRequest(BaseModel):
    scope: str = "selected"          # "selected" (explicit docs) | "all" (whole query)
    docs: list[dict] = []            # [{index, id}] when scope="selected"
    per_index_queries: list[dict] = []   # when scope="all"
    max_docs: int = 100_000


class BulkFieldRequest(BaseModel):
    field: str
    op: str = "set"                  # "set" | "delete"
    value: Any = None
    scope: str = "selected"
    docs: list[dict] = []
    per_index_queries: list[dict] = []
    max_docs: int = 100_000


def _resolve_types(es, index: str, ids: list[str]) -> dict:
    """Map _id -> _type for the given ids in an index (batched ids search)."""
    out: dict = {}
    for i in range(0, len(ids), 1000):
        chunk = ids[i:i + 1000]
        resp = es.search(index, {"query": {"ids": {"values": chunk}},
                                 "size": len(chunk), "_source": False})
        for h in resp.get("hits", {}).get("hits", []):
            out[h.get("_id")] = h.get("_type")
    return out


def _gather_op_hits(es, req, want_source: bool):
    """Yield (index, type, id, source) triples for a bulk op, honoring scope.

    scope="all"  → scroll every doc matching each per_index_query.
    scope="selected" → look up the explicit {index,id} docs (grouped per index).
    """
    count = 0
    if req.scope == "all":
        for it in req.per_index_queries:
            index = it.get("index")
            if not index:
                continue
            query = (it.get("query_body") or {}).get("query", {"match_all": {}})
            for h in _scroll_hits(es, index, query):
                yield h.get("_index"), h.get("_type"), h.get("_id"), (h.get("_source") or {})
                count += 1
                if count >= req.max_docs:
                    return
    else:
        by_index: dict = {}
        for d in req.docs:
            if d.get("index") and d.get("id") is not None:
                by_index.setdefault(d["index"], []).append(str(d["id"]))
        for index, ids in by_index.items():
            for i in range(0, len(ids), 1000):
                chunk = ids[i:i + 1000]
                body = {"query": {"ids": {"values": chunk}}, "size": len(chunk)}
                if not want_source:
                    body["_source"] = False
                resp = es.search(index, body)
                for h in resp.get("hits", {}).get("hits", []):
                    yield (h.get("_index"), h.get("_type"), h.get("_id"),
                           (h.get("_source") or {}))


@router.post("/docs/bulk-delete")
def bulk_delete(req: BulkDeleteRequest):
    """Delete whole documents — either an explicit selection or every doc
    matching the current query (scope="all")."""
    try:
        es = get_client()
        actions: list = []
        for idx, dtype, _id, _src in _gather_op_hits(es, req, want_source=False):
            if idx and dtype and _id is not None:
                actions.append(json.dumps({"delete": {"_index": idx, "_type": dtype, "_id": _id}}))
        if not actions:
            return {"deleted": 0}
        es.bulk("\n".join(actions) + "\n", refresh=True)
        logger.info("[bulk-delete] scope=%s deleted=%s", req.scope, len(actions))
        return {"deleted": len(actions)}
    except Exception as e:
        logger.error("[bulk-delete] error: %s", e)
        return {"error": str(e)}


@router.post("/docs/bulk-field")
def bulk_field(req: BulkFieldRequest):
    """Set or delete one field across many documents (selection or whole query)."""
    try:
        es = get_client()
        lines: list = []
        n = 0
        for idx, dtype, _id, src in _gather_op_hits(es, req, want_source=True):
            if not (idx and dtype and _id is not None):
                continue
            if req.op == "delete":
                if req.field not in src:
                    continue
                src.pop(req.field, None)
            elif req.op == "set":
                src[req.field] = _coerce_value(req.value, src.get(req.field))
            else:
                return {"error": f"unknown op {req.op!r}"}
            lines.append(json.dumps({"index": {"_index": idx, "_type": dtype, "_id": _id}}))
            lines.append(json.dumps(src, default=str))
            n += 1
        if lines:
            es.bulk("\n".join(lines) + "\n", refresh=True)
        logger.info("[bulk-field] scope=%s op=%s field=%s updated=%s",
                    req.scope, req.op, req.field, n)
        return {"updated": n}
    except Exception as e:
        logger.error("[bulk-field] error: %s", e)
        return {"error": str(e)}


@router.get("/cc/traffic")
def traffic_summary():
    """Aggregated DP traffic data by device."""
    try:
        es = get_client()
        body = {
            "size": 0,
            "aggs": {
                "by_device": {
                    "terms": {"field": "deviceIp.keyword", "size": 50},
                    "aggs": {"latest_ts": {"max": {"field": "startTime"}}},
                },
                "traffic_over_time": {
                    "date_histogram": {"field": "startTime", "interval": "1h"},
                    "aggs": {
                        "avg_in":  {"avg": {"field": "inBandwidth"}},
                        "avg_out": {"avg": {"field": "outBandwidth"}},
                    },
                },
            },
        }
        _log_es_query("/api/cc/traffic", "dp-traffic-agg-*", body)
        resp = es.search("dp-traffic-agg-*", body)
        aggs = resp.get("aggregations", {})
        hits_total = resp.get("hits", {}).get("total")
        return {
            "total_records":     hits_total.get("value") if isinstance(hits_total, dict) else hits_total,
            "by_device":         _buckets(aggs, "by_device"),
            "traffic_over_time": _date_buckets(aggs, "traffic_over_time"),
        }
    except Exception as e:
        return {"error": str(e)}


# ── Helpers ───────���───────────────────────────────────────────────────────────

def _val(v):
    """Return None for N_A / empty strings, otherwise return the value."""
    if v is None or str(v).strip() in ("N_A", "null", ""):
        return None
    return v

def _round(v, ndigits=2):
    """Round a float, return None if falsy."""
    if v is None:
        return None
    try:
        return round(float(v), ndigits)
    except (TypeError, ValueError):
        return None

def _extract_attack_type(index_name: str) -> str:
    """
    Extract attack type from dp-attack-raw-ty-{type}-sid-... index name.
    e.g. 'dp-attack-raw-ty-dns__flood-sid-0-sl-1472' → 'Dns Flood'
    """
    if "-ty-" in index_name:
        after_ty  = index_name.split("-ty-", 1)[1]
        type_part = after_ty.split("-sid-")[0] if "-sid-" in after_ty else after_ty
        return type_part.replace("__", " ").replace("_", " ").title()
    return ""

def _first_bucket(agg_bucket: dict, key: str):
    """Return the top bucket key (ignoring N_A), or None."""
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

def _epoch_to_iso(ms) -> str | None:
    """Convert epoch-milliseconds to ISO-8601 UTC string."""
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(float(ms) / 1000, tz=timezone.utc).isoformat()
    except Exception:
        return None

def _fmt_stats(stats: dict) -> dict:
    return {
        "count": stats.get("count"),
        "min_s": _ms_to_s(stats.get("min")),
        "max_s": _ms_to_s(stats.get("max")),
        "avg_s": _ms_to_s(stats.get("avg")),
    }

def _hist_buckets(aggs: dict, key: str) -> list:
    return [
        {"date": b.get("key_as_string"), "ts": b.get("key"), "count": b.get("doc_count")}
        for b in aggs.get(key, {}).get("buckets", [])
        if b.get("doc_count", 0) > 0
    ]

def _compute_gaps(sorted_timestamps: list) -> dict:
    """Given sorted list of epoch-ms timestamps, return gap stats in seconds."""
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

def _buckets(aggs: dict, key: str) -> list:
    return [{"key": b.get("key"), "count": b.get("doc_count")}
            for b in aggs.get(key, {}).get("buckets", [])]


def _date_buckets(aggs: dict, key: str) -> list:
    result = []
    for b in aggs.get(key, {}).get("buckets", []):
        entry = {"date": b.get("key_as_string"), "count": b.get("doc_count")}
        for k, v in b.items():
            if k not in ("key", "key_as_string", "doc_count") and isinstance(v, dict) and "value" in v:
                entry[k] = v["value"]
        result.append(entry)
    return result


def _resolve_date_field(es, index: str, hint: str) -> str:
    """Legacy wrapper — prefer _pick_date_field(_get_date_fields(...), hint)."""
    return _pick_date_field(_get_date_fields(es, index), hint)


# ── Free-text → ES field resolution ─────────────────────────────────────────
# Word synonyms so human phrasing maps onto real field names regardless of the
# exact spelling used in the index (e.g. "ip" also matches "...Address" fields).
_FIELD_WORD_SYNONYMS: dict[str, tuple[str, ...]] = {
    "ip":          ("ip", "address", "addr"),
    "address":     ("address", "addr", "ip"),
    "addr":        ("addr", "address", "ip"),
    "id":          ("id", "identifier"),
    "identifier":  ("identifier", "id"),
    "src":         ("source", "src"),
    "source":      ("source", "src"),
    "dst":         ("dest", "destination"),
    "dest":        ("dest", "destination"),
    "destination": ("destination", "dest"),
    "target":      ("dest", "destination", "target"),
    "proto":       ("protocol", "proto"),
    "protocol":    ("protocol", "proto"),
    "type":        ("type", "category", "name"),
}

# Words that separate two distinct references in free text.
_REF_BOUNDARY = {"and", "or", "but", "then", "where"}
# Connective words to skip (not part of the field name) while scanning back.
_REF_FILLER = {
    "is", "are", "was", "were", "of", "for", "the", "a", "an", "to", "that",
    "equals", "equal", "named", "called", "on", "in", "by", "whose", "which",
    "has", "with", "value",
}
# Operator markers — detected while scanning back from a value.
_OP_CONTAINS = {"contains", "containing", "contain", "like", "includes",
                "including", "include", "matching", "matches"}
_OP_NEGATE   = {"not", "without", "no", "isnt", "arent", "exclude",
                "excluding", "except", "doesnt", "dont", "isn", "aren"}
# Connectives that signal "a value follows" — lets a bare word (e.g. "Incorrect")
# be treated as a value: 'attack name contains Incorrect', 'name is blacklist'.
_CONNECTIVE_WORDS = {"is", "are", "was", "were", "equals", "equal",
                     "named", "called"}
# Bare-word values that are really category/status/risk keywords are handled by
# their own dedicated blocks — don't double-capture them as field values.
_STOP_VALUE_WORDS = {
    "dns", "webddos", "behavioraldos", "synflood", "intrusions", "intrusion",
    "anomalies", "anomaly", "antiscanning", "acl", "statefulacl", "dosshield",
    "trafficfilters", "active", "closed", "terminated", "open",
    "high", "medium", "low", "blocking", "blocked",
}


def _norm_field(s: str) -> str:
    """Lower-case and strip non-alphanumerics for fuzzy field-name matching."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _field_tokens(name: str) -> list[str]:
    """Split a field name into lower-case word tokens (camelCase + separators).

    e.g. "attackIpsId" -> ["attack","ips","id"]; "destAddress" -> ["dest","address"].
    Token-level matching avoids false hits like "ip" matching inside "attackIpsId".
    """
    s = re.sub(r"[^A-Za-z0-9]+", " ", name)
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)   # camelCase boundary
    s = re.sub(r"(?<=[A-Za-z])(?=[0-9])", " ", s)    # letter→digit boundary
    return [t.lower() for t in s.split() if t]


def _word_matches_tokens(word: str, tokens: list[str]) -> bool:
    """True if a descriptor word (via synonyms) matches a field token.

    Short synonyms (<4 chars, e.g. "ip", "id") require an exact token match;
    longer ones may match as a prefix/substring of a token (e.g. "dest" in
    "destination", "footprint" in "footprint")."""
    for syn in _FIELD_WORD_SYNONYMS.get(word, (word,)):
        for tok in tokens:
            if tok == syn or (len(syn) >= 4 and syn in tok):
                return True
    return False


# Words that OR two values of the SAME field together ("A or B", "A nor B").
_OR_WORDS = {"or", "nor"}


def _is_or_continuation(tokens: list[str], prev_i: int, i: int) -> bool:
    """True if the value at *i* is another OR-ed value for the SAME field as the
    value at *prev_i* — i.e. the only things between them are OR connectives
    and/or commas (no "and", no new descriptor word, no boundary). This lets
    "sourceIp is A or B or C" (or "A, B or C") collapse into one terms clause,
    while "... and policyName is X" stays a separate field clause."""
    saw_or = saw_comma = False
    for t in tokens[prev_i + 1:i]:
        low = t.lower()
        if low in _OR_WORDS:
            saw_or = True
            continue
        if t in (",", ";"):
            saw_comma = True
            continue
        if low == "and":                 # hard boundary between distinct fields
            return False
        if t.isalpha():                  # a new descriptor word → new field clause
            return False
        # other punctuation ("=", ":", "(", ...) is ignored between OR-ed values
    return saw_or or saw_comma


def _extract_field_refs(text: str) -> list[dict]:
    """
    Pull "<descriptor words> [operator] <value(s)>" references out of free text.

    e.g. "attack ID 26-1781864824"    -> words=["attack","id"], values=["26-1781864824"], op="eq"
         "name contains \"flood\""     -> words=["name"],        values=["flood"],          op="contains"
         "category not BehavioralDOS"  -> words=["category"],    values=["..."],            op="neq"

    Consecutive values joined by OR for the SAME field ("A or B or C", "A, B or
    C") are merged into ONE ref carrying multiple `values` — so they become a
    single terms/should clause (OR within a field). Distinct fields separated by
    "and" stay separate refs (AND across fields).

    `op` is one of: "eq" (match/terms), "contains" (wildcard *v*), "neq"
    (must_not match/terms), "ncontains" (must_not wildcard). The descriptor words
    are resolved to a real field per-index from the mapping — we never assume a
    field name exists.
    """
    refs: list[dict] = []
    # Tokenize keeping IPv4 and compound tokens (embedded "-"/"_", e.g. "PO_7_1"
    # or "26-1781864824") intact — user-typed names/values must never be split
    # at separators. A value is either an always-value token (quoted/IP/id/
    # number/mixed-alnum/compound-with-digit) or a bare word that follows a
    # connective ("contains Incorrect", "is blacklist").
    tokens = re.findall(
        r'"[^"]*"|\d{1,3}(?:\.\d{1,3}){3}|[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)+|[A-Za-z0-9]+|[^\s]', text)

    prev_val_i: int | None = None      # token index of the last emitted value

    for i, tok in enumerate(tokens):
        # OR-continuation: this value belongs to the previous ref's field (only
        # OR connectives / commas sit between them). Inherits that ref's
        # descriptor words + operator instead of re-deriving them — walking back
        # would break at the "or" boundary and lose the field.
        or_cont = (bool(refs) and prev_val_i is not None
                   and _is_or_continuation(tokens, prev_val_i, i))

        kind, value = _classify_value(tok)
        if kind is None:
            # Bare alphabetic word (or alpha-only compound like "web-based")
            # counts as a value only after a connective — or as another OR-ed
            # value for the current field ("contains flood or storm") — and only
            # if it isn't itself an operator/negation/connective word.
            low_tok = tok.lower()
            if (re.fullmatch(r"[A-Za-z]+(?:[-_][A-Za-z]+)*", tok)
                    and low_tok not in _STOP_VALUE_WORDS
                    and low_tok not in _OP_NEGATE
                    and low_tok not in _OP_CONTAINS
                    and low_tok not in _CONNECTIVE_WORDS
                    and low_tok not in _REF_FILLER
                    and low_tok not in _REF_BOUNDARY
                    and (or_cont or _gated_by_connective(tokens, i))):
                kind, value = "str", tok
            else:
                continue

        if or_cont:
            if value not in refs[-1]["values"]:
                refs[-1]["values"].append(value)
            prev_val_i = i
            continue

        # Walk back over preceding tokens to collect descriptor words and ops.
        words: list[str] = []
        contains = negate = False
        j = i - 1
        while j >= 0:
            t = tokens[j]
            low = t.lower()
            if low in _REF_BOUNDARY:
                break
            if not t.isalpha():
                if t in (",", ";"):
                    break
                if t == "!":
                    negate = True
                j -= 1
                continue
            if low in _OP_CONTAINS:
                contains = True
            elif low in _OP_NEGATE:
                negate = True
            elif low in _CONNECTIVE_WORDS or low in _REF_FILLER:
                pass
            else:
                words.insert(0, low)
                if len(words) >= 3:
                    break
            j -= 1

        op = ("ncontains" if contains and negate else
              "contains"  if contains else
              "neq"       if negate else "eq")
        refs.append({"words": words, "values": [value], "kind": kind, "op": op})
        prev_val_i = i
    return refs


def _classify_value(tok: str) -> tuple[str | None, str | None]:
    """Classify an always-value token; returns (kind, value) or (None, None)."""
    if len(tok) >= 2 and tok[0] == '"' and tok[-1] == '"':
        return "str", tok[1:-1]
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", tok):                 return "ip", tok
    if re.fullmatch(r"\d+(?:[-_]\d+)+", tok):                        return "id", tok
    if re.fullmatch(r"\d{2,}", tok):                                 return "num", tok
    if re.fullmatch(r"[A-Za-z]+\d[A-Za-z0-9]*|\d+[A-Za-z][A-Za-z0-9]*", tok):
        return "str", tok
    # Compound token containing a digit (e.g. "PO_7_1", "rule-7a") — a value.
    # Alpha-only compounds ("web-based") stay bare words needing a connective.
    if ("-" in tok or "_" in tok) and any(ch.isdigit() for ch in tok) \
            and re.fullmatch(r"[A-Za-z0-9_-]+", tok):
        return "str", tok
    return None, None


def _gated_by_connective(tokens: list[str], i: int) -> bool:
    """True if the token before position *i* (skipping negators) is a connective."""
    j = i - 1
    while j >= 0 and tokens[j].lower() in _OP_NEGATE:
        j -= 1
    if j < 0:
        return False
    t = tokens[j].lower()
    return t in _CONNECTIVE_WORDS or t in _OP_CONTAINS or tokens[j] in ("=", ":")


# Anchor words for field-existence predicates.
_EXIST_POS = {"exist", "exists", "existing", "present", "populated", "set"}
_EXIST_NEG = {"missing", "absent", "empty", "unset", "null", "none"}
_EXIST_SKIP = {"field", "fields", "is", "are", "does", "do", "the", "a", "an",
               "value", "values", "that", "which", "has", "have", "any", "an"}


def _extract_existence_refs(text: str) -> list[dict]:
    """
    Detect field-existence predicates that carry no value, e.g.
      "footprint field exists"      -> words=["footprint"], present=True
      "footprint does not exist"    -> words=["footprint"], present=False
      "missing footprint" / "no vlan" -> present=False (field words follow the anchor)

    Returns [{words: [...], present: bool}].
    """
    refs: list[dict] = []
    tokens = re.findall(r"[A-Za-z]+|[^\sA-Za-z]", text)
    low = [t.lower() for t in tokens]

    def _collect(start: int, step: int) -> list[str]:
        words: list[str] = []
        j = start
        while 0 <= j < len(low):
            tok, lw = tokens[j], low[j]
            if lw in _REF_BOUNDARY:
                break
            if not tok.isalpha():
                if tok in (",", ";"):
                    break
                j += step
                continue
            if lw in _EXIST_SKIP or lw in _EXIST_POS or lw in _EXIST_NEG \
                    or lw in _OP_NEGATE:
                j += step
                continue
            words.append(lw)
            if len(words) >= 3:
                break
            j += step
        return words[::-1] if step < 0 else words

    for i, t in enumerate(low):
        if t in _EXIST_POS:
            present, anchor = True, "pos"
        elif t in _EXIST_NEG:
            present, anchor = False, "neg"
        elif t in ("no", "without"):
            present, anchor = False, "prefix"
        else:
            continue

        # Negation just before a positive anchor ("does not exist", "isn't present")
        if anchor == "pos" and any(w in _OP_NEGATE for w in low[max(0, i - 3):i]):
            present = False

        # Field words usually precede the anchor; for prefix negators they follow.
        words = _collect(i - 1, -1)
        if not words:
            words = _collect(i + 1, +1)
        if words:
            refs.append({"words": words, "present": present})
    return refs


def _resolve_field_by_words(words: list[str], field_names: list[str]) -> tuple[str | None, str]:
    """
    Resolve descriptor words to a field name.

    The word nearest the value (the last one — typically the field-type word like
    "id", "ip", "name") must match; this filters out noise words ("from", "attacks").
    Among fields that match the nearest word, the one matching the most descriptor
    words wins, tie-broken by the shortest (most specific) name.

    Matching is via `_FIELD_WORD_SYNONYMS`, so "ip" also matches "...Address" fields.
    Returns (field|None, reason).
    """
    field, _score, _total, reason = _resolve_field_scored(words, field_names)
    return field, reason


def _resolve_field_scored(words: list[str], field_names: list[str]) -> tuple[str | None, int, int, str]:
    """
    Like `_resolve_field_by_words` but also reports match quality:
    returns (field|None, matched_words, total_words, reason).

    A *confident* match has matched_words == total_words (every descriptor word
    found in the field name). A field with matched_words < total_words is only a
    best-effort suggestion (e.g. "radware id" -> "attackIpsId" matches "id" but
    not "radware") and should be confirmed by the user before querying.
    """
    if not words:
        return None, 0, 0, "no descriptor words"

    total   = len(words)

    # Fast path: the descriptor(s) spell out a field name exactly once separators
    # and case are ignored — e.g. the user types the real field name as one
    # camelCase token ("sourceIp", "attackIpsId", "policyName") or space-split
    # ("attack ips id"). Token-by-token matching misses this because a single
    # concatenated word never equals the field's individual tokens. Treat an
    # exact normalized-name hit as a confident full match.
    joined = _norm_field("".join(words))
    if joined:
        exact = [f for f in field_names if _norm_field(f) == joined]
        if exact:
            exact.sort(key=len)          # prefer the shortest/base name on ties
            return exact[0], total, total, f"exact field-name match -> '{exact[0]}'"

    nearest = words[-1]
    best, best_score, best_len = None, -1, 1_000_000

    for f in field_names:
        tokens = _field_tokens(f)
        if not _word_matches_tokens(nearest, tokens):
            continue   # nearest (most significant) word must match a token
        score = sum(1 for w in words if _word_matches_tokens(w, tokens))
        nf_len = len(_norm_field(f))
        if score > best_score or (score == best_score and nf_len < best_len):
            best, best_score, best_len = f, score, nf_len

    if best is None:
        return None, 0, total, f"no field matched nearest word '{nearest}'"
    return best, best_score, total, f"nearest '{nearest}' matched; score={best_score}/{total} -> '{best}'"


def _candidate_fields(words: list[str], field_names: list[str], limit: int = 6) -> list[dict]:
    """Fields matching at least one descriptor word, ranked best-first.

    Used to offer the user a choice when no field matches every word
    (e.g. "policy name" -> [{name}, {ruleName}, ...]). Ranked by number of
    matched words (desc), then shortest name."""
    scored: list[tuple[int, int, str]] = []
    for f in field_names:
        tokens = _field_tokens(f)
        score = sum(1 for w in words if _word_matches_tokens(w, tokens))
        if score >= 1:
            scored.append((score, len(_norm_field(f)), f))
    scored.sort(key=lambda t: (-t[0], t[1], t[2]))
    return [{"field": f, "score": sc} for sc, _ln, f in scored[:limit]]


# ── Data-driven enum values (status / risk / category …) ─────────────────────
_ENUM_CACHE: dict = {}        # (base_url, index, fields) -> (ts, {field:{term:canonical}})
_ENUM_TTL = 120               # seconds

# Used only when ES can't be probed.
_ENUM_FALLBACK = {
    "status":   ["Active", "Terminated"],
    "risk":     ["High", "Medium", "Low"],
    "category": ["DNS", "WebDDoS", "BehavioralDOS", "SynFlood", "Intrusions",
                 "Anomalies", "AntiScanning", "ACL", "StatefulACL", "DOSShield",
                 "TrafficFilters"],
}
# Synonyms that aren't substrings of the canonical value (added only when the
# canonical value actually exists for that field).
_ENUM_SYNONYMS = {
    "status": {"closed": "Terminated", "terminated": "Terminated", "active": "Active"},
}


def _enum_tokens(value: str) -> list[str]:
    """Split an enum value into lowercase parts (camelCase + acronym aware)."""
    s = re.sub(r"[^A-Za-z0-9]+", " ", value)
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s)   # acronym → word boundary
    s = re.sub(r"(?<=[A-Za-z])(?=[0-9])", " ", s)
    return [t.lower() for t in s.split() if t]


def _enum_pattern(term: str):
    """Regex matching *term* in free text, tolerant of separators/case
    (so "WebDDoS" matches 'web ddos', 'webddos', 'web-ddos')."""
    parts = _enum_tokens(term) or [term.lower()]
    body = r"[\s_\-]*".join(re.escape(p) for p in parts)
    return re.compile(r"(?<![A-Za-z0-9])" + body + r"(?![A-Za-z0-9])", re.IGNORECASE)


def _get_enum_maps(es, index: str, fields: list[str]) -> dict:
    """Return {field: {search_term: canonical_value}} for *fields*, taken from a
    terms aggregation over *index* (briefly cached). For real values the search
    term IS the canonical value (camelCase preserved); known synonyms are added
    only when their canonical value exists. Falls back to a built-in list when
    ES can't be reached."""
    key = (getattr(es, "base_url", ""), index, tuple(fields))
    cached = _ENUM_CACHE.get(key)
    if cached and (time.time() - cached[0]) < _ENUM_TTL:
        return {f: dict(m) for f, m in cached[1].items()}

    maps: dict = {}
    for f in fields:
        vmap: dict = {}
        try:
            resp = es.search(index, {"size": 0,
                                     "aggs": {"v": {"terms": {"field": f, "size": 500}}}})
            for b in resp.get("aggregations", {}).get("v", {}).get("buckets", []):
                k = b.get("key")
                if isinstance(k, str) and k.strip():
                    vmap[k] = k                       # term == canonical value
        except Exception as exc:
            logger.debug("[enum] probe field=%r failed: %s", f, exc)
        if not vmap:                                  # fallback canonical list
            for canon in _ENUM_FALLBACK.get(f, []):
                vmap[canon] = canon
        real_canons = set(vmap.values())
        for syn, canon in _ENUM_SYNONYMS.get(f, {}).items():
            if canon in real_canons:
                vmap.setdefault(syn, canon)
        if vmap:
            maps[f] = vmap

    _ENUM_CACHE[key] = (time.time(), {f: dict(m) for f, m in maps.items()})
    return maps


def _collect_all_fields(es, index: str) -> list[str]:
    """Return all leaf field names from the mapping of *index* (robust walk)."""
    seen: set[str] = set()
    names: list[str] = []
    try:
        mapping_resp = es.get(f"/{index}/_mapping", params={})
    except Exception as exc:
        logger.warning("[_collect_all_fields] index=%r  error=%s", index, exc)
        return names

    def _walk(node) -> None:
        if not isinstance(node, dict):
            return
        props = node.get("properties")
        if isinstance(props, dict):
            for fname, fmeta in props.items():
                if isinstance(fmeta, dict) and "properties" not in fmeta \
                        and fname not in seen:
                    seen.add(fname)
                    names.append(fname)
        for value in node.values():
            _walk(value)

    _walk(mapping_resp)
    return names


def _collect_date_fields(es, index: str) -> list[tuple[str, bool]]:
    """
    Fetch the ES mapping for *index* and return all date-type fields as
    (field_name, sortable) tuples, deduplicated across the indices a wildcard
    expands to.

    `sortable` is False only when the field has doc_values explicitly disabled.

    Robust against the varying ES mapping nesting:
      ES 1.x: { idx: { mappings: { type: { properties: {...} } } } }
      ES 5+:  { idx: { mappings: { properties: {...} } } }
    Rather than assume a fixed depth (older/proxied ES servers occasionally put
    a bare string where a dict is expected — which crashed the previous fixed
    walk with "'str' object has no attribute 'get'"), this walks every dict node
    and harvests date fields from any "properties" container it encounters.
    """
    seen: set[str] = set()
    result: list[tuple[str, bool]] = []
    try:
        mapping_resp = es.get(f"/{index}/_mapping", params={})
    except Exception as exc:
        logger.warning("[_collect_date_fields] index=%r  error=%s", index, exc)
        return result

    def _walk(node) -> None:
        if not isinstance(node, dict):
            return
        props = node.get("properties")
        if isinstance(props, dict):
            for fname, fmeta in props.items():
                if isinstance(fmeta, dict) and fmeta.get("type") == "date" \
                        and fname not in seen:
                    seen.add(fname)
                    sortable = fmeta.get("doc_values", True) is not False
                    result.append((fname, sortable))
        # Recurse into every dict value to reach nested mappings / objects.
        for value in node.values():
            _walk(value)

    _walk(mapping_resp)
    return result


def _get_date_fields(es, index: str) -> list[str]:
    """Fetch the ES mapping for *index* and return all date-type field names."""
    return [name for name, _sortable in _collect_date_fields(es, index)]


def _pick_date_field(date_fields: list[str], hint: str) -> str:
    """Thin wrapper — returns only the field name."""
    field, _ = _pick_date_field_verbose(date_fields, hint)
    return field


def _pick_date_field_verbose(date_fields: list[str], hint: str) -> tuple[str, str]:
    """
    From *date_fields*, pick the best date field for a filter or sort.
    Returns (field_name, reason_string) so callers can log why a field was chosen.

    Selection priority for hint="end":
      1. Field whose name contains "end"           → e.g. "endTime"
      2. Field whose name does NOT contain "start" → e.g. "timeStamp" beats "startTime"
      3. First date field in the list              → last resort

    For hint="start":
      1. Field whose name contains "start"
      2. First date field in the list

    For hint="":
      1. First date field in the list

    When date_fields is empty, returns a hard-coded fallback
    ("endTime" for hint="end", "startTime" otherwise).
    """
    fallback = "endTime" if hint == "end" else "startTime"
    if not date_fields:
        return fallback, f"no date fields in mapping — hard-coded fallback '{fallback}'"

    if hint:
        # 1. Direct name match (e.g. "end" in "endTime")
        for f in date_fields:
            if hint.lower() in f.lower():
                return f, f"direct match: '{hint}' found in field name '{f}'"

        # 2. For "end" hint: prefer any field that doesn't contain "start"
        #    Covers dp-attack-extra-* where fields are ["startTime","timeStamp"]
        #    → "timeStamp" wins because it contains no "start"
        if hint == "end":
            for f in date_fields:
                if "start" not in f.lower():
                    return f, f"non-start fallback: '{f}' does not contain 'start' (fields={date_fields})"

    # 3. Fall back to first available date field
    return date_fields[0], f"first available date field '{date_fields[0]}' (fields={date_fields})"


def _pick_sort_field(date_fields: list[str]) -> str:
    """
    Choose the best date field for sorting.
    Priority: fields containing "start" → fields containing "end" → any date field.
    """
    if not date_fields:
        return "startTime"
    for f in date_fields:
        if "start" in f.lower():
            return f
    for f in date_fields:
        if "end" in f.lower():
            return f
    return date_fields[0]


def _resolve_sort_field(es, index: str, hint: str) -> tuple[str, str]:
    """
    Re-fetch /{index}/_mapping and return the best **sortable** date field.

    Unlike _pick_date_field_verbose (which operates on a pre-built name list that
    was collected without checking doc_values), this function reads the raw mapping
    and skips any date field with doc_values explicitly set to False — those fields
    cannot be used in an ES sort clause.

    Concrete example — dp-attack-extra-*:
      Mapping has: startTime (doc_values:false), endTime (doc_values:false), timeStamp
      _get_date_fields would return ["startTime", "endTime", "timeStamp"].
      _pick_date_field_verbose with hint="end" would stop at step-1 ("end" in "endTime")
      and return "endTime" — which is unsortable.
      _resolve_sort_field filters out both disabled fields first, leaving ["timeStamp"],
      and then _pick_date_field_verbose correctly picks "timeStamp" via the non-start fallback.

    Returns (field_name, reason_string).
    """
    fallback = "endTime" if hint == "end" else "startTime"
    fields = _collect_date_fields(es, index)

    if not fields:
        return fallback, f"no date fields in mapping — hard-coded fallback '{fallback}'"

    # Prefer fields that are sortable (doc_values not explicitly disabled).
    sortable = [name for name, ok in fields if ok]
    if sortable:
        return _pick_date_field_verbose(sortable, hint)

    # No doc_values-backed fields, but date fields DO exist — pick one of those
    # rather than a hard-coded name that may not exist in this index.
    all_names = [name for name, _ in fields]
    field, reason = _pick_date_field_verbose(all_names, hint)
    return field, reason + " (no doc_values date fields — sorting via field data)"


def _discover_index_groups(es, pattern: str) -> list[str]:
    """
    Expand an index pattern into a list of distinct "group wildcards".

    Examples:
      "dp-attack-*"              → ["dp-attack-extra-*", "dp-attack-raw-*"]
      "dp-attack-raw-*"          → ["dp-attack-raw-*"]
      "adc-contained-hourly-*"   → ["adc-contained-hourly-*"]
      "appconfig2"               → ["appconfig2"]   (no wildcard)

    Grouping rule: indices sharing the same prefix before the first "-ty-"
    segment belong to the same group.  Indices without a "-ty-" segment are
    treated as their own single-member group.
    """
    if "*" not in pattern:
        return [pattern]   # exact index — no expansion needed

    try:
        raw = es.get(f"/_cat/indices/{pattern}", params={"format": "json", "h": "index"})
        names = [r.get("index", "") for r in (raw if isinstance(raw, list) else []) if r.get("index")]
    except Exception as exc:
        logger.warning("[_discover_index_groups] pattern=%r  error=%s", pattern, exc)
        return [pattern]

    if not names:
        return [pattern]

    groups: dict[str, bool] = {}
    for name in names:
        if "-ty-" in name:
            prefix = name.split("-ty-")[0]
            groups[f"{prefix}-*"] = True
        else:
            groups[name] = True          # no -ty- convention → use as-is

    result = sorted(groups.keys())
    logger.debug("[_discover_index_groups] pattern=%r → %s", pattern, result)
    return result


def _parse_datetime_to_ms(dt_str: str) -> int | None:
    """
    Convert a datetime-local ISO string (e.g. "2026-06-28T10:46") to epoch ms.
    Returns None when the string is empty or cannot be parsed.
    """
    if not dt_str or not dt_str.strip():
        return None
    try:
        dt = datetime.fromisoformat(dt_str.strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


def _parse_nl_time(text: str) -> dict | None:
    """
    Detect a relative-time phrase in free text and return epoch ms + label,
    or None if no known phrase is found.
    """
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    t   = text.lower()
    if   "last hour"  in t or "past hour"  in t:
        return {"value": int((now - timedelta(hours=1)).timestamp()  * 1000), "label": "last 1 hour"}
    elif "last 24"    in t or "past 24"    in t:
        return {"value": int((now - timedelta(hours=24)).timestamp() * 1000), "label": "last 24 hours"}
    elif "today"      in t:
        sod = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return {"value": int(sod.timestamp() * 1000), "label": "today"}
    elif "last week"  in t or "past week"  in t:
        return {"value": int((now - timedelta(weeks=1)).timestamp()  * 1000), "label": "last 7 days"}
    elif "last month" in t or "past month" in t:
        return {"value": int((now - timedelta(days=30)).timestamp()  * 1000), "label": "last 30 days"}
    return None




