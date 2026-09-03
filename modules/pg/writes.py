"""Changing one cell of one row in the CC's PostgreSQL.

Same shape, same restraint, and the same reasoning as modules/maria/writes.py
— read that file's docstring for why this is deliberately narrow: ONE column
of ONE row, addressed by its FULL primary key, and why each refusal below
exists. This is the only code in the PostgreSQL module that writes, and it is
reached only when `pg.write` is unlocked — otherwise its router is never
registered and the endpoint does not exist.

What is refused, mapped from MariaDB's five to PostgreSQL's equivalents:

  * columns that are part of the primary key — identical reasoning;
  * `bytea` columns — PostgreSQL's binary type, holding the same kind of
    Java-serialised payloads observed in modules/maria's BLOBs (verified on a
    live CC: dfc.protection_pulse.protection_message is bytea and IS one);
  * columns the server generates for itself — PostgreSQL spells this two ways
    where MariaDB has one (AUTO_INCREMENT): GENERATED ... AS IDENTITY
    (`is_identity`) and the older serial/sequence convention, a plain column
    whose default is `nextval(...)` — both observed on this CC
    (dp_udf_policy_map.id, protected_object.id) and both server-owned for the
    same reason AUTO_INCREMENT is: a value the caller supplied would be
    overtaken by the sequence and quietly diverge from it;
  * STORED generated columns (`is_generated = 'ALWAYS'`) — computed from other
    columns, same as MariaDB's GENERATED;
  * a key that does not name exactly the primary key, or that matches zero or
    more than one row;
  * a row whose current value is not what the caller last saw — the
    concurrency check that makes this safe against a system with live traffic.

What is NOT here yet, and matters: identity and a durable audit trail. See
modules/maria/writes.py's docstring — the same Phase 1 gap applies here
unchanged, and until it lands this capability should stay off anywhere its
use would need to be proven after the fact.
"""

from __future__ import annotations

import json
import logging

import pg8000.exceptions

from modules.pg.client import PgError, _driver_message, connection

logger = logging.getLogger(__name__)

# Written at INFO on every attempt, successful or not — a separate logger name
# so an operator can route just this to its own file, matching
# modules/maria/writes.py's `cc_admin.audit.maria`.
audit = logging.getLogger("cc_admin.audit.pg")


class WriteRefused(Exception):
    """The edit is not one this endpoint will perform. The message is shown to
    the user verbatim, so it says what was refused and why."""


def _column_meta(database: str, table: str, column: str) -> dict:
    """The server's own description of the column, or raise. Read through a
    READ ONLY connection — the checks must not run on the write connection,
    where a mistake would already be inside the writing transaction."""
    from modules.pg.client import run
    try:
        rows, _ = run(
            "SELECT column_name AS name, data_type, is_identity, "
            "       identity_generation, is_generated, generation_expression, "
            "       column_default "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s AND column_name = %s",
            (table, column), database=database, limit=1)
    except PgError as exc:
        raise WriteRefused(str(exc)) from exc
    if not rows:
        raise WriteRefused(f"{column!r} is not a column of {database}.{table}")
    return rows[0]


def _primary_key(database: str, table: str) -> list[str]:
    from modules.pg.client import run
    try:
        rows, _ = run(
            "SELECT kcu.column_name AS name "
            "FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu "
            "  ON kcu.constraint_name = tc.constraint_name AND kcu.table_schema = tc.table_schema "
            "WHERE tc.table_schema = 'public' AND tc.table_name = %s "
            "  AND tc.constraint_type = 'PRIMARY KEY' "
            "ORDER BY kcu.ordinal_position",
            (table,), database=database, limit=64)
    except PgError as exc:
        raise WriteRefused(str(exc)) from exc
    return [r["name"] for r in rows]


def check(database: str, table: str, column: str, key: dict) -> dict:
    """Validate everything about the target without touching it. Same split
    as modules/maria/writes.py::check, and for the same reason: the UI asks
    "could I edit this?" through this exact function, so its answer and the
    write's own refusal can never drift apart."""
    meta = _column_meta(database, table, column)

    if (meta["data_type"] or "").lower() == "bytea":
        raise WriteRefused(
            f"{column} is bytea. On this CC these hold serialised Java "
            f"objects (verified on protection_pulse.protection_message), and "
            f"a typed replacement would not deserialise — the failure would "
            f"surface later, somewhere unrelated.")

    default = (meta["column_default"] or "")
    if (meta["is_identity"] or "").upper() == "YES":
        raise WriteRefused(f"{column} is a GENERATED ... AS IDENTITY column — "
                           f"the server owns it.")
    if default.startswith("nextval("):
        raise WriteRefused(f"{column} defaults from a sequence ({default}) — "
                           f"the server owns it, the same as an identity "
                           f"column.")
    if (meta["is_generated"] or "").upper() == "ALWAYS" or meta["generation_expression"]:
        raise WriteRefused(f"{column} is a generated column — it is computed "
                           f"from other columns, so edit those instead.")

    pk = _primary_key(database, table)
    if not pk:
        raise WriteRefused(
            f"{database}.{table} has no primary key, so a single row cannot "
            f"be addressed — there is no way to guarantee an edit hits one row.")
    if column in pk:
        raise WriteRefused(
            f"{column} is part of the primary key. Changing it re-identifies "
            f"the row rather than editing it, and other tables may reference "
            f"the old value.")
    if set(key) != set(pk):
        raise WriteRefused(f"key must name exactly the primary key "
                           f"({', '.join(pk)})")

    return {"column": meta, "primary_key": pk}


def _ident(name: str) -> str:
    return f'"{name.replace(chr(34), chr(34) * 2)}"'


def update_cell(database: str, table: str, column: str, key: dict,
                value, expected, *, who: str = "") -> dict:
    """Set one column of one row. Returns {before, after, rows}.

    `expected` is the value the caller last saw. The row is re-read inside the
    writing transaction — held with FOR UPDATE, PostgreSQL's row lock, so it
    cannot change between the check and the write — and compared before
    anything changes, so a row the CC modified in the meantime is reported
    rather than overwritten.
    """
    info = check(database, table, column, key)
    pk = info["primary_key"]

    ident = f"{_ident('public')}.{_ident(table)}"
    col_ident = _ident(column)
    where = " AND ".join(f"{_ident(c)} = %s" for c in pk)
    key_params = tuple(key[c] for c in pk)

    from modules.pg.client import resolve_host
    cc_host, _ = resolve_host()
    target = f"{cc_host} {database}.{table}.{column} {json.dumps(key, default=str)}"

    with connection(database, readonly=False) as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {col_ident} FROM {ident} WHERE {where} FOR UPDATE",
                    key_params)
                rows = cur.fetchmany(2)

                if not rows:
                    raise WriteRefused("no row matches that key")
                if len(rows) > 1:
                    # Impossible against a real primary key, so the catalog and
                    # the data disagree. Do not pick one.
                    raise WriteRefused("that key matched more than one row")

                before = rows[0][0]
                if not _same(before, expected):
                    raise WriteRefused(
                        f"this row changed since you loaded it — it now holds "
                        f"{_show(before)}, not {_show(expected)}. Refresh and "
                        f"look again before editing.")

                # No LIMIT on UPDATE in PostgreSQL — unlike MariaDB, there is no
                # syntax for it, and none is needed: the WHERE names the full
                # primary key, which is unique by definition, so at most one
                # row can ever match.
                cur.execute(
                    f"UPDATE {ident} SET {col_ident} = %s WHERE {where}",
                    (value, *key_params))
                affected = cur.rowcount

            conn.commit()
        except WriteRefused:
            conn.rollback()
            audit.info("[pg-write] REFUSED %s by %s", target, who or "unknown")
            raise
        except pg8000.exceptions.Error as exc:
            conn.rollback()
            msg = _driver_message(exc)
            audit.info("[pg-write] FAILED %s by %s — %s", target,
                       who or "unknown", msg)
            raise PgError(msg) from exc

    audit.info("[pg-write] OK %s by %s — %s -> %s", target, who or "unknown",
               _show(before), _show(value))
    return {"ok": True, "before": _jsonable_scalar(before),
            "after": value, "rows": affected}


def _same(a, b) -> bool:
    """Whether the stored value matches what the caller thinks it is.
    Compared as text — identical reasoning to modules/maria/writes.py::_same."""
    if a is None or b is None:
        return a is None and b is None
    return str(a) == str(b)


def _show(v) -> str:
    if v is None:
        return "NULL"
    s = str(v)
    return repr(s if len(s) <= 60 else s[:60] + "…")


def _jsonable_scalar(v):
    if isinstance(v, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(v))} bytes>"
    if v is None or isinstance(v, (int, float, bool, str)):
        return v
    return str(v)
