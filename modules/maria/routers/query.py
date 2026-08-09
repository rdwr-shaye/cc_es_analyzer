"""The raw-SQL escape hatch.

The roadmap is explicit that this is not the front door — the catalog and the
browse screens are. It exists because a debugging session eventually needs a
join nobody anticipated, and the alternative is the engineer leaving the tool
for a mysql shell where nothing is capped, timed out or audited.

Read-only is enforced in modules/maria/client.py, at the transaction as well as
the statement, so this router adds no privilege of its own.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter
from pydantic import BaseModel

from config import settings
from modules.maria.client import MariaError, run

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/maria", tags=["mariadb"])


class SqlRequest(BaseModel):
    sql: str
    schema_: str = ""          # `schema` shadows a BaseModel attribute
    limit: int | None = None

    model_config = {"populate_by_name": True,
                    "json_schema_extra": {"examples": [
                        {"sql": "SELECT * FROM device LIMIT 10",
                         "schema_": "vision_ng"}]}}


@router.post("/query")
def maria_query(req: SqlRequest):
    """Run one read-only statement and return its rows.

    Reports the elapsed time along with the rows: on a live CC the useful
    question is often "is this slow?", and the answer is otherwise invisible
    from the UI.
    """
    started = time.monotonic()
    try:
        rows, truncated = run(req.sql, schema=req.schema_, limit=req.limit)
    except MariaError as exc:
        # A rejected or failed statement is an ordinary outcome of exploring an
        # unfamiliar schema, not a server fault — 200 with the reason, so the
        # UI shows the message instead of a generic failure.
        return {"error": str(exc)}

    elapsed_ms = int((time.monotonic() - started) * 1000)
    return {
        "columns": list(rows[0].keys()) if rows else [],
        "rows": rows,
        "count": len(rows),
        "truncated": truncated,
        "row_cap": req.limit or settings.maria_max_rows,
        "took_ms": elapsed_ms,
    }
