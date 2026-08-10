"""The one write endpoint in the MariaDB module.

Registered only when `maria.write` is unlocked. When it is not, this file is
still imported (declaring the router costs nothing) but main.py never includes
it, so the path is absent from the OpenAPI schema and answers 404 — which is
the claim the security review will actually test.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from modules.maria.client import MariaError
from modules.maria.writes import WriteRefused, check, update_cell

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/maria", tags=["mariadb"])


class CellEdit(BaseModel):
    schema_: str = Field(..., alias="schema")
    table: str
    column: str
    # The row's full primary key, as {column: value}. Same addressing scheme as
    # the blob endpoints — a row is named by its key or not at all.
    key: dict
    value: str | int | float | None = None
    # What the caller last saw in this cell. Required, including when it is
    # null: making it optional would let a client skip the concurrency check by
    # simply not sending it.
    expected: str | int | float | None = None
    expected_null: bool = False


@router.get("/cell/editable")
def maria_cell_editable(schema: str = Query(...), table: str = Query(...),
                        column: str = Query(...)):
    """Whether this column may be edited, and why not when it may not.

    The UI asks before offering an editable cell, so the refusal is shown as an
    explanation on a disabled control rather than as an error after someone has
    already typed a new value.
    """
    try:
        # A placeholder key: this checks the COLUMN, and the key is validated
        # for real on the write. Passing the true primary key here keeps the
        # single validation path honest about everything except the row.
        from modules.maria.writes import _primary_key
        pk = _primary_key(schema, table)
        check(schema, table, column, {c: None for c in pk})
    except WriteRefused as exc:
        return {"editable": False, "reason": str(exc)}
    except MariaError as exc:
        return {"editable": False, "reason": str(exc)}
    return {"editable": True, "reason": ""}


@router.post("/cell")
def maria_cell_update(body: CellEdit, request: Request):
    """Set one column of one row, addressed by its full primary key."""
    # Not identity — Phase 1 replaces this — but it is the same attribution the
    # app already puts on every other change, and the audit line should not be
    # less informative than the peer notifications already are.
    from core import sessions
    sid = getattr(request.state, "sid", "") or ""
    try:
        who = sessions.describe_session(sid)
    except Exception:
        who = sid[:8] or "unknown"

    expected = None if body.expected_null else body.expected
    try:
        return update_cell(body.schema_, body.table, body.column, body.key,
                           body.value, expected, who=who)
    except WriteRefused as exc:
        # 409: the request was well-formed and the caller may retry after
        # looking again. A 400 would suggest the client built it wrong.
        return {"error": str(exc), "refused": True}
    except MariaError as exc:
        return {"error": str(exc)}
