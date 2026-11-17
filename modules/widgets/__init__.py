"""Widget Debugging module — why is this dashboard widget showing no data.

CC Admin already answers "is this CC healthy" (modules/system) and lets an
engineer browse Elasticsearch, MariaDB and PostgreSQL directly. This module
answers a narrower, more specific question a support engineer actually opens a
ticket with: a named widget on the CC's own dashboard is empty, and the fix
depends on WHERE in the pipeline the data was lost — the query layer, the
index, or collection from the device itself. See KVISION_PROJECT's
WIDGET_DEBUG_HELPER.md (outside this repo — the monorepo root) for the
three-check method this module implements, and
C:\\Users\\ShayE\\.claude\\plans\\indexed-scribbling-fairy.md for the design
this file follows.

TWO CAPABILITIES, in different states, because the three checks are not all
the same kind of thing:

  ON EVERYWHERE — widget.debug. Replays the widget's own REST call (Check 1),
  resolves and inspects its Elasticsearch index (Check 2), and reads the last
  polling-task status from MariaDB (Check 3, Tier A). All three are reads —
  the same risk class as system.health, which is also on everywhere.

  LOCKED EVERYWHERE UNTIL AN OPERATOR UNLOCKS IT — widget.device_probe. Calls
  a managed DefensePro device's own HTTPS management API directly, using that
  device's stored credential decrypted from vision_ng.device_access. This is a
  materially different kind of access from anything else in the tool: every
  other capability reads THIS CC's own datastores; this one authenticates to
  and calls a SEPARATE, customer-owned appliance the CC merely manages. Off
  everywhere by default, and — see modules/widgets/device_probe.py — reachable
  only where a second, independent key (a read-only bind mount of the CC
  host's own AES key file) has also been deliberately added at deploy time.
  Standalone can never carry it: the bind mount only exists inside the
  monitoring compose an embedded install rides in, so there is nothing to
  unlock there no matter how the property file is set.
"""

from __future__ import annotations

from core.policy import Capability, Module, EMBEDDED, STANDALONE

_BOTH = (STANDALONE, EMBEDDED)
_NEITHER: tuple[str, ...] = ()


def _module() -> Module:
    # Imported inside the function for the reason every other module in this
    # package documents: the routers import this package's siblings, so a
    # module-scope import would be a cycle.
    from modules.widgets.routers import debug, probe

    return Module(
        id="widgets",
        title="Widget Debugging",
        capabilities=(
            Capability(
                id="widget.debug",
                title="Diagnose why a dashboard widget shows no data",
                profiles=_BOTH,
                note="Three read-only checks: replay the widget's own VRM "
                     "REST call, resolve and inspect its Elasticsearch "
                     "index, read the last polling-task status from "
                     "MariaDB. Never touches a DefensePro device directly "
                     "and never reads a device credential — that is "
                     "widget.device_probe, a separate capability.",
            ),
            Capability(
                id="widget.device_probe",
                title="Call a managed DefensePro device's own API directly",
                profiles=_NEITHER,
                unlockable=True,
                note="Decrypts the device's stored HTTPS credential "
                     "(vision_ng.device_access) and calls its management "
                     "API on port 8790 — the same customer appliance the "
                     "CC manages, not the CC itself. Two independent keys "
                     "required: this capability unlocked via the property "
                     "file, AND a read-only bind mount of the CC host's "
                     "AES key file added to the compose file an operator "
                     "deploys. Reachable only where that mount exists — "
                     "which today means embedded only; there is nothing "
                     "for standalone to unlock.",
            ),
        ),
        routers=(
            (debug.router, "widget.debug"),
            (probe.router, "widget.device_probe"),
        ),
    )


MODULE = _module
