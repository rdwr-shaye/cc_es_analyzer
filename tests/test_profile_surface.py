"""Tests that the two shipping profiles expose exactly the endpoints they should.

This is the regression guard for the failure nobody would notice in review: the
standalone and embedded builds come from ONE image and differ only by which
routers main.py registers, so an endpoint added to the wrong router is not a
crash, a warning, or a failing request — it is a customer's appliance quietly
carrying a capability that was never meant to reach it. The first symptom would
be someone finding it.

Two things are checked, and the second is the one with teeth:

  1. The endpoint surface of each profile matches the table below. Adding an
     endpoint therefore requires saying, here, which profiles get it.

  2. Every declared capability actually GOVERNS a router. A capability that
     names no router is decorative: /api/policy reports a boundary that route
     registration does not keep, the UI dutifully greys out a button, and the
     endpoint answers anyway. That is strictly worse than having no capability,
     because the registry then misleads the person auditing it.

Each profile is resolved in its OWN subprocess. core/policy.py caches the
resolution at startup — deliberately, so a capability cannot change under a
running request — which means one process cannot honestly answer for both.

    python tests/test_profile_surface.py
    pytest tests/test_profile_surface.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_FAILURES: list[str] = []


# ── The table ────────────────────────────────────────────────────────────────
# Endpoints that must exist in STANDALONE and must NOT exist in EMBEDDED.
# Everything else is expected in both. The reason is recorded with each entry
# because "why is this not on a customer's CC" is the question a reviewer asks,
# and answering it here means answering it once.
STANDALONE_ONLY = {
    "/api/artificial":
        "fabricates documents outright — never on a customer's appliance",
    "/api/artificial/info/{index_name}":
        "describes what the fabricator would produce; same capability",
    "/api/indices/{index_name}/duplicate":
        "synthetic copy of real data — a reproduction tool",
    "/api/indices/create":
        "a CC builds its own indices from its templates, so creating one by "
        "hand there is a reproduction activity. DELETE /api/indices/{name} is "
        "deliberately NOT here — a corrupted index has to be removable by the "
        "engineer who found it, on a customer's box included",
    "/api/update/check":   "the embedded build upgrades with the CC",
    "/api/update/apply":   "the embedded build upgrades with the CC",
    "/api/update/status":  "the embedded build upgrades with the CC",
    "/api/update/job":     "the embedded build upgrades with the CC",
}

# Endpoints that must exist in BOTH, listed because their absence would be a
# silent loss of function rather than a security question. Not exhaustive —
# these are the ones whose gating changed and could plausibly regress.
IN_BOTH = [
    "/api/indices/possible",          # the point of the read-only catalog
    "/api/indices/{index_name}",      # delete a corrupted index
    "/api/indices/{index_name}/import",
    "/api/doc/update",
    "/api/docs/bulk-delete",
    "/api/docs/bulk-field",
    "/api/docs/bulk-update",
    "/api/exports",
    "/api/exports/restore",
    "/api/system/summary",
    "/api/system/containers/{name}/logs",
]

# Endpoints that must exist in NEITHER until someone deliberately unlocks the
# capability. These are the unlockable ones: off in every profile by default.
IN_NEITHER = [
    "/api/system/storage/delete",     # needs the property file AND --allow-delete
]


def _surface(profile: str) -> dict:
    """{paths, enabled} for one profile, resolved in a clean process."""
    code = (
        "import json, main\n"
        "from core import policy\n"
        "print('@@' + json.dumps({\n"
        "  'paths': sorted(main.app.openapi()['paths']),\n"
        "  'enabled': sorted(policy.state()['enabled']),\n"
        "}))\n"
    )
    env = dict(os.environ, ANALYZER_PROFILE=profile, PYTHONIOENCODING="utf-8")
    out = subprocess.run([sys.executable, "-c", code], cwd=_ROOT, env=env,
                         capture_output=True, text=True, encoding="utf-8")
    for line in (out.stdout or "").splitlines():
        if line.startswith("@@"):
            return json.loads(line[2:])
    raise SystemExit(f"could not resolve the {profile} profile:\n{out.stderr}")


def test_standalone_only(standalone: dict, embedded: dict) -> None:
    print("\nendpoints that must NOT reach a customer's CC")
    s, e = set(standalone["paths"]), set(embedded["paths"])
    for path, why in sorted(STANDALONE_ONLY.items()):
        if path not in s:
            _FAILURES.append(f"{path} is missing from STANDALONE entirely")
            print(f"  BAD {path} — absent from standalone too")
        elif path in e:
            _FAILURES.append(f"{path} IS REGISTERED IN EMBEDDED — {why}")
            print(f"  BAD {path} — present in embedded")
        else:
            print(f"  ok  {path}")

    # The other direction: embedded must never carry something standalone lacks.
    extra = e - s
    if extra:
        _FAILURES.append(f"embedded exposes endpoints standalone does not: {sorted(extra)}")
        print(f"  BAD embedded-only endpoints: {sorted(extra)}")
    else:
        print("  ok  embedded exposes nothing standalone does not")

    # And the difference must be EXACTLY the table — an endpoint dropped from
    # embedded for an undocumented reason is as much a surprise as one added.
    undocumented = (s - e) - set(STANDALONE_ONLY)
    if undocumented:
        _FAILURES.append(
            "standalone-only endpoints missing from the table in this file: "
            f"{sorted(undocumented)} — add them with the reason they are held back")
        print(f"  BAD undocumented standalone-only: {sorted(undocumented)}")
    else:
        print("  ok  the standalone/embedded difference is exactly the table")


def test_present_in_both(standalone: dict, embedded: dict) -> None:
    print("\nendpoints both profiles must keep")
    for path in IN_BOTH:
        missing = [n for n, d in (("standalone", standalone), ("embedded", embedded))
                   if path not in set(d["paths"])]
        if missing:
            _FAILURES.append(f"{path} is missing from: {', '.join(missing)}")
            print(f"  BAD {path} — missing from {', '.join(missing)}")
        else:
            print(f"  ok  {path}")


def test_present_in_neither(standalone: dict, embedded: dict) -> None:
    print("\nendpoints that stay unregistered until deliberately unlocked")
    for path in IN_NEITHER:
        present = [n for n, d in (("standalone", standalone), ("embedded", embedded))
                   if path in set(d["paths"])]
        if present:
            _FAILURES.append(f"{path} is registered by default in: {', '.join(present)}")
            print(f"  BAD {path} — registered in {', '.join(present)}")
        else:
            print(f"  ok  {path}")


def test_every_capability_governs_a_router() -> None:
    """The check with teeth. See the module docstring."""
    print("\nevery declared capability controls at least one router")
    import modules
    from core import policy

    governed: set[str] = set()
    declared: dict[str, str] = {}
    for module in modules.discover():
        for cap in module.capabilities:
            declared[cap.id] = module.id
        for _router, gating in module.routers:
            if gating:
                governed.add(gating)
    for cap in policy.CORE_CAPABILITIES:
        declared[cap.id] = "core"
    governed.add("app.self_update")      # main.py registers core/routers/update.py

    # Capabilities that are allowed to govern no router, each for a stated
    # reason. This list is the point of the test: the day a route appears for
    # one of these, deleting its line here is part of that change.
    ungoverned_on_purpose = {
        # Declared so /api/policy can explain a disabled control. Not built.
        "system.es.delete_index": "not implemented yet",
        "system.maria.repair":    "not implemented yet",
        "system.maria.recreate":  "not implemented yet",
        # Whole-module reads. If one of these is off, discover() drops the
        # entire module rather than registering a console for a store you
        # cannot read, so there is no separate router to gate.
        "es.read":               "gates the whole es module",
        "maria.read":            "gates the whole maria module",
        # Standalone-only by profile, and what it controls is a SCREEN plus the
        # meaning of ES_HOST, not a distinct set of routes.
        "es.connect":            "controls the connection screen, not a router",
    }

    for cap_id in sorted(declared):
        if cap_id in governed:
            print(f"  ok  {cap_id}")
        elif cap_id in ungoverned_on_purpose:
            print(f"  --  {cap_id} ({ungoverned_on_purpose[cap_id]})")
        else:
            _FAILURES.append(
                f"{cap_id} is declared by module '{declared[cap_id]}' but gates no "
                "router: /api/policy would report a boundary that route "
                "registration does not keep. Either register it against its "
                "router, or add it to ungoverned_on_purpose with the reason")
            print(f"  BAD {cap_id} — governs nothing")


def main() -> int:
    print("resolving both profiles in separate processes…")
    standalone = _surface("standalone")
    embedded = _surface("embedded")
    print(f"  standalone: {len(standalone['paths'])} paths, "
          f"{len(standalone['enabled'])} capabilities")
    print(f"  embedded:   {len(embedded['paths'])} paths, "
          f"{len(embedded['enabled'])} capabilities")

    test_standalone_only(standalone, embedded)
    test_present_in_both(standalone, embedded)
    test_present_in_neither(standalone, embedded)
    test_every_capability_governs_a_router()

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
