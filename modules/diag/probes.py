"""Staged connectivity probes: DNS, TCP, TLS, HTTP — reported separately.

The staging IS the feature. "Cannot reach the feed" sends an engineer to argue
with a firewall team; "DNS resolved, TCP connected, TLS failed because the
certificate is not trusted" tells them it is the customer's TLS-inspecting
proxy and hands them the fix. So every stage records its own verdict, timing
and detail, and the first hard failure names the layer that broke.

Deliberately built on the standard library plus `requests`, which is already a
dependency. No new package enters an image that has to pass the CC pipeline's
SBOM and CVE rules for a diagnostic screen.

WHERE THIS RUNS. Inside the CC Admin container. That is worth stating on screen
rather than glossing, because the container's egress is not guaranteed to be
identical to the host's or to the service that actually fetches the feed. What
the result proves is "this container can/cannot reach X" — strong evidence,
short of proof, about the appliance as a whole. `summarize()` never claims more
than that.

The IO lives in `run_probe`; everything that DECIDES anything is a pure
function below it, so the rules that tell an engineer their network is fine can
be tested without a network.
"""

from __future__ import annotations

import logging
import os
import socket
import ssl
import time
from urllib.parse import urlparse

# Severity vocabulary shared with the System Health module: a check that could
# not run reports `unknown`, never `ok`.
OK, UNKNOWN, WARN, CRIT = "ok", "unknown", "warn", "crit"
_ORDER = {OK: 0, UNKNOWN: 1, WARN: 2, CRIT: 3}

STAGES = ("dns", "tcp", "tls", "http")

logger = logging.getLogger(__name__)


def _stage(name: str, status: str, detail: str = "", ms: int | None = None,
           **extra) -> dict:
    return {"stage": name, "status": status, "detail": detail, "ms": ms, **extra}


def addresses_are_private(addrs: list[str]) -> bool:
    """True when EVERY resolved address is non-public.

    A public Radware service that resolves to 10.x, 127.x, 169.254.x or a
    CGNAT range is not the public service. It is split-horizon DNS, a sinkhole,
    or a /etc/hosts override — and on a lab or customer box that is very often
    the entire root cause.

    This matters because of what the tool would otherwise say. The name
    resolves, so DNS "succeeded"; the connection then fails, so the probe
    blames the firewall and sends an engineer to the wrong team entirely. The
    resolved ADDRESS is the evidence that distinguishes the two, and it is
    already in hand by the time the TCP stage runs.

    All-or-nothing on purpose: a service behind a CDN can legitimately return a
    mix during a migration, and one private answer among public ones is not
    grounds for calling DNS broken.
    """
    import ipaddress
    if not addrs:
        return False
    for a in addrs:
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            return False
        if not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_unspecified):
            return False
    return True


# ── Proxy resolution ─────────────────────────────────────────────────────────

def proxy_for(host: str, env: dict | None = None) -> dict:
    """Which proxy applies to *host*, from the environment.

    A CC in a customer data centre usually egresses through a proxy, and a
    probe that ignored it would report a failure while the real feed works
    perfectly — the most expensive kind of wrong answer a diagnostic can give,
    because it sends people to fix a network that was never broken.
    """
    env = os.environ if env is None else env
    get = lambda *names: next(  # noqa: E731
        (env[n] for n in names if env.get(n)), "")
    no_proxy = get("no_proxy", "NO_PROXY")
    for rule in (r.strip() for r in no_proxy.split(",")):
        if not rule:
            continue
        if rule == "*" or host == rule or host.endswith("." + rule.lstrip(".")):
            return {"in_use": False, "url": "", "reason": f"matched no_proxy rule {rule!r}"}
    url = get("https_proxy", "HTTPS_PROXY") or get("http_proxy", "HTTP_PROXY")
    if not url:
        return {"in_use": False, "url": "", "reason": "no proxy configured"}
    return {"in_use": True, "url": url, "reason": "from the environment"}


# ── Interpretation (pure) ────────────────────────────────────────────────────

def http_verdict(status_code: int | None, critical: bool) -> tuple[str, str]:
    """Turn an HTTP status into a verdict about CONNECTIVITY, not about the URL.

    The distinction matters more than it looks. 401/403 from a feed bucket
    means the request arrived, was understood and was answered — connectivity
    is proven. Calling that a failure would send an engineer chasing a network
    problem that does not exist, which is exactly the outcome this screen is
    supposed to prevent.
    """
    if status_code is None:
        return UNKNOWN, "no HTTP response"
    if 200 <= status_code < 400:
        return OK, f"HTTP {status_code}"
    if status_code in (401, 403):
        return OK, (f"HTTP {status_code} — reached the service and it answered; "
                    "authentication is expected here and does not indicate a "
                    "connectivity problem")
    if status_code == 404:
        return OK, (f"HTTP {status_code} — the host answered, so the path is "
                    "wrong but the network is fine")
    if 500 <= status_code:
        return (WARN if critical else UNKNOWN,
                f"HTTP {status_code} — reachable, but the far end is failing")
    return WARN, f"HTTP {status_code}"


def summarize(stages: list[dict], critical: bool) -> dict:
    """Roll staged results into one verdict plus a headline an engineer can act on."""
    by_name = {s["stage"]: s for s in stages}
    failed = next((s for s in stages if s["status"] in (WARN, CRIT)), None)

    if failed is None:
        unknown = [s for s in stages if s["status"] == UNKNOWN]
        if unknown:
            return {"severity": UNKNOWN,
                    "headline": f"could not complete the {unknown[0]['stage'].upper()} check",
                    "failed_stage": unknown[0]["stage"]}
        dns = by_name.get("dns", {})
        if dns.get("private_only"):
            # Reached it, and it is not the public address. That is a
            # legitimate arrangement — an internal mirror or a proxy VIP — so
            # it is not a fault. It is still worth saying out loud, because
            # "reachable" alone would let an engineer conclude they are talking
            # to Radware when they are talking to something inside the estate.
            return {"severity": OK, "failed_stage": None,
                    "headline": "reachable, but at a PRIVATE address — this "
                                "appliance reaches this service through an "
                                "internal mirror or proxy, not directly",
                    "dns_override": True}
        return {"severity": OK, "headline": "reachable", "failed_stage": None}

    # A non-critical target that fails is worth showing and not worth alarming
    # about — nothing on the appliance depends on it.
    severity = failed["status"] if critical else UNKNOWN
    name = failed["stage"]
    headline = {
        "dns":  "name does not resolve — DNS or the resolver configuration",
        "tcp":  "resolves, but the connection is refused or times out — "
                "a firewall or a proxy in the path",
        "tls":  "connects, but the TLS handshake fails — commonly a "
                "TLS-inspecting proxy whose CA this appliance does not trust",
        "http": "connects and negotiates TLS, but the request fails",
    }.get(name, f"{name} failed")
    # The correction that matters most. When the name resolved only to private
    # addresses and the connection then failed, the fault is DNS, not the
    # firewall — and blaming the firewall costs an engineer a conversation with
    # a team who will correctly tell them nothing is blocked.
    dns = by_name.get("dns", {})
    if name in ("tcp", "tls", "http") and dns.get("private_only"):
        addrs = ", ".join(dns.get("addresses", [])[:3])
        headline = (f"the name resolves to a PRIVATE address ({addrs}) and the "
                    "connection there fails — this box's DNS is answering with "
                    "an override or sinkhole rather than the public service. "
                    "Check DNS and /etc/hosts before the firewall.")
        return {"severity": severity, "headline": headline,
                "failed_stage": "dns", "detail": failed.get("detail", ""),
                "dns_override": True}

    if name == "http" and failed.get("trust_mismatch"):
        headline = ("reachable, but its certificate is not trusted by every "
                    "trust store on this box — a TLS-inspecting proxy whose CA "
                    "is only half installed")
    return {"severity": severity, "headline": headline, "failed_stage": name,
            "detail": failed.get("detail", ""),
            "reached": [s["stage"] for s in stages
                        if s["status"] == OK and _ORDER[s["status"]] == 0
                        and STAGES.index(s["stage"]) < STAGES.index(name)]}


def roll_up(results: list[dict]) -> dict:
    """Worst-of across every target, matching the System Health convention."""
    if not results:
        return {"severity": UNKNOWN, "headline": "nothing was checked"}
    worst = max((r.get("severity", UNKNOWN) for r in results), key=lambda s: _ORDER[s])
    bad = [r for r in results if r.get("severity") in (WARN, CRIT)]
    unknown = [r for r in results if r.get("severity") == UNKNOWN]
    if worst == OK:
        return {"severity": OK,
                "headline": f"all {len(results)} destinations reachable"}
    if bad:
        # Word it by SEVERITY. A trust-store mismatch is a `warn` on a target
        # the probe demonstrably reached, so "cannot reach" would contradict the
        # card directly underneath it — and a summary that disagrees with its
        # own detail teaches an engineer to distrust the whole screen.
        unreachable = [r for r in bad if r.get("severity") == CRIT]
        degraded = [r for r in bad if r.get("severity") == WARN]
        parts = []
        if unreachable:
            names = ", ".join(r["label"] for r in unreachable[:2])
            more = f" and {len(unreachable) - 2} more" if len(unreachable) > 2 else ""
            parts.append(f"cannot reach {names}{more}")
        if degraded:
            names = ", ".join(r["label"] for r in degraded[:2])
            more = f" and {len(degraded) - 2} more" if len(degraded) > 2 else ""
            parts.append(f"reached {names}{more} with a problem")
        return {"severity": worst, "headline": "; ".join(parts)}
    return {"severity": UNKNOWN,
            "headline": f"{len(unknown)} destination(s) could not be checked"}


# ── Probing from the CC itself ───────────────────────────────────────────────
# The vantage point that actually answers the question. Running the probe in
# this process answers "can the machine running CC Admin reach X" — which
# STANDALONE is the engineer's own laptop, and is very nearly the wrong
# question: a laptop on the open internet will happily report a service as
# reachable while the customer's appliance cannot resolve it at all. That is
# not a hypothetical; it is what the lab CC does with services.radware.com.
#
# So when there is a way to run commands on the CC, the probe runs THERE and
# this parser turns the result back into the same stage structure the in-process
# probe produces. One set of interpretation rules, two vantage points — the
# rules that decide what a failure MEANS must not fork, or the two paths will
# drift and only one of them will be tested.

# curl exit codes worth naming. The HTTP status cannot express these: a
# certificate failure and a refused connection both yield no status at all.
_CURL_ERRORS = {
    5:  ("tcp",  "could not resolve the proxy"),
    6:  ("dns",  "could not resolve the host"),
    7:  ("tcp",  "could not connect — refused, or no route"),
    28: ("tcp",  "timed out"),
    35: ("tls",  "TLS handshake failed"),
    51: ("tls",  "the certificate did not match the host name"),
    60: ("tls",  "certificate not trusted — commonly a TLS-inspecting proxy "
                 "whose CA this appliance does not have"),
}


def parse_remote_probe(text: str, target) -> list[dict]:
    """Turn `net.probe` output into stages. Pure, and therefore tested."""
    dns_line = tcp_line = http_line = ""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith("DNS "):
            dns_line = line[4:].strip()
        elif line.startswith("TCP "):
            tcp_line = line[4:].strip()
        elif line.startswith("HTTP "):
            http_line = line[5:].strip()

    stages: list[dict] = []

    # DNS
    if not dns_line or dns_line == "-":
        stages.append(_stage("dns", CRIT, "the name did not resolve on this CC"))
        return stages
    addrs = [a for a in dns_line.split(",") if a]
    stages.append(_stage("dns", OK, ", ".join(addrs[:4]), None,
                         addresses=addrs,
                         private_only=addresses_are_private(addrs)))

    # TCP
    if tcp_line != "ok":
        stages.append(_stage("tcp", CRIT,
                             f"no connection to port {target.port} from this CC"))
        return stages
    stages.append(_stage("tcp", OK, f"connected to port {target.port}"))

    # TLS + HTTP, both carried by curl
    parts = http_line.split()
    code = int(parts[0]) if parts and parts[0].isdigit() else 0
    exit_code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0

    if exit_code in _CURL_ERRORS:
        stage_name, why = _CURL_ERRORS[exit_code]
        if stage_name == "tls":
            stages.append(_stage("tls", CRIT, why))
            return stages
        # A DNS or TCP error surfacing only now contradicts the stages above,
        # which usually means something intercepted the connection. Report it
        # against HTTP rather than rewriting an earlier stage that genuinely
        # succeeded.
        stages.append(_stage("tls", UNKNOWN, "not reached"))
        stages.append(_stage("http", CRIT if target.critical else UNKNOWN, why))
        return stages

    stages.append(_stage("tls", OK, "handshake completed"))
    if code == 0:
        stages.append(_stage("http", CRIT if target.critical else UNKNOWN,
                             f"no HTTP response (curl exit {exit_code})"))
    else:
        status, detail = http_verdict(code, target.critical)
        stages.append(_stage("http", status, detail, status_code=code))
    return stages


def run_probe_on_cc(target) -> dict | None:
    """Probe *target* from the CC itself. None when that is not possible."""
    from core import hostexec
    try:
        out = hostexec.run_op("net.probe", host=target.host, port=target.port)
    except Exception as exc:                                   # noqa: BLE001
        logger.info("[diag] cannot probe from the CC: %s", exc)
        return None
    if out.get("rc") not in (0, None):
        logger.info("[diag] net.probe on the CC returned rc=%s", out.get("rc"))
    stages = parse_remote_probe(out.get("stdout", ""), target)
    result = _result(target, stages, {"in_use": False, "url": "",
                                      "reason": "probed on the CC"})
    result["vantage"] = "cc"
    return result


# ── The probe itself (IO) ────────────────────────────────────────────────────

def run_probe(target, timeout: float = 5.0) -> dict:
    """Probe one target through DNS → TCP → TLS → HTTP, stopping at a hard fail."""
    stages: list[dict] = []
    proxy = proxy_for(target.host)

    # Through a proxy the DNS and TCP stages describe the PROXY, not the
    # destination, so running them would produce a confident answer about the
    # wrong machine. Say so instead.
    if proxy["in_use"]:
        for name in ("dns", "tcp", "tls"):
            stages.append(_stage(name, UNKNOWN,
                                 "skipped: egress goes through a proxy, so this "
                                 "stage would describe the proxy rather than "
                                 f"{target.host}"))
        stages.append(_http_stage(target, timeout, proxy))
        return _result(target, stages, proxy)

    # 1. DNS
    t0 = time.time()
    try:
        infos = socket.getaddrinfo(target.host, target.port, proto=socket.IPPROTO_TCP)
        addrs = sorted({i[4][0] for i in infos})
        private = addresses_are_private(addrs)
        stages.append(_stage("dns", OK, ", ".join(addrs[:4]),
                             int((time.time() - t0) * 1000),
                             addresses=addrs, private_only=private))
    except socket.gaierror as exc:
        stages.append(_stage("dns", CRIT, f"{exc}", int((time.time() - t0) * 1000)))
        return _result(target, stages, proxy)
    except Exception as exc:                                   # noqa: BLE001
        stages.append(_stage("dns", UNKNOWN, f"{exc}"))
        return _result(target, stages, proxy)

    # 2. TCP
    t0 = time.time()
    sock = None
    try:
        sock = socket.create_connection((target.host, target.port), timeout=timeout)
        stages.append(_stage("tcp", OK, f"connected to port {target.port}",
                             int((time.time() - t0) * 1000)))
    except (socket.timeout, TimeoutError):
        stages.append(_stage("tcp", CRIT,
                             f"timed out after {timeout:g}s — typically a "
                             "firewall dropping the packets rather than "
                             "refusing them", int((time.time() - t0) * 1000)))
        return _result(target, stages, proxy)
    except OSError as exc:
        stages.append(_stage("tcp", CRIT, f"{exc}", int((time.time() - t0) * 1000)))
        return _result(target, stages, proxy)

    # 3. TLS
    try:
        if target.scheme == "https":
            t0 = time.time()
            ctx = ssl.create_default_context()
            try:
                with ctx.wrap_socket(sock, server_hostname=target.host) as tls:
                    cert = tls.getpeercert() or {}
                    issuer = _rdn(cert.get("issuer"))
                    expires = cert.get("notAfter", "")
                    stages.append(_stage(
                        "tls", OK,
                        f"{tls.version()} · issued by {issuer or 'unknown'}",
                        int((time.time() - t0) * 1000),
                        issuer=issuer, expires=expires))
                sock = None                       # closed by the context manager
            except ssl.SSLCertVerificationError as exc:
                stages.append(_stage(
                    "tls", CRIT,
                    f"certificate not trusted: {exc.verify_message or exc}. "
                    "On an appliance behind a TLS-inspecting proxy this is "
                    "expected, and the fix is to trust the proxy's CA rather "
                    "than to open the firewall.",
                    int((time.time() - t0) * 1000)))
                return _result(target, stages, proxy)
            except ssl.SSLError as exc:
                stages.append(_stage("tls", CRIT, f"{exc}",
                                     int((time.time() - t0) * 1000)))
                return _result(target, stages, proxy)
        else:
            stages.append(_stage("tls", UNKNOWN, "not an HTTPS target"))
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    # 4. HTTP — told what the TLS stage concluded, so a disagreement between
    # the two trust stores can be named rather than reported twice, differently.
    tls = next((s for s in stages if s["stage"] == "tls"), {})
    stages.append(_http_stage(target, timeout, proxy,
                              tls_trusted=tls.get("status") == OK,
                              tls_issuer=tls.get("issuer", "")))
    return _result(target, stages, proxy)


def _http_stage(target, timeout: float, proxy: dict,
                tls_trusted: bool = False, tls_issuer: str = "") -> dict:
    """The HTTP request, plus the one disagreement worth calling out by name.

    The TLS stage above verifies against the OS trust store; `requests` verifies
    against certifi's bundled one. On an appliance behind a TLS-INSPECTING
    PROXY those two routinely disagree — the OS has been taught to trust the
    proxy's CA and certifi has not — and the raw result is a screen saying TLS
    is fine on one line and untrusted on the next, which is worse than either
    answer alone.

    That disagreement is not noise, it is the diagnosis: the network path works
    (the handshake completed), and what is missing is trust configuration. It
    is also a warning about the REST of the appliance, because every service
    with its own trust store — the JVMs especially — faces the same question
    independently. So it is reported as its own finding, at warn rather than
    crit, since connectivity itself is proven.
    """
    import requests
    url = f"{target.scheme}://{target.host}{target.path}"
    t0 = time.time()
    try:
        resp = requests.head(url, timeout=timeout, allow_redirects=True)
        # Some hosts refuse HEAD outright; that is about the verb, not the path.
        if resp.status_code in (405, 501):
            resp = requests.get(url, timeout=timeout, stream=True,
                                allow_redirects=True)
            resp.close()
        status, detail = http_verdict(resp.status_code, target.critical)
        return _stage("http", status, detail, int((time.time() - t0) * 1000),
                      status_code=resp.status_code)
    except requests.exceptions.SSLError as exc:
        ms = int((time.time() - t0) * 1000)
        if tls_trusted:
            who = f" ({tls_issuer})" if tls_issuer else ""
            return _stage(
                "http", WARN if target.critical else UNKNOWN,
                "certificate TRUST STORE mismatch: the handshake above "
                f"succeeded against this system's trust store{who}, but the "
                "HTTP client's own CA bundle rejects the same certificate. "
                "The network path works — this is a trust configuration gap, "
                "and every service here with its own trust store (the JVMs in "
                "particular) will hit it separately.",
                ms, trust_mismatch=True)
        return _stage("http", CRIT if target.critical else UNKNOWN,
                      f"TLS verification failed: {exc}", ms)
    except Exception as exc:                                   # noqa: BLE001
        status, _ = http_verdict(None, target.critical)
        return _stage("http", CRIT if target.critical else UNKNOWN,
                      f"{type(exc).__name__}: {exc}",
                      int((time.time() - t0) * 1000))


def _rdn(issuer) -> str:
    """Pull the common or organisation name out of a certificate issuer tuple."""
    if not issuer:
        return ""
    flat = {k: v for rdn in issuer for k, v in rdn}
    return flat.get("commonName") or flat.get("organizationName") or ""


def _result(target, stages: list[dict], proxy: dict) -> dict:
    summary = summarize(stages, target.critical)
    return {
        "id": target.id,
        "label": target.label,
        "host": target.host,
        "port": target.port,
        "purpose": target.purpose,
        "caveat": target.caveat,
        "sources": list(target.sources),
        "critical": target.critical,
        "proxy": proxy,
        "stages": stages,
        **summary,
    }
