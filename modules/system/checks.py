"""Turning command output into a health verdict.

Everything in this module is a pure function over text. That is deliberate:
these are the rules that decide whether a support engineer is told a customer's
CyberController is fine, and rules like that should be testable without a CC in
the room. tests/test_system_checks.py exercises all of them against output
captured from a real appliance.

The severity vocabulary is four words, and the order they rank in matters:

    ok  <  unknown  <  warn  <  crit

`unknown` outranking `ok` is the important one. A check that could not run —
no host agent, SSH refused, the command failed — must never contribute a green
tile. A dashboard that says "healthy" because it could not look is worse than
no dashboard, because someone acts on it.
"""

from __future__ import annotations

import re

OK = "ok"
UNKNOWN = "unknown"
WARN = "warn"
CRIT = "crit"

_RANK = {OK: 0, UNKNOWN: 1, WARN: 2, CRIT: 3}


def worst(*severities) -> str:
    """The most severe of the arguments. Accepts strings or iterables of them,
    so callers can mix a pane's own state with its rows'."""
    flat: list[str] = []
    for item in severities:
        if isinstance(item, str):
            flat.append(item)
        elif item:
            flat.extend(x for x in item if isinstance(x, str))
    if not flat:
        return UNKNOWN
    return max(flat, key=lambda s: _RANK.get(s, 1))


# ── Containers ───────────────────────────────────────────────────────────────
# `docker compose ps` status strings, as a CC actually emits them:
#
#   Up 8 days (healthy)              a service with a healthcheck, passing
#   Up 8 days                        a service that declares no healthcheck
#   Up 3 seconds (health: starting)  still inside its start period
#   Up 2 minutes (unhealthy)         the healthcheck is failing
#   Restarting (1) 5 seconds ago     crash-looping
#   Exited (137) 4 minutes ago       stopped
#   Created / Paused / Dead
#
# "Up" with no health suffix is treated as GREEN, not unknown. Four of the CC's
# own services declare no healthcheck (kvision-assist-service and
# kvision-ha-operator among them), so calling those unknown would light the
# dashboard amber permanently on a perfectly healthy box — and a warning that is
# always on is a warning nobody reads.

_UNHEALTHY = re.compile(r"\(unhealthy\)", re.I)
_STARTING = re.compile(r"\(health:\s*starting\)", re.I)


def container_severity(status: str) -> str:
    text = (status or "").strip()
    if not text:
        return UNKNOWN
    low = text.lower()

    if _UNHEALTHY.search(text):
        return CRIT
    if low.startswith("exited") or low.startswith("dead"):
        return CRIT
    if low.startswith("restarting"):
        # Crash-looping is on its way to critical, but a container restarting
        # once during a deploy is normal. Amber, and the row says why.
        return WARN
    if _STARTING.search(text) or low.startswith("created") or low.startswith("paused"):
        return WARN
    if low.startswith("up"):
        return OK
    return UNKNOWN


def parse_compose_ps(text: str) -> list[dict]:
    """`docker compose ps --all --format 'table {{.Service}}\\t{{.Name}}\\t{{.Status}}'`.

    Three columns, and the SERVICE one earns its place: it is the name the
    compose file uses, which is what an expected-service list can be compared
    against. The container name carries the project prefix and the instance
    suffix (`config_dfc_1`) and differs per host.

    Tolerates both the tab the format string asks for and the space padding
    docker actually emits — which you get depends on the docker version, and
    getting it wrong silently yields one column. Two-column output (an older
    agent that has not been updated) is still read, with the container name
    standing in for the service.
    """
    rows: list[dict] = []
    for line in (text or "").splitlines():
        line = line.rstrip()
        if not line.strip():
            continue
        parts = [p.strip() for p in
                 re.split(r"\t+|\s{2,}", line.strip(), maxsplit=2)]
        if len(parts) == 3:
            service, name, status = parts
        elif len(parts) == 2:
            service, name, status = parts[0], parts[0], parts[1]
        else:
            continue
        if service.upper() in ("SERVICE", "NAME"):       # the header row
            continue
        rows.append({"service": service, "name": name, "status": status,
                     "severity": container_severity(status)})
    return rows


def reconcile_compose(rows: list[dict], expected: list[str]) -> list[dict]:
    """Add a row for every expected service that has no container at all.

    This is the check that `docker compose ps` cannot make on its own, and the
    gap it leaves is the dangerous kind. Without `--all` a STOPPED service
    vanishes from the listing entirely, so a CC with one service down reported
    "all 35 services running" — the count quietly shrank by one and the tile
    went green. With `--all` a stopped container comes back as Exited; a
    service that was never created still would not, so it is synthesised here.

    `expected` comes from `docker compose config --services`, which applies
    COMPOSE_PROFILES from the CC's .env — so services belonging to a profile
    this appliance does not run are correctly absent from it, not reported
    missing.
    """
    if not expected:
        return rows
    present = {r["service"] for r in rows}
    missing = [s for s in expected if s not in present]
    return rows + [{"service": s, "name": "", "status": "not created",
                    "severity": CRIT, "missing": True} for s in missing]


def containers_pane(rows: list[dict], expected: list[str] | None = None) -> dict:
    bad = [r for r in rows if r["severity"] in (CRIT, WARN)]
    missing = [r for r in rows if r.get("missing")]
    severity = worst(OK if rows else UNKNOWN, [r["severity"] for r in rows])
    if not rows:
        headline = "no containers reported"
    elif not bad:
        headline = f"all {len(rows)} services running"
    else:
        crit = sum(1 for r in bad if r["severity"] == CRIT)
        headline = (f"{len(bad)} of {len(rows)} services need attention"
                    + (f" ({crit} down or unhealthy)" if crit else ""))
        if missing:
            headline += (f" — {len(missing)} never started"
                         if len(missing) > 1 else
                         f" — {missing[0]['service']} has no container")
    return {"severity": severity, "headline": headline,
            "total": len(rows), "problems": len(bad),
            "missing": len(missing),
            # How many the compose file says should be here, so the UI can say
            # "34 of 36" rather than a count that shrinks when things break.
            # Named _count because the router puts the LIST under "expected".
            "expected_count": len(expected) if expected else len(rows)}


# ── Storage ──────────────────────────────────────────────────────────────────
# `df -PT`, not `df -h`. A CC with 37 containers emits about fifty `overlay`
# rows from `df -h`, one per container, every one of them reporting the SAME
# underlying disk — so a single full filesystem would fire the threshold fifty
# times and the pane would be unreadable. Filtering by the TYPE column, which
# only -T prints, is what removes them.

_PSEUDO_TYPES = {
    "overlay", "tmpfs", "devtmpfs", "squashfs", "nsfs", "ramfs", "autofs",
    "proc", "sysfs", "cgroup", "cgroup2", "devpts", "mqueue", "debugfs",
    "tracefs", "securityfs", "pstore", "bpf", "configfs", "hugetlbfs",
    "efivarfs", "fusectl", "binfmt_misc", "rpc_pipefs", "none",
}


def _is_real_filesystem(fs_type: str) -> bool:
    low = (fs_type or "").lower()
    # fuse.* covers gvfs, sshfs and the portal mounts a desktop image carries.
    return bool(low) and low not in _PSEUDO_TYPES and not low.startswith("fuse")


def parse_df(text: str) -> list[dict]:
    """`df -PT` → one row per REAL filesystem.

    POSIX format guarantees one line per filesystem with no wrapping, which is
    the other reason for -P: `df` normally breaks a long device name across two
    lines and a naive parser then reads half a row.
    """
    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        # 7 fields, and only the last ("Mounted on") may contain spaces.
        parts = line.split(None, 6)
        if len(parts) != 7:
            continue
        device, fs_type, blocks, used, avail, capacity, mount = parts
        if device == "Filesystem":              # the header row
            continue
        if not _is_real_filesystem(fs_type):
            continue
        try:
            pct = int(capacity.rstrip("%"))
            size_kb, used_kb, avail_kb = int(blocks), int(used), int(avail)
        except ValueError:
            continue
        key = (device, mount)
        if key in seen:
            continue
        seen.add(key)
        rows.append({"device": device, "type": fs_type, "mount": mount,
                     "size_kb": size_kb, "used_kb": used_kb,
                     "avail_kb": avail_kb, "pct": pct})
    return rows


def storage_severity(pct: int, warn_pct: int, crit_pct: int) -> str:
    if pct >= crit_pct:
        return CRIT
    if pct >= warn_pct:
        return WARN
    return OK


def storage_pane(rows: list[dict], warn_pct: int, crit_pct: int) -> dict:
    for row in rows:
        row["severity"] = storage_severity(row["pct"], warn_pct, crit_pct)
    bad = sorted((r for r in rows if r["severity"] != OK),
                 key=lambda r: -r["pct"])
    severity = worst(OK if rows else UNKNOWN, [r["severity"] for r in rows])
    if not rows:
        headline = "no filesystems reported"
    elif not bad:
        fullest = max(rows, key=lambda r: r["pct"])
        headline = f"fullest is {fullest['mount']} at {fullest['pct']}%"
    elif len(bad) == 1:
        headline = f"{bad[0]['mount']} is {bad[0]['pct']}% full"
    else:
        headline = (f"{len(bad)} filesystems are filling up — "
                    f"{bad[0]['mount']} at {bad[0]['pct']}%")
    return {"severity": severity, "headline": headline,
            "total": len(rows), "problems": len(bad)}


def parse_largest(text: str) -> list[dict]:
    """`find … -printf '%s\\t%p\\n' | sort -rn | head` → [{bytes, path}]."""
    rows: list[dict] = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        size, sep, path = line.partition("\t")
        if not sep:
            continue
        try:
            rows.append({"bytes": int(size.strip()), "path": path})
        except ValueError:
            continue
    return rows


# ── MariaDB ──────────────────────────────────────────────────────────────────
# `mariadb-check --check --all-databases` prints one line per healthy table:
#
#     vision_ng.user_mgt                                 OK
#
# and, for anything else, the table name alone on a line followed by keyed
# message lines:
#
#     vision_ng.attack_log
#     warning  : 1 client is using or hasn't closed the table properly
#     error    : Table 'vision_ng.attack_log' is marked as crashed
#
# "Table is already up to date" is also a pass — it is what an engine that has
# nothing to check says, and reading it as a failure would report most of a
# healthy CC as broken.

_OK_STATUSES = {"ok", "table is already up to date"}
# One or more spaces, not two: the client pads the name to a fixed column, so a
# table whose qualified name is longer than that column — and on a CC
# `kvision_auto_engine_db.clusters_vcenter_network_data` is — gets exactly one
# space before its verdict. Requiring two silently dropped those rows.
_ROW_RE = re.compile(r"^(\S+\.\S+)\s+(.+?)\s*$")
_NAME_RE = re.compile(r"^(\S+\.\S+)\s*$")
_MSG_RE = re.compile(r"^\s*(note|warning|error|status|info)\s*:\s*(.*)$", re.I)


def parse_mariadb_check(text: str) -> list[dict]:
    tables: list[dict] = []
    current: dict | None = None

    def close():
        nonlocal current
        if current is None:
            return
        errors = [m for m in current["messages"] if m["level"] == "error"]
        status_msgs = [m["text"].strip().lower() for m in current["messages"]
                       if m["level"] == "status"]
        # A block whose only verdict is `status : OK` was repaired or merely
        # remarked on; it is not corrupt. Corruption is an `error :` line, or a
        # status line that is not a pass.
        current["corrupt"] = bool(errors) or any(
            s not in _OK_STATUSES for s in status_msgs)
        if not current["status"]:
            current["status"] = (errors[0]["text"] if errors
                                 else (status_msgs[0] if status_msgs else ""))
        tables.append(current)
        current = None

    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue

        message = _MSG_RE.match(line)
        if message and current is not None:
            current["messages"].append({"level": message.group(1).lower(),
                                        "text": message.group(2).strip()})
            continue
        if message:
            continue                     # a keyed line with no table — ignore

        row = _ROW_RE.match(line)
        if row:
            close()
            schema, _, table = row.group(1).partition(".")
            status = row.group(2).strip()
            tables.append({"schema": schema, "table": table,
                           "status": status, "messages": [],
                           "corrupt": status.lower() not in _OK_STATUSES})
            continue

        name = _NAME_RE.match(line)
        if name:
            close()
            schema, _, table = name.group(1).partition(".")
            current = {"schema": schema, "table": table, "status": "",
                       "messages": [], "corrupt": False}
            continue
        # Anything else is banner or noise from the client; skip it rather than
        # guess, so a version that adds a header line does not become a false
        # "corrupt table" the moment it ships.
    close()
    return tables


def maria_pane(tables: list[dict], error: str = "") -> dict:
    if error:
        return {"severity": UNKNOWN, "headline": error,
                "total": 0, "problems": 0}
    corrupt = [t for t in tables if t["corrupt"]]
    if not tables:
        return {"severity": UNKNOWN, "headline": "no tables were checked",
                "total": 0, "problems": 0}
    if corrupt:
        return {"severity": CRIT,
                "headline": f"{len(corrupt)} of {len(tables)} tables are corrupt",
                "total": len(tables), "problems": len(corrupt)}
    return {"severity": OK, "headline": f"all {len(tables)} tables check out",
            "total": len(tables), "problems": 0}


# ── Elasticsearch ────────────────────────────────────────────────────────────
# EVERY index is judged the same way. `appconfig2` used to be exempt from the
# yellow rule — it asks for a replica a single-node CC cannot place, so its
# yellow was treated as expected and hidden from the verdict.
#
# That exemption is gone, at the operator's request, and the reasoning is worth
# keeping: an index that is permanently excused is an index nobody looks at. The
# exemption was written for the single-node case, but it applied everywhere,
# so on a CC where appconfig2 turned yellow for some OTHER reason the screen
# would have said "every index is green". A dashboard with a permanent blind
# spot is worse than one with a known-noisy row, because the noisy row is at
# least visible.
#
# If a genuinely single-node CC now shows one steady yellow index, that is a
# true statement about its replica settings and belongs on the screen.


def es_indices_health(indices: list[dict]) -> dict:
    """`_cat/indices` rows → {severity, red[], yellow[]}.

    `expected_yellow` is still returned, always empty, so a caller written
    against the old shape keeps working rather than raising a KeyError.
    """
    red, yellow = [], []
    for row in indices or []:
        health = (row.get("health") or "").lower()
        if health == "red":
            red.append(row)
        elif health == "yellow":
            yellow.append(row)

    if red:
        severity = CRIT
    elif yellow:
        severity = WARN
    else:
        severity = OK
    return {"severity": severity, "red": red, "yellow": yellow,
            "expected_yellow": []}


def es_pane(result: dict, error: str = "") -> dict:
    if error:
        return {"severity": UNKNOWN, "headline": error,
                "total": 0, "problems": 0}
    red, yellow = result.get("red", []), result.get("yellow", [])
    if red:
        headline = (f"{len(red)} RED "
                    f"{'index' if len(red) == 1 else 'indices'}"
                    + (f", {len(yellow)} yellow" if yellow else ""))
    elif yellow:
        headline = (f"{len(yellow)} yellow "
                    f"{'index' if len(yellow) == 1 else 'indices'}")
    else:
        headline = "every index is green"
    return {"severity": result.get("severity", UNKNOWN), "headline": headline,
            "total": len(red) + len(yellow), "problems": len(red) + len(yellow)}
