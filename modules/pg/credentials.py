"""Where the PostgreSQL credentials come from.

Unlike MariaDB (modules/maria/credentials.py), there is no per-CC wrapper
script to read this account from — the `postgres` account is set once, as the
`config_postgres_1` container's own POSTGRES_USER / POSTGRES_PASSWORD
environment, baked into that image rather than typed by an operator on the
host filesystem. There is nothing on the host to bind-mount and follow the way
the mysql wrapper is followed, so resolution here is one source shorter.

Resolution order, first hit wins:

  1. PG_USER / PG_PASSWORD in the environment. What CI/CD should render into
     the compose entry, matching the DATASOURCE_* convention three system
     services already use, and the same override contract MariaDB gets from
     MARIA_USER / MARIA_PASSWORD. An explicit override always beats the
     built-in default.
  2. The documented defaults in config.py — the account baked into the
     config_postgres_1 image today. Last resort, so a standalone run or a CC
     that has not been given an override still works instead of failing
     obscurely.

Resolved once at first use, for the same reason as MariaDB: the credentials
cannot change under a running container without a restart, and re-reading per
connection would only invite the two halves to disagree.

Nothing here ever logs a password. The source is logged, because "which of the
two did it use" is the first question when a connection is refused, and the
answer is not sensitive.
"""

from __future__ import annotations

import logging
import os

from config import settings

logger = logging.getLogger(__name__)

_cache: dict | None = None


def _from_env() -> dict | None:
    user = os.environ.get("PG_USER", "").strip()
    password = os.environ.get("PG_PASSWORD", "").strip()
    if user and password:
        return {"user": user, "password": password, "source": "environment"}
    return None


def resolve() -> dict:
    global _cache
    if _cache is not None:
        return _cache

    found = (_from_env()
             or {"user": settings.pg_user,
                 "password": settings.pg_password,
                 "source": "built-in default"})

    logger.info("[pg] credentials for %r from %s", found["user"], found["source"])
    _cache = found
    return _cache


def reset() -> None:
    """Drop the cached resolution. For tests."""
    global _cache
    _cache = None
