#!/usr/bin/env python3
"""
Attack-ID report: aggregate dp-sampled-data-* by attackIpsId, then enrich each
attack ID from dp-attack-raw-* with its category + start/end time.

Step 1 — size-0 terms aggregation on the sampled-data indices (same as the
         app's "Aggregate results" dialog): attackIpsId -> doc count.
Step 2 — one terms query against the attack-raw index to fetch each ID's
         category, startTime and endTime (both dash/underscore ID spellings
         are matched, since CC stores '3-178…' in some indices and '3_178…'
         in others).

Usage:
  python scripts/attack_id_report.py --host 172.17.154.235
  python scripts/attack_id_report.py --host <es-host> --top 200 --csv report.csv
  python scripts/attack_id_report.py --host <es-host> \
      --sampled-index "dp-sampled-data-*" --raw-index dp-attack-raw-8

  # Time window (relative): last day / week / month (h=hours, d=days,
  # w=weeks, m=months — a month is a fixed 30 days):
  python scripts/attack_id_report.py --host <es-host> --since 1d
  python scripts/attack_id_report.py --host <es-host> --since 2w

  # Or an explicit window:
  python scripts/attack_id_report.py --host <es-host> \
      --from 2026-07-01 --to 2026-07-14T18:00

Only needs: pip install requests
"""
import argparse
import csv
import json
import re
import sys
from datetime import datetime, timedelta, timezone

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def es_search(args, index: str, body: dict) -> dict:
    url = f"{args.scheme}://{args.host}:{args.port}/{index}/_search"
    auth = (args.user, args.password) if args.user else None
    r = requests.post(url, json=body, auth=auth, verify=False, timeout=60)
    r.raise_for_status()
    return r.json()


def fmt_time(ms):
    """Epoch-ms (or ISO passthrough) -> readable UTC string."""
    if ms is None:
        return ""
    try:
        return datetime.fromtimestamp(float(ms) / 1000, tz=timezone.utc) \
                       .strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return str(ms)


_SINCE_UNIT_MS = {"h": 3_600_000, "d": 86_400_000,
                  "w": 7 * 86_400_000, "m": 30 * 86_400_000}


def parse_since(spec: str) -> int:
    """'1d' / '2w' / '1m' / '12h' -> epoch-ms lower bound (now - span)."""
    m = re.fullmatch(r"(\d+)\s*([hdwm])", spec.strip().lower())
    if not m:
        raise ValueError(f"bad --since value {spec!r} — use e.g. 12h, 1d, 2w, 1m")
    span_ms = int(m.group(1)) * _SINCE_UNIT_MS[m.group(2)]
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return now_ms - span_ms


def parse_dt(spec: str) -> int:
    """'2026-07-01' or '2026-07-01T18:00' -> epoch ms (UTC)."""
    dt = datetime.fromisoformat(spec.strip())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def time_range_clause(args) -> dict | None:
    """Build the range filter from --since / --from / --to (or None)."""
    bounds: dict = {}
    if args.since:
        bounds["gte"] = parse_since(args.since)
    if getattr(args, "from"):
        bounds["gte"] = parse_dt(getattr(args, "from"))   # explicit beats --since
    if args.to:
        bounds["lte"] = parse_dt(args.to)
    if not bounds:
        return None
    if "gte" in bounds and "lte" in bounds and bounds["gte"] > bounds["lte"]:
        raise ValueError("--from is later than --to")
    return {"range": {args.time_field: bounds}}


def id_variants(attack_id: str) -> tuple:
    """Both CC spellings of an attack ID: '3-178…' and '3_178…'."""
    return (attack_id,
            attack_id.replace("-", "_", 1),
            attack_id.replace("_", "-", 1))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1],
                                formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--host", required=True, help="Elasticsearch host/IP")
    p.add_argument("--port", type=int, default=9200)
    p.add_argument("--scheme", default="http", choices=["http", "https"])
    p.add_argument("--user", default="", help="basic-auth user (optional)")
    p.add_argument("--password", default="")
    p.add_argument("--sampled-index", default="dp-sampled-data-*",
                   help="indices to aggregate attack IDs from")
    p.add_argument("--raw-index", default="dp-attack-raw-*",
                   help="index holding category/startTime/endTime per attack "
                        "(e.g. dp-attack-raw-8)")
    p.add_argument("--top", type=int, default=100,
                   help="max attack IDs to aggregate (terms agg size)")
    p.add_argument("--since", default="", metavar="SPAN",
                   help="relative time window, e.g. 12h / 1d / 2w / 1m "
                        "(h=hours, d=days, w=weeks, m=months of fixed 30 days)")
    p.add_argument("--from", default="", metavar="DATETIME", dest="from",
                   help="window start, e.g. 2026-07-01 or 2026-07-01T18:00 "
                        "(UTC; overrides --since)")
    p.add_argument("--to", default="", metavar="DATETIME",
                   help="window end (same format as --from; default: now)")
    p.add_argument("--time-field", default="startTime",
                   help="date field the time window filters on")
    p.add_argument("--csv", default="", metavar="FILE",
                   help="also write the report as CSV to this file")
    args = p.parse_args()

    try:
        time_clause = time_range_clause(args)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if time_clause:
        b = time_clause["range"][args.time_field]
        print(f"Time window on {args.time_field!r}: "
              f"{fmt_time(b.get('gte')) or '…'} → {fmt_time(b.get('lte')) or 'now'} (UTC)",
              file=sys.stderr)

    # ── Step 1: aggregate attackIpsId over the sampled-data indices ──────────
    agg_body = {
        "size": 0,
        "query": time_clause or {"match_all": {}},
        "aggs": {"by_attack": {"terms": {"field": "attackIpsId",
                                         "size": args.top}}},
    }
    try:
        resp = es_search(args, args.sampled_index, agg_body)
    except Exception as exc:
        print(f"ERROR aggregating {args.sampled_index!r}: {exc}", file=sys.stderr)
        return 1
    buckets = resp.get("aggregations", {}).get("by_attack", {}).get("buckets", [])
    if not buckets:
        print(f"No attackIpsId buckets found in {args.sampled_index!r}.")
        return 0
    counts = {str(b["key"]): b.get("doc_count", 0) for b in buckets}
    print(f"[1/2] {args.sampled_index}: {len(counts)} attack ID(s) aggregated "
          f"(top {args.top}).", file=sys.stderr)

    # ── Step 2: fetch category + start/end per attack ID from attack-raw ─────
    # Query with BOTH dash/underscore spellings so either storage format hits.
    all_ids = sorted({v for aid in counts for v in id_variants(aid)})
    enrich: dict = {}
    CHUNK = 500
    for i in range(0, len(all_ids), CHUNK):
        body = {
            "size": min(len(all_ids), 10_000),
            "query": {"terms": {"attackIpsId": all_ids[i:i + CHUNK]}},
            "_source": ["attackIpsId", "category", "startTime", "endTime"],
        }
        try:
            rr = es_search(args, args.raw_index, body)
        except Exception as exc:
            print(f"ERROR querying {args.raw_index!r}: {exc}", file=sys.stderr)
            return 1
        for h in rr.get("hits", {}).get("hits", []):
            src = h.get("_source", {})
            rid = str(src.get("attackIpsId") or h.get("_id", ""))
            for v in id_variants(rid):
                enrich.setdefault(v, src)
    print(f"[2/2] {args.raw_index}: matched {len(set(map(id, enrich.values())))} "
          f"raw attack record(s).", file=sys.stderr)

    # ── Assemble + print ──────────────────────────────────────────────────────
    rows = []
    for aid, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        src = next((enrich[v] for v in id_variants(aid) if v in enrich), {})
        rows.append({
            "attackIpsId": aid,
            "sampled_docs": n,
            "category":  src.get("category", ""),
            "startTime": fmt_time(src.get("startTime")),
            "endTime":   fmt_time(src.get("endTime")),
        })

    cols = ["attackIpsId", "sampled_docs", "category", "startTime", "endTime"]
    widths = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in cols}
    line = "  ".join(c.ljust(widths[c]) for c in cols)
    print(line)
    print("-" * len(line))
    missing = 0
    for r in rows:
        if not r["category"] and not r["startTime"]:
            missing += 1
        print("  ".join(str(r[c]).ljust(widths[c]) for c in cols))
    print(f"\n{len(rows)} attack ID(s); {missing} had no match in {args.raw_index!r}.")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
        print(f"CSV written to {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
