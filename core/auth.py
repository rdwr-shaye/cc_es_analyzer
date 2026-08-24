"""Password authentication for the embedded profile.

Until this module existed there was no login at all: core/sessions.py identifies
a browser by a cookie and takes its owner's word for their name, which is
correct for standalone — the app runs on the engineer's own machine, and
anything that reaches it has already reached their laptop. Embedded is
different. The app sits on a customer's appliance, reachable by anyone who can
reach the CC's nginx, and it can read every datastore on the box.

WHAT THIS IS AND IS NOT. This is a single shared appliance credential, the same
shape as the console password on any other network device: one account, no
roles, no directory integration. It is not the identity system the roadmap's
Phase 1 calls for — that one has to answer "WHICH engineer did this" for the
audit trail, and a shared password cannot. This exists because "no password at
all" is not a defensible position for a component on a customer's box, and it
is deliberately shaped so the real thing can replace it: everything outside this
module asks `is_authenticated(sid)`, never `check_password(...)`.

STORAGE. scrypt from hashlib, not bcrypt or passlib. Both would be new
dependencies in an image that has to pass the CC pipeline's SBOM and CVE rules,
and scrypt is memory-hard, in the standard library, and needs no wheel. Cost
parameters below verify in roughly 40 ms, which is slow enough to make offline
guessing expensive and fast enough that nobody notices a login.

THE DEFAULT PASSWORD IS SHIPPED AS A HASH, NEVER AS PLAINTEXT. This repository
is public. A default credential in the source would be the out-of-box password
for a component that ships on appliances, readable by anyone. The hash below
lets a fresh CC accept the documented default without the source disclosing what
it is. Operators learn it from the internal runbook, which is where a default
credential belongs.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import threading
import time

from config import settings

logger = logging.getLogger(__name__)

# scrypt cost. n is the memory/CPU knob; r and p are the block and parallelism
# factors the RFC recommends leaving alone. Raising n later is safe — an
# encoded hash carries its own parameters, so old ones keep verifying and are
# transparently upgraded on the next successful login.
_N, _R, _P, _DKLEN = 1 << 14, 8, 1, 32
_SALT_BYTES = 16

# scrypt hash of the documented default. See the module docstring for why the
# plaintext is not here. Changing this changes the out-of-box password.
DEFAULT_PASSWORD_HASH = (
    "scrypt$16384$8$1$KVQ9BP/5CS9DnIe5VaiQ6Q==$fTCBWIdEya83ui2HyshAqn5TMUNsZYIcqpEoqgexYh8="
)

_lock = threading.Lock()
_cache: dict | None = None


# ── Hashing ──────────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    """Encode as ``scrypt$n$r$p$salt_b64$key_b64``.

    Self-describing on purpose: the parameters travel with the hash, so the
    cost can be raised in a later release without invalidating the password
    every deployed CC is already using.
    """
    salt = os.urandom(_SALT_BYTES)
    key = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                         n=_N, r=_R, p=_P, dklen=_DKLEN)
    return "scrypt${}${}${}${}${}".format(
        _N, _R, _P,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(key).decode("ascii"))


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check of *password* against an encoded hash.

    Every failure path returns False rather than raising: a malformed stored
    hash must read as "wrong password" to the caller and be loud in the log,
    not become a traceback that tells an unauthenticated caller how the store
    is broken.
    """
    try:
        scheme, n, r, p, salt_b64, key_b64 = encoded.split("$")
        if scheme != "scrypt":
            raise ValueError(f"unknown scheme {scheme!r}")
        expected = base64.b64decode(key_b64)
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=base64.b64decode(salt_b64),
            n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except Exception as exc:                                   # noqa: BLE001
        logger.warning("[auth] stored password hash is unusable: %s", exc)
        return False
    return hmac.compare_digest(actual, expected)


# ── The store ────────────────────────────────────────────────────────────────
# One small JSON file. It has to outlive the container: applying a property-file
# change means recreating it, and an operator who loses their password every
# time they unlock a capability would go straight back to the default.

def _path() -> str:
    return settings.auth_file


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    data = {}
    try:
        with open(_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        pass                                    # normal on a fresh appliance
    except (OSError, ValueError) as exc:
        # Do NOT fall back to the default silently — a corrupt store that
        # quietly reinstates the shipped password is how a box everyone
        # believes is hardened stops being hardened.
        logger.error("[auth] cannot read %s (%s) — the DEFAULT password is in "
                     "effect until this file is repaired or removed",
                     _path(), exc)
    if not isinstance(data, dict):
        data = {}
    _cache = data
    return data


def _save(data: dict) -> None:
    global _cache
    path = _path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    # Written 0600 before it is moved into place: the file is only a hash, but
    # a hash is exactly what an offline guessing attack wants, and the
    # directory is a bind mount other things on the host can read.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except Exception:
        os.unlink(tmp)
        raise
    os.replace(tmp, path)
    _cache = data


def current_hash() -> str:
    """The hash in force: the stored one, or the shipped default."""
    return _load().get("password_hash") or DEFAULT_PASSWORD_HASH


def is_default() -> bool:
    """True while the appliance is still on the password it shipped with."""
    return not _load().get("password_hash")


def username() -> str:
    return settings.auth_user


def check_password(password: str) -> bool:
    with _lock:
        return verify_password(password, current_hash())


def set_password(new_password: str) -> None:
    """Replace the password. Callers must have verified the current one."""
    with _lock:
        data = dict(_load())
        data["password_hash"] = hash_password(new_password)
        data["updated_at"] = int(time.time())
        _save(data)
    logger.info("[auth] password changed")


def mark_default_kept() -> None:
    """Record that the user was offered a change and declined.

    Only stops the prompt from reappearing every login. It deliberately does
    NOT count as hardening, and is_default() still reports the truth, so
    anything that wants to warn about a shipped credential still can.
    """
    with _lock:
        data = dict(_load())
        data["default_kept_at"] = int(time.time())
        _save(data)
    logger.warning("[auth] the shipped default password was kept in place")


def required() -> bool:
    """Whether a login is enforced at all.

    Embedded yes, standalone no. Standalone runs on the engineer's own machine
    against a CC they already hold credentials for; a second password there
    guards nothing and would be typed past. Overridable both ways by
    AUTH_REQUIRED for a lab that wants the other behaviour.
    """
    if settings.auth_required is not None:
        return bool(settings.auth_required)
    from core import policy
    return policy.profile() == policy.EMBEDDED


def state() -> dict:
    """What the UI may know before anyone has logged in.

    Says nothing a caller could not already establish by trying to log in: no
    hash, no store path, no hint about the password itself.
    """
    return {
        "required": required(),
        "username": username(),
        "using_default": is_default(),
        "change_offered": bool(_load().get("default_kept_at")),
    }


def reset_cache() -> None:
    """Drop the cached store — for tests, which rewrite the file underneath."""
    global _cache
    _cache = None
