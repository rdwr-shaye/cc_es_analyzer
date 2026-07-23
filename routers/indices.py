import csv
import io
import json
import re

from fastapi import APIRouter, Query, UploadFile, File
from pydantic import BaseModel
from services.es_client import get_client
from services.cc_indices import CC_INDEX_CATALOG, CATEGORIES, resolve_prefix

router = APIRouter(prefix="/api/indices", tags=["indices"])


@router.get("")
def list_indices(cc_only: bool = Query(default=False)):
    """List all indices with doc count, store size, and CC metadata."""
    try:
        es   = get_client()
        rows = es.cat_indices()
        result = []
        for row in rows:
            index_name = row.get("index", "")
            if index_name.startswith("."):
                continue
            meta = resolve_prefix(index_name)
            if cc_only and meta is None:
                continue
            result.append({
                "name":       index_name,
                "health":     row.get("health"),
                "status":     row.get("status"),
                "docs_count": _safe_int(row.get("docs.count")),
                "store_size": row.get("store.size"),
                "primaries":  _safe_int(row.get("pri")),
                "replicas":   _safe_int(row.get("rep")),
                "cc_meta":    meta,
            })
        return {"indices": result, "total": len(result)}
    except Exception as e:
        return {"error": str(e)}


@router.get("/catalog")
def get_cc_catalog():
    """Return the full CC index catalog grouped by category."""
    grouped = {cat: [] for cat in CATEGORIES}
    for prefix, meta in CC_INDEX_CATALOG.items():
        cat = meta.get("category", "Other")
        if cat not in grouped:
            grouped[cat] = []
        grouped[cat].append({"prefix": prefix, **meta})
    return {"catalog": grouped, "categories": CATEGORIES}


@router.get("/{index_name}/stats")
def index_stats(index_name: str):
    """Detailed stats for a single index."""
    try:
        es      = get_client()
        stats   = es.index_stats(index_name)
        mapping = es.index_mapping(index_name)

        idx_stats = stats.get("indices", {}).get(index_name, {})
        primaries = idx_stats.get("primaries", {})
        return {
            "name":           index_name,
            "cc_meta":        resolve_prefix(index_name),
            "docs_count":     primaries.get("docs", {}).get("count", 0),
            "docs_deleted":   primaries.get("docs", {}).get("deleted", 0),
            "store_bytes":    primaries.get("store", {}).get("size_in_bytes", 0),
            "indexing_total": primaries.get("indexing", {}).get("index_total", 0),
            "search_total":   primaries.get("search", {}).get("query_total", 0),
            "mapping_fields": _count_mapping_fields(mapping),
            "mapping_field_names": _top_level_field_names(mapping),
            "mapping_date_fields": _date_field_names(mapping),
        }
    except Exception as e:
        return {"error": str(e)}


@router.get("/{index_name}/sample")
def index_sample(index_name: str, size: int = Query(default=10, le=10000)):
    """Fetch a sample of recent documents, sorted by time field if present."""
    try:
        es      = get_client()
        mapping = es.index_mapping(index_name)

        # Find a sortable time field
        sort_candidates = ["startTime", "timestamp", "@timestamp", "createdAt"]
        sort_field      = None
        idx_mapping     = list(mapping.values())[0]
        props           = idx_mapping.get("mappings", {}).get("properties", {})
        for field in sort_candidates:
            if field in props:
                sort_field = field
                break

        body: dict = {"query": {"match_all": {}}, "size": size}
        if sort_field:
            body["sort"] = [{sort_field: {"order": "desc"}}]

        resp = es.search(index_name, body)
        hits = resp.get("hits", {})
        total = hits.get("total")
        return {
            "total":      total.get("value") if isinstance(total, dict) else total,
            "hits":       [{"_id": h.get("_id"), "_index": h.get("_index"),
                            **h.get("_source", {})} for h in hits.get("hits", [])],
            "sort_field": sort_field,
        }
    except Exception as e:
        return {"error": str(e)}


class FieldValuesRequest(BaseModel):
    """Distinct-value request for a table column filter."""
    indices: list[str] = []          # concrete indices / patterns to aggregate over
    index:   str = ""                # convenience single-index alias
    field:   str                     # the column/field to list distinct values for
    query:   dict = {}               # cascading query (other active filters); default match_all
    size:    int = 1000              # max distinct values to return


@router.post("/field-values")
def field_values(req: FieldValuesRequest):
    """
    Return the DISTINCT values of a field via a terms aggregation, so a table
    column filter can offer every value that exists in the index — not just the
    values present in the currently loaded page of rows.

    Honors an optional ``query`` (the other active column filters) so the value
    list stays Excel-style cascading, but computed across the whole index rather
    than the loaded sample.
    """
    try:
        es = get_client()
        idx_list = [i for i in (req.indices or ([req.index] if req.index else [])) if i]
        if not idx_list:
            return {"error": "no index provided", "values": []}
        target     = ",".join(idx_list)
        base_query = req.query or {"match_all": {}}

        # Date fields store epoch-millis in _source, but a terms aggregation's
        # key_as_string is the FORMATTED date. Returning the formatted string
        # would never match the raw cell value the table holds (the filter would
        # select a value that matches zero rows). So for date-typed fields we
        # return the raw epoch `key`; the UI formats it for display. Other types
        # keep key_as_string (e.g. booleans → "true"/"false").
        date_fields: set = set()
        try:
            mp = es.index_mapping(idx_list[0])
            date_fields = set(_date_field_names(mp))
        except Exception:
            pass
        field_is_date = req.field in date_fields

        def _agg(field_name: str) -> list:
            body = {
                "size": 0,
                "query": base_query,
                "aggs": {"vals": {"terms": {"field": field_name, "size": req.size}}},
            }
            resp = es.search(target, body)
            return resp.get("aggregations", {}).get("vals", {}).get("buckets", [])

        # ES 7+ text fields can't be aggregated directly — retry on the
        # conventional ``.keyword`` sub-field when the first attempt fails.
        try:
            buckets = _agg(req.field)
        except Exception as first_err:
            try:
                buckets = _agg(req.field + ".keyword")
            except Exception:
                raise first_err

        values = []
        for b in buckets:
            k = b.get("key") if field_is_date else b.get("key_as_string", b.get("key"))
            if k is None:
                k = b.get("key")
            if k is not None:
                values.append(str(k))
        # Distinct: two epoch-ms keys can format to the same second, so raw keys
        # avoid the "same date shown twice" the formatted output produced.
        seen, uniq = set(), []
        for v in values:
            if v not in seen:
                seen.add(v); uniq.append(v)
        return {"field": req.field, "values": uniq, "count": len(uniq),
                "is_date": field_is_date}
    except Exception as e:
        return {"error": str(e), "values": []}


# ── Create / delete indices ─────────────────────────────────────────────────────

class CreateIndexRequest(BaseModel):
    """Create a new (empty) index."""
    name:     str
    shards:   int = 1
    replicas: int = 0


@router.post("/create")
def create_index(req: CreateIndexRequest):
    """Create a new empty index with the given name and shard/replica counts."""
    name = (req.name or "").strip()
    ok, reason = _valid_index_name(name)
    if not ok:
        return {"error": reason}
    try:
        es = get_client()
        body = {"settings": {"index": {
            "number_of_shards":   max(1, int(req.shards)),
            "number_of_replicas": max(0, int(req.replicas)),
        }}}
        resp = es.put(f"/{name}", body)
        return {"ok": True, "name": name,
                "acknowledged": resp.get("acknowledged", True)}
    except Exception as e:
        return {"error": _es_error(e)}


class DuplicateIndexRequest(BaseModel):
    """Duplicate an index, optionally shifting every date field by an offset."""
    target:          str
    shift_amount:    int = 0          # 0 = plain copy, no date shifting
    shift_unit:      str = "days"     # minutes|hours|days|weeks|months (month = fixed 30 days)
    shift_direction: str = "past"     # "past" | "future"


# All shift units are FIXED intervals (a month is always 30 × 24h) so the
# offset applied to every document is identical and reversible.
_SHIFT_UNIT_MS = {
    "minutes": 60_000,
    "hours":   3_600_000,
    "days":    86_400_000,
    "weeks":   7 * 86_400_000,
    "months":  30 * 86_400_000,
}


@router.post("/{index_name}/duplicate")
def duplicate_index(index_name: str, req: DuplicateIndexRequest):
    """
    Copy ``index_name`` into a NEW index ``req.target``: settings (shards /
    replicas), mappings, and every document (ids preserved). When
    ``shift_amount`` is non-zero, every date-type field (per the source
    mapping) is shifted by the requested offset into the past or future —
    epoch-millisecond numbers and ISO-8601 strings are both handled; values
    that can't be parsed are copied unchanged.
    """
    from routers.query import _collect_date_fields, _scroll_hits

    source = (index_name or "").strip()
    target = (req.target or "").strip()
    if not source:
        return {"error": "source index name is required"}
    if any(ch in source for ch in "*?,"):
        return {"error": "wildcards are not allowed — pass an exact source index name"}
    ok, reason = _valid_index_name(target)
    if not ok:
        return {"error": f"target name: {reason}"}
    if target == source:
        return {"error": "target name must differ from the source index"}
    if req.shift_amount and req.shift_unit not in _SHIFT_UNIT_MS:
        return {"error": f"unknown shift unit {req.shift_unit!r} "
                         f"(expected one of {', '.join(_SHIFT_UNIT_MS)})"}
    if req.shift_direction not in ("past", "future"):
        return {"error": "shift_direction must be 'past' or 'future'"}

    try:
        es = get_client()

        # Refuse to overwrite an existing target.
        try:
            es.get(f"/{target}/_mapping")
            return {"error": f"index {target!r} already exists"}
        except Exception:
            pass                                   # 404 → good, target is free

        # ── Source mapping → date fields + create body ──────────────────────
        mapping_resp = es.index_mapping(source)
        src_map = mapping_resp.get(source) or next(iter(mapping_resp.values()), {})
        mappings = src_map.get("mappings", {})

        date_fields = {name for name, _sortable in _collect_date_fields(es, source)}

        shards, replicas = _source_shards_replicas(es, source)
        create_body: dict = {"settings": {"index": {
            "number_of_shards":   shards,
            "number_of_replicas": replicas,
        }}}
        if mappings:
            create_body["mappings"] = mappings     # pass through verbatim (ES 1.x & 5+ shapes)
        es.put(f"/{target}", create_body)

        # ── Copy documents, shifting date fields ────────────────────────────
        delta_ms = 0
        if req.shift_amount:
            delta_ms = req.shift_amount * _SHIFT_UNIT_MS[req.shift_unit]
            if req.shift_direction == "past":
                delta_ms = -delta_ms

        copied = failed = 0
        errors: list[str] = []
        batch: list[tuple[str, dict]] = []
        BULK_CHUNK = 1000

        def _flush(last: bool) -> None:
            nonlocal copied, failed
            if not batch:
                return
            ok_n, errs = _flush_batch(es, target, batch, refresh=last)
            copied += ok_n
            failed += len(errs)
            for msg in errs:
                if len(errors) < 5:
                    errors.append(msg)
            batch.clear()

        for h in _scroll_hits(es, source, {"match_all": {}}):
            src = h.get("_source") or {}
            if delta_ms and date_fields:
                src = _shift_date_values(src, date_fields, delta_ms)
            batch.append((str(h.get("_id") or ""), src))
            if len(batch) >= BULK_CHUNK:
                _flush(last=False)
        _flush(last=True)
        if copied and not batch:
            try:
                es.post(f"/{target}/_refresh")
            except Exception:
                pass

        return {"ok": failed == 0, "source": source, "target": target,
                "copied": copied, "failed": failed,
                "shift_ms": delta_ms, "shifted_fields": sorted(date_fields) if delta_ms else [],
                "errors": errors}
    except Exception as e:
        return {"error": _es_error(e)}


def _source_shards_replicas(es, index: str) -> tuple:
    """Read (number_of_shards, number_of_replicas) from an index's settings,
    tolerating both the nested (ES 5+) and dotted-key (ES 1.x) shapes."""
    try:
        resp = es.get(f"/{index}/_settings")
        st = next(iter(resp.values()), {}).get("settings", {})
        idx = st.get("index", st)
        shards   = idx.get("number_of_shards")   or st.get("index.number_of_shards")
        replicas = idx.get("number_of_replicas") or st.get("index.number_of_replicas")
        return int(shards or 1), int(replicas or 0)
    except Exception:
        return 1, 0


def _shift_date_values(node, date_fields: set, delta_ms: int):
    """Return a copy of *node* with every date field's value shifted by
    *delta_ms*. Walks nested objects/arrays; a key counts as a date field by
    its leaf name (matching how the mapping walk collects them). Epoch-ms
    numbers and ISO-8601 strings are shifted; anything unparsable is kept."""
    if isinstance(node, list):
        return [_shift_date_values(v, date_fields, delta_ms) for v in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for k, v in node.items():
        if k in date_fields and not isinstance(v, (dict, list)):
            out[k] = _shift_one_date(v, delta_ms)
        else:
            out[k] = _shift_date_values(v, date_fields, delta_ms)
    return out


def _shift_one_date(value, delta_ms: int):
    """Shift a single date value, preserving its original representation."""
    from datetime import datetime, timedelta
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return type(value)(value + delta_ms)
    if isinstance(value, str):
        t = value.strip()
        if _INT_RE.match(t):                     # epoch ms stored as a string
            return str(int(t) + delta_ms)
        try:
            iso = t.replace("Z", "+00:00") if t.endswith("Z") else t
            dt = datetime.fromisoformat(iso) + timedelta(milliseconds=delta_ms)
            s = dt.isoformat()
            if t.endswith("Z") and s.endswith("+00:00"):
                s = s[:-6] + "Z"
            return s
        except ValueError:
            return value                          # unknown format — copy as-is
    return value


@router.delete("/{index_name}")
def delete_index(index_name: str):
    """Delete an index. Refuses system indices and wildcards for safety."""
    name = (index_name or "").strip()
    if not name:
        return {"error": "index name is required"}
    if name.startswith("."):
        return {"error": "refusing to delete a system index (name starts with '.')"}
    if any(ch in name for ch in "*?,"):
        return {"error": "wildcards are not allowed when deleting — pass an exact index name"}
    try:
        es = get_client()
        resp = es.delete(f"/{name}")
        return {"ok": True, "name": name,
                "acknowledged": resp.get("acknowledged", True)}
    except Exception as e:
        return {"error": _es_error(e)}


# ── CSV import ──────────────────────────────────────────────────────────────────

@router.post("/{index_name}/import")
async def import_csv(index_name: str,
                     file: UploadFile = File(...),
                     id_column: str = Query(default="_id")):
    """
    Bulk-index the rows of an uploaded CSV into ``index_name``.

    The CSV is expected in the same shape the app exports: a header row of
    column names, ``_id`` / ``_index`` metadata columns (dropped from the doc
    body), object/array cells JSON-encoded, scalar cells stringified, and empty
    cells meaning the field was absent. The ``_id`` column (configurable via
    ``id_column``) sets each document's id; drop/rename it to let ES auto-assign.
    """
    name = (index_name or "").strip()
    if not name:
        return {"error": "index name is required"}
    try:
        raw = await file.read()
    except Exception as e:
        return {"error": f"could not read upload: {e}"}
    if not raw:
        return {"error": "uploaded file is empty"}

    # utf-8-sig transparently strips a BOM that Excel likes to prepend.
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")

    reader = csv.reader(io.StringIO(text))
    try:
        headers = [h.strip() for h in next(reader)]
    except StopIteration:
        return {"error": "uploaded file has no rows"}
    if not any(headers):
        return {"error": "uploaded file has no header row"}

    es = get_client()
    meta_cols = {"_id", "_index"}
    indexed = failed = rows = 0
    errors: list[str] = []
    batch: list[tuple[str, dict]] = []
    BULK_CHUNK = 1000

    def take_errors(errs: list) -> None:
        for msg in errs:
            if len(errors) < 5:
                errors.append(msg)

    for row in reader:
        if not row or all(c == "" for c in row):
            continue                       # skip blank lines
        rows += 1
        doc_id = ""
        source: dict = {}
        for i, col in enumerate(headers):
            if not col or i >= len(row):
                continue
            cell = row[i]
            if col == id_column:
                doc_id = cell.strip()
            if col in meta_cols:
                continue                   # metadata, not a document field
            val = _coerce_cell(cell)
            if val is not _MISSING:
                source[col] = val
        batch.append((doc_id, source))
        if len(batch) >= BULK_CHUNK:
            ok, errs = _flush_batch(es, name, batch, refresh=False)
            indexed += ok
            failed  += len(errs)
            take_errors(errs)
            batch = []

    if batch:
        # Refresh on the final chunk so the imported docs are visible at once.
        ok, errs = _flush_batch(es, name, batch, refresh=True)
        indexed += ok
        failed  += len(errs)
        take_errors(errs)
    elif indexed:
        try:
            es.post(f"/{name}/_refresh")
        except Exception:
            pass

    return {"ok": failed == 0, "index": name, "rows": rows,
            "indexed": indexed, "failed": failed, "errors": errors}


def _flush_batch(es, index_name: str, batch: list, refresh: bool) -> tuple:
    """Bulk-index one batch of (doc_id, source) pairs. Returns (ok, [errors])."""
    lines = []
    for doc_id, source in batch:
        action = {"index": {"_index": index_name}}
        if doc_id:
            action["index"]["_id"] = doc_id
        lines.append(json.dumps(action))
        lines.append(json.dumps(source, default=str))
    resp = es.bulk("\n".join(lines) + "\n", refresh=refresh)
    ok, errs = 0, []
    for item in resp.get("items", []):
        res = item.get("index") or item.get("create") or {}
        err = res.get("error")
        if err:
            if isinstance(err, dict):
                errs.append(f"{err.get('type', 'error')}: {err.get('reason', '')}".strip())
            else:
                errs.append(str(err))
        else:
            ok += 1
    return ok, errs


_MISSING  = object()
_INT_RE   = re.compile(r"^-?(?:0|[1-9][0-9]*)$")
_FLOAT_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)\.[0-9]+(?:[eE][-+]?[0-9]+)?$")


def _coerce_cell(raw: str):
    """Turn an exported CSV cell back into a typed JSON value.

    Mirrors the app's CSV export: objects/arrays were JSON-encoded, scalars
    stringified, and an empty cell meant the field was absent (→ ``_MISSING``,
    so it is dropped rather than stored as ""). Integers/floats/booleans are
    coerced back so a re-import into a fresh index gets sensible dynamic types;
    numbers with leading zeros stay strings to preserve ids.
    """
    if raw == "":
        return _MISSING
    t = raw.strip()
    if not t:
        return raw
    if t[0] in "{[":
        try:
            return json.loads(t)
        except Exception:
            return raw
    low = t.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if _INT_RE.match(t):
        try:
            return int(t)
        except Exception:
            return raw
    if ("." in t or "e" in low) and _FLOAT_RE.match(t):
        try:
            return float(t)
        except Exception:
            return raw
    return raw


def _valid_index_name(name: str) -> tuple:
    """Validate an index name against Elasticsearch's naming rules."""
    if not name:
        return False, "index name is required"
    if len(name.encode("utf-8")) > 255:
        return False, "index name is too long (max 255 bytes)"
    if name in (".", ".."):
        return False, "index name cannot be '.' or '..'"
    if name != name.lower():
        return False, "index name must be lowercase"
    if name[0] in "-_+":
        return False, "index name cannot start with '-', '_' or '+'"
    bad = sorted(set(name) & set('\\/*?"<>| ,#:'))
    if bad:
        return False, "index name contains invalid characters: " + " ".join(bad)
    return True, ""


def _es_error(exc: Exception) -> str:
    """Extract a human-readable message from a requests/ES error."""
    resp = getattr(exc, "response", None)
    if resp is not None:
        try:
            j = resp.json()
            err = j.get("error", j)
            if isinstance(err, dict):
                return err.get("reason") or err.get("type") or str(j)
            return str(err)
        except Exception:
            return f"HTTP {resp.status_code}: {resp.text[:300]}"
    return str(exc)



# ── Helpers ────────────────────────────────────────────────────────────���──────

def _safe_int(val) -> int:
    try:   return int(val)
    except: return 0


def _count_mapping_fields(mapping: dict) -> int:
    count = 0
    def _walk(obj):
        nonlocal count
        if isinstance(obj, dict):
            if "type" in obj:
                count += 1
            for v in obj.values():
                _walk(v)
    for idx_map in mapping.values():
        _walk(idx_map.get("mappings", {}).get("properties", {}))
    return count


def _top_level_field_names(mapping: dict) -> list:
    """Top-level property names declared in the index mapping.

    Table columns are the top-level keys of each document's ``_source``; nested
    objects appear as a single column, so we only expose the direct children of
    ``properties`` here.  Used by the UI to tell mapped vs. unmapped columns apart.
    """
    names = set()
    for idx_map in mapping.values():
        props = idx_map.get("mappings", {}).get("properties", {})
        if isinstance(props, dict):
            names.update(props.keys())
    return sorted(names)


def _date_field_names(mapping: dict) -> list:
    """Top-level property names whose mapping type is ``date``.

    The UI uses this to render epoch-millis date columns human-readable while
    keeping the raw value for filtering/matching (CC stores dates as epoch ms).
    """
    names = set()
    for idx_map in mapping.values():
        props = idx_map.get("mappings", {}).get("properties", {})
        if isinstance(props, dict):
            for fname, fmeta in props.items():
                if isinstance(fmeta, dict) and fmeta.get("type") == "date":
                    names.add(fname)
    return sorted(names)

