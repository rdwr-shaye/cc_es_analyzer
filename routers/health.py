from fastapi import APIRouter
from services.es_client import ping, get_client

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
def cluster_health():
    """Return Elasticsearch cluster health and basic info."""
    if not ping():
        return {"connected": False, "error": "Cannot reach Elasticsearch"}
    try:
        es = get_client()
        health = es.cluster_health()
        info = es.info()
        return {
            "connected": True,
            "cluster_name": health.get("cluster_name"),
            "status": health.get("status"),
            "number_of_nodes": health.get("number_of_nodes"),
            "number_of_data_nodes": health.get("number_of_data_nodes"),
            "active_shards": health.get("active_shards"),
            "unassigned_shards": health.get("unassigned_shards"),
            "es_version": info.get("version", {}).get("number"),
        }
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
