# AGENTS.md — CC Admin (repo: `cc_es_analyzer`)

Guidance for AI coding agents (GitHub Copilot, Codex, Cursor, Claude Code) working in
this repository. It is written to be read **cold**: everything an agent needs to make a
correct change without prior conversation.

> **Name note.** The repository is still called `cc_es_analyzer`; the *product* is
> **CC Admin**. The rename is agreed but has not landed (it propagates into compose
> files, images, packages and docs). Expect both names in code and comments.

---

## 1. What this is

A **CyberController (CC) debugging platform**. It began as an Elasticsearch analyzer and
is growing into one surface over every CC datastore plus the health of the appliance
itself.

Single-process **FastAPI** app that serves both the REST API and a **no-build vanilla-JS
SPA**. ~16.5k lines of Python plus a ~10k-line `frontend/static/js/app.js`.

### Two supported deployment modes — both are shipping products

| Profile | What it is |
|---|---|
| `standalone` (default) | An engineer runs CC Admin on their own machine and points it at a CC over the network. **This is the only way to reach the CCs already in the field**, which do not carry the embedded build. Not a dev mode. |
| `embedded` | The copy that rides a modern CC's **monitoring** docker-compose and talks to the datastores beside it. |

One image, one codebase. `ANALYZER_PROFILE` selects the profile;
`core/policy.py` decides what the running instance may do.

**A regression in `standalone` is a regression for every CC in the field.** Any change
must be checked in both modes.

---

## 1a. Repositories and publishing

There are **two publish targets, structured differently**. Get this wrong and you push a
whole repo into someone else's monorepo folder.

| Target | Where | Branch | How |
|---|---|---|---|
| **GitHub** (primary) | `https://github.com/rdwr-shaye/cc_es_analyzer.git` — remote `origin` of this working copy | `main` | ordinary `git push origin main` |
| **Bitbucket** (mirror) | `https://bitbucket.org/rdwr/ams_qa_ai_toolkit.git` — a **shared monorepo**; this project is the **subfolder `cc_es_analyzer/`** | `dev` | manual sync from a *separate* clone; **this working copy has no Bitbucket remote** |

The Bitbucket monorepo has one folder per contributor (`AbhishekKoparde`, `GiladH`,
`cc_es_analyzer`, …). **Only ever touch `cc_es_analyzer/`.** The mirror is always behind
GitHub; GitHub is the source of truth.

**Sync recipe** (only after committing to GitHub `main`), run from the separate clone at
`C:\Users\ShayE\KVISION_PROJECT\ams_qa_ai_toolkit`:

1. `git fetch origin dev` and confirm 0/0 ahead/behind.
2. Mirror the *tracked* tree only:
   `git -C <analyzer> archive --format=tar HEAD | tar -xf - -C ams_qa_ai_toolkit/cc_es_analyzer`
   — `git archive HEAD` guarantees only tracked files copy, which is what excludes `.git`,
   `__pycache__` and the untracked `scripts/attack_id_report - Copy.py`.
3. `git add -A cc_es_analyzer`, review `git status` (must be exactly the batch diff), commit
   with a `cc_es_analyzer: …` prefix.
4. Push. **Never force-push a shared branch** — if `dev` diverged, stop and ask.

On Windows, Bitbucket network operations (fetch / ls-remote / push) must run through
PowerShell rather than the Bash sandbox. Credentials for `shayeven@bitbucket.org/rdwr/*`
are cached in the OS credential manager.

Commit messages in this repo are a full sentence describing the change in product terms —
`The SQL screen learns to edit a query without being retyped`, `MariaDB, read-only by
construction, reachable from both profiles`. Match that voice.

## 2. Commands

```bash
pip install -r requirements.txt
```

```bash
python main.py
```

Runs on `http://localhost:8000`, interactive docs at `/docs`. Hot reload is on unless you
pass `--no-reload`.

```bash
ANALYZER_PROFILE=embedded python main.py
```

```bash
python tests/test_system_checks.py && python tests/test_system_safety.py && python tests/test_profile_surface.py
```

Tests are plain scripts *and* pytest-compatible. There is no other test runner and no
build step for the frontend.

---

## 3. Repository layout (current — see §11 for the stale docs warning)

```
main.py                      FastAPI app, logging, session middleware, router assembly,
                             /api 404 guard, SPA catch-all
config.py                    Pydantic Settings — every tunable, reads .env
VERSION                      single source of truth for the app version

core/                        cross-cutting, datastore-agnostic
  policy.py                  profiles, Capability/Module registry, property-file unlock
  hostexec.py                host access: operation allowlist + agent/ssh/local backends
  sessions.py                per-browser session, presence, peer notification inboxes
  updater.py                 version check + one-click update (standalone only)
  tls.py                     self-signed cert generation for SERVICE_SSL
  remote/
    ssh_ops.py               SSHSession (paramiko) — run(), run_full(), key auth
    ssh_tunnel.py            local port-forward to a datastore behind a CC
    ssh_opener.py            connection opening helpers
    cred_store.py            Fernet-encrypted SSH credential store
  routers/
    policy.py                GET /api/policy
    presence.py              GET /api/presence, /peers · POST /name
    update.py                GET /api/update/status|job · POST /check|apply

modules/                     one package per capability area
  __init__.py                ENABLED tuple + discover(); adding a datastore = one line
  system/                    ← the System Health dashboard (landing page)
    __init__.py              Module + 7 capabilities
    checks.py                pure parsers + severity rules (no I/O — unit-testable)
    safety.py                the delete/download file classifier
    routers/dashboard.py     4 routers, 12 endpoints
  es/
    __init__.py  client.py  catalog.py  discovery.py  field_types.py
    routers/     health.py  indices.py  query.py  exports.py  artificial.py
  maria/
    __init__.py  client.py  catalog.py  credentials.py  blobs.py  writes.py
    routers/     browse.py  query.py  edit.py

deploy/
  host_agent.py              root systemd agent on the CC host (the other half of hostexec)
  update_agent.sh            host-side git fetch / fast-forward / compose rebuild
  nginx_detect.py            find whoever owns :443/:80 and read its config via `nginx -T`
  setup_nginx_path.py        publish the app at /cc_admin/ on that nginx
  deploy.py  tunnel.py  setup_nginx.py
  embedded-compose.snippet.yaml   what to paste into the CC's monitoring compose
  cc_admin.properties.sample      the capability-unlock property file

frontend/
  index.html                 single HTML file; __ASSET_V__ is stamped by main.py
  static/js/app.js           the whole SPA — edit directly, no bundler
  static/css/style.css

scripts/                     standalone utilities (see §11 — one file is never committed)
tests/                       test_system_checks.py, test_system_safety.py, test_profile_surface.py
docs/                        LIVE_INDEX_DISCOVERY.md, overview deck
```

`routers/` and `services/` at the repo root are **empty leftovers** (`__pycache__` only).
The code moved to `core/` + `modules/`. Do not add to them; delete them if you are
touching that area.

---

## 4. The capability system — read this before adding any endpoint

`core/policy.py` is the security spine. It exists so one image can ship to a customer's
production appliance and to an internal support laptop.

### How it works

```python
@dataclass(frozen=True)
class Capability:
    id: str                      # "es.artificial", "system.storage.delete"
    title: str
    profiles: tuple[str, ...]    # profiles that carry it with nothing further required
    unlockable: bool = False     # may the property file switch it on?
    note: str = ""

@dataclass(frozen=True)
class Module:
    id: str
    title: str
    capabilities: tuple[Capability, ...]
    routers: tuple                # ((router, gating_capability_id_or_None), ...)
```

`modules/__init__.py:discover()` imports each enabled package, calls its `MODULE()`
factory and registers the capabilities. Then `main.py`:

```python
for module in modules.discover():
    for router, gating_capability in module.routers:
        if gating_capability is None or policy.enabled(gating_capability):
            app.include_router(router)
```

**Gating is by REGISTRATION, not by a runtime check.** A disabled capability's routes are
never added to the app: absent from `/openapi.json`, **404** on a direct call. A hidden
menu item is not a control; a route that does not exist is. This is the claim a product
security review will actually test — do not weaken it by adding an `if policy.enabled()`
inside a handler instead.

The `/api/{full_path:path}` catch-all in `main.py:171` exists so a gated path 404s rather
than falling through to the SPA catch-all and answering `200` with HTML.

### Unlocking

Property file (product convention, java-style `key=value`):

```
/opt/radware/mgt-server/properties/cc_admin.properties
```

```
capability.system.storage.delete=true
```

Only `unlockable=True` capabilities can be switched on this way; anything else is logged
and ignored. **Resolution is cached at startup — changing the file requires a container
restart.** That is deliberate (routes are registered at import time, so a live re-read
would only let the two halves disagree).

`policy.snapshot()` — served at `GET /api/policy` — tells the UI which capabilities are
on so it can explain a disabled control instead of showing an unexplained grey box. It
deliberately **does not** leak the property file's path.

### The governing rule

> Read access, and edits to data that **already exists**, are part of debugging a live
> system, so they ship enabled. What does not is anything that **fabricates** data — on a
> customer's production CC, synthetic documents are indistinguishable from real ones once
> written.

### Current capabilities

| id | profiles | unlockable | notes |
|---|---|---|---|
| `app.self_update` | standalone | no | An appliance follows the CC release train; the updater cannot reach git from a customer network. |
| `es.read` | both | — | gates the whole module: if it is off, `discover()` drops `modules.es` rather than registering a console for a store you cannot read |
| `es.connect` | standalone | — | embedded the datastore is the one running beside the app; there is nothing to pick |
| `es.doc.write` | both | — | corrects data already there: `/api/doc/update` and the three `/api/docs/bulk-*` |
| `es.doc.import` | — | **yes** | bulk-loads a CSV of unknown provenance. Embedded, the sanctioned way to put data back on a CC is an **archive restore**, which carries data this tool exported from a CC in the first place |
| `es.index.create` | — | **yes** | a CC builds its own indices from its templates. Embedded, `GET /api/indices/possible` still lists every family it *could* produce — reading the catalog is diagnosis, creating from it is not |
| `es.index.delete` | both | — | deliberately available on a customer's CC: a corrupted index has to be removable by the engineer who found it |
| `es.archive.export`, `es.archive.restore` | both | — | export also covers `/download/{name}` and `DELETE /{name}`, the verbs that move data off the box |
| `es.index.duplicate` | — | **yes** | fabricates data |
| `es.artificial` | — | **yes** | fabricates data |
| `maria.read` | both | — | |
| `maria.query.raw` | both | — | read-only by construction |
| `maria.write` | both | — | single-cell edits, PK-scoped |
| `system.health` | both | — | four checks, changes nothing |
| `system.logs` | both | — | container log read + download |
| `system.storage.download` | both | — | same allowlist as delete |
| `system.storage.delete` | — | **yes** | + host agent `--allow-delete` (two keys) |
| `system.es.delete_index` | — | yes | **declared, not implemented** |
| `system.maria.repair` | — | yes | **declared, not implemented** |
| `system.maria.recreate` | — | yes | **declared, not implemented** |

The three "declared, not implemented" entries exist so `/api/policy` can name the reason
each disabled button is dead. They have no route and no host operation.

---

## 5. Host access — `core/hostexec.py` + `deploy/host_agent.py`

**The problem.** The embedded container genuinely cannot see the host: `docker inspect`
shows three read-only bind mounts and **no docker socket**. It cannot run
`docker compose ps`, its own `df` reports the container's filesystems, and
`mariadb-check` exists only *inside* the MariaDB container. Every System Health check
needs host access the app does not have.

**The design.** The app names an **operation**, never a command string. Two independent
copies of the allowlist:

* `core/hostexec.py` — what the app is willing to *ask for*.
* `deploy/host_agent.py` — what the host is willing to *do*. Lives outside the container,
  runs as root under systemd, re-validates the op name **and every argument** against its
  own table, and refuses + logs anything else.

The duplication is deliberate: the container gaining the spool mount does not gain host
execution. **The host consents last.**

### The operations table

| op | what the host runs |
|---|---|
| `compose.ps` | `docker compose --file $COMPOSE_FILE ps --all --format 'table {{.Service}}\t{{.Name}}\t{{.Status}}'` |
| `compose.expected` | `docker compose --file $COMPOSE_FILE config --services` |
| `container.logs` | `docker logs --timestamps --tail N <name>` |
| `disk.usage` | `df -PT` |
| `disk.largest` | `find <mount> -xdev -type f -printf '%s\t%p\n' \| sort -rn \| head -n N` |
| `maria.check` | `docker exec <discovered mariadb> mariadb-check --check --all-databases` |
| `file.delete` | agent-only, native `os.remove` — **no shell**, behind `--allow-delete` |
| `file.read` | agent-only, chunked base64 (2 GB ceiling, ~14 MB/s) |

Argument validation before anything leaves the process: container name
`[A-Za-z0-9_.-]{1,120}`; `lines` capped at 5000; `n` capped at 100; `mount` must be one
the preceding `disk.usage` actually reported, never free text.

Two flags matter and are non-obvious:

* **`ps --all`** — without it, *stopped* containers vanish from the listing entirely, so
  the count silently shrinks and a dead service reads as healthy. This caused a real
  false-green (`dfc` was `Exited (128)` and the tile stayed green).
* **`df -PT`** — POSIX format plus the TYPE column, which is what lets the parser drop
  the ~50 `overlay` rows a CC emits (one per container, all the same filesystem).
* **`config --services`** — resolves `COMPOSE_PROFILES` from the `.env` beside the compose
  file, giving the list of services that *should* be running. `reconcile_compose()`
  synthesises a `missing` row for anything expected but absent.

### Backends

| kind | when | mechanism |
|---|---|---|
| `agent` | embedded | JSON files through the bind-mounted spool: `<spool>/requests/<uuid>.json` → `<spool>/results/<uuid>.json`, plus `agent.json` heartbeat advertising `ops` and `allow_delete`. Mounted at `/app/.hostexec`. |
| `ssh` | standalone | `core/remote/ssh_ops.SSHSession` against the connected CC. Target follows `get_client().cc_host`, with `CC_SSH_HOST` / `CC_SSH_USER` / `CC_SSH_KEY` env fallbacks. |
| `local` | dev only | refused in the embedded profile. |
| `none` | — | the dashboard says so and prints the install hint. |

`backend()` returns `{kind, ok, detail, hint, allow_delete}`.

**A check that cannot run reports `unknown`, never `ok`.** The failure that matters is a
dashboard saying "healthy" because it could not look.

### Running the agent

```bash
sudo python3 /opt/radware/cc_admin/deploy/host_agent.py --install --allow-delete
```

Flags: `--install` / `--uninstall` / `--watch` (default) / `--once` / `--self-test` /
`--spool` / `--compose` / `--allow-delete`.

---

## 6. System Health module (`modules/system/`)

The landing page. Four checks, one verdict each, global state = most severe.

Severity vocabulary, ordered: **`ok < unknown < warn < crit`**. `unknown` outranks `ok`
on purpose.

| pane | source | rules |
|---|---|---|
| Containers | `compose.ps` + `compose.expected` | `(unhealthy)` / `Exited` / `Dead` / missing → **crit**; `health: starting` / `Restarting` / `Created` → **warn**; bare `Up` → **ok** (many CC services declare no healthcheck). |
| Storage | `df -PT` | `≥ disk_crit_pct` (90) → **crit**; `≥ disk_warn_pct` (80) → **warn**. Pseudo filesystems dropped by type. |
| Elasticsearch | existing ES client, no host access needed | any **non-`appconfig2`** index yellow → **warn**; **any** index red, `appconfig2` included → **crit**. |
| MariaDB | `maria.check` | any `error :` line → **crit**. |

`checks.py` is **pure functions over command output — no I/O**. These are the rules that
decide whether an engineer is told the box is fine, so they are tested without a CC.
Keep new rules there, not in the router.

### Endpoints

```
GET  /api/system/summary                        all four panes WITH rows + checked_at + took_ms
GET  /api/system/hostexec                       backend kind, reachability, install hint
GET  /api/system/containers
GET  /api/system/storage
GET  /api/system/databases
GET  /api/system/storage/largest?mount=&n=20    job-based (walks tens of GB)
GET  /api/system/storage/largest/{job_id}
GET  /api/system/containers/{name}/logs?lines=          [system.logs]
GET  /api/system/containers/{name}/logs/download        [system.logs]
GET  /api/system/storage/download?path=                 [system.storage.download]
POST /api/system/storage/delete                         [system.storage.delete]
```

**`/summary` carries full rows on purpose.** The tile bar and the drilldown render from
the *same object*, because when they were two separate fetches they disagreed — the user
saw a green tile over a red list. `summary()` fans the four checks out over a
`ThreadPoolExecutor` using `contextvars.copy_context()` (the ES client and SSH target
live in a `ContextVar` set by `session_middleware`, so a bare thread loses them).

`/summary` never fails as a whole: each pane carries its own `{severity, headline, error}`,
so a dead agent degrades the containers pane to `unknown` while ES still reports.

### The file safety classifier — `modules/system/safety.py`

Three gates, **deny wins**, evaluated in order:

1. **Directory deny** — `/opt/radware/storage/backup/` (the DB recovery procedure restores
   from these — deleting one destroys the recovery path for the exact failure the
   dashboard exists to spot), `/opt/radware/storage/dc_config/`,
   `/opt/radware/mgt-server/properties/`, `/opt/radware/box/`, and the OS tree
   (`/boot /etc /usr /bin /sbin /lib /lib64 /proc /sys /dev /root`).
2. **Name deny** — MariaDB internals (`aria_log.`, `ib_logfile`, `ibdata`, `ibtmp`,
   `undo_`, `mysql-bin.`, `.ibd/.frm/.myd/.myi/.par`), Lucene/ES internals
   (`segments_`, `write.lock`, `_state`, `translog`, `node_lock`), binaries
   (`.war/.jar/.so/.a/.o/.dll/.exe/.class/.pyc`), source, config, dumps, keys, archives
   and images. Also matched against a **de-compressed** name, so `service.jar.gz` is
   denied as a jar rather than falling through.
   Datastore docker volumes are denied via `/volumes/<name>/_data` + a name regex
   (`dbdata|osdata|esdata|pgdata|mysql|maria|postgres|redis|rabbit|prometheus|grafana`).
3. **Allowlist** — `.log .out .err .hprof .zip .dmp .txt`, or a name ending `_log`, or one
   of `syslog messages dmesg debug secure maillog cron boot faillog xferlog auth daemon
   kern user` (extension-less system logs; `syslog.1` was refused until this existed).

`annotate(files)` tags each `{bytes, path}` row with the verdict and, when refused, the
**reason** — a 403 that says "it is a backup" is actionable; a grey button is not.

**Two keys for deletion**: the `system.storage.delete` capability *and* the agent's
`--allow-delete`. Both are set on the host, by different mechanisms. Unlocking one alone
does nothing.

Download uses the **same** allowlist: the files an engineer may take a copy of are the
files they may remove, and having both is what makes deleting one safe.

---

## 7. Elasticsearch module (`modules/es/`)

The original product and still the differentiator. Do not rewrite it.

* **`client.py` uses raw `requests`, not `elasticsearch-py`** — deliberately, to bypass
  the product-check that rejects the older/proxied ES servers common in CC deployments.
  Do not switch.
* **Per-session client.** `set_session()` / `get_client()` bind a client to the browser
  session through a `ContextVar` set in `session_middleware`. Never instantiate
  `ESHttpClient` directly in a router; never assume a bare thread inherits the context
  (use `contextvars.copy_context()`).
* **`catalog.py`** owns `CC_INDEX_CATALOG` (prefix → description/category) and
  `resolve_prefix()`. Every CC-aware annotation flows through it — add new prefixes here,
  never inline in a router.
* **`discovery.py`** answers "what indices can this CC actually produce", as opposed to
  the catalog's "what index families exist".
* **`exports.py`** holds the in-process **job engine** (`_new_job` / `_finish_job` /
  `_JobCancelled`), shared by `artificial.py` and by System Health's `storage/largest`.
  **Job state is in memory — a restart loses running work.** Persisting it is a known
  Phase 1 item.
* Snapshot export/restore reaches the ES machine over the SSH stack in `core/remote/`.

## 8. MariaDB module (`modules/maria/`)

**Read-only by construction**, with one exception.

* `credentials.py` resolves the account at runtime: `MARIA_USER`/`MARIA_PASSWORD` env →
  the CC's own `/usr/local/bin/mysql` wrapper (a two-line script with `-u`/`-p` inline,
  bind-mounted read-only) → the config defaults. Reading the wrapper is what lets a CC
  that changes the account be followed without a rebuild.
* Every statement carries `maria_timeout_s` (15) and `maria_max_rows` (1000).
* `query.py` (`maria.query.raw`) is read-only by construction, not by string inspection.
* `edit.py` (`maria.write`) does **single-cell, primary-key-scoped** edits only.
* `keys` endpoint surfaces the index/relation map — "what does this key mean" was the
  question that made the browser usable.

---

## 9. Frontend (`frontend/static/js/app.js`)

Vanilla JS, **no build step, no framework, no bundler**. One 10k-line file. Edit directly.

Conventions to follow when adding a screen:

* `showView('<id>')` switches views; `REFRESHERS['<id>'] = (manual) => ...` wires the
  shared auto-refresh select; `HELP_CONTENT['<id>']` supplies the help panel. Every
  screen has all three.
* `__ASSET_V__` in `index.html` is stamped by `main.py:_spa_page()` from the VERSION plus
  asset mtimes, so an update cannot leave a browser running old JS against new HTML.
* `uiChoice(...)` is the shared multi-button dialog (supports an `icon` option).

### Auto-refresh contract (System Health)

The user asked for this explicitly and it is worth preserving:

> the REST call is sent by the client **without refreshing the UI**, and the UI updates
> **only after** the response arrives.

Consequences encoded in the System block:

* `_sysSummary` is **the one source** — tiles and drilldown both render from it.
* An in-flight guard (`_sysInFlight`) prevents overlapping polls.
* Nothing is blanked while a request is in flight; on failure the UI only repaints if the
  refresh was **manual**.
* The **drilldown survives auto-refresh**: `_sysScan` (largest-files results), `_sysOpenLog`
  (the open container log) and `_sysScanning` are cached and re-painted after each render,
  and scroll position is preserved. A row already deleted must not resurrect from a stale
  pane — `deleteFile()` marks `deleted` on both `_sysLargestFiles` and the `_sysScan` cache.

### Download vs. download-and-delete

Two different mechanics, on purpose:

* **Download alone** → plain navigation. Streams straight to disk, no memory ceiling.
* **Download *and* delete** → `fetch` → blob → size check → only then delete. A navigation
  gives the page no completion signal, so the delete would fire mid-transfer. A short read
  refuses the delete.

---

## 10. Session, presence and audit — `main.py`

`_MANIPULATIONS` (`main.py:75`) is a `method + path regex → description` table of **every
endpoint that changes CC data**. `session_middleware` matches the request against it,
inspects the response body (these endpoints report failures as `{"error": ...}` at HTTP
200) and notifies the other users connected to the same CC only when the change actually
happened.

**This table is the security boundary.** Any new mutating endpoint must be added to it.
It is also the intended hook for the Phase 1 authorization + immutable audit trail — extend
it, do not build a parallel mechanism.

Identity today is a **self-declared name** (`core/sessions.py`) and there is **no
authentication**. That is a known Phase 1 gap, not a design choice.

---

## 11. Rules an agent must not break

1. **Never commit `scripts/attack_id_report - Copy.py`.** Stage files by **explicit name**;
   **never `git add -A`**.
2. **Never commit or push without the user's explicit go-ahead in that turn.**
3. **Do not weaken registration-based gating** into a runtime `if` inside a handler.
4. **Do not add a host operation to `core/hostexec.py` alone** and assume it works — the
   matching op must be added to `deploy/host_agent.py` on the host. That asymmetry is the
   feature.
5. **Do not relax `modules/system/safety.py`** without saying which gate you moved and why.
   Backups are protected absolutely.
6. **Do not switch ES away from raw `requests`.**
7. **`CLAUDE.md` and `docs/` are stale** — `CLAUDE.md`'s architecture tree still describes
   the retired `routers/` + `services/` layout. **This file (`AGENTS.md`) is the current
   description.** If you update the architecture, update this file.
8. Comments in this codebase explain **why**, at length, especially around security
   decisions. Match that density — a terse patch in this repo reads as an unexplained one.

Known doc bug worth fixing: in `modules/system/__init__.py`, `system.storage.download` sits
*below* the `# ── Not built in this pass ──` comment which asserts these have no route and
no host op — but download has both (`dashboard.download_router`, op `file.read`). Move or
reword the comment.

---

## 12. Configuration (`config.py`)

All settings are `pydantic-settings` fields read from `.env` / environment.

| env | default | purpose |
|---|---|---|
| `ES_HOST` / `ES_PORT` / `ES_SCHEME` | localhost / 9200 / http | ES connection |
| `ES_USER` / `ES_PASSWORD` / `ES_VERIFY_CERTS` | "" / "" / false | optional basic auth, TLS |
| `MARIA_HOST` / `MARIA_PORT` | `config_kvision-infra-mariadb_1` / 3306 | resolves on the CC's `vision` docker network |
| `MARIA_CRED_FILE` | `/usr/local/bin/mysql` | the CC's mysql wrapper, read for credentials |
| `MARIA_TIMEOUT_S` / `MARIA_MAX_ROWS` | 15 / 1000 | guard rails on every statement |
| `HOSTEXEC_DIR` | `./.hostexec` | spool shared with `deploy/host_agent.py` |
| `COMPOSE_FILE` | `/deploy/config/docker-compose.yaml` | the CC's **system** compose (not the monitoring one carrying this app) |
| `DISK_WARN_PCT` / `DISK_CRIT_PCT` | 80 / 90 | storage thresholds — data, not constants |
| `ANALYZER_PROFILE` | `standalone` | `standalone` \| `embedded` (`lab` is an accepted alias) |
| `POLICY_FILE` | `/opt/radware/mgt-server/properties/cc_admin.properties` | capability unlock |
| `SERVICE_HOST` / `SERVICE_PORT` | 0.0.0.0 / 8000 | binding |
| `SERVICE_SSL` / `SSL_CERTFILE` / `SSL_KEYFILE` | false | self-signed pair generated if enabled without one |
| `EXPORTS_DIR` | `./exports` | server-side archives (mount it in Docker) |
| `SNAP_HOST_DIR` / `SNAP_ES_DIR` | `/opt/radware/tmp/es` / `/usr/share/opensearch/backup` | snapshot repo, host path vs. ES's view of it |
| `UPDATE_DIR`, `UPDATE_*` | | self-updater, standalone only |

> `policy_file` once defaulted to `cc_analyzer.properties` while the sample told operators
> to create `cc_admin.properties`. The unlock silently did nothing — the worst failure mode
> for a security control, because it reads as "the gate held". Fixed; keep the file name
> tracking the service name.

---

## 13. Deploying to a CC (embedded)

```bash
scp -r <repo> root@<cc>:/opt/radware/cc_admin/src
```

```bash
cd /opt/radware/cc_admin/src && docker build -q -t kvision/cc-admin:dev-local .
```

```bash
docker compose -f <monitoring-compose> up -d --no-deps --force-recreate cc-admin
```

The compose entry lives in `deploy/embedded-compose.snippet.yaml`; it must include the
hostexec bind mount:

```yaml
- /opt/radware/storage/data/cc-admin/hostexec:/app/.hostexec
```

Then install the agent on the host (§5). `deploy/setup_nginx_path.py` publishes the app at
`/cc_admin/` on whichever nginx owns :443 — it resolves the *port owner* back to a
container/unit/process and reads config from `nginx -T`, because container names differ per
host.

After a rebuild the container needs ~30 s; a 502 immediately after `up -d` is normal.

### Lab CC — 10.205.189.20

Key-based SSH is set up. CC Admin is deployed there at `/cc_admin/` as a hand-built image.
Its `cc_admin.properties` currently unlocks `system.storage.delete`, `es.artificial` and
`es.index.duplicate`, and the agent runs with `--allow-delete`, so **deletion is armed on
that box**. The monitoring compose has a backup at `.bak-preSystemDashboard`.

---

## 14. Verification expectations

A change to this repo is not done until:

1. **Parsers tested offline** — `tests/` runs without a CC. New severity rules go there.
2. **Both profiles start.** `ANALYZER_PROFILE=standalone` and `=embedded`; `/api/policy`
   lists the gated capabilities as disabled and their routes are **absent** from
   `/openapi.json`.
3. **The agent refuses what it should.** Hand-write a request JSON naming an op outside the
   allowlist, and one with a shell metacharacter in a container name — both refused and
   logged, nothing executed.
4. **No backend degrades honestly.** Stop the agent: the three host-dependent panes report
   `unknown` with an install hint, and the ES pane still reports.
5. **Live on the lab CC**, compared against the same commands run by hand over SSH.
6. **Standalone re-verified unchanged** — it is a shipping product.

---

## 15. Roadmap — where this is going

Full plan: `C:\Users\ShayE\.claude\plans\indexed-scribbling-fairy.md`.

The tool is being embedded into the CC itself, built and released by **CC's CI/CD**,
carried in the ISO/OVA/upgrade packages and declared in the **monitoring** compose —
deliberately not alongside the system services. That boundary is a constraint, not a
detail: this component *observes* the system and is held to a lighter privilege and blast
radius than the services that run it.

* **Phase 0** (parallel, blocking) — CI/CD onboarding, auth-integration spike, security-review
  checklist, Jira/Confluence access.
* **Phase 1 — make it shippable** (the security phase): capability registry ✅, identity +
  RBAC + immutable audit on `_MANIPULATIONS`, elevation flow for gated writes, updater
  disabled in embedded ✅, secrets hardening (`cred_store.py` keeps its key beside the
  data), data-residency policy for exports, **durable jobs**.
* **Phase 2 — multi-datastore**: PostgreSQL driver, CC topology discovery, cross-store
  investigation. Relational stores get the ES treatment — a **curated catalog** of the
  tables that matter, not a schema tree. Raw SQL is an escape hatch, not the front door.
* **Phase 3 — diagnostics and known issues**: log access, a **deterministic signature
  matcher first** (no LLM in the matching path), a KB with **mandatory provenance** (every
  suggestion cites its Jira key), and an authoring UI for Tier 3/4 — most of this knowledge
  is in engineers' heads, so authoring is the load-bearing piece, not the importer.
* **Phase 4 — guided remediation**: preconditions, dry-run, confirmation, rollback, audit.
* **Phase 5 — self-healing**: narrow, proven, reversible actions only; autonomy earned per
  action type from Phase 4 evidence.

**Open decision worth tracking:** capability gating is per *instance*, but risk is per
*target*. Embedded they are the same box. Standalone they are decoupled — a standalone
install carries every capability and can be pointed at a customer's production CC. So
"a customer's CC cannot have data fabricated on it" currently holds only for embedded.
Undecided; decide before the security review.

**Deliberately not doing:** rewriting the ES tooling; building a schema-tree SQL client;
starting with an LLM diagnosis engine; a frontend framework migration.

---

### The automation backlog — `.github/system_operations.md`

A generated (~6 MB) catalogue of **1,447 operational procedures** mined from the Radware
APSolute Vision / Cyber Controller support knowledge base — 5,995 articles parsed so far
against a target of 7,863. Each entry keeps the source article's exact guided steps and
verbatim commands, grouped by category (Licensing 498, Upgrade & Installation 556,
Networking 129, Backup & Restore 88, Services & Containers 19, Logs & Debug 17,
MariaDB 8, Monitoring & Health 10, …) and tagged with an advisory **impact**
(`modifying` / `read_only` / `informational`) plus a `modifying_actions` list.

This is the **implementation brief for Phases 3–5**: it is where the host operations that
CC Admin should automate come from. Rules when working from it:

* The **source KB article is the source of truth**, not the extracted text — commands were
  pulled heuristically and may carry trailing prose. **Confirm every destructive step**
  (`rm -rf`, `--reset-database`, service stops) against the article before automating it.
* Implement each as an **idempotent host op** in `deploy/host_agent.py`, gated by the
  `core/hostexec.py` allowlist. **Never widen the allowlist beyond the listed steps.**
* Machine-readable companion: `knowledge_base/data/action_items.json` — consume `impact`
  to gate destructive automation.
* The generator lives **outside this repo** (`knowledge_base/scan_action_items.py`,
  `knowledge_base/gen_system_operations.py`); regenerate with
  `python knowledge_base/scan_action_items.py && python knowledge_base/gen_system_operations.py`
  after the KB grows.

## 16. Immediate outstanding work

* `system.maria.recreate` — wrap the CC's `repair_mysql_db.sh`: destroy the MariaDB
  container, recreate the schemas, restore the nightly dumps. For the case where MariaDB
  will not start at all (typically a corrupt Aria log). The heaviest action in the tool —
  it stops `vision` and loses everything written since the last dump.
* `system.es.delete_index` — delete a RED index. Fastest way back to a green cluster, and
  always a data loss.
* `system.maria.repair` — in-place `mariadb-check --repair` for the flagged tables.
* Fix the misplaced comment in `modules/system/__init__.py` (§11).
* Rewrite or retire the stale `CLAUDE.md` architecture section.
* Persist the job engine's state (Phase 1).
* Delete the empty `routers/` and `services/` directories.
* Rename `cc_es_analyzer` → CC Admin before it enters CC's CI/CD.
