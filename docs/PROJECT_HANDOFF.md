# CC Admin — project handoff

Written for an assistant picking this project up cold (GitHub Copilot / Claude in Copilot).
Read this first, then [`AGENTS.md`](../AGENTS.md) for the full technical detail.

Snapshot date: **2026-08-13**. App version: **1.2.0** (`VERSION`).

---

## 1. What the product is

**CC Admin** — a debugging and administration platform for Radware **CyberController (CC)**
appliances (APSolute Vision family). It began as an Elasticsearch analyzer and is becoming
one surface over the whole appliance: its health, its Elasticsearch/OpenSearch, its MariaDB,
and eventually its known-issue knowledge and remediation.

Technically: a **single-process FastAPI app** that serves both the REST API and a
**no-build vanilla-JS SPA**. ~16.5k lines of Python plus a ~10k-line `app.js`. No bundler,
no frontend framework, no build step.

### Who it is for and why it exists

Tier-4 engineers asked for it to ship **as part of the CC itself**, because customers resist
installing support tools in their data centres. Before it existed, a support engineer
answered "is this box healthy?" by SSHing in and running three commands by hand.

### Two deployment modes — both shipping products

| Mode | Description |
|---|---|
| `standalone` (default) | The engineer runs CC Admin on their own machine and points it at a CC over the network. **The only way to reach the CCs already in the field**, since they do not carry the embedded build. Not a dev mode. |
| `embedded` | Ships inside a modern CC, declared in the **monitoring** docker-compose (deliberately not alongside the system services), built and released by CC's own CI/CD, carried in the ISO/OVA/upgrade packages. |

One image, one codebase. `ANALYZER_PROFILE` selects the mode; `core/policy.py` decides what
the running instance may do. **A regression in `standalone` is a regression for every CC in
the field** — verify both modes on every change.

---

## 2. Repositories

| Target | URL | Branch | Notes |
|---|---|---|---|
| **GitHub** (source of truth) | `https://github.com/rdwr-shaye/cc_es_analyzer.git` | `main` | remote `origin` of the working copy at `C:\Users\ShayE\KVISION_PROJECT\cc_es_analyzer` |
| **Bitbucket** (mirror) | `https://bitbucket.org/rdwr/ams_qa_ai_toolkit.git` | `dev` | this project is the **subfolder `cc_es_analyzer/`** of a shared monorepo. This working copy has **no** Bitbucket remote — the sync runs from a separate clone at `C:\Users\ShayE\KVISION_PROJECT\ams_qa_ai_toolkit`. |

The Bitbucket monorepo holds one folder per contributor. **Only ever touch
`cc_es_analyzer/`.** The mirror is always behind GitHub. Full sync recipe in AGENTS.md §1a.

> The repo is still named `cc_es_analyzer`; the product is **CC Admin**. The rename is agreed
> but has not landed — it propagates into compose files, image names, packages and docs, and
> should happen before the project enters CC's CI/CD.

### Git working rules (non-negotiable)

1. **Never commit `scripts/attack_id_report - Copy.py`** — a personal working copy.
2. **Stage files by explicit name. Never `git add -A`** in this repo.
3. **Never commit or push without an explicit go-ahead from the user in that exchange.**
4. Commit messages are a sentence in product terms, e.g. *"MariaDB, read-only by
   construction, reachable from both profiles"*.

### Current git state

Last commit `5f7ac31` *"The SQL screen learns to edit a query without being retyped"*.
Branch `main`, in sync with `origin/main`.

**Everything below is uncommitted** — the entire System Health feature:

```
 M .gitignore  AGENTS.md  config.py  core/remote/ssh_ops.py  main.py  modules/__init__.py
 M deploy/cc_admin.properties.sample  deploy/embedded-compose.snippet.yaml
 M frontend/index.html  frontend/static/css/style.css  frontend/static/js/app.js
 ?? .github/  core/hostexec.py  deploy/host_agent.py  modules/system/  tests/
 ?? scripts/attack_id_report - Copy.py     ← NEVER COMMIT THIS
```

---

## 3. Architecture

```
main.py           FastAPI app · logging · session middleware · router assembly ·
                  /api 404 guard · SPA catch-all with asset-version stamping
config.py         Pydantic Settings — every tunable, reads .env
VERSION           single source of truth for the version

core/             cross-cutting, datastore-agnostic
  policy.py       profiles · Capability/Module registry · property-file unlock
  hostexec.py     host access — operation allowlist + agent/ssh/local backends
  sessions.py     per-browser session, presence, peer notifications
  updater.py      version check + one-click update (standalone only)
  tls.py          self-signed cert generation
  remote/         ssh_ops · ssh_tunnel · ssh_opener · cred_store (Fernet)
  routers/        policy · presence · update

modules/          one package per capability area; adding a datastore = one line in __init__
  system/         System Health dashboard (the landing page)
    checks.py     pure parsers + severity rules (no I/O — unit-testable without a CC)
    safety.py     the file delete/download classifier
    routers/dashboard.py   4 routers, 12 endpoints
  es/             client (raw requests) · catalog · discovery · field_types
                  routers: health · indices · query · exports (job engine) · artificial
  maria/          client · catalog · credentials · blobs · writes
                  routers: browse · query · edit

deploy/           host_agent.py · update_agent.sh · nginx_detect.py · setup_nginx_path.py
                  embedded-compose.snippet.yaml · cc_admin.properties.sample
frontend/         index.html · static/js/app.js · static/css/style.css
tests/            test_system_checks.py · test_system_safety.py
.github/          copilot-instructions.md · system_operations.md (the automation backlog)
```

Root-level `routers/` and `services/` are **empty leftovers** (`__pycache__` only) from the
pre-`core/`+`modules/` layout. Do not add to them.

---

## 4. The three load-bearing mechanisms

Understand these before changing anything. They are what makes the product shippable into a
customer's data centre.

### (a) Capability policy — gating by route registration

`core/policy.py`. Every feature declares a `Capability` (id, title, which profiles carry it,
whether a property file may unlock it). Every `Module` pairs each router with the capability
that gates it. `main.py` then does:

```python
for module in modules.discover():
    for router, gating_capability in module.routers:
        if gating_capability is None or policy.enabled(gating_capability):
            app.include_router(router)
```

**A disabled capability's routes are never registered** — absent from `/openapi.json`,
**404** to a direct call. A hidden menu item is not a control; a route that does not exist
is. This is the claim a product security review will actually test. **Never** replace it
with an `if policy.enabled()` check inside a handler.

Unlocking uses the product's existing convention — a java-style property file at
`/opt/radware/mgt-server/properties/cc_admin.properties` containing e.g.
`capability.system.storage.delete=true`. Only `unlockable=True` capabilities respond;
resolution is cached at startup, so **a change needs a container restart**.

The governing rule for what ships enabled:

> Read access, and edits to data that **already exists**, are part of debugging a live
> system. What does not ship enabled is anything that **fabricates** data — on a customer's
> production CC, synthetic documents are indistinguishable from real ones once written.

16 capabilities today; the table is in AGENTS.md §4.

### (b) Host access — an operation allowlist, not a shell

`core/hostexec.py` + `deploy/host_agent.py`.

The embedded container genuinely cannot see the host: three read-only bind mounts and **no
docker socket**. It cannot run `docker compose ps`, its `df` shows its own filesystems, and
`mariadb-check` only exists inside the MariaDB container.

So the app names an **operation** (`compose.ps`, `disk.usage`, `container.logs`,
`maria.check`, `file.delete`, `file.read`, …), never a command string. There are **two
independent copies of the allowlist**: one in the app (what it will ask for) and one in
`deploy/host_agent.py`, a root systemd process **outside the container** (what the host will
do). The agent re-validates the op name and every argument against its own table and refuses
anything else. **The host consents last** — adding an op to the app alone does nothing.

Backends: `agent` (JSON files through a bind-mounted spool, embedded), `ssh` (paramiko,
standalone), `local` (dev, refused in embedded), `none`.

**A check that cannot run reports `unknown`, never `ok`.** Severity order is
`ok < unknown < warn < crit`, precisely so a dashboard never says "healthy" because it
could not look.

### (c) Mutation table / audit hook

`_MANIPULATIONS` in `main.py:75` is a `method + path regex → description` table of every
endpoint that changes CC data. `session_middleware` matches against it, inspects the
response (these endpoints report failures as `{"error": ...}` at HTTP 200), and notifies the
other users on that CC only when the change really happened.

**That table is the security boundary.** Every new mutating endpoint must be added to it,
and it is the intended hook for the coming authorization + immutable audit trail.

Note: there is currently **no authentication** — identity is a self-declared name in
`core/sessions.py`. That is the largest known Phase 1 gap.

---

## 5. Feature areas

### System Health (`modules/system/`) — the landing page, newest work

Four checks, one verdict each, global state = the most severe:

| Pane | Source | Rules |
|---|---|---|
| Containers | `docker compose ps --all` + `config --services` | unhealthy / Exited / Dead / **missing** → crit; starting / Restarting / Created → warn; bare `Up` → ok |
| Storage | `df -PT` | ≥90% crit, ≥80% warn; pseudo filesystems dropped by type |
| Elasticsearch | the existing ES client | any non-`appconfig2` index yellow → warn; **any** index red (incl. `appconfig2`) → crit |
| MariaDB | `mariadb-check --check --all-databases` | any `error :` line → crit |

Three flags that are load-bearing and easy to lose:

* **`ps --all`** — without it, *stopped* containers vanish from the listing entirely. This
  caused a real false-green: 35 containers reported healthy while `dfc` was `Exited (128)`.
* **`config --services`** — resolves `COMPOSE_PROFILES` from the `.env` beside the compose
  file, giving what *should* be running; `reconcile_compose()` synthesises a `missing` row.
* **`df -PT`** — the TYPE column is what lets the parser drop the ~50 `overlay` rows a CC
  emits (one per container, all the same filesystem).

`/api/system/summary` returns **full pane rows**, and the tile bar and the drilldown render
from that same object — when they were two separate fetches they disagreed and the user saw
a green tile over a red list. The four checks fan out over a `ThreadPoolExecutor` using
`contextvars.copy_context()`, because the ES client and SSH target live in a `ContextVar`
that a bare thread would lose. Each pane carries its own `{severity, headline, error}`, so a
dead agent degrades one pane to `unknown` while the rest still report.

**Drilldowns**: container logs (view + download), the 20 largest files on a filesystem
(download / delete), RED ES indices, corrupted MariaDB tables.

**File safety classifier** (`modules/system/safety.py`) — three gates, deny wins:
directory deny (backups **absolutely** — the DB recovery procedure restores from them —
plus config, properties, `/opt/radware/box/` and the OS tree) → name deny (MariaDB and
Lucene internals, binaries, source, config, dumps, keys, archives; also checked against a
decompressed name so `service.jar.gz` is denied as a jar) → allowlist
(`.log .out .err .hprof .zip .dmp .txt`, names ending `_log`, and extension-less system logs
like `syslog`). Refusals carry a **reason**, so a 403 says *"it is a backup"*.

**Deleting takes two keys**: the `system.storage.delete` capability *and* the agent started
with `--allow-delete`. Both are set on the host by different mechanisms. Download uses the
same allowlist, on the principle that the files you may copy are the files you may remove.

### Elasticsearch (`modules/es/`) — the original product, the differentiator

Query editor with natural-language translation, a curated CC index catalog
(`catalog.py` → prefix ⇒ description/category), live index discovery ("what can this CC
actually produce"), attack/traffic analytics, CSV + snapshot archive export/restore over
SSH, artificial-data generation and index duplication (both gated — they fabricate data).

`client.py` uses **raw `requests`, not `elasticsearch-py`**, deliberately, to bypass the
product-check that rejects the older/proxied ES servers common in CC deployments. **Do not
switch.** The client is per-browser-session via a `ContextVar`.

`exports.py` holds the in-process **job engine** shared by artificial data and the
largest-files scan. **Job state is in memory — a restart loses running work.** Persisting it
is a Phase 1 item.

### MariaDB (`modules/maria/`) — read-only by construction

Schema/table browser with an index-and-relation map ("what does this key mean"), a
read-only SQL screen with an editable query, and single-cell **primary-key-scoped** edits
behind `maria.write`. Credentials resolve at runtime from env → the CC's own
`/usr/local/bin/mysql` wrapper → defaults, so a CC that changes the account is followed
without a rebuild. Every statement carries a 15 s timeout and a 1000-row cap.

### Frontend

One 10k-line `app.js`. Per screen: `showView('<id>')`, `REFRESHERS['<id>']`,
`HELP_CONTENT['<id>']`.

The auto-refresh contract the user specified explicitly, and which must be preserved:
**the REST call goes out without disturbing the UI, and the UI updates only after the
response arrives.** In practice: one source object (`_sysSummary`), an in-flight guard,
nothing blanked mid-request, failures only repaint on a *manual* refresh, and the drilldown
(scan results, open log, scroll position) survives every auto-refresh without re-navigating.

Download alone uses plain navigation (streams to disk); *download-and-delete* uses
fetch→blob→size-check→delete, because a navigation gives the page no completion signal and
the delete would fire mid-transfer.

---

## 6. Environment — the lab CC

**10.205.189.20**, key-based SSH configured. CC Admin is deployed there at `/cc_admin/` as a
hand-built image on the monitoring compose (backup of the compose at
`.bak-preSystemDashboard`).

**Deletion is currently ARMED on that box**: its `cc_admin.properties` unlocks
`system.storage.delete`, `es.artificial` and `es.index.duplicate`, and the host agent runs
with `--allow-delete`.

Deploy loop: `scp` the tree to `/opt/radware/cc_admin/src` →
`docker build -q -t kvision/cc-admin:dev-local .` →
`docker compose -f <monitoring-compose> up -d --no-deps --force-recreate cc-admin`. A 502
in the first ~30 s is normal. The compose entry must include the hostexec bind mount
`/opt/radware/storage/data/cc-admin/hostexec:/app/.hostexec`.

Verified live on that CC: refusals (403) for `/etc/passwd`, a nightly dump and an
`aria_log`; a 12 MB log deleted; a 10 GB test file deleted taking `/` from 93% → 60%; a
315 MB `syslog.1` downloaded byte-exact (md5 match) in 22.8 s.

---

## 7. Roadmap

Phases 0–2 are the current work; 3–5 are explicitly *not* in the first release, but the
present design must make them cheap to add.

* **Phase 0** (parallel, blocking) — CC CI/CD onboarding (registry, pinned base image, SBOM,
  CVE scanning, signing, ISO/OVA/upgrade packaging), auth-integration spike, obtain the
  security-review checklist *before* designing, Jira/Confluence access.
* **Phase 1 — make it shippable** (the security phase): capability registry ✅, updater
  disabled in embedded ✅, then identity + RBAC + immutable audit built on `_MANIPULATIONS`,
  an elevation flow for gated writes, secrets hardening (`cred_store.py` keeps its key beside
  the data), a data-residency policy for exports, and **durable jobs**.
* **Phase 2 — multi-datastore**: PostgreSQL driver, CC topology discovery ("what does this CC
  actually run"), cross-store investigation. Relational stores get the ES treatment — a
  **curated catalog of the tables that matter**, not a schema tree. Raw SQL is an escape
  hatch, not the front door.
* **Phase 3 — diagnostics and known issues**: log access and normalisation, a
  **deterministic signature matcher first** (no LLM in the matching path), a KB with
  **mandatory provenance** (every suggestion cites its Jira key), and an authoring UI for
  Tier 3/4 — most of this knowledge is in engineers' heads, so authoring is the load-bearing
  piece, not the importer.
* **Phase 4 — guided remediation**: preconditions, dry-run, confirmation, rollback, audit.
  Assisted only — the engineer decides, the tool executes correctly and reversibly.
* **Phase 5 — self-healing**: a narrow set of proven reversible actions, autonomy earned per
  action type from Phase 4 evidence.

**The automation backlog for phases 3–5 already exists**: `.github/system_operations.md`, a
generated ~6 MB catalogue of **1,447 procedures** mined from **5,995** parsed Radware support
KB articles (target 7,863), grouped by category and tagged with an advisory impact
(`modifying` / `read_only` / `informational`). Rules: the **source article is the truth**,
commands were extracted heuristically, **confirm every destructive step** before automating,
implement each as an idempotent host op gated by the allowlist, and never widen the
allowlist beyond the listed steps. The generator lives outside this repo under
`knowledge_base/`.

**Open decision to settle before the security review:** capability gating is per *instance*,
but risk is per *target*. Embedded they are the same box. Standalone they are decoupled — a
standalone install carries every capability and can be pointed at a customer's production CC.
So the guarantee "a customer's CC cannot have data fabricated on it" currently holds only for
embedded.

**Deliberately not doing:** rewriting the ES tooling; building a schema-tree SQL client
(DBeaver exists, and a raw table tree puts "which of these hundreds matters?" back on the
engineer); starting with an LLM diagnosis engine; a frontend framework migration.

---

## 8. What to pick up next

1. `system.maria.recreate` — wrap the CC's own `repair_mysql_db.sh`: destroy the MariaDB
   container, recreate the schemas, restore the nightly dumps. For the case where MariaDB
   will not start at all (typically a corrupt Aria log). The heaviest action in the tool —
   it stops `vision` and loses everything written since the last dump. Declared as a
   capability, not implemented.
2. `system.es.delete_index` — delete a RED index. Declared, not implemented.
3. `system.maria.repair` — in-place `mariadb-check --repair` on the flagged tables.
   Declared, not implemented.
4. Fix the misplaced comment in `modules/system/__init__.py` — `system.storage.download` sits
   under a "Not built in this pass" block that asserts these have no route and no host op,
   but download has both.
5. Rewrite or retire the stale architecture section in `CLAUDE.md` (it still describes the
   retired `routers/` + `services/` layout).
6. Persist the job engine's state.
7. Delete the empty `routers/` and `services/` directories.
8. Commit the System Health work (explicit go-ahead required; stage by name).
9. Rename `cc_es_analyzer` → CC Admin before CI/CD onboarding.

## 9. Verification bar

A change is not done until: parsers tested offline in `tests/`; **both profiles start** and
gated routes are absent from `/openapi.json`; the agent refuses an unknown op and a shell
metacharacter in an argument; with no backend the host-dependent panes report `unknown` with
an install hint while ES still reports; it is compared live on the lab CC against the same
commands run by hand over SSH; and **standalone is re-verified unchanged**.
