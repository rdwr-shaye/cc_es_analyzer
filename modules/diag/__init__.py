"""Diagnostics module — eliminate or confirm a root cause.

The first phase of the corrective-actions work, and deliberately the read-only
half of it. Several CC features fetch from the internet — signature updates,
the ERT Active Attackers Feed, GeoDB location updates, licence activation — and
when one of them silently stops working, the first question is whether the
appliance can reach the outside world at all. Today that is answered by SSHing
in and running `wget services.radware.com`, which is a step the knowledge base
documents in as many words (KB 3391). This module makes it a screen.

WHY READ-ONLY FIRST. Corrective actions that CHANGE a customer's appliance need
provenance, preconditions, a dry run, confirmation and an audit record before
any of them can ship. A connectivity check needs none of that: it changes
nothing, needs no privilege the app does not already have, and is safe on a
production CC. It is also where the evidence pointed — of the first knowledge
base articles reviewed for automation, the majority resolved to a DIAGNOSTIC
check rather than to a fix.

CAPABILITIES. One, on in both profiles. Reading whether a name resolves is not
a change to the appliance, and an engineer debugging a customer's feed needs it
exactly where the customer's CC is. The target list is FIXED (see targets.py) —
the endpoints take an id, never a host and port, so this cannot become a
general-purpose network probe inside someone's data centre.
"""

from __future__ import annotations

from core.policy import Capability, Module, EMBEDDED, STANDALONE

_BOTH = (STANDALONE, EMBEDDED)


def _module() -> Module:
    # Imported inside the function for the reason the other modules document:
    # the routers import this package's siblings, so a module-scope import
    # would be a cycle.
    from modules.diag.routers import connectivity

    return Module(
        id="diag",
        title="Diagnostics",
        capabilities=(
            Capability(
                id="diag.connectivity",
                title="Check whether this CC can reach the services it depends on",
                profiles=_BOTH,
                note="Resolves, connects and negotiates TLS to a fixed list of "
                     "Radware endpoints, reporting each stage separately so a "
                     "failure names the layer that broke. Changes nothing, and "
                     "cannot be pointed at an arbitrary address.",
            ),
        ),
        routers=(
            (connectivity.router, "diag.connectivity"),
        ),
    )


MODULE = _module
