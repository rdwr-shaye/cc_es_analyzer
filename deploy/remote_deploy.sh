#!/usr/bin/env bash
###############################################################################
# CC Elasticsearch Analyzer — remote deploy (runs ON the target host).
#
# SAFETY: this script is intentionally scoped to a SINGLE, uniquely named
# container/image and NEVER touches any other container, image, network or
# volume on the host. It does not prune, does not stop other stacks, and only
# removes a pre-existing container if it is EXACTLY our own ("cc_es_analyzer").
#
# Usage:  bash remote_deploy.sh [HOST_PORT]
#         HOST_PORT defaults to 8801 and auto-advances if already in use.
###############################################################################
set -euo pipefail

NAME="cc_es_analyzer"
IMAGE="cc_es_analyzer:latest"
INTERNAL_PORT=8000
BASE_DIR="/opt/cc_es_analyzer"
CTX_DIR="${BASE_DIR}/app"
REQ_PORT="${1:-8801}"

log() { echo "[deploy] $*"; }

command -v docker >/dev/null 2>&1 || { echo "[deploy] ERROR: docker not found on host"; exit 2; }

# ── Unpack the uploaded build context ────────────────────────────────────────
log "Preparing build context in ${CTX_DIR}"
rm -rf "${CTX_DIR}"
mkdir -p "${CTX_DIR}"
tar -xzf "${BASE_DIR}/context.tar.gz" -C "${CTX_DIR}"

# ── Pick a free host port (never reuse one already bound) ────────────────────
port_in_use() {
  local p="$1"
  # Listening TCP sockets (ss), plus ports already published by docker.
  if command -v ss >/dev/null 2>&1; then
    ss -ltnH 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]${p}\$" && return 0
  fi
  docker ps --format '{{.Ports}}' 2>/dev/null | grep -Eq "(:|>)${p}->" && return 0
  return 1
}

HOST_PORT="${REQ_PORT}"
for _ in $(seq 0 20); do
  if port_in_use "${HOST_PORT}"; then
    log "Port ${HOST_PORT} is in use — trying next."
    HOST_PORT=$((HOST_PORT + 1))
  else
    break
  fi
done
if port_in_use "${HOST_PORT}"; then
  echo "[deploy] ERROR: could not find a free host port near ${REQ_PORT}"; exit 3
fi
log "Using host port ${HOST_PORT}"

# ── Build the image (tagged uniquely; won't overwrite anything else) ─────────
log "Building image ${IMAGE} (this may take a couple of minutes)…"
docker build -t "${IMAGE}" "${CTX_DIR}"

# ── Replace ONLY our own container, if it exists ─────────────────────────────
if docker ps -a --format '{{.Names}}' | grep -qx "${NAME}"; then
  log "Existing '${NAME}' container found — removing just that one."
  docker rm -f "${NAME}" >/dev/null
fi

# ── Run, isolated: own name, own restart policy, single published port ───────
log "Starting container '${NAME}' on 0.0.0.0:${HOST_PORT} -> ${INTERNAL_PORT}"
docker run -d \
  --name "${NAME}" \
  --restart unless-stopped \
  -p "${HOST_PORT}:${INTERNAL_PORT}" \
  -e SERVICE_HOST=0.0.0.0 \
  -e SERVICE_PORT="${INTERNAL_PORT}" \
  "${IMAGE}" >/dev/null

# ── Verify liveness ───────────────────────────────────────────��──────────────
log "Waiting for the service to come up…"
ok=0
for _ in $(seq 1 15); do
  if curl -fsS "http://127.0.0.1:${HOST_PORT}/api/health" >/dev/null 2>&1; then
    ok=1; break
  fi
  sleep 2
done

echo
docker ps --filter "name=${NAME}" --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
echo
if [ "${ok}" = "1" ]; then
  log "SUCCESS — CC ES Analyzer is live. Open:  http://<host-ip>:${HOST_PORT}/"
  echo "DEPLOY_RESULT=OK PORT=${HOST_PORT}"
else
  log "Container started but health check did not pass yet. Recent logs:"
  docker logs --tail 40 "${NAME}" || true
  echo "DEPLOY_RESULT=STARTED_NO_HEALTH PORT=${HOST_PORT}"
fi

