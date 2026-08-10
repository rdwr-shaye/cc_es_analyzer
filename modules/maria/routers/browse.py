"""Browsing the CC's MariaDB: health, schemas, tables, sample rows.

Every endpoint here is read-only. They answer with {"error": ...} at HTTP 200
for operational failures — an unreachable database is a normal state of a CC
being debugged, not an exceptional one — matching what the ES routers do and
what the frontend already knows how to render.
"""

from __future__ import annotations

import json
import logging
import re

from fastapi import APIRouter, Query, Response

from config import settings
from modules.maria import blobs, catalog
from modules.maria.client import MariaError, resolve_host, run, run_raw, server_info

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/maria", tags=["mariadb"])


@router.get("/health")
def maria_health():
    """Is the CC's MariaDB reachable, and what is it."""
    try:
        return server_info()
    except MariaError as exc:
        host, why = resolve_host()
        return {"connected": False, "error": str(exc),
                "host": host, "host_source": why, "port": settings.maria_port}


@router.get("/schemas")
def maria_schemas(include_system: bool = Query(False)):
    """Schemas on this CC, annotated from the curated catalog.

    Driven by what the server actually reports rather than by the catalog, so a
    CC carrying a schema we have never seen still shows it — flagged as
    uncatalogued instead of quietly omitted.
    """
    try:
        rows, _ = run(
            "SELECT table_schema AS name, COUNT(*) AS tables, "
            "       COALESCE(ROUND(SUM(data_length + index_length) / 1048576), 0) AS mb "
            "FROM information_schema.tables "
            "GROUP BY table_schema ORDER BY 3 DESC",
            limit=500,
        )
    except MariaError as exc:
        return {"error": str(exc)}

    out = []
    for row in rows:
        name = row["name"]
        info = catalog.describe(name)
        if info and info.system and not include_system:
            continue
        out.append({
            "name": name,
            "tables": int(row["tables"] or 0),
            "size_mb": int(row["mb"] or 0),
            "title": info.title if info else name,
            "description": info.description if info else "",
            "category": info.category if info else "Uncatalogued",
            "system": bool(info and info.system),
            "catalogued": info is not None,
        })
    return {"schemas": out, "count": len(out)}


@router.get("/tables")
def maria_tables(schema: str = Query(...), search: str = Query("")):
    """Tables in one schema, with row estimates and sizes.

    table_rows from information_schema is an ESTIMATE for InnoDB, sometimes off
    by a wide margin. Labelled as such in the payload so the UI can present it
    honestly rather than as a count someone might quote in a bug report.
    """
    try:
        rows, truncated = run(
            "SELECT table_name AS name, table_rows AS row_estimate, "
            "       COALESCE(ROUND((data_length + index_length) / 1024), 0) AS kb, "
            "       table_comment AS comment "
            "FROM information_schema.tables "
            "WHERE table_schema = %s AND table_name LIKE %s "
            "ORDER BY name",
            (schema, f"%{search}%" if search else "%"),
            limit=2000,
        )
    except MariaError as exc:
        return {"error": str(exc)}

    info = catalog.describe(schema)
    return {
        "schema": schema,
        "title": info.title if info else schema,
        "description": info.description if info else "",
        "tables": [{"name": r["name"], "row_estimate": int(r["row_estimate"] or 0),
                    "size_kb": int(r["kb"] or 0), "comment": r["comment"] or ""}
                   for r in rows],
        "row_estimate_is_approximate": True,
        "truncated": truncated,
    }


@router.get("/columns")
def maria_columns(schema: str = Query(...), table: str = Query(...)):
    """Column definitions for one table."""
    try:
        rows, _ = run(
            # `extra` and `data_type` travel with the definition so the UI can
            # decide which cells to OFFER as editable without a round-trip per
            # column. It is not the authority on what may be written —
            # modules/maria/writes.py re-checks all of it server-side.
            "SELECT column_name AS name, column_type AS type, is_nullable AS nullable, "
            "       column_key AS key_type, column_default AS default_value, "
            "       column_comment AS comment, extra, data_type "
            "FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s "
            "ORDER BY ordinal_position",
            (schema, table),
            limit=2000,
        )
    except MariaError as exc:
        return {"error": str(exc)}
    return {"schema": schema, "table": table, "columns": rows}


@router.get("/sample")
def maria_sample(schema: str = Query(...), table: str = Query(...),
                 size: int = Query(50, ge=1, le=1000)):
    """First rows of a table.

    The identifiers cannot be parameter-bound (they are not values), so they are
    validated against information_schema and then quoted. Checking existence is
    what makes this safe: a name that is not a real table in that schema never
    reaches a statement.
    """
    exists, _ = _table_exists(schema, table)
    if not exists:
        return {"error": f"no table {schema}.{table} on this CC"}

    ident = f"`{schema.replace('`', '')}`.`{table.replace('`', '')}`"
    try:
        rows, truncated = run(f"SELECT * FROM {ident}", limit=size)
    except MariaError as exc:
        return {"error": str(exc)}

    columns = list(rows[0].keys()) if rows else []
    # The primary key travels with the rows so the UI can address one of them
    # later — downloading a BLOB needs to name a row, and without a key there
    # is no honest way to say which. A table with no primary key simply gets no
    # download offered, rather than a WHERE built from every column.
    return {"schema": schema, "table": table, "columns": columns,
            "rows": rows, "count": len(rows), "truncated": truncated,
            "primary_key": _primary_key(schema, table),
            "blob_columns": _blob_columns(schema, table)}


@router.get("/keys")
def maria_keys(schema: str = Query(...), table: str = Query(...)):
    """What the PRI / UNI / MUL flags on a column actually mean for this table.

    A key badge on its own says a column is indexed but not what it is indexed
    WITH — and on a composite key that is the whole question. So this returns
    the indexes with their column order, plus the relationships:

      * ``outbound`` — this table's declared foreign keys.
      * ``inbound``  — other tables whose foreign keys point AT this one, which
        is what tells you a row cannot simply be deleted.
      * ``candidates`` — same-named columns elsewhere in the schema.

    That last one exists because of what the CC's data model actually is: a
    schema can carry a full set of MUL columns and no FOREIGN KEY constraints
    at all, and then ``outbound``/``inbound`` are both empty and the screen
    would imply the table is unrelated to anything. Name matching is a guess,
    and is labelled a guess — but ``device_id`` in eleven tables is exactly the
    join an engineer is looking for. The two are kept in separate fields so
    nobody mistakes the inference for a declaration.
    """
    exists, err = _table_exists(schema, table)
    if not exists:
        return {"error": err or f"no table {schema}.{table} on this CC"}

    try:
        idx_rows, _ = run(
            "SELECT index_name AS name, non_unique, seq_in_index, column_name AS col "
            "FROM information_schema.statistics "
            "WHERE table_schema = %s AND table_name = %s "
            "ORDER BY index_name, seq_in_index",
            (schema, table), limit=1000)

        out_rows, _ = run(
            "SELECT k.constraint_name AS name, k.column_name AS col, "
            "       k.referenced_table_schema AS ref_schema, "
            "       k.referenced_table_name AS ref_table, "
            "       k.referenced_column_name AS ref_col, k.ordinal_position AS pos "
            "FROM information_schema.key_column_usage k "
            "WHERE k.table_schema = %s AND k.table_name = %s "
            "  AND k.referenced_table_name IS NOT NULL "
            "ORDER BY k.constraint_name, k.ordinal_position",
            (schema, table), limit=500)

        in_rows, _ = run(
            "SELECT k.constraint_name AS name, k.table_schema AS from_schema, "
            "       k.table_name AS from_table, k.column_name AS from_col, "
            "       k.referenced_column_name AS col, k.ordinal_position AS pos "
            "FROM information_schema.key_column_usage k "
            "WHERE k.referenced_table_schema = %s AND k.referenced_table_name = %s "
            "ORDER BY k.table_name, k.constraint_name, k.ordinal_position",
            (schema, table), limit=500)
    except MariaError as exc:
        return {"error": str(exc)}

    # Indexes, rebuilt from the one-row-per-column listing.
    indexes: dict[str, dict] = {}
    for r in idx_rows:
        entry = indexes.setdefault(r["name"], {
            "name": r["name"],
            "unique": not int(r["non_unique"] or 0),
            "primary": r["name"] == "PRIMARY",
            "columns": [],
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
        "schema": schema, "table": table,
        "indexes": index_list,
        "outbound": outbound,
        "inbound": inbound,
        "declared": bool(outbound or inbound),
        "candidates": _candidate_relations(schema, table, index_list),
    }


def _group_fk(rows: list[dict], ident, col_key: str, ref_key: str) -> list[dict]:
    """Collapse one-row-per-column FK listings into one entry per constraint,
    preserving column order so a composite key reads correctly."""
    grouped: dict[tuple, dict] = {}
    for r in rows:
        info = ident(r)
        gid = (r["name"],) + tuple(info.values())
        entry = grouped.setdefault(gid, {"constraint": r["name"], **info,
                                         "columns": [], "ref_columns": []})
        entry["columns"].append(r[col_key])
        entry["ref_columns"].append(r[ref_key])
    return list(grouped.values())


# Cap on how many tables a single candidate column will name. A column called
# `id` or `name` exists nearly everywhere, and listing 90 tables is noise that
# buries the columns where the match means something.
_CANDIDATE_TABLE_CAP = 25


def _candidate_relations(schema: str, table: str,
                         indexes: list[dict]) -> list[dict]:
    """Same-named columns in other tables of this schema, for the indexed
    columns of this one. An inference, never presented as a declaration.

    Restricted to columns that are part of an index here: an unindexed column
    sharing a name is far more likely to be a coincidence (`name`, `status`)
    than a join, and including them made the list useless on vision_ng.
    """
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
            (schema, table, *keyed), limit=5000)
    except MariaError:
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
    # Fewest matches first: a column in 2 other tables is a far stronger signal
    # about the data model than one in 60.
    out.sort(key=lambda c: c["count"])
    return out


def _primary_key(schema: str, table: str) -> list[str]:
    try:
        rows, _ = run(
            "SELECT column_name AS name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s AND column_key = 'PRI' "
            "ORDER BY ordinal_position",
            (schema, table), limit=64)
    except MariaError:
        return []
    return [r["name"] for r in rows]


def _blob_columns(schema: str, table: str) -> list[str]:
    """Binary columns, by declared type. What the UI offers a download for."""
    try:
        rows, _ = run(
            "SELECT column_name AS name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s "
            "  AND data_type IN ('blob','tinyblob','mediumblob','longblob','binary','varbinary') "
            "ORDER BY ordinal_position",
            (schema, table), limit=256)
    except MariaError:
        return []
    return [r["name"] for r in rows]


@router.get("/blob/preview")
def maria_blob_preview(schema: str = Query(...), table: str = Query(...),
                       column: str = Query(...), key: str = Query(...)):
    """The readable content of a binary column, without downloading it.

    Same validation as the download — this is the same data through a different
    lens, so it must not be an easier path to it.
    """
    data, err, status = _fetch_blob(schema, table, column, key)
    if err:
        return Response(content=json.dumps({"error": err}), status_code=status,
                        media_type="application/json")
    view = blobs.describe(data)
    view.update({"schema": schema, "table": table, "column": column})
    return view


@router.get("/blob")
def maria_blob(schema: str = Query(...), table: str = Query(...),
               column: str = Query(...), key: str = Query(...)):
    """Download one binary column value as a file.

    `key` is a JSON object of primary-key column -> value, which is how a
    single row is named. Everything in it is validated against
    information_schema before it is used: the column must really be a binary
    column of that table, and every key name must really be part of its primary
    key. Identifiers cannot be parameter-bound, so validating them against the
    server's own catalog — rather than escaping them — is what makes this safe.
    Values ARE bound.
    """
    data, err, status = _fetch_blob(schema, table, column, key)
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


def _fetch_blob(schema: str, table: str, column: str,
                key: str) -> tuple[bytes, str, int]:
    """Validate the request and return the raw bytes: (data, error, status).

    Shared by the download and the preview deliberately — two entry points to
    the same bytes must not be two sets of checks, or the weaker one becomes
    the way in. Identifiers are validated against information_schema (they
    cannot be parameter-bound); values are bound.
    """
    try:
        wanted = json.loads(key)
        if not isinstance(wanted, dict) or not wanted:
            raise ValueError
    except (ValueError, TypeError):
        return b"", ("key must be a JSON object of primary-key column to value"), 400

    if column not in _blob_columns(schema, table):
        return b"", f"{column!r} is not a binary column of {schema}.{table}", 400

    pk = _primary_key(schema, table)
    if not pk:
        return b"", (f"{schema}.{table} has no primary key, so a single row "
                     f"cannot be addressed"), 400
    if set(wanted) != set(pk):
        return b"", f"key must name exactly the primary key ({', '.join(pk)})", 400

    ident = f"`{schema.replace('`', '')}`.`{table.replace('`', '')}`"
    col_ident = f"`{column.replace('`', '')}`"
    where = " AND ".join(f"`{c}` = %s" for c in pk)
    params = tuple(wanted[c] for c in pk)

    try:
        rows = run_raw(f"SELECT {col_ident} AS blob_value FROM {ident} WHERE {where}",
                       params, schema=schema)
    except MariaError as exc:
        return b"", str(exc), 400

    if not rows:
        return b"", "no row matches that key", 404
    if len(rows) > 1:
        # Should be impossible against a real primary key, so it means the
        # catalog and the data disagree — say so rather than serving one at
        # random and letting the engineer draw a conclusion from it.
        return b"", "that key matched more than one row", 409

    value = rows[0].get("blob_value")
    if value is None:
        return b"", "this row's value is NULL", 404
    return (bytes(value) if not isinstance(value, bytes) else value), "", 200


def _table_exists(schema: str, table: str) -> tuple[bool, str]:
    try:
        rows, _ = run(
            "SELECT 1 AS ok FROM information_schema.tables "
            "WHERE table_schema = %s AND table_name = %s",
            (schema, table), limit=1)
    except MariaError as exc:
        return False, str(exc)
    return bool(rows), ""
