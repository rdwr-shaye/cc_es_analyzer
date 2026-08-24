# Copilot instructions — CC Admin (`cc_es_analyzer`)

**Read [`AGENTS.md`](../AGENTS.md) at the repo root first.** It is the current, elaborated
description of this project: architecture, the capability/policy system, host access, the
System Health module, the frontend contract, deployment, and the roadmap. `CLAUDE.md` and
`docs/` are stale — do not follow their architecture tree.

## The 60-second version

Single-process **FastAPI** app + a **no-build vanilla-JS SPA** (`frontend/static/js/app.js`,
edit directly). It is a **CyberController debugging platform**: System Health dashboard,
Elasticsearch, MariaDB.

Layout: `main.py`, `config.py`, `core/` (policy, hostexec, sessions, updater, remote/SSH),
`modules/{system,es,maria}/` (each declares its own capabilities + routers),
`deploy/` (host agent, nginx, compose snippets), `tests/`.
Root `routers/` and `services/` are **empty leftovers** — the code moved to `core/` + `modules/`.

Two shipping deployment modes from one image: `standalone` (engineer's machine → a CC over
the network; the only way to reach CCs already in the field) and `embedded` (rides the CC's
monitoring compose). `ANALYZER_PROFILE` picks one. **A regression in standalone is a
regression for every CC in the field.**

## Rules

1. **Never commit `scripts/attack_id_report - Copy.py`.** Stage by explicit name; never `git add -A`.
   The remote is **public**: no credentials, ever. The embedded login's default password
   ships as a scrypt hash in `core/auth.py`, never as plaintext.
2. **Never commit or push without an explicit go-ahead.**
3. **Capabilities are gated by ROUTE REGISTRATION, not a runtime check** (`core/policy.py`,
   `main.py`). A disabled capability's routes are absent from `/openapi.json` and 404.
   Do not replace this with an `if policy.enabled()` inside a handler.
4. **Host access is an operation allowlist, never a command channel** (`core/hostexec.py`).
   Adding an op there does nothing until the matching op is added to `deploy/host_agent.py`
   on the host. That asymmetry is the security feature — the host consents last.
5. **Do not relax `modules/system/safety.py`** (which files may be deleted/downloaded)
   without stating which gate moved and why. Backups are protected absolutely.
6. **Do not switch ES away from raw `requests`** — `elasticsearch-py`'s product-check
   rejects the proxied/older ES servers common in CC deployments.
7. **Any new endpoint that changes CC data must be added to `_MANIPULATIONS` in `main.py`** —
   that table is the security boundary and the hook for the coming audit trail.
8. Health checks that cannot run report `unknown`, never `ok`. Severity order:
   `ok < unknown < warn < crit`.
9. Comments here explain **why**, at length, especially around security decisions. Match
   that density.

## Commands

```bash
pip install -r requirements.txt
```

```bash
python main.py
```

```bash
python tests/test_system_checks.py && python tests/test_system_safety.py && python tests/test_profile_surface.py && python tests/test_discovery_live_match.py && python tests/test_auth_lifecycle.py
```

## System operations catalogue (automation backlog)

[`system_operations.md`](./system_operations.md) is the generated implementation
brief for automating support procedures: every action item (scripts, `*.sh`/`*.py`,
Linux/CC commands, guided steps) mined from the Radware support knowledge base,
grouped by operation category, each with its purpose, exact steps, and verbatim
commands. Use it as the backlog/spec when implementing new host operations here.

- Source of truth for each entry is the linked KB article — **confirm destructive
  steps** (`rm -rf`, `--reset-database`, service stops) against it before automating.
- Implement each as an idempotent host op (`deploy/host_agent.py`) gated by the
  `core/hostexec.py` allowlist; never widen the allowlist beyond the listed steps.
- Regenerate after the knowledge base grows:
  `python knowledge_base/scan_action_items.py && python knowledge_base/gen_system_operations.py`.


