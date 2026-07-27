"""
Who is using the analyzer right now, and which CC each of them is working on.

There is no login: a browser is identified by a `cc_sid` cookie the middleware
issues on first contact. Around that we keep whatever we can observe — client
IP (honouring nginx's X-Forwarded-For), a reverse-DNS hostname resolved in the
background, the browser/OS from the User-Agent, and an optional display name
the user types in — purely so people sharing a CC can see who else is there.

Two things are built on the registry:
  * peers(): everyone else currently working against the same ES target, used
    to warn a user BEFORE they modify or delete anything;
  * notify(): a small per-session inbox the UI polls, so the other users on
    that CC are told when someone actually changes data.

All of this is in-memory and per-process (the app runs a single uvicorn
worker); a restart simply starts everyone fresh.
"""
import logging
import re
import socket
import threading
import time
import uuid
from collections import deque

from services.es_client import drop_session, session_target

logger = logging.getLogger(__name__)

COOKIE_NAME = "cc_sid"

# A session counts as "here" while it has talked to us in the last IDLE_S; the
# UI polls every few seconds, so this survives a slow page or a short pause.
IDLE_S = 90
# Dropped entirely (and its ES connection closed) after this much silence.
EVICT_S = 8 * 3600
INBOX_MAX = 50            # per-session notification backlog cap

_sessions: dict[str, dict] = {}
_lock = threading.Lock()

# ip -> hostname ("" = looked up, nothing found). Resolution runs off-request.
_host_cache: dict[str, str] = {}
_host_lock = threading.Lock()


# ── Client identification ────────────────────────────────────────────────────

def client_ip(request) -> str:
    """Real client IP: the first hop in X-Forwarded-For when we're behind the
    bundled nginx, else the socket peer."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    real = request.headers.get("x-real-ip", "")
    if real:
        return real.strip()
    return getattr(request.client, "host", "") or "unknown"


def _resolve_hostname(ip: str) -> None:
    """Reverse-DNS an IP into the cache. Runs in its own thread — a lookup can
    block for seconds and must never sit in the request path."""
    name = ""
    try:
        name = socket.gethostbyaddr(ip)[0]
    except Exception:
        name = ""
    with _host_lock:
        _host_cache[ip] = name


def hostname_for(ip: str) -> str:
    """Cached reverse-DNS name for *ip* ("" when unknown). The first call for a
    new IP kicks off a background lookup and returns "" immediately."""
    if not ip or ip == "unknown":
        return ""
    with _host_lock:
        if ip in _host_cache:
            return _host_cache[ip]
        _host_cache[ip] = ""          # mark in-flight so we only resolve once
    threading.Thread(target=_resolve_hostname, args=(ip,), daemon=True,
                     name=f"rdns-{ip}").start()
    return ""


_UA_BROWSERS = [
    ("Edg/", "Edge"), ("OPR/", "Opera"), ("Chrome/", "Chrome"),
    ("Firefox/", "Firefox"), ("Safari/", "Safari"), ("curl/", "curl"),
    ("python-requests", "python-requests"),
]
_UA_OS = [
    ("Windows NT 10", "Windows"), ("Windows", "Windows"), ("Mac OS X", "macOS"),
    ("Android", "Android"), ("Linux", "Linux"), ("iPhone", "iOS"),
]


def describe_agent(ua: str) -> str:
    """Coarse "Chrome on Windows" from a User-Agent string."""
    if not ua:
        return ""
    browser = next((label for token, label in _UA_BROWSERS if token in ua), "")
    if browser in ("Chrome", "Safari"):
        m = re.search(r"(?:Chrome|Version)/(\d+)", ua)
        if m:
            browser = f"{browser} {m.group(1)}"
    os_name = next((label for token, label in _UA_OS if token in ua), "")
    return " on ".join(p for p in (browser, os_name) if p) or ""


# ── Registry ─────────────────────────────────────────────────────────────────

def touch(sid: str | None, ip: str, user_agent: str) -> str:
    """Record activity for *sid* (minting one when absent). Returns the sid."""
    now = time.time()
    if not sid:
        sid = uuid.uuid4().hex
    with _lock:
        s = _sessions.get(sid)
        if s is None:
            s = _sessions[sid] = {
                "sid": sid, "first_seen": now, "name": "",
                "inbox": deque(maxlen=INBOX_MAX), "seq": 0,
            }
            logger.info("[presence] new session %s from %s (%s)",
                        sid[:8], ip, describe_agent(user_agent) or "unknown agent")
        s["ip"] = ip
        s["user_agent"] = user_agent
        s["last_seen"] = now
    return sid


def set_name(sid: str, name: str) -> None:
    with _lock:
        if sid in _sessions:
            _sessions[sid]["name"] = (name or "").strip()[:40]


def _label(s: dict) -> str:
    """How a user is shown to the others: their own name when they gave one,
    else the reverse-DNS host, else the bare IP."""
    if s.get("name"):
        return s["name"]
    host = hostname_for(s.get("ip", ""))
    return host or s.get("ip", "unknown")


def _public(s: dict, target: str | None) -> dict:
    ip = s.get("ip", "")
    return {
        "sid_short": s["sid"][:8],
        "label": _label(s),
        "name": s.get("name", ""),
        "ip": ip,
        "hostname": hostname_for(ip),
        "agent": describe_agent(s.get("user_agent", "")),
        "es_target": target,
        "idle_seconds": int(time.time() - s.get("last_seen", 0)),
        "since": int(s.get("first_seen", 0)),
    }


def _evict_locked(now: float) -> None:
    stale = []
    for k, v in _sessions.items():
        idle = now - v.get("last_seen", 0)
        # Cookie-less callers (curl, the standalone scripts, health probes) mint
        # a session per request and never connect anywhere — drop those quickly
        # so they can't pile up; real users are kept for the full EVICT_S.
        if idle > EVICT_S or (idle > 600 and session_target(k) is None):
            stale.append(k)
    for sid in stale:
        _sessions.pop(sid, None)
        drop_session(sid)


def snapshot(sid: str) -> dict:
    """{you, peers} — peers being everyone else active on the SAME ES target.
    Sessions that never connected anywhere have no target and match nobody."""
    now = time.time()
    with _lock:
        _evict_locked(now)
        me = _sessions.get(sid)
        others = [v for k, v in _sessions.items()
                  if k != sid and now - v.get("last_seen", 0) <= IDLE_S]
    if me is None:
        return {"you": None, "peers": []}
    my_target = session_target(sid)
    peers = []
    for o in others:
        t = session_target(o["sid"])
        if t and my_target and t == my_target:
            peers.append(_public(o, t))
    peers.sort(key=lambda p: p["idle_seconds"])
    return {"you": _public(me, my_target), "peers": peers}


def peers_on_target(target: str | None, exclude_sid: str) -> list[dict]:
    if not target:
        return []
    now = time.time()
    with _lock:
        others = [v for k, v in _sessions.items()
                  if k != exclude_sid and now - v.get("last_seen", 0) <= IDLE_S]
    return [_public(o, target) for o in others if session_target(o["sid"]) == target]


# ── Notifications ────────────────────────────────────────────────────────────

def notify_peers(sid: str, action: str, detail: str = "") -> int:
    """Tell everyone else on the actor's CC that they changed data. Returns how
    many sessions were notified."""
    target = session_target(sid)
    if not target:
        return 0
    with _lock:
        me = _sessions.get(sid)
        actor = _label(me) if me else "someone"
        now = time.time()
        recipients = [v for k, v in _sessions.items()
                      if k != sid and now - v.get("last_seen", 0) <= IDLE_S]
        sent = 0
        for r in recipients:
            if session_target(r["sid"]) != target:
                continue
            r["seq"] += 1
            r["inbox"].append({
                "id": r["seq"], "ts": now, "actor": actor,
                "actor_ip": (me or {}).get("ip", ""),
                "action": action, "detail": detail, "es_target": target,
            })
            sent += 1
    if sent:
        logger.info("[presence] %s on %s: %s %s -> notified %s user(s)",
                    actor, target, action, detail, sent)
    return sent


def describe_session(sid: str) -> str:
    """"name (ip, Chrome on Windows)" — for logs and for telling other users who
    did something."""
    with _lock:
        s = _sessions.get(sid)
        if not s:
            return "an unknown user"
        label, ip, agent = _label(s), s.get("ip", ""), describe_agent(s.get("user_agent", ""))
    extra = ", ".join(p for p in (ip if ip != label else "", agent) if p)
    return f"{label} ({extra})" if extra else label


def broadcast(action: str, detail: str = "", exclude_sid: str = "") -> int:
    """Notify EVERY active session, whatever CC they are on — for things that
    affect the whole service (an update restarting the app), not one cluster."""
    with _lock:
        me = _sessions.get(exclude_sid)
        actor = _label(me) if me else "someone"
        now = time.time()
        sent = 0
        for k, r in _sessions.items():
            if k == exclude_sid or now - r.get("last_seen", 0) > IDLE_S:
                continue
            r["seq"] += 1
            r["inbox"].append({
                "id": r["seq"], "ts": now, "actor": actor,
                "actor_ip": (me or {}).get("ip", ""),
                "action": action, "detail": detail, "es_target": None,
                "scope": "service",
            })
            sent += 1
    if sent:
        logger.info("[presence] broadcast from %s: %s %s -> %s user(s)",
                    actor, action, detail, sent)
    return sent


def drain(sid: str) -> list[dict]:
    """Take and clear a session's pending notifications."""
    with _lock:
        s = _sessions.get(sid)
        if not s:
            return []
        out = list(s["inbox"])
        s["inbox"].clear()
    return out
