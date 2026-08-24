# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in this repository.

**[`AGENTS.md`](AGENTS.md) at the repo root is the authoritative description of this
project** — architecture, the capability/policy system, host access, the System Health
module, the frontend contract, deployment, and the roadmap. Read it first. This file is
the short version and the rules; it deliberately does not duplicate the detail.

## What this is

CC Admin (the repository is still named `cc_es_analyzer` — the rename is pending) is a
debugging and administration platform for Radware **CyberController** appliances:
a System Health dashboard, an Elasticsearch workbench, and a MariaDB browser.

Single-process **FastAPI** app serving both the REST API and a **no-build vanilla-JS SPA**.
There is no build step — edit `frontend/static/js/app.js` directly.

It ships in **two modes from one image**, selected by `ANALYZER_PROFILE`:

- `standalone` — an engineer runs it on their own machine and connects to a CC over the
  network. This is the only way to reach the CCs already in the field, so it is a shipping
  product, not a dev mode. **A regression here is a regression for every CC in the field.**
- `embedded` — rides the CC's *monitoring* docker-compose (deliberately not the system one).

## Commands

```bash
pip install -r requirements.txt
```

```bash
python main.py
```

```bash
python tests/test_system_checks.py && python tests/test_system_safety.py && python tests/test_profile_surface.py && python tests/test_discovery_live_match.py && python tests/test_auth_lifecycle.py && python deploy/host_agent.py --self-test
```

The app runs on `http://localhost:8000`; interactive API docs at `/docs`.

## Configuration

Copy `.env.example` to `.env`. See `config.py` — every setting is a `Field` with an alias
and a comment explaining why it exists. The ES connection can also be changed at runtime
via `POST /api/connect` (the UI's "Connect" button).

## Layout

```
main.py            FastAPI app, _MANIPULATIONS table, session middleware, SPA catch-all
config.py          Pydantic Settings
core/              policy, hostexec, sessions, updater, tls, remote/ (SSH), routers/
modules/           one package per capability area: system, es, maria
deploy/            host_agent.py, update_agent.sh, nginx detection, compose snippets
frontend/          index.html + static/js/app.js + static/css/style.css
tests/             offline unit tests for the health parsers and the delete safety gates
VERSION            single source of truth for the app version
```

## Rules

1. **Never commit `scripts/attack_id_report - Copy.py`.** Stage by explicit name;
   never `git add -A` in this repo.
2. **Never commit or push without an explicit go-ahead in that turn.**
3. **The GitHub remote is PUBLIC.** Nothing derived from Radware's internal knowledge
   base, no customer data, and no credentials may be committed. `.github/system_operations.md`
   is gitignored for exactly this reason, and the embedded login's default password ships
   as a **scrypt hash** (`core/auth.py`) — never add the plaintext to this repo.
4. **Capabilities are gated by ROUTE REGISTRATION, not a runtime check** (`core/policy.py`,
   the loop in `main.py`). A disabled capability's routes are absent from `/openapi.json`
   and 404 on a direct call. Do not replace this with an `if policy.enabled()` in a handler.
5. **Host access is an operation allowlist, never a command channel** (`core/hostexec.py`).
   Adding an op there does nothing until the matching op is added to `deploy/host_agent.py`
   on the host. That asymmetry is the security feature — the host consents last.
6. **Do not relax `modules/system/safety.py`** (which files may be downloaded or deleted)
   without stating which gate moved and why. Backups are protected absolutely.
7. **Do not switch ES away from raw `requests`** — `elasticsearch-py`'s product-check
   rejects the proxied/older ES servers common in CC deployments.
8. **Any new endpoint that changes CC data must be added to `_MANIPULATIONS` in `main.py`** —
   that table is the security boundary and the hook for the coming audit trail.
9. A health check that cannot run reports `unknown`, never `ok`.
   Severity order: `ok < unknown < warn < crit`.
10. Comments here explain **why**, at length, especially around security decisions.
    Match that density.
