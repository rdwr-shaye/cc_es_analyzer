# AGENTS.md

This file provides guidance to AI coding agents (e.g., OpenAI Codex, Cursor, Copilot) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the service (with hot reload)
python main.py

# Run with a specific ES host
ES_HOST=192.168.1.100 python main.py
```

The app runs on `http://localhost:8000` by default. Interactive API docs at `http://localhost:8000/docs`.

## Configuration

Copy `.env.example` to `.env` and set:
- `ES_HOST`, `ES_PORT`, `ES_SCHEME` — Elasticsearch connection
- `ES_USER`, `ES_PASSWORD` — optional basic auth
- `ES_VERIFY_CERTS` — TLS cert verification (default false)
- `SERVICE_HOST`, `SERVICE_PORT` — binding for the FastAPI server

The ES connection can also be updated at runtime via `POST /api/connect`.

## Architecture

**Single-process FastAPI app** serving both the REST API and the SPA frontend.

```
main.py              — FastAPI app, logging setup, SPA catch-all route
config.py            — Pydantic Settings (reads .env)
routers/
  health.py          — /api/health, /api/nodes, /api/connect
  indices.py         — /api/indices, /api/indices/catalog, /api/indices/{name}/stats|sample
  query.py           — /api/query, /api/cc/attacks, /api/cc/attacks/summary, /api/cc/traffic
services/
  es_client.py       — ESHttpClient singleton; plain HTTP to ES (no elasticsearch-py)
  cc_indices.py      — CC_INDEX_CATALOG dict mapping known index prefixes → metadata
frontend/
  index.html         — Single HTML file loading the JS app
  static/js/app.js   — Vanilla JS frontend (no build step)
  static/css/style.css
```

## Key Design Decisions

- **No `elasticsearch-py`** — `es_client.py` uses raw `requests` to bypass the product-check that rejects older/proxied ES servers common in CyberController deployments. Do not switch to `elasticsearch-py`.
- **ES client singleton** — `get_client()` returns the module-level `_client`. Use `update_client()` to replace it (e.g., when connection settings change). Do not instantiate `ESHttpClient` directly in routers.
- **CC index catalog** — `cc_indices.py` owns `CC_INDEX_CATALOG` (prefix → description/category) and `resolve_prefix()`. All CC-aware index annotations flow through here. Add new index prefixes to this file, not inline in routers.
- **No frontend build step** — the frontend is vanilla JS. Edit `frontend/static/js/app.js` and `frontend/index.html` directly; there is no bundler or transpiler.
- **New API routes** — add a new file under `routers/` and register it in `main.py` with `app.include_router(...)`.
