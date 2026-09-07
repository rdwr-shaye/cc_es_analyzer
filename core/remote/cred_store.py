"""
Per-machine credentials, keyed by host and now also by kind.

Stored Fernet-encrypted in <project>/data/ssh_creds.enc with the key beside it
(0600). This protects an at-rest copy of the file, NOT against root on the
analyzer host itself — acceptable per the user's "remember per machine" choice.
Passwords are write-only through the API: they are never returned to the UI.

Originally SSH-only (the snapshot-archive flows), one entry per host. The
MariaDB credential override (modules/maria/routers/credentials.py) reuses this
same store rather than inventing a second encrypted file for the same
"remember one secret per CC" shape — `kind` namespaces the two apart.
`kind="ssh"` keeps the ORIGINAL bare-host key so every credential saved before
this changed keeps reading back exactly as it did; every other kind is keyed
as `f"{kind}:{host}"`, so nothing here needs a migration step.
"""
import json
import os
import threading

from cryptography.fernet import Fernet

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
_KEY_FILE = os.path.join(_DATA_DIR, ".ssh_creds.key")
_ENC_FILE = os.path.join(_DATA_DIR, "ssh_creds.enc")

_LOCK = threading.Lock()


def _fernet() -> Fernet:
    os.makedirs(_DATA_DIR, exist_ok=True)
    if not os.path.isfile(_KEY_FILE):
        key = Fernet.generate_key()
        fd = os.open(_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
    with open(_KEY_FILE, "rb") as fh:
        return Fernet(fh.read())


def _load() -> dict:
    if not os.path.isfile(_ENC_FILE):
        return {}
    try:
        with open(_ENC_FILE, "rb") as fh:
            return json.loads(_fernet().decrypt(fh.read()))
    except Exception:
        return {}          # unreadable/corrupt → treat as empty (re-prompt)


def _dump(data: dict) -> None:
    blob = _fernet().encrypt(json.dumps(data).encode())
    tmp = _ENC_FILE + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(blob)
    os.replace(tmp, _ENC_FILE)


def _key(host: str, kind: str) -> str:
    return host if kind == "ssh" else f"{kind}:{host}"


def get(host: str, kind: str = "ssh") -> dict | None:
    """Return {'user': ..., 'password': ...} for host, or None."""
    with _LOCK:
        return _load().get(_key(host, kind))


def save(host: str, user: str, password: str, kind: str = "ssh") -> None:
    with _LOCK:
        data = _load()
        data[_key(host, kind)] = {"user": user, "password": password}
        _dump(data)


def delete(host: str, kind: str = "ssh") -> bool:
    with _LOCK:
        data = _load()
        key = _key(host, kind)
        if key not in data:
            return False
        del data[key]
        _dump(data)
        return True


def hosts() -> list:
    """SSH hosts that have remembered credentials (no secrets returned).
    Unchanged from before "kind" existed. Every non-"ssh" kind's keys carry
    that kind's own `kind:` prefix (see _key), filtered out here by name
    rather than by "any colon", since an IPv6 literal is a plausible host."""
    with _LOCK:
        keys = _load().keys()
    return sorted(k for k in keys if not k.startswith("maria:"))
