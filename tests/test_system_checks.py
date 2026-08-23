"""Tests for modules/system/checks.py and core/hostexec.py's validation.

These are the rules that decide whether a support engineer is told a customer's
CyberController is healthy, so they are tested where no CC is required. The
fixtures are real output captured from a lab appliance (10.205.189.20), with the
failure cases spliced in — a healthy box cannot produce a crashed table on
demand, and waiting for one is not a test strategy.

Run it either way:

    python tests/test_system_checks.py
    pytest tests/test_system_checks.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.system import checks as C          # noqa: E402


# ── Fixtures ─────────────────────────────────────────────────────────────────

# Real, trimmed. The last four rows are the states a healthy CC never shows,
# added by hand: an unhealthy service, one inside its start period, a crash
# loop and a stopped container.
COMPOSE_PS = """\
SERVICE                            NAME                                        STATUS
dfc                                config_dfc_1                                Up 8 days (healthy)
kvision-assist-service             config_kvision-assist-service_1             Up 8 days
kvision-ha-operator                config_kvision-ha-operator_1                Up 8 days
kvision-infra-mariadb              config_kvision-infra-mariadb_1              Up 8 days (healthy)
kvision-webui                      config_kvision-webui_1                      Up 8 days (unhealthy)
kvision-reporter                   config_kvision-reporter_1                   Up 3 seconds (health: starting)
kvision-collector                  config_kvision-collector_1                  Restarting (1) 5 seconds ago
postgres                           config_postgres_1                           Exited (137) 4 minutes ago
"""

# `docker compose config --services` on the lab CC: COMPOSE_PROFILES in
# /deploy/config/.env selects 36 of the 37 services the file describes. Trimmed
# to the same set as COMPOSE_PS, plus one that has no container at all.
COMPOSE_EXPECTED = ["dfc", "kvision-assist-service", "kvision-ha-operator",
                    "kvision-infra-mariadb", "kvision-webui", "kvision-reporter",
                    "kvision-collector", "postgres", "kvision-lls"]

# Real `df -PT` from the lab CC. The overlay rows are the point: the live box
# emits about fifty of them, one per running container, all describing the same
# 412G disk. Six are kept here — enough to prove they are dropped and that the
# one real filesystem behind them is counted once.
DF_PT = """\
Filesystem                     Type    1024-blocks     Used Available Capacity Mounted on
tmpfs                          tmpfs      13199576     5916  13193660       1% /run
/dev/mapper/vg_disk-lv_root    ext4       32716560  7796412  23226044      26% /
tmpfs                          tmpfs      65997872    47952  65949920       1% /dev/shm
/dev/sda2                      ext4         996780   134368    793600      15% /boot
/dev/mapper/vg_disk-lv_radware ext4       32716560  1724300  29298156       6% /opt/radware
/dev/mapper/vg_disk-lv_storage ext4      431331344 55944516 353402984      14% /var/lib/docker
overlay                        overlay   431331344 55944516 353402984      14% /var/lib/docker/docker-root/overlay2/0fc7/merged
overlay                        overlay   431331344 55944516 353402984      14% /var/lib/docker/docker-root/overlay2/3995/merged
overlay                        overlay   431331344 55944516 353402984      14% /var/lib/docker/docker-root/overlay2/540f/merged
overlay                        overlay   431331344 55944516 353402984      14% /var/lib/docker/docker-root/overlay2/c868/merged
overlay                        overlay   431331344 55944516 353402984      14% /var/lib/docker/docker-root/overlay2/a7ff/merged
overlay                        overlay   431331344 55944516 353402984      14% /var/lib/docker/docker-root/overlay2/f146/merged
tmpfs                          tmpfs      13199572        0  13199572       0% /run/user/0
"""

# Same shape, wound up so the thresholds have something to fire on.
DF_FULL = """\
Filesystem                     Type    1024-blocks     Used Available Capacity Mounted on
/dev/mapper/vg_disk-lv_root    ext4       32716560 30716560   2000000      94% /
/dev/mapper/vg_disk-lv_radware ext4       32716560 27000000   5716560      83% /opt/radware
/dev/sda2                      ext4         996780   134368    793600      15% /boot
"""

# Real `mariadb-check` rows, with one crashed and one merely warned-about table
# spliced in — the two shapes the multi-line block form produces.
MARIA_CHECK = """\
kvision_auto_engine_db.clusters_vcenter_network_data OK
vision_ng.user_mgt                                 OK
vision_ng.vision_license                           OK
quartz.qrtz_locks                                  Table is already up to date
vision_ng.attack_log
warning  : 1 client is using or hasn't closed the table properly
error    : Table 'vision_ng.attack_log' is marked as crashed and should be repaired
vision_ng.srp_statistics
warning  : Table is marked as crashed
status   : OK
vision.dashboard_config                            OK
"""


# ── Harness ──────────────────────────────────────────────────────────────────

_FAILURES: list[str] = []


def check(label, got, want):
    ok = got == want
    if not ok:
        _FAILURES.append(f"{label}\n     got:  {got!r}\n     want: {want!r}")
    print(f"  {'ok ' if ok else 'BAD'} {label}")


# ── Containers ───────────────────────────────────────────────────────────────

def test_containers():
    print("parse_compose_ps")
    rows = C.parse_compose_ps(COMPOSE_PS)
    check("the header row is not a container", len(rows), 8)
    by_name = {r["name"]: r["severity"] for r in rows}

    check("the service name is kept, not just the container",
          rows[0]["service"], "dfc")
    check("healthy is green", by_name["config_dfc_1"], C.OK)
    # The one that would otherwise light the dashboard amber forever: four CC
    # services declare no healthcheck at all.
    check("Up with no healthcheck is green",
          by_name["config_kvision-assist-service_1"], C.OK)
    check("unhealthy is red", by_name["config_kvision-webui_1"], C.CRIT)
    check("health: starting is amber",
          by_name["config_kvision-reporter_1"], C.WARN)
    check("restarting is amber", by_name["config_kvision-collector_1"], C.WARN)
    check("exited is red", by_name["config_postgres_1"], C.CRIT)

    pane = C.containers_pane(rows)
    check("the pane takes the worst", pane["severity"], C.CRIT)
    check("the pane counts the bad ones", pane["problems"], 4)

    healthy = C.parse_compose_ps(
        "SERVICE  NAME    STATUS\na  a_1  Up 8 days (healthy)\nb  b_1  Up 2 days\n")
    check("an all-green box is green", C.containers_pane(healthy)["severity"], C.OK)
    check("an all-green headline names the count",
          C.containers_pane(healthy)["headline"], "all 2 services running")

    # docker emits tabs when asked for them and spaces when it feels like it.
    tabbed = C.parse_compose_ps("SERVICE\tNAME\tSTATUS\nfoo\tfoo_1\tUp 1 day (unhealthy)\n")
    check("tab-separated output parses too",
          [(r["service"], r["name"], r["severity"]) for r in tabbed],
          [("foo", "foo_1", C.CRIT)])

    # An agent too old to send the SERVICE column still has to parse.
    old = C.parse_compose_ps("NAME    STATUS\nconfig_dfc_1    Up 8 days (healthy)\n")
    check("two-column output from an older agent still parses",
          [(r["service"], r["name"]) for r in old],
          [("config_dfc_1", "config_dfc_1")])

    check("nothing at all is unknown, not green",
          C.containers_pane([])["severity"], C.UNKNOWN)


def test_missing_services():
    """The bug this exists for: a service with NO container is invisible to
    `docker compose ps`, so the count silently shrinks and the tile goes green.
    On the lab CC that read as "all 35 services running" while dfc was down."""
    print("\nreconcile_compose")

    # Every container present and running, but the compose profiles require one
    # more — kvision-lls has no container at all.
    running = C.parse_compose_ps(
        "SERVICE   NAME     STATUS\n"
        "dfc       dfc_1    Up 8 days (healthy)\n"
        "postgres  pg_1     Up 8 days (healthy)\n")
    expected = ["dfc", "postgres", "kvision-lls"]

    naive = C.containers_pane(running)
    check("without reconciliation it reads as healthy", naive["severity"], C.OK)
    check("...and the count quietly shrinks", naive["headline"],
          "all 2 services running")

    rows = C.reconcile_compose(running, expected)
    check("the missing service gets a row", len(rows), 3)
    gap = [r for r in rows if r.get("missing")]
    check("named by its compose service", [r["service"] for r in gap],
          ["kvision-lls"])
    check("with no container name to offer", gap[0]["name"], "")
    check("and it is critical", gap[0]["severity"], C.CRIT)

    pane = C.containers_pane(rows, expected)
    check("the pane is now red", pane["severity"], C.CRIT)
    check("and says which service has nothing", pane["headline"],
          "1 of 3 services need attention (1 down or unhealthy) — "
          "kvision-lls has no container")
    check("the total is what SHOULD run", pane["expected_count"], 3)

    # Two or more, and naming them all in a tile headline is noise.
    rows = C.reconcile_compose(running, expected + ["kvision-ted"])
    pane = C.containers_pane(rows, expected + ["kvision-ted"])
    check("several missing are counted, not listed", pane["headline"],
          "2 of 4 services need attention (2 down or unhealthy) — 2 never started")

    # A profile this CC does not run must not be reported as missing: the
    # expected list is already filtered by COMPOSE_PROFILES, so a service the
    # file describes but this appliance excludes simply is not in it.
    rows = C.reconcile_compose(running, ["dfc", "postgres"])
    check("nothing missing when the profiles agree",
          [r for r in rows if r.get("missing")], [])
    check("...and the pane is green", C.containers_pane(rows, ["dfc", "postgres"])["severity"], C.OK)

    # An agent too old to answer compose.expected: degrade to what is running
    # rather than inventing a gap.
    check("no expected list means no synthesised rows",
          len(C.reconcile_compose(running, [])), 2)

    # And the exited container that started all this — with --all it is in the
    # listing as Exited, so it needs no synthesising, just correct grading.
    exited = C.parse_compose_ps(
        "SERVICE   NAME          STATUS\n"
        "dfc       config_dfc_1  Exited (128) 47 minutes ago\n"
        "postgres  pg_1          Up 8 days (healthy)\n")
    rows = C.reconcile_compose(exited, ["dfc", "postgres"])
    check("a stopped container is present and red, not missing",
          [(r["service"], r["severity"], bool(r.get("missing"))) for r in rows],
          [("dfc", C.CRIT, False), ("postgres", C.OK, False)])


# ── Storage ──────────────────────────────────────────────────────────────────

def test_storage():
    print("\nparse_df")
    rows = C.parse_df(DF_PT)
    mounts = [r["mount"] for r in rows]
    check("only real filesystems survive", mounts,
          ["/", "/boot", "/opt/radware", "/var/lib/docker"])
    check("the six overlay rows are gone",
          [m for m in mounts if "overlay2" in m], [])
    check("tmpfs is gone too", [r for r in rows if r["type"] == "tmpfs"], [])
    check("sizes are parsed", rows[0]["size_kb"], 32716560)

    pane = C.storage_pane(rows, 80, 90)
    check("a roomy box is green", pane["severity"], C.OK)
    check("and says which is fullest", pane["headline"], "fullest is / at 26%")

    full = C.parse_df(DF_FULL)
    pane = C.storage_pane(full, 80, 90)
    check("94% is critical", pane["severity"], C.CRIT)
    check("both over-threshold filesystems are counted", pane["problems"], 2)
    by_mount = {r["mount"]: r["severity"] for r in full}
    check("83% is a warning", by_mount["/opt/radware"], C.WARN)
    check("15% is fine", by_mount["/boot"], C.OK)

    check("exactly at the warn threshold warns",
          C.storage_severity(80, 80, 90), C.WARN)
    check("exactly at the crit threshold is critical",
          C.storage_severity(90, 80, 90), C.CRIT)
    check("one under warns not at all",
          C.storage_severity(79, 80, 90), C.OK)

    print("\nparse_largest")
    largest = C.parse_largest(
        "10737418240\t/var/lib/docker/big.img\n"
        "52428800\t/var/lib/docker/some file with spaces.log\n"
        "garbage line\n")
    check("size and path split on the tab",
          largest, [{"bytes": 10737418240, "path": "/var/lib/docker/big.img"},
                    {"bytes": 52428800,
                     "path": "/var/lib/docker/some file with spaces.log"}])


# ── MariaDB ──────────────────────────────────────────────────────────────────

def test_maria():
    print("\nparse_mariadb_check")
    tables = C.parse_mariadb_check(MARIA_CHECK)
    check("every table is accounted for", len(tables), 7)

    by_table = {t["table"]: t for t in tables}
    check("a plain OK is healthy", by_table["user_mgt"]["corrupt"], False)
    check("'already up to date' is healthy too",
          by_table["qrtz_locks"]["corrupt"], False)
    check("an error line means corrupt", by_table["attack_log"]["corrupt"], True)
    check("the schema is split off", by_table["attack_log"]["schema"], "vision_ng")
    check("the error text is kept for the operator",
          by_table["attack_log"]["status"],
          "Table 'vision_ng.attack_log' is marked as crashed and should be repaired")
    # A warning followed by `status : OK` is a table that was fine after all.
    # Reading that as corrupt would send an engineer to repair a healthy table.
    check("a warning that resolves to OK is not corrupt",
          by_table["srp_statistics"]["corrupt"], False)
    check("its warning is still visible",
          [m["level"] for m in by_table["srp_statistics"]["messages"]],
          ["warning", "status"])

    pane = C.maria_pane(tables)
    check("one crashed table is critical", pane["severity"], C.CRIT)
    check("and it is counted alone", pane["problems"], 1)

    clean = C.parse_mariadb_check("vision_ng.a    OK\nvision_ng.b    OK\n")
    check("a clean check is green", C.maria_pane(clean)["severity"], C.OK)
    check("a check that did not run is unknown",
          C.maria_pane([], error="no host agent")["severity"], C.UNKNOWN)
    check("nothing checked is unknown, not green",
          C.maria_pane([])["severity"], C.UNKNOWN)


# ── Elasticsearch ────────────────────────────────────────────────────────────

def test_es():
    print("\nes_indices_health")
    idx = lambda name, health: {"index": name, "health": health}  # noqa: E731

    result = C.es_indices_health([idx("appconfig2", "yellow"),
                                  idx("cc-attacks-2026.08", "green")])
    check("appconfig2 yellow is expected, so green overall",
          result["severity"], C.OK)
    check("and it is reported as expected rather than hidden",
          [r["index"] for r in result["expected_yellow"]], ["appconfig2"])
    check("the headline says so",
          C.es_pane(result)["headline"], "every index is green (1 expected yellow)")

    result = C.es_indices_health([idx("appconfig2", "yellow"),
                                  idx("cc-attacks-2026.08", "yellow")])
    check("any other yellow index is a warning", result["severity"], C.WARN)
    check("appconfig2 is not counted among them",
          [r["index"] for r in result["yellow"]], ["cc-attacks-2026.08"])

    result = C.es_indices_health([idx("appconfig2", "red")])
    check("appconfig2 RED is critical — the exemption is yellow only",
          result["severity"], C.CRIT)

    result = C.es_indices_health([idx("a", "green"), idx("b", "green")])
    check("all green is green", result["severity"], C.OK)
    check("a cluster we could not reach is unknown",
          C.es_pane({}, error="not connected")["severity"], C.UNKNOWN)


# ── Roll-up ──────────────────────────────────────────────────────────────────

def test_worst():
    print("\nworst")
    check("crit beats everything", C.worst(C.OK, C.WARN, C.CRIT), C.CRIT)
    check("warn beats unknown", C.worst(C.UNKNOWN, C.WARN), C.WARN)
    # The one that matters: a check that could not run must not read as healthy.
    check("unknown beats ok", C.worst(C.OK, C.UNKNOWN), C.UNKNOWN)
    check("all ok is ok", C.worst(C.OK, C.OK), C.OK)
    check("a list works too", C.worst([C.OK, C.CRIT]), C.CRIT)
    check("nothing at all is unknown", C.worst(), C.UNKNOWN)


# ── hostexec argument validation ─────────────────────────────────────────────
# The app's own copy of the allowlist. deploy/host_agent.py --self-test covers
# the host's independent copy; both have to hold, and they are separate code.

def test_hostexec_validation():
    print("\nhostexec.validate")
    from core import hostexec

    def refuses(label, op, args):
        try:
            hostexec.validate(op, args)
        except hostexec.HostExecError as exc:
            print(f"  ok  refuses {label} -- {exc}")
            return
        _FAILURES.append(f"validate accepted {label}: {op} {args}")
        print(f"  BAD accepted {label}")

    refuses("an unknown operation", "shell.exec", {})
    refuses("a semicolon in a container name", "container.logs", {"name": "a; id"})
    refuses("backticks in a container name", "container.logs", {"name": "`id`"})
    refuses("a space in a container name", "container.logs", {"name": "a b"})
    refuses("a traversal in a mount", "disk.largest", {"mount": "/var/../etc"})
    refuses("a relative mount", "disk.largest", {"mount": "var"})
    refuses("an unexpected argument", "compose.ps", {"cmd": "id"})
    refuses("a line count past the cap", "container.logs",
            {"name": "x", "lines": 10 ** 9})
    refuses("a missing required argument", "disk.largest", {})

    check("defaults are filled in",
          hostexec.validate("container.logs", {"name": "cc-admin"}),
          {"name": "cc-admin", "lines": 500})
    check("a legitimate mount is accepted",
          hostexec.validate("disk.largest", {"mount": "/var/lib/docker"}),
          {"mount": "/var/lib/docker", "n": 20})

    # Whatever the validators let through must still be inert once it reaches a
    # command line. This is the belt to the allowlist's braces.
    for op, args in [("container.logs", {"name": "config_kvision-infra-mariadb_1"}),
                     ("disk.largest", {"mount": "/opt/radware"})]:
        line = hostexec.OPS[op]["command"](hostexec.validate(op, args))
        bad = [ch for ch in (";", "&&", "||", "$(", "`") if ch in line.replace(
            "$(docker", "").replace("2>&1", "")]
        check(f"{op} builds a command with no injected shell", bad, [])


def main() -> int:
    test_containers()
    test_missing_services()
    test_storage()
    test_maria()
    test_es()
    test_worst()
    test_hostexec_validation()
    print("\n" + "=" * 66)
    if _FAILURES:
        print("FAILURES:")
        for failure in _FAILURES:
            print(" - " + failure)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
