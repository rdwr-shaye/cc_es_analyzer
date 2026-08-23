"""The System dashboard's endpoints.

One rule governs the whole file: **a check that could not run reports `unknown`,
never `ok`.** Every handler catches its own failures and puts the reason in the
pane rather than raising, so one dead backend degrades one tile instead of
blanking the screen — and so nobody is ever shown a green box that means "we
could not look".

Everything here is read-only. The remediation endpoints the UI shows as disabled
buttons do not exist in this pass; see modules/system/__init__.py for the
capabilities that describe them and what turning one on would take.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from config import settings
from core import hostexec, policy
from modules.system import checks, safety

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/system", tags=["system"])
# Split so the log endpoints can be gated on their own capability: seeing that
# a container is unhealthy and reading what it said are different permissions,
# and a deployment may well want the first without the second.
logs_router = APIRouter(prefix="/api/system", tags=["system"])
# Registered ONLY when system.storage.delete is unlocked. In every other
# deployment this path does not exist — absent from the OpenAPI schema, 404 to
# a direct call — which is a materially different claim from a hidden button.
delete_router = APIRouter(prefix="/api/system", tags=["system"])
# Taking a copy of a file OFF the appliance is its own capability, separate
# from reading a container log and separate from deleting. It ships on, because
# "keep the log before you clear the disk" is the normal way to do this job —
# but it is named and switchable, because moving customer data off a customer's
# CC is a data-residency question somebody may eventually want to answer no to.
download_router = APIRouter(prefix="/api/system", tags=["system"])


# ── Running a host operation without letting it break the page ───────────────

def _host(op: str, **args) -> tuple[str, str]:
    """(stdout, error). Never raises — the caller puts `error` in its pane."""
    try:
        result = hostexec.run_op(op, **args)
    except hostexec.HostExecError as exc:
        return "", str(exc)
    except Exception as exc:                       # noqa: BLE001
        logger.exception("[system] %s failed", op)
        return "", f"{op} failed: {exc}"

    if result["rc"] != 0 and not result["stdout"].strip():
        detail = (result["stderr"] or "").strip().splitlines()
        return "", (detail[0] if detail else f"{op} exited {result['rc']}")
    return result["stdout"], ""


def _in_context(fn):
    """Run `fn` in a worker thread with THIS request's context copied in.

    The ES client and the SSH target it carries are resolved from a ContextVar
    set by session_middleware (see modules/es/client.set_session). A plain
    thread starts with an empty context, so the worker would silently resolve
    the default client and report on the wrong CC — or on none. Copying the
    context is what makes the parallel fan-out below safe.
    """
    ctx = contextvars.copy_context()
    return lambda: ctx.run(fn)


# ── Panes ────────────────────────────────────────────────────────────────────

def _containers() -> dict:
    text, error = _host("compose.ps")
    if error:
        pane = {"severity": checks.UNKNOWN, "headline": error,
                "total": 0, "problems": 0}
        return {**pane, "error": error, "rows": [], "expected": []}
    rows = checks.parse_compose_ps(text)

    # What the compose file says SHOULD be here, filtered by this CC's
    # COMPOSE_PROFILES. Asked separately because `ps` can only describe
    # containers that exist, and the interesting failure is the one that does
    # not: a service with no container is invisible to `ps` at any flag.
    #
    # A host agent too old to know this operation refuses it, and the check
    # degrades to "what is running" rather than failing — the reconciliation
    # is an improvement on the answer, not a precondition for having one.
    expected_text, expected_error = _host("compose.expected")
    expected = [s.strip() for s in (expected_text or "").splitlines()
                if s.strip() and not s.startswith(" ")]
    if not expected_error:
        rows = checks.reconcile_compose(rows, expected)

    pane = checks.containers_pane(rows, expected)
    return {**pane, "error": "", "rows": rows,
            "expected": expected,
            # Not a failure — say it out loud so a stale agent presents as a
            # weaker check rather than as a CC that has nothing missing.
            "expected_error": expected_error}


def _storage() -> dict:
    text, error = _host("disk.usage")
    if error:
        pane = {"severity": checks.UNKNOWN, "headline": error,
                "total": 0, "problems": 0}
        return {**pane, "error": error, "rows": []}
    rows = checks.parse_df(text)
    pane = checks.storage_pane(rows, settings.disk_warn_pct,
                               settings.disk_crit_pct)
    return {**pane, "error": "", "rows": rows,
            "warn_pct": settings.disk_warn_pct,
            "crit_pct": settings.disk_crit_pct}


def _elasticsearch() -> dict:
    from modules.es.client import get_client
    try:
        rows = get_client().cat_indices()
    except Exception as exc:                       # noqa: BLE001
        # Not connected is the ordinary case standalone, so the TILE says that
        # in four words. urllib3's "HTTPConnectionPool(host=…) Max retries
        # exceeded … NewConnectionError(…)" is the whole story and none of the
        # point; it stays in `error` for the drilldown, where someone is
        # actually debugging the connection rather than glancing at a tile.
        return {**checks.es_pane({}, error="Elasticsearch is not reachable"),
                "error": str(exc), "red": [], "yellow": [], "expected_yellow": []}

    result = checks.es_indices_health(rows)
    return {**checks.es_pane(result), "error": "",
            "red": result["red"], "yellow": result["yellow"],
            "expected_yellow": result["expected_yellow"],
            "total_indices": len(rows)}


def _mariadb() -> dict:
    text, error = _host("maria.check")
    if error:
        return {**checks.maria_pane([], error=error), "error": error,
                "tables": [], "corrupt": []}
    tables = checks.parse_mariadb_check(text)
    corrupt = [t for t in tables if t["corrupt"]]
    return {**checks.maria_pane(tables), "error": "",
            "corrupt": corrupt, "checked": len(tables)}


@router.get("/summary")
def summary():
    """Everything the landing page needs, in one call.

    The four checks are independent and three of them cost a round trip to the
    host, so they run in parallel — sequentially this is the slowest screen in
    the app, and a landing page that takes five seconds is one people stop
    opening.
    """
    started = time.time()
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            "containers": pool.submit(_in_context(_containers)),
            "storage": pool.submit(_in_context(_storage)),
            "elasticsearch": pool.submit(_in_context(_elasticsearch)),
            "mariadb": pool.submit(_in_context(_mariadb)),
        }
        panes = {}
        for name, future in futures.items():
            try:
                panes[name] = future.result()
            except Exception as exc:               # noqa: BLE001
                logger.exception("[system] pane %s failed", name)
                panes[name] = {"severity": checks.UNKNOWN, "headline": str(exc),
                               "error": str(exc), "total": 0, "problems": 0}

    # The two database checks share a tile on the dashboard — an engineer asks
    # "are the databases healthy", not "is OpenSearch healthy and separately is
    # MariaDB healthy" — but keep both underneath so the drilldown can separate
    # them again.
    databases = {
        "severity": checks.worst(panes["elasticsearch"]["severity"],
                                 panes["mariadb"]["severity"]),
        "headline": _databases_headline(panes["elasticsearch"], panes["mariadb"]),
        "elasticsearch": panes["elasticsearch"],
        "mariadb": panes["mariadb"],
    }

    # FULL panes, rows and all — not just the tile headline.
    #
    # The screen used to fetch /summary for the tiles and then /containers for
    # the table underneath them. Two calls, two independent round trips to the
    # host, two `docker compose ps` runs at different moments — so a service
    # that started between them made the tile say "1 of 36 need attention" over
    # a table showing all 36 running. Both were true when they were measured,
    # which is the worst kind of wrong: nothing looks broken, the screen just
    # contradicts itself.
    #
    # One call, one measurement, one truth. The payload is a few kilobytes.
    return {
        "state": checks.worst(panes["containers"]["severity"],
                              panes["storage"]["severity"],
                              databases["severity"]),
        "panes": {
            "containers": panes["containers"],
            "storage": panes["storage"],
            "databases": databases,
        },
        "hostexec": hostexec.backend(),
        "profile": policy.profile(),
        "checked_at": time.time(),
        "took_ms": int((time.time() - started) * 1000),
    }


def _databases_headline(es: dict, maria: dict) -> str:
    parts = []
    for label, pane in (("Elasticsearch", es), ("MariaDB", maria)):
        if pane["severity"] != checks.OK:
            parts.append(f"{label}: {pane['headline']}")
    if parts:
        return " · ".join(parts)
    return "Elasticsearch and MariaDB are both healthy"


# ── Drilldowns ───────────────────────────────────────────────────────────────

@router.get("/hostexec")
def host_backend():
    """How this instance reaches the CC host, and what to do when it cannot.

    Its own endpoint because "the dashboard is blank" and "the dashboard says
    everything is fine" have completely different causes, and the operator needs
    to be able to tell which one they are looking at.
    """
    info = hostexec.backend()
    return {**info, "profile": policy.profile(),
            "spool": settings.hostexec_dir if policy.profile() == policy.EMBEDDED else "",
            "operations": sorted(hostexec.OPS)}


@router.get("/containers")
def containers():
    return _containers()


@router.get("/storage")
def storage():
    return _storage()


@router.get("/databases")
def databases():
    """Both stores' detail, side by side — the drilldown behind the one tile."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        es_future = pool.submit(_in_context(_elasticsearch))
        maria_future = pool.submit(_in_context(_mariadb))
        es, maria = es_future.result(), maria_future.result()
    return {"elasticsearch": es, "mariadb": maria,
            "severity": checks.worst(es["severity"], maria["severity"])}


# ── The largest-files walk, as a job ─────────────────────────────────────────
# `find -xdev` over a filesystem is 11 seconds on a lightly-used lab CC and
# minutes on the full one an engineer is actually looking at — which is the only
# time anyone asks. Too long to hold a request open, so it runs in a thread and
# the client polls. A small local registry rather than the ES module's engine:
# reaching across modules for thirty lines would couple two things the whole
# module layout exists to keep apart.

_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
_KEEP_JOBS = 10


@router.get("/storage/largest")
def largest_files(mount: str = Query(...), n: int = Query(default=20)):
    """Start the walk. Returns a job id to poll."""
    try:
        args = hostexec.validate("disk.largest", {"mount": mount, "n": n})
    except hostexec.HostExecError as exc:
        return {"error": str(exc)}

    # Only a filesystem `df` actually reported. Not because the validator is
    # weak — it is a strict allowlist — but because "biggest files on a mount
    # that is not a mount" is a question with a misleading answer: find would
    # happily walk an ordinary directory and report sizes that have nothing to
    # do with the filesystem the operator is trying to empty.
    text, error = _host("disk.usage")
    if error:
        return {"error": error}
    known = {row["mount"] for row in checks.parse_df(text)}
    if args["mount"] not in known:
        return {"error": f"{args['mount']} is not one of this host's "
                         f"filesystems ({', '.join(sorted(known))})"}

    job_id = uuid.uuid4().hex[:12]
    job = {"id": job_id, "mount": args["mount"], "n": args["n"],
           "status": "running", "started_at": time.time(),
           "files": [], "error": ""}
    with _JOBS_LOCK:
        _JOBS[job_id] = job
        for old in sorted((j for j in _JOBS.values() if j["status"] != "running"),
                          key=lambda j: j["started_at"])[:-_KEEP_JOBS or None]:
            if len(_JOBS) > _KEEP_JOBS:
                _JOBS.pop(old["id"], None)

    threading.Thread(target=_in_context(lambda: _run_largest(job)),
                     daemon=True, name=f"largest-{job_id}").start()
    return {"job": job_id, "mount": job["mount"], "status": "running"}


def _run_largest(job: dict) -> None:
    text, error = _host("disk.largest", mount=job["mount"], n=job["n"])
    if error:
        job["error"] = error
        job["status"] = "error"
    else:
        # Every row carries its own verdict and the reason for it. Classified
        # HERE rather than in the browser so the answer is the same one the
        # server would enforce, and so the reason next to a 952 MB file the
        # engineer cannot delete is specific ("it is an OpenSearch index file")
        # rather than a shrug.
        job["files"] = safety.annotate(checks.parse_largest(text))
        job["status"] = "done"
    job["finished_at"] = time.time()
    logger.info("[system] largest files on %s: %s in %.1fs", job["mount"],
                job["status"], job["finished_at"] - job["started_at"])


@router.get("/storage/largest/{job_id}")
def largest_files_job(job_id: str):
    job = _JOBS.get(job_id)
    if job is None:
        return {"error": "no such job — it may have expired; run the scan again"}
    return {k: v for k, v in job.items()}


# ── Container logs ───────────────────────────────────────────────────────────
# Gated on system.logs rather than system.health: noticing a container is
# unhealthy and reading what it said are different permissions.

@download_router.get("/storage/download")
def download_file(path: str = Query(...)):
    """Stream one log, heap dump or zip off the CC.

    Same allowlist as deletion, and for a reason worth stating: the files an
    engineer may take a copy of are exactly the files they may remove. The pair
    is what makes the delete button safe to use at all — "keep it, then clear
    the disk" instead of "hope nobody needed that".

    Streamed in chunks pulled through the host agent rather than read directly,
    because the container cannot see the host filesystem and giving it a
    read-only mount of the whole root — the obvious shortcut, and something
    node-exporter already does on this appliance — would hand it every secret
    on the box to save a loop.
    """
    verdict = safety.classify(path)
    if not verdict["deletable"]:
        return PlainTextResponse(
            f"{path} cannot be downloaded from here — {verdict['reason']}\n",
            status_code=403)

    import base64
    import json as _json

    CHUNK = 8 * 1024 * 1024

    def pull():
        offset = 0
        while True:
            try:
                result = hostexec.run_op("file.read", path=path,
                                         offset=offset, length=CHUNK)
            except hostexec.HostExecError as exc:
                logger.warning("[system] download of %s stopped: %s", path, exc)
                # Mid-stream there is no status code left to change, so the
                # only honest thing is to put the reason in the file itself
                # rather than let it end early and look complete.
                yield f"\n--- download interrupted: {exc} ---\n".encode()
                return
            if result["rc"] != 0:
                yield f"\n--- download failed: {result['stderr'].strip()} ---\n".encode()
                return
            part = _json.loads(result["stdout"] or "{}")
            data = base64.b64decode(part.get("data") or "")
            if data:
                yield data
            offset += len(data)
            if part.get("eof") or not data:
                return

    name = path.rsplit("/", 1)[-1] or "download"
    logger.info("[system] downloading %s", path)
    return StreamingResponse(pull(), media_type="application/octet-stream",
                             headers={"Content-Disposition":
                                      f'attachment; filename="{name}"'})


class DeleteRequest(BaseModel):
    path: str


@delete_router.post("/storage/delete")
def delete_file(req: DeleteRequest):
    """Remove one log, heap dump or zip to reclaim disk space.

    POST rather than DELETE with the path in the URL: a filesystem path in a
    URL has to survive two layers of encoding through nginx, and a path that
    arrives subtly different from the one shown on screen is the last thing
    this endpoint should tolerate.

    The safety rule is applied THREE times and they are independent: the UI
    greys out what it will not offer, this route refuses what it will not ask
    for, and deploy/host_agent.py refuses again on the host before touching the
    disk. Only the last one is binding — it is outside the container — but the
    first two mean a mistake shows up as a refusal here rather than as a
    request the host has to be trusted to decline.
    """
    verdict = safety.classify(req.path)
    if not verdict["deletable"]:
        return {"error": f"{req.path} cannot be deleted from here — "
                         f"{verdict['reason']}"}

    try:
        result = hostexec.run_op("file.delete", path=req.path)
    except hostexec.HostExecError as exc:
        return {"error": str(exc)}

    if result["rc"] != 0:
        return {"error": (result["stderr"] or "").strip() or
                         f"the host refused to delete {req.path}"}

    import json as _json
    try:
        detail = _json.loads(result["stdout"] or "{}")
    except ValueError:
        detail = {}
    freed = int(detail.get("bytes") or 0)
    logger.warning("[system] DELETED %s (%s bytes)", req.path, freed)
    return {
        "deleted": req.path,
        "bytes": freed,
        # An open file's space does not come back until the last handle closes.
        # An engineer who deletes a 2 GB log, sees `df` unchanged and concludes
        # the tool is broken has been failed by the tool, not by Linux.
        "was_open": bool(detail.get("was_open")),
    }


@logs_router.get("/containers/{name}/logs")
def container_logs(name: str, lines: int = Query(default=500)):
    text, error = _host("container.logs", name=name, lines=lines)
    if error:
        return {"error": error, "name": name}
    return {"name": name, "lines": lines, "log": text,
            "truncated": "truncated at" in text[-200:]}


@logs_router.get("/containers/{name}/logs/download")
def download_container_logs(name: str, lines: int = Query(default=5000)):
    """The same log as a file. The point of the whole containers drilldown is
    that a support engineer can attach this to a ticket in one click instead of
    talking a customer through `docker logs` on the phone."""
    text, error = _host("container.logs", name=name, lines=lines)
    if error:
        return PlainTextResponse(f"could not read the log for {name}: {error}\n",
                                 status_code=502)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    # `name` reached here through hostexec.validate's allowlist, so it cannot
    # carry a quote or a newline into the header.
    return PlainTextResponse(text, headers={
        "Content-Disposition": f'attachment; filename="{name}-{stamp}.log"'})
