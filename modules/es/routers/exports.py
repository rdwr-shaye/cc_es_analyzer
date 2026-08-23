"""
Server-side index archives.

Exporting a huge index (millions of docs / GBs) through the browser tab fails —
so the BACKEND scrolls Elasticsearch and writes each index to a gzipped CSV
archive in its own exports directory (a Docker volume in container deployments).
The UI polls job progress, offers finished archives for direct download, and can
RESTORE an archive (uploaded from another machine, or already on this server)
into whatever ES the app is connected to — i.e. index transfer between machines.

No root credentials are involved anywhere: the app server itself does the work.
"""
import csv
import gzip
import io
import json
import os
import re
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from config import settings
from modules.es.client import get_client

router = APIRouter(prefix="/api/exports", tags=["exports"])

import logging
logger = logging.getLogger(__name__)

EXPORTS_DIR = settings.exports_dir
os.makedirs(EXPORTS_DIR, exist_ok=True)

# Archive names we are willing to serve/delete/restore (no path traversal).
# .csv/.csv.gz = document archives; .zip = native snapshot archives.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.(csv(\.gz)?|zip)$")

# Snapshot-archive names: become an ES repository name, a directory under the
# fixed SNAP_HOST_DIR, and "<name>.zip" — so the charset is deliberately tight.
_SNAP_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

_SCROLL_PAGE = 2000
_BULK_CHUNK = 1000
_KEEP_FINISHED_JOBS = 20

# ── Job registry ──────────────────────────────────────────────────────────────
# Single-process app → a module dict + lock is sufficient (matches the
# es_client singleton style). Each job runs on a daemon thread.
_JOBS: dict = {}
_JOBS_LOCK = threading.Lock()


def _new_job(kind: str, items: list) -> dict:
    job = {
        "id": uuid.uuid4().hex[:12],
        "kind": kind,                       # "export" | "restore"
        "status": "running",                # running | done | error | cancelled
        "cancelled": False,                 # set by POST /jobs/{id}/cancel
        "error": None,
        "items": items,                     # [{index,total,done,file}]
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
    }
    with _JOBS_LOCK:
        _JOBS[job["id"]] = job
        # Drop the oldest FINISHED jobs beyond the keep limit.
        finished = [j for j in _JOBS.values() if j["status"] != "running"]
        for old in sorted(finished, key=lambda j: j["started_at"])[:-_KEEP_FINISHED_JOBS or None]:
            if len(_JOBS) > _KEEP_FINISHED_JOBS:
                _JOBS.pop(old["id"], None)
    return job


class _JobCancelled(Exception):
    """Raised inside a job loop when its cancel flag has been set."""


def _finish_job(job: dict, error: str | None = None,
                cancelled: bool = False) -> None:
    job["status"] = "cancelled" if cancelled else ("error" if error else "done")
    job["error"] = error
    job["finished_at"] = datetime.now(timezone.utc).isoformat()


def _err_text(exc) -> str:
    """Error message including the ES response body — a bare '500 Server
    Error' from raise_for_status hides the actual reason (bad path.repo,
    unwritable repo dir, version incompatibility, …)."""
    msg = str(exc)
    resp = getattr(exc, "response", None)
    if resp is not None:
        try:
            body = (resp.text or "").strip()
            if body and body not in msg:
                msg += f" — ES said: {body[:400]}"
        except Exception:
            pass
    return msg


def _source_host(es) -> str:
    """Host of the ES machine a job reads from (for the archive source tag)."""
    try:
        return urlsplit(es.base_url).hostname or ""
    except Exception:
        return ""


def _running_export_indices() -> set:
    with _JOBS_LOCK:
        return {it["index"]
                for j in _JOBS.values()
                if j["kind"] == "export" and j["status"] == "running"
                for it in j["items"]}


# ── Export ────────────────────────────────────────────────────────────────────

class ExportRequest(BaseModel):
    indices: list[str]


@router.post("")
def start_export(req: ExportRequest):
    """Start a background job archiving each index to `<name>.csv.gz` —
    ALL documents, scrolled server-side (never through the browser)."""
    names = [n.strip() for n in req.indices if n and n.strip()]
    if not names:
        return {"error": "no indices given"}
    for n in names:
        if any(ch in n for ch in "*?,/\\"):
            return {"error": f"invalid index name {n!r} — pass exact names, no wildcards"}
        if not _SAFE_NAME.match(f"{n}.csv.gz"):
            return {"error": f"index name {n!r} cannot be used as an archive filename"}
    busy = _running_export_indices() & set(names)
    if busy:
        return {"error": f"already being exported: {', '.join(sorted(busy))}"}

    # Capture the ES client ONCE — the job keeps talking to the cluster it
    # started on even if another user switches the app's connection mid-run.
    try:
        es = get_client()
    except Exception as exc:
        return {"error": str(exc)}

    items = [{"index": n, "total": None, "done": 0, "file": f"{n}.csv.gz"} for n in names]
    job = _new_job("export", items)
    threading.Thread(target=_run_export_job, args=(job, es), daemon=True,
                     name=f"export-{job['id']}").start()
    logger.info("[exports] job %s started for %s", job["id"], names)
    return {"job_id": job["id"]}


def _run_export_job(job: dict, es) -> None:
    from modules.es.routers.query import _scroll_hits, _collect_top_fields, _csv_cell
    source = _source_host(es)
    try:
        for item in job["items"]:
            if job["cancelled"]:
                raise _JobCancelled()
            index = item["index"]
            # Total docs (for the progress bar).
            try:
                resp = es.search(index, {"size": 0, "query": {"match_all": {}}})
                total = resp.get("hits", {}).get("total")
                item["total"] = total.get("value") if isinstance(total, dict) else total
            except Exception:
                item["total"] = None

            cols = ["_id", "_index"]
            seen = set(cols)
            for f in _collect_top_fields(es, index):
                if f not in seen:
                    seen.add(f)
                    cols.append(f)

            final = os.path.join(EXPORTS_DIR, item["file"])
            part = f"{final}.{job['id']}.part"        # job-scoped → no cross-job races
            try:
                with gzip.open(part, "wt", encoding="utf-8", newline="") as fh:
                    # Source tag rides inside the file so it survives a
                    # download → upload transfer to another machine.
                    fh.write(f"#cc-es-archive source={source} "
                             f"exported={datetime.now(timezone.utc).isoformat()}\r\n")
                    w = csv.writer(fh, lineterminator="\r\n")
                    w.writerow(cols)
                    for h in _scroll_hits(es, index, {"match_all": {}}, page=_SCROLL_PAGE):
                        if job["cancelled"]:
                            raise _JobCancelled()
                        row = {"_id": h.get("_id"), "_index": h.get("_index"),
                               **(h.get("_source") or {})}
                        w.writerow([_csv_cell(row.get(c)) for c in cols])
                        item["done"] += 1
                os.replace(part, final)               # atomic: complete or absent
                logger.info("[exports] %s: wrote %s docs to %s",
                            job["id"], item["done"], item["file"])
            except Exception:
                try:
                    os.remove(part)
                except OSError:
                    pass
                raise
        _finish_job(job)
    except _JobCancelled:
        # Archives of indices that finished before the cancel are kept.
        logger.info("[exports] job %s cancelled by user", job["id"])
        _finish_job(job, cancelled=True)
    except Exception as exc:
        logger.error("[exports] job %s failed: %s", job["id"], exc)
        _finish_job(job, error=str(exc))


# ── Restore ───────────────────────────────────────────────────────────────────

@router.post("/restore")
async def start_restore(file: UploadFile | None = File(default=None),
                        filename: str = Form(default=""),
                        target: str = Form(default=""),
                        id_column: str = Form(default="_id"),
                        generate_ids: bool = Form(default=False)):
    """Restore an archive into the currently-connected ES.

    Give EITHER an uploaded .csv/.csv.gz file (the cross-machine 'upload' flow)
    OR `filename` of an archive already in this server's exports directory.
    `target` = index to restore into (default: the archive's name stem).

    `id_column` decides each document's id: `_id` (default) reuses the archived
    id so a re-restore overwrites; any other column takes the id from that field
    (e.g. `attackIpsId`). Set `generate_ids` to send no id at all and let ES
    assign one — a separate flag because FastAPI resolves an empty-string Form
    value to the field's default, so `id_column=""` can never reach us.
    """
    from modules.es.routers.indices import _valid_index_name

    if file is not None and file.filename:
        name = os.path.basename(file.filename)
        if not _SAFE_NAME.match(name):
            return {"error": f"unsupported archive name {name!r} "
                             f"(expected .csv, .csv.gz or .zip)"}
        # Stream the upload to disk (uploads can be huge — never fully in memory).
        path = os.path.join(EXPORTS_DIR, name)
        try:
            with open(path, "wb") as out:
                while chunk := await file.read(1 << 20):
                    out.write(chunk)
        except Exception as exc:
            return {"error": f"could not save upload: {exc}"}
        if name.endswith(".zip"):
            # Snapshot archives are only SAVED here; restoring one needs the
            # SSH-credentials handshake → the UI follows up with
            # POST /api/exports/snapshot/restore. Validate now so a broken or
            # non-snapshot zip is rejected at upload time, not mid-restore.
            report = validate_snapshot_zip(path)
            if not report["ok"]:
                try:
                    os.remove(path)          # don't keep an unusable archive
                except OSError:
                    pass
                return {"error": "this zip is not a restorable ES snapshot: "
                                 + "; ".join(report["errors"]),
                        "validation": report}
            logger.info("[exports] uploaded snapshot %s validated: %s entries, "
                        "integrity %s, %s indices", name, report["entries"],
                        report["integrity"], len(report["indices"]))
            return {"saved": name, "type": "snapshot", "validation": report}
    elif filename:
        name = os.path.basename(filename)
        if not _SAFE_NAME.match(name):
            return {"error": "invalid archive name"}
        if name.endswith(".zip"):
            return {"error": "snapshot archives are restored via "
                             "/api/exports/snapshot/restore"}
        path = os.path.join(EXPORTS_DIR, name)
        if not os.path.isfile(path):
            return {"error": f"archive {name!r} not found on this server"}
    else:
        return {"error": "provide an archive file or a server-side filename"}

    tgt = (target or "").strip() or re.sub(r"\.csv(\.gz)?$", "", name)
    ok, reason = _valid_index_name(tgt)
    if not ok:
        return {"error": f"target index: {reason}"}

    try:
        es = get_client()                     # captured once — see export note
    except Exception as exc:
        return {"error": str(exc)}

    id_col = "" if generate_ids else (id_column or "").strip()
    job = _new_job("restore", [{"index": tgt, "total": None, "done": 0, "file": name}])
    threading.Thread(target=_run_restore_job, args=(job, es, path, tgt, id_col),
                     daemon=True, name=f"restore-{job['id']}").start()
    logger.info("[exports] restore job %s: %s -> index %r (id from %s)",
                job["id"], name, tgt, id_col or "ES (auto-generated)")
    return {"job_id": job["id"], "target": tgt, "id_column": id_col or None}


def _run_restore_job(job: dict, es, path: str, target: str,
                     id_col: str = "_id") -> None:
    from modules.es.routers.indices import _coerce_cell, _flush_batch, _MISSING
    item = job["items"][0]
    meta_cols = {"_id", "_index"}
    try:
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8", newline="") as fh:
            reader = csv.reader(fh)
            try:
                headers = [h.strip() for h in next(reader)]
                # Skip metadata comment lines (e.g. "#cc-es-archive source=…")
                # written before the real CSV header by the export job.
                while headers and headers[0].startswith("#"):
                    headers = [h.strip() for h in next(reader)]
            except StopIteration:
                raise ValueError("archive has no rows")
            if id_col and id_col not in headers:
                raise ValueError(f"id column {id_col!r} is not in the archive "
                                 f"header (columns: "
                                 f"{', '.join(h for h in headers if h) or 'none'})")

            batch: list = []
            failed = 0
            errors: list = []

            def flush(last: bool) -> None:
                nonlocal failed
                if not batch:
                    return
                ok_n, errs = _flush_batch(es, target, batch, refresh=last)
                item["done"] += ok_n
                failed += len(errs)
                for e in errs:
                    if len(errors) < 5:
                        errors.append(e)
                batch.clear()

            for row in reader:
                if job["cancelled"]:
                    raise _JobCancelled()
                if not row or all(c == "" for c in row):
                    continue
                doc_id = ""
                source: dict = {}
                for i, col in enumerate(headers):
                    if not col or i >= len(row):
                        continue
                    if id_col and col == id_col:
                        doc_id = row[i].strip()
                    if col in meta_cols:
                        continue
                    val = _coerce_cell(row[i])
                    if val is not _MISSING:
                        source[col] = val
                batch.append((doc_id, source))
                if len(batch) >= _BULK_CHUNK:
                    flush(last=False)
            flush(last=True)

        if failed:
            _finish_job(job, error=f"{failed} doc(s) failed to index "
                                   f"(first errors: {'; '.join(errors)})")
        else:
            _finish_job(job)
        logger.info("[exports] restore %s: %s docs into %r (failed=%s)",
                    job["id"], item["done"], target, failed)
    except _JobCancelled:
        # Docs already bulk-flushed stay in the target index.
        logger.info("[exports] restore %s cancelled by user (%s docs kept in %r)",
                    job["id"], item["done"], target)
        _finish_job(job, cancelled=True)
    except Exception as exc:
        logger.error("[exports] restore job %s failed: %s", job["id"], exc)
        _finish_job(job, error=str(exc))


# ── Snapshot archives (native ES/OpenSearch snapshots over SSH) ───────────────
# FAST path for huge indices: ES itself writes the snapshot to its repository
# dir on the ES machine, we zip that dir and SFTP the zip into EXPORTS_DIR.
# Restore reverses it on the machine of the currently-connected ES.

class SnapshotSSH(BaseModel):
    user: str = "root"
    password: str = ""
    remember: bool = True


class SnapshotRequest(BaseModel):
    indices: list[str]
    name: str
    ssh: SnapshotSSH | None = None


class SnapshotRestoreRequest(BaseModel):
    filename: str
    ssh: SnapshotSSH | None = None
    # Restore only these indices; empty = every index in the snapshot.
    indices: list[str] = []


def _resolve_creds(host: str, ssh: SnapshotSSH | None):
    """Stored or freshly-supplied SSH credentials for *host*; None → the UI
    must prompt (need_credentials handshake)."""
    from core.remote import cred_store
    if ssh is not None and ssh.password:
        if ssh.remember:
            cred_store.save(host, ssh.user or "root", ssh.password)
        return {"user": ssh.user or "root", "password": ssh.password}
    return cred_store.get(host)


def _es_call(jid: str, es, method: str, path: str, body=None, params=None):
    """ES REST call with full request/response debug logging (snapshot flows)."""
    logger.info("[exports %s] ES %s %s%s", jid, method.upper(), path,
                f" body={json.dumps(body)[:400]}" if body is not None else "")
    try:
        if method == "get":
            resp = es.get(path, params=params)
        elif method == "delete":
            resp = es.delete(path)
        else:
            resp = getattr(es, method)(path, body)
        logger.info("[exports %s] ES %s %s -> %s", jid, method.upper(), path,
                    json.dumps(resp)[:600])
        return resp
    except Exception as exc:
        logger.error("[exports %s] ES %s %s FAILED: %s", jid, method.upper(),
                     path, _err_text(exc))
        raise


def _log_fs(jid: str, ssh, path: str, note: str) -> None:
    """Log numeric owner/group/permissions of a remote path tree (debugging)."""
    try:
        listing = ssh.run(f"ls -lnRa {path} 2>/dev/null | head -60; "
                          f"stat -c '%n %U(%u):%G(%g) %a' {path} 2>/dev/null")
        logger.info("[exports %s] FS on %s (%s):\n%s", jid, ssh.host, note,
                    listing.strip()[:2000])
    except Exception as exc:
        logger.warning("[exports %s] FS listing of %s failed: %s", jid, path, exc)


def _snap_paths(name: str) -> dict:
    """Every remote path used by the flows — always under the fixed base dirs."""
    return {
        "host_dir": f"{settings.snap_host_dir}/{name}",
        "host_zip": f"{settings.snap_host_dir}/{name}.zip",
        "host_meta": f"{settings.snap_host_dir}/{name}.cc-meta.json",
        "es_location": f"{settings.snap_es_dir}/{name}",
    }


def _snap_cleanup(name: str, es=None, ssh=None) -> bool:
    """Removal of the snapshot, repository and host-side files.

    ES refuses to delete a snapshot/repository that a restore (or snapshot) is
    still using — retry those deletes, and NEVER remove the files on disk while
    ES still holds the repo: deleting them under an in-progress restore kills
    the recovering shards and leaves the indices red. Returns True when
    everything (that was requested) got cleaned."""
    p = _snap_paths(name)
    es_ok = True
    if es is not None:
        for path in (f"/_snapshot/{name}/{name}", f"/_snapshot/{name}"):
            ok = False
            for attempt in range(36):            # up to ~3 min per object
                try:
                    logger.info("[exports] cleanup: ES DELETE %s (try %s)",
                                path, attempt + 1)
                    es.delete(path)
                    ok = True
                    break
                except Exception as exc:
                    msg = _err_text(exc)
                    if "missing" in msg or "404" in msg:
                        ok = True                # already gone — good enough
                        break
                    logger.warning("[exports] cleanup: ES DELETE %s busy/failed"
                                   " (retry in 5s): %s", path, msg[:300])
                    time.sleep(5)
            es_ok = es_ok and ok
    if ssh is not None:
        if es is not None and not es_ok:
            logger.warning("[exports] cleanup: ES still uses repo %r — KEEPING "
                           "files on %s (%s) to protect running recoveries",
                           name, ssh.host, p["host_dir"])
            return False
        try:
            ssh.run(f"rm -rf {p['host_dir']} {p['host_zip']} {p['host_meta']}")
        except Exception as exc:
            logger.warning("[exports] cleanup: remote rm failed: %s", exc)
            return False
    return es_ok


def _probe_ssh(host: str, port: int = 22, timeout: float = 4.0) -> bool:
    """True when a TCP connection to host:port succeeds — a cheap 'is there an
    sshd there' test used to pick the CC host address embedded."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _default_gateway() -> str:
    """The container's default-route gateway, which in a bridge network IS the
    Docker host — i.e. the CC itself. Read from /proc so it needs no `ip`
    binary (the slim image has none). Empty string when it can't be read."""
    try:
        with open("/proc/net/route") as fh:
            for line in fh.readlines()[1:]:
                f = line.strip().split()
                # Destination 00000000 = default route; gateway is little-endian hex.
                if len(f) > 2 and f[1] == "00000000":
                    g = int(f[2], 16)
                    return f"{g & 0xff}.{(g >> 8) & 0xff}.{(g >> 16) & 0xff}.{(g >> 24) & 0xff}"
    except Exception:
        pass
    return ""


def _snapshot_ssh_host(es) -> str:
    """The host to SSH into for the snapshot zip/pull — which is NOT always the
    host the ES *client* talks to.

    Standalone, they are the same box: the engineer connected the app to a CC
    over the network, ES answers on that CC's address, and that address is an
    SSH target we hold (or can prompt for) credentials on. So use it.

    Embedded, they diverge. The ES client reaches Elasticsearch across the CC's
    internal compose network at a SERVICE NAME (e.g. ``kvision-infra-efk``) —
    which has no sshd and is not a box anyone logs into. But the snapshot files
    ES writes live on the CC HOST (``snap_host_dir`` is a bind mount from the
    host into the ES container), and that host — the CC itself — does run sshd.
    The credentials the user gives are for the SYSTEM, not for the ES
    container, exactly as they'd expect. So resolve to the CC host:

      1. an explicit SNAP_SSH_HOST if the deployment set one (most reliable);
      2. else the container's default-route gateway, which in a bridge network
         is the Docker host = the CC, when its sshd answers;
      3. else the conventional docker0 host alias 172.17.0.1 when THAT answers.

    Returns "" only when no CC host with an open sshd can be found, so the
    caller can give a precise error instead of dialling a container name.
    """
    from core import policy
    if policy.profile() != policy.EMBEDDED:
        # Standalone: the connected CC. Prefer the appliance address the client
        # recorded (survives an SSH tunnel, where base_url is 127.0.0.1).
        try:
            return getattr(es, "cc_host", "") or _source_host(es)
        except Exception:
            return _source_host(es)

    # Embedded: the CC host, never the ES service name.
    configured = (settings.snap_ssh_host or "").strip()
    if configured:
        return configured
    for cand in (_default_gateway(), "172.17.0.1"):
        if cand and _probe_ssh(cand):
            logger.info("[exports] embedded snapshot SSH host resolved to %s", cand)
            return cand
    return ""


@router.post("/snapshot")
def start_snapshot(req: SnapshotRequest):
    """Archive indices via a native snapshot: repo+snapshot named after the
    user's chosen name, zipped on the ES host, pulled into EXPORTS_DIR."""
    from core.remote.ssh_ops import check_login

    name = (req.name or "").strip()
    if not _SNAP_NAME.match(name):
        return {"error": "invalid archive name — use letters, digits, '-' and '_' "
                         "(max 64 chars, must start with a letter or digit)"}
    indices = [n.strip() for n in req.indices if n and n.strip()]
    if not indices:
        return {"error": "no indices given"}
    if os.path.isfile(os.path.join(EXPORTS_DIR, f"{name}.zip")):
        return {"error": f"archive {name}.zip already exists — pick another name "
                         f"or delete it first"}
    with _JOBS_LOCK:
        busy = any(j["status"] == "running" and j["kind"] in ("snapshot", "snap-restore")
                   and j["items"] and j["items"][0]["index"] == name
                   for j in _JOBS.values())
    if busy:
        return {"error": f"a snapshot job named {name!r} is already running"}

    try:
        es = get_client()                     # captured once — see export note
    except Exception as exc:
        return {"error": str(exc)}
    host = _snapshot_ssh_host(es)
    if not host:
        return {"error": "cannot determine the CC host to snapshot on. Set "
                         "SNAP_SSH_HOST to the CC's address, or ensure its SSH "
                         "port is reachable from this container."}

    creds = _resolve_creds(host, req.ssh)
    if creds is None:
        return {"need_credentials": True, "host": host}
    err = check_login(host, creds["user"], creds["password"])
    if err:
        return {"error": f"SSH login to {host} failed: {err}", "need_credentials": True,
                "host": host}

    item = {"index": name, "total": None, "done": 0, "file": f"{name}.zip",
            "phase": "snapshot", "unit": "shards", "indices": indices}
    job = _new_job("snapshot", [item])
    threading.Thread(target=_run_snapshot_export_job,
                     args=(job, es, host, name, indices, creds), daemon=True,
                     name=f"snapshot-{job['id']}").start()
    logger.info("[exports] snapshot job %s: %s indices -> %s.zip (host %s)",
                job["id"], len(indices), name, host)
    return {"job_id": job["id"], "host": host}


def _run_snapshot_export_job(job: dict, es, host: str, name: str,
                             indices: list, creds: dict) -> None:
    from core.remote.ssh_ops import SSHSession
    item = job["items"][0]
    p = _snap_paths(name)
    ssh = None
    part = os.path.join(EXPORTS_DIR, f"{name}.zip.{job['id']}.part")

    def check_cancel():
        if job["cancelled"]:
            raise _JobCancelled()

    jid = job["id"]
    try:
        # 1) Register the repository and start the snapshot (async on ES side).
        _es_call(jid, es, "put", f"/_snapshot/{name}",
                 {"type": "fs", "settings": {"location": p["es_location"]}})
        _es_call(jid, es, "put", f"/_snapshot/{name}/{name}",
                 {"ignore_unavailable": True, "include_global_state": False,
                  "indices": ",".join(indices)})
        item["phase"] = "snapshot"
        while True:
            check_cancel()
            st = _es_call(jid, es, "get", f"/_snapshot/{name}/{name}/_status")
            snaps = st.get("snapshots") or []
            state = snaps[0].get("state") if snaps else None
            shards = (snaps[0].get("shards_stats") or {}) if snaps else {}
            item["done"] = shards.get("done", 0)
            item["total"] = shards.get("total") or item["total"]
            if state == "SUCCESS":
                break
            if state in ("FAILED", "PARTIAL", "ABORTED"):
                raise RuntimeError(f"snapshot ended in state {state}")
            time.sleep(2)

        # 2) Zip on the host (relative paths → unzip on the target recreates
        #    SNAP_HOST_DIR/<name>) with a meta file riding beside the repo dir.
        ssh = SSHSession(host, creds["user"], creds["password"])
        _log_fs(jid, ssh, settings.snap_host_dir, "source base dir")
        _log_fs(jid, ssh, p["host_dir"], "source repo dir after snapshot")
        meta = {"name": name, "source": host, "indices": indices,
                "created": datetime.now(timezone.utc).isoformat(),
                "es_version": (lambda i: (i.get("version") or {}).get("number"))(
                    _safe_info(es))}
        with ssh.sftp().open(p["host_meta"], "w") as fh:
            fh.write(json.dumps(meta, indent=1))
        item["phase"] = "zip"
        item["done"], item["total"], item["unit"] = 0, None, ""
        ssh.run(f"cd {settings.snap_host_dir} && rm -f {name}.zip && "
                f"zip -rq {name}.zip {name} {name}.cc-meta.json")
        check_cancel()

        # 3) Pull the zip into EXPORTS_DIR (job-scoped .part → atomic rename).
        item["phase"] = "transfer"
        item["unit"] = "bytes"
        size = ssh.sftp().stat(p["host_zip"]).st_size
        item["total"] = size

        def _cb(done, _total):
            item["done"] = done
            if job["cancelled"]:
                raise _JobCancelled()
        ssh.get(p["host_zip"], part, progress_cb=_cb)
        os.replace(part, os.path.join(EXPORTS_DIR, f"{name}.zip"))

        # 4) Leave the source machine clean.
        item["phase"] = "cleanup"
        _snap_cleanup(name, es=es, ssh=ssh)
        _finish_job(job)
        logger.info("[exports] snapshot %s: %s.zip ready (%s bytes)",
                    job["id"], name, size)
    except _JobCancelled:
        logger.info("[exports] snapshot job %s cancelled", job["id"])
        _cleanup_after_failure(name, es, ssh, part)
        _finish_job(job, cancelled=True)
    except Exception as exc:
        logger.error("[exports] snapshot job %s failed: %s", job["id"], _err_text(exc))
        _cleanup_after_failure(name, es, ssh, part)
        _finish_job(job, error=_err_text(exc))
    finally:
        if ssh is not None:
            try:
                ssh.close()
            except Exception:
                pass


def _safe_info(es) -> dict:
    try:
        return es.info()
    except Exception:
        return {}


def _cleanup_after_failure(name: str, es, ssh, part: str) -> None:
    _snap_cleanup(name, es=es, ssh=ssh)
    try:
        os.remove(part)
    except OSError:
        pass


@router.post("/snapshot/restore")
def start_snapshot_restore(req: SnapshotRestoreRequest):
    """Restore a snapshot archive (.zip in EXPORTS_DIR) into the machine of the
    currently-connected ES: push zip, unzip, register repo, native _restore."""
    from core.remote.ssh_ops import check_login

    fname = os.path.basename(req.filename or "")
    if not fname.endswith(".zip") or not _SAFE_NAME.match(fname):
        return {"error": "invalid snapshot archive name"}
    path = os.path.join(EXPORTS_DIR, fname)
    if not os.path.isfile(path):
        return {"error": f"archive {fname!r} not found on this server"}
    name = fname[:-4]
    if not _SNAP_NAME.match(name):
        return {"error": "invalid snapshot archive name"}

    try:
        es = get_client()
    except Exception as exc:
        return {"error": str(exc)}
    host = _snapshot_ssh_host(es)
    if not host:
        return {"error": "cannot determine the CC host to restore on. Set "
                         "SNAP_SSH_HOST to the CC's address, or ensure its SSH "
                         "port is reachable from this container."}

    creds = _resolve_creds(host, req.ssh)
    if creds is None:
        return {"need_credentials": True, "host": host}
    err = check_login(host, creds["user"], creds["password"])
    if err:
        return {"error": f"SSH login to {host} failed: {err}", "need_credentials": True,
                "host": host}

    meta = _zip_meta(path) or {}
    known = meta.get("indices") or []
    wanted = [i.strip() for i in (req.indices or []) if i and i.strip()]
    if wanted and known:
        unknown = [i for i in wanted if i not in known]
        if unknown:
            return {"error": "these indices are not in the snapshot: "
                             + ", ".join(unknown[:5])}
    item = {"index": name, "total": None, "done": 0, "file": fname,
            "phase": "transfer", "unit": "bytes",
            "indices": wanted or known, "selected": wanted}
    job = _new_job("snap-restore", [item])
    threading.Thread(target=_run_snapshot_restore_job,
                     args=(job, es, host, name, creds, wanted), daemon=True,
                     name=f"snaprestore-{job['id']}").start()
    logger.info("[exports] snap-restore job %s: %s -> host %s", job["id"], fname, host)
    return {"job_id": job["id"], "host": host}


class _RestoreStalled(Exception):
    """Restore made no progress — fail WITHOUT deleting the repo files."""


def _run_snapshot_restore_job(job: dict, es, host: str, name: str, creds: dict,
                              selected: list | None = None) -> None:
    from core.remote.ssh_ops import SSHSession
    jid = job["id"]
    item = job["items"][0]
    p = _snap_paths(name)
    local = os.path.join(EXPORTS_DIR, f"{name}.zip")
    ssh = None

    def check_cancel():
        if job["cancelled"]:
            raise _JobCancelled()

    try:
        # 1) Push the zip to the target host and unzip (recreates <dir>/<name>).
        ssh = SSHSession(host, creds["user"], creds["password"])
        item["phase"] = "transfer"
        item["total"] = os.path.getsize(local)

        def _cb(done, _total):
            item["done"] = done
            if job["cancelled"]:
                raise _JobCancelled()
        ssh.run(f"mkdir -p {settings.snap_host_dir}")
        _log_fs(jid, ssh, settings.snap_host_dir, "target base dir before transfer")
        ssh.put(local, p["host_zip"], progress_cb=_cb)
        item["phase"] = "unzip"
        item["done"], item["total"], item["unit"] = 0, None, ""
        ssh.run(f"cd {settings.snap_host_dir} && rm -rf {name} && unzip -oq {name}.zip")
        _log_fs(jid, ssh, p["host_dir"], "target repo dir after unzip (pre-chown)")
        # Root extracted the files, but ES (its own user, in its container) must
        # be able to READ them and WRITE into the repo dir — repository
        # registration verifies the repo by writing a test file, so root-owned
        # read-only files make the PUT fail with a bare 500. Match the owner of
        # the backup base dir (the uid ES writes with) and open permissions.
        owner = ssh.run(f"stat -c '%u:%g' {settings.snap_host_dir}").strip()
        if owner and owner != "0:0":
            ssh.run(f"chown -R {owner} {p['host_dir']}")
        ssh.run(f"chmod -R a+rwX {p['host_dir']}")
        _log_fs(jid, ssh, p["host_dir"], "target repo dir after chown/chmod")
        check_cancel()

        # 2) Register the repository and verify the snapshot is visible.
        item["phase"] = "register"
        _es_call(jid, es, "get", "/")                 # target ES version, for the log
        _es_call(jid, es, "put", f"/_snapshot/{name}",
                 {"type": "fs", "settings": {"location": p["es_location"]}})
        listing = _es_call(jid, es, "get", f"/_snapshot/{name}/_all")
        snaps = [s.get("snapshot") for s in (listing.get("snapshots") or [])]
        if name not in snaps:
            raise RuntimeError(f"snapshot {name!r} not visible in the repository "
                               f"after unzip (found: {snaps})")

        # The authoritative index list comes from ES itself — the zip's meta is
        # only a fallback. Without it we must NOT restore: the recovery wait
        # below would have nothing to watch and cleanup would delete the repo
        # files while ES is still restoring from them (⇒ red indices).
        snap_info = _es_call(jid, es, "get", f"/_snapshot/{name}/{name}")
        es_indices = ((snap_info.get("snapshots") or [{}])[0].get("indices")) or []
        indices = es_indices or item.get("indices") or []
        if not indices:
            raise RuntimeError("cannot determine which indices the snapshot "
                               "contains — refusing to restore blindly")
        # Partial restore: keep only what the user picked, validated against
        # what the snapshot really holds (ES is the authority, not the zip meta).
        if selected:
            missing = [i for i in selected if i not in indices]
            if missing:
                raise RuntimeError("selected indices are not in this snapshot: "
                                   + ", ".join(missing[:5]))
            indices = [i for i in indices if i in selected]
            logger.info("[exports %s] partial restore: %s of %s indices",
                        jid, len(indices), len(es_indices))
        item["indices"] = indices

        # 3) Native restore, then wait until recovery REALLY finishes.
        # Index health is NOT a safe signal: while a primary is still being
        # restored the index can already report yellow (docs.count null) — and
        # cleaning up at that point deletes the repo files a running recovery
        # still reads (⇒ red indices). The recovery API is authoritative: wait
        # until every snapshot-type shard recovery of these indices is DONE and
        # nothing is red.
        item["phase"] = "restore"
        item["unit"] = "shards"
        restore_body: dict = {"ignore_unavailable": True}
        if selected:
            restore_body["indices"] = ",".join(indices)
        _es_call(jid, es, "post", f"/_snapshot/{name}/{name}/_restore", restore_body)
        idx_path = ",".join(indices)
        stall_polls, last_done = 0, -1
        while True:
            check_cancel()
            time.sleep(3)
            try:
                rec = _es_call(jid, es, "get", f"/_cat/recovery/{idx_path}",
                               params={"format": "json", "h": "index,type,stage"})
                health = _es_call(jid, es, "get", f"/_cat/indices/{idx_path}",
                                  params={"format": "json", "h": "index,health"})
            except Exception:
                continue                       # indices may not all exist yet
            snap_rows = [r for r in rec
                         if (r.get("type") or "").lower() == "snapshot"]
            done = sum(1 for r in snap_rows
                       if (r.get("stage") or "").lower() == "done")
            if snap_rows:
                item["total"] = len(snap_rows)
                item["done"] = done
            nonred = sum(1 for r in health if r.get("health") in ("yellow", "green"))
            if (snap_rows and done == len(snap_rows)
                    and len(health) >= len(indices) and nonred >= len(indices)):
                break
            # Stall guard: zero recovery progress for ~5 min means the restore
            # died — surface it and KEEP the repo files for inspection.
            stall_polls = stall_polls + 1 if done == last_done else 0
            last_done = done
            if stall_polls >= 100:
                busy = [f"{r.get('index')}:{r.get('stage')}" for r in snap_rows
                        if (r.get("stage") or "").lower() != "done"]
                raise _RestoreStalled(
                    f"restore stalled: {done}/{len(snap_rows) or '?'} shard "
                    f"recoveries done, pending: {', '.join(busy[:10]) or 'unknown'}"
                    f" — repo files kept at {p['host_dir']} for inspection")

        rows = _es_call(jid, es, "get", "/_cat/indices/" + ",".join(indices),
                        params={"format": "json",
                                "h": "index,health,status,docs.count,store.size"})
        logger.info("[exports %s] restored indices final state: %s", jid,
                    json.dumps(rows)[:1500])

        # 4) Leave the target machine clean.
        item["phase"] = "cleanup"
        _snap_cleanup(name, es=es, ssh=ssh)
        _finish_job(job)
        logger.info("[exports] snap-restore %s: %s restored on %s",
                    job["id"], name, host)
    except _RestoreStalled as exc:
        # Deliberately NO cleanup — the repo files stay for debugging/retry.
        logger.error("[exports] snap-restore job %s stalled: %s", job["id"], exc)
        _finish_job(job, error=str(exc))
    except _JobCancelled:
        logger.info("[exports] snap-restore job %s cancelled", job["id"])
        # The only way to abort a RUNNING restore is deleting the indices it is
        # restoring (created by this restore, so nothing pre-existing is
        # touched). This also releases the repo so cleanup can proceed. Before
        # the restore phase nothing was created — delete nothing.
        if item.get("phase") == "restore":
            for ix in (item.get("indices") or []):
                try:
                    _es_call(jid, es, "delete", f"/{ix}")
                except Exception:
                    pass
        _snap_cleanup(name, es=es, ssh=ssh)
        _finish_job(job, cancelled=True)
    except Exception as exc:
        logger.error("[exports] snap-restore job %s failed: %s", job["id"], _err_text(exc))
        _snap_cleanup(name, es=es, ssh=ssh)
        _finish_job(job, error=_err_text(exc))
    finally:
        if ssh is not None:
            try:
                ssh.close()
            except Exception:
                pass


# ── Snapshot metadata + SSH credentials ───────────────────────────────────────

def _zip_meta(path: str) -> dict | None:
    """cc-meta.json from a snapshot zip (cheap central-directory read)."""
    try:
        with zipfile.ZipFile(path) as zf:
            for n in zf.namelist():
                if n.endswith(".cc-meta.json"):
                    return json.loads(zf.read(n))
    except Exception:
        pass
    return None


# CRC-checking every entry reads the whole archive; skip it past this size and
# say so, rather than making an upload appear to hang.
_DEEP_CHECK_MAX_BYTES = 512 * 1024 * 1024


def validate_snapshot_zip(path: str) -> dict:
    """Check that a .zip really is a restorable ES snapshot repository.

    Fatal problems land in `errors` (restore would fail); cosmetic ones in
    `warnings`. The structure checked is what both the app's snapshot job and
    the generated fetch script produce:

        <name>/index-N                 repository index
        <name>/snap-<uuid>.dat         snapshot metadata
        <name>/indices/<uuid>/<shard>/…
        <name>.cc-meta.json            our own metadata (optional)

    The top-level directory MUST match the zip's stem: restore unzips into the
    host dir and registers the repository at <host_dir>/<stem>, so a mismatch
    produces an empty repository and a confusing failure.
    """
    name = os.path.basename(path)
    stem = name[:-4] if name.endswith(".zip") else name
    out: dict = {"name": name, "ok": False, "errors": [], "warnings": [],
                 "meta": None, "indices": [], "entries": 0, "integrity": "not checked"}
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        out["errors"].append(f"cannot read the file: {exc}")
        return out

    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            out["entries"] = len(names)
            roots = {n.split("/")[0] for n in names if n}
            repo_files = [n for n in names if n.startswith(f"{stem}/")]

            if stem not in roots:
                out["errors"].append(
                    f"the archive does not contain a top-level '{stem}/' directory "
                    f"(found: {', '.join(sorted(roots)[:4])}) — the file name and the "
                    f"snapshot name inside must match")
            else:
                if not any(re.match(rf"^{re.escape(stem)}/index-", n) for n in repo_files):
                    out["errors"].append("no repository index file ('index-N') — "
                                         "this is not a snapshot repository")
                if not any(n.startswith(f"{stem}/indices/") for n in repo_files):
                    out["errors"].append("no 'indices/' directory — the snapshot holds no data")
                if not any(re.match(rf"^{re.escape(stem)}/snap-.*\.dat$", n) for n in repo_files):
                    out["warnings"].append("no top-level 'snap-*.dat' metadata file")

            meta = None
            for n in names:
                if n.endswith(".cc-meta.json"):
                    try:
                        meta = json.loads(zf.read(n))
                    except Exception as exc:
                        out["warnings"].append(f"cc-meta.json is not readable: {exc}")
                    break
            if meta is None:
                out["warnings"].append(
                    "no cc-meta.json — the source machine and index list are unknown "
                    "(the archive can still be restored)")
            else:
                out["meta"] = meta
                out["indices"] = meta.get("indices") or []

            if size <= _DEEP_CHECK_MAX_BYTES:
                bad = zf.testzip()
                if bad:
                    out["errors"].append(f"corrupted entry: {bad}")
                    out["integrity"] = "failed"
                else:
                    out["integrity"] = "ok"
            else:
                out["integrity"] = "skipped (archive larger than 512 MB)"
    except zipfile.BadZipFile:
        out["errors"].append("not a valid zip file")
        return out
    except Exception as exc:
        out["errors"].append(f"could not read the archive: {exc}")
        return out

    out["ok"] = not out["errors"]
    return out


@router.get("/validate/{name}")
def validate_archive(name: str):
    """Check a snapshot archive already stored on this server."""
    if not _SAFE_NAME.match(name) or not name.endswith(".zip"):
        return {"error": "validation applies to snapshot (.zip) archives"}
    path = os.path.join(EXPORTS_DIR, name)
    if not os.path.isfile(path):
        return {"error": f"archive {name!r} not found"}
    return validate_snapshot_zip(path)


# ── Standalone fetch script ──────────────────────────────────────────────────

class ScriptRequest(BaseModel):
    """Generate a self-contained snapshot script to run ON a CC machine."""
    name: str
    indices: list[str] = []
    es_url: str = "http://localhost:9200"


_FETCH_SCRIPT = r'''#!/bin/sh
# ---------------------------------------------------------------------------
# CC ES Analyzer — standalone snapshot fetch script
#
# Produces {name}.zip: exactly the archive the analyzer's "Snapshot archive"
# button creates, so the result can be uploaded straight into its Archives
# panel and restored on another machine.
#
# Run it ON the CC machine (as root):
#     sh {script_name}
#
# It needs only sh, curl and zip — no Python, no analyzer, no network access
# back to anything. Generated {generated} for {source_desc}.
# ---------------------------------------------------------------------------
set -e

ES_URL="${{ES_URL:-{es_url}}}"
NAME="{name}"
HOST_DIR="${{HOST_DIR:-{host_dir}}}"     # repo dir as seen on THIS machine
ES_DIR="${{ES_DIR:-{es_dir}}}"           # same dir as the ES process sees it
INDICES="{indices_csv}"

REPO_DIR="$HOST_DIR/$NAME"
ZIP_PATH="$HOST_DIR/$NAME.zip"
META_PATH="$HOST_DIR/$NAME.cc-meta.json"

echo "== CC snapshot: $NAME"
echo "   ES        : $ES_URL"
echo "   indices   : $INDICES"
echo "   repo dir  : $REPO_DIR  (ES sees $ES_DIR/$NAME)"
echo

for tool in curl zip; do
    command -v "$tool" >/dev/null 2>&1 || {{ echo "ERROR: '$tool' is not installed"; exit 1; }}
done

api() {{  # api <METHOD> <PATH> [BODY]
    if [ -n "$3" ]; then
        curl -sS -X "$1" "$ES_URL$2" -H 'Content-Type: application/json' -d "$3"
    else
        curl -sS -X "$1" "$ES_URL$2"
    fi
}}

# Fail early rather than half-way through a snapshot.
api GET / >/dev/null || {{ echo "ERROR: cannot reach ES at $ES_URL"; exit 1; }}

cleanup_repo() {{
    api DELETE "/_snapshot/$NAME/$NAME" >/dev/null 2>&1 || true
    api DELETE "/_snapshot/$NAME"       >/dev/null 2>&1 || true
    rm -rf "$REPO_DIR" "$META_PATH"
}}

# A leftover repo/snapshot from an interrupted run would make the PUTs fail.
cleanup_repo
mkdir -p "$HOST_DIR"
rm -f "$ZIP_PATH"

echo "1/5 registering repository"
api PUT "/_snapshot/$NAME" \
    "{{\"type\":\"fs\",\"settings\":{{\"location\":\"$ES_DIR/$NAME\"}}}}"
echo

echo "2/5 starting snapshot"
api PUT "/_snapshot/$NAME/$NAME" \
    "{{\"ignore_unavailable\":true,\"include_global_state\":false,\"indices\":\"$INDICES\"}}"
echo

echo "3/5 waiting for the snapshot to finish"
while : ; do
    STATUS=$(api GET "/_snapshot/$NAME/$NAME/_status")
    # First "state" is the snapshot's own (per-index ones follow) — take it.
    STATE=$(echo "$STATUS" | tr ',' '\n' | grep -m1 '"state"' | cut -d'"' -f4)
    # Shard progress lives in one flat object; isolate it before reading numbers.
    SS=$(echo "$STATUS" | sed -n 's/.*"shards_stats":{{\([^}}]*\)}}.*/\1/p')
    DONE=$(echo "$SS" | sed -n 's/.*"done":\([0-9]*\).*/\1/p')
    TOTAL=$(echo "$SS" | sed -n 's/.*"total":\([0-9]*\).*/\1/p')
    echo "    state=${{STATE:-?}} shards=${{DONE:-?}}/${{TOTAL:-?}}"
    case "$STATE" in
        SUCCESS)                 break ;;
        FAILED|PARTIAL|ABORTED)  echo "ERROR: snapshot ended in state $STATE"
                                 cleanup_repo; exit 1 ;;
    esac
    sleep 2
done
echo

echo "4/5 writing metadata and zipping"
ES_VERSION=$(api GET / | tr ',' '\n' | grep -m1 '"number"' | cut -d'"' -f4)
cat > "$META_PATH" <<META
{{
 "name": "$NAME",
 "source": "$(hostname -f 2>/dev/null || hostname)",
 "indices": [{indices_json}],
 "created": "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)",
 "es_version": "$ES_VERSION",
 "generated_by": "standalone fetch script"
}}
META
# Relative paths only — unzip on the target must recreate <dir>/<name>.
cd "$HOST_DIR" && zip -rq "$NAME.zip" "$NAME" "$NAME.cc-meta.json"
echo "    $ZIP_PATH"
echo

echo "5/5 removing the snapshot and repo from this machine"
cleanup_repo

echo
echo "DONE — copy this file off the machine and upload it in the analyzer's"
echo "       Archives panel (Upload archive):"
ls -lh "$ZIP_PATH"
'''


@router.post("/script")
def generate_fetch_script(req: ScriptRequest):
    """A standalone shell script that performs the snapshot archive flow
    locally on a CC machine, producing the same <name>.zip the app makes."""
    name = (req.name or "").strip()
    if not _SNAP_NAME.match(name):
        return {"error": "invalid archive name — use letters, digits, '-' and '_' "
                         "(max 64 chars, must start with a letter or digit)"}
    indices = [n.strip() for n in (req.indices or []) if n and n.strip()]
    if not indices:
        return {"error": "no indices given"}
    bad = [n for n in indices if any(c in n for c in ' "\'\\$`\n')]
    if bad:
        return {"error": f"index names contain unsupported characters: {', '.join(bad[:5])}"}

    try:
        source_desc = _source_host(get_client()) or "an unknown machine"
    except Exception:
        source_desc = "an unknown machine"

    script_name = f"fetch_{name}.sh"
    body = _FETCH_SCRIPT.format(
        name=name,
        script_name=script_name,
        es_url=req.es_url or "http://localhost:9200",
        host_dir=settings.snap_host_dir,
        es_dir=settings.snap_es_dir,
        indices_csv=",".join(indices),
        indices_json=", ".join(json.dumps(i) for i in indices),
        generated=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        source_desc=source_desc,
    )
    # LF endings — the script runs on the CC's Linux shell.
    return Response(content=body.replace("\r\n", "\n"),
                    media_type="text/x-shellscript",
                    headers={"Content-Disposition":
                             f'attachment; filename="{script_name}"'})


@router.get("/meta/{name}")
def archive_meta(name: str):
    """Metadata of an archive — for zips: the embedded cc-meta.json
    (source machine, index list, ES version)."""
    if not _SAFE_NAME.match(name):
        return {"error": "invalid archive name"}
    path = os.path.join(EXPORTS_DIR, name)
    if not os.path.isfile(path):
        return {"error": f"archive {name!r} not found"}
    if name.endswith(".zip"):
        return {"name": name, "type": "snapshot", "meta": _zip_meta(path)}
    st = os.stat(path)
    return {"name": name, "type": "csv", "meta": {"source": _archive_source(path, st)}}


@router.get("/ssh-creds")
def list_ssh_creds():
    """Hosts with remembered SSH credentials — never the secrets themselves."""
    from core.remote import cred_store
    return {"hosts": cred_store.hosts()}


@router.delete("/ssh-creds/{host}")
def delete_ssh_creds(host: str):
    from core.remote import cred_store
    return {"ok": cred_store.delete(host), "host": host}


# ── Files + status ────────────────────────────────────────────────────────────

# Source-tag cache: (name, mtime, size) → source host (or None). Peeking means
# decompressing the first line of the gzip — cheap, but not worth repeating on
# every 2-second poll of the Archives panel.
_SOURCE_CACHE: dict = {}
_META_LINE = re.compile(r"^#cc-es-archive\b(.*)$")


def _archive_source(path: str, st) -> str | None:
    """Machine the archive was taken from: #cc-es-archive line for CSVs,
    embedded cc-meta.json for snapshot zips."""
    key = (os.path.basename(path), st.st_mtime_ns, st.st_size)
    if key in _SOURCE_CACHE:
        return _SOURCE_CACHE[key]
    source = None
    try:
        if path.endswith(".zip"):
            source = (_zip_meta(path) or {}).get("source") or None
        else:
            opener = gzip.open if path.endswith(".gz") else open
            with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
                m = _META_LINE.match(fh.readline().strip())
                if m:
                    for tok in m.group(1).split():
                        if tok.startswith("source="):
                            source = tok[len("source="):] or None
    except Exception:
        source = None                        # foreign/corrupt file → no source
    _SOURCE_CACHE[key] = source
    if len(_SOURCE_CACHE) > 500:             # drop stale keys (renamed/deleted)
        for k in list(_SOURCE_CACHE)[:250]:
            _SOURCE_CACHE.pop(k, None)
    return source


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    """Ask a running export/restore job to stop. Export: the in-flight index's
    partial file is discarded (finished archives are kept). Restore: docs
    already bulk-indexed stay in the target index."""
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if not job:
        return {"error": "unknown job"}
    if job["status"] != "running":
        return {"ok": True, "status": job["status"], "note": "job already finished"}
    job["cancelled"] = True
    logger.info("[exports] job %s cancel requested", job_id)
    return {"ok": True, "status": "cancelling"}


@router.delete("/jobs/{job_id}")
def ack_job(job_id: str):
    """Acknowledge (dismiss) a finished job — its card disappears from the
    Archives panel. Error jobs stay visible until acknowledged this way."""
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return {"error": "unknown job"}
        if job["status"] == "running":
            return {"error": "job is still running — cancel it first"}
        _JOBS.pop(job_id, None)
    logger.info("[exports] job %s (%s/%s) acknowledged and removed",
                job_id, job["kind"], job["status"])
    return {"ok": True}


@router.get("")
def list_exports():
    """Archives on this server + export/restore jobs (running first)."""
    files = []
    try:
        for name in sorted(os.listdir(EXPORTS_DIR)):
            if not _SAFE_NAME.match(name):
                continue                     # skip .part temp files etc.
            path = os.path.join(EXPORTS_DIR, name)
            st = os.stat(path)
            files.append({"name": name, "size": st.st_size,
                          "type": "snapshot" if name.endswith(".zip") else "csv",
                          "source": _archive_source(path, st),
                          "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()})
    except OSError as exc:
        return {"error": str(exc)}
    with _JOBS_LOCK:
        jobs = sorted(_JOBS.values(),
                      key=lambda j: (j["status"] != "running", j["started_at"]),
                      reverse=False)
    return {"files": files, "jobs": jobs, "dir": EXPORTS_DIR}


@router.get("/jobs/{job_id}")
def job_status(job_id: str):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    return job if job else {"error": "unknown job"}


@router.get("/download/{name}")
def download_export(name: str):
    if not _SAFE_NAME.match(name):
        return {"error": "invalid archive name"}
    path = os.path.join(EXPORTS_DIR, name)
    if not os.path.isfile(path):
        return {"error": f"archive {name!r} not found"}
    media = ("application/zip" if name.endswith(".zip")
             else "application/gzip" if name.endswith(".gz") else "text/csv")
    return FileResponse(path, media_type=media, filename=name)


@router.delete("/{name}")
def delete_export(name: str):
    if not _SAFE_NAME.match(name):
        return {"error": "invalid archive name"}
    path = os.path.join(EXPORTS_DIR, name)
    if not os.path.isfile(path):
        return {"error": f"archive {name!r} not found"}
    try:
        os.remove(path)
        return {"ok": True, "name": name}
    except OSError as exc:
        return {"error": str(exc)}
