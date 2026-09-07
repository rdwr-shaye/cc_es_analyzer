"""Running a diagnostic command on the CC's HOST, from either profile.

The System dashboard asks three questions the app cannot answer from inside
itself: which containers are unhealthy, how full the disks are, and whether any
MariaDB table is corrupt. All three live on the host:

  * embedded, the cc-admin container has three read-only bind mounts and no
    docker socket — its own ``df`` describes the container, ``docker`` is not
    installed, and ``mariadb-check`` exists only inside the MariaDB container;
  * standalone, the host is a CC somewhere on the network.

So this module exists, and the shape it takes is the security decision of the
whole feature. It is NOT a remote shell. It is a fixed table of named
OPERATIONS, each with typed, validated arguments; a caller asks for
``compose.ps``, never for a command string. Two consequences worth stating
because a security review will ask both:

  1. adding an operation is a code change in two files, one of which lives on
     the host outside the container's reach (deploy/host_agent.py);
  2. nothing a caller can say — including a caller who has found a bug
     elsewhere in the app — turns into shell.

Two backends serve the same table:

  ``agent``  Embedded. The container writes a request JSON into a bind-mounted
             spool directory; deploy/host_agent.py, running as root on the
             host, picks it up, RE-VALIDATES it against its own independent
             allowlist, runs it, and writes the result back. The container
             never receives docker.sock, and gaining the spool mount does not
             give it host execution — only the ability to ask for one of the
             listed operations. This is the same bridge deploy/update_agent.sh
             already uses for updates, for the same reason.

  ``ssh``    Standalone. paramiko to the connected CC, reusing the credentials
             the engineer already gave for Elasticsearch. Here the command IS
             built locally, because there is no agent on the far side — which
             is exactly why the operation table, not the caller, builds it.

  ``none``   Neither is available. Every check then reports ``unknown``, never
             green. A dashboard that says "healthy" because it could not look
             is the failure mode that actually hurts someone.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import time
import uuid

from config import settings
from core import policy

logger = logging.getLogger(__name__)


class HostExecError(Exception):
    """The operation could not be run, or the host refused it. The message is
    written for the operator — it is what the dashboard shows in the pane."""


# ── Argument validation ──────────────────────────────────────────────────────
# Every value that reaches a command goes through one of these. They are
# deliberately strict allowlists rather than "escape the dangerous characters":
# there is no legitimate container name with a space in it, so accepting one
# and quoting it carefully is a larger surface than refusing it.

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")
# An absolute path with no shell metacharacters, no whitespace, and no "..".
# Only ever a MOUNT POINT in this pass; nothing here takes a file path.
_MOUNT_RE = re.compile(r"^/[A-Za-z0-9_./-]{0,255}$")


def _name(value) -> str:
    text = str(value or "")
    if not _NAME_RE.match(text):
        raise HostExecError(f"not a valid container name: {text!r}")
    return text


def _mount(value) -> str:
    text = str(value or "")
    if not _MOUNT_RE.match(text) or ".." in text:
        raise HostExecError(f"not a valid mount point: {text!r}")
    return text


def _bounded_int(lo: int, hi: int):
    def check(value) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise HostExecError(f"expected a number, got {value!r}") from None
        if not lo <= number <= hi:
            raise HostExecError(f"{number} is outside {lo}..{hi}")
        return number
    return check


# ── The operation table ──────────────────────────────────────────────────────
# `args` maps each accepted argument to its validator and default. `command`
# builds the shell line for the SSH backend ONLY — the agent backend never
# receives it and builds its own from the op name.
#
# `timeout` is per-operation because they are not comparable: `df` answers
# instantly, while walking /var/lib/docker for the largest files on a CC with a
# few hundred GB of images is minutes of I/O.

# Discovering the MariaDB container by name pattern rather than by a configured
# name, for the reason deploy/nginx_detect.py already documents: the compose
# project prefix differs per host (`config_kvision-infra-mariadb_1` here,
# something else on an appliance deployed differently), so a hardcoded name is
# a bug waiting for the next customer.
_FIND_MARIA = "docker ps --format '{{.Names}}' | grep -m1 -i mariadb"

# The credentials come off the host's own mysql wrapper — the same source
# modules/maria/credentials.py reads — so no password ever travels in a
# request. `grep` pulls the -u/-p pair out of the line that invokes the client.
#
# Not every appliance HAS a -u/-p pair to find. Confirmed on a second lab CC
# (10.205.189.21, an HA-config appliance): its /usr/local/bin/mysql wrapper is
# a bare `mysql "$@"` with nothing embedded at all — the product's own scripts
# on that box (net_utils.sh, lls_utils.sh) call `mysql -u root` with NO
# password, relying on MariaDB's unix_socket auth plugin to authenticate root
# by OS user once `docker exec` has already put us inside the container as
# root. Verified directly: `mariadb-check -uroot --check` succeeds there with
# no password at all. So when the wrapper yields nothing, fall back to trying
# root with no password — a real credential mismatch on some other box then
# surfaces as mariadb-check's own access-denied message instead of a
# misleading "no credentials" refusal, which is a strictly more honest
# failure. This account is not looked up anywhere else in the app — it is
# only ever used inside a `docker exec` on THIS host, over the unix socket,
# never over the network, so it grants nothing a network attacker could reach.
_MARIA_CREDS = (
    "creds=$(grep -m1 -E -- '-u[^ ]+ +-p[^ ]+' /usr/local/bin/mysql "
    "| grep -o -E -- '-u[^ ]+ +-p[^ ]+'); "
    "[ -n \"$creds\" ] || creds='-uroot'"
)


def _cmd_compose_ps(_args: dict) -> str:
    # --all, and it is not optional. Without it `docker compose ps` lists only
    # RUNNING containers, so a stopped service disappears from the output
    # entirely rather than appearing as Exited — which is how a CC with one
    # service down reported "all 35 services running" and showed a green tile.
    # The failure mode of an omitted row is silence, which is the worst one a
    # health check can have.
    compose = shlex.quote(settings.compose_file)
    return (f"docker compose --file {compose} ps --all "
            f"--format 'table {{{{.Service}}}}\\t{{{{.Name}}}}\\t{{{{.Status}}}}'")


def _cmd_compose_expected(_args: dict) -> str:
    # What SHOULD be running, which is a different question from what is.
    # `config --services` resolves COMPOSE_PROFILES from the .env file beside
    # the compose file, so a CC running the insight/plus/x/activation profiles
    # is told about those services and not about the ones its profiles exclude
    # (37 services in the file, 36 enabled, on the appliance this was built
    # against). Comparing the two lists is the only way to notice a service
    # that has no container at all.
    compose = shlex.quote(settings.compose_file)
    return f"docker compose --file {compose} config --services"


def _cmd_container_logs(args: dict) -> str:
    return (f"docker logs --timestamps --tail {args['lines']} "
            f"{shlex.quote(args['name'])} 2>&1")


def _cmd_disk_usage(_args: dict) -> str:
    # -PT, not -h: POSIX output is one line per filesystem with no wrapping,
    # and the TYPE column is what lets the parser drop the ~50 `overlay` rows a
    # CC emits (one per running container, all describing the same disk).
    return "df -PT"


def _cmd_disk_largest(args: dict) -> str:
    mount = shlex.quote(args["mount"])
    # -xdev keeps the walk on the filesystem the operator asked about, which is
    # the whole point: without it, asking about / would descend into
    # /var/lib/docker and report files that are not filling / at all.
    return (f"find {mount} -xdev -type f -printf '%s\\t%p\\n' 2>/dev/null "
            f"| sort -rn | head -n {args['n']}")


def _cmd_maria_check(_args: dict) -> str:
    # _MARIA_CREDS now always leaves $creds non-empty (its own root fallback),
    # so a genuine credential problem shows up as mariadb-check's own
    # access-denied output below, not as a refusal here.
    return (f"c=$({_FIND_MARIA}); [ -n \"$c\" ] || {{ echo 'no mariadb container' >&2; exit 3; }}; "
            f"{_MARIA_CREDS}; "
            f"docker exec \"$c\" sh -c \"mariadb-check $creds --check --all-databases\" 2>&1")


# What modules/maria/credentials.py needs is different from _cmd_maria_check's
# own: that op runs INSIDE the container via `docker exec`, where MariaDB's
# unix_socket plugin authenticates root by OS user with no password at all —
# fine for a one-off check, useless for modules/maria/client.py's real TCP
# connection, which unix_socket auth cannot answer regardless of password.
#
# _MARIA_CREDS's `-uroot` fallback (no password) is therefore not good enough
# here. When the wrapper has nothing, this instead reads the MariaDB
# container's own MARIADB_ROOT_PASSWORD environment variable — confirmed on
# the same HA-config lab CC (10.205.189.21) to be root's real, network-capable
# password (`mariadb -h127.0.0.1 -uroot -p<that value>` succeeds over TCP,
# where a bare `-uroot` does not). Reading exactly that one named variable
# from exactly the container _FIND_MARIA already validated — not a general
# `docker inspect`, which could hand back unrelated secrets from that
# container's other environment variables.
_MARIA_CREDS_NETWORK = (
    _MARIA_CREDS + "; "
    "u=$(printf '%s' \"$creds\" | grep -o -E -- '-u[^ ]+' | cut -c3-); "
    "p=$(printf '%s' \"$creds\" | grep -o -E -- '-p[^ ]+' | cut -c3-); "
    "if [ -z \"$p\" ]; then "
    "c=$(" + _FIND_MARIA + "); "
    "if [ -n \"$c\" ]; then "
    "rp=$(docker inspect \"$c\" "
    "--format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null "
    "| grep -m1 '^MARIADB_ROOT_PASSWORD=' | cut -d= -f2-); "
    "[ -n \"$rp\" ] && u=root && p=\"$rp\"; "
    "fi; fi"
)


def _cmd_maria_creds(_args: dict) -> str:
    return (f"{_MARIA_CREDS_NETWORK}; "
            f"[ -n \"$u\" ] && [ -n \"$p\" ] && printf '%s\\t%s' \"$u\" \"$p\"")


# ── Connectivity probing ─────────────────────────────────────────────────────
# The hostnames this operation may be pointed at. A SECOND copy of the list in
# modules/diag/targets.py, and deliberately not an import: core/ does not
# depend on modules/, and more importantly this is the allowlist that stops the
# op being a general-purpose network probe. deploy/host_agent.py carries a THIRD
# copy for the same reason it carries its own copy of everything else — the
# host's rules live on the host.
#
# Without this the op would take a hostname from the caller, and "run a
# connection attempt to any address you name, from inside the customer's data
# centre, and tell me what answered" is a port scanner with a friendly UI.
_PROBE_HOSTS = frozenset({
    "services.radware.com",
    "radwareti.s3.amazonaws.com",
})


def _probe_host(value) -> str:
    host = str(value or "").strip().lower()
    if host not in _PROBE_HOSTS:
        raise HostExecError(f"not a probeable host: {value!r}")
    return host


def _cmd_net_probe(args: dict) -> str:
    """One shell run that reports DNS, TCP and HTTP for a known host.

    Written for what a CC actually has — getent, bash, curl, timeout — rather
    than for what would be tidy. The output is three fixed lines so the parser
    cannot be surprised by locale or by a curl version that words things
    differently. curl's EXIT CODE carries the TLS verdict that the HTTP status
    cannot: 35 and 60 are handshake and certificate failures respectively, and
    both mean something quite different from "no answer".
    """
    host = args["host"]
    port = int(args["port"])
    return (
        f"H={host}; P={port}; "
        "A=$(getent ahostsv4 \"$H\" 2>/dev/null | awk '{print $1}' | sort -u | paste -sd, -); "
        "echo \"DNS ${A:--}\"; "
        "if timeout 5 bash -c \"cat < /dev/null > /dev/tcp/$H/$P\" 2>/dev/null; "
        "then echo 'TCP ok'; else echo 'TCP fail'; fi; "
        "C=$(timeout 12 curl -s -o /dev/null -w '%{http_code}' \"https://$H/\" 2>/dev/null); E=$?; "
        "echo \"HTTP ${C:-000} $E\""
    )


def _cmd_file_delete(args: dict) -> str:
    # Re-checks symlink/directory on the host at the moment of deletion, the
    # same defence deploy/host_agent.py's Python delete_file() applies for the
    # embedded path — safety.py's path-pattern allowlist runs before this op is
    # reached, but it is lexical (it cannot see what a path actually resolves
    # to), so this is the check that catches "looks like a log, is actually a
    # symlink into /etc". `rm -f` alone would silently follow neither of those
    # protections.
    #
    # The success JSON mirrors host_agent.py's delete_file() ({"bytes",
    # "was_open"}) since modules/system/routers/dashboard.py's delete_file()
    # reads both fields off whichever backend answered. The path itself is
    # deliberately left out of that JSON — the caller already has it and
    # building it into a printf format string would mean either escaping
    # whatever _path() lets through or trusting user-controlled text inside
    # the one string here that isn't pure program output.
    path = shlex.quote(args["path"])
    return (
        f"if [ -L {path} ]; then echo 'refusing a symlink' >&2; exit 3; fi; "
        f"if [ -d {path} ]; then echo 'refusing a directory' >&2; exit 4; fi; "
        f"if [ ! -f {path} ]; then echo 'no longer exists' >&2; exit 2; fi; "
        f"size=$(stat -c%s -- {path} 2>/dev/null || echo 0); "
        f"open=false; fuser -s -- {path} 2>/dev/null && open=true; "
        f"rm -f -- {path} || {{ echo 'could not delete' >&2; exit 1; }}; "
        f'printf \'{{"bytes":%s,"was_open":%s}}\' "$size" "$open"'
    )


OPS: dict[str, dict] = {
    "compose.ps": {
        "args": {},
        "command": _cmd_compose_ps,
        "timeout": 60,
        "what": "list the CC's containers and their health",
    },
    "compose.expected": {
        "args": {},
        "command": _cmd_compose_expected,
        "timeout": 60,
        "what": "list the services this CC's profiles say should run",
    },
    "container.logs": {
        "args": {
            "name": (_name, None),
            "lines": (_bounded_int(1, 5000), 500),
        },
        "command": _cmd_container_logs,
        "timeout": 120,
        "what": "read the tail of one container's log",
    },
    "net.probe": {
        "args": {
            "host": (_probe_host, None),
            "port": (_bounded_int(1, 65535), 443),
        },
        "command": _cmd_net_probe,
        "timeout": 45,
        "what": "check whether this CC can reach one known Radware service",
    },
    "disk.usage": {
        "args": {},
        "command": _cmd_disk_usage,
        "timeout": 30,
        "what": "list the host's filesystems and how full they are",
    },
    "disk.largest": {
        "args": {
            "mount": (_mount, None),
            "n": (_bounded_int(1, 100), 20),
        },
        "command": _cmd_disk_largest,
        # Minutes, not seconds: this walks every inode on the filesystem.
        "timeout": 900,
        "what": "find the largest files on one filesystem",
    },
    "maria.check": {
        "args": {},
        "command": _cmd_maria_check,
        "timeout": 300,
        "what": "check every MariaDB table for corruption",
    },
    "maria.creds": {
        "args": {},
        "command": _cmd_maria_creds,
        "timeout": 20,
        "what": "discover which account reaches this CC's MariaDB over the network",
    },
    # The one operation in this table that CHANGES the appliance.
    #
    # Embedded needs TWO keys turned independently: the app's
    # `system.storage.delete` capability (or the route that calls this does
    # not exist), and the host agent started with --allow-delete. Unlocking
    # the capability alone does nothing there — the agent still refuses, and
    # deletes with os.remove, never a shell.
    #
    # Standalone ships this capability on by default (modules/system/__init__.py)
    # because the operator already has an interactive SSH session's worth of
    # access to whatever CC they connected to — this button saves them a
    # terminal, it does not grant anything new. _cmd_file_delete builds the
    # remove for that path; safety.py's allowlist (backups, config, datastore
    # volumes refused outright) is checked before this op is ever reached and
    # applies identically regardless of which backend runs it.
    "file.delete": {
        "args": {"path": (lambda v: _path(v), None)},
        "command": _cmd_file_delete,
        "timeout": 60,
        "what": "delete one log, heap dump or zip",
    },
    # Reading a file back, a chunk at a time. A READ — it needs no --allow-delete
    # on the agent — but held to the same allowlist as deletion: the files an
    # engineer may take a copy of are exactly the files they may remove.
    "file.read": {
        "args": {"path": (lambda v: _path(v), None),
                 "offset": (_bounded_int(0, 1 << 40), 0),
                 "length": (_bounded_int(1, 16 * 1024 * 1024), 8 * 1024 * 1024)},
        "command": None,
        "timeout": 120,
        "what": "read one chunk of a log, heap dump or zip",
        "agent_only": True,
    },
}


# An absolute path with nothing clever in it. This is NOT the safety rule —
# modules/system/safety.py decides what may be deleted, and the host agent
# decides again for itself. This only ensures they are judging a real path.
_PATH_RE = re.compile(r"^/[^\x00-\x1f\x7f]{1,4095}$")


def _path(value) -> str:
    text = str(value or "")
    if not _PATH_RE.match(text) or ".." in text:
        raise HostExecError(f"not a plain absolute path: {text!r}")
    return text


def validate(op: str, args: dict | None = None) -> dict:
    """Resolve and check the arguments for `op`. Raises HostExecError.

    Separate from run_op so a caller can reject bad input before deciding
    whether a backend even exists — and so the tests can exercise the refusals
    without a host.
    """
    spec = OPS.get(op)
    if spec is None:
        raise HostExecError(f"unknown operation {op!r}")

    supplied = dict(args or {})
    unknown = set(supplied) - set(spec["args"])
    if unknown:
        raise HostExecError(f"{op}: unexpected argument(s) "
                            f"{', '.join(sorted(unknown))}")

    resolved: dict = {}
    for key, (check, default) in spec["args"].items():
        value = supplied.get(key, default)
        if value is None:
            raise HostExecError(f"{op}: missing required argument {key!r}")
        resolved[key] = check(value)
    return resolved


# ── Backend: the host agent (embedded) ───────────────────────────────────────
# One directory, four kinds of file — the same contract deploy/update_agent.sh
# uses, so an operator who has seen one has seen both:
#
#   agent.json           written by the agent — "alive here", + heartbeat
#   requests/<id>.json   written by us        — {"op": ..., "args": {...}}
#   results/<id>.json    written by the agent — {"rc", "stdout", "stderr"}
#
# A heartbeat older than this means the agent is gone (crashed, stopped, host
# rebooted). Its own loop writes every 5s, so a minute is generous enough not
# to flap under load and short enough that the dashboard notices.
_HEARTBEAT_STALE_S = 60
_POLL_INTERVAL_S = 0.2


def _spool() -> str:
    return settings.hostexec_dir


def _agent_status() -> dict:
    """What agent.json says, if anything. Never raises — "no agent" is an
    ordinary state, not an error."""
    path = os.path.join(_spool(), "agent.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, NotADirectoryError):
        return {"present": False, "reason": "no agent.json in the spool directory"}
    except (OSError, ValueError) as exc:
        return {"present": False, "reason": f"agent.json unreadable: {exc}"}

    age = time.time() - float(data.get("heartbeat") or 0)
    if age > _HEARTBEAT_STALE_S:
        return {"present": False, "stale": True,
                "reason": f"the agent last checked in {int(age)}s ago",
                "version": data.get("version", "")}
    return {"present": True, "version": data.get("version", ""),
            "ops": data.get("ops", []), "age_s": round(age, 1),
            # The agent's second key. "the capability is off" and "the host
            # will not do it" are different problems with different fixes, and
            # the UI has to be able to say which one is greying out a button.
            "allow_delete": bool(data.get("allow_delete"))}


def _run_via_agent(op: str, args: dict, timeout: int) -> dict:
    spool = _spool()
    req_dir = os.path.join(spool, "requests")
    res_dir = os.path.join(spool, "results")
    os.makedirs(req_dir, exist_ok=True)
    os.makedirs(res_dir, exist_ok=True)

    job_id = uuid.uuid4().hex
    body = json.dumps({"id": job_id, "op": op, "args": args,
                       "timeout": timeout, "asked_at": time.time()})

    # Write beside and rename: the agent polls this directory, and a partially
    # written file it happened to catch mid-write would be refused as malformed
    # and the request silently lost.
    tmp = os.path.join(req_dir, f".{job_id}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.replace(tmp, os.path.join(req_dir, f"{job_id}.json"))

    result_path = os.path.join(res_dir, f"{job_id}.json")
    # Give the agent the operation's own budget plus a little, so a timeout
    # here means "the agent is not answering", not "the command is slow".
    deadline = time.time() + timeout + 15
    while time.time() < deadline:
        try:
            with open(result_path, "r", encoding="utf-8") as fh:
                result = json.load(fh)
            break
        except (FileNotFoundError, ValueError):
            time.sleep(_POLL_INTERVAL_S)
    else:
        raise HostExecError(
            f"the host agent did not answer within {timeout + 15}s — it may "
            f"have stopped; check `systemctl status cc-admin-host-agent`")

    try:
        os.remove(result_path)
        os.remove(os.path.join(req_dir, f"{job_id}.json"))
    except OSError:
        pass       # the agent expires leftovers itself; not worth failing over

    if result.get("refused"):
        # The agent decided it would not do this. Surfacing its own words
        # matters: this is the boundary doing its job, and hiding it behind a
        # generic error is how a real refusal gets mistaken for a bug.
        raise HostExecError(f"the host agent refused {op}: "
                            f"{result.get('refused')}")
    return {"rc": int(result.get("rc", -1)),
            "stdout": result.get("stdout", ""),
            "stderr": result.get("stderr", ""),
            "via": "agent"}


# ── Backend: SSH (standalone) ────────────────────────────────────────────────

def _ssh_target() -> dict | None:
    """Which CC to reach, and how to log in. None when there is nothing to try.

    Follows the ES connection rather than adding a second thing to configure,
    for the reason modules/maria/client.py:resolve_host() already spells out:
    they are the same appliance, and two settings that must agree eventually
    do not.
    """
    from modules.es.client import get_client

    client = get_client()
    host = (getattr(client, "cc_host", "") or "").strip()
    if not host or host in ("localhost", "127.0.0.1"):
        # An explicit override, for the case the ES connection cannot cover:
        # a CC whose Elasticsearch is unreachable is exactly the box someone
        # wants a health check on, and refusing to look at the host because we
        # could not reach the database would be the wrong way round.
        host = (os.environ.get("CC_SSH_HOST", "") or "").strip()
    if not host or host in ("localhost", "127.0.0.1"):
        return None

    ssh = getattr(client, "ssh", None) or {}
    user = (ssh.get("user") or "").strip()
    password = ssh.get("password") or ""
    port = int(ssh.get("port") or 22)

    if not user:
        from core.remote import cred_store
        saved = cred_store.get(host)
        if saved:
            user = saved.get("user", "")
            password = saved.get("password", "")

    # A key is the remaining case, and the common one for engineers with lab
    # CCs: no password anywhere, but an agent or ~/.ssh key that works.
    key_file = (os.environ.get("CC_SSH_KEY", "") or "").strip()
    if not user:
        user = (os.environ.get("CC_SSH_USER", "") or "").strip()
    if not user:
        return None
    return {"host": host, "user": user, "password": password,
            "port": port, "key_file": key_file}


def _run_via_ssh(op: str, args: dict, timeout: int, target: dict) -> dict:
    from core.remote.ssh_ops import SSHSession, SSHError

    command = OPS[op]["command"](args)
    try:
        with SSHSession(target["host"], target["user"], target["password"],
                        port=target["port"], key_filename=target["key_file"],
                        use_local_keys=not target["password"]) as ssh:
            # exit_status_ok=False: several of these operations answer usefully
            # with a non-zero rc (`docker logs` on a container that has none,
            # `find` on a directory it cannot enter), and the caller inspects
            # rc itself. Raising here would turn a partial answer into no
            # answer at all.
            out, err, rc = ssh.run_full(command, timeout=timeout)
    except SSHError as exc:
        raise HostExecError(str(exc)) from exc
    except Exception as exc:
        raise HostExecError(f"cannot reach {target['host']} over SSH — {exc}") from exc
    return {"rc": rc, "stdout": out, "stderr": err, "via": "ssh"}


# ── Backend: local (a developer running outside a container) ─────────────────
# Deliberately NOT a supported deployment. It exists so a maintainer on a Linux
# box can exercise the parsers against a real `df`, and it is refused outright
# in the embedded profile so it can never become an accidental path to the
# host's shell from inside a shipped appliance.
def _run_locally(op: str, args: dict, timeout: int) -> dict:
    command = OPS[op]["command"](args)
    proc = subprocess.run(["/bin/sh", "-c", command], capture_output=True,
                          text=True, timeout=timeout)
    return {"rc": proc.returncode, "stdout": proc.stdout,
            "stderr": proc.stderr, "via": "local"}


# ── Choosing one ─────────────────────────────────────────────────────────────

def backend() -> dict:
    """Which backend is available, and why — this is what the dashboard shows
    when a pane cannot be filled, so the `detail` is written to be read by an
    operator rather than logged."""
    agent = _agent_status()
    if agent.get("present"):
        return {"kind": "agent", "ok": True,
                "detail": f"host agent{' ' + agent['version'] if agent.get('version') else ''} "
                          f"(last seen {agent.get('age_s', 0)}s ago)",
                "allow_delete": agent.get("allow_delete", False)}

    if policy.profile() == policy.EMBEDDED:
        # Embedded there is no second option: no SSH credentials exist, and the
        # local shell is the container's, which knows nothing about the host.
        return {"kind": "none", "ok": False,
                "detail": agent.get("reason", "no host agent"),
                "hint": "Install the host agent on this CC: "
                        "`deploy/host_agent.py --install`"}

    target = _ssh_target()
    if target:
        return {"kind": "ssh", "ok": True,
                "detail": f"SSH to {target['user']}@{target['host']}"}

    if os.environ.get("HOSTEXEC_LOCAL") == "1":
        return {"kind": "local", "ok": True,
                "detail": "running commands on this machine (HOSTEXEC_LOCAL=1)"}

    return {"kind": "none", "ok": False,
            "detail": "no CC is connected, or its SSH credentials are not known",
            "hint": "Connect to a CC with SSH enabled on the Connection screen, "
                    "or set CC_SSH_HOST / CC_SSH_USER / CC_SSH_KEY."}


def run_op(op: str, **args) -> dict:
    """Run one named operation on the CC host. Raises HostExecError.

    Returns {"rc", "stdout", "stderr", "via", "took_ms"}. A non-zero rc is
    returned, not raised — several of these operations are informative when
    they partly fail, and the caller is the one that knows which.
    """
    resolved = validate(op, args)
    timeout = int(OPS[op]["timeout"])
    chosen = backend()
    started = time.time()

    if OPS[op].get("agent_only") and chosen["kind"] != "agent":
        raise HostExecError(
            f"{op} is only available through the host agent, and this instance "
            f"is using {chosen['kind']}. Changing a CC over SSH from a remote "
            f"install is not something this tool does.")

    if chosen["kind"] == "agent":
        result = _run_via_agent(op, resolved, timeout)
    elif chosen["kind"] == "ssh":
        result = _run_via_ssh(op, resolved, timeout, _ssh_target())
    elif chosen["kind"] == "local":
        if policy.profile() == policy.EMBEDDED:
            raise HostExecError("the local backend is refused in the embedded "
                                "profile")
        result = _run_locally(op, resolved, timeout)
    else:
        raise HostExecError(chosen["detail"])

    result["took_ms"] = int((time.time() - started) * 1000)
    logger.info("[hostexec] %s via %s rc=%s in %sms", op, result["via"],
                result["rc"], result["took_ms"])
    return result
