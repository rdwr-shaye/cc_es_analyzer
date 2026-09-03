"""Browsing the CC's PostgreSQL: health, databases, tables, sample rows.

Same shape as modules/maria/routers/browse.py, and the same reporting
convention: every endpoint here is read-only, and answers with {"error": ...}
at HTTP 200 for operational failures — an unreachable database is a normal
state of a CC being debugged, not an exceptional one.

Endpoints take `database`, not `schema`. PostgreSQL's `public` schema inside
each database is where this CC keeps its own tables (verified on a lab CC:
`pg_catalog`/`information_schema` are the only other schemas present, and they
are PostgreSQL's own, not the CC's) — so this module browses ONE database at
a time and, within it, defaults to `public` rather than asking the user to
pick a schema they will not otherwise have reason to think about.
"""

from __future__ import annotations

import json
import logging
import re

from fastapi import APIRouter, Query, Response

from config import settings
from modules.maria import blobs  # generic byte-blob analysis, not MariaDB-specific
from modules.pg import catalog
from modules.pg.client import PgError, resolve_host, run, run_raw, server_info

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/pg", tags=["postgresql"])

# The schema this module treats as "the CC's own tables" inside a database.
# See modules/pg/catalog.py for why this is fixed rather than discovered.
_APP_SCHEMA = "public"


@router.get("/health")
def pg_health():
    """Is the CC's PostgreSQL reachable, and what is it."""
    try:
        return server_info()
    except PgError as exc:
        host, why = resolve_host()
        return {"connected": False, "error": str(exc),
                "host": host, "host_source": why, "port": settings.pg_port}


@router.get("/databases")
def pg_databases(include_system: bool = Query(False)):
    """Databases on this CC, annotated from the curated catalog.

    Driven by what the server actually reports rather than the catalog, so a
    CC carrying a database this file has never seen still shows it — flagged
    as uncatalogued instead of quietly omitted. Connects to the maintenance
    database (settings.pg_default_database): pg_database is server-wide and
    is not owned by any one CC database.
    """
    try:
        rows, _ = run(
            "SELECT datname AS name, pg_database_size(datname) AS bytes "
            "FROM pg_database WHERE NOT datistemplate ORDER BY 2 DESC",
            database=settings.pg_default_database, limit=500,
        )
    except PgError as exc:
        return {"error": str(exc)}

    out = []
    for row in rows:
        name = row["name"]
        info = catalog.describe(name)
        if info and info.system and not include_system:
            continue
        out.append({
            "name": name,
            "size_mb": round((row["bytes"] or 0) / 1048576, 1),
            "title": info.title if info else name,
            "description": info.description if info else "",
            "category": info.category if info else "Uncatalogued",
            "system": bool(info and info.system),
            "catalogued": info is not None,
        })
    return {"databases": out, "count": len(out)}


@router.get("/tables")
def pg_tables(database: str = Query(...), search: str = Query("")):
    """Tables in one database's `public` schema, with row estimates and sizes.

    n_live_tup from pg_stat_user_tables is an ESTIMATE refreshed by autovacuum/
    ANALYZE, not a live count — the same caveat modules/maria/routers/browse.py
    states for MariaDB's table_rows, and for the same reason: labelled here so
    the UI can present it honestly.
    """
    try:
        rows, truncated = run(
            "SELECT c.relname AS name, "
            "       COALESCE(s.n_live_tup, 0) AS row_estimate, "
            "       ROUND(pg_total_relation_size(c.oid) / 1024.0) AS kb, "
            "       obj_description(c.oid, 'pg_class') AS comment "
            "FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid "
            "WHERE n.nspname = %s AND c.relkind = 'r' AND c.relname ILIKE %s "
            "ORDER BY name",
            (_APP_SCHEMA, f"%{search}%" if search else "%"),
            database=database, limit=2000,
        )
    except PgError as exc:
        return {"error": str(exc)}

    info = catalog.describe(database)
    return {
        "database": database,
        "title": info.title if info else database,
        "description": info.description if info else "",
        "tables": [{"name": r["name"], "row_estimate": int(r["row_estimate"] or 0),
                    "size_kb": int(r["kb"] or 0), "comment": r["comment"] or ""}
                   for r in rows],
        "row_estimate_is_approximate": True,
        "truncated": truncated,
    }


@router.get("/columns")
def pg_columns(database: str = Query(...), table: str = Query(...)):
    """Column definitions for one table."""
    try:
        rows, _ = run(
            # `is_identity`/`identity_generation` are PostgreSQL's equivalent
            # of MariaDB's `extra` AUTO_INCREMENT flag — what the UI checks
            # before offering a cell as editable. modules/pg/writes.py
            # re-checks all of it server-side; this is not the authority.
            "SELECT column_name AS name, data_type, udt_name, is_nullable AS nullable, "
            "       column_default AS default_value, is_identity, identity_generation, "
            "       is_generated, generation_expression "
            "FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s "
            "ORDER BY ordinal_position",
            (_APP_SCHEMA, table), database=database, limit=2000,
        )
    except PgError as exc:
        return {"error": str(exc)}
    pk = _primary_key(database, table)
    return {"database": database, "table": table,
            "columns": [{**r, "key_type": "PRI" if r["name"] in pk else ""}
                        for r in rows]}


@router.get("/sample")
def pg_sample(database: str = Query(...), table: str = Query(...),
             size: int = Query(50, ge=1, le=1000)):
    """First rows of a table.

    Identifiers cannot be parameter-bound (they are not values), so they are
    validated against information_schema and then quoted. Checking existence
    is what makes this safe: a name that is not a real table in that database
    never reaches a statement.
    """
    exists, _ = _table_exists(database, table)
    if not exists:
        return {"error": f"no table {database}.{table} on this CC"}

    ident = _ident(table)
    try:
        rows, truncated = run(f"SELECT * FROM {ident}", database=database, limit=size)
    except PgError as exc:
        return {"error": str(exc)}

    columns = list(rows[0].keys()) if rows else []
    return {"database": database, "table": table, "columns": columns,
            "rows": rows, "count": len(rows), "truncated": truncated,
            "primary_key": _primary_key(database, table),
            "blob_columns": _blob_columns(database, table)}


@router.get("/keys")
def pg_keys(database: str = Query(...), table: str = Query(...)):
    """What the primary/unique/foreign keys on this table actually are.

    Same three-part answer as modules/maria/routers/browse.py::maria_keys:
    ``outbound`` (this table's own foreign keys), ``inbound`` (other tables'
    foreign keys pointing AT this one — what tells you a row cannot simply be
    deleted), and ``candidates`` (same-named indexed columns elsewhere in the
    database, a labelled guess, not a declaration).
    """
    exists, err = _table_exists(database, table)
    if not exists:
        return {"error": err or f"no table {database}.{table} on this CC"}

    try:
        idx_rows, _ = run(
            # `ix.indkey` is an int2vector, PostgreSQL's own fixed-length
            # vector type rather than a normal 1-based array — cast then
            # unnest WITH ORDINALITY rather than array_position(), which was
            # verified on a live CC to return int2vector's internal 0-based
            # subscript instead of a usable rank.
            "SELECT i.relname AS name, ix.indisunique AS is_unique, "
            "       ix.indisprimary AS is_primary, a.attname AS col, ord.pos "
            "FROM pg_index ix "
            "JOIN pg_class t ON t.oid = ix.indrelid "
            "JOIN pg_class i ON i.oid = ix.indexrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "JOIN LATERAL unnest(ix.indkey::int2[]) WITH ORDINALITY AS ord(attnum, pos) ON true "
            "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ord.attnum "
            "WHERE n.nspname = %s AND t.relname = %s "
            "ORDER BY i.relname, ord.pos",
            (_APP_SCHEMA, table), database=database, limit=1000)

        # information_schema.constraint_column_usage does NOT pair local and
        # referenced columns positionally for a COMPOSITE foreign key — joining
        # it to key_column_usage on constraint_name alone produces every
        # combination of the two sides (verified on a live two-column FK: 2x2
        # rows instead of 2). pg_constraint's own conkey/confkey arrays are
        # already correctly paired by position, so unnest them together WITH
        # ORDINALITY instead of going through information_schema at all.
        out_rows, _ = run(
            "SELECT con.conname AS name, att.attname AS col, "
            "       fn.nspname AS ref_schema, ft.relname AS ref_table, "
            "       fatt.attname AS ref_col, ord.pos "
            "FROM pg_constraint con "
            "JOIN pg_class t ON t.oid = con.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "JOIN pg_class ft ON ft.oid = con.confrelid "
            "JOIN pg_namespace fn ON fn.oid = ft.relnamespace "
            "JOIN LATERAL unnest(con.conkey, con.confkey) WITH ORDINALITY "
            "  AS ord(localattnum, fattnum, pos) ON true "
            "JOIN pg_attribute att ON att.attrelid = t.oid AND att.attnum = ord.localattnum "
            "JOIN pg_attribute fatt ON fatt.attrelid = ft.oid AND fatt.attnum = ord.fattnum "
            "WHERE con.contype = 'f' AND n.nspname = %s AND t.relname = %s "
            "ORDER BY con.conname, ord.pos",
            (_APP_SCHEMA, table), database=database, limit=500)

        in_rows, _ = run(
            "SELECT con.conname AS name, n.nspname AS from_schema, "
            "       t.relname AS from_table, att.attname AS from_col, "
            "       fatt.attname AS col, ord.pos "
            "FROM pg_constraint con "
            "JOIN pg_class t ON t.oid = con.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "JOIN pg_class ft ON ft.oid = con.confrelid "
            "JOIN pg_namespace fn ON fn.oid = ft.relnamespace "
            "JOIN LATERAL unnest(con.conkey, con.confkey) WITH ORDINALITY "
            "  AS ord(localattnum, fattnum, pos) ON true "
            "JOIN pg_attribute att ON att.attrelid = t.oid AND att.attnum = ord.localattnum "
            "JOIN pg_attribute fatt ON fatt.attrelid = ft.oid AND fatt.attnum = ord.fattnum "
            "WHERE con.contype = 'f' AND fn.nspname = %s AND ft.relname = %s "
            "ORDER BY t.relname, con.conname, ord.pos",
            (_APP_SCHEMA, table), database=database, limit=500)
    except PgError as exc:
        return {"error": str(exc)}

    indexes: dict[str, dict] = {}
    for r in idx_rows:
        entry = indexes.setdefault(r["name"], {
            "name": r["name"], "unique": bool(r["is_unique"]),
            "primary": bool(r["is_primary"]), "columns": [],
        })
        entry["columns"].append(r["col"])
    index_list = sorted(indexes.values(),
                        key=lambda i: (not i["primary"], not i["unique"], i["name"]))

    outbound = _group_fk(out_rows, lambda r: {
        "ref_schema": r["ref_schema"], "ref_table": r["ref_table"]},
        col_key="col", ref_key="ref_col")
    inbound = _group_fk(in_rows, lambda r: {
        "from_schema": r["from_schema"], "from_table": r["from_table"]},
        col_key="from_col", ref_key="col")

    return {
        "database": database, "table": table,
        "indexes": index_list,
        "outbound": outbound,
        "inbound": inbound,
        "declared": bool(outbound or inbound),
        "candidates": _candidate_relations(database, table, index_list),
    }


def _group_fk(rows: list[dict], ident, col_key: str, ref_key: str) -> list[dict]:
    grouped: dict[tuple, dict] = {}
    for r in rows:
        info = ident(r)
        gid = (r["name"],) + tuple(info.values())
        entry = grouped.setdefault(gid, {"constraint": r["name"], **info,
                                         "columns": [], "ref_columns": []})
        entry["columns"].append(r[col_key])
        entry["ref_columns"].append(r[ref_key])
    return list(grouped.values())


_CANDIDATE_TABLE_CAP = 25


def _candidate_relations(database: str, table: str,
                         indexes: list[dict]) -> list[dict]:
    """Same-named columns in other tables of this database, for the indexed
    columns of this one. An inference, never presented as a declaration —
    identical reasoning to modules/maria/routers/browse.py::_candidate_relations."""
    keyed: list[str] = []
    for idx in indexes:
        for col in idx["columns"]:
            if col not in keyed:
                keyed.append(col)
    if not keyed:
        return []

    placeholders = ", ".join(["%s"] * len(keyed))
    try:
        rows, _ = run(
            f"SELECT column_name AS col, table_name AS name "
            f"FROM information_schema.columns "
            f"WHERE table_schema = %s AND table_name <> %s "
            f"  AND column_name IN ({placeholders}) "
            f"ORDER BY column_name, table_name",
            (_APP_SCHEMA, table, *keyed), database=database, limit=5000)
    except PgError:
        return []

    by_col: dict[str, list[str]] = {}
    for r in rows:
        by_col.setdefault(r["col"], []).append(r["name"])

    out = []
    for col in keyed:
        tables = by_col.get(col, [])
        if not tables:
            continue
        out.append({
            "column": col,
            "count": len(tables),
            "tables": tables[:_CANDIDATE_TABLE_CAP],
            "truncated": len(tables) > _CANDIDATE_TABLE_CAP,
        })
    out.sort(key=lambda c: c["count"])
    return out


def _primary_key(database: str, table: str) -> list[str]:
    try:
        rows, _ = run(
            "SELECT kcu.column_name AS name "
            "FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu "
            "  ON kcu.constraint_name = tc.constraint_name AND kcu.table_schema = tc.table_schema "
            "WHERE tc.table_schema = %s AND tc.table_name = %s "
            "  AND tc.constraint_type = 'PRIMARY KEY' "
            "ORDER BY kcu.ordinal_position",
            (_APP_SCHEMA, table), database=database, limit=64)
    except PgError:
        return []
    return [r["name"] for r in rows]


def _blob_columns(database: str, table: str) -> list[str]:
    """bytea columns — PostgreSQL's binary type. What the UI offers a
    download for, matching modules/maria/routers/browse.py::_blob_columns."""
    try:
        rows, _ = run(
            "SELECT column_name AS name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s AND data_type = 'bytea' "
            "ORDER BY ordinal_position",
            (_APP_SCHEMA, table), database=database, limit=256)
    except PgError:
        return []
    return [r["name"] for r in rows]


@router.get("/blob/preview")
def pg_blob_preview(database: str = Query(...), table: str = Query(...),
                    column: str = Query(...), key: str = Query(...)):
    """The readable content of a bytea column, without downloading it."""
    data, err, status = _fetch_blob(database, table, column, key)
    if err:
        return Response(content=json.dumps({"error": err}), status_code=status,
                        media_type="application/json")
    view = blobs.describe(data)
    view.update({"database": database, "table": table, "column": column})
    return view


@router.get("/blob")
def pg_blob(database: str = Query(...), table: str = Query(...),
           column: str = Query(...), key: str = Query(...)):
    """Download one bytea column value as a file."""
    data, err, status = _fetch_blob(database, table, column, key)
    if err:
        return Response(content=json.dumps({"error": err}), status_code=status,
                        media_type="application/json")

    safe = re.sub(r"[^A-Za-z0-9._-]", "_", f"{table}.{column}")
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{safe}.bin"',
            "Content-Length": str(len(data)),
        },
    )


def _fetch_blob(database: str, table: str, column: str,
                key: str) -> tuple[bytes, str, int]:
    """Validate the request and return the raw bytes: (data, error, status).
    Identical contract to modules/maria/routers/browse.py::_fetch_blob."""
    try:
        wanted = json.loads(key)
        if not isinstance(wanted, dict) or not wanted:
            raise ValueError
    except (ValueError, TypeError):
        return b"", ("key must be a JSON object of primary-key column to value"), 400

    if column not in _blob_columns(database, table):
        return b"", f"{column!r} is not a bytea column of {database}.{table}", 400

    pk = _primary_key(database, table)
    if not pk:
        return b"", (f"{database}.{table} has no primary key, so a single row "
                     f"cannot be addressed"), 400
    if set(wanted) != set(pk):
        return b"", f"key must name exactly the primary key ({', '.join(pk)})", 400

    ident = _ident(table)
    col_ident = _col_ident(column)
    where = " AND ".join(f"{_col_ident(c)} = %s" for c in pk)
    params = tuple(wanted[c] for c in pk)

    try:
        rows = run_raw(f"SELECT {col_ident} AS blob_value FROM {ident} WHERE {where}",
                       params, database=database)
    except PgError as exc:
        return b"", str(exc), 400

    if not rows:
        return b"", "no row matches that key", 404
    if len(rows) > 1:
        return b"", "that key matched more than one row", 409

    value = rows[0][0]
    if value is None:
        return b"", "this row's value is NULL", 404
    return (bytes(value) if not isinstance(value, bytes) else value), "", 200


def _table_exists(database: str, table: str) -> tuple[bool, str]:
    try:
        rows, _ = run(
            "SELECT 1 AS ok FROM information_schema.tables "
            "WHERE table_schema = %s AND table_name = %s",
            (_APP_SCHEMA, table), database=database, limit=1)
    except PgError as exc:
        return False, str(exc)
    return bool(rows), ""


def _ident(table: str) -> str:
    """Quote an already-validated table name in the app schema. Identifiers
    cannot be parameter-bound; doubling embedded quotes is what makes this
    safe against a name containing one, matching PostgreSQL's own escaping
    rule for double-quoted identifiers."""
    return f'"{_APP_SCHEMA}"."{table.replace(chr(34), chr(34) * 2)}"'


def _col_ident(column: str) -> str:
    return f'"{column.replace(chr(34), chr(34) * 2)}"'
