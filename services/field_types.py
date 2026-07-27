"""
Which field name to use for an EXACT match.

CC index templates map many string fields as analyzed text with a `raw`
(or `keyword`) sub-field beside them, e.g.

    "applicationId": {
        "type": "text", "analyzer": "autocomplete",
        "fields": {"raw": {"type": "keyword"}}
    }

A `term` query does not analyze its input, so `term: {applicationId: "798:80"}`
looks for that literal string among the tokens the analyzer produced — for the
autocomplete analyzer those are `7, 79, 798, 9, 98, 8, 80, 0`, so it matches
NOTHING. The same field also cannot be aggregated or sorted on ("Text fields
are not optimised for operations that require per-document field data").
`applicationId.raw` is a keyword: it holds the value verbatim and works for
all three.

`exact_field_map()` returns, for every leaf field in an index, the name to use
when an exact value is meant. Fields that are already exact (keyword, numeric,
date, boolean, ip) map to themselves, so callers can simply do

    field = exact_map.get(field, field)

Both mapping shapes are handled: ES 5+/OpenSearch (`text` + `fields`) and the
ES 1.x form (`string` with `index: not_analyzed`, sub-fields under `fields`).
"""
import logging
import threading
import time

logger = logging.getLogger(__name__)

# Types that are already exact-matchable as-is.
_EXACT_TYPES = {
    "keyword", "constant_keyword", "wildcard", "boolean", "ip", "date",
    "long", "integer", "short", "byte", "double", "float", "half_float",
    "scaled_float", "unsigned_long", "version",
}

# Preference order when a text field has several exact sub-fields.
_SUBFIELD_PREFERENCE = ("raw", "keyword", "exact", "not_analyzed")

_CACHE_TTL_S = 300
_cache: dict[tuple, tuple[float, dict]] = {}
_lock = threading.Lock()


def _is_exact_spec(spec: dict) -> bool:
    """True when this mapping node can take a term query verbatim."""
    t = spec.get("type")
    if t in _EXACT_TYPES:
        return True
    # ES 1.x: analyzed unless explicitly not_analyzed.
    if t == "string":
        return spec.get("index") == "not_analyzed"
    return False


def _pick_subfield(spec: dict) -> str | None:
    """Name of the sub-field to use for exact matching, if there is one."""
    subs = spec.get("fields") or spec.get("multi_fields") or {}
    if not isinstance(subs, dict):
        return None
    exact = [n for n, s in subs.items() if isinstance(s, dict) and _is_exact_spec(s)]
    if not exact:
        return None
    for preferred in _SUBFIELD_PREFERENCE:
        if preferred in exact:
            return preferred
    return sorted(exact)[0]


def _walk(props: dict, prefix: str, out: dict) -> None:
    for name, spec in (props or {}).items():
        if not isinstance(spec, dict):
            continue
        path = f"{prefix}{name}"
        nested = spec.get("properties")
        if isinstance(nested, dict):
            _walk(nested, path + ".", out)
            continue
        if _is_exact_spec(spec):
            out.setdefault(path, path)
        else:
            sub = _pick_subfield(spec)
            # An analyzed field with no exact sub-field stays as-is: a term
            # query on it is unreliable, but inventing a name would 400.
            out.setdefault(path, f"{path}.{sub}" if sub else path)


def exact_field_map(es, index: str) -> dict:
    """{field: field-to-use-for-exact-match} for *index* (a name or pattern).

    A pattern is merged across the indices it expands to; the first index that
    declares a field wins, which is what the UI shows anyway.
    """
    key = (getattr(es, "base_url", "?"), index)
    now = time.time()
    with _lock:
        hit = _cache.get(key)
    if hit and now - hit[0] < _CACHE_TTL_S:
        return hit[1]

    out: dict = {}
    try:
        resp = es.get(f"/{index}/_mapping", params={})
    except Exception as exc:
        logger.warning("[field_types] mapping fetch for %r failed: %s", index, exc)
        return out

    def _collect(node) -> None:
        if not isinstance(node, dict):
            return
        props = node.get("properties")
        if isinstance(props, dict):
            _walk(props, "", out)
            return
        for v in node.values():
            _collect(v)

    _collect(resp)
    with _lock:
        _cache[key] = (now, out)
    return out


def resolve_exact(es, index: str, field: str) -> str:
    """The exact-match name for one field (the field itself when unknown)."""
    if not field:
        return field
    return exact_field_map(es, index).get(field, field)
