"""MariaDB access for CC Admin.

Read-only by construction, not by convention. Three things enforce that, and
they are deliberately layered so no single mistake removes the guarantee:

  1. every connection opens a READ ONLY transaction, so the server itself
     rejects a write even if a statement slips past us;
  2. statements are checked against a strict allowlist of leading keywords
     before they are sent;
  3. every statement carries a timeout and every result a row cap, because on
     a customer's production CC a careless query is an outage, not a typo.

Layer 1 is the one that matters in a security review: it does not depend on our
parser being clever, and the answer to "what if you missed a syntax" is that the
transaction is read-only regardless.

Connections are made per request rather than pooled. The ES client pools because
the UI fires many small parallel searches per screen; SQL browsing here is a
handful of deliberate queries, so a pool would add shared mutable state and
transaction-scope bugs to buy nothing measurable.
"""

from __future__ import annotations

import logging
import os
import re
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal

import pymysql
from pymysql.constants import FIELD_TYPE
from pymysql.cursors import DictCursor

from config import settings
from core import policy
from core.remote import ssh_tunnel
from modules.maria import credentials

logger = logging.getLogger(__name__)


class MariaError(Exception):
    """Anything that stopped a statement running. Carries a message meant for
    the operator — the driver's own text is usually the most useful thing we
    can say, so it is passed through rather than replaced with a generic one."""


# Statements this module will send. SELECT/SHOW/DESCRIBE/EXPLAIN are the
# browsing verbs; WITH is here because a read-only CTE is an ordinary way to
# write a diagnostic query. Everything else — including SET, USE and any DDL or
# DML — is refused, so nothing can change server or session state either.
_ALLOWED_LEADING = ("select", "show", "describe", "desc", "explain", "with")

# Strip /* */ and -- comments before inspecting the leading keyword, so
# `/*harmless*/ DELETE ...` cannot present itself as a comment.
_COMMENTS = re.compile(r"/\*.*?\*/|--[^\n]*|#[^\n]*", re.S)

# A leading SELECT is not by itself proof of a read. These forms start with an
# allowed keyword and still write, lock or reach outside the database:
#
#   INTO OUTFILE / DUMPFILE  writes a FILE on the database server. The READ ONLY
#                            transaction does not stop it — it is not a table
#                            write — so nothing else in this module would.
#   FOR UPDATE / LOCK IN     takes write locks on rows of a live production
#   SHARE MODE               table, which is an outage risk even though no data
#                            changes.
#   LOAD_FILE()              reads an arbitrary server file into the result set.
#
# Each is refused by name. This is a denylist on top of the allowlist, and a
# denylist is never complete — which is exactly why the read-only transaction
# in connection() stays as the layer that does not depend on enumeration.
_FORBIDDEN = (
    (re.compile(r"\binto\s+(outfile|dumpfile)\b", re.I),
     "INTO OUTFILE/DUMPFILE writes a file on the database server"),
    (re.compile(r"\bfor\s+update\b", re.I),
     "FOR UPDATE takes write locks on a live table"),
    (re.compile(r"\block\s+in\s+share\s+mode\b", re.I),
     "LOCK IN SHARE MODE takes locks on a live table"),
    (re.compile(r"\bload_file\s*\(", re.I),
     "LOAD_FILE() reads files from the database server"),
)


def _strip(sql: str) -> str:
    return _COMMENTS.sub(" ", sql).strip()


def check_readonly(sql: str) -> str:
    """Return the statement, or raise if it is not one we are willing to send.

    Rejects multiple statements outright. PyMySQL does not enable multi-
    statement execution by default, but relying on a driver default for a
    security property is the kind of thing that quietly stops being true after
    a dependency bump.
    """
    stripped = _strip(sql)
    if not stripped:
        raise MariaError("empty statement")

    body = stripped.rstrip(";")
    if ";" in body:
        raise MariaError("only one statement at a time")

    leading = body.split(None, 1)[0].lower() if body.split() else ""
    if leading not in _ALLOWED_LEADING:
        raise MariaError(
            f"{leading.upper() or 'that'} is not allowed here — this connection "
            f"is read-only ({', '.join(k.upper() for k in _ALLOWED_LEADING)})")

    for pattern, why in _FORBIDDEN:
        if pattern.search(body):
            raise MariaError(f"not allowed here — {why}")
    return body


def resolve_host() -> tuple[str, str]:
    """Which machine to reach MariaDB on: (host, why).

    The default host is a DOCKER CONTAINER NAME, which only resolves inside the
    CC's own network. That is right embedded and meaningless standalone, where
    the tool runs on an engineer's laptop — it produced a getaddrinfo failure
    that read like a configuration mistake rather than a category error.

    Standalone the answer is "the CC you are pointed at": the same box serving
    the Elasticsearch this session connected to, which is also where MariaDB
    listens. So the target follows the ES connection instead of being a second
    thing to configure and keep in sync.

    NOTE: reaching into the ES module for the current target is the wrong long
    term home for this. It belongs in the shared DataSource/target registry the
    roadmap puts in Phase 2 — Postgres will need exactly the same answer. Kept
    as one lazy import until that exists, rather than inventing half of it here.
    """
    explicit = os.environ.get("MARIA_HOST", "").strip()
    if explicit:
        return explicit, "MARIA_HOST"

    if policy.profile() == policy.EMBEDDED:
        return settings.maria_host, "co-located on this CC"

    from modules.es.client import get_client
    # cc_host, not host: with ES reached through an SSH tunnel, `host` is
    # 127.0.0.1 and the appliance is elsewhere. Using `host` here would have
    # pointed MariaDB at the engineer's own machine.
    host = (getattr(get_client(), "cc_host", "") or "").strip()
    if not host or host in ("localhost", "127.0.0.1"):
        # Nothing useful to inherit. Say what to do instead of attempting a
        # connection that can only fail with a DNS error.
        return "", "no CC connected"
    return host, "the connected CC"


# How each CC turned out to be reachable, so the decision is made once rather
# than re-probing a closed port on every query. Cleared when a tunnel dies.
_route_cache: dict[str, tuple[str, int]] = {}


def _direct_ok(host: str, port: int, timeout: float = 3.0) -> bool:
    import socket
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


def _route(host: str) -> tuple[str, int, str]:
    """Where to actually point PyMySQL for this CC: (host, port, how).

    Standalone, 3306 is routinely closed between an engineer and a customer's
    appliance while SSH is open — that is the whole reason the SSH stack exists
    in this codebase. So: try direct once, and otherwise forward 3306 through
    the SSH connection the user already authenticated for Elasticsearch, rather
    than asking for the same credentials twice.
    """
    port = settings.maria_port
    cached = _route_cache.get(host)
    if cached:
        # A dead tunnel still accepts connections locally and then hangs, so
        # confirm it is alive rather than trusting the cache.
        if cached[0] != "127.0.0.1" or ssh_tunnel.active_tunnel(_TUNNEL) is not None:
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
        logger.warning("[maria] SSH tunnel to %s failed: %s", host, tun.get("error"))
        return host, port, "direct"
    _route_cache[host] = (tun["local_host"], tun["local_port"])
    return tun["local_host"], tun["local_port"], "SSH tunnel"


_TUNNEL = "maria"


@contextmanager
def connection(schema: str = "", readonly: bool = True):
    """A connection to the CC's MariaDB, read-only unless asked otherwise.

    `schema` only sets the default database for unqualified names; it grants
    nothing, and every catalog query names its schema explicitly anyway.

    `readonly=False` exists for exactly one caller — modules/maria/writes.py,
    reached only when the `maria.write` capability is unlocked, whose route is
    not registered otherwise. It is a keyword with a read-only default so that
    a connection nobody deliberately asked to write through cannot write.
    """
    creds = credentials.resolve()
    cc_host, why = resolve_host()
    if not cc_host:
        raise MariaError(
            "no CC is connected — MariaDB is reached on the same machine as the "
            "Elasticsearch this session is pointed at, so connect to a CC first "
            "(or set MARIA_HOST to reach one directly)")

    # Embedded the container name resolves on the shared docker network and
    # there is nothing to route around; only the remote tool needs the fallback.
    if policy.profile() == policy.EMBEDDED:
        host, port, via = cc_host, settings.maria_port, "direct"
    else:
        host, port, via = _route(cc_host)

    try:
        conn = pymysql.connect(
            host=host,
            port=port,
            user=creds["user"],
            password=creds["password"],
            database=schema or None,
            cursorclass=DictCursor,
            connect_timeout=settings.maria_timeout_s,
            read_timeout=settings.maria_timeout_s,
            write_timeout=settings.maria_timeout_s,
            charset="utf8mb4",
            autocommit=False,
        )
    except pymysql.Error as exc:
        # The driver's message names the actual cause (host unknown, access
        # denied, connection refused); saying WHERE that host came from, and
        # how we tried to reach it, is what makes it actionable.
        raise MariaError(f"cannot reach MariaDB on {cc_host}:{settings.maria_port} "
                         f"({why}, via {via}) — {exc}") from exc

    try:
        with conn.cursor() as cur:
            if readonly:
                # Layer 1. Belt and braces: the session cannot write, and
                # neither can this transaction.
                cur.execute("SET SESSION TRANSACTION READ ONLY")
            cur.execute(f"SET SESSION max_statement_time={int(settings.maria_timeout_s)}")
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


# Marker put in place of a binary column value. A BLOB is not text and must not
# be guessed at: FastAPI's default encoder calls bytes.decode(), which raised
# UnicodeDecodeError and turned an ordinary row into HTTP 500 — quartz's
# JOB_DATA holds serialised Java objects (they start 0xAC 0xED), so this is the
# normal case for that table, not an exotic one.
BLOB_KEY = "__blob__"


def bit_value(v):
    """A BIT column as the number it represents.

    The driver hands BIT back as raw bytes — b"\\x01" for a bit(1) — which is
    technically what the wire carries and useless to read. Untreated it reached
    _jsonable(), was classified as binary, and a boolean flag like
    `c_user_mgt.is_admin` rendered as a 0.0 KB download link that could never
    have worked: the server does not count `bit` among its blob types, so the
    download would have been refused for a column the UI had just offered.

    Big-endian, and width-agnostic, so bit(1) and bit(64) both come back as an
    integer — the same thing you would get from CAST(col AS UNSIGNED).
    """
    if v is None:
        return None
    if isinstance(v, (bytes, bytearray, memoryview)):
        return int.from_bytes(bytes(v), "big")
    return int(v)


def _bit_columns(description) -> set[str]:
    """Names of the BIT columns in a result set, from the driver's own
    per-column type codes rather than from a second catalog lookup — this has
    to be right for arbitrary SQL from the query screen too, where there is no
    single table to look up."""
    return {d[0] for d in (description or ()) if d[1] == FIELD_TYPE.BIT}


def _jsonable(value):
    """Make one column value safe to serialise, without pretending binary is text."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return {BLOB_KEY: True, "bytes": len(raw)}
    if isinstance(value, Decimal):
        # str, not float: DECIMAL is exact and float would silently round it.
        return str(value)
    if isinstance(value, timedelta):
        return str(value)
    if isinstance(value, set):
        return sorted(value)
    return value


def run(sql: str, params: tuple = (), schema: str = "",
        limit: int | None = None) -> tuple[list[dict], bool]:
    """Run one read-only statement. Returns (rows, truncated).

    `truncated` is the honest half of the row cap: a silently shortened result
    set is indistinguishable from a complete one, which is how someone
    concludes a table is empty when it is merely capped.
    """
    body = check_readonly(sql)
    cap = settings.maria_max_rows if limit is None else max(1, int(limit))

    with connection(schema) as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(body, params or None)
                rows = cur.fetchmany(cap + 1)
                bits = _bit_columns(cur.description)
            except pymysql.Error as exc:
                raise MariaError(str(exc)) from exc

    truncated = len(rows) > cap
    clean = [{k: (bit_value(v) if k in bits else _jsonable(v))
              for k, v in row.items()} for row in rows[:cap]]
    return clean, truncated


def run_raw(sql: str, params: tuple = (), schema: str = "") -> list[dict]:
    """Like run(), but WITHOUT the JSON sanitising — the caller wants the real
    bytes. Only for the blob download path, which streams them rather than
    serialising them."""
    body = check_readonly(sql)
    with connection(schema) as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(body, params or None)
                return list(cur.fetchmany(2))     # 2, so an ambiguous key shows up
            except pymysql.Error as exc:
                raise MariaError(str(exc)) from exc


def server_info() -> dict:
    """Version and uptime — the MariaDB node's identity for the sidebar."""
    rows, _ = run("SELECT VERSION() AS version")
    version = (rows[0].get("version") if rows else "") or ""
    creds = credentials.resolve()
    host, why = resolve_host()
    routed = _route_cache.get(host)
    via = "direct" if (not routed or routed[0] == host) else "SSH tunnel"
    return {"connected": True, "version": version,
            "host": host, "host_source": why, "via": via,
            "port": settings.maria_port,
            # Which account, and where it was read from — the first question
            # when a connection is refused. The password is never included.
            "user": creds["user"], "credential_source": creds["source"]}
