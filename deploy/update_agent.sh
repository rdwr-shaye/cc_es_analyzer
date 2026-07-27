#!/usr/bin/env bash
###############################################################################
# CC ES Analyzer — host-side update agent.
#
# The app runs in a container built from this checkout, so it can neither reach
# the git remote nor rebuild itself. This tiny agent runs ON the host beside the
# checkout and bridges that gap through one shared directory (bind-mounted into
# the container as /app/.update):
#
#   agent.json    written by us    — "an agent is alive here", + heartbeat
#   state.json    written by us    — what the last `git fetch` found
#   request.json  written by the app — "please update", picked up within ~5s
#   job.json      written by us    — step-by-step progress of that update
#
# The agent NEVER runs anything the app tells it to: the only action it can
# perform is a fast-forward of the branch this checkout already tracks, followed
# by `docker compose up -d`. A diverged or dirty checkout aborts the update.
#
# Usage:
#   deploy/update_agent.sh                 # watch (default): poll + serve requests
#   deploy/update_agent.sh --once          # single check, write state.json, exit
#   deploy/update_agent.sh --interval 600  # seconds between fetches (default 300)
#   deploy/update_agent.sh --install       # install+start a systemd service
#   deploy/update_agent.sh --uninstall     # stop and remove that service
###############################################################################
set -uo pipefail

SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"    # holds docker-compose.yml
STATE_DIR="${APP_DIR}/.update"
INTERVAL=300
MODE="watch"
SERVICE_NAME="cc-es-analyzer-updater"

while [ $# -gt 0 ]; do
  case "$1" in
    --once)       MODE="once" ;;
    --watch)      MODE="watch" ;;
    --install)    MODE="install" ;;
    --uninstall)  MODE="uninstall" ;;
    --interval)   INTERVAL="${2:-300}"; shift ;;
    --app-dir)    APP_DIR="$(cd "$2" && pwd)"; STATE_DIR="${APP_DIR}/.update"; shift ;;
    --state-dir)  STATE_DIR="$2"; shift ;;
    -h|--help)    sed -n '3,26p' "$0"; exit 0 ;;
    *) echo "update_agent: unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

log() { echo "[updater] $(date '+%Y-%m-%d %H:%M:%S') $*"; }

# ── systemd install/uninstall ────────────────────────────────────────────────
if [ "$MODE" = "install" ]; then
  if ! command -v systemctl >/dev/null 2>&1; then
    log "systemd not available — start it manually instead:"
    log "  nohup ${SCRIPT_PATH} --watch >>${APP_DIR}/logs/updater.log 2>&1 &"
    exit 1
  fi
  cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=CC ES Analyzer update agent (checks the repo, applies one-click updates)
After=docker.service network-online.target
Wants=docker.service

[Service]
Type=simple
ExecStart=${SCRIPT_PATH} --watch --app-dir ${APP_DIR} --interval ${INTERVAL}
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now "${SERVICE_NAME}" >/dev/null 2>&1
  log "installed and started ${SERVICE_NAME}.service"
  systemctl --no-pager -n 5 status "${SERVICE_NAME}" || true
  exit 0
fi

if [ "$MODE" = "uninstall" ]; then
  systemctl disable --now "${SERVICE_NAME}" >/dev/null 2>&1
  rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
  systemctl daemon-reload 2>/dev/null
  log "removed ${SERVICE_NAME}.service"
  exit 0
fi

# ── Repo layout ──────────────────────────────────────────────────────────────
command -v git >/dev/null 2>&1 || { log "ERROR: git not found on this host"; exit 3; }
REPO="$(git -C "$APP_DIR" rev-parse --show-toplevel 2>/dev/null)"
if [ -z "$REPO" ]; then
  log "ERROR: ${APP_DIR} is not inside a git checkout — nothing to update from."
  exit 3
fi
# Empty when the checkout root IS the app; "cc_es_analyzer/" when the app is a
# subfolder of a bigger repo (the Bitbucket monorepo layout).
SUBDIR="$(git -C "$APP_DIR" rev-parse --show-prefix 2>/dev/null)"
BRANCH="$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null)"
UPSTREAM="$(git -C "$REPO" rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null)"
REMOTE="${UPSTREAM%%/*}"; [ -n "$UPSTREAM" ] || { REMOTE="origin"; UPSTREAM="origin/${BRANCH}"; }
TRACK_BRANCH="${UPSTREAM#*/}"
REMOTE_URL="$(git -C "$REPO" remote get-url "$REMOTE" 2>/dev/null)"

if docker compose version >/dev/null 2>&1; then COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then COMPOSE="docker-compose"
else COMPOSE=""; fi

mkdir -p "$STATE_DIR" "${APP_DIR}/logs"

# ── JSON helpers (no jq dependency on the host) ──────────────────────────────
# Pure bash on purpose: this runs once per field of every commit message, and a
# helper process here (jq/python/sed) is both slow and one more thing that can
# be missing or misbehave on a locked-down host.
esc() {
  local s="$1"
  s="${s//\\/\\\\}"                 # backslash first, or we'd escape our own escapes
  s="${s//\"/\\\"}"
  s="${s//[$'\001'-$'\037']/ }"     # tabs, newlines and other control chars
  printf '%s' "$s"
}

write_json() {  # write_json <file> <json-body>
  local f="$1"; shift
  printf '%s' "$*" > "${f}.tmp" && mv -f "${f}.tmp" "$f"
}

# Commits on the remote branch that we don't have yet, restricted to our folder
# (so unrelated work elsewhere in a shared monorepo doesn't look like an update).
changes_json() {
  local range="$1" out="" first=1 line h d s
  while IFS=$'\x1f' read -r h d s; do
    [ -z "$h" ] && continue
    [ $first -eq 1 ] || out="${out},"
    first=0
    out="${out}{\"hash\":\"$(esc "$h")\",\"date\":\"$(esc "$d")\",\"message\":\"$(esc "$s")\"}"
  done < <(git -C "$REPO" log --no-merges -n 20 --date=iso-strict \
             --pretty=$'%h\x1f%ad\x1f%s' "$range" -- "${SUBDIR:-.}" 2>/dev/null)
  printf '[%s]' "$out"
}

heartbeat() {
  write_json "${STATE_DIR}/agent.json" \
    "{\"agent\":\"update_agent.sh\",\"repo\":\"$(esc "$REPO")\",\"app_dir\":\"$(esc "$APP_DIR")\",\
\"subdir\":\"$(esc "$SUBDIR")\",\"remote\":\"$(esc "$REMOTE")\",\"branch\":\"$(esc "$TRACK_BRANCH")\",\
\"remote_url\":\"$(esc "$REMOTE_URL")\",\"interval\":${INTERVAL},\"heartbeat\":$(date +%s),\
\"compose\":\"$(esc "$COMPOSE")\"}"
}

# ── The check ────────────────────────────────────────────────────────────────
do_check() {
  local err="" ok="true"
  if ! git -C "$REPO" fetch --quiet "$REMOTE" "$TRACK_BRANCH" 2>/tmp/cc_upd_fetch_err; then
    err="$(cat /tmp/cc_upd_fetch_err 2>/dev/null | head -3)"
    ok="false"
  fi
  local ref="${REMOTE}/${TRACK_BRANCH}"
  local head latest behind dirty local_ver remote_ver date
  head="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null)"
  latest="$(git -C "$REPO" rev-parse --short "$ref" 2>/dev/null)"
  behind="$(git -C "$REPO" rev-list --count "HEAD..${ref}" -- "${SUBDIR:-.}" 2>/dev/null)"
  dirty="$(git -C "$REPO" status --porcelain 2>/dev/null | head -20)"
  date="$(git -C "$REPO" log -1 --date=iso-strict --pretty=%ad "$ref" 2>/dev/null)"
  local_ver="$(cat "${APP_DIR}/VERSION" 2>/dev/null | tr -d '[:space:]')"
  remote_ver="$(git -C "$REPO" show "${ref}:${SUBDIR}VERSION" 2>/dev/null | tr -d '[:space:]')"
  [ -n "$behind" ] || behind=0
  [ -n "$remote_ver" ] || remote_ver="$local_ver"

  local dirty_json="false"
  [ -n "$dirty" ] && dirty_json="true"

  write_json "${STATE_DIR}/${1:-state.json}" \
"{\"ok\":${ok},\"error\":\"$(esc "$err")\",\"checked_at\":$(date +%s),\
\"repo\":\"$(esc "$REPO")\",\"subdir\":\"$(esc "$SUBDIR")\",\
\"remote\":\"$(esc "$REMOTE")\",\"branch\":\"$(esc "$TRACK_BRANCH")\",\
\"remote_url\":\"$(esc "$REMOTE_URL")\",\
\"local\":{\"version\":\"$(esc "$local_ver")\",\"commit\":\"$(esc "$head")\"},\
\"latest\":{\"version\":\"$(esc "$remote_ver")\",\"commit\":\"$(esc "$latest")\",\"date\":\"$(esc "$date")\"},\
\"behind\":${behind},\"dirty\":${dirty_json},\"changes\":$(changes_json "HEAD..${ref}")}"

  if [ "$ok" = "true" ] && [ "${behind:-0}" -gt 0 ]; then
    log "update available: ${local_ver} (${head}) -> ${remote_ver} (${latest}), ${behind} commit(s) behind"
  fi
}

# ── Applying an update ───────────────────────────────────────────────────────
JOB_ID=""; JOB_STEPS=""
job_write() {  # job_write <state> [error]
  write_json "${STATE_DIR}/job.json" \
    "{\"id\":\"$(esc "$JOB_ID")\",\"state\":\"$1\",\"error\":\"$(esc "${2:-}")\",\
\"updated\":$(date +%s),\"steps\":[${JOB_STEPS}]}"
}
job_step() {  # job_step <name> <status> [output]
  [ -n "$JOB_STEPS" ] && JOB_STEPS="${JOB_STEPS},"
  JOB_STEPS="${JOB_STEPS}{\"name\":\"$(esc "$1")\",\"status\":\"$(esc "$2")\",\"output\":\"$(esc "${3:-}")\"}"
  job_write "running"
}

do_apply() {
  JOB_ID="$1"; JOB_STEPS=""
  log "update requested (job ${JOB_ID}) by ${2:-unknown}"
  job_write "running"

  local out
  if ! out="$(git -C "$REPO" fetch "$REMOTE" "$TRACK_BRANCH" 2>&1)"; then
    job_step "fetch" "error" "$out"; job_write "error" "git fetch failed: $out"; return 1
  fi
  job_step "fetch" "ok" "fetched ${REMOTE}/${TRACK_BRANCH}"

  out="$(git -C "$REPO" status --porcelain 2>&1 | head -20)"
  if [ -n "$out" ]; then
    job_step "verify" "error" "$out"
    job_write "error" "the checkout has local modifications — refusing to update: $out"
    return 1
  fi
  job_step "verify" "ok" "working tree is clean"

  if ! out="$(git -C "$REPO" merge --ff-only "${REMOTE}/${TRACK_BRANCH}" 2>&1)"; then
    job_step "pull" "error" "$out"
    job_write "error" "fast-forward failed (the checkout has diverged): $out"
    return 1
  fi
  job_step "pull" "ok" "$out"

  if [ -z "$COMPOSE" ]; then
    job_write "error" "docker compose not found — code is updated, restart the app manually"
    return 1
  fi
  # pull_policy: build in docker-compose.yml, so this rebuilds the image.
  if ! out="$($COMPOSE -f "${APP_DIR}/docker-compose.yml" --project-directory "${APP_DIR}" up -d 2>&1 | tail -20)"; then
    job_step "rebuild" "error" "$out"; job_write "error" "container rebuild failed: $out"; return 1
  fi
  job_step "rebuild" "ok" "$(printf '%s' "$out" | tail -3)"

  # Wait for the new container to answer before declaring success.
  local port scheme code i
  port="$(grep -E '^HOST_PORT=' "${APP_DIR}/.env" 2>/dev/null | cut -d= -f2)"
  [ -n "$port" ] || port=8801
  scheme=http
  grep -qiE '^SERVICE_SSL=true' "${APP_DIR}/.env" 2>/dev/null && scheme=https
  for i in $(seq 1 60); do
    code="$(curl -sk -m 3 -o /dev/null -w '%{http_code}' "${scheme}://127.0.0.1:${port}/api/health" 2>/dev/null)"
    [ "$code" = "200" ] && break
    sleep 2
  done
  if [ "$code" = "200" ]; then
    job_step "health" "ok" "app answered on ${scheme}://127.0.0.1:${port}"
    job_write "done"
    log "update complete (job ${JOB_ID}) — now at $(git -C "$REPO" rev-parse --short HEAD)"
  else
    job_step "health" "error" "app did not answer (last HTTP ${code:-none})"
    job_write "error" "updated, but the app did not come back up — check: docker logs cc_es_analyzer"
  fi
  do_check
}

# ── Main ─────────────────────────────────────────────────────────────────────
heartbeat
if [ "$MODE" = "once" ]; then
  do_check
  exit 0
fi

log "watching ${REPO} (${REMOTE}/${TRACK_BRANCH}${SUBDIR:+, subfolder ${SUBDIR}}) every ${INTERVAL}s"
log "state dir: ${STATE_DIR}"
do_check
LAST_CHECK=$(date +%s)
while true; do
  heartbeat
  REQ="${STATE_DIR}/request.json"
  if [ -f "$REQ" ]; then
    # Take the request before acting so a crash can't loop on it.
    mv -f "$REQ" "${STATE_DIR}/request.processing"
    id="$(sed -n 's/.*"id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "${STATE_DIR}/request.processing")"
    by="$(sed -n 's/.*"requested_by"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "${STATE_DIR}/request.processing")"
    do_apply "${id:-manual}" "${by:-unknown}"
    rm -f "${STATE_DIR}/request.processing"
    LAST_CHECK=$(date +%s)
  fi
  NOW=$(date +%s)
  if [ $((NOW - LAST_CHECK)) -ge "$INTERVAL" ]; then
    do_check
    LAST_CHECK=$NOW
  fi
  sleep 5
done
