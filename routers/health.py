import logging

from fastapi import APIRouter
from services.es_client import ping, get_client

router = APIRouter(prefix="/api", tags=["health"])
logger = logging.getLogger(__name__)


def _unhealthy_indices(es) -> list:
    """Per-index health for indices that are NOT green (why the cluster is
    yellow/red). Works on ES 1.x .. OpenSearch via /_cluster/health?level=indices."""
    out = []
    try:
        h = es.get("/_cluster/health", params={"level": "indices"})
        for name, d in (h.get("indices") or {}).items():
            if isinstance(d, dict) and d.get("status") != "green":
                out.append({
                    "index":              name,
                    "status":             d.get("status"),
                    "active_shards":      d.get("active_shards"),
                    "relocating_shards":  d.get("relocating_shards"),
                    "initializing_shards": d.get("initializing_shards"),
                    "unassigned_shards":  d.get("unassigned_shards"),
                })
    except Exception as exc:
        logger.warning("[health] per-index health unavailable: %s", exc)
    out.sort(key=lambda x: (x["status"] != "red", x["index"]))   # red first
    return out


def _unassigned_shards(es) -> list:
    """Every shard that is not STARTED, with the allocation reason — so the UI
    can explain exactly what's wrong with each unassigned/relocating shard."""
    out = []
    try:
        rows = es.get("/_cat/shards", params={
            "h": "index,shard,prirep,state,unassigned.reason,node",
            "format": "json",
        })
        for r in rows or []:
            state = r.get("state")
            if state and state != "STARTED":
                out.append({
                    "index":  r.get("index"),
                    "shard":  r.get("shard"),
                    "type":   "primary" if r.get("prirep") == "p" else "replica",
                    "state":  state,
                    "reason": r.get("unassigned.reason") or "",
                    "node":   r.get("node") or "",
                })
    except Exception as exc:
        logger.warning("[health] shard detail unavailable: %s", exc)
    return out


@router.get("/health")
def cluster_health():
    """Return Elasticsearch cluster health and basic info. When the cluster is
    not green, also return WHY: the non-green indices and every unassigned/
    relocating shard with its allocation reason (for a copyable UI tooltip)."""
    if not ping():
        return {"connected": False, "error": "Cannot reach Elasticsearch"}
    try:
        es = get_client()
        health = es.cluster_health()
        info = es.info()
        status = health.get("status")
        result = {
            "connected": True,
            "cluster_name": health.get("cluster_name"),
            "status": status,
            "number_of_nodes": health.get("number_of_nodes"),
            "number_of_data_nodes": health.get("number_of_data_nodes"),
            "active_shards": health.get("active_shards"),
            "unassigned_shards": health.get("unassigned_shards"),
            "es_version": info.get("version", {}).get("number"),
        }
        if status and status != "green":
            result["unhealthy_indices"] = _unhealthy_indices(es)
        if health.get("unassigned_shards") or (status and status != "green"):
            result["unassigned_detail"] = _unassigned_shards(es)
        return result
    except Exception as e:
        return {"connected": False, "error": str(e)}


@router.get("/nodes")
def nodes_info():
    """Return basic stats for each ES node."""
    try:
        es = get_client()
        stats = es.nodes_stats()
        nodes = []
        for node_id, node in stats.get("nodes", {}).items():
            nodes.append({
                "id": node_id,
                "name": node.get("name"),
                "host": node.get("host"),
                "jvm_heap_used_pct": node.get("jvm", {}).get("mem", {}).get("heap_used_percent"),
                "cpu_pct": node.get("os", {}).get("cpu", {}).get("percent"),
                "disk_free_gb": round(
                    node.get("fs", {}).get("total", {}).get("available_in_bytes", 0) / 1e9, 2
                ),
            })
        return {"nodes": nodes}
    except Exception as e:
        return {"error": str(e)}
