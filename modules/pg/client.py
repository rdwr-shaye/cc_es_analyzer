"""PostgreSQL access for CC Admin.

Read-only by construction, mirroring modules/maria/client.py layer for layer:

  1. every connection opens an explicit READ ONLY transaction, so the server
     itself rejects a write even if a statement slips past us;
  2. statements are checked against a strict allowlist of leading keywords
     before they are sent;
  3. every statement carries a timeout and every result a row cap, because on
     a customer's production CC a careless query is an outage, not a typo.

Layer 1 is the one that matters in a security review, and it is done
differently here than in MariaDB on purpose. `SET SESSION CHARACTERISTICS AS
TRANSACTION READ ONLY` looks like the direct equivalent of MariaDB's `SET
SESSION TRANSACTION READ ONLY`, but it is NOT reliable through this driver:
pg8000 opens an implicit transaction on the first statement it sends (its
`autocommit` defaults to False), so by the time that SET would apply to "the
next transaction", one is already open and the SET's effect never arrives.
Verified against a live CC. `BEGIN READ ONLY` sent as literally the first
statement on a fresh connection does not have that race — it both opens the
transaction and marks it read-only in one statement — and was confirmed on
the same box to refuse a real UPDATE with PostgreSQL's own
`cannot execute UPDATE in a read-only transaction` (SQLSTATE 25006).

A PostgreSQL connection is scoped to exactly one DATABASE, unlike MariaDB
where one connection sees every schema on the server — there is no `USE`.
That is why every function here takes `database` as a required argument
rather than an optional default schema, and why listing the databases
themselves (modules/pg/routers/browse.py::pg_databases) has to connect to one
first (settings.pg_default_database) to ask the server what else exists.

Connections are made per request rather than pooled, for the same reason as
MariaDB: SQL browsing here is a handful of deliberate queries, not a
high-frequency path, so a pool would add shared mutable state to buy nothing
measurable.
"""

from __future__ import annotations

import logging
import os
import re
from contextlib import contextmanager
from datetime import date, datetime, time as time_, timedelta
from decimal import Decimal

import pg8000
import pg8000.exceptions

from config import settings
from core import policy
from core.remote import ssh_tunnel
from modules.pg import credentials

logger = logging.getLogger(__name__)


class PgError(Exception):
    """Anything that stopped a statement running. Carries a message meant for
    the operator — the driver's own text is usually the most useful thing we
    can say, so it is passed through rather than replaced with a generic one."""


def _driver_message(exc: Exception) -> str:
    """The server's own error text, not pg8000's wrapper around it.

    pg8000 raises with `args[0]` set to the raw PostgreSQL error response — a
    dict of single-letter fields ('S' severity, 'C' SQLSTATE, 'M' message,
    'P' character position, ...) — so `str(exc)` on an unfiltered exception
    prints that whole dict, e.g. `{'S': 'ERROR', 'V': 'ERROR', 'C': '42601',
    'M': 'trailing junk after numeric literal...', 'P': '49', ...}`. Verified
    against a live CC. 'M' is the message a human asked for; the rest is
    useful to PostgreSQL's own docs, not to an engineer reading a toast.
    """
    if exc.args and isinstance(exc.args[0], dict) and "M" in exc.args[0]:
        return exc.args[0]["M"]
    return str(exc)


# Statements this module will send. SELECT/WITH/EXPLAIN/TABLE/VALUES are the
# browsing verbs; SHOW reads a server setting. DESCRIBE/DESC are deliberately
# absent — unlike MariaDB, PostgreSQL has no such statement, only a psql
# meta-command, so accepting the words here would accept SQL that fails on the
# server rather than SQL that writes. Everything else — including SET, USE,
# and any DDL or DML — is refused, so nothing can change server or session
# state either.
_ALLOWED_LEADING = ("select", "with", "explain", "show", "table", "values")

# Strip /* */ and -- comments before inspecting the leading keyword, so
# `/*harmless*/ DELETE ...` cannot present itself as a comment.
_COMMENTS = re.compile(r"/\*.*?\*/|--[^\n]*", re.S)

# A leading SELECT is not by itself proof of a read. These forms start with an
# allowed keyword and still write, lock, or reach outside the database:
#
#   SELECT ... INTO table   CREATES A TABLE from the result — PostgreSQL's own
#                            write hazard hiding behind a SELECT, with no
#                            MariaDB equivalent (MariaDB's INTO OUTFILE writes
#                            a FILE, not a table, which is the pattern this
#                            regex is modelled on).
#   FOR UPDATE / FOR SHARE / FOR NO KEY UPDATE / FOR KEY SHARE
#                            takes row locks on a live production table, which
#                            is an outage risk even though no data changes.
#   COPY                     reads OR writes a file on the database server
#                            depending on direction, and — sent as a top-level
#                            statement — is not something a leading-keyword
#                            check alone can allow safely in one direction.
#   pg_read_file / pg_read_binary_file / pg_ls_dir
#                            read arbitrary files or directories on the
#                            database server — PostgreSQL's LOAD_FILE().
#   lo_import / lo_export    PostgreSQL's large-object import/export, which
#                            read or write a file on the server by name.
#   dblink                   the cross-database-query extension; out of scope
#                            for a read confined to what THIS check validated.
#
# Each is refused by name. This is a denylist on top of the allowlist, and a
# denylist is never complete — which is exactly why the read-only transaction
# in connection() stays as the layer that does not depend on enumeration.
_FORBIDDEN = (
    (re.compile(r"\binto\b", re.I),
     "SELECT ... INTO creates a table on the database server"),
    (re.compile(r"\bfor\s+(update|share|no\s+key\s+update|key\s+share)\b", re.I),
     "FOR UPDATE/SHARE takes row locks on a live table"),
    (re.compile(r"\bcopy\b", re.I),
     "COPY reads or writes a file on the database server"),
    (re.compile(r"\bpg_read_(file|binary_file)\s*\(", re.I),
     "pg_read_file() reads files from the database server"),
    (re.compile(r"\bpg_ls_dir\s*\(", re.I),
     "pg_ls_dir() lists directories on the database server"),
    (re.compile(r"\blo_(import|export)\s*\(", re.I),
     "lo_import/lo_export read or write a file on the database server"),
    (re.compile(r"\bdblink\w*\s*\(", re.I),
     "dblink reaches outside the database this check validated"),
)


def _strip(sql: str) -> str:
    return _COMMENTS.sub(" ", sql).strip()


def check_readonly(sql: str) -> str:
    """Return the statement, or raise if it is not one we are willing to send.

    Rejects multiple statements outright. pg8000 does not run a semicolon-
    joined batch as several implicit statements the way some clients do, but
    relying on a driver default for a security property is the kind of thing
    that quietly stops being true after a dependency bump.
    """
    stripped = _strip(sql)
    if not stripped:
        raise PgError("empty statement")

    body = stripped.rstrip(";")
    if ";" in body:
        raise PgError("only one statement at a time")

    leading = body.split(None, 1)[0].lower() if body.split() else ""
    if leading not in _ALLOWED_LEADING:
        raise PgError(
            f"{leading.upper() or 'that'} is not allowed here — this connection "
            f"is read-only ({', '.join(k.upper() for k in _ALLOWED_LEADING)})")

    for pattern, why in _FORBIDDEN:
        if pattern.search(body):
            raise PgError(f"not allowed here — {why}")
    return body


def resolve_host() -> tuple[str, str]:
    """Which machine to reach PostgreSQL on: (host, why).

    Identical reasoning to modules/maria/client.py::resolve_host, which that
    file documents at length: the default host is a docker container name,
    right embedded and meaningless standalone, so standalone follows whichever
    CC the current Elasticsearch session is pointed at instead of being a
    second thing to configure and keep in sync.
    """
    explicit = os.environ.get("PG_HOST", "").strip()
    if explicit:
        return explicit, "PG_HOST"

    if policy.profile() == policy.EMBEDDED:
        return settings.pg_host, "co-located on this CC"

    from modules.es.client import get_client
    host = (getattr(get_client(), "cc_host", "") or "").strip()
    if not host or host in ("localhost", "127.0.0.1"):
        return "", "no CC connected"
    return host, "the connected CC"


# How each CC turned out to be reachable, so the decision is made once rather
# than re-probing a closed port on every query. Cleared when a tunnel dies.
_route_cache: dict[str, tuple[str, int]] = {}

_TUNNEL = "pg"


def _direct_ok(host: str, port: int, timeout: float = 3.0) -> bool:
    import socket
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


def _route(host: str) -> tuple[str, int, str]:
    """Where to actually point pg8000 for this CC: (host, port, how).

    Same fallback as modules/maria/client.py::_route: try the port directly,
    and otherwise forward it through the SSH connection the user already
    authenticated for Elasticsearch, rather than asking for the same
    credentials twice.
    """
    port = settings.pg_port
    cached = _route_cache.get(host)
    if cached:
        active = ssh_tunnel.active_tunnel(_TUNNEL)
        # A dead tunnel still accepts connections locally and then hangs, so
        # confirm it is alive — and, since the "pg" tunnel is ONE named slot
        # shared across whichever CC is currently connected (not one per
        # host), confirm it is STILL pointed at the CC this cache entry was
        # recorded for. Switching from CC A to CC B and back left A's entry
        # pointing at a local port that now forwards to B instead — verified
        # live: reconnecting to a previously-visited CC got a timeout on
        # 127.0.0.1, because the cached port was answering for a different
        # appliance's tunnel by then. Same bug modules/maria/client.py had.
        if cached[0] != "127.0.0.1" or (active is not None and active.meta.get("ssh_host") == host):
            return cached[0], cached[1], ("direct" if cached[0] == host else "SSH tunnel")
        _route_cache.pop(host, None)

    if _direct_ok(host, port):
        _route_cache[host] = (host, port)
        return host, port, "direct"

    from modules.es.client import get_client
    ssh = getattr(get_client(), "ssh", None)
    if not ssh or not ssh.get("user"):
        return host, port, "direct"      # nothing else to try; let it fail honestly

    tun = ssh_tunnel.start_tunnel(
        ssh_host=host, ssh_user=ssh["user"], ssh_password=ssh.get("password", ""),
        ssh_port=int(ssh.get("port") or 22),
        remote_host="127.0.0.1", remote_port=port, name=_TUNNEL,
    )
    if not tun.get("ok"):
        logger.warning("[pg] SSH tunnel to %s failed: %s", host, tun.get("error"))
        return host, port, "direct"
    _route_cache[host] = (tun["local_host"], tun["local_port"])
    return tun["local_host"], tun["local_port"], "SSH tunnel"


@contextmanager
def connection(database: str, readonly: bool = True):
    """A connection to one PostgreSQL database on the CC, read-only unless
    asked otherwise.

    `database` is not optional the way MariaDB's `schema` is: PostgreSQL has
    no `USE`, so which database to reach is not a default but the whole
    question of where this connection goes.

    `readonly=False` exists for exactly one caller — modules/pg/writes.py,
    reached only when the `pg.write` capability is unlocked, whose route is
    not registered otherwise.
    """
    if not database:
        raise PgError("no database given — PostgreSQL has no USE statement, "
                      "so a database must be named up front")

    creds = credentials.resolve()
    cc_host, why = resolve_host()
    if not cc_host:
        raise PgError(
            "no CC is connected — PostgreSQL is reached on the same machine as "
            "the Elasticsearch this session is pointed at, so connect to a CC "
            "first (or set PG_HOST to reach one directly)")

    if policy.profile() == policy.EMBEDDED:
        host, port, via = cc_host, settings.pg_port, "direct"
    else:
        host, port, via = _route(cc_host)

    try:
        conn = pg8000.connect(
            host=host,
            port=port,
            user=creds["user"],
            password=creds["password"],
            database=database,
            timeout=settings.pg_timeout_s,
        )
    except (pg8000.exceptions.Error, OSError) as exc:
        raise PgError(f"cannot reach PostgreSQL on {cc_host}:{settings.pg_port} "
                      f"({why}, via {via}) — {_driver_message(exc)}") from exc

    try:
        with conn.cursor() as cur:
            # Literally the first statement on this connection — see the
            # module docstring for why that ordering is load-bearing and a
            # session-level SET afterwards is not.
            cur.execute("BEGIN READ ONLY" if readonly else "BEGIN")
            cur.execute(f"SET statement_timeout = {int(settings.pg_timeout_s * 1000)}")
        yield conn
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


# Marker put in place of a binary column value, matching modules/maria/client.py
# so the frontend's existing blob-column handling needs no PostgreSQL-specific
# branch. bytea already arrives from pg8000 as `bytes` — no unwrapping needed,
# unlike MariaDB's BIT type.
BLOB_KEY = "__blob__"


def _jsonable(value):
    """Make one column value safe to serialise, without pretending binary is
    text. Mirrors modules/maria/client.py::_jsonable; the type set differs
    because pg8000 hands back native Python types more often than PyMySQL
    does (UUID and JSON columns already arrive as str, for instance)."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return {BLOB_KEY: True, "bytes": len(raw)}
    if isinstance(value, Decimal):
        # str, not float: NUMERIC is exact and float would silently round it.
        return str(value)
    if isinstance(value, (datetime, date, time_)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, list):
        # PostgreSQL arrays. Recurse so an array of bytea or numeric is still
        # honestly represented rather than handed to the JSON encoder raw.
        return [_jsonable(v) for v in value]
    return value


def run(sql: str, params: tuple = (), database: str = "",
        limit: int | None = None) -> tuple[list[dict], bool]:
    """Run one read-only statement against one database. Returns (rows, truncated).

    `truncated` is the honest half of the row cap: a silently shortened result
    set is indistinguishable from a complete one, which is how someone
    concludes a table is empty when it is merely capped.
    """
    body = check_readonly(sql)
    cap = settings.pg_max_rows if limit is None else max(1, int(limit))

    with connection(database) as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(body, params)
                columns = [d[0] for d in (cur.description or ())]
                rows = cur.fetchmany(cap + 1)
            except pg8000.exceptions.Error as exc:
                raise PgError(_driver_message(exc)) from exc

    truncated = len(rows) > cap
    clean = [{col: _jsonable(v) for col, v in zip(columns, row)}
             for row in rows[:cap]]
    return clean, truncated


def run_raw(sql: str, params: tuple = (), database: str = "") -> list[tuple]:
    """Like run(), but WITHOUT the JSON sanitising and as plain tuples — the
    caller wants the real bytes. Only for the blob download path, which
    streams them rather than serialising them."""
    body = check_readonly(sql)
    with connection(database) as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(body, params)
                return list(cur.fetchmany(2))     # 2, so an ambiguous key shows up
            except pg8000.exceptions.Error as exc:
                raise PgError(_driver_message(exc)) from exc


def server_info() -> dict:
    """Version and connection identity — PostgreSQL's node identity for the
    sidebar. Connects to the default maintenance database: this is asked
    before any CC database has necessarily been chosen."""
    rows, _ = run("SELECT version() AS version",
                  database=settings.pg_default_database)
    version = (rows[0].get("version") if rows else "") or ""
    creds = credentials.resolve()
    host, why = resolve_host()
    routed = _route_cache.get(host)
    via = "direct" if (not routed or routed[0] == host) else "SSH tunnel"
    return {"connected": True, "version": version,
            "host": host, "host_source": why, "via": via,
            "port": settings.pg_port,
            "user": creds["user"], "credential_source": creds["source"]}
