"""Tests for modules/es/discovery.unsliced_live_example.

The bug this pins: the possible-indices picker showed `dp-https-server` as
"new" on a CC where that index existed. Most CC indices are named
`<family>-ty-<type>-sid-<n>-sl-<n>` and are recognised by _LIVE_NAME_RE, but
the twelve families in UNSLICED are not named that way at all — `dp-https-server`
IS the whole index name. They therefore never matched, never received a
live_example, and every one of them was labelled "new" while sitting in the
cluster.

That column is the one an engineer reads to tell an index that is MISSING from
one that was never expected on this machine, so a false "new" is not cosmetic:
it invites someone to create an index that is already there.

    python tests/test_discovery_live_match.py
    pytest tests/test_discovery_live_match.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.es.discovery import unsliced_live_example      # noqa: E402

_FAILURES: list[str] = []

# A slice of what /_cat/indices returns on a CC: the unsliced definition-style
# indices alongside the ordinary sliced ones. Creation dates ascend so "newest
# wins" is actually exercised rather than accidentally satisfied by ordering.
LIVE = [
    ("dp-https-server", 1_700_000_000_000),
    ("dp-auth-table-status", 1_700_000_001_000),
    ("user-activity-log", 1_700_000_002_000),
    ("appconfig2", 1_700_000_003_000),
    ("dp-attack-raw-ty-anomalies-sid-0-sl-1459", 1_700_000_004_000),
    ("dp-attack-raw-ty-anomalies-sid-0-sl-1460", 1_700_000_005_000),
    ("detection-engine-baseline-hourly-ty-detection-engine-baseline-hourly-sid-0-sl-2954",
     1_700_000_006_000),
]


def check(got, want, note):
    if got == want:
        print(f"  ok  {note}  -> {got!r}")
    else:
        _FAILURES.append(f"{note}: expected {want!r}, got {got!r}")
        print(f"  BAD {note}  -> {got!r} (expected {want!r})")


def test_the_reported_bug():
    print("\nthe reported bug: dp-https-server existed but read as new")
    check(unsliced_live_example("dp-https-server", "dp-https-server", LIVE),
          "dp-https-server", "exact family name is found")


def test_other_unsliced_families():
    print("\nthe other UNSLICED families have the same shape")
    for name in ("dp-auth-table-status", "user-activity-log"):
        check(unsliced_live_example(name, name, LIVE), name, name)


def test_absent_families_stay_new():
    print("\na family with no live index must still read as new")
    check(unsliced_live_example("rt-alert-def", "rt-alert-def", LIVE),
          None, "rt-alert-def is genuinely absent")
    check(unsliced_live_example("", "", LIVE), None, "empty pattern")
    check(unsliced_live_example("snapshot-definition", "", LIVE),
          None, "pattern with no match and no family")


def test_wildcard_patterns_pick_the_newest():
    """A template pattern with a wildcard should resolve to the NEWEST match —
    an example index the engineer can actually look at, not an arbitrary one."""
    print("\nwildcard patterns resolve to the newest match")
    check(unsliced_live_example("dp-attack-raw-*", "dp-attack-raw", LIVE),
          "dp-attack-raw-ty-anomalies-sid-0-sl-1460", "newest of two sliced indices")
    check(unsliced_live_example("appconfig*", "appconfig", LIVE),
          "appconfig2", "appconfig* finds appconfig2")


def test_family_beats_pattern():
    """When both could match, the exact family name wins: it is the name the
    catalog is keyed by, so it is the one the caller means."""
    print("\nan exact family name beats a looser pattern")
    check(unsliced_live_example("dp-*", "dp-https-server", LIVE),
          "dp-https-server", "exact family preferred over the glob")


def main() -> int:
    test_the_reported_bug()
    test_other_unsliced_families()
    test_absent_families_stay_new()
    test_wildcard_patterns_pick_the_newest()
    test_family_beats_pattern()
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
