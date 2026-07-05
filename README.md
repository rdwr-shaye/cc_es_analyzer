# CC Elasticsearch Analyzer

A Python FastAPI service with a Web UI dashboard for analyzing CyberController (CC) Elasticsearch data on a remote Linux machine.

## Features

- **Dashboard** — Cluster health, shard stats, ES version, attack charts (by category + over time)
- **Index Explorer** — Browse all indices in the sidebar; click any index to see stats + sample documents
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

Build and run locally:
```bash
docker compose up -d --build         # serves on http://localhost:8801
# or a plain docker run:
docker build -t cc_es_analyzer:latest .
docker run -d --name cc_es_analyzer --restart unless-stopped -p 8801:8000 cc_es_analyzer:latest
```

The container is self-contained (Elasticsearch connection details are entered in the
UI at runtime). Logs are kept in the `cc_es_analyzer_logs` volume (`/app/logs`).

## Deploy to a remote Linux host

`deploy/deploy.py` packs the project, uploads it over SSH/SFTP, and runs
`deploy/remote_deploy.sh` on the host to build + start a single isolated container.
It is scoped to one uniquely named container/image and **never touches other
containers** on the host (no prune, no stopping other stacks). The host port
defaults to **8801** and auto-advances if that port is already taken.

```bash
pip install paramiko
python deploy/deploy.py --host 10.27.20.24 --user root      # prompts for password
# or non-interactive:
python deploy/deploy.py --host 10.27.20.24 --user root --password '***' --port 8801
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
python deploy/setup_nginx.py --host 10.27.20.24 --user root
```

The vhost config lives in `deploy/nginx/cc-analyzer.conf`. To use it from a
browser, point the hostname at the VM (add to your OS hosts file):

```
10.27.20.24  cc-analyzer  cc-analyzer.local
```

Then open **http://cc-analyzer/** (HTTP, no cert warning) — or
`https://cc-analyzer/` (self-signed cert warning is expected).

### Alternative: SSH tunnel (no nginx / no hosts change)
```bash
python deploy/tunnel.py --host 10.27.20.24 --user root
# then open http://localhost:8801/
```



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

