"""Which files on a CyberController may be deleted from this tool.

The storage drilldown lists a filesystem's twenty largest files, sorted by
size. That sort is the problem: the biggest file on a CC is very often one that
must never be removed. On the appliance this was built against, the top of the
list looks like this —

    2.2 GB  …/jboss-4.2.3.GA/server/insite/log/boot.log     safe, and stale
    952 MB  …/config_osdata/_data/nodes/0/…/_23d.fdt        an OpenSearch shard
    504 MB  …/config_dbdata/_data/data/aria_log.00000001    MariaDB's transaction log

— and the two below the first will corrupt a datastore. So a tool that offers
"delete" next to every row is a tool that eventually deletes one of them, at
3am, on a customer's production box, on the word of an engineer who was told
the disk was full.

Hence this module. Everything here is a pure function over a path string, which
is what makes it testable: tests/test_system_safety.py runs several hundred real
CC paths through it, and the ones that matter are the refusals.

    THREE GATES, AND DENY ALWAYS WINS.

      1. the DIRECTORY it lives in — backups, configuration and datastore
         volumes are untouchable no matter what the file is called;
      2. the NAME — jars, libraries, keys, configuration, SQL, engine files and
         Lucene segments are refused no matter where they live;
      3. an ALLOWLIST of what is left: logs, heap dumps and zips only.

A file must clear all three. Anything unrecognised is refused, so the failure
mode of a shape nobody anticipated is "the button is greyed out" rather than
"the database is gone".

This module is the app's copy. deploy/host_agent.py carries its own, written
out separately and running on the host outside the container's reach — the app's
copy decides what to OFFER, the host's decides what to DO. They are meant to
agree; if they ever do not, the host wins and the operator gets a refusal.
"""

from __future__ import annotations

import re

# ── Gate 1: directories nothing may be deleted from ──────────────────────────
# Prefix matches. Ordered roughly by how much damage the mistake would do.
DENY_DIRS: tuple[tuple[str, str], ...] = (
    # THE most important entry in this file. /opt/radware/storage/backup holds
    # mysql_dumps/<schema>/DD.MM.YYYY_*.sql.gz — the nightly dumps that
    # repair_mysql_db.sh restores from when the MariaDB container will not
    # start. Deleting one is not a lost file, it is the loss of the recovery
    # path for the failure this tool exists to help with. Note they are .gz,
    # which is exactly the shape a naive "compressed things are rotated logs"
    # rule would have swept up.
    ("/opt/radware/storage/backup/", "it is a backup — the DB recovery procedure restores from these"),
    ("/opt/radware/storage/dc_config/", "it is service configuration"),
    ("/opt/radware/mgt-server/properties/", "it is a system property file"),
    ("/opt/radware/box/", "it is part of the appliance's own tooling"),
    ("/opt/radware/mgt-server/bin/", "it is part of the appliance's own tooling"),
    # System directories. Not because anyone would mean to, but because a
    # symlink or an odd `find` result should not be enough.
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

# Docker named volumes that ARE a datastore. Matched on the volume name inside
# the path rather than a fixed prefix, because the docker root differs per host
# (/var/lib/docker/docker-root/volumes/… here, plain /var/lib/docker/volumes/…
# elsewhere) and a prefix that missed would fail open.
_VOLUME_RE = re.compile(r"/volumes/([^/]+)/_data(/|$)")
_DATASTORE_VOLUME = re.compile(
    r"(dbdata|osdata|esdata|pgdata|mysql|maria|postgres|redis|rabbit|prometheus|grafana)",
    re.I)


# ── Gate 2: names that are refused wherever they live ────────────────────────
# The engine files first: these are the ones that end an appliance's day.
DENY_NAMES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"^aria_log\.", re.I),
     "it is MariaDB's Aria transaction log — deleting it corrupts the database"),
    (re.compile(r"^(ib_logfile|ibdata|ibtmp|undo_)", re.I),
     "it is an InnoDB engine file — deleting it corrupts the database"),
    (re.compile(r"^(mysql-bin|mariadb-bin|relay-bin)\.", re.I),
     "it is a MariaDB binary log"),
    (re.compile(r"\.(ibd|frm|myd|myi|par)$", re.I),
     "it is a MariaDB table file"),
    # Lucene/OpenSearch segment files. A shard is thousands of these and the
    # big ones sort straight to the top of a largest-files list.
    (re.compile(r"\.(fdt|fdx|fnm|dvd|dvm|tim|tip|tmd|doc|pos|pay|nvd|nvm|"
                r"cfs|cfe|si|kdd|kdi|kdm|vec|vem|vex|liv)$", re.I),
     "it is an OpenSearch/Lucene index file — deleting it corrupts a shard"),
    (re.compile(r"^(segments_|write\.lock$|_state)", re.I),
     "it is OpenSearch index state"),
    (re.compile(r"^(translog|node_lock)", re.I),
     "it is an OpenSearch translog"),
    # Code and libraries.
    (re.compile(r"\.(war|jar|ear|sar|rar|so|a|o|dll|exe|class|pyc)$", re.I),
     "it is program code or a library"),
    (re.compile(r"\.so\.[0-9]", re.I), "it is a shared library"),
    (re.compile(r"\.(py|sh|bash|pl|rb|php|jsp|js|ts)$", re.I), "it is a script"),
    # Configuration and data.
    (re.compile(r"\.(conf|cnf|cfg|ini|properties|ya?ml|xml|json|toml|env)$", re.I),
     "it is a configuration file"),
    (re.compile(r"\.(sql|dump|bak|db|sqlite3?|mdb)$", re.I),
     "it is a database file or dump"),
    (re.compile(r"\.(pem|key|crt|cer|p12|pfx|jks|keystore|truststore)$", re.I),
     "it is a certificate or key"),
    (re.compile(r"^(id_rsa|id_ecdsa|id_ed25519|authorized_keys|known_hosts)", re.I),
     "it is an SSH credential"),
    # Container images and layers.
    (re.compile(r"\.(tar|tgz|tar\.gz|img|qcow2|vmdk|iso)$", re.I),
     "it is an image or archive, not a log"),
)


# ── Gate 3: what is actually allowed ─────────────────────────────────────────
# Deliberately short. Logs, heap dumps, zips. Anything else is somebody's data
# until proven otherwise.
#
# `.txt` is here by an explicit decision, and it is the widest entry in the
# list. Tomcat writes `localhost_access_log.2026-08-11.txt` by default, so real
# CC logs genuinely carry it — but so does anything a person saved by hand. It
# is allowed because a support engineer clearing a full disk needs the access
# logs more often than they need protection from their own notes, and because
# the denylist above still holds: a `.txt` inside a backup directory, a
# datastore volume or a system directory is refused regardless.
ALLOW_EXT = (".log", ".out", ".err", ".hprof", ".zip", ".dmp", ".txt")

# Classic /var/log files that carry no extension at all. Without these,
# `kern.log.1` was deletable and `syslog.1` sitting right beside it in the same
# list was not — an inconsistency with no explanation an engineer could act on.
# Matched on the whole name after rotation and compression are stripped, so
# `syslog.1` and `messages-20260811.gz` are covered and `syslogd.conf` is not.
ALLOW_NAMES = frozenset((
    "syslog", "messages", "dmesg", "debug", "secure", "maillog", "cron",
    "boot", "faillog", "xferlog", "auth", "daemon", "kern", "user",
))

_COMPRESSION = re.compile(r"\.(gz|bz2|xz|zst|z)$", re.I)
# Rotation, in the shapes the CC's own services actually produce:
#   boot.log.1            numeric
#   app.log-20260811      dated
#   app.log.2026-08-11    dated with dots
_ROTATION = re.compile(r"([.\-]\d{4}-\d{2}-\d{2}|[.\-]\d{8}|\.\d{1,4})$")

_BAD_PATH = re.compile(r"[^\x20-\x7e]")     # control characters, anything odd


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _core_name(name: str) -> str:
    """Strip compression and rotation suffixes to get at the real extension.

    `boot.log.1.gz` and `catalina.out-20260811` are both logs; nothing else in
    this module would recognise them without this. Applied repeatedly because
    `app.log.2026-08-11.gz` carries both.
    """
    previous = None
    while name != previous:
        previous = name
        name = _COMPRESSION.sub("", name)
        name = _ROTATION.sub("", name)
    return name


def classify(path: str) -> dict:
    """{"deletable": bool, "reason": str} for one absolute path.

    `reason` is written for the operator and shown in the UI on the row itself:
    "this is why the button next to this 500 MB file is greyed out" is a
    question that deserves an answer at the point it is asked.
    """
    text = str(path or "")

    if not text.startswith("/") or ".." in text or _BAD_PATH.search(text):
        return {"deletable": False, "reason": "not a plain absolute path"}
    if text.endswith("/"):
        return {"deletable": False, "reason": "it is a directory"}

    # Gate 1 — where it lives.
    for prefix, why in DENY_DIRS:
        if text.startswith(prefix):
            return {"deletable": False, "reason": why}

    name = _basename(text)
    if not name:
        return {"deletable": False, "reason": "not a file"}
    core = _core_name(name)

    # Gate 2 — what it is called. Checked against BOTH the name and the name
    # with compression stripped, so `service.jar.gz` is refused as code rather
    # than falling through to the generic "not a log" at the end. Same verdict
    # either way; the difference is whether the operator is told something
    # useful. Ahead of the datastore-volume rule below for the same reason:
    # "it is MariaDB's Aria transaction log" beats "it is inside a volume".
    for pattern, why in DENY_NAMES:
        if pattern.search(name) or pattern.search(core):
            return {"deletable": False, "reason": why}

    volume = _VOLUME_RE.search(text)
    if volume and _DATASTORE_VOLUME.search(volume.group(1)):
        return {"deletable": False,
                "reason": f"it is inside the {volume.group(1)} datastore volume"}

    # Gate 3 — the allowlist.
    lowered = core.lower()
    # `_log` as well as `.log`: apache and several CC services write
    # `access_log` / `error_log` with no dot, and they are the ordinary case on
    # a full disk.
    if (lowered.endswith(ALLOW_EXT) or lowered.endswith("_log")
            or lowered in ALLOW_NAMES):
        return {"deletable": True, "reason": ""}

    # A log that names itself in the middle: `access_log.2026-08-11`,
    # `catalina.out.5`. _core_name already handles those; what is left here is
    # genuinely unrecognised.
    return {"deletable": False,
            "reason": "only log files, heap dumps and zips can be removed from "
                      "here — anything else has to be done on the machine"}


def annotate(files: list[dict]) -> list[dict]:
    """Tag each {bytes, path} row from the largest-files scan."""
    out = []
    for row in files or []:
        verdict = classify(row.get("path", ""))
        out.append({**row, "deletable": verdict["deletable"],
                    "reason": verdict["reason"]})
    return out
