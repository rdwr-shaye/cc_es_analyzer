"""The time box: this container runs for an hour, then stops itself.

Embedded, CC Admin is not a service that should sit running on a customer's
appliance forever. It reads every datastore on the box, so the safe default is
that it is OFF, and someone turns it on for as long as the job takes. Hence two
halves that only make sense together:

  * the container does NOT restart on its own — `restart: "no"` in the
    monitoring compose — so nothing brings it back but a deliberate
    `docker start cc-admin` from outside this service;
  * it stops itself when the time box expires.

HOW IT STOPS. By exiting its own process. That is the whole mechanism, and the
reason it is worth stating: the obvious alternative is to ask the host to stop
the container, which would mean a new operation in deploy/host_agent.py and a
container that can act on the appliance's docker. This needs neither. The app
raises SIGTERM against itself, uvicorn shuts down its workers cleanly, PID 1
exits, and docker records a normal exit — no socket, no allowlist entry, no new
privilege anywhere.

WORK IN PROGRESS IS NOT KILLED. If a job is still running when the clock
expires, the container does not stop mid-export. It enters a "stopping when the
current work finishes" state, says so on screen, and keeps the Extend control
live throughout — so an engineer who looks up during a long archive can still
decide to keep the box open. Only when the last job finishes does it exit.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time

from config import settings

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_started_at = 0.0
_expires_at = 0.0
_extensions = 0
_draining = False          # expired, but waiting for running work to finish
_stopping = False          # the exit has been signalled; nothing cancels it
_thread: threading.Thread | None = None


def enabled() -> bool:
    """Whether the time box applies. Off when the window is zero or negative.

    Standalone leaves it off: the app runs on the engineer's own machine and
    shutting their tool down under them every hour would be an act of
    hostility, not of security.
    """
    if settings.session_minutes <= 0:
        return False
    if settings.session_timebox is not None:
        return bool(settings.session_timebox)
    from core import policy
    return policy.profile() == policy.EMBEDDED


def _running_jobs() -> list[str]:
    """Descriptions of jobs that would be destroyed by exiting right now.

    Imported lazily and defensively: the job engine belongs to modules.es, and
    a profile without it must not stop the clock from working. A failure to
    look is treated as "nothing running" — the alternative is a container that
    refuses to stop because it could not read a job table.
    """
    try:
        from modules.es.routers.exports import _JOBS, _JOBS_LOCK
        with _JOBS_LOCK:
            return [f"{j.get('kind', 'job')}" for j in _JOBS.values()
                    if j.get("status") == "running"]
    except Exception:                                          # noqa: BLE001
        return []


def start() -> None:
    """Begin the countdown. Called once, at startup."""
    global _started_at, _expires_at, _thread
    if not enabled():
        logger.info("[lifecycle] no time box (session_minutes=%s, profile "
                    "override=%s)", settings.session_minutes,
                    settings.session_timebox)
        return
    with _lock:
        _started_at = time.time()
        _expires_at = _started_at + settings.session_minutes * 60
        if _thread is None:
            _thread = threading.Thread(target=_watch, name="lifecycle",
                                       daemon=True)
            _thread.start()
    logger.info("[lifecycle] this container will stop in %s minutes unless "
                "extended; restart it with `docker start cc-admin`",
                settings.session_minutes)


def extend(by_minutes: int | None = None) -> dict:
    """Push the deadline out. Returns the new state.

    Refused once the exit has been signalled: at that point the process is on
    its way down and telling the user they bought another hour would be a lie.
    """
    global _expires_at, _extensions, _draining
    minutes = by_minutes or settings.session_minutes
    with _lock:
        if _stopping:
            return dict(state(), extended=False,
                        error="this container is already shutting down")
        _expires_at = time.time() + minutes * 60
        _extensions += 1
        was_draining, _draining = _draining, False
    logger.info("[lifecycle] extended by %s minutes%s", minutes,
                " (was waiting for running work to finish)" if was_draining else "")
    return dict(state(), extended=True)


def state() -> dict:
    """What the UI needs to run the countdown and raise the prompt."""
    if not enabled():
        return {"enabled": False}
    now = time.time()
    left = max(0, int(_expires_at - now))
    jobs = _running_jobs() if (_draining or left <= settings.session_warn_minutes * 60) else []
    return {
        "enabled": True,
        "started_at": int(_started_at),
        "expires_at": int(_expires_at),
        "seconds_left": left,
        "warn_seconds": settings.session_warn_minutes * 60,
        # True once the UI should be asking. The server decides this rather
        # than the browser so every open tab agrees, and so a clock-skewed
        # laptop cannot miss the prompt entirely.
        "warning": left <= settings.session_warn_minutes * 60,
        "draining": _draining,
        "stopping": _stopping,
        "running_jobs": jobs,
        "extensions": _extensions,
        "window_minutes": settings.session_minutes,
    }


def _shutdown() -> None:
    """Signal ourselves and let uvicorn wind down."""
    global _stopping
    _stopping = True
    logger.warning("[lifecycle] time box expired — stopping. Start it again "
                   "with `docker start cc-admin`.")
    # SIGTERM, not os._exit: uvicorn traps it, finishes in-flight responses and
    # closes sockets, so the last request gets an answer instead of a reset
    # connection. On Windows there is no SIGTERM worth the name, so fall back.
    try:
        os.kill(os.getpid(), signal.SIGTERM)
    except (AttributeError, OSError):
        os._exit(0)


def _watch() -> None:
    global _draining
    while True:
        time.sleep(5)
        if _stopping:
            return
        with _lock:
            expired = time.time() >= _expires_at
        if not expired:
            continue

        jobs = _running_jobs()
        if jobs:
            if not _draining:
                _draining = True
                logger.info("[lifecycle] expired, but %s job(s) are still "
                            "running — will stop when they finish: %s",
                            len(jobs), ", ".join(jobs))
            continue                      # check again on the next tick

        _shutdown()
        return
