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
import os
import re
import threading
import time
import uuid
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
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.csv(\.gz)?$")

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
            return {"error": f"unsupported archive name {name!r} (expected .csv or .csv.gz)"}
        # Stream the upload to disk (uploads can be huge — never fully in memory).
        path = os.path.join(EXPORTS_DIR, name)
        try:
            with open(path, "wb") as out:
                while chunk := await file.read(1 << 20):
                    out.write(chunk)
        except Exception as exc:
            return {"error": f"could not save upload: {exc}"}
    elif filename:
        name = os.path.basename(filename)
        if not _SAFE_NAME.match(name):
            return {"error": "invalid archive name"}
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


# ── Files + status ────────────────────────────────────────────────────────────

# Source-tag cache: (name, mtime, size) → source host (or None). Peeking means
# decompressing the first line of the gzip — cheap, but not worth repeating on
# every 2-second poll of the Archives panel.
_SOURCE_CACHE: dict = {}
_META_LINE = re.compile(r"^#cc-es-archive\b(.*)$")


def _archive_source(path: str, st) -> str | None:
    """Machine the archive was taken from, read from its #cc-es-archive line."""
    key = (os.path.basename(path), st.st_mtime_ns, st.st_size)
    if key in _SOURCE_CACHE:
        return _SOURCE_CACHE[key]
    source = None
    try:
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
    return FileResponse(path, media_type="application/gzip" if name.endswith(".gz") else "text/csv",
                        filename=name)


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
