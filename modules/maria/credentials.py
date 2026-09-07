"""Where the MariaDB credentials come from.

The CC has no credential store for this. The `common_host` account is
hardcoded in at least five places on the appliance — `/usr/local/bin/mysql`,
`mysql_no_tty`, `system_backup.sh`, `user_management` and
`py_cli/vdirect_utils.py` — each carrying the password inline. There is
nothing to look it up in, so "read it from the system" means reading one of
those, and the honest choice is the one whose only job is to hold this
connection: the `mysql` wrapper the operator himself types.

Except that convention is not universal. A second lab CC (10.205.189.21,
HA-config) has no such line to find at all — its wrapper is a bare
`mysql "$@"`, and the account that answers a real network connection
(`root`, over TCP) only exists as the MariaDB container's own
MARIADB_ROOT_PASSWORD environment variable. Two things follow from that:

  * discovery needs a second tier, reachable only over host access
    (core/hostexec.py's `maria.creds` op — the SSH backend standalone, the
    agent embedded), for the appliances where the wrapper alone answers
    nothing;
  * no fixed discovery order can be trusted to always land on the right
    account on every appliance CC Admin will ever meet, so an operator who
    knows better needs to be able to just SAY what the account is, from the
    UI, without a redeploy. That is `set_override` / `clear_override` below,
    reached through modules/maria/routers/credentials.py.

Resolution order, first hit wins:

  1. An operator override for THIS CC, saved through the UI
     (core/remote/cred_store.py, kind="maria"). Deliberately first: an
     operator who set this explicitly gets exactly what they asked for,
     never silently shadowed by something auto-discovered afterwards.
  2. MARIA_USER / MARIA_PASSWORD in the environment. What CI/CD should render
     into the compose entry, matching the DATASOURCE_* convention three system
     services already use.
  3. The CC's own mysql wrapper (MARIA_CRED_FILE, default /usr/local/bin/mysql),
     read locally. Only ever finds anything embedded, where the file is
     bind-mounted read-only into this container — standalone, this process
     runs on an engineer's own machine, and that path simply does not exist
     there. This is the point of the exercise for a CC that keeps the -u/-p
     convention: it follows an account change on the box without a rebuild,
     because it reads the same line the operator does.
  4. Host-mediated discovery (core/hostexec.py's `maria.creds`), which reaches
     the CC over SSH standalone or the host agent embedded rather than a local
     file. This is what covers a CC like 10.205.189.21: the wrapper it reads
     is the SAME file as tier 3, but read on the box itself rather than
     through a bind mount, and it additionally knows to fall back to the
     MariaDB container's own MARIADB_ROOT_PASSWORD when the wrapper has
     nothing. Slower than the others (a real round trip), so this is the
     last thing tried before giving up on discovery.
  5. The documented defaults in config.py. Last resort, so a run with no
     override, no environment, and no host access still does SOMETHING
     instead of failing obscurely.

Resolved once per connected CC, then cached — re-discovering per connection
would only invite the tiers to disagree with themselves, the same reasoning
as core/policy.py. reset() is called whenever the connected CC changes
(modules/es/routers/query.py's `connect()`) and whenever the override is
written, so switching appliances or saving a new override takes effect on the
very next connection rather than needing a restart.

Nothing here ever logs a password. The source is logged, because "which tier
answered" is the first question when a connection is refused, and the answer
is not sensitive.
"""

from __future__ import annotations

import logging
import os
import re

from config import settings

logger = logging.getLogger(__name__)

# `mariadb -ucommon_host -pradware "$@"` — MySQL/MariaDB take the value glued
# to -u/-p, and the wrapper writes it that way. The spaced form is accepted too
# because other product scripts use it (vdirect_utils.py passes ['-u', 'x']).
_USER_RE = re.compile(r"-u\s*([^\s\"']+)")
_PASS_RE = re.compile(r"-p\s*([^\s\"']+)")

_cache: dict | None = None


def _from_override(host: str) -> dict | None:
    if not host:
        return None
    from core.remote import cred_store
    saved = cred_store.get(host, kind="maria")
    if saved and saved.get("user") and saved.get("password"):
        return {"user": saved["user"], "password": saved["password"],
                "source": "operator override"}
    return None


def _from_env() -> dict | None:
    user = os.environ.get("MARIA_USER", "").strip()
    password = os.environ.get("MARIA_PASSWORD", "").strip()
    if user and password:
        return {"user": user, "password": password, "source": "environment"}
    return None


def _from_wrapper(path: str) -> dict | None:
    """Parse the CC's mysql wrapper. Missing file is the normal case off-box."""
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except FileNotFoundError:
        return None
    except OSError as exc:
        # The mount exists but is unreadable — worth a warning, because to the
        # operator this presents as "it ignored the credentials on the box".
        logger.warning("[maria] cannot read %s: %s", path, exc)
        return None

    # Only look at the line that actually invokes the client. Scanning the whole
    # file would happily pick up a -p from an unrelated command or a comment.
    for line in text.splitlines():
        if "mariadb" not in line and "mysql" not in line:
            continue
        if "-u" not in line or "-p" not in line:
            continue
        user = _USER_RE.search(line)
        password = _PASS_RE.search(line)
        if user and password:
            return {"user": user.group(1), "password": password.group(1),
                    "source": os.path.basename(path)}
    return None


def _from_host_discovery() -> dict | None:
    """Ask core/hostexec.py's `maria.creds` op — a real round trip to the CC,
    so this is only reached when the faster, local tiers found nothing.
    Lazily imported for the reason core/hostexec.py itself lazily imports
    modules.es.client: avoiding a module-scope cycle between core/ and
    modules/, not because one actually exists today."""
    from core import hostexec
    try:
        result = hostexec.run_op("maria.creds")
    except hostexec.HostExecError as exc:
        logger.info("[maria] host-mediated credential discovery unavailable: %s", exc)
        return None
    if result["rc"] != 0 or not result["stdout"].strip():
        return None
    parts = result["stdout"].strip().split("\t", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return {"user": parts[0], "password": parts[1], "source": "discovered on the CC"}


def resolve(host: str = "") -> dict:
    global _cache
    if _cache is not None:
        return _cache

    found = (_from_override(host)
             or _from_env()
             or _from_wrapper(settings.maria_cred_file)
             or _from_host_discovery()
             or {"user": settings.maria_user,
                 "password": settings.maria_password,
                 "source": "built-in default"})

    logger.info("[maria] credentials for %r from %s", found["user"], found["source"])
    _cache = found
    return _cache


def reset() -> None:
    """Drop the cached resolution — called whenever the connected CC changes
    or the override is written, and by tests."""
    global _cache
    _cache = None


def override_state(host: str) -> dict:
    """Whether an operator override is set for this CC, without its password."""
    if not host:
        return {"set": False}
    from core.remote import cred_store
    saved = cred_store.get(host, kind="maria")
    if not saved or not saved.get("user"):
        return {"set": False}
    return {"set": True, "user": saved["user"]}


def set_override(host: str, user: str, password: str) -> None:
    """Save an operator-supplied account for this CC and make it effective
    immediately — the whole point of putting this in the UI rather than an
    environment variable is that it must not need a restart to take hold."""
    from core.remote import cred_store
    cred_store.save(host, user, password, kind="maria")
    reset()


def clear_override(host: str) -> bool:
    from core.remote import cred_store
    removed = cred_store.delete(host, kind="maria")
    if removed:
        reset()
    return removed
