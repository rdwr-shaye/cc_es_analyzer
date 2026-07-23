"""
Artificial-data generator for CC time-sliced indices.

CC index names end in "-sl-<N>" where N = epoch_seconds // slice_seconds
(same convention as scripts/time_slice.sh): e.g.
"...-hourly-...-sid-0-sl-2950" with a weekly slice covers
16/7/2026 00:00 → 23/7/2026 00:00 UTC (2950 * 604800 s).  The slice length
is guessed the way time_slice.sh does it — pick the portion whose CURRENT
slice number is relatively closest to N.  Data granularity defaults from
name tokens ("hourly" → 1 h, "daily" → 1 d, "five-min" → 5 min, …).

Generation is a background job (shares the exports job engine, so the
Archives panel and the cancel/acknowledge endpoints work on it too):
  * one document per time step × per combination of the configured field
    values (3 protocols × 2 severities = 6 docs per step);
  * steps whose timestamp falls OUTSIDE the target index's slice window
    go to the neighbouring "-sl-<N±k>" index — only after the user
    confirmed the spill.  A missing neighbour is created with the source
    index's mappings and filled WITHOUT existence-checking;
  * for existing indices the job first scans the affected time range and
    skips any step+combination that is already present.
"""
import itertools
import json
import logging
import math
import re
import threading
import time
from datetime import datetime, timezone

from fastapi import APIRouter
from pydantic import BaseModel

from services.es_client import get_client
from routers.exports import _new_job, _finish_job, _JobCancelled, _err_text

router = APIRouter(prefix="/api/artificial", tags=["artificial"])
logger = logging.getLogger(__name__)

_SL_RE = re.compile(r"^(?P<prefix>.+-sl-)(?P<num>\d+)$")

# Slice portions (label, seconds) — mirrors scripts/time_slice.sh.
_PORTIONS = [
    ("20 minutes", 1200),
    ("hour", 3600),
    ("day", 86_400),
    ("week", 604_800),
    ("14 days", 1_209_600),
    ("30 days", 2_592_000),
    ("180 days", 15_552_000),
]

# Granularity defaults from index-name tokens (first match wins).
_GRAN_TOKENS = [
    ("five-min", 300), ("five_min", 300),
    ("hourly", 3600),
    ("daily", 86_400),
    ("weekly", 604_800),
    ("monthly", 2_592_000),
]

_MAX_PLANNED_DOCS = 200_000
_BULK_LINES = 2000                     # 1000 docs per bulk request


# ── Slice / name parsing ─────────────────────────────────────────────────────

def _guess_portion(slice_no: int, now_s: float) -> tuple[str, int]:
    """Pick the portion whose current slice number is relatively closest to
    *slice_no* (the time_slice.sh heuristic). Returns (label, seconds)."""
    best = None
    best_score = None
    for label, secs in _PORTIONS:
        cur = int(now_s) // secs
        if cur <= 0:
            continue
        score = abs(slice_no - cur) / cur
        if best_score is None or score < best_score:
            best, best_score = (label, secs), score
    return best or ("week", 604_800)


def _parse_slice(index_name: str, now_s: float | None = None) -> dict | None:
    """Return slice info for a "-sl-<N>" index name, or None."""
    m = _SL_RE.match(index_name)
    if not m:
        return None
    now_s = now_s if now_s is not None else time.time()
    num = int(m.group("num"))
    label, secs = _guess_portion(num, now_s)
    start = num * secs
    return {
        "number": num,
        "prefix": m.group("prefix"),          # includes the trailing "-sl-"
        "portion_label": label,
        "portion_seconds": secs,
        "start": start,
        "end": start + secs,
        "start_iso": datetime.fromtimestamp(start, tz=timezone.utc).isoformat(),
        "end_iso": datetime.fromtimestamp(start + secs, tz=timezone.utc).isoformat(),
    }


def _guess_granularity(index_name: str) -> int:
    """Default step size (seconds) from tokens in the index name."""
    low = index_name.lower()
    for token, secs in _GRAN_TOKENS:
        if token in low:
            return secs
    return 3600                                # sensible default: hourly


# ── Mapping helpers ──────────────────────────────────────────────────────────

def _field_types(es, index: str) -> list[dict]:
    """All leaf fields as [{name (dotted), type}] — handles both the ES 1.x
    (mappings→type→properties) and ES 5+/OpenSearch (mappings→properties)
    nesting shapes."""
    try:
        resp = es.get(f"/{index}/_mapping", params={})
    except Exception as exc:
        logger.warning("[artificial] mapping fetch %r failed: %s", index, exc)
        return []
    out: list[dict] = []
    seen: set[str] = set()

    def _from_props(props: dict, prefix: str) -> None:
        for fname, fmeta in props.items():
            if not isinstance(fmeta, dict):
                continue
            dotted = prefix + fname
            sub = fmeta.get("properties")
            if isinstance(sub, dict):
                _from_props(sub, dotted + ".")
            elif dotted not in seen:
                seen.add(dotted)
                out.append({"name": dotted, "type": fmeta.get("type", "")})

    def _find_roots(node) -> None:
        if not isinstance(node, dict):
            return
        props = node.get("properties")
        if isinstance(props, dict):
            _from_props(props, "")
            return
        for v in node.values():
            _find_roots(v)

    _find_roots(resp)
    return out


def _source_mappings(es, index: str) -> dict:
    """Raw mappings body of *index* (verbatim — reused when creating a
    neighbouring slice index), or {} when unavailable."""
    try:
        resp = es.get(f"/{index}/_mapping", params={})
        entry = resp.get(index) or next(iter(resp.values()), {})
        return entry.get("mappings", {}) if isinstance(entry, dict) else {}
    except Exception:
        return {}


def _index_exists(es, index: str) -> bool:
    try:
        es.get(f"/{index}/_mapping", params={})
        return True
    except Exception:
        return False


def _coerce(value, es_type: str):
    """Coerce a user-typed value to the mapping type (best effort)."""
    if not isinstance(value, str):
        return value
    v = value.strip()
    try:
        if es_type in ("long", "integer", "short", "byte"):
            return int(float(v))
        if es_type in ("double", "float", "half_float", "scaled_float"):
            return float(v)
        if es_type == "boolean":
            return v.lower() in ("true", "1", "yes")
    except (TypeError, ValueError):
        pass
    return v


# ── Existence keys (dedup) ───────────────────────────────────────────────────

def _dget(obj, dotted: str):
    cur = obj
    for p in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def _dset(doc: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    cur = doc
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _to_ms(v) -> int | None:
    """Normalize a stored date value (epoch ms number / numeric string /
    ISO-8601 string) to epoch milliseconds for key comparison."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


def _key(ts_ms, values) -> tuple:
    return (ts_ms, tuple(str(v) for v in values))


# ── Info endpoint (drives the dialog) ────────────────────────────────────────

@router.get("/info/{index_name}")
def artificial_info(index_name: str):
    """Everything the "create artificial data" dialog needs: slice window,
    default granularity, date fields, and the value-fields list with types."""
    try:
        es = get_client()
        from routers.query import _collect_date_fields, _pick_date_field
        if not _index_exists(es, index_name):
            return {"error": f"index {index_name!r} not found"}
        # A fresh index can have an EMPTY mapping (dynamic) — still usable:
        # the dialog then offers free-text field names.
        all_fields = _field_types(es, index_name)
        date_fields = [n for n, _s in _collect_date_fields(es, index_name)]
        main_guess = _pick_date_field(date_fields, "start") if date_fields else None
        gran = _guess_granularity(index_name)
        docs = None
        try:
            r = es.search(index_name, {"size": 0, "query": {"match_all": {}}})
            t = r.get("hits", {}).get("total")
            docs = t.get("value") if isinstance(t, dict) else t
        except Exception:
            pass
        return {
            "index": index_name,
            "slice": _parse_slice(index_name),
            "granularity_seconds": gran,
            "date_fields": date_fields,
            "main_field_guess": main_guess,
            "fields": [f for f in all_fields if f["name"] not in date_fields],
            "docs_count": docs,
        }
    except Exception as e:
        return {"error": str(e)}


# ── Generation ───────────────────────────────────────────────────────────────

class DateGap(BaseModel):
    field: str
    gap_seconds: float = 0.0          # offset from the main field (may be negative)


class FieldValues(BaseModel):
    field: str
    values: list                       # 1+ values → cartesian product across fields


class ArtificialRequest(BaseModel):
    index: str
    main_field: str
    granularity_seconds: float
    round_time: bool = True
    other_dates: list[DateGap] = []
    span_mode: str = "slice"           # slice | relative | absolute
    span_seconds: float = 86_400.0     # for relative mode ("now → N ago")
    span_from: str = ""                # for absolute mode (ISO, UTC)
    span_to: str = ""
    fields: list[FieldValues] = []
    confirm_spill: bool = False        # user approved writing beyond the slice


def _parse_abs(s: str) -> float | None:
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, AttributeError):
        return None


@router.post("")
def start_artificial(req: ArtificialRequest):
    """Plan the insertion; ask for confirmation when steps spill into other
    slice indices; otherwise start the background job."""
    index = (req.index or "").strip()
    main = (req.main_field or "").strip()
    if not index or not main:
        return {"error": "index and main date field are required"}
    g = max(1.0, float(req.granularity_seconds or 0))

    try:
        es = get_client()
    except Exception as exc:
        return {"error": str(exc)}

    now = time.time()
    sl = _parse_slice(index, now)

    # ── Time span ────────────────────────────────────────────────────────────
    if req.span_mode == "slice":
        if not sl:
            return {"error": "index name has no '-sl-<N>' suffix — "
                             "use a relative or absolute time span"}
        start, end = float(sl["start"]), min(now, float(sl["end"]))
    elif req.span_mode == "relative":
        span = max(1.0, float(req.span_seconds or 0))
        start, end = now - span, now
    elif req.span_mode == "absolute":
        start, end = _parse_abs(req.span_from), _parse_abs(req.span_to)
        if start is None or end is None:
            return {"error": "invalid absolute from/to datetime"}
    else:
        return {"error": f"unknown span_mode {req.span_mode!r}"}
    if start >= end:
        return {"error": "time span is empty — 'from' must be before 'to'"}

    # ── Time steps ───────────────────────────────────────────────────────────
    t = math.ceil(start / g) * g if req.round_time else start
    steps: list[int] = []
    while t < end:
        steps.append(int(t * 1000))
        t += g
        if len(steps) > _MAX_PLANNED_DOCS:
            return {"error": f"more than {_MAX_PLANNED_DOCS:,} time steps — "
                             f"use a coarser granularity or a shorter span"}
    if not steps:
        return {"error": "the span produces no time steps "
                         "(shorter than one granularity unit?)"}

    # ── Value combinations (cartesian product) ───────────────────────────────
    types = {f["name"]: f["type"] for f in _field_types(es, index)}
    value_fields = [(fv.field.strip(), [
        _coerce(v, types.get(fv.field.strip(), "")) for v in fv.values
    ]) for fv in req.fields if fv.field.strip() and fv.values]
    field_names = [f for f, _ in value_fields]
    combos = list(itertools.product(*[vals for _, vals in value_fields])) \
        if value_fields else [()]
    planned = len(steps) * len(combos)
    if planned > _MAX_PLANNED_DOCS:
        return {"error": f"{planned:,} documents planned (steps × value "
                         f"combinations) — above the {_MAX_PLANNED_DOCS:,} cap"}

    # ── Route each step to its slice index ───────────────────────────────────
    per_index: dict[str, list[int]] = {}
    if sl:
        psec = sl["portion_seconds"]
        for ts in steps:
            n = (ts // 1000) // psec
            tgt = index if n == sl["number"] else f"{sl['prefix']}{n}"
            per_index.setdefault(tgt, []).append(ts)
    else:
        per_index[index] = list(steps)
    # Chosen index first, then neighbours chronologically.
    targets = sorted(per_index.items(), key=lambda kv: (kv[0] != index, kv[0]))

    extra = [idx for idx, _ in targets if idx != index]
    if extra and not req.confirm_spill:
        return {
            "needs_confirm": True,
            "targets": [{"index": idx, "steps": len(ts_list),
                         "docs": len(ts_list) * len(combos),
                         "exists": _index_exists(es, idx)}
                        for idx, ts_list in targets],
            "message": "the time span extends beyond this index's slice "
                       "window — data would also be written to the indices "
                       "listed",
        }

    plan = {
        "main_field": main,
        "field_names": field_names,
        "combos": combos,
        "other_dates": [(d.field.strip(), float(d.gap_seconds))
                        for d in req.other_dates
                        if d.field.strip() and d.field.strip() != main],
        "targets": targets,
        "mappings": _source_mappings(es, index),
    }
    items = [{"index": idx, "total": len(ts_list) * len(combos), "done": 0,
              "inserted": 0, "skipped": 0, "failed": 0,
              "phase": "queued", "unit": "docs"}
             for idx, ts_list in targets]
    job = _new_job("artificial", items)
    threading.Thread(target=_run_artificial_job, args=(job, es, plan),
                     daemon=True, name=f"artificial-{job['id']}").start()
    logger.info("[artificial] job %s: %s docs planned (%s steps × %s combos) "
                "into %s — main=%r gaps=%s fields=%s",
                job["id"], planned, len(steps), len(combos),
                [idx for idx, _ in targets], main,
                plan["other_dates"], field_names)
    return {"job_id": job["id"], "planned": planned,
            "targets": [{"index": idx, "docs": len(ts) * len(combos)}
                        for idx, ts in targets]}


def _run_artificial_job(job: dict, es, plan: dict) -> None:
    from routers.query import _scroll_hits
    main = plan["main_field"]
    fields = plan["field_names"]
    try:
        for item, (idx, ts_list) in zip(job["items"], plan["targets"]):
            if job["cancelled"]:
                raise _JobCancelled()

            exists = _index_exists(es, idx)
            existing: set = set()
            if not exists:
                # Missing neighbour slice → create it with the source
                # mappings and insert WITHOUT existence checks.
                item["phase"] = "create index"
                body: dict = {"settings": {"index": {
                    "number_of_shards": 1, "number_of_replicas": 0}}}
                if plan["mappings"]:
                    body["mappings"] = plan["mappings"]
                logger.info("[artificial %s] PUT /%s (create, %s mapping keys)",
                            job["id"], idx, len(plan["mappings"] or {}))
                es.put(f"/{idx}", body)
            else:
                item["phase"] = "check existing"
                lo, hi = min(ts_list), max(ts_list)
                query = {"range": {main: {"gte": lo, "lte": hi}}}
                logger.info("[artificial %s] scanning %s for existing docs: %s",
                            job["id"], idx, json.dumps(query))
                for h in _scroll_hits(es, idx, query, page=1000):
                    src = h.get("_source", {})
                    ts = _to_ms(_dget(src, main))
                    if ts is not None:
                        existing.add(_key(ts, [_dget(src, f) for f in fields]))
                logger.info("[artificial %s] %s existing doc keys in range on %s",
                            job["id"], len(existing), idx)

            item["phase"] = "insert"
            buf: list[str] = []

            def _flush() -> None:
                if not buf:
                    return
                resp = es.bulk("\n".join(buf) + "\n")
                n = len(buf) // 2
                if resp.get("errors"):
                    ok = 0
                    for it in resp.get("items", []):
                        st = (it.get("index") or it.get("create") or {}).get("status", 200)
                        if st and st >= 300:
                            item["failed"] += 1
                        else:
                            ok += 1
                    item["inserted"] += ok
                    logger.warning("[artificial %s] bulk to %s: %s ok, %s failed",
                                   job["id"], idx, ok, item["failed"])
                else:
                    item["inserted"] += n
                buf.clear()

            for ts in ts_list:
                if job["cancelled"]:
                    raise _JobCancelled()
                for combo in plan["combos"]:
                    item["done"] += 1
                    if exists and _key(ts, list(combo)) in existing:
                        item["skipped"] += 1
                        continue
                    doc: dict = {}
                    _dset(doc, main, ts)
                    for f, gap in plan["other_dates"]:
                        _dset(doc, f, ts + int(gap * 1000))
                    for f, v in zip(fields, combo):
                        _dset(doc, f, v)
                    buf.append(json.dumps({"index": {"_index": idx}}))
                    buf.append(json.dumps(doc, default=str))
                    if len(buf) >= _BULK_LINES:
                        _flush()
            _flush()
            try:
                es.post(f"/{idx}/_refresh")
            except Exception:
                pass
            item["phase"] = "done"
            logger.info("[artificial %s] %s: %s inserted, %s skipped, %s failed",
                        job["id"], idx, item["inserted"], item["skipped"],
                        item["failed"])
        _finish_job(job)
    except _JobCancelled:
        logger.info("[artificial %s] cancelled — inserted docs are kept", job["id"])
        _finish_job(job, cancelled=True)
    except Exception as exc:
        logger.error("[artificial %s] failed: %s", job["id"], _err_text(exc))
        _finish_job(job, error=_err_text(exc))
