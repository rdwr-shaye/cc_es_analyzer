# CC ES Analyzer — Session Handoff

_Handoff for a continuing agent. Summarizes everything built/changed in this session._

## What the app is
FastAPI service + vanilla-JS SPA that connects to a CyberController (CC) Elasticsearch
(**ES 1.3.14**, host set at runtime via the UI, e.g. `10.205.189.20:9200`) and lets the user
browse indices, run natural-language queries, and view/edit/export results.

- Run: `python main.py` (hot-reload on) → http://localhost:8000 (NOT `0.0.0.0:8000`).
- ES client is plain `requests` (no elasticsearch-py) — `services/es_client.py`.
- Frontend is a single `frontend/static/js/app.js` (no build step) + `index.html` + `style.css`.

---

## Backend work (this session)

### `main.py`
- **Port guard**: refuses to start (exit 1, clear log) if the port is already in use — prevents
  a stale orphan server from silently double-binding and hanging the browser.
- **`--no-reload`** flag: disables uvicorn reload for the hidden auto-start run.
- A Windows Scheduled Task ("CC ES Analyzer") was registered to auto-start at logon (hidden,
  `pythonw main.py --no-reload`). Documented in `README.md`.

### `services/es_client.py`
Added `put`, `bulk(ndjson, refresh)` methods (alongside existing get/post/search).

### `routers/query.py` — NL translation (`POST /api/query/translate`)
The translator resolves free text against each index group's **real mapping** (never assumes a
field exists). Key pieces:
- `_extract_field_refs(text)` — pulls `<descriptor words> [operator] <value>` refs. Values:
  quoted, IPv4, id-like (`5-1781705361`), numbers, **mixed alphanumerics** (`pol47`), and
  **bare words after a connective** (`contains flood`, `is blacklist`). Operators: `eq`,
  `contains` (wildcard), `neq`/`ncontains` (must_not).
- `_extract_existence_refs(text)` — "footprint field exists", "no vlan", "X is missing" →
  `exists` clause routed to `must` (present) or `must_not` (missing). **ES 1.x compatible**
  (bare `exists`, NOT `constant_score`/`missing`).
- `_field_tokens` + `_word_matches_tokens` — camelCase-aware token matching so `attack id` →
  `attackIpsId`, `source ip` → `sourceAddress`; short words (`ip`,`id`) need exact token match.
- `_resolve_field_scored` / `_candidate_fields` — **confident** match (all words) is applied;
  **partial** match becomes a **suggestion** returned in `response.suggestions` (frontend lets
  the user pick from candidate fields per index). e.g. `radware id` on an index lacking it →
  suggests `attackIpsId`.
- **Data-driven enums** (`_get_enum_maps`, `_enum_pattern`, `_get_enum_maps` cache 120s):
  `status`/`risk`/`category` values + exact casing come from a `terms` aggregation, so
  `terminated` → the stored `Terminated`, and multiple stored variants (`Terminated` AND
  `TerminaTed`) are BOTH matched (grouped by normalized form → `IN` clause). Small built-in
  fallback + synonyms (`closed`→`Terminated`) only when the canonical value exists.
- **Per-group gating**: category/status/risk/blocking clauses are applied to an index group
  only if that field exists in the group's mapping (fixed querying `status` on
  `dp-attack-extra-*` which has no such field).
- Sort field resolution: `_resolve_sort_field` filters `doc_values:false` date fields; the
  earlier `search_type=scan` was replaced with a **plain scroll** (`_scroll_hits`) that some
  ES builds require.

### `routers/query.py` — data endpoints
- `POST /api/query/multi-run` — runs per-index queries, merges + globally re-sorts hits.
- `POST /api/doc/update` — set/delete ONE field on ONE doc (by `_id`, resolves `_type`, re-index).
- `POST /api/docs/bulk-delete` — delete whole docs (explicit selection OR whole query via scroll).
- `POST /api/docs/bulk-field` — set/delete a field across many docs (selection OR whole query).
- `POST /api/query/export` — **StreamingResponse** CSV/JSON of ALL matching docs via scroll
  (flat memory; partial data preserved on mid-stream failure). CSV columns from top-level
  mapping fields; `max_rows` cap 100k.

### `routers/indices.py`
`GET /{index}/sample` now includes `_id`/`_index` per hit (needed for edit/delete).

---

## Frontend work (this session) — `frontend/static/js/app.js`

### Layout / navigation
- Collapsible left sidebar (hamburger), resizable Query/Results split, collapsible sidebar
  index categories (persisted).

### Results viewer (Query Editor)
- **JSON / Table / CSV** view toggle, **Download** (shown rows), **☁ All** (server-side scroll
  export), **Read-only/Write** toggle, **⤢ pop-out** to a separate window.
- Rich **Table**: sticky header, per-column **sort**, per-column **value filter** (checkbox list
  with search + "(Select all)"), **frozen column order** (deleting a field doesn't reorder).
- **Write mode** (off by default) gates all edits: per-cell **✎ edit / 🗑 delete**, **row
  selection** (+ select-all) → bulk delete documents, **column selection** → edit/delete field
  with **viewed vs ALL matching** scope choice (warns about inconsistency when > 10,000).
- **Missing field cells** render red-gray with a tooltip + **＋ add** control.
- **Field suggestions** panel (from translate) — pick a candidate field to apply per index.

### Two independent viewers (key architecture)
`activateViewer('query'|'index')` swaps a saved state bundle (hits/total/json/cols/view/sort/
filters/selection + container ids `RV` + `activeContextItems`) into shared globals, so the
**Query Editor** and **Index Detail** each keep their own data while sharing ALL render/control
code. `showView('query')` and `showIndexDetail()` activate the right viewer.

### Index Detail
Clicking an index shows stats + the **same full viewer** (JSON/Table/CSV + all controls +
pop-out + export-all) over the index's sample docs.

### Pop-out window
Mirrors the active viewer with its **own** JSON/Table/CSV toggle + download/All/Write buttons.
Interactive handlers are bound onto the pop-out `window`; they run in the main context and
re-render both windows (`refreshTables` → `renderResultViews`). Loads `style.css` + icons.

### Dialogs
All `prompt`/`confirm`/`alert` replaced with **doc-aware modals** (`uiPrompt`, `uiConfirm`,
`chooseScopeDialog`) that render in whichever window (main or pop-out) triggered them — the
triggering element's `ownerDocument` is threaded through `editCell`/`deleteCell`/`columnFieldOp`/
`deleteSelectedRows`.

### Dashboard
CC Indices Overview has a Table/CSV toggle + Download CSV.

---

## Known caveats / possible next steps
- Edits/deletes write directly to ES (no undo); gated behind Write mode.
- Selection/edit actions do a **full table re-render** (resets scroll) to keep both windows
  consistent.
- The pop-out's **☁ All** spinner shows on the main toolbar button (export still works).
- Export/bulk `max_rows` cap = 100k; whole-column "viewed" on huge sets warns about
  inconsistency (reload the index to refresh).
- No automated tests yet. Candidate: unit tests for `_extract_field_refs`,
  `_resolve_field_scored`, `_enum_pattern`, `_scroll_hits`.
- Consider `DELETE /_search/scroll` cleanup after large exports.
