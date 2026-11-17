"""Check 3, Tier B — call the DefensePro device directly.

Locked everywhere by default (`widget.device_probe`, unlockable=True) and
reachable only where the read-only bind mount below actually exists — which
today means embedded, deliberately: see modules/widgets/__init__.py for why
standalone is excluded rather than merely discouraged.

Ported from the already-working `KVISION_PROJECT/decrypt_device_passwords.py`
(root of the monorepo, not this repo) — the AES-256/ECB/PKCS7 scheme, the
"ENC<digit><millis>_<password>" unwrap convention, and the executor -> device
REST path table are all confirmed against that script and independently
against kvision_libs' `EncryptionCore.java` / `EncryptionUtils.java` and the
provider-client source in kvision_data_polling_scheduler.

Uses `cryptography` (already a dependency — see requirements.txt) rather than
pulling in pycryptodome, which decrypt_device_passwords.py uses as a
standalone script outside this image: a new package in an image that has to
pass the CC pipeline's SBOM and CVE rules is a cost paid for every future
image, not just this feature.

TWO KEYS, same shape as system.storage.delete: (1) the `widget.device_probe`
capability, unlocked via cc_admin.properties, checked by the router before
this module is ever called — and (2) `settings.vision_key_file` actually
existing, which only happens when an operator has added the bind mount to the
compose file. Neither alone is enough. Missing key 2 is not an error to alarm
about — it is the default, honest state of every install that has not made
that deploy-time decision.

NEVER logs a device's user, password, or the decrypted plaintext. NEVER
returns them in an HTTP response — the caller gets a classification, a row
count and a small truncated sample, not the credential and not the raw body.
"""

from __future__ import annotations

import base64
import logging
import time

import requests
from cryptography.hazmat.primitives import padding as _padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from requests.auth import HTTPBasicAuth

from config import settings

logger = logging.getLogger(__name__)

HTTP_ERROR, UNSUPPORTED_VERSION, EMPTY, DATA = (
    "HTTP_ERROR", "UNSUPPORTED_VERSION", "EMPTY", "DATA")

# executor_type -> (device REST path, fixed query params). Identical to
# WIDGET_DEBUG_HELPER.md's own table and to decrypt_device_passwords.py's
# EXECUTOR_API_MAP, cross-checked against the provider-client source in
# kvision_data_polling_scheduler/dps/.../clients/dp/*.java.
EXECUTOR_API_MAP: dict[str, tuple[str, dict]] = {
    "DP_ATTACK_DATA":             ("/v1/attack-data",                         {"protections": "All"}),
    "DP_APPLICATIONS":            ("/v2/traffic/application",                 {}),
    "DP_TRAFFIC_DATA":            ("/v1/traffic/policy",                      {}),
    "DP_TRAFFIC_BASELINE":        ("/v1/traffic/application/characteristics", {"protections": "All"}),
    "DP_TRAFFIC_STATISTICS":      ("/v2/traffic/statistics",                  {"protocol": "TLS"}),
    "WEB_DDOS_REAL_TIME_ATTACK":  ("/v1/traffic/attack/characteristics",      {"protections": "Web-DDoS"}),
}


def load_key() -> tuple[bytes | None, str]:
    """(key_bytes, why-not). None whenever key 2 (the bind mount) is absent —
    the default state everywhere until an operator adds it deliberately."""
    path = settings.vision_key_file
    try:
        with open(path, "r", encoding="utf-8") as fh:
            b64_key = fh.readline().strip()
    except FileNotFoundError:
        return None, (
            f"{path} is not mounted — the live device probe needs a read-only "
            "bind mount of the CC host's /opt/radware/storage/secrets/.key "
            "into this container (see deploy/embedded-compose.snippet.yaml). "
            "This is expected on every install that has not added that mount.")
    except OSError as exc:
        return None, f"cannot read {path}: {exc}"

    try:
        key_bytes = base64.b64decode(b64_key)
    except Exception as exc:                                   # noqa: BLE001
        return None, f"{path} does not hold a valid base64 key: {exc}"
    if len(key_bytes) != 32:
        return None, f"expected a 32-byte AES-256 key, got {len(key_bytes)} bytes"
    return key_bytes, ""


def decrypt_password(encrypted_b64: str, aes_key: bytes) -> str:
    """Reverse of kvision_libs' EncryptionCore: base64-decode, AES-256-ECB
    decrypt, PKCS7-unpad, strip the "ENC<digit><millis>_" prefix if present.

    A value that is not actually encrypted (not base64, or decrypts to
    something without the ENC prefix) is returned unchanged rather than
    raising — decrypt_device_passwords.py established this passthrough
    behaviour works correctly against real CC data, where some accounts are
    plain text.
    """
    if not encrypted_b64:
        return encrypted_b64
    try:
        ciphertext = base64.b64decode(encrypted_b64)
    except Exception:                                          # noqa: BLE001
        return encrypted_b64
    try:
        decryptor = Cipher(algorithms.AES(aes_key), modes.ECB()).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = _padding.PKCS7(128).unpadder()
        raw = unpadder.update(padded) + unpadder.finalize()
        decrypted = raw.decode("utf-8")
    except Exception:                                          # noqa: BLE001
        return encrypted_b64
    if decrypted.startswith("ENC") and "_" in decrypted:
        return decrypted[decrypted.index("_") + 1:]
    return decrypted


def _version_at_least(version: str, minimum: str) -> bool | None:
    """None when either version string cannot be parsed — the version
    precheck is skipped rather than guessed, and the device is called."""
    try:
        v = tuple(int(p) for p in version.strip().split("."))
        m = tuple(int(p) for p in minimum.strip().split("."))
    except (ValueError, AttributeError):
        return None
    return v >= m


def classify_probe(http_status: int | None, payload) -> tuple[str, object]:
    """(status, sample) — pure, no network. `sample` is truncated, never the
    full raw body: a device's real attack/traffic payload flowing verbatim
    into CC Admin's UI and logs by default is a bigger surface than a
    diagnostic screen needs to open."""
    if http_status is None or not (200 <= http_status < 300):
        return HTTP_ERROR, None
    if payload is None:
        return EMPTY, None
    if isinstance(payload, list):
        return (DATA if payload else EMPTY), payload[:5]
    if isinstance(payload, dict):
        return (DATA if payload else EMPTY), payload
    return EMPTY, None


def probe_device(device_ip: str, executor_type: str,
                 last_update_iso: str | None = None,
                 device_version: str = "") -> dict:
    """Decrypt this device's stored credential and call its own REST API.

    Every early return still reports `took_ms` and a `status`, so the caller
    (routers/probe.py) never needs a second error path — same discipline as
    modules/widgets/client.py::call_widget_api.
    """
    started = time.time()

    def _done(status: str, detail: str = "", **extra) -> dict:
        return {"device_ip": device_ip, "executor_type": executor_type,
                "status": status, "detail": detail,
                "took_ms": int((time.time() - started) * 1000), **extra}

    if executor_type not in EXECUTOR_API_MAP:
        return _done(HTTP_ERROR, f"no device API mapping for executor type {executor_type!r}")

    if device_version:
        min_version = None
        from modules.widgets import catalog
        for w in catalog.WIDGET_CATALOG.values():
            if w.executor_type == executor_type and w.min_dp_version:
                min_version = w.min_dp_version
                break
        if min_version:
            ok = _version_at_least(device_version, min_version)
            if ok is False:
                return _done(UNSUPPORTED_VERSION,
                            f"device is {device_version}, this executor needs "
                            f"{min_version} or newer — skipped the call")

    key, why = load_key()
    if key is None:
        return _done(HTTP_ERROR, why)

    from modules.maria import client
    try:
        # Column names confirmed live against the lab CC (2026-09-09) via
        # SHOW COLUMNS FROM vision_ng.device_access: https_username, not
        # https_user — the JPA field name (DeviceAccess.httpsUsername) and the
        # SQL column happen to differ in exactly this one spot.
        rows, _truncated = client.run(
            "SELECT https_username, https_password FROM device_access WHERE mgt_ip = %s",
            params=(device_ip,), schema=settings.widgets_vision_schema)
    except client.MariaError as exc:
        return _done(HTTP_ERROR, f"could not read device_access: {exc}")
    if not rows:
        return _done(HTTP_ERROR, f"no device_access row for {device_ip}")

    user = rows[0].get("https_username") or ""
    password = decrypt_password(rows[0].get("https_password") or "", key)

    path, fixed_params = EXECUTOR_API_MAP[executor_type]
    query = dict(fixed_params)
    if last_update_iso:
        query["last_update"] = last_update_iso
    url = f"https://{device_ip}:{settings.dp_device_port}{path}"

    http_status, payload = None, None
    try:
        # DefensePro devices carry self-signed certificates — confirmed by
        # decrypt_device_passwords.py's own ssl context, matching the
        # product's RESTUtil.java. Trusting them is what makes the call
        # possible at all; it is not a weaker guarantee than the browser
        # warning an engineer already clicks through for the same device.
        r = requests.get(url, params=query, auth=HTTPBasicAuth(user, password),
                         verify=False, timeout=settings.vrm_timeout_s)
        http_status = r.status_code
        try:
            payload = r.json()
        except ValueError:
            payload = None
    except Exception as exc:                                   # noqa: BLE001
        return _done(HTTP_ERROR, f"{type(exc).__name__}: {exc}")
    finally:
        # Best-effort scrub — CPython gives no hard guarantee a string is
        # unreachable after this, but there is no reason to hold a reference
        # to the plaintext password any longer than the request that used it.
        user = password = None                                 # noqa: F841

    status, sample = classify_probe(http_status, payload)
    logger.info("[widgets] live probe %s %s -> %s", device_ip, executor_type, status)
    return _done(status, "", http_status=http_status, sample=sample)
