#!/usr/bin/env bash
#
# One-command installer for CC ES Analyzer, run ON the target Linux host after a
# `git clone`. It:
#   1. Enables HTTPS on the app by default (SERVICE_SSL=true in .env) so the app's
#      own port serves TLS — correct for HTTPS-only hosts. Opt out with --no-ssl.
#   2. Builds + starts the container (docker compose up -d; pull_policy: build).
#   3. Waits for the app to answer, then finds the host's nginx reverse proxy —
#      whatever it is called, in Docker or installed on the machine — and
#      publishes the app at /cc_es_analyzer/ on it (via
#      deploy/setup_nginx_path.py --local, detection in deploy/nginx_detect.py).
#      If there is no nginx proxy, that step is skipped cleanly and the app is
#      reachable directly on its port.
#   4. Installs the update agent (deploy/update_agent.sh) so the UI can tell
#      users when a newer version is in the repository and update in one click.
#
# Result: on a machine with the docs-platform nginx proxy, BOTH URLs work —
#   https://<host>/cc_es_analyzer/   (through nginx, port 443)
#   https://<host>:<HOST_PORT>/      (direct to the app; self-signed cert warning)
#
# Usage (on the host):
#   ./deploy/install.sh                 # HTTPS app + nginx path (recommended)
#   ./deploy/install.sh --no-ssl        # keep the app on plain HTTP
#   ./deploy/install.sh --no-updater    # skip the update agent
#   HOST_PORT=9000 ./deploy/install.sh  # publish the app on a different host port
set -euo pipefail

SSL="true"
UPDATER="true"
for arg in "$@"; do
  case "$arg" in
    --no-ssl|--http) SSL="false" ;;
    --ssl|--https)   SSL="true"  ;;
    --no-updater)    UPDATER="false" ;;
    -h|--help)
      sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "install.sh: unknown option: $arg" >&2; exit 2 ;;
  esac
done

# Move to the project root (this script lives in deploy/).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

HOST_PORT="${HOST_PORT:-8801}"
export HOST_PORT

# Pick a docker compose invocation (v2 plugin preferred, fall back to v1).
if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
else
  echo "install.sh: docker compose not found — install Docker first." >&2
  exit 3
fi

# 1) Set SERVICE_SSL in .env (idempotent: replace an existing line, else append).
touch .env
if grep -q '^SERVICE_SSL=' .env; then
  sed -i "s/^SERVICE_SSL=.*/SERVICE_SSL=${SSL}/" .env
else
  printf 'SERVICE_SSL=%s\n' "$SSL" >> .env
fi
echo "[install] SERVICE_SSL=${SSL}  (app port ${HOST_PORT} will serve $([ "$SSL" = true ] && echo HTTPS || echo HTTP))."

# 2) Build + start the app container. The .update directory is the handoff
#    point with the update agent and is bind-mounted by docker-compose.yml —
#    create it first so Docker doesn't make it root-only.
mkdir -p .update
echo "[install] Starting the app container …"
$COMPOSE up -d

# 3) Wait for the app to answer on its published port (http or https).
scheme=$([ "$SSL" = true ] && echo https || echo http)
echo "[install] Waiting for the app on ${scheme}://127.0.0.1:${HOST_PORT}/api/health …"
for i in $(seq 1 30); do
  code="$(curl -sk -m 3 -o /dev/null -w '%{http_code}' "${scheme}://127.0.0.1:${HOST_PORT}/api/health" || true)"
  [ "$code" = "200" ] && { echo "[install] App is up (HTTP 200)."; break; }
  sleep 1
  [ "$i" = 30 ] && echo "[install] WARN: app didn't return 200 yet (last=${code:-none}); continuing anyway."
done

# 4) Publish through the host's nginx reverse proxy (skipped cleanly if none).
#    Detection makes no assumption about names: it resolves whoever owns :443/:80
#    back to a container, a systemd service or a plain process. Run
#    `python3 deploy/nginx_detect.py` any time to see what it finds.
echo "[install] Looking for this host's nginx reverse proxy …"
PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then
  echo "[install] WARN: python3 not found — skipping nginx setup. The app is still reachable"
  echo "          directly at ${scheme}://<host>:${HOST_PORT}/ . Install python3 and run:"
  echo "          python3 deploy/setup_nginx_path.py --local --skip-if-no-proxy"
else
  "$PY" deploy/setup_nginx_path.py --local --skip-if-no-proxy --app-port "$HOST_PORT"
fi

# 5) Update agent: reports new versions to the UI and performs one-click updates.
if [ "$UPDATER" = "true" ]; then
  if ! git -C . rev-parse --show-toplevel >/dev/null 2>&1; then
    echo "[install] NOTE: not a git checkout — skipping the update agent (update checks"
    echo "          need the repository this was cloned from)."
  elif [ "$(id -u)" != "0" ] || ! command -v systemctl >/dev/null 2>&1; then
    echo "[install] NOTE: no systemd or not root — start the update agent yourself with:"
    echo "          nohup ${PROJECT_ROOT}/deploy/update_agent.sh --watch >>logs/updater.log 2>&1 &"
  else
    echo "[install] Installing the update agent …"
    ./deploy/update_agent.sh --install || echo "[install] WARN: update agent install failed (non-fatal)."
  fi
fi

echo
echo "[install] DONE."
echo "  Direct:      ${scheme}://<host-ip>:${HOST_PORT}/"
echo "  Via nginx:   https://<host-ip>/cc_es_analyzer/   (if an nginx proxy was found)"
echo "  Updates:     the UI shows a banner when the repo has a newer version."
