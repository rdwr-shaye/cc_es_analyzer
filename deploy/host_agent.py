#!/usr/bin/env python3
"""CC Admin — host-side diagnostic agent.

The cc-admin container has three read-only bind mounts and no docker socket, so
it cannot answer the three questions the System dashboard exists to answer:
which containers are unhealthy, how full the disks are, and whether any MariaDB
table is corrupt. This agent runs as root ON THE HOST and answers them, over one
shared directory bind-mounted into the container:

    agent.json           written by us  — "an agent is alive here", + heartbeat
    requests/<id>.json   written by app — {"id":..., "op":..., "args": {...}}
    results/<id>.json    written by us  — {"rc":..., "stdout":..., "stderr":...}

The security property this file exists to provide, stated plainly:

    THE APP CANNOT SEND A COMMAND. It can only name one of the operations in
    OPS below, with arguments this file validates for itself.

That is why the allowlist here is written out in full rather than imported from
core/hostexec.py. The two are deliberately independent: the app's copy says what
it will ask for, this copy says what the host will do, and only this one is
outside the container's reach. If they ever disagree, this one wins and the app
gets a refusal — which is the correct direction for them to fail in.

Everything here is READ-ONLY. Nothing deletes, repairs, restarts or writes. The
remediation actions the dashboard shows disabled are not implemented on this
side either, so unlocking one is a deliberate change to this file on the host,
not a capability the app can talk its way into.

Python rather than shell, unlike deploy/update_agent.sh, for one reason: this
parses JSON written by another process, and hand-rolled JSON parsing in bash is
exactly where an injection would hide.

Usage:
    deploy/host_agent.py --watch                 # serve requests (default)
    deploy/host_agent.py --once                  # drain the queue and exit
    deploy/host_agent.py --install               # install + start a systemd unit
    deploy/host_agent.py --uninstall             # stop and remove that unit
    deploy/host_agent.py --self-test             # prove the refusals work
    deploy/host_agent.py --spool /path           # override the shared directory
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time

VERSION = "1.0"
SERVICE_NAME = "cc-admin-host-agent"

# Host side of the bind mount declared in deploy/embedded-compose.snippet.yaml.
# The container sees the same directory as /app/.hostexec.
DEFAULT_SPOOL = "/opt/radware/storage/data/cc-admin/hostexec"
DEFAULT_COMPOSE = "/deploy/config/docker-compose.yaml"

HEARTBEAT_S = 5
POLL_S = 0.2
# A request nobody collected, or a result nobody read, is litter after this.
# Longer than the slowest operation's own budget so a live job is never swept.
EXPIRE_S = 3600


def log(*parts) -> None:
    print(f"[host-agent] {time.strftime('%Y-%m-%d %H:%M:%S')} "
          + " ".join(str(p) for p in parts), flush=True)


# ── Argument validation ──────────────────────────────────────────────────────
# Strict allowlists, not escaping. There is no legitimate container name with a
# space in it, so refusing one is both simpler and safer than quoting it well.

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")
_MOUNT_RE = re.compile(r"^/[A-Za-z0-9_./-]{0,255}$")


class Refused(Exception):
    """This request will not be run. The message goes back to the app verbatim
    — a refusal the operator cannot read is indistinguishable from a bug."""


def v_name(value):
    text = value if isinstance(value, str) else ""
    if not _NAME_RE.match(text):
        raise Refused(f"not a valid container name: {value!r}")
    return text


def v_mount(value):
    text = value if isinstance(value, str) else ""
    if not _MOUNT_RE.match(text) or ".." in text:
        raise Refused(f"not a valid mount point: {value!r}")
    return text


def v_int(lo, hi):
    def check(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise Refused(f"expected a number, got {value!r}")
        number = int(value)
        if not lo <= number <= hi:
            raise Refused(f"{number} is outside {lo}..{hi}")
        return number
    return check


# ── The operations ───────────────────────────────────────────────────────────
# The compose project prefix differs per host (`config_kvision-infra-mariadb_1`
# on one CC, something else on another), so the MariaDB container is discovered
# rather than named — the same reasoning deploy/nginx_detect.py documents.
FIND_MARIA = "docker ps --format '{{.Names}}' | grep -m1 -i mariadb"

# Credentials come off the host's own mysql wrapper, the same file
# modules/maria/credentials.py reads. They are read HERE, on the host, so no
# password ever appears in a request or a result.
#
# Not every appliance has a -u/-p pair to find — confirmed on an HA-config lab
# CC (10.205.189.21) whose wrapper is a bare `mysql "$@"`, relying on
# MariaDB's unix_socket auth plugin to authenticate root by OS user once
# `docker exec` has already put us inside the container as root (verified:
# `mariadb-check -uroot` succeeds there with no password). So when nothing is
# found, fall back to root with no password rather than refusing outright — a
# real mismatch then surfaces as mariadb-check's own access-denied message.
# This account is only ever used inside a docker exec on THIS host, over the
# unix socket, never over the network.
MARIA_CREDS = (
    "creds=$(grep -m1 -E -- '-u[^ ]+ +-p[^ ]+' /usr/local/bin/mysql "
    "| grep -o -E -- '-u[^ ]+ +-p[^ ]+'); "
    "[ -n \"$creds\" ] || creds='-uroot'"
)

# What modules/maria/credentials.py needs for the app's actual TCP connection
# is different from MARIA_CREDS's own -uroot fallback: root-via-unix_socket
# authenticates by OS user through `docker exec`, but that plugin does not
# answer a real network connection at all, regardless of password. So when the
# wrapper has nothing, this instead reads the MariaDB container's own
# MARIADB_ROOT_PASSWORD environment variable — confirmed on the same
# HA-config lab CC to be root's real, network-capable password. Reading
# exactly that one named variable from exactly the container FIND_MARIA
# already validated, not a general docker inspect.
MARIA_CREDS_NETWORK = (
    MARIA_CREDS + "; "
    "u=$(printf '%s' \"$creds\" | grep -o -E -- '-u[^ ]+' | cut -c3-); "
    "p=$(printf '%s' \"$creds\" | grep -o -E -- '-p[^ ]+' | cut -c3-); "
    "if [ -z \"$p\" ]; then "
    "c=$(" + FIND_MARIA + "); "
    "if [ -n \"$c\" ]; then "
    "rp=$(docker inspect \"$c\" "
    "--format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null "
    "| grep -m1 '^MARIADB_ROOT_PASSWORD=' | cut -d= -f2-); "
    "[ -n \"$rp\" ] && u=root && p=\"$rp\"; "
    "fi; fi"
)


def build(op: str, args: dict, compose_file: str) -> str:
    if op == "compose.ps":
        # --all, and it is not optional. Plain `ps` lists only RUNNING
        # containers, so a stopped service vanishes from the output instead of
        # showing as Exited — which is how a CC with one service down reported
        # "all 35 services running".
        return (f"docker compose --file {shlex.quote(compose_file)} ps --all "
                f"--format 'table {{{{.Service}}}}\\t{{{{.Name}}}}\\t{{{{.Status}}}}'")

    if op == "compose.expected":
        # What SHOULD run: `config --services` applies COMPOSE_PROFILES from
        # the .env beside the compose file, so the answer is this appliance's
        # profiles, not every service the file can describe.
        return (f"docker compose --file {shlex.quote(compose_file)} "
                f"config --services")

    if op == "net.probe":
        # Deliberately the same three lines core/hostexec.py builds, so both
        # sides produce output the one parser understands. The host is already
        # validated against PROBE_HOSTS above; quoting it as well because a
        # validator and a quote protect against different mistakes.
        host = shlex.quote(args["host"])
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

    if op == "container.logs":
        return (f"docker logs --timestamps --tail {args['lines']} "
                f"{shlex.quote(args['name'])} 2>&1")

    if op == "disk.usage":
        # -PT, not -h: one line per filesystem, never wrapped, and the TYPE
        # column is what lets the app drop the ~50 `overlay` rows a CC emits.
        return "df -PT"

    if op == "disk.largest":
        # -xdev keeps the walk on the filesystem actually asked about; without
        # it, asking about / descends into /var/lib/docker and reports files
        # that are not filling / at all.
        return (f"find {shlex.quote(args['mount'])} -xdev -type f "
                f"-printf '%s\\t%p\\n' 2>/dev/null | sort -rn "
                f"| head -n {args['n']}")

    if op == "maria.check":
        # MARIA_CREDS now always leaves $creds non-empty (its own root
        # fallback), so a genuine credential problem shows up as
        # mariadb-check's own access-denied output below, not a refusal here.
        return (f"c=$({FIND_MARIA}); "
                f"[ -n \"$c\" ] || {{ echo 'no mariadb container' >&2; exit 3; }}; "
                f"{MARIA_CREDS}; "
                f"docker exec \"$c\" sh -c \"mariadb-check $creds --check --all-databases\" 2>&1")

    if op == "maria.creds":
        return (f"{MARIA_CREDS_NETWORK}; "
                f"[ -n \"$u\" ] && [ -n \"$p\" ] && printf '%s\\t%s' \"$u\" \"$p\"")

    raise Refused(f"unknown operation {op!r}")


def v_path(value):
    """An absolute path with nothing clever in it. The deletion rules below do
    the real work; this only guarantees they are judging what they think they
    are."""
    text = value if isinstance(value, str) else ""
    if not text.startswith("/") or ".." in text or len(text) > 4096:
        raise Refused(f"not a plain absolute path: {value!r}")
    if any(ch < " " or ch == "\x7f" for ch in text):
        raise Refused("path contains control characters")
    return text


# The hostnames this agent will attempt a connection to. The THIRD copy of this
# list — core/hostexec.py and modules/diag/targets.py hold the others — and the
# duplication is the security property, not an oversight. The container asks;
# the host decides. Nothing the app can say adds a hostname here, so the op
# cannot be turned into a port scanner pointed at the customer's own network by
# anyone who finds a bug on the other side of the spool directory.
PROBE_HOSTS = frozenset({
    "services.radware.com",
    "radwareti.s3.amazonaws.com",
    "radware.flexnetoperations.com",
    "filepile.radware.com",
    "support.radware.com",
})


def v_probe_host(value):
    host = str(value or "").strip().lower()
    if host not in PROBE_HOSTS:
        raise Refused(f"not a probeable host: {value!r}")
    return host


OPS = {
    "compose.ps":       {"args": {}, "timeout": 60},
    "compose.expected": {"args": {}, "timeout": 60},
    "container.logs": {"args": {"name": (v_name, None),
                                "lines": (v_int(1, 5000), 500)},
                       "timeout": 120},
    # Read-only, and confined to the fixed host list above. It answers "can
    # THIS APPLIANCE reach one known Radware service", which is a question the
    # container cannot answer for itself: its own egress is not the CC's.
    "net.probe":      {"args": {"host": (v_probe_host, None),
                                "port": (v_int(1, 65535), 443)},
                       "timeout": 45},
    "disk.usage":     {"args": {}, "timeout": 30},
    "disk.largest":   {"args": {"mount": (v_mount, None),
                                "n": (v_int(1, 100), 20)},
                       "timeout": 900},
    "maria.check":    {"args": {}, "timeout": 300},
    "maria.creds":    {"args": {}, "timeout": 20},
    # The only operation here that CHANGES anything. Off unless the agent was
    # started with --allow-delete; see delete_file().
    "file.delete":    {"args": {"path": (v_path, None)}, "timeout": 60},
    # Reading a file back off the host, one chunk at a time. A READ, so it does
    # NOT need --allow-delete — but it is held to exactly the same allowlist as
    # deletion, which is the point: the agent will hand over a log, a heap dump
    # or a zip and nothing else. It cannot be used to read /etc/shadow, a key,
    # a backup or a datastore file, because classify() refuses all of those.
    "file.read":      {"args": {"path": (v_path, None),
                                "offset": (v_int(0, 1 << 40), 0),
                                "length": (v_int(1, 16 * 1024 * 1024),
                                           8 * 1024 * 1024)},
                       "timeout": 120},
}

# Chunks travel base64-encoded inside a JSON result file, so the whole transfer
# costs about 1.4x the file on the wire and a round trip per chunk. That is
# fine for a log and hopeless for a disk image, so there is a ceiling — and it
# is stated rather than discovered halfway through a download.
DOWNLOAD_MAX = 2 * 1024 * 1024 * 1024


# ── What may be deleted ──────────────────────────────────────────────────────
# The host's own copy of modules/system/safety.py, written out here rather than
# imported. That duplication is the point: the app decides what to OFFER, this
# file decides what to DO, and this file lives on the host outside the
# container's reach. If they ever disagree, this one wins.
#
# THREE GATES, AND DENY ALWAYS WINS: the directory, then the name, then a short
# allowlist. Anything unrecognised is refused, so a shape nobody anticipated
# fails closed.
#
# Why it matters, concretely: the storage screen sorts by size, and on a CC the
# biggest files are a 952 MB OpenSearch shard segment, a 504 MB MariaDB Aria
# log, and a 484 MB application jar. A tool that offered "delete" beside those
# would eventually take one, at 3am, on a customer's production box.

DENY_DIRS = (
    # The single most important entry. mysql_dumps/<schema>/*.sql.gz are what
    # repair_mysql_db.sh restores from when MariaDB will not start. Deleting
    # one removes the recovery path for the very failure this tool spots.
    ("/opt/radware/storage/backup/", "it is a backup — the DB recovery procedure restores from these"),
    ("/opt/radware/storage/dc_config/", "it is service configuration"),
    ("/opt/radware/mgt-server/properties/", "it is a system property file"),
    ("/opt/radware/box/", "it is part of the appliance's own tooling"),
    ("/opt/radware/mgt-server/bin/", "it is part of the appliance's own tooling"),
    ("/boot/", "it is a system directory"),
    ("/etc/", "it is a system directory"),
    ("/usr/", "it is a system directory"),
    ("/bin/", "it is a system directory"),
    ("/sbin/", "it is a system directory"),
    ("/lib/", "it is a system directory"),
    ("/lib64/", "it is a system directory"),
    ("/proc/", "it is a kernel interface"),
    ("/sys/", "it is a kernel interface"),
    ("/dev/", "it is a device node"),
    ("/root/", "it is a home directory"),
)

_VOLUME_RE = re.compile(r"/volumes/([^/]+)/_data(/|$)")
_DATASTORE_VOLUME = re.compile(
    r"(dbdata|osdata|esdata|pgdata|mysql|maria|postgres|redis|rabbit|prometheus|grafana)",
    re.I)

DENY_NAMES = (
    (re.compile(r"^aria_log\.", re.I),
     "it is MariaDB's Aria transaction log — deleting it corrupts the database"),
    (re.compile(r"^(ib_logfile|ibdata|ibtmp|undo_)", re.I),
     "it is an InnoDB engine file — deleting it corrupts the database"),
    (re.compile(r"^(mysql-bin|mariadb-bin|relay-bin)\.", re.I),
     "it is a MariaDB binary log"),
    (re.compile(r"\.(ibd|frm|myd|myi|par)$", re.I), "it is a MariaDB table file"),
    (re.compile(r"\.(fdt|fdx|fnm|dvd|dvm|tim|tip|tmd|doc|pos|pay|nvd|nvm|"
                r"cfs|cfe|si|kdd|kdi|kdm|vec|vem|vex|liv)$", re.I),
     "it is an OpenSearch/Lucene index file — deleting it corrupts a shard"),
    (re.compile(r"^(segments_|write\.lock$|_state)", re.I),
     "it is OpenSearch index state"),
    (re.compile(r"^(translog|node_lock)", re.I), "it is an OpenSearch translog"),
    (re.compile(r"\.(war|jar|ear|sar|rar|so|a|o|dll|exe|class|pyc)$", re.I),
     "it is program code or a library"),
    (re.compile(r"\.so\.[0-9]", re.I), "it is a shared library"),
    (re.compile(r"\.(py|sh|bash|pl|rb|php|jsp|js|ts)$", re.I), "it is a script"),
    (re.compile(r"\.(conf|cnf|cfg|ini|properties|ya?ml|xml|json|toml|env)$", re.I),
     "it is a configuration file"),
    (re.compile(r"\.(sql|dump|bak|db|sqlite3?|mdb)$", re.I),
     "it is a database file or dump"),
    (re.compile(r"\.(pem|key|crt|cer|p12|pfx|jks|keystore|truststore)$", re.I),
     "it is a certificate or key"),
    (re.compile(r"^(id_rsa|id_ecdsa|id_ed25519|authorized_keys|known_hosts)", re.I),
     "it is an SSH credential"),
    (re.compile(r"\.(tar|tgz|tar\.gz|img|qcow2|vmdk|iso)$", re.I),
     "it is an image or archive, not a log"),
)

# `.txt` is deliberate and is the widest entry: Tomcat's default access log
# carries it, and so does anything a person saved by hand. The denylist above
# still holds — a .txt inside a backup, a datastore volume or a system
# directory is refused regardless of its name.
ALLOW_EXT = (".log", ".out", ".err", ".hprof", ".zip", ".dmp", ".txt")
# Classic /var/log files with no extension. Without them `kern.log.1` was
# deletable while `syslog.1` beside it was not, for no reason an operator could
# act on. Matched after rotation and compression are stripped.
ALLOW_NAMES = frozenset((
    "syslog", "messages", "dmesg", "debug", "secure", "maillog", "cron",
    "boot", "faillog", "xferlog", "auth", "daemon", "kern", "user",
))

_COMPRESSION = re.compile(r"\.(gz|bz2|xz|zst|z)$", re.I)
_ROTATION = re.compile(r"([.\-]\d{4}-\d{2}-\d{2}|[.\-]\d{8}|\.\d{1,4})$")


def _core_name(name: str) -> str:
    previous = None
    while name != previous:
        previous = name
        name = _COMPRESSION.sub("", name)
        name = _ROTATION.sub("", name)
    return name


def classify(path: str) -> dict:
    """{"deletable", "reason"} — the host's verdict, which is the binding one."""
    text = str(path or "")
    if not text.startswith("/") or ".." in text:
        return {"deletable": False, "reason": "not a plain absolute path"}
    if text.endswith("/"):
        return {"deletable": False, "reason": "it is a directory"}

    for prefix, why in DENY_DIRS:
        if text.startswith(prefix):
            return {"deletable": False, "reason": why}

    name = text.rsplit("/", 1)[-1]
    if not name:
        return {"deletable": False, "reason": "not a file"}
    core = _core_name(name)

    # Both the raw name and the decompressed one, so `service.jar.gz` is
    # refused as code rather than falling through to the generic message.
    for pattern, why in DENY_NAMES:
        if pattern.search(name) or pattern.search(core):
            return {"deletable": False, "reason": why}

    volume = _VOLUME_RE.search(text)
    if volume and _DATASTORE_VOLUME.search(volume.group(1)):
        return {"deletable": False,
                "reason": f"it is inside the {volume.group(1)} datastore volume"}

    lowered = core.lower()
    if (lowered.endswith(ALLOW_EXT) or lowered.endswith("_log")
            or lowered in ALLOW_NAMES):
        return {"deletable": True, "reason": ""}
    return {"deletable": False,
            "reason": "only log files, heap dumps and zips can be removed from "
                      "here — anything else has to be done on the machine"}


def read_file(path: str, offset: int, length: int) -> dict:
    """Hand back one chunk of a file, base64 encoded.

    Held to the SAME allowlist as deletion, deliberately: the set of files a
    support engineer may take a copy of is the set they may remove, and
    widening one without the other would be an accident waiting to happen.

    Every chunk re-checks the path. The caller is a loop over offsets and the
    file could in principle be swapped between chunks; re-checking costs
    nothing and means no single approval covers a later, different file.
    """
    verdict = classify(path)
    if not verdict["deletable"]:
        raise Refused(f"{path}: {verdict['reason']}")
    if os.path.islink(path):
        raise Refused(f"{path} is a symlink")
    if not os.path.isfile(path):
        return {"rc": 2, "stdout": "", "stderr": f"{path} does not exist"}

    size = os.path.getsize(path)
    if size > DOWNLOAD_MAX:
        raise Refused(
            f"{path} is {size} bytes, over the {DOWNLOAD_MAX}-byte limit for "
            f"pulling a file through the agent — copy it off with scp instead")

    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            chunk = fh.read(length)
    except OSError as exc:
        return {"rc": 1, "stdout": "", "stderr": f"could not read {path}: {exc}"}

    import base64
    return {"rc": 0, "stderr": "", "stdout": json.dumps({
        "size": size,
        "offset": offset,
        "bytes": len(chunk),
        "eof": offset + len(chunk) >= size,
        "data": base64.b64encode(chunk).decode("ascii"),
    })}


def _still_open(path: str) -> bool:
    """Is a running process holding this file open?

    Not a refusal — deleting an open log is legal and the inode goes away when
    the last handle closes. It is reported because the SPACE does not come back
    until then, and an engineer who deletes a 2 GB log, sees `df` unchanged and
    concludes the tool is broken has been failed by the tool, not by Linux.
    """
    try:
        return subprocess.run(["fuser", "-s", path], timeout=10).returncode == 0
    except Exception:                                   # noqa: BLE001
        return False


def delete_file(path: str, allow_delete: bool) -> dict:
    """Remove one file. Implemented in Python, with no shell anywhere near it.

    Two independent keys have to be turned for this to do anything:
      * the APP's capability (system.storage.delete) must be unlocked by the
        property file, or the route that calls it does not exist;
      * this agent must have been started with --allow-delete.
    Both are on the host and both are deliberate acts. Unlocking the capability
    alone changes nothing here — which is the property worth having.
    """
    if not allow_delete:
        raise Refused(
            "this host agent was not started with --allow-delete, so it will "
            "not remove files. Add the flag to the systemd unit and restart it "
            "if that is intended.")

    verdict = classify(path)
    if not verdict["deletable"]:
        raise Refused(f"{path}: {verdict['reason']}")

    if os.path.islink(path):
        raise Refused(f"{path} is a symlink")
    if os.path.isdir(path):
        raise Refused(f"{path} is a directory")
    if not os.path.isfile(path):
        return {"rc": 2, "stdout": "", "stderr": f"{path} no longer exists"}

    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    open_by_process = _still_open(path)

    try:
        os.remove(path)
    except OSError as exc:
        return {"rc": 1, "stdout": "", "stderr": f"could not delete {path}: {exc}"}

    log(f"DELETED {path} ({size} bytes, still open: {open_by_process})")
    return {"rc": 0, "stderr": "", "stdout": json.dumps({
        "deleted": path, "bytes": size, "was_open": open_by_process})}

# Nothing this agent produces is worth megabytes in a JSON file the app then
# parses. A container log longer than this is truncated with a note rather than
# silently — the engineer needs to know they are not looking at the whole thing.
MAX_OUTPUT = 4 * 1024 * 1024


def check_args(op: str, raw) -> dict:
    spec = OPS.get(op)
    if spec is None:
        raise Refused(f"unknown operation {op!r}")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise Refused("args must be an object")

    unknown = set(raw) - set(spec["args"])
    if unknown:
        raise Refused(f"unexpected argument(s): {', '.join(sorted(unknown))}")

    out = {}
    for key, (validate, default) in spec["args"].items():
        value = raw.get(key, default)
        if value is None:
            raise Refused(f"missing required argument {key!r}")
        out[key] = validate(value)
    return out


def execute(op: str, args: dict, compose_file: str,
            allow_delete: bool = False) -> dict:
    # Deleting is done in Python, not by handing a path to a shell. There is no
    # quoting bug available in `os.remove`.
    if op == "file.delete":
        return delete_file(args["path"], allow_delete)
    if op == "file.read":
        return read_file(args["path"], args["offset"], args["length"])

    command = build(op, args, compose_file)
    timeout = OPS[op]["timeout"]
    log(f"run {op} {args or ''} -> {command[:160]}")
    try:
        proc = subprocess.run(["/bin/sh", "-c", command], capture_output=True,
                              text=True, errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"rc": 124, "stdout": "",
                "stderr": f"{op} did not finish within {timeout}s"}

    out, err = proc.stdout, proc.stderr
    if len(out) > MAX_OUTPUT:
        out = (out[:MAX_OUTPUT]
               + f"\n… truncated at {MAX_OUTPUT} bytes by the host agent\n")
    return {"rc": proc.returncode, "stdout": out, "stderr": err[:16384]}


# ── Spool handling ───────────────────────────────────────────────────────────

def write_atomic(path: str, text: str) -> None:
    """Write beside and rename. The app polls for these files, and a partially
    written one it caught mid-write would parse as malformed and be discarded."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def heartbeat(spool: str, allow_delete: bool = False) -> None:
    write_atomic(os.path.join(spool, "agent.json"), json.dumps({
        "version": VERSION,
        "heartbeat": time.time(),
        "pid": os.getpid(),
        # What this host will actually do, so the app can say "the agent here
        # is older than this app and does not know that operation" rather than
        # timing out on a request nobody will ever answer.
        "ops": sorted(op for op in OPS
                      if op != "file.delete" or allow_delete),
        # Advertised separately so the UI can explain a greyed-out Delete
        # precisely: "the capability is off" and "the host agent will not do it"
        # are different problems with different fixes.
        "allow_delete": bool(allow_delete),
    }))


def serve_one(spool: str, name: str, compose_file: str,
              allow_delete: bool = False) -> None:
    req_path = os.path.join(spool, "requests", name)
    try:
        with open(req_path, "r", encoding="utf-8") as fh:
            request = json.load(fh)
    except (OSError, ValueError) as exc:
        log(f"discarding unreadable request {name}: {exc}")
        _unlink(req_path)
        return

    job_id = str(request.get("id") or "")[:64]
    if not re.match(r"^[A-Za-z0-9_-]{1,64}$", job_id):
        log(f"discarding request with a bad id: {name}")
        _unlink(req_path)
        return

    op = request.get("op")
    try:
        args = check_args(op if isinstance(op, str) else "", request.get("args"))
        result = execute(op, args, compose_file, allow_delete)
    except Refused as exc:
        # Loud on purpose. A refusal is either a version mismatch worth fixing
        # or someone probing the boundary, and both deserve to be in the log.
        log(f"REFUSED {op!r}: {exc}")
        result = {"refused": str(exc)}
    except Exception as exc:                       # noqa: BLE001 — never die
        log(f"error running {op!r}: {exc}")
        result = {"rc": -1, "stdout": "", "stderr": f"host agent error: {exc}"}

    write_atomic(os.path.join(spool, "results", f"{job_id}.json"),
                 json.dumps(result))
    _unlink(req_path)


def _unlink(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def sweep(spool: str) -> None:
    """Remove requests and results nobody collected."""
    cutoff = time.time() - EXPIRE_S
    for sub in ("requests", "results"):
        directory = os.path.join(spool, sub)
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            path = os.path.join(directory, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    log(f"expired {sub}/{name}")
            except OSError:
                pass


def prepare(spool: str) -> None:
    for sub in ("requests", "results"):
        os.makedirs(os.path.join(spool, sub), exist_ok=True)
    # The container writes requests and reads results, so both directories have
    # to be writable by whoever it runs as. 0777 is deliberate and bounded: the
    # only thing reachable through them is the operation list above, and the
    # alternative — matching uids across an image rebuild — silently breaks.
    for sub in ("", "requests", "results"):
        try:
            os.chmod(os.path.join(spool, sub), 0o777)
        except OSError:
            pass


def watch(spool: str, compose_file: str, once: bool = False,
          allow_delete: bool = False) -> int:
    prepare(spool)
    log(f"version {VERSION} · spool {spool} · compose {compose_file}")
    log(f"operations: {', '.join(sorted(OPS))}")
    # Stated at startup, every startup. Whether this agent will delete files is
    # the one line of this log anybody will ever go looking for.
    log("file deletion: ARMED (--allow-delete)" if allow_delete
        else "file deletion: refused (start with --allow-delete to enable)")
    last_beat = 0.0
    last_sweep = 0.0
    while True:
        now = time.time()
        if now - last_beat >= HEARTBEAT_S:
            heartbeat(spool, allow_delete)
            last_beat = now
        if now - last_sweep >= 300:
            sweep(spool)
            last_sweep = now

        try:
            pending = sorted(n for n in os.listdir(os.path.join(spool, "requests"))
                             if n.endswith(".json"))
        except OSError as exc:
            log(f"cannot read the spool: {exc}")
            pending = []

        for name in pending:
            serve_one(spool, name, compose_file, allow_delete)

        if once:
            return 0
        time.sleep(POLL_S)


# ── systemd ──────────────────────────────────────────────────────────────────

UNIT = """[Unit]
Description=CC Admin host agent (runs the System dashboard's read-only checks)
After=docker.service
Wants=docker.service

[Service]
Type=simple
ExecStart={exe} {script} --watch --spool {spool} --compose {compose}{delete}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
"""


def install(spool: str, compose_file: str, allow_delete: bool = False) -> int:
    script = os.path.abspath(__file__)
    unit_path = f"/etc/systemd/system/{SERVICE_NAME}.service"
    try:
        with open(unit_path, "w", encoding="utf-8") as fh:
            fh.write(UNIT.format(exe=sys.executable, script=script,
                                 spool=spool, compose=compose_file,
                                 delete=" --allow-delete" if allow_delete else ""))
    except OSError as exc:
        log(f"cannot write {unit_path}: {exc} — run this as root")
        return 1
    prepare(spool)
    for cmd in (["systemctl", "daemon-reload"],
                ["systemctl", "enable", SERVICE_NAME],
                ["systemctl", "restart", SERVICE_NAME]):
        subprocess.run(cmd, check=False)
    log(f"installed and started {SERVICE_NAME}"
        + (" WITH file deletion armed" if allow_delete else ""))
    log(f"  status: systemctl status {SERVICE_NAME}")
    log(f"  logs:   journalctl -u {SERVICE_NAME} -f")
    return 0


def uninstall() -> int:
    for cmd in (["systemctl", "disable", "--now", SERVICE_NAME],):
        subprocess.run(cmd, check=False)
    _unlink(f"/etc/systemd/system/{SERVICE_NAME}.service")
    subprocess.run(["systemctl", "daemon-reload"], check=False)
    log(f"removed {SERVICE_NAME}")
    return 0


# ── Self-test ────────────────────────────────────────────────────────────────
# Runs on the host, executes nothing, and proves the boundary holds. Worth
# having on the box itself: "show me it refuses that" is the first thing asked
# in a review, and the answer should not require the reviewer to trust a
# document written elsewhere.

def self_test() -> int:
    cases = [
        ("an operation that does not exist",      "shell.exec",      {}),
        ("a command smuggled as an operation",    "compose.ps; id",  {}),
        ("shell metacharacters in a name",        "container.logs",  {"name": "a; id"}),
        ("a pipe in a name",                      "container.logs",  {"name": "a|b"}),
        ("command substitution in a name",        "container.logs",  {"name": "$(id)"}),
        ("a path traversal in a mount",           "disk.largest",    {"mount": "/var/../etc"}),
        ("a relative mount",                      "disk.largest",    {"mount": "var/lib"}),
        ("an argument that is not expected",      "compose.ps",      {"cmd": "id"}),
        ("a line count past the cap",             "container.logs",  {"name": "x", "lines": 999999}),
        ("args that are not an object",           "compose.ps",      "id"),
    ]
    failures = 0
    for label, op, args in cases:
        try:
            check_args(op, args)
        except Refused as exc:
            print(f"  refused  {label:38} -- {exc}")
            continue
        except Exception as exc:                   # noqa: BLE001
            print(f"  refused  {label:38} -- {exc}")
            continue
        print(f"  ACCEPTED {label:38} -- THIS IS A BUG")
        failures += 1

    # And the other half: the legitimate calls must still work.
    for label, op, args in [
        ("compose.ps",     "compose.ps",     {}),
        ("container.logs", "container.logs", {"name": "config_kvision-infra-mariadb_1"}),
        ("disk.largest",   "disk.largest",   {"mount": "/var/lib/docker", "n": 20}),
    ]:
        try:
            check_args(op, args)
            print(f"  accepted {label:38} -- as it should")
        except Refused as exc:
            print(f"  REFUSED  {label:38} -- THIS IS A BUG: {exc}")
            failures += 1

    # ── Deletion ─────────────────────────────────────────────────────────────
    # The rules that stand between a full disk and a corrupted appliance. Run
    # on the box itself so "show me it will not touch that" can be answered
    # here rather than from a document written somewhere else.
    print("\n  file deletion — what this host would refuse:")
    must_refuse = [
        ("the MariaDB Aria log",
         "/var/lib/docker/docker-root/volumes/config_dbdata/_data/data/aria_log.00000001"),
        ("an OpenSearch shard segment",
         "/var/lib/docker/docker-root/volumes/config_osdata/_data/nodes/0/indices/a/0/index/_23d.fdt"),
        ("a nightly DB dump the restore needs",
         "/opt/radware/storage/backup/mysql_dumps/vision_ng/11.08.2026_00_00_01_vision_ng.sql.gz"),
        ("an application jar", "/opt/radware/policy-service/app/policy-service.jar"),
        ("a shared library", "/opt/radware/app/lib/libcrypto.so.3"),
        ("service configuration", "/opt/radware/storage/dc_config/whatever.yaml"),
        ("a system file", "/etc/passwd"),
        ("the appliance's own scripts", "/opt/radware/box/bin/repair_mysql_db.sh"),
        ("a path with .. in it", "/opt/radware/logs/../../etc/passwd"),
        ("a directory", "/opt/radware/logs/"),
        ("something unrecognised", "/opt/radware/storage/data/somefile"),
    ]
    for label, path in must_refuse:
        verdict = classify(path)
        if verdict["deletable"]:
            print(f"  ALLOWS   {label:38} -- THIS IS A BUG")
            failures += 1
        else:
            print(f"  refuses  {label:38} -- {verdict['reason'][:52]}")

    print("\n  file deletion — what this host would allow:")
    for label, path in [
        ("a stale log", "/opt/radware/logs/insite/boot.log"),
        ("a rotated log", "/opt/radware/logs/es/vision-es.log.7.gz"),
        ("a heap dump", "/opt/radware/tmp/java_pid1234.hprof"),
        ("a techsupport zip", "/opt/radware/tmp/techsupport-20260811.zip"),
    ]:
        verdict = classify(path)
        if verdict["deletable"]:
            print(f"  allows   {label:38} -- as it should")
        else:
            print(f"  REFUSES  {label:38} -- THIS IS A BUG: {verdict['reason']}")
            failures += 1

    print()
    print("FAIL" if failures else "PASS")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--watch", action="store_true", help="serve requests (default)")
    mode.add_argument("--once", action="store_true", help="drain the queue and exit")
    mode.add_argument("--install", action="store_true", help="install a systemd unit")
    mode.add_argument("--uninstall", action="store_true", help="remove that unit")
    mode.add_argument("--self-test", action="store_true",
                      help="prove the refusals work; runs nothing")
    parser.add_argument("--spool", default=DEFAULT_SPOOL)
    parser.add_argument("--compose", default=DEFAULT_COMPOSE)
    parser.add_argument("--allow-delete", action="store_true",
                        help="permit file.delete for log/heap-dump/zip files. "
                             "OFF by default: unlocking the app's capability "
                             "alone must not be enough to make a host delete "
                             "anything.")
    args = parser.parse_args()

    if args.self_test:
        return self_test()
    if args.install:
        return install(args.spool, args.compose, args.allow_delete)
    if args.uninstall:
        return uninstall()
    return watch(args.spool, args.compose, once=args.once,
                 allow_delete=args.allow_delete)


if __name__ == "__main__":
    sys.exit(main())
