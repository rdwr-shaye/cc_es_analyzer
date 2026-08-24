"""Tests for core/auth.py and core/lifecycle.py — the login and the time box.

These two decide whether a stranger who can reach a customer's appliance can
read its datastores, and how long a window stays open once someone does. Both
are the kind of code whose failure is silent: a gate that stops gating still
returns 200, and a countdown that never fires just leaves the container up. So
the assertions here lean on the REFUSALS and on the paths nobody exercises by
hand — a locked-out client, a corrupt password file, an expired box with work
still running.

The HTTP half runs against a real app instance through TestClient, because the
gate lives in main.py's middleware and a unit test of auth.py alone would prove
nothing about whether any endpoint is actually protected.

    python tests/test_auth_lifecycle.py
    pytest tests/test_auth_lifecycle.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

# Must be set before config/main import: the profile and the store path are
# read at import time, and the point is to exercise the EMBEDDED behaviour.
_TMP = tempfile.mkdtemp(prefix="cc_admin_auth_test_")
os.environ["ANALYZER_PROFILE"] = "embedded"
os.environ["AUTH_FILE"] = os.path.join(_TMP, "auth.json")
os.environ["SESSION_TIMEBOX"] = "false"      # the HTTP half must not self-stop

from fastapi.testclient import TestClient    # noqa: E402
import main as app_main                      # noqa: E402
from core import auth, lifecycle             # noqa: E402

_FAILURES: list[str] = []

# NOT the shipped default. This repository is public, so the real out-of-box
# password must not appear here any more than it appears in core/auth.py — a
# hash in the source with the plaintext in the tests beside it would be a lock
# with the key taped to the door.
#
# The shipped hash is swapped for a hash of this stand-in instead, which loses
# nothing: every behaviour worth testing — the gate, the lockout, the change
# flow, the is_default() bookkeeping — is about the MECHANISM, and none of it
# depends on which string the appliance happens to ship with. The real constant
# is checked separately, for being a well-formed hash rather than for its value.
DEFAULT_PW = "test-only-default-password"
_REAL_DEFAULT_HASH = auth.DEFAULT_PASSWORD_HASH
auth.DEFAULT_PASSWORD_HASH = auth.hash_password(DEFAULT_PW)


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok  {label}")
    else:
        _FAILURES.append(f"{label}: expected {want!r}, got {got!r}")
        print(f"  BAD {label} — expected {want!r}, got {got!r}")


def _fresh_client() -> TestClient:
    """A client with its own cookie jar, and the store reset to shipped state.

    The lockout counter is cleared too. It is keyed by client IP, and every
    test here arrives from the same one, so without this the rate-limit test
    would lock out every test that ran after it — which is exactly what it did
    the first time this file was run.
    """
    try:
        os.unlink(os.environ["AUTH_FILE"])
    except FileNotFoundError:
        pass
    auth.reset_cache()
    from core.routers import auth as auth_router
    with auth_router._fail_lock:
        auth_router._fails.clear()
    return TestClient(app_main.app)


# ── Hashing ──────────────────────────────────────────────────────────────────

def test_shipped_default_is_a_hash() -> None:
    """The one thing worth asserting about the REAL shipped credential.

    Not what it is — this file must never know that — but that it is a properly
    formed scrypt hash with a real salt and a sane cost. A plaintext password
    pasted into that constant by mistake would not crash anything: it would
    simply fail every login, and read as a wrong-password bug rather than as
    the disclosure it actually is.
    """
    print("\nthe shipped default is a hash, not a password")
    parts = _REAL_DEFAULT_HASH.split("$")
    check("it has the scheme/n/r/p/salt/key shape", len(parts), 6)
    check("...and names scrypt", parts[0], "scrypt")
    check("...with a sane cost", int(parts[1]) >= 1 << 13, True)
    import base64
    check("...a real salt", len(base64.b64decode(parts[4])) >= 16, True)
    check("...and a 32-byte key", len(base64.b64decode(parts[5])), 32)


def test_hashing() -> None:
    print("\npassword hashing")
    check("a hash accepts the password it was made from",
          auth.verify_password(DEFAULT_PW, auth.DEFAULT_PASSWORD_HASH), True)
    check("rejects a wrong password",
          auth.verify_password("wrong", auth.DEFAULT_PASSWORD_HASH), False)
    check("the REAL shipped hash rejects this file's stand-in",
          auth.verify_password(DEFAULT_PW, _REAL_DEFAULT_HASH), False)
    check("rejects the empty password",
          auth.verify_password("", auth.DEFAULT_PASSWORD_HASH), False)
    check("rejects a one-character-short near miss",
          auth.verify_password(DEFAULT_PW[:-1], auth.DEFAULT_PASSWORD_HASH), False)

    # A salt that did not vary would let one rainbow table cover every CC that
    # shares a password, which on an appliance fleet is most of them.
    check("the same password hashes differently each time",
          auth.hash_password("repeated") != auth.hash_password("repeated"), True)
    check("a freshly hashed password verifies",
          auth.verify_password("repeated", auth.hash_password("repeated")), True)

    # A malformed store must read as "wrong password", not raise: the caller is
    # unauthenticated, and a traceback would describe the store to them.
    for junk in ("", "garbage", "scrypt$x$y$z$q$r", "bcrypt$12$abc"):
        check(f"malformed hash {junk[:18]!r} is a refusal, not a crash",
              auth.verify_password("anything", junk), False)


# ── The gate ─────────────────────────────────────────────────────────────────

PROTECTED = [
    ("GET",  "/api/system/summary"),
    ("GET",  "/api/indices"),
    ("GET",  "/api/maria/schemas"),
    ("GET",  "/api/system/containers"),
    ("GET",  "/api/exports"),
]

PUBLIC = [
    "/api/auth/state",
    "/api/session/lifetime",
    "/api/policy",
]


def test_gate() -> None:
    print("\nthe gate: nothing reaches a datastore without a login")
    c = _fresh_client()
    for method, path in PROTECTED:
        r = c.request(method, path)
        check(f"{method} {path} refused while logged out", r.status_code, 401)

    for path in PUBLIC:
        r = c.get(path)
        check(f"GET {path} answers without a login", r.status_code, 200)

    # The unauthenticated view must not disclose that the box is still on the
    # password it shipped with — that is half of the credential.
    body = c.get("/api/auth/state").json()
    check("auth/state hides using_default from a stranger",
          "using_default" in body, False)


def test_login_and_logout() -> None:
    print("\nlogin and logout")
    c = _fresh_client()

    r = c.post("/api/auth/login", json={"username": "admin", "password": "nope"})
    check("wrong password is refused", r.status_code, 401)
    check("...and does not say which half was wrong",
          r.json()["error"], "Incorrect username or password.")

    r = c.post("/api/auth/login", json={"username": "root", "password": DEFAULT_PW})
    check("wrong username is refused", r.status_code, 401)
    check("...with the identical message, so the account name stays secret",
          r.json()["error"], "Incorrect username or password.")

    r = c.post("/api/auth/login", json={"username": "admin", "password": DEFAULT_PW})
    check("the documented default signs in", r.status_code, 200)
    check("...and reports that the shipped password is still in place",
          r.json()["using_default"], True)

    check("a protected path now answers",
          c.get("/api/system/summary").status_code, 200)

    c.post("/api/auth/logout")
    check("logging out closes the door again",
          c.get("/api/system/summary").status_code, 401)


def test_password_change() -> None:
    print("\nchanging the password")
    c = _fresh_client()
    c.post("/api/auth/login", json={"username": "admin", "password": DEFAULT_PW})

    r = c.post("/api/auth/password", json={"current": "wrong", "new": "abcdefghij"})
    check("the current password is required even when logged in", r.status_code, 403)

    r = c.post("/api/auth/password", json={"current": DEFAULT_PW, "new": "short"})
    check("a too-short password is refused", r.status_code, 400)

    r = c.post("/api/auth/password", json={"current": DEFAULT_PW, "new": "a-fine-new-password"})
    check("a valid change is accepted", r.status_code, 200)
    check("...and the box no longer reports the shipped default",
          auth.is_default(), False)

    c.post("/api/auth/logout")
    check("the OLD password stops working",
          c.post("/api/auth/login",
                 json={"username": "admin", "password": DEFAULT_PW}).status_code, 401)
    check("the NEW password works",
          c.post("/api/auth/login",
                 json={"username": "admin", "password": "a-fine-new-password"}).status_code, 200)

    # The container is recreated whenever a capability is unlocked. A password
    # that did not survive that would silently revert to the shipped default.
    auth.reset_cache()
    check("the change survives a restart (it is on disk, not in memory)",
          auth.is_default(), False)


def test_change_password_needs_a_session() -> None:
    print("\nan anonymous caller cannot change the password")
    c = _fresh_client()
    check("POST /api/auth/password while logged out",
          c.post("/api/auth/password",
                 json={"current": DEFAULT_PW, "new": "abcdefghij"}).status_code, 401)
    check("POST /api/auth/keep-default while logged out",
          c.post("/api/auth/keep-default").status_code, 401)


def test_rate_limit() -> None:
    print("\nguessing is rate limited")
    c = _fresh_client()
    codes = [c.post("/api/auth/login",
                    json={"username": "admin", "password": f"guess{i}"}).status_code
             for i in range(7)]
    check("the first attempts are plain refusals", codes[:5], [401] * 5)
    check("further attempts are locked out", codes[5:], [429, 429])
    # The lockout has to hold against a CORRECT password too, or an attacker
    # simply keeps going until they hit it.
    check("even the right password is refused while locked out",
          c.post("/api/auth/login",
                 json={"username": "admin", "password": DEFAULT_PW}).status_code, 429)


def test_keep_default_is_recorded_but_not_hardening() -> None:
    print("\ndeclining the change is recorded honestly")
    c = _fresh_client()
    c.post("/api/auth/login", json={"username": "admin", "password": DEFAULT_PW})
    c.post("/api/auth/keep-default")
    state = c.get("/api/auth/state").json()
    check("the offer is marked as made", state["change_offered"], True)
    check("...but the box still reports it is on the shipped password",
          state["using_default"], True)


# ── The time box ─────────────────────────────────────────────────────────────

def test_lifecycle() -> None:
    print("\nthe time box")
    from config import settings

    # The HTTP half above runs with SESSION_TIMEBOX=false so that a test run
    # can never signal its own process. The state machine is exercised here
    # with it forced on, and driven directly — start() would spawn the watcher
    # thread, whose whole job is to call os.kill on this interpreter.
    from config import settings as _s
    _s.session_timebox = True
    lifecycle._stopping = False
    lifecycle._draining = False
    lifecycle._expires_at = time.time() + settings.session_minutes * 60
    lifecycle._started_at = time.time()

    st = lifecycle.state()
    check("a fresh window is not warning yet", st["warning"], False)

    lifecycle._expires_at = time.time() + 60          # inside the 5-minute warning
    check("the server raises the warning near the end",
          lifecycle.state()["warning"], True)

    before = lifecycle.state()["seconds_left"]
    lifecycle.extend()
    check("extending pushes the deadline out",
          lifecycle.state()["seconds_left"] > before, True)
    check("...and clears the warning", lifecycle.state()["warning"], False)

    # Expired WITH work running must drain, never kill: an export that dies
    # halfway leaves a half-written archive and no way to tell.
    import modules.es.routers.exports as exports
    with exports._JOBS_LOCK:
        exports._JOBS["test-job"] = {"kind": "export", "status": "running"}
    try:
        check("a running job is visible to the watcher",
              lifecycle._running_jobs(), ["export"])
        lifecycle._draining = True
        st = lifecycle.state()
        check("draining is reported to the UI", st["draining"], True)
        check("...along with what it is waiting for", st["running_jobs"], ["export"])
        check("the box has NOT stopped while work runs", st["stopping"], False)

        r = lifecycle.extend()
        check("extending during the drain is allowed", r["extended"], True)
        check("...and cancels the drain", lifecycle.state()["draining"], False)
    finally:
        with exports._JOBS_LOCK:
            exports._JOBS.pop("test-job", None)

    check("with no jobs left there is nothing to wait for",
          lifecycle._running_jobs(), [])

    # Once the exit is signalled, extending must not claim to have worked.
    lifecycle._stopping = True
    check("extending a container that is already stopping is refused",
          lifecycle.extend().get("extended"), False)
    lifecycle._stopping = False
    _s.session_timebox = False          # leave the process unable to stop itself


def test_standalone_is_untouched() -> None:
    """Standalone is a shipping product; neither feature may appear there.

    Resolved in a subprocess because both decisions are read from the profile,
    which this process fixed to embedded at import time.
    """
    print("\nstandalone keeps no login and no time box")
    import subprocess
    code = (
        "from core import auth, lifecycle\n"
        "print('@@%s,%s' % (auth.required(), lifecycle.enabled()))\n"
    )
    env = dict(os.environ, ANALYZER_PROFILE="standalone", PYTHONIOENCODING="utf-8")
    env.pop("SESSION_TIMEBOX", None)
    env.pop("AUTH_REQUIRED", None)
    out = subprocess.run([sys.executable, "-c", code], cwd=_ROOT, env=env,
                         capture_output=True, text=True, encoding="utf-8")
    line = next((l for l in out.stdout.splitlines() if l.startswith("@@")), "")
    check("standalone requires no login and self-stops never",
          line, "@@False,False")


def main() -> int:
    test_shipped_default_is_a_hash()
    test_hashing()
    test_gate()
    test_login_and_logout()
    test_password_change()
    test_change_password_needs_a_session()
    test_rate_limit()
    test_keep_default_is_recorded_but_not_hardening()
    test_lifecycle()
    test_standalone_is_untouched()

    print("\n" + "=" * 66)
    if _FAILURES:
        print("FAILURES:")
        for failure in _FAILURES:
            print(" - " + failure)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
