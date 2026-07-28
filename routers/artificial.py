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
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter
from pydantic import BaseModel

from services.es_client import get_client
from routers.exports import _new_job, _finish_job, _JobCancelled, _err_text

router = APIRouter(prefix="/api/artificial", tags=["artificial"])
logger = logging.getLogger(__name__)

# "-pt-<n>" partition suffixes (index hit its max size) are tolerated and
# stripped: neighbour slices are always written without a partition.
_SL_RE = re.compile(r"^(?P<prefix>.+-sl-)(?P<num>\d+)(?:-pt-\d+)?$")

# Slice portions (label, seconds) — mirrors scripts/time_slice.sh. Only used
# as the LAST-RESORT fallback when the live catalog doesn't know the family.
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
    *slice_no* (the time_slice.sh heuristic). Returns (label, seconds).
    FALLBACK ONLY — the live catalog (services/index_discovery.py) is the
    authoritative slice-size source; this runs when it doesn't know the family."""
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


def _portion_label(minutes: int) -> str:
    """Human label for a slice length in minutes ("5 minutes", "hour", "14 days")."""
    if minutes % 1440 == 0:
        d = minutes // 1440
        return "day" if d == 1 else f"{d} days"
    if minutes % 60 == 0:
        h = minutes // 60
        return "hour" if h == 1 else f"{h} hours"
    return f"{minutes} minutes"


def _parse_slice(index_name: str, now_s: float | None = None, es=None) -> dict | None:
    """Return slice info for a "-sl-<N>" index name, or None.

    The slice LENGTH comes from the live possible-indices catalog (index
    templates + appconfig + live indices) when *es* is given and the family is
    known; only unknown families fall back to the old relative-closeness guess,
    and the result says so (guessed=True + source) instead of hiding it.
    """
    m = _SL_RE.match(index_name)
    if not m:
        return None
    now_s = now_s if now_s is not None else time.time()
    num = int(m.group("num"))

    secs, source, guessed = None, None, False
    if es is not None:
        from services.index_discovery import slice_for_index
        minutes, src = slice_for_index(es, index_name)
        if minutes:
            secs, source = minutes * 60, src
    if secs is None:
        label, secs = _guess_portion(num, now_s)
        source = "guessed from the slice number — family not in the live catalog"
        guessed = True
    else:
        label = _portion_label(secs // 60)

    start = num * secs
    return {
        "number": num,
        "prefix": m.group("prefix"),          # includes the trailing "-sl-"
        "portion_label": label,
        "portion_seconds": secs,
        "source": source,
        "guessed": guessed,
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


# ── Derived / dependent fields ───────────────────────────────────────────────
# Fields whose value is COMPUTED from a document's timestamp (a "dependency
# rule"), e.g. day-of-week and hour-of-day. Rule ids are stable API values;
# labels drive the dialog. All parts are read in UTC, optionally shifted by a
# whole-timezone offset (minutes) so a customer whose day/hour are local can
# match them.
_DERIVE_RULES = [
    {"id": "weekday_iso",   "label": "Day of week (Mon=1 … Sun=7)"},
    {"id": "weekday_sun0",  "label": "Day of week (Sun=0 … Sat=6)"},
    {"id": "weekday_sun1",  "label": "Day of week (Sun=1 … Sat=7)"},
    {"id": "weekday_mon0",  "label": "Day of week (Mon=0 … Sun=6)"},
    {"id": "hour",          "label": "Hour of day (0–23)"},
    {"id": "minute",        "label": "Minute of hour (0–59)"},
    {"id": "second",        "label": "Second (0–59)"},
    {"id": "minute_of_day", "label": "Minute of day (0–1439)"},
    {"id": "day_of_month",  "label": "Day of month (1–31)"},
    {"id": "month",         "label": "Month (1–12)"},
    {"id": "month0",        "label": "Month (0–11)"},
    {"id": "quarter",       "label": "Quarter (1–4)"},
    {"id": "year",          "label": "Year"},
    {"id": "day_of_year",   "label": "Day of year (1–366)"},
    {"id": "week_of_year",  "label": "ISO week of year (1–53)"},
    {"id": "epoch_seconds", "label": "Epoch seconds"},
    {"id": "epoch_millis",  "label": "Epoch milliseconds"},
]
_DERIVE_IDS = {r["id"] for r in _DERIVE_RULES}


def _derive(rule: str, ts_ms: int, tz_offset_min: int = 0):
    """Compute a derived value from an epoch-millis timestamp. Date parts are
    read at UTC + tz_offset_min (so day/hour reflect the chosen timezone)."""
    if rule == "epoch_millis":
        return int(ts_ms)
    if rule == "epoch_seconds":
        return int(ts_ms) // 1000
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    if tz_offset_min:
        dt = dt + timedelta(minutes=tz_offset_min)   # tzinfo stays UTC → parts shift
    if rule == "weekday_iso":   return dt.isoweekday()          # Mon=1 … Sun=7
    if rule == "weekday_mon0":  return dt.weekday()             # Mon=0 … Sun=6
    if rule == "weekday_sun0":  return dt.isoweekday() % 7      # Sun=0 … Sat=6
    if rule == "weekday_sun1":  return dt.isoweekday() % 7 + 1  # Sun=1 … Sat=7
    if rule == "hour":          return dt.hour
    if rule == "minute":        return dt.minute
    if rule == "second":        return dt.second
    if rule == "minute_of_day": return dt.hour * 60 + dt.minute
    if rule == "day_of_month":  return dt.day
    if rule == "month":         return dt.month
    if rule == "month0":        return dt.month - 1
    if rule == "quarter":       return (dt.month - 1) // 3 + 1
    if rule == "year":          return dt.year
    if rule == "day_of_year":   return dt.timetuple().tm_yday
    if rule == "week_of_year":  return dt.isocalendar()[1]
    return None


# ── Document _id ─────────────────────────────────────────────────────────────
# By default ES generates an id per document. Several CC families keep the id
# in a field too — on dp-attack-raw* the _id IS the attackIpsId — so the id can
# instead be copied from a generated field or built from a template of them.
_ID_MODES = [
    {"id": "auto",     "label": "Auto — Elasticsearch generates the id"},
    {"id": "field",    "label": "Copy a generated field"},
    {"id": "template", "label": "Template of generated fields"},
]
_ID_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")
# Template placeholders that aren't document fields.
_ID_SPECIALS = {"n": "document number (0, 1, 2 …)",
                "ts": "main timestamp in epoch millis",
                "index": "target index name"}


def _build_id_rule(rule, written: set) -> tuple[dict | None, str | None]:
    """Validate an _id rule against the fields this job will actually write.
    Returns (rule-dict-or-None, error-or-None); None/None = ES generates ids."""
    if rule is None:
        return None, None
    mode = (rule.mode or "auto").strip()
    if mode in ("", "auto"):
        return None, None
    avail = ", ".join(sorted(written)) or "none"
    if mode == "field":
        f = (rule.field or "").strip()
        if not f:
            return None, "_id: choose the field to copy the id from"
        if f not in written:
            return None, (f"_id: field {f!r} is not generated by this job "
                          f"(available: {avail})")
        return {"mode": "field", "field": f}, None
    if mode == "template":
        tpl = (rule.template or "").strip()
        keys = [k.strip() for k in _ID_PLACEHOLDER.findall(tpl)]
        if not keys:
            return None, ("_id template needs at least one {field} placeholder "
                          "— otherwise every document would get the same id "
                          "and only one would survive")
        unknown = [k for k in keys if k not in written and k not in _ID_SPECIALS]
        if unknown:
            return None, (f"_id template: unknown placeholder(s) "
                          f"{', '.join(repr(k) for k in unknown)} — "
                          f"available fields: {avail}; specials: "
                          f"{', '.join(sorted(_ID_SPECIALS))}")
        return {"mode": "template", "template": tpl}, None
    return None, f"unknown _id mode {mode!r}"


def _doc_id(rule: dict | None, doc: dict, n: int, ts_ms: int, index: str):
    """The _id for one generated document; None → let ES generate one."""
    if not rule:
        return None
    if rule["mode"] == "field":
        v = _dget(doc, rule["field"])
        return None if v is None else str(v)

    def _sub(m):
        key = m.group(1).strip()
        if key == "n":
            return str(n)
        if key == "ts":
            return str(ts_ms)
        if key == "index":
            return index
        v = _dget(doc, key)
        return "" if v is None else str(v)

    return _ID_PLACEHOLDER.sub(_sub, rule["template"]) or None


# ── Info endpoint (drives the dialog) ────────────────────────────────────────

@router.get("/info/{index_name}")
def artificial_info(index_name: str):
    """Everything the "create artificial data" dialog needs: slice window,
    default granularity, date fields, and the value-fields list with types.

    Works for indices that DON'T exist yet, as long as their family is in the
    live possible-indices catalog: the field list then comes from the family's
    index template (which ES applies automatically on first write)."""
    try:
        es = get_client()
        from routers.query import _collect_date_fields, _pick_date_field
        exists = _index_exists(es, index_name)
        if exists:
            # A fresh index can have an EMPTY mapping (dynamic) — still usable:
            # the dialog then offers free-text field names.
            all_fields = _field_types(es, index_name)
            date_fields = [n for n, _s in _collect_date_fields(es, index_name)]
        else:
            from services.index_discovery import catalog_entry_for_index
            entry = catalog_entry_for_index(es, index_name)
            if entry is None:
                return {"error": f"index {index_name!r} not found and no CC "
                                 f"index template matches its family"}
            all_fields = [{"name": n, "type": t}
                          for n, t in sorted(entry.get("fields", {}).items())]
            date_fields = [f["name"] for f in all_fields if f["type"] == "date"]
        main_guess = _pick_date_field(date_fields, "start") if date_fields else None
        gran = _guess_granularity(index_name)
        docs = None
        if exists:
            try:
                r = es.search(index_name, {"size": 0, "query": {"match_all": {}}})
                t = r.get("hits", {}).get("total")
                docs = t.get("value") if isinstance(t, dict) else t
            except Exception:
                pass
        return {
            "index": index_name,
            "exists": exists,
            "slice": _parse_slice(index_name, es=es),
            "granularity_seconds": gran,
            "date_fields": date_fields,
            "main_field_guess": main_guess,
            "fields": [f for f in all_fields if f["name"] not in date_fields],
            "derive_rules": _DERIVE_RULES,
            "id_modes": _ID_MODES,
            "id_specials": _ID_SPECIALS,
            "docs_count": docs,
        }
    except Exception as e:
        return {"error": str(e)}


# ── Random / incremental field values ────────────────────────────────────────

_INT_TYPES   = {"long", "integer", "short", "byte"}
_FLOAT_TYPES = {"double", "float", "half_float", "scaled_float"}


def _rand_kind(field: str, es_type: str) -> str:
    """Guess the random-value kind from the field name and mapping type.
    Mirrors frontend _adRandKind — keep the two in sync."""
    leaf = field.split(".")[-1].lower()
    if re.search(r"port$", leaf):
        return "port"
    if es_type == "boolean":
        return "bool"
    if re.search(r"ip$|address$|addr$", leaf) and es_type not in _INT_TYPES | _FLOAT_TYPES:
        return "ip"
    if es_type in _INT_TYPES:
        return "int"
    if es_type in _FLOAT_TYPES:
        return "float"
    return "token"


def _special_value(sp: dict, n: int, rng):
    """Value for a random/increment field spec on the n-th planned document.
    Values land as strings when the mapping type is string-ish (CC stores e.g.
    ports as keyword), as numbers/booleans otherwise."""
    numeric_target = sp["es_type"] in _INT_TYPES | _FLOAT_TYPES
    if sp["mode"] == "increment":
        v = sp["start"] + sp["step"] * n
        v = int(v) if float(v).is_integer() else round(v, 6)
        if sp["prefix"] or not numeric_target:
            return f"{sp['prefix']}{v}"
        return v
    k = sp["kind"]
    if k == "ip":
        return (f"{rng.randint(1, 254)}.{rng.randint(0, 254)}."
                f"{rng.randint(0, 254)}.{rng.randint(1, 254)}")
    if k == "bool":
        return rng.random() < 0.5
    if k == "token":
        leaf = sp["field"].split(".")[-1]
        return f"{leaf}{rng.randint(1, max(1, int(sp['pool'] or 10)))}"
    lo_def, hi_def = (1, 65535) if k == "port" else (0, 1000)
    lo = sp["min"] if sp["min"] is not None else lo_def
    hi = sp["max"] if sp["max"] is not None else hi_def
    if hi < lo:
        lo, hi = hi, lo
    if k == "float":
        v = round(rng.uniform(lo, hi), 2)
    else:
        v = rng.randint(int(lo), int(hi))
    return v if numeric_target else str(v)


# ── Generation ───────────────────────────────────────────────────────────────

class DateGap(BaseModel):
    field: str
    gap_seconds: float = 0.0          # offset from the main field (may be negative)


class FieldValues(BaseModel):
    field: str
    values: list = []                  # list mode: 1+ values → cartesian product
    mode: str = "list"                 # list | random | increment
    # random mode:
    kind: str = ""                     # int|float|ip|port|bool|token ("" = auto by name+type)
    min: float | None = None           # numeric range (defaults per kind)
    max: float | None = None
    pool: int = 10                     # token mode: pick from <leaf>1 … <leaf><pool>
    # increment mode:
    prefix: str = ""                   # optional, e.g. "14-" → "14-1", "14-2", …
    start: float = 1.0
    step: float = 1.0


class DerivedField(BaseModel):
    field: str                         # target field to fill
    rule: str                          # one of _DERIVE_RULES ids
    source: str = ""                   # date field to derive from (default: main)


class DocIdRule(BaseModel):
    mode: str = "auto"                 # auto | field | template
    field: str = ""                    # mode=field: field holding the id
    template: str = ""                 # mode=template: "{attackIpsId}", "11-{n}", …


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
    derived: list[DerivedField] = []   # dependency rules (day-of-week, hour, …)
    doc_id: DocIdRule | None = None    # None/auto → ES generates each _id
    tz_offset_minutes: int = 0         # timezone for derived date parts (0 = UTC)
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
    sl = _parse_slice(index, now, es=es)

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
    if not types:
        # Not-yet-existing index: types come from the family's template so
        # random/increment values (and coercion) match what ES will apply.
        from services.index_discovery import catalog_entry_for_index
        entry = catalog_entry_for_index(es, index)
        if entry:
            types = dict(entry.get("fields", {}))
    value_fields = [(fv.field.strip(), [
        _coerce(v, types.get(fv.field.strip(), "")) for v in fv.values
    ]) for fv in req.fields
        if fv.mode in ("", "list") and fv.field.strip() and fv.values]
    field_names = [f for f, _ in value_fields]
    combos = list(itertools.product(*[vals for _, vals in value_fields])) \
        if value_fields else [()]

    # Random / incremental fields: filled per DOCUMENT (no combo expansion,
    # not part of the dedup key).
    special = []
    for fv in req.fields:
        f = fv.field.strip()
        if not f or fv.mode in ("", "list"):
            continue
        if fv.mode not in ("random", "increment"):
            return {"error": f"unknown field mode {fv.mode!r} for {f!r}"}
        es_type = types.get(f, "")
        special.append({
            "field": f, "mode": fv.mode, "es_type": es_type,
            "kind": (fv.kind or _rand_kind(f, es_type)),
            "min": fv.min, "max": fv.max, "pool": fv.pool,
            "prefix": fv.prefix, "start": float(fv.start), "step": float(fv.step),
        })
    planned = len(steps) * len(combos)
    if planned > _MAX_PLANNED_DOCS:
        return {"error": f"{planned:,} documents planned (steps × value "
                         f"combinations) — above the {_MAX_PLANNED_DOCS:,} cap"}

    # ── Document _id rule ────────────────────────────────────────────────────
    # Validated here — BEFORE the spill confirmation — so a bad rule doesn't
    # surface only after the user approved writing into neighbouring indices.
    written = {main}
    written |= {d.field.strip() for d in req.other_dates if d.field.strip()}
    written |= {d.field.strip() for d in req.derived if d.field.strip()}
    for fv in req.fields:
        f = fv.field.strip()
        # A list-mode field with no values is omitted from the document.
        if f and not (fv.mode in ("", "list") and not fv.values):
            written.add(f)
    id_rule, id_err = _build_id_rule(req.doc_id, written)
    if id_err:
        return {"error": id_err}

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

    other_dates = [(d.field.strip(), float(d.gap_seconds))
                   for d in req.other_dates
                   if d.field.strip() and d.field.strip() != main]
    gap_by_field = {f: g for f, g in other_dates}

    # Derived (dependency) fields: value computed from a source date field's
    # timestamp. source defaults to the main field; if source is one of the
    # other date fields, apply that field's gap so the derivation matches the
    # value actually stored there.
    derived = []
    for d in req.derived:
        f, rule, src = d.field.strip(), d.rule.strip(), d.source.strip()
        if not f or rule not in _DERIVE_IDS:
            continue
        gap_ms = int(gap_by_field.get(src, 0.0) * 1000) if src and src != main else 0
        derived.append((f, rule, gap_ms))

    plan = {
        "main_field": main,
        "field_names": field_names,
        "combos": combos,
        "special": special,
        "other_dates": other_dates,
        "derived": derived,
        "doc_id": id_rule,
        "tz_offset": int(req.tz_offset_minutes or 0),
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
                "into %s — main=%r gaps=%s fields=%s special=%s derived=%s "
                "tz=%s _id=%s",
                job["id"], planned, len(steps), len(combos),
                [idx for idx, _ in targets], main,
                plan["other_dates"], field_names,
                [(s["field"], s["mode"], s["kind"]) for s in special],
                [(f, r) for f, r, _ in derived], plan["tz_offset"],
                id_rule or "auto")
    return {"job_id": job["id"], "planned": planned,
            "targets": [{"index": idx, "docs": len(ts) * len(combos)}
                        for idx, ts in targets]}


def _run_artificial_job(job: dict, es, plan: dict) -> None:
    import random
    from routers.query import _scroll_hits
    main = plan["main_field"]
    fields = plan["field_names"]
    special = plan.get("special", [])
    id_rule = plan.get("doc_id")
    rng = random.Random()
    doc_no = 0          # planned-doc counter across ALL targets — drives increments
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
                    # The counter advances for every PLANNED doc (even skipped
                    # ones) so incremental values stay aligned to time steps.
                    n = doc_no
                    doc_no += 1
                    if exists and _key(ts, list(combo)) in existing:
                        item["skipped"] += 1
                        continue
                    doc: dict = {}
                    _dset(doc, main, ts)
                    for f, gap in plan["other_dates"]:
                        _dset(doc, f, ts + int(gap * 1000))
                    for f, v in zip(fields, combo):
                        _dset(doc, f, v)
                    for sp in special:
                        _dset(doc, sp["field"], _special_value(sp, n, rng))
                    # Dependency rules — computed from the (possibly gap-shifted)
                    # source timestamp; applied last so they always win.
                    for f, rule, gap_ms in plan["derived"]:
                        _dset(doc, f, _derive(rule, ts + gap_ms, plan["tz_offset"]))
                    # _id last — it can reference any field written above.
                    action = {"index": {"_index": idx}}
                    if id_rule:
                        did = _doc_id(id_rule, doc, n, ts, idx)
                        if did:
                            action["index"]["_id"] = did
                    buf.append(json.dumps(action))
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
