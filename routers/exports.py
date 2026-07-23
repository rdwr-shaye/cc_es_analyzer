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
from fastapi.responses import FileResponse
from pydantic import BaseModel

from config import settings
from services.es_client import get_client

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
    from routers.query import _scroll_hits, _collect_top_fields, _csv_cell
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
                        target: str = Form(default="")):
    """Restore an archive into the currently-connected ES.

    Give EITHER an uploaded .csv/.csv.gz file (the cross-machine 'upload' flow)
    OR `filename` of an archive already in this server's exports directory.
    `target` = index to restore into (default: the archive's name stem).
    """
    from routers.indices import _valid_index_name

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
            # POST /api/exports/snapshot/restore.
            return {"saved": name, "type": "snapshot"}
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

    job = _new_job("restore", [{"index": tgt, "total": None, "done": 0, "file": name}])
    threading.Thread(target=_run_restore_job, args=(job, es, path, tgt), daemon=True,
                     name=f"restore-{job['id']}").start()
    logger.info("[exports] restore job %s: %s -> index %r", job["id"], name, tgt)
    return {"job_id": job["id"], "target": tgt}


def _run_restore_job(job: dict, es, path: str, target: str) -> None:
    from routers.indices import _coerce_cell, _flush_batch, _MISSING
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
                    if col == "_id":
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


def _resolve_creds(host: str, ssh: SnapshotSSH | None):
    """Stored or freshly-supplied SSH credentials for *host*; None → the UI
    must prompt (need_credentials handshake)."""
    from services import cred_store
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


@router.post("/snapshot")
def start_snapshot(req: SnapshotRequest):
    """Archive indices via a native snapshot: repo+snapshot named after the
    user's chosen name, zipped on the ES host, pulled into EXPORTS_DIR."""
    from services.ssh_ops import check_login

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
    host = _source_host(es)
    if not host:
        return {"error": "cannot determine the ES machine's host"}

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
    from services.ssh_ops import SSHSession
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
    from services.ssh_ops import check_login

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
    host = _source_host(es)
    if not host:
        return {"error": "cannot determine the ES machine's host"}

    creds = _resolve_creds(host, req.ssh)
    if creds is None:
        return {"need_credentials": True, "host": host}
    err = check_login(host, creds["user"], creds["password"])
    if err:
        return {"error": f"SSH login to {host} failed: {err}", "need_credentials": True,
                "host": host}

    meta = _zip_meta(path) or {}
    item = {"index": name, "total": None, "done": 0, "file": fname,
            "phase": "transfer", "unit": "bytes",
            "indices": meta.get("indices") or []}
    job = _new_job("snap-restore", [item])
    threading.Thread(target=_run_snapshot_restore_job,
                     args=(job, es, host, name, creds), daemon=True,
                     name=f"snaprestore-{job['id']}").start()
    logger.info("[exports] snap-restore job %s: %s -> host %s", job["id"], fname, host)
    return {"job_id": job["id"], "host": host}


class _RestoreStalled(Exception):
    """Restore made no progress — fail WITHOUT deleting the repo files."""


def _run_snapshot_restore_job(job: dict, es, host: str, name: str, creds: dict) -> None:
    from services.ssh_ops import SSHSession
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
        _es_call(jid, es, "post", f"/_snapshot/{name}/{name}/_restore",
                 {"ignore_unavailable": True})
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
    from services import cred_store
    return {"hosts": cred_store.hosts()}


@router.delete("/ssh-creds/{host}")
def delete_ssh_creds(host: str):
    from services import cred_store
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
