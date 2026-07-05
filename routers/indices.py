from fastapi import APIRouter, Query
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
            k = b.get("key_as_string", b.get("key"))
            if k is not None:
                values.append(str(k))
        return {"field": req.field, "values": values, "count": len(values)}
    except Exception as e:
        return {"error": str(e), "values": []}



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

