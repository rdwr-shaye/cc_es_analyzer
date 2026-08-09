"""
"Is there a newer version of this tool, and can I install it from here?"

The analyzer is deployed by cloning the repo onto a host and running
`docker compose up -d`, so an update is a `git pull` + a container rebuild.
Neither of those can happen inside the container — it has no checkout and no
docker socket — so the update path depends on what the running instance can
actually see. Three modes, detected at runtime:

  agent  — the normal Docker deployment. deploy/update_agent.sh runs ON the host
           next to the checkout, does the `git fetch` itself (using the same git
           credentials that cloned the repo — no API token to configure here)
           and writes what it found into a small directory that is bind-mounted
           into the container. Applying an update = dropping a request file in
           that directory; the agent pulls, rebuilds and restarts the container.

  git    — the app is running straight from a checkout (`python main.py`), so it
           can run git itself. Fetch/compare and a fast-forward merge happen
           in-process; the process still has to be restarted to load the new
           code (automatic under --reload).

  api    — no checkout and no agent (e.g. an image someone copied). Then the
           only thing possible is ASKING Bitbucket over its REST API, which
           needs credentials in .env. Check-only: nothing here can install.

Everything is best-effort and non-fatal: if the check cannot run, the UI simply
shows why. Nothing about the update path can execute arbitrary commands — the
agent only ever fast-forwards the configured branch of the configured remote.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid

from config import settings

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION_FILE = os.path.join(PROJECT_ROOT, "VERSION")

# Files exchanged with the host agent (see deploy/update_agent.sh).
AGENT_MARKER = "agent.json"      # written by the agent at startup: repo, branch…
AGENT_STATE  = "state.json"      # what the last `git fetch` found
AGENT_JOB    = "job.json"        # progress of the update currently running
AGENT_REQUEST = "request.json"   # written by US to ask for an update

_CHECK_TTL_S = 6 * 3600          # how long a check result stays fresh
_GIT_TIMEOUT_S = 90

_lock = threading.Lock()
_cache: dict = {}                 # last computed status
_checking = False


# ── Version helpers ───────────────────────────────────────────────────────────

def _version_tuple(v: str):
    parts = re.findall(r"\d+", v or "")
    return tuple(int(p) for p in parts[:4]) if parts else ()


def is_newer(remote: str, local: str) -> bool:
    """True when `remote` is a strictly higher version than `local`."""
    rt, lt = _version_tuple(remote), _version_tuple(local)
    if rt and lt:
        return rt > lt
    return bool(remote) and bool(local) and remote != local


def local_version() -> str:
    try:
        with open(VERSION_FILE, "r", encoding="utf-8") as fh:
            return fh.read().strip() or "0.0.0"
    except OSError:
        # Loud on purpose: a missing VERSION makes the app report 0.0.0, which
        # looks like a downgrade and makes every remote version seem newer.
        logger.warning("[update] no VERSION file at %s — reporting 0.0.0. If this "
                       "is a container, VERSION is missing from the image.",
                       VERSION_FILE)
        return "0.0.0"


# ── Shelling out to git ───────────────────────────────────────────────────────

def _git(repo: str, *args: str, timeout: int = _GIT_TIMEOUT_S):
    """Run git in `repo`. Never prompts — a credential prompt would hang the
    request thread, so an auth failure is reported as an error instead."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    try:
        p = subprocess.run(["git", "-C", repo, *args], capture_output=True,
                           text=True, timeout=timeout, env=env)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except FileNotFoundError:
        return 127, "", "git is not installed"
    except subprocess.TimeoutExpired:
        return 124, "", f"git {' '.join(args)} timed out after {timeout}s"


def _git_available(repo: str) -> bool:
    if not os.path.isdir(os.path.join(repo, ".git")):
        return False
    rc, _, _ = _git(repo, "rev-parse", "--git-dir", timeout=10)
    return rc == 0


def _upstream(repo: str) -> tuple[str, str]:
    """(remote, branch) this checkout tracks, defaulting to origin/<current>."""
    rc, out, _ = _git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", timeout=10)
    if rc == 0 and "/" in out:
        remote, branch = out.split("/", 1)
        return remote, branch
    rc, branch, _ = _git(repo, "rev-parse", "--abbrev-ref", "HEAD", timeout=10)
    return "origin", (branch if rc == 0 and branch else "main")


def _log_entries(repo: str, rev_range: str, path: str = "") -> list[dict]:
    args = ["log", "--no-merges", "-n", "20", "--date=iso-strict",
            "--pretty=%h%x1f%ad%x1f%s", rev_range]
    if path:
        args += ["--", path]
    rc, out, _ = _git(repo, *args)
    entries = []
    if rc == 0:
        for line in out.splitlines():
            bits = line.split("\x1f")
            if len(bits) == 3:
                entries.append({"hash": bits[0], "date": bits[1], "message": bits[2]})
    return entries


# ── Mode: the app runs from a git checkout ────────────────────────────────────

def _check_git(repo: str) -> dict:
    remote, branch = _upstream(repo)
    rc, _, err = _git(repo, "fetch", "--quiet", remote, branch)
    if rc != 0:
        return {"ok": False, "error": f"git fetch failed: {err or rc}",
                "remote": remote, "branch": branch}
    ref = f"{remote}/{branch}"
    _rc, head, _ = _git(repo, "rev-parse", "--short", "HEAD", timeout=10)
    _rc, latest, _ = _git(repo, "rev-parse", "--short", ref, timeout=10)
    _rc, behind, _ = _git(repo, "rev-list", "--count", f"HEAD..{ref}", timeout=20)
    _rc, remote_version, _ = _git(repo, "show", f"{ref}:VERSION", timeout=20)
    _rc, dirty, _ = _git(repo, "status", "--porcelain", timeout=20)
    _rc, date, _ = _git(repo, "log", "-1", "--date=iso-strict", "--pretty=%ad", ref, timeout=20)
    return {
        "ok": True, "error": "", "repo": repo, "remote": remote, "branch": branch,
        "local": {"version": local_version(), "commit": head},
        "latest": {"version": (remote_version or "").strip() or local_version(),
                   "commit": latest, "date": date},
        "behind": int(behind) if behind.isdigit() else 0,
        "dirty": bool(dirty.strip()),
        # "XY path" — split on whitespace rather than slicing, because the
        # leading status column may already have been stripped.
        "dirty_files": [l.split(maxsplit=1)[-1] for l in dirty.splitlines()[:10] if l.strip()],
        "changes": _log_entries(repo, f"HEAD..{ref}"),
    }


def _apply_git(repo: str) -> dict:
    """Fast-forward the checkout. Refuses anything that isn't a clean FF."""
    remote, branch = _upstream(repo)
    rc, _, err = _git(repo, "fetch", "--quiet", remote, branch)
    if rc != 0:
        return {"ok": False, "error": f"git fetch failed: {err or rc}"}
    rc, dirty, _ = _git(repo, "status", "--porcelain", timeout=20)
    if dirty.strip():
        return {"ok": False,
                "error": "the checkout has local modifications — commit, stash or "
                         "discard them first:\n" + dirty}
    rc, out, err = _git(repo, "merge", "--ff-only", f"{remote}/{branch}")
    if rc != 0:
        return {"ok": False, "error": f"fast-forward failed: {err or out}"}
    _rc, head, _ = _git(repo, "rev-parse", "--short", "HEAD", timeout=10)
    return {"ok": True, "output": out, "commit": head}


# ── Mode: a host agent does the git work for us ───────────────────────────────

def agent_dir() -> str:
    return settings.update_dir


def _agent_read(name: str) -> dict | None:
    path = os.path.join(agent_dir(), name)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _agent_present() -> bool:
    info = _agent_read(AGENT_MARKER)
    if not info:
        return False
    # A stale marker from an agent that is no longer running would strand the UI
    # on "waiting for the agent"; treat a long-silent agent as absent.
    beat = info.get("heartbeat") or info.get("started") or 0
    return (time.time() - beat) < max(3600, 4 * int(info.get("interval") or 300))


# ── Mode: ask Bitbucket directly ──────────────────────────────────────────────

def _bitbucket_auth():
    if settings.update_bb_token:
        return {"Authorization": f"Bearer {settings.update_bb_token}"}, None
    if settings.update_bb_user and settings.update_bb_password:
        return {}, (settings.update_bb_user, settings.update_bb_password)
    return {}, None


def _check_api() -> dict:
    import requests
    ws, repo = settings.update_bb_workspace, settings.update_bb_repo
    branch = settings.update_bb_branch
    path = settings.update_bb_path.strip("/")
    if not (ws and repo):
        return {"ok": False, "error": "no update source configured"}
    base = f"https://api.bitbucket.org/2.0/repositories/{ws}/{repo}"
    headers, auth = _bitbucket_auth()
    try:
        vr = requests.get(f"{base}/src/{branch}/{path + '/' if path else ''}VERSION",
                          headers=headers, auth=auth, timeout=20)
        cr = requests.get(f"{base}/commits/{branch}",
                          params={"path": path, "pagelen": 10} if path else {"pagelen": 10},
                          headers=headers, auth=auth, timeout=20)
    except Exception as exc:
        return {"ok": False, "error": f"Bitbucket unreachable: {exc}"}
    if vr.status_code in (401, 403) or cr.status_code in (401, 403):
        return {"ok": False, "error": "Bitbucket rejected the credentials "
                                      "(set UPDATE_BB_TOKEN or UPDATE_BB_USER/PASSWORD in .env)"}
    if vr.status_code != 200:
        return {"ok": False, "error": f"Bitbucket returned HTTP {vr.status_code} for VERSION"}
    latest_version = vr.text.strip()
    changes = []
    latest_commit, latest_date = "", ""
    if cr.status_code == 200:
        for c in (cr.json().get("values") or [])[:10]:
            changes.append({"hash": (c.get("hash") or "")[:9],
                            "date": c.get("date", ""),
                            "message": (c.get("message") or "").strip().split("\n")[0]})
        if changes:
            latest_commit, latest_date = changes[0]["hash"], changes[0]["date"]
    return {
        "ok": True, "error": "", "remote": f"bitbucket:{ws}/{repo}", "branch": branch,
        "local": {"version": local_version(), "commit": ""},
        "latest": {"version": latest_version or local_version(),
                   "commit": latest_commit, "date": latest_date},
        "behind": 0, "dirty": False, "changes": changes,
    }


# ── Public API ────────────────────────────────────────────────────────────────

def mode() -> str:
    """Which update path this instance has: agent | git | api | none."""
    if _agent_present():
        return "agent"
    if _git_available(PROJECT_ROOT):
        return "git"
    if settings.update_bb_workspace and settings.update_bb_repo:
        return "api"
    return "none"


def _raw_check() -> dict:
    m = mode()
    if m == "agent":
        state = _agent_read(AGENT_STATE) or {}
        if not state:
            return {"ok": False, "error": "the host updater has not reported yet",
                    "mode": m}
        return {**state, "mode": m}
    if m == "git":
        return {**_check_git(PROJECT_ROOT), "mode": m}
    if m == "api":
        return {**_check_api(), "mode": m}
    return {"ok": False, "mode": m,
            "error": "no update source: this instance has no checkout, no host "
                     "updater and no Bitbucket credentials"}


def _decorate(raw: dict) -> dict:
    """Add the fields the UI actually renders."""
    local = raw.get("local") or {"version": local_version()}
    latest = raw.get("latest") or {}
    behind = int(raw.get("behind") or 0)
    available = bool(raw.get("ok")) and (
        behind > 0 or is_newer(latest.get("version", ""), local.get("version", "")))
    m = raw.get("mode") or mode()
    can_apply = bool(settings.update_allow_apply) and available and m in ("agent", "git") \
        and not raw.get("dirty")
    why = ""
    if available and not can_apply:
        if not settings.update_allow_apply:
            why = "one-click update is disabled on this server (UPDATE_ALLOW_APPLY=false)"
        elif raw.get("dirty"):
            why = "the checkout on the server has uncommitted local changes"
        elif m == "api":
            why = ("this instance can only check for updates — run the update on the "
                   "host: git pull && docker compose up -d")
        else:
            why = "no update path is available from here"
    return {
        **raw,
        "mode": m,
        "current": local,
        "latest": latest,
        "behind": behind,
        "update_available": available,
        "can_apply": can_apply,
        "cannot_apply_reason": why,
        "checked_at": raw.get("checked_at") or int(time.time()),
    }


def status(max_age_s: int = _CHECK_TTL_S) -> dict:
    """Cached status; refreshes in the background when stale."""
    with _lock:
        cached = dict(_cache) if _cache else None
    fresh = cached and (time.time() - cached.get("checked_at", 0) < max_age_s)
    if fresh:
        return cached
    if cached:
        _check_async()
        return cached
    return check()


def check() -> dict:
    """Run the check now (blocking) and cache it."""
    raw = _raw_check()
    raw.setdefault("checked_at", int(time.time()))
    out = _decorate(raw)
    with _lock:
        _cache.clear()
        _cache.update(out)
    if out.get("update_available"):
        logger.info("[update] newer version available: %s -> %s (%s commits behind, mode=%s)",
                    out["current"].get("version"), out["latest"].get("version"),
                    out.get("behind"), out.get("mode"))
    return out


def _check_async() -> None:
    global _checking
    with _lock:
        if _checking:
            return
        _checking = True

    def _run():
        global _checking
        try:
            check()
        except Exception:
            logger.exception("[update] background check failed")
        finally:
            with _lock:
                _checking = False

    threading.Thread(target=_run, daemon=True, name="update-check").start()


def start_background_checks(interval_s: int = _CHECK_TTL_S) -> None:
    """Check once at startup and then periodically, off the request path."""
    if not settings.update_check_enabled:
        logger.info("[update] update checks disabled (UPDATE_CHECK_ENABLED=false)")
        return

    def _loop():
        while True:
            try:
                check()
            except Exception:
                logger.exception("[update] periodic check failed")
            time.sleep(max(300, interval_s))

    threading.Thread(target=_loop, daemon=True, name="update-poll").start()


# ── Applying ──────────────────────────────────────────────────────────────────

_local_job: dict = {}


def apply(requested_by: str = "") -> dict:
    """Start an update. Returns {ok, job} or {error}."""
    if not settings.update_allow_apply:
        return {"error": "one-click update is disabled on this server."}
    st = status(max_age_s=60)
    if not st.get("update_available"):
        return {"error": "already up to date."}
    if not st.get("can_apply"):
        return {"error": st.get("cannot_apply_reason") or "cannot update from here."}

    m = st.get("mode")
    job_id = uuid.uuid4().hex[:12]
    if m == "agent":
        req = {"id": job_id, "requested_at": int(time.time()),
               "requested_by": requested_by,
               "target": (st.get("latest") or {}).get("commit", "")}
        try:
            os.makedirs(agent_dir(), exist_ok=True)
            tmp = os.path.join(agent_dir(), AGENT_REQUEST + ".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(req, fh)
            os.replace(tmp, os.path.join(agent_dir(), AGENT_REQUEST))
        except OSError as exc:
            return {"error": f"could not reach the host updater: {exc}"}
        logger.warning("[update] update requested by %s (job %s) — the host updater "
                       "will pull and restart the app", requested_by or "a user", job_id)
        return {"ok": True, "job": {"id": job_id, "state": "queued",
                                    "steps": [], "mode": "agent"}}

    # git mode: do it here, in a thread, so the response returns immediately.
    _local_job.clear()
    _local_job.update({"id": job_id, "state": "running", "mode": "git",
                       "started": int(time.time()), "steps": [], "error": ""})

    def _run():
        _local_job["steps"].append({"name": "pull", "status": "running"})
        res = _apply_git(PROJECT_ROOT)
        step = _local_job["steps"][-1]
        step["status"] = "ok" if res.get("ok") else "error"
        step["output"] = res.get("output") or res.get("error", "")
        if not res.get("ok"):
            _local_job.update({"state": "error", "error": res.get("error", ""),
                               "finished": int(time.time())})
            return
        _local_job.update({"state": "done", "finished": int(time.time()),
                           "commit": res.get("commit", ""),
                           "restart_required": True})
        logger.warning("[update] updated to %s — restart the service to load it",
                       res.get("commit", ""))

    threading.Thread(target=_run, daemon=True, name="update-apply").start()
    return {"ok": True, "job": dict(_local_job)}


def job(job_id: str = "") -> dict:
    """Progress of the running/last update."""
    if mode() == "agent":
        j = _agent_read(AGENT_JOB)
        if not j:
            pending = os.path.exists(os.path.join(agent_dir(), AGENT_REQUEST))
            return {"state": "queued" if pending else "idle", "steps": [],
                    "mode": "agent"}
        return {**j, "mode": "agent"}
    return dict(_local_job) if _local_job else {"state": "idle", "steps": []}
