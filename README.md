# CC Elasticsearch Analyzer

A Python FastAPI service with a Web UI dashboard for analyzing CyberController (CC) Elasticsearch data on a remote Linux machine.

## Features

- **Dashboard** — Cluster health, shard stats, ES version, attack charts (by category + over time)
- **Index Explorer** — Browse all indices in the sidebar; click any index to see stats + sample documents
- **Manage Indices** — Create a new index (**+** next to the sidebar "Indices" header) or delete one (**Delete Index** in the detail view, with typed confirmation)
- **CSV Import/Export** — Export any result set to CSV, then re-import a CSV (same format) back into an index via **Import CSV** in the detail view
- **CC-Aware** — All known CC index prefixes (`dp-attack-raw-*`, `dp-ts-attack-raw-*`, `dp-traffic-agg-*`, etc.) are annotated with descriptions and grouped by category
- **Attacks View** — Tabular view of recent DefensePro attacks with status filter
- **Query Editor** — Run any Elasticsearch DSL query with built-in templates (match_all, active attacks, category aggregation)

## Quick Start

### 1. Install dependencies
```bash
cd cc_es_analyzer
pip install -r requirements.txt
```

### 2. Configure connection (optional)
```bash
cp .env.example .env
# Edit .env with your ES host/port
```

### 3. Run the service
```bash
python main.py
```

Open **http://localhost:8000** in your browser.

### 4. Connect to a remote ES
Click **Connect** in the top-right, enter the remote Linux machine's IP and Elasticsearch port (default 9200).

## Run in Docker

### Deploy on a Linux host (one command)

On any Linux machine with **git** and **docker**, the installer does everything —
starts the app over HTTPS and, if the host runs an nginx reverse proxy, publishes
the app at `/cc_es_analyzer/` on it automatically:

```bash
git clone https://github.com/rdwr-shaye/cc_es_analyzer.git
cd cc_es_analyzer
./deploy/install.sh
```

When it finishes, **both** of these work (the second only if an nginx proxy exists):

- `https://<host-ip>:8801/` — direct to the app (self-signed cert → one-time browser warning)
- `https://<host-ip>/cc_es_analyzer/` — through the host's nginx proxy on port 443

`install.sh` sets `SERVICE_SSL=true` in `.env` so the app serves TLS (correct for
HTTPS-only hosts), runs `docker compose up -d`, then runs
`deploy/setup_nginx_path.py --local` to patch nginx — auto-detecting the proxy
container, the upstream address, and the http/https scheme. If there's no nginx
proxy, that step is skipped cleanly and only the direct URL applies.

Options: `./deploy/install.sh --no-ssl` keeps the app on plain HTTP;
`HOST_PORT=9000 ./deploy/install.sh` publishes it on a different host port.

Click **Connect** in the UI to point it at your Elasticsearch host.

#### Manual (without the installer)

```bash
docker compose up -d --build     # serves on http://<LINUX-IP>:8801/
```

To use a different host port, set `HOST_PORT` (e.g. `HOST_PORT=9000 docker compose up -d --build`).

To update to the latest code later, just `git pull` and re-run `docker compose up -d`
(the `--build` is no longer required — the compose file sets `pull_policy: build`, so
`up -d` always rebuilds the image from the current source before starting):

```bash
git pull
docker compose up -d
```

### Build and run locally
```bash
docker compose up -d --build         # serves on http://localhost:8801
# or a plain docker run:
docker build -t cc_es_analyzer:latest .
docker run -d --name cc_es_analyzer --restart unless-stopped -p 8801:8000 cc_es_analyzer:latest
```

The container is self-contained (Elasticsearch connection details are entered in the
UI at runtime). Logs are kept in the `cc_es_analyzer_logs` volume (`/app/logs`).

### Serve over HTTPS

On hosts that don't allow plain HTTP, enable TLS with `SERVICE_SSL=true`. With no
certificate supplied, a self-signed one is generated on first start (browsers show a
one-time warning — expected for internal use):

```bash
SERVICE_SSL=true docker compose up -d --build   # then browse https://<LINUX-IP>:8801/
```

To mount your own certificate instead of the self-signed one, provide `SSL_CERTFILE`
and `SSL_KEYFILE` (PEM paths reachable inside the container) alongside `SERVICE_SSL=true`.
Running directly (no Docker) works the same way: `SERVICE_SSL=true python main.py`.

## Deploy to a remote Linux host

`deploy/deploy.py` packs the project, uploads it over SSH/SFTP, and runs
`deploy/remote_deploy.sh` on the host to build + start a single isolated container.
It is scoped to one uniquely named container/image and **never touches other
containers** on the host (no prune, no stopping other stacks). The host port
defaults to **8801** and auto-advances if that port is already taken.

```bash
pip install paramiko
python deploy/deploy.py --host <host> --user root      # prompts for password
# or non-interactive:
python deploy/deploy.py --host <host> --user root --password '***' --port 8801
```

After it prints `DEPLOY_RESULT=OK PORT=<n>`, open `http://<host-ip>:<n>/`.

Manage the container on the host (affects only this app):
```bash
docker logs -f cc_es_analyzer
docker restart cc_es_analyzer
docker rm -f cc_es_analyzer          # remove just this container
```

## Access through the host nginx reverse proxy

On hosts where a perimeter firewall only allows 80/443 (and those are served by the
existing `docs-platform` nginx reverse proxy), the analyzer is published via a
**dedicated name-based vhost** — it does **not** touch the docs routing. The docs
proxy routes by path on `server_name _` (owning `/`, `/api/`, `/mcp/`, `/images/`),
so the analyzer gets its own `server_name` instead of sharing that path space.

`deploy/setup_nginx.py` applies this safely: it backs up the proxy's source
template, appends our vhost (idempotent, marker-guarded, durable across image
rebuilds), then applies it live as a **separate** `conf.d/cc-analyzer.conf`,
validates with `nginx -t` (aborting if invalid — docs untouched), and does a
graceful `nginx -s reload`.

```bash
python deploy/setup_nginx.py --host <host> --user root
```

The vhost config lives in `deploy/nginx/cc-analyzer.conf`. To use it from a
browser, point the hostname at the VM (add to your OS hosts file):

```
<host-ip>  cc-analyzer  cc-analyzer.local
```

Then open **http://cc-analyzer/** (HTTP, no cert warning) — or
`https://cc-analyzer/` (self-signed cert warning is expected).

### Alternative: SSH tunnel (no nginx / no hosts change)
```bash
python deploy/tunnel.py --host <host> --user root
# then open http://localhost:8801/
```

### Alternative: share the docs proxy's path space (/cc_es_analyzer/)

If you'd rather reach it at `http://<host-ip>/cc_es_analyzer/` — same IP, no hosts
file edit, no separate hostname — `deploy/setup_nginx_path.py` inserts a
`location /cc_es_analyzer/` block directly into the docs proxy's own
`server_name _;` block(s). This is **more invasive** than the name-based vhost
above: it edits the shared docs template rather than adding a fully isolated
file, since a location can only take effect inside the server block that
actually matches the request. It still backs up the template first, applies
the live change to a copy, validates with `nginx -t`, and rolls back
automatically if validation fails — no other docs route is touched or removed.

```bash
# from your workstation, over SSH:
python deploy/setup_nginx_path.py --host <host> --user root
# or ON the host itself (what install.sh runs — no SSH):
python3 deploy/setup_nginx_path.py --local --skip-if-no-proxy
# then open http://<host-ip>/cc_es_analyzer/  (or https://<host-ip>/cc_es_analyzer/)
```

It auto-detects whether the app serves TLS (`SERVICE_SSL=true`) and proxies over
`https://` (with `proxy_ssl_verify off`, since the app's cert is self-signed) or
`http://` accordingly — so the `/cc_es_analyzer/` URL keeps working whether or not
the app port itself is HTTPS. `./deploy/install.sh` runs this step for you.



## Auto-start at Windows logon

The service can start automatically each time you log in to Windows, running hidden
(no console window) via a Scheduled Task. It launches with `--no-reload`, which runs a
single stable process (hot-reload is for interactive development only). Logs still go to
`logs/cc_es_analyzer.log`.

`main.py` also refuses to start if port 8000 is already in use, so a manual run and the
auto-started instance will never silently double-bind the port — whichever starts second
exits with a clear message.

Register the task (PowerShell, one-time):
```powershell
$proj = "C:\Users\ShayE\KVISION_PROJECT\cc_es_analyzer"
$pyw  = Join-Path $proj ".venv\Scripts\pythonw.exe"
$action   = New-ScheduledTaskAction -Execute $pyw -Argument "main.py --no-reload" -WorkingDirectory $proj
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName "CC ES Analyzer" -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force
```

Manage it:
```powershell
Start-ScheduledTask  -TaskName "CC ES Analyzer"   # start now (needs port 8000 free)
Stop-ScheduledTask   -TaskName "CC ES Analyzer"   # stop the auto-started instance
Disable-ScheduledTask -TaskName "CC ES Analyzer"  # keep task, skip at next logon
Enable-ScheduledTask  -TaskName "CC ES Analyzer"
Unregister-ScheduledTask -TaskName "CC ES Analyzer" -Confirm:$false   # remove entirely
```

The task runs only while you are logged in (it stops at logoff). For a service that runs
before login / survives logoff, register a boot-triggered task under the SYSTEM account instead.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/health` | Cluster health + ES version |
| `GET` | `/api/nodes` | Node stats (JVM, CPU, disk) |
| `GET` | `/api/indices` | List all indices with stats |
| `GET` | `/api/indices/catalog` | CC index catalog grouped by category |
| `GET` | `/api/indices/{name}/stats` | Detailed stats for one index |
| `GET` | `/api/indices/{name}/sample` | Sample documents (latest 10) |
| `POST` | `/api/indices/create` | Create a new empty index |
| `DELETE` | `/api/indices/{name}` | Delete an index (refuses system indices/wildcards) |
| `POST` | `/api/indices/{name}/import` | Bulk-import a CSV file as documents |
| `POST` | `/api/connect` | Update active ES connection |
| `POST` | `/api/query` | Generic ES query |
| `GET` | `/api/cc/attacks` | Recent DP attacks |
| `GET` | `/api/cc/attacks/summary` | Attack aggregations |
| `GET` | `/api/cc/traffic` | Traffic data summary |

Interactive API docs: **http://localhost:8000/docs**

## CC Index Coverage

| Prefix | Category |
|--------|----------|
| `dp-attack-raw-*` | DP Attacks |
| `dp-ts-attack-raw-*` | DP Attacks |
| `dp-ts-hourly-attack-*` | DP Attacks |
| `dp-ts-daily-attack-*` | DP Attacks |
| `dp-traffic-agg-*` | DP Traffic |
| `dp-traffic-five-min-agg-*` | DP Traffic |
| `dp-traffic-dailyagg-*` | DP Traffic |
| `dp-applications-*` | DP Applications |
| `dp-hourly-applications-*` | DP Applications |
| `dp-daily-applications-*` | DP Applications |
| `dp-baseline-portion-*` | DP Baselines |
| `dp-hourly-baseline-portion-*` | DP Baselines |
| `dp-daily-baseline-portion-*` | DP Baselines |
| `dp-connection-statistics-*` | DP Statistics |

