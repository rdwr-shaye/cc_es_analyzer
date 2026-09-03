"""The one write endpoint in the PostgreSQL module.

Registered only when `pg.write` is unlocked. When it is not, this file is
still imported (declaring the router costs nothing) but main.py never includes
it, so the path is absent from the OpenAPI schema and answers 404 — matching
modules/maria/routers/edit.py exactly, which is the claim the security review
will actually test.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from modules.pg.client import PgError
from modules.pg.writes import WriteRefused, check, update_cell

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/pg", tags=["postgresql"])


class CellEdit(BaseModel):
    database: str
    table: str
    column: str
    key: dict
    value: str | int | float | None = None
    expected: str | int | float | None = None
    expected_null: bool = False


@router.get("/cell/editable")
def pg_cell_editable(database: str = Query(...), table: str = Query(...),
                     column: str = Query(...)):
    """Whether this column may be edited, and why not when it may not.
    Identical contract to modules/maria/routers/edit.py::maria_cell_editable."""
    try:
        from modules.pg.writes import _primary_key
        pk = _primary_key(database, table)
        check(database, table, column, {c: None for c in pk})
    except WriteRefused as exc:
        return {"editable": False, "reason": str(exc)}
    except PgError as exc:
        return {"editable": False, "reason": str(exc)}
    return {"editable": True, "reason": ""}


@router.post("/cell")
def pg_cell_update(body: CellEdit, request: Request):
    """Set one column of one row, addressed by its full primary key."""
    from core import sessions
    sid = getattr(request.state, "sid", "") or ""
    try:
        who = sessions.describe_session(sid)
    except Exception:
        who = sid[:8] or "unknown"

    expected = None if body.expected_null else body.expected
    try:
        return update_cell(body.database, body.table, body.column, body.key,
                           body.value, expected, who=who)
    except WriteRefused as exc:
        return {"error": str(exc), "refused": True}
    except PgError as exc:
        return {"error": str(exc)}
