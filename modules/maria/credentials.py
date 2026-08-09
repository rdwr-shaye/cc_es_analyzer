"""Where the MariaDB credentials come from.

The CC has no credential store for this. The `common_host` account is
hardcoded in at least five places on the appliance — `/usr/local/bin/mysql`,
`mysql_no_tty`, `system_backup.sh`, `user_management` and
`py_cli/vdirect_utils.py` — each carrying the password inline. There is
nothing to look it up in, so "read it from the system" means reading one of
those, and the honest choice is the one whose only job is to hold this
connection: the `mysql` wrapper the operator himself types.

Resolution order, first hit wins:

  1. MARIA_USER / MARIA_PASSWORD in the environment. What CI/CD should render
     into the compose entry, matching the DATASOURCE_* convention three system
     services already use. An explicit override always beats discovery.
  2. The CC's own mysql wrapper (MARIA_CRED_FILE, default /usr/local/bin/mysql),
     bind-mounted read-only. This is the point of the exercise: if a hardened
     CC changes the account, the tool follows it without a rebuild, because it
     reads the same line the operator does.
  3. The documented defaults in config.py. Last resort, so a standalone run or
     a CC without the mount still works instead of failing obscurely.

Resolved once at first use. The credentials cannot change under a running
container without a restart, and re-reading per connection would only invite
the two halves to disagree — the same reasoning as core/policy.py.

Nothing here ever logs a password. The source is logged, because "which of the
three did it use" is the first question when a connection is refused, and the
answer is not sensitive.
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


def resolve() -> dict:
    global _cache
    if _cache is not None:
        return _cache

    found = (_from_env()
             or _from_wrapper(settings.maria_cred_file)
             or {"user": settings.maria_user,
                 "password": settings.maria_password,
                 "source": "built-in default"})

    logger.info("[maria] credentials for %r from %s", found["user"], found["source"])
    _cache = found
    return _cache


def reset() -> None:
    """Drop the cached resolution. For tests."""
    global _cache
    _cache = None
