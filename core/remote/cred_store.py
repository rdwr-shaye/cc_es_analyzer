"""
Per-machine SSH credentials for the snapshot-archive flows.

Stored Fernet-encrypted in <project>/data/ssh_creds.enc with the key beside it
(0600). This protects an at-rest copy of the file, NOT against root on the
analyzer host itself — acceptable per the user's "remember per machine" choice.
Passwords are write-only through the API: they are never returned to the UI.
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


def get(host: str) -> dict | None:
    """Return {'user': ..., 'password': ...} for host, or None."""
    with _LOCK:
        return _load().get(host)


def save(host: str, user: str, password: str) -> None:
    with _LOCK:
        data = _load()
        data[host] = {"user": user, "password": password}
        _dump(data)


def delete(host: str) -> bool:
    with _LOCK:
        data = _load()
        if host not in data:
            return False
        del data[host]
        _dump(data)
        return True


def hosts() -> list:
    """Hosts that have remembered credentials (no secrets returned)."""
    with _LOCK:
        return sorted(_load().keys())
