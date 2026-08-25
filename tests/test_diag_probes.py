"""Tests for modules/diag/probes.py — the rules that interpret a probe.

The network half of this module cannot be tested without a network, and does
not need to be: `socket` and `ssl` are not ours to verify. What IS ours, and
what these tests cover, is the INTERPRETATION — the rules that decide whether
an engineer is told their connectivity is fine.

Both directions of that judgement are expensive to get wrong, and they are
expensive in different ways:

  * A false FAILURE sends someone to argue with a customer's firewall team
    about a network that was never broken. The classic cause is treating an
    HTTP 401/403 as a connectivity problem when it is proof of the opposite —
    the request arrived, was understood, and was answered.

  * A false SUCCESS is worse and quieter. It closes off the real root cause,
    and the engineer goes looking somewhere else entirely.

So the assertions below lean on the awkward cases rather than the happy path.

    python tests/test_diag_probes.py
    pytest tests/test_diag_probes.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.diag import probes                      # noqa: E402
from modules.diag.probes import OK, UNKNOWN, WARN, CRIT   # noqa: E402

_FAILURES: list[str] = []


def check(label, got, want):
    if got == want:
        print(f"  ok  {label}")
    else:
        _FAILURES.append(f"{label}: expected {want!r}, got {got!r}")
        print(f"  BAD {label} — expected {want!r}, got {got!r}")


def stage(name, status, detail=""):
    return {"stage": name, "status": status, "detail": detail, "ms": 1}


# ── HTTP interpretation ──────────────────────────────────────────────────────

def test_http_verdict():
    print("\nan HTTP status is a statement about CONNECTIVITY, not about the URL")
    for code in (200, 204, 301, 302):
        check(f"HTTP {code} is reachable", probes.http_verdict(code, True)[0], OK)

    # The rule that matters most. A feed bucket answering 403 has PROVEN the
    # path works; calling it a failure is how a diagnostic screen sends someone
    # to fix a firewall that was fine.
    for code in (401, 403):
        status, detail = probes.http_verdict(code, True)
        check(f"HTTP {code} counts as reachable", status, OK)
        check(f"...and HTTP {code} explains why",
              "does not indicate a connectivity problem" in detail, True)

    status, detail = probes.http_verdict(404, True)
    check("HTTP 404 is reachable — the host answered", status, OK)
    check("...and says the path is wrong, not the network",
          "network is fine" in detail, True)

    check("HTTP 500 on a critical target is a warning",
          probes.http_verdict(500, True)[0], WARN)
    check("HTTP 500 on a target nothing depends on is only unknown",
          probes.http_verdict(500, False)[0], UNKNOWN)
    check("no response at all is unknown, never ok",
          probes.http_verdict(None, True)[0], UNKNOWN)


# ── Staged summary ───────────────────────────────────────────────────────────

def test_summarize_success():
    print("\na clean run reports reachable")
    stages = [stage("dns", OK), stage("tcp", OK), stage("tls", OK), stage("http", OK)]
    out = probes.summarize(stages, critical=True)
    check("severity", out["severity"], OK)
    check("no failed stage", out["failed_stage"], None)


def test_summarize_names_the_layer():
    """The headline has to name the LAYER, because that is what decides who
    fixes it: DNS is the resolver, TCP is the firewall, TLS is usually the
    inspecting proxy. 'Cannot reach it' identifies none of them."""
    print("\na failure names the layer that broke")

    out = probes.summarize([stage("dns", CRIT, "NXDOMAIN")], critical=True)
    check("dns failure severity", out["severity"], CRIT)
    check("...blames DNS", "DNS" in out["headline"], True)

    out = probes.summarize([stage("dns", OK), stage("tcp", CRIT, "timed out")],
                           critical=True)
    check("tcp failure", out["failed_stage"], "tcp")
    check("...points at a firewall or proxy",
          "firewall" in out["headline"], True)

    out = probes.summarize(
        [stage("dns", OK), stage("tcp", OK), stage("tls", CRIT, "bad cert")],
        critical=True)
    check("tls failure", out["failed_stage"], "tls")
    check("...points at a TLS-inspecting proxy",
          "TLS-inspecting proxy" in out["headline"], True)


def test_non_critical_is_downgraded():
    """A file share nothing depends on must not turn the screen red — a
    dashboard that cries wolf gets ignored on the day it is right."""
    print("\na target nothing depends on cannot raise an alarm")
    stages = [stage("dns", OK), stage("tcp", CRIT, "refused")]
    check("critical target warns", probes.summarize(stages, True)["severity"], CRIT)
    check("non-critical target is only unknown",
          probes.summarize(stages, False)["severity"], UNKNOWN)


def test_unknown_never_becomes_ok():
    """The System Health rule, restated here: a check that could not run
    reports unknown. A screen that says 'reachable' because it failed to look
    is the one failure mode that matters."""
    print("\na check that could not run never reports ok")
    stages = [stage("dns", UNKNOWN, "skipped: proxy in use"),
              stage("tcp", UNKNOWN), stage("tls", UNKNOWN), stage("http", OK)]
    out = probes.summarize(stages, critical=True)
    check("severity is unknown, not ok", out["severity"], UNKNOWN)


def test_dns_override_is_detected():
    """The bug this exists to prevent, found on a real CC.

    On the lab appliance `services.radware.com` resolves to 10.10.10.10 — a
    sinkhole or split-horizon override — and the connection there fails. The
    first version of this module called that a firewall problem, because DNS
    had "succeeded" and TCP had not. That is a misdiagnosis with a cost: the
    engineer takes it to a firewall team who correctly report nothing is
    blocked, and the actual cause sits in DNS untouched.
    """
    print("\na private address is the evidence that DNS, not the firewall, is at fault")
    check("a lab sinkhole address is private",
          probes.addresses_are_private(["10.10.10.10"]), True)
    check("loopback is private", probes.addresses_are_private(["127.0.0.1"]), True)
    check("link-local is private", probes.addresses_are_private(["169.254.1.1"]), True)
    check("a public address is not",
          probes.addresses_are_private(["66.22.36.191"]), False)
    # All-or-nothing: one private answer among public ones is a CDN mid-change,
    # not a broken resolver.
    check("a mix is not treated as an override",
          probes.addresses_are_private(["10.10.10.10", "66.22.36.191"]), False)
    check("nothing resolved is not an override",
          probes.addresses_are_private([]), False)
    check("a hostname that is not an IP is not an override",
          probes.addresses_are_private(["not-an-ip"]), False)

    dns_private = {"stage": "dns", "status": OK, "detail": "10.10.10.10", "ms": 2,
                   "addresses": ["10.10.10.10"], "private_only": True}
    out = probes.summarize([dns_private, stage("tcp", CRIT, "timed out")], True)
    check("the fault is attributed to DNS", out["failed_stage"], "dns")
    check("...and the headline says so", "PRIVATE address" in out["headline"], True)
    check("...and sends them to DNS before the firewall",
          "before the firewall" in out["headline"], True)

    # The same failure with a PUBLIC address must still blame the firewall,
    # or the fix would simply move the misdiagnosis somewhere else.
    dns_public = {"stage": "dns", "status": OK, "detail": "66.22.36.191", "ms": 2,
                  "addresses": ["66.22.36.191"], "private_only": False}
    out = probes.summarize([dns_public, stage("tcp", CRIT, "timed out")], True)
    check("a public address still points at the firewall", out["failed_stage"], "tcp")
    check("...and does not mention DNS overrides",
          "PRIVATE address" in out["headline"], False)


def test_private_address_that_works_is_not_a_fault():
    """An internal mirror is a legitimate arrangement, not a problem. But the
    engineer still has to be told, or they will believe they are talking to
    Radware when they are talking to something inside the estate."""
    print("\na private address that answers is a mirror, not a fault")
    stages = [
        {"stage": "dns", "status": OK, "detail": "10.1.1.5", "ms": 1,
         "addresses": ["10.1.1.5"], "private_only": True},
        stage("tcp", OK), stage("tls", OK), stage("http", OK),
    ]
    out = probes.summarize(stages, critical=True)
    check("it is not a failure", out["severity"], OK)
    check("...but the private address is called out",
          "PRIVATE address" in out["headline"], True)
    check("...and flagged for the UI", out.get("dns_override"), True)


def test_remote_probe_parsing():
    """Parsing what the CC itself reported.

    This path exists because of a real misreading: standalone, the tool probed
    the ENGINEER'S LAPTOP and showed a green tick for services.radware.com
    while the CC being debugged resolved that name to a sinkhole. The laptop's
    answer was correct and irrelevant. Probing the appliance is the only way to
    answer the question actually being asked, and this parser turns the
    appliance's answer back into the same stages the local probe produces — so
    the rules that decide what a failure MEANS are shared, not forked.
    """
    from modules.diag import targets
    t = targets.get("radware-services")
    tf = targets.get("radware-ti-feed")
    print("\nparsing a probe that ran on the CC")

    def out_of(*lines):
        """The three lines net.probe emits, assembled without escapes."""
        return "\n".join(lines) + "\n"

    # The lab CC, exactly as it answers today.
    st = probes.parse_remote_probe(
        out_of("DNS 10.10.10.10", "TCP fail", "HTTP 000 7"), t)
    out = probes.summarize(st, t.critical)
    check("the sinkhole is caught remotely too", out["failed_stage"], "dns")
    check("...and named", "10.10.10.10" in out["headline"], True)

    st = probes.parse_remote_probe(
        out_of("DNS 66.22.15.208", "TCP ok", "HTTP 200 0"), t)
    check("a healthy CC reports reachable",
          probes.summarize(st, t.critical)["severity"], OK)

    # The 403 rule has to hold on this path too, or the two vantage points
    # would disagree about the same service.
    st = probes.parse_remote_probe(
        out_of("DNS 16.15.191.134", "TCP ok", "HTTP 403 0"), tf)
    check("403 from the feed bucket is still reachable",
          probes.summarize(st, tf.critical)["severity"], OK)

    # curl's exit code carries what the HTTP status cannot: a certificate
    # failure and a refused connection both yield no status at all.
    st = probes.parse_remote_probe(
        out_of("DNS 66.22.15.208", "TCP ok", "HTTP 000 60"), t)
    check("a certificate failure is a TLS failure",
          probes.summarize(st, t.critical)["failed_stage"], "tls")
    st = probes.parse_remote_probe(
        out_of("DNS 66.22.15.208", "TCP ok", "HTTP 000 35"), t)
    check("a handshake failure is a TLS failure",
          probes.summarize(st, t.critical)["failed_stage"], "tls")

    st = probes.parse_remote_probe(
        out_of("DNS -", "TCP fail", "HTTP 000 6"), t)
    check("a name that does not resolve on the CC",
          probes.summarize(st, t.critical)["failed_stage"], "dns")

    # Garbage in must not become a confident verdict. An agent that answered
    # oddly must degrade to "could not check", never to "reachable".
    for junk in ("", "nonsense", out_of("DNS", "TCP", "HTTP")):
        st = probes.parse_remote_probe(junk, t)
        sev = probes.summarize(st, t.critical)["severity"]
        check(f"unparseable output {junk[:12]!r} never reports ok", sev != OK, True)


def test_roll_up():
    print("\nthe global verdict is the worst of them")
    ok = [{"severity": OK, "label": "A"}, {"severity": OK, "label": "B"}]
    check("all clear", probes.roll_up(ok)["severity"], OK)
    check("...and counts them", "2 destinations" in probes.roll_up(ok)["headline"], True)

    mixed = [{"severity": OK, "label": "A"}, {"severity": CRIT, "label": "Feed"}]
    out = probes.roll_up(mixed)
    check("one failure decides the verdict", out["severity"], CRIT)
    check("...and the headline names it", "Feed" in out["headline"], True)

    unknown = [{"severity": OK, "label": "A"}, {"severity": UNKNOWN, "label": "B"}]
    check("unknown outranks ok", probes.roll_up(unknown)["severity"], UNKNOWN)
    check("nothing checked is unknown", probes.roll_up([])["severity"], UNKNOWN)


# ── Proxy resolution ─────────────────────────────────────────────────────────

def test_proxy_resolution():
    """A CC in a customer data centre usually egresses through a proxy. A probe
    that ignored it would report failure while the real feed works — the most
    expensive wrong answer available, because it looks authoritative."""
    print("\nproxy configuration is honoured")
    host = "services.radware.com"

    check("no environment means no proxy",
          probes.proxy_for(host, {})["in_use"], False)

    check("https_proxy is picked up",
          probes.proxy_for(host, {"https_proxy": "http://p:3128"})["in_use"], True)
    check("HTTPS_PROXY works too",
          probes.proxy_for(host, {"HTTPS_PROXY": "http://p:3128"})["in_use"], True)
    check("http_proxy is the fallback",
          probes.proxy_for(host, {"http_proxy": "http://p:3128"})["url"],
          "http://p:3128")
    check("https_proxy wins over http_proxy",
          probes.proxy_for(host, {"http_proxy": "http://a", "https_proxy": "http://b"})["url"],
          "http://b")

    env = {"https_proxy": "http://p:3128", "no_proxy": "radware.com"}
    check("a no_proxy suffix exempts the host",
          probes.proxy_for(host, env)["in_use"], False)
    check("...and says which rule matched",
          "radware.com" in probes.proxy_for(host, env)["reason"], True)

    check("an exact no_proxy entry exempts it",
          probes.proxy_for(host, {"https_proxy": "http://p",
                                  "no_proxy": host})["in_use"], False)
    check("a wildcard exempts everything",
          probes.proxy_for(host, {"https_proxy": "http://p",
                                  "no_proxy": "*"})["in_use"], False)
    check("an unrelated no_proxy entry does not exempt",
          probes.proxy_for(host, {"https_proxy": "http://p",
                                  "no_proxy": "example.com"})["in_use"], True)


def test_targets_are_a_fixed_table():
    """The endpoints resolve an ID against this table and never accept a host
    and port. Without that this screen would be a network scanner running
    inside a customer's data centre."""
    print("\nthe target table is fixed and self-describing")
    from modules.diag import targets
    check("there are targets", len(targets.TARGETS) > 0, True)
    check("an unknown id resolves to nothing", targets.get("../etc/passwd"), None)
    check("an empty id resolves to nothing", targets.get(""), None)
    for t in targets.TARGETS:
        check(f"{t.id} explains why it matters", bool(t.purpose.strip()), True)
        check(f"{t.id} has a real host", "." in t.host, True)


def main() -> int:
    test_http_verdict()
    test_summarize_success()
    test_summarize_names_the_layer()
    test_non_critical_is_downgraded()
    test_unknown_never_becomes_ok()
    test_dns_override_is_detected()
    test_remote_probe_parsing()
    test_private_address_that_works_is_not_a_fault()
    test_roll_up()
    test_proxy_resolution()
    test_targets_are_a_fixed_table()

    print("\n" + "=" * 66)
    if _FAILURES:
        print("FAILURES:")
        for f in _FAILURES:
            print(" - " + f)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
