"""What this CC needs to reach on the outside, and why.

A FIXED table, and that is a security property rather than a limitation. The
connectivity endpoints take a target ID from this table, never a host and port
from the caller — otherwise the screen would be a general-purpose probe running
inside a customer's data centre, able to map their internal network and report
which of their addresses answer. Anyone wanting to test something not listed
here has a shell; the tool declines to become one.

Every entry answers "why would a support engineer care", because the whole
point of the screen is to eliminate or confirm a root cause. "portals.radware.com
is unreachable" means nothing on its own; "the Signature Update Service cannot
be reached, which is why signature updates have not applied" is the finding.

PROVENANCE. Each target cites the knowledge-base article that establishes it as
a real dependency. These were not guessed — KB 3391 documents the manual check
this screen automates, in as many words:

    "run the following CLI command to verify that APSolute Vision has
     connectivity to services.radware.com: wget services.radware.com"
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Target:
    id: str
    label: str
    host: str
    port: int = 443
    scheme: str = "https"
    path: str = "/"
    # What breaks when this is unreachable. Written for the engineer reading
    # the result, not for whoever added the row.
    purpose: str = ""
    # Why a failure here might be a false alarm, when that is true of the
    # target. A CDN-backed host that refuses HEAD is not a broken network.
    caveat: str = ""
    # Knowledge-base article ids that establish this dependency.
    sources: tuple[str, ...] = field(default_factory=tuple)
    # Whether a failure is a real finding or merely informational. A support
    # file share being down does not stop the appliance doing its job.
    critical: bool = True


TARGETS: tuple[Target, ...] = (
    Target(
        id="radware-services",
        label="Radware Services (SUS / EAAF / GEO / etc.)",
        host="services.radware.com",
        purpose="Signature Update Service and ERT Active Attackers Feed "
                "authorisation. If this is unreachable, signature and feed "
                "updates cannot be fetched or authorised, and devices keep "
                "running whatever they last received.",
        caveat="The address behind this name changes; a probe result is about "
               "reachability now, not about a fixed IP.",
        sources=("3391",),
    ),
    Target(
        id="radware-ti-feed",
        label="Threat-intel / AWS s3 bucket",
        host="radwareti.s3.amazonaws.com",
        purpose="Where the ERT Active Attackers Feed and the GeoDB location "
                "updates are actually downloaded from, after Services "
                "authorises them. A CC that authorises but cannot download "
                "will look like a feed that silently never updates.",
        caveat="S3 behind a CDN: the address varies by location, and the "
               "bucket root may answer 403 to an unauthenticated request. "
               "Reaching the TLS layer is the meaningful signal here, not the "
               "HTTP status.",
        sources=("1093778", "1029823"),
    ),
    # FlexNet licensing, the file-transfer share and the support portal were
    # dropped from this screen — not useful checked from inside the CC: an
    # engineer debugging connectivity from here cares whether the appliance
    # can reach what it actually depends on to function (Services and the
    # feed bucket above), not licensing or support-site reachability.
)

_BY_ID = {t.id: t for t in TARGETS}


def get(target_id: str) -> Target | None:
    return _BY_ID.get(target_id)


def all_ids() -> list[str]:
    return [t.id for t in TARGETS]
