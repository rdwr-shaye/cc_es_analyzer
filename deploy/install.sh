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
#   0. Verifies the docker prerequisites and installs them from the DISTRO
#      packages if missing (never by piping a remote script into a shell), then
#      starts and enables the daemon. Opt out with --no-docker-install.
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
#   ./deploy/install.sh --no-docker-install   # check docker, never install it
#   HOST_PORT=9000 ./deploy/install.sh  # publish the app on a different host port
set -euo pipefail

SSL="true"
UPDATER="true"
DOCKER_INSTALL="true"
for arg in "$@"; do
  case "$arg" in
    --no-ssl|--http) SSL="false" ;;
    --ssl|--https)   SSL="true"  ;;
    --no-updater)    UPDATER="false" ;;
    --no-docker-install) DOCKER_INSTALL="false" ;;
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

# ── Docker prerequisites ─────────────────────────────────────────────────────
# Everything below this point assumes docker and a compose implementation. On a
# CC they are already there; on an engineer's own Linux box, often not, and the
# old behaviour was to fail here with "install Docker first", which is a
# instruction, not an installer.
#
# Deliberately uses the DISTRO PACKAGE MANAGER rather than the convenience
# script at get.docker.com. Piping a remote script into a root shell is exactly
# the supply-chain shape this project refuses elsewhere, and a support tool that
# does it on a customer-adjacent machine would be indefensible in review. The
# distro packages are signed by a repo the host already trusts. If they are not
# available, the script SAYS what to run rather than reaching for the pipe.
#
# Skip with --no-docker-install to check and report without changing anything.
run_as_root() {
  if [ "$(id -u)" = "0" ]; then "$@"; else sudo "$@"; fi
}

docker_daemon_ok() { docker info >/dev/null 2>&1; }

install_docker_packages() {
  local mgr=""
  for candidate in dnf yum apt-get zypper; do
    if command -v "$candidate" >/dev/null 2>&1; then mgr="$candidate"; break; fi
  done
  if [ -z "$mgr" ]; then
    echo "install.sh: no supported package manager (dnf/yum/apt-get/zypper) found." >&2
    return 1
  fi
  echo "install.sh: installing Docker with $mgr — this needs root and network access."
  case "$mgr" in
    apt-get)
      run_as_root apt-get update
      # docker.io + the compose PLUGIN; docker-compose-v2 is the plugin package
      # on current Debian/Ubuntu, and the older standalone binary is the fallback.
      run_as_root apt-get install -y docker.io         || return 1
      run_as_root apt-get install -y docker-compose-v2         || run_as_root apt-get install -y docker-compose         || echo "install.sh: no compose package available from apt; continuing to check." ;;
    dnf|yum)
      run_as_root "$mgr" install -y docker docker-compose-plugin         || run_as_root "$mgr" install -y docker         || return 1 ;;
    zypper)
      run_as_root zypper --non-interactive install docker docker-compose         || return 1 ;;
  esac
}

ensure_docker() {
  if command -v docker >/dev/null 2>&1 && docker_daemon_ok      && { docker compose version >/dev/null 2>&1 || command -v docker-compose >/dev/null 2>&1; }; then
    echo "install.sh: docker + compose present."
    return 0
  fi

  if [ "$DOCKER_INSTALL" != "true" ]; then
    echo "install.sh: docker prerequisites are missing and --no-docker-install was given." >&2
    return 1
  fi

  if ! command -v docker >/dev/null 2>&1; then
    echo "install.sh: docker not found."
    install_docker_packages || return 1
  fi

  # A freshly installed docker is usually stopped and not enabled at boot. This
  # is also the fix when docker was already installed but the daemon was down,
  # which reads identically to "not installed" from the caller's side.
  if ! docker_daemon_ok; then
    if command -v systemctl >/dev/null 2>&1; then
      echo "install.sh: starting and enabling the docker service."
      run_as_root systemctl enable --now docker || true
    fi
  fi

  if ! docker_daemon_ok; then
    echo "install.sh: the docker daemon is still not responding." >&2
    echo "  Check: systemctl status docker" >&2
    echo "  If this user is not root, it may also need: usermod -aG docker $USER" >&2
    echo "  (log out and back in for the group to take effect)" >&2
    return 1
  fi

  if ! docker compose version >/dev/null 2>&1 && ! command -v docker-compose >/dev/null 2>&1; then
    echo "install.sh: docker works but no compose implementation is installed." >&2
    echo "  Install the compose plugin for your distro, e.g.:" >&2
    echo "    apt-get install docker-compose-v2   |   dnf install docker-compose-plugin" >&2
    return 1
  fi

  echo "install.sh: docker prerequisites satisfied."
}

if ! ensure_docker; then
  echo "install.sh: cannot continue without docker + compose." >&2
  exit 3
fi

# Pick a docker compose invocation (v2 plugin preferred, fall back to v1).
if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
else
  echo "install.sh: docker compose not found after the prerequisite check." >&2
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
