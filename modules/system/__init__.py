"""System module — is this CyberController healthy?

The first question a support engineer has on a CC is not "what is in this
index", it is "is this box actually working". Until now the tool could not
answer it and the engineer answered it by hand, over SSH, with three commands.
This module makes that the landing page.

Four checks, one verdict each, and a global state that is the most severe of
them: the CC's containers, the host's filesystems, Elasticsearch's indices and
MariaDB's tables. Three of the four need the HOST, which the container cannot
see — see core/hostexec.py for how they get there and why it is an operation
allowlist rather than a shell.

CAPABILITIES, in three groups, because they are not all in the same state and a
reviewer who assumes they are will draw the wrong conclusion about where the
boundary sits.

  ON EVERYWHERE — system.health, system.logs, system.storage.download. Reading
  a check, a container log, or a copy of a log file is what support does all
  day and none of it changes the appliance. Download is named separately from
  the other two only so a deployment with a data-residency rule can switch it
  off; it is on by default.

  BUILT BUT LOCKED — system.storage.delete. The route, the host operation and
  the safety classifier all exist. It is off because it needs TWO keys: this
  capability unlocked via the property file, AND the host agent started with
  --allow-delete. Unlocking it here alone does nothing, which is the point.

  DECLARED, NOT BUILT — system.es.delete_index, system.maria.repair,
  system.maria.recreate. No route, and deploy/host_agent.py has no operation
  that would carry them out. They exist in the registry so the UI can name the
  reason a control is dead rather than showing an unexplained grey box. Turning
  one on is three edits: implement the route, register it against its
  capability below, and add the matching operation to deploy/host_agent.py on
  the host. The last of those is deliberate — the host's allowlist is outside
  the container's reach, so no change here alone can make an appliance act.
"""

from __future__ import annotations

from core.policy import Capability, Module, EMBEDDED, STANDALONE

_BOTH = (STANDALONE, EMBEDDED)
_NEITHER: tuple[str, ...] = ()


def _module() -> Module:
    # Imported inside the function for the reason modules/es and modules/maria
    # both document: the routers import this package's siblings, so importing
    # them at module scope would be a cycle.
    from modules.system.routers import dashboard

    return Module(
        id="system",
        title="System health",
        capabilities=(
            Capability(
                id="system.health",
                title="See whether this CC is healthy",
                profiles=_BOTH,
                note="Reads container status, disk usage, index health and "
                     "table integrity. Changes nothing.",
            ),
            Capability(
                id="system.logs",
                title="Read and download a container's log",
                profiles=_BOTH,
                note="The point of noticing a container is unhealthy is being "
                     "able to see why without leaving the tool.",
            ),

            # ── Reading a file off the CC ────────────────────────────────────
            # Built and on: this one HAS a route (download_router) and a host
            # operation (file.read), held to the same allowlist as deletion.
            Capability(
                id="system.storage.download",
                title="Download a log, heap dump or zip off this CC",
                profiles=_BOTH,
                note="Same allowlist as deletion — the files an engineer may "
                     "take a copy of are the files they may remove, and having "
                     "both is what makes deleting one safe. On by default "
                     "because keeping the log before clearing the disk is the "
                     "normal way to do this job, but named separately so a "
                     "deployment with a data-residency rule can switch it off.",
            ),
            Capability(
                id="system.storage.delete",
                title="Delete a log, heap dump or zip to reclaim disk space",
                profiles=_NEITHER,
                unlockable=True,
                note="Restricted by modules/system/safety.py to logs, heap "
                     "dumps and zips, and refused outright inside backups, "
                     "configuration and datastore volumes — the biggest files "
                     "on a CC are usually a Lucene segment or MariaDB's Aria "
                     "log. TWO keys: this capability, plus the host agent "
                     "started with --allow-delete. Unlocking this alone does "
                     "nothing, which is the point.",
            ),
            Capability(
                id="system.es.delete_index",
                title="Delete a RED Elasticsearch index",
                profiles=_NEITHER,
                unlockable=True,
                note="Not implemented yet. A RED index is usually the fastest "
                     "way back to a green cluster, and always a data loss.",
            ),
            Capability(
                id="system.maria.repair",
                title="Repair a corrupted MariaDB table",
                profiles=_NEITHER,
                unlockable=True,
                note="Not implemented yet. In-place `mariadb-check --repair` "
                     "for the tables the check flagged; non-disruptive, but it "
                     "writes to a customer's production database.",
            ),
            Capability(
                id="system.maria.recreate",
                title="Recreate the schemas and restore the last backup",
                profiles=_NEITHER,
                unlockable=True,
                note="Not implemented yet. Wraps the CC's own "
                     "repair_mysql_db.sh, for a MariaDB container that will not "
                     "start. Stops vision and loses everything written since "
                     "the last nightly dump — the heaviest action in the tool.",
            ),
        ),
        routers=(
            (dashboard.router, "system.health"),
            (dashboard.logs_router, "system.logs"),
            (dashboard.download_router, "system.storage.download"),
            (dashboard.delete_router, "system.storage.delete"),
        ),
    )


MODULE = _module
