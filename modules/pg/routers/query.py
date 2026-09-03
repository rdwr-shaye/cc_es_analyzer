"""The raw-SQL escape hatch for PostgreSQL.

Same role as modules/maria/routers/query.py: not the front door, but there
for the join the curated screens do not cover. Read-only is enforced in
modules/pg/client.py, at the transaction as well as the statement, so this
router adds no privilege of its own.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter
from pydantic import BaseModel

from config import settings
from modules.pg.client import PgError, run

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/pg", tags=["postgresql"])


class SqlRequest(BaseModel):
    sql: str
    database: str = ""
    limit: int | None = None

    model_config = {"json_schema_extra": {"examples": [
        {"sql": "SELECT * FROM active_policy LIMIT 10", "database": "dfc"}]}}


@router.post("/query")
def pg_query(req: SqlRequest):
    """Run one read-only statement and return its rows.

    Reports the elapsed time along with the rows: on a live CC the useful
    question is often "is this slow?", and the answer is otherwise invisible
    from the UI.
    """
    started = time.monotonic()
    try:
        rows, truncated = run(req.sql, database=req.database, limit=req.limit)
    except PgError as exc:
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
        "row_cap": req.limit or settings.pg_max_rows,
        "took_ms": elapsed_ms,
    }
