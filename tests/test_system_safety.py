"""Tests for modules/system/safety.py — what may and may not be deleted.

Weighted deliberately towards REFUSALS. An allow-rule that is too tight costs
an engineer a trip to the machine; a deny-rule that is too loose costs a
customer their database. The paths below are real ones taken from a CC
(10.205.189.20), including the actual top of its largest-files list, where two
of the three biggest files on the box are datastore internals.

    python tests/test_system_safety.py
    pytest tests/test_system_safety.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.system import safety                    # noqa: E402

_FAILURES: list[str] = []
_DOCKER = "/var/lib/docker/docker-root"


def allows(path, note=""):
    verdict = safety.classify(path)
    if verdict["deletable"]:
        print(f"  ok  allows   {path[:78]}")
    else:
        _FAILURES.append(f"should ALLOW {path}\n     refused: {verdict['reason']}")
        print(f"  BAD refuses  {path[:78]}")


def refuses(path, expect="", note=""):
    """`expect` is a fragment of the reason. It is asserted because a refusal
    the operator cannot act on is only half the job: "it is MariaDB's Aria
    transaction log" tells them to go and truncate it properly, "not allowed"
    tells them nothing."""
    verdict = safety.classify(path)
    if not verdict["deletable"]:
        if expect and expect.lower() not in verdict["reason"].lower():
            _FAILURES.append(f"{path}\n     refused for the wrong reason: "
                             f"{verdict['reason']!r} (wanted {expect!r})")
            print(f"  BAD wrong reason {path[:66]}")
        else:
            print(f"  ok  refuses  {path[:60]} -- {verdict['reason'][:44]}")
    else:
        _FAILURES.append(f"should REFUSE {path}" + (f" ({note})" if note else ""))
        print(f"  BAD ALLOWS   {path[:78]}  <-- {note}")


# ── The list the storage screen actually shows ───────────────────────────────

def test_the_real_largest_files():
    """The genuine top of `find /var/lib/docker -printf '%s\\t%p'| sort -rn`
    on the lab CC. Two of the three biggest files are datastore internals —
    which is the whole reason this module exists."""
    print("the real largest-files list, biggest first")

    allows(f"{_DOCKER}/overlay2/573856a5/diff/opt/radware/mgt-server/third-party/"
           f"jboss-4.2.3.GA/server/insite/log/boot.log")
    refuses(f"{_DOCKER}/volumes/config_osdata/_data/nodes/0/indices/aIP1hYtE/0/index/_23d.fdt",
            expect="Lucene", note="952 MB OpenSearch shard segment")
    refuses(f"{_DOCKER}/volumes/config_osdata/_data/nodes/0/indices/aIP1hYtE/0/index/_15e.fdt",
            expect="Lucene", note="900 MB OpenSearch shard segment")
    refuses(f"{_DOCKER}/volumes/config_dbdata/_data/data/aria_log.00000001",
            expect="Aria", note="504 MB MariaDB transaction log")
    refuses(f"{_DOCKER}/overlay2/2dc991ef/diff/opt/radware/policy-service/app/policy-service.jar",
            expect="code", note="484 MB application jar")
    refuses(f"{_DOCKER}/volumes/config_osdata/_data/nodes/0/indices/aIP1hYtE/0/index/"
            f"_15e_Lucene80_0.dvd", expect="Lucene")
    refuses("/var/lib/docker/radware-storage/data/prometheus/data/01KYTM37/chunks/000001",
            note="prometheus TSDB chunk, no extension at all")
    refuses(f"{_DOCKER}/overlay2/4d117ad9/diff/opt/radware/reporting-module/node_modules/"
            f"puppeteer/.local-chromium/linux-901912/chrome-linux/chrome",
            note="a 298 MB binary with no extension")


# ── The backups the recovery procedure needs ─────────────────────────────────

def test_backups_are_untouchable():
    """repair_mysql_db.sh restores the newest dump per schema. Deleting one is
    not a lost file — it is the loss of the recovery path for the exact failure
    the dashboard is there to spot. They are also .gz, the shape a naive
    "compressed things are rotated logs" rule would have swept up."""
    print("\nthe nightly MariaDB dumps")

    for schema in ("vision_ng", "vision", "quartz", "kvision_auto_engine_db"):
        refuses(f"/opt/radware/storage/backup/mysql_dumps/{schema}/"
                f"11.08.2026_00_00_01_{schema}.sql.gz",
                expect="backup", note="the DB restore depends on this")

    refuses("/opt/radware/storage/backup/mysql_dumps/vision_ng/anything.log",
            expect="backup",
            note="the directory is off limits whatever the file is called")
    refuses("/opt/radware/storage/backup/elasticsearch/snapshot-1.zip",
            expect="backup", note="a zip, but still a backup")


# ── Everything else that must be refused ─────────────────────────────────────

def test_refusals():
    print("\nengine and index files")
    refuses("/var/lib/mysql/ib_logfile0", expect="InnoDB")
    refuses("/var/lib/mysql/ibdata1", expect="InnoDB")
    refuses("/var/lib/mysql/undo_001", expect="InnoDB")
    refuses("/var/lib/mysql/vision_ng/user_mgt.ibd", expect="table")
    refuses("/var/lib/mysql/mysql-bin.000042")
    refuses("/data/nodes/0/indices/abc/0/index/segments_9")
    refuses("/data/nodes/0/indices/abc/0/translog/translog-3.tlog")

    print("\ncode, configuration and secrets")
    refuses("/opt/radware/app/service.jar", expect="code")
    refuses("/opt/radware/app/lib/libcrypto.so.3")
    refuses("/tmp/deploy.sh", expect="script")
    refuses("/tmp/settings.yaml", expect="configuration")
    refuses("/tmp/server.key", expect="certificate")
    refuses("/opt/radware/mgt-server/properties/cc_admin.properties",
            expect="property", note="our own capability unlock")
    refuses("/opt/radware/box/bin/repair_mysql_db.sh",
            expect="appliance", note="the recovery script itself")

    print("\nsystem directories")
    for path, why in (("/etc/passwd", "system"), ("/boot/vmlinuz-5.15.0", "system"),
                      ("/usr/bin/docker", "system"), ("/lib/systemd/systemd", "system"),
                      ("/root/.bashrc", "home"), ("/proc/1/mem", "kernel")):
        refuses(path, expect=why)
    # Even when the name would otherwise pass.
    refuses("/etc/nginx/access.log", expect="system",
            note="a log, but in a system directory")

    print("\nshapes that are not files, or not plain paths")
    refuses("relative/path.log", expect="absolute")
    refuses("/var/log/../../etc/passwd", expect="absolute")
    refuses("/var/log/", expect="directory")
    refuses("/var/log/app.log\n/etc/passwd", expect="absolute",
            note="an embedded newline")
    refuses("", expect="absolute")

    # .txt is allowed generally, so the denylist is the only thing standing
    # between a hand-saved text file and deletion when it sits somewhere that
    # matters. These are the cases that prove it still does.
    print("\n.txt is allowed, but not everywhere")
    refuses("/opt/radware/storage/backup/mysql_dumps/vision_ng/notes.txt",
            expect="backup")
    refuses("/opt/radware/storage/dc_config/kvision-infra-mariadb/readme.txt",
            expect="configuration")
    refuses("/etc/hostname.txt", expect="system")
    refuses(f"{_DOCKER}/volumes/config_osdata/_data/nodes/0/indices/a/0/notes.txt",
            expect="datastore")

    print("\nthings that are simply not logs")
    refuses("/opt/radware/storage/data/report.pdf")
    refuses("/opt/radware/storage/data/export.csv")
    refuses("/opt/radware/storage/tmp/backup.tar.gz", expect="archive")
    refuses("/opt/radware/storage/tmp/vm.qcow2", expect="image")
    refuses("/opt/radware/storage/data/somefile")


def test_allows():
    print("\nwhat SHOULD be deletable")
    allows("/opt/radware/logs/es/vision-es.log")
    allows("/opt/radware/logs/es/vision-es.log.1")
    allows("/opt/radware/logs/es/vision-es.log.7.gz")
    allows("/opt/radware/logs/app/catalina.out")
    allows("/opt/radware/logs/app/catalina.out.5")
    allows("/opt/radware/logs/app/stderr.err")
    allows("/opt/radware/logs/app/access_log-20260811")
    allows("/opt/radware/logs/app/app.log.2026-08-11")
    allows("/opt/radware/logs/app/app.log.2026-08-11.gz")
    # .txt, by explicit decision: Tomcat's default access log carries it. The
    # widest entry in the allowlist, and the one that leans on the denylist
    # below to stay safe.
    allows("/opt/radware/logs/tomcat/localhost_access_log.2026-08-11.txt")
    allows("/deploy/testfile.txt")
    # Classic /var/log names with no extension. Seen side by side on a real CC:
    # kern.log.1 was deletable and syslog.1 next to it was not, which is an
    # inconsistency an engineer cannot act on.
    allows("/var/log/syslog")
    allows("/var/log/syslog.1")
    allows("/var/log/kern.log.1")
    allows("/var/log/messages-20260811.gz")
    allows("/var/log/dmesg.0")
    refuses("/var/log/syslogd.conf", expect="configuration",
            note="the bare-name rule must not swallow a config that starts the same")
    allows("/opt/radware/tmp/java_pid1234.hprof", note="the heap dump case")
    allows("/opt/radware/tmp/java_pid1234.hprof.gz")
    allows("/opt/radware/tmp/techsupport-20260811.zip")
    allows("/opt/radware/tmp/heapdump.dmp")
    # Inside a container's writable layer — where the 2.2 GB boot.log lives.
    allows(f"{_DOCKER}/overlay2/abc123/diff/var/log/messages.log")


def test_gz_is_not_a_free_pass():
    """.gz means 'compressed', not 'rotated log'. The rule strips the suffix
    and judges what is underneath, so a compressed log passes and a compressed
    SQL dump does not — and the dump is additionally in a denied directory, so
    two independent gates hold it."""
    print("\n.gz is judged by what is underneath it")
    allows("/opt/radware/logs/app/server.log.gz")
    refuses("/opt/radware/tmp/vision_ng.sql.gz", expect="database",
            note="a dump that happens to be outside the backup directory")
    refuses("/opt/radware/tmp/config.yaml.gz", expect="configuration")
    refuses("/opt/radware/tmp/service.jar.gz", expect="code")


def test_annotate():
    print("\nannotate")
    rows = safety.annotate([
        {"bytes": 2364196622, "path": "/opt/radware/logs/insite/boot.log"},
        {"bytes": 504815616,
         "path": f"{_DOCKER}/volumes/config_dbdata/_data/data/aria_log.00000001"},
    ])
    ok = (rows[0]["deletable"] is True and rows[1]["deletable"] is False
          and rows[0]["bytes"] == 2364196622 and "Aria" in rows[1]["reason"])
    print(f"  {'ok ' if ok else 'BAD'} rows keep their size and gain a verdict")
    if not ok:
        _FAILURES.append(f"annotate: {rows}")


def main() -> int:
    test_the_real_largest_files()
    test_backups_are_untouchable()
    test_refusals()
    test_allows()
    test_gz_is_not_a_free_pass()
    test_annotate()
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
