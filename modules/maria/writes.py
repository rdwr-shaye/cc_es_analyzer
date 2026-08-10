"""Changing one cell of one row in the CC's MariaDB.

This is the only code in the MariaDB module that writes, and it is reached only
when the `maria.write` capability is unlocked — otherwise its router is never
registered and the endpoint does not exist. See modules/maria/__init__.py.

The shape is deliberately narrow: ONE column, of ONE row, addressed by its
FULL primary key. Not a WHERE the user composes, not a statement they write,
not several rows at once. A cell edit on a live appliance is a scalpel, and
everything that would make it a broader instrument has been left out — the raw
SQL endpoint next door stays read-only precisely so there is no second, wider
path to the same effect.

Five things are refused rather than attempted, each because getting it wrong on
a customer's CC is unrecoverable:

  * columns that are part of the primary key — changing one is not editing a
    row, it is replacing its identity, and the WHERE clause here addresses rows
    BY that identity;
  * binary columns — a BLOB in the CC is typically a serialised Java object,
    and a hand-typed replacement produces a payload the JVM will fail to
    deserialise at some unrelated later moment. Nothing about that failure
    would point back here;
  * generated, virtual and auto_increment columns — the server owns them;
  * a key that does not name exactly the primary key, or that matches zero or
    more than one row;
  * a row whose current value is not what the caller last saw. That last check
    is what makes this safe to use against a system with live traffic: between
    reading a row and editing it, the CC itself may have changed it, and
    overwriting that silently is how a debugging session causes an outage.

What is NOT here yet, and matters: identity and a durable audit trail. Phase 1
of the roadmap brings both, and the audit line written below is a log record,
not an audit record — it is attributable to a browser session and an IP, which
is what this app currently knows, and it is not tamper-evident. Until that
lands this capability should stay off anywhere its use would need to be proven
after the fact.
"""

from __future__ import annotations

import json
import logging

import pymysql

from modules.maria.client import MariaError, bit_value, connection, resolve_host

logger = logging.getLogger(__name__)

# Written at INFO on every attempt, successful or not. A separate logger name so
# an operator can route just this to its own file without the rest of the app's
# chatter, and so it is obvious in a log dump what these lines are.
audit = logging.getLogger("cc_admin.audit.maria")

# Column types we refuse to accept a typed replacement for.
_BINARY_TYPES = {"blob", "tinyblob", "mediumblob", "longblob",
                 "binary", "varbinary"}


class WriteRefused(Exception):
    """The edit is not one this endpoint will perform. The message is shown to
    the user verbatim, so it says what was refused and why."""


def _column_meta(schema: str, table: str, column: str) -> dict:
    """The server's own description of the column, or raise. Read through a
    READ ONLY connection — the checks must not run on the write connection,
    where a mistake would already be inside the writing transaction."""
    from modules.maria.client import run
    try:
        rows, _ = run(
            "SELECT column_name AS name, data_type, column_type, is_nullable, "
            "       column_key, extra, generation_expression "
            "FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s AND column_name = %s",
            (schema, table, column), limit=1)
    except MariaError as exc:
        raise WriteRefused(str(exc)) from exc
    if not rows:
        raise WriteRefused(f"{column!r} is not a column of {schema}.{table}")
    return rows[0]


def _primary_key(schema: str, table: str) -> list[str]:
    from modules.maria.client import run
    try:
        rows, _ = run(
            "SELECT column_name AS name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s AND column_key = 'PRI' "
            "ORDER BY ordinal_position",
            (schema, table), limit=64)
    except MariaError as exc:
        raise WriteRefused(str(exc)) from exc
    return [r["name"] for r in rows]


def check(schema: str, table: str, column: str, key: dict) -> dict:
    """Validate everything about the target without touching it.

    Split out from update_cell() so the UI can ask "could I edit this?" before
    offering an editable cell, and get the same answer, from the same code, as
    the write itself would give. Two sets of rules would drift, and the weaker
    one would become the way in.
    """
    meta = _column_meta(schema, table, column)

    if (meta["data_type"] or "").lower() in _BINARY_TYPES:
        raise WriteRefused(
            f"{column} is a binary column. These hold serialised Java objects "
            f"on a CC, and a typed replacement would not deserialise — the "
            f"failure would surface later, somewhere unrelated.")

    extra = (meta["extra"] or "").lower()
    if "auto_increment" in extra:
        raise WriteRefused(f"{column} is AUTO_INCREMENT — the server owns it.")
    if "generated" in extra or (meta["generation_expression"] or ""):
        raise WriteRefused(f"{column} is a generated column — it is computed "
                           f"from other columns, so edit those instead.")

    pk = _primary_key(schema, table)
    if not pk:
        raise WriteRefused(
            f"{schema}.{table} has no primary key, so a single row cannot be "
            f"addressed — there is no way to guarantee an edit hits one row.")
    if column in pk:
        raise WriteRefused(
            f"{column} is part of the primary key. Changing it re-identifies "
            f"the row rather than editing it, and other tables may reference "
            f"the old value.")
    if set(key) != set(pk):
        raise WriteRefused(f"key must name exactly the primary key "
                           f"({', '.join(pk)})")

    return {"column": meta, "primary_key": pk}


def update_cell(schema: str, table: str, column: str, key: dict,
                value, expected, *, who: str = "") -> dict:
    """Set one column of one row. Returns {before, after, rows}.

    `expected` is the value the caller last saw. The row is re-read inside the
    writing transaction and compared before anything changes, so a row the CC
    modified in the meantime is reported rather than overwritten.
    """
    info = check(schema, table, column, key)
    pk = info["primary_key"]
    is_bit = (info["column"]["data_type"] or "").lower() == "bit"

    if is_bit:
        # BIT must be bound as a NUMBER. Bound as the string "1", MySQL reads it
        # as a binary string of eight bits and rejects it on a bit(1) as out of
        # range — so the obvious pass-through would fail on exactly the columns
        # this branch exists for.
        try:
            value = int(str(value).strip())
        except (TypeError, ValueError):
            raise WriteRefused(
                f"{column} is a {info['column']['column_type']} column — it "
                f"takes a number (0 or 1 for bit(1)), not {value!r}.") from None
        width = info["column"]["column_type"]
        if width.lower().startswith("bit(") and value.bit_length() > _bit_width(width):
            raise WriteRefused(f"{value} does not fit in {width}.")

    # Identifiers cannot be parameter-bound. Every one used below has been
    # matched against information_schema above — that is what makes it safe,
    # not the backtick stripping, which only stops a name from ending the quote.
    ident = f"`{schema.replace('`', '')}`.`{table.replace('`', '')}`"
    col_ident = f"`{column.replace('`', '')}`"
    where = " AND ".join(f"`{c.replace('`', '')}` = %s" for c in pk)
    key_params = tuple(key[c] for c in pk)

    cc_host, _ = resolve_host()
    target = f"{cc_host} {schema}.{table}.{column} {json.dumps(key, default=str)}"

    with connection(schema, readonly=False) as conn:
        try:
            with conn.cursor() as cur:
                # FOR UPDATE: hold the row for the life of the transaction so
                # the value cannot change between the check and the write.
                cur.execute(
                    f"SELECT {col_ident} AS v FROM {ident} WHERE {where} "
                    f"LIMIT 2 FOR UPDATE", key_params)
                rows = cur.fetchall()

                if not rows:
                    raise WriteRefused("no row matches that key")
                if len(rows) > 1:
                    # Impossible against a real primary key, so the catalog and
                    # the data disagree. Do not pick one.
                    raise WriteRefused("that key matched more than one row")

                before = rows[0]["v"]
                # The driver returns BIT as bytes; the browser sent back the
                # integer it was shown. Compared untreated, b"\x01" != "1" and
                # every edit to a flag column would look like someone else had
                # changed it underneath.
                if is_bit:
                    before = bit_value(before)
                if not _same(before, expected):
                    raise WriteRefused(
                        f"this row changed since you loaded it — it now holds "
                        f"{_show(before)}, not {_show(expected)}. Refresh and "
                        f"look again before editing.")

                cur.execute(
                    f"UPDATE {ident} SET {col_ident} = %s WHERE {where} LIMIT 1",
                    (value, *key_params))
                affected = cur.rowcount

            conn.commit()
        except WriteRefused:
            conn.rollback()
            audit.info("[maria-write] REFUSED %s by %s", target, who or "unknown")
            raise
        except pymysql.Error as exc:
            conn.rollback()
            audit.info("[maria-write] FAILED %s by %s — %s", target,
                       who or "unknown", exc)
            # The server's own message is the useful part: a CHECK constraint,
            # a foreign key, a value too long for the column.
            raise MariaError(str(exc)) from exc

    audit.info("[maria-write] OK %s by %s — %s -> %s", target, who or "unknown",
               _show(before), _show(value))
    return {"ok": True, "before": _jsonable_scalar(before),
            "after": value, "rows": affected}


def _bit_width(column_type: str) -> int:
    """The N in bit(N). Falls back to 64 (MySQL's maximum) if it cannot be
    read, so an unparsed type never rejects a value that would have fit."""
    try:
        return int(column_type.strip().lower().removeprefix("bit(").rstrip(")"))
    except ValueError:
        return 64


def _same(a, b) -> bool:
    """Whether the stored value matches what the caller thinks it is.

    Compared as text on purpose. The value came back to the browser as JSON and
    returns as a string, so a DECIMAL, a DATETIME and an int all arrive in a
    different type than they left in; comparing types here would make every
    edit look like a conflict.
    """
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
