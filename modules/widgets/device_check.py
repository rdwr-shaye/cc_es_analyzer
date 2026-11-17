"""Check 3, Tier A — the last polling-task result, from MariaDB.

Real signal about whether the underlying DefensePro device has been collected
from recently, without touching a device credential or calling a device
directly — that is Tier B (device_probe.py), a separate, explicitly-gated
capability. This tier is always on: it reads two tables (`device_access` for
the device's own identity, `polling_tasks` for the last collection result)
that already sit on a MariaDB instance CC Admin can already reach through
`modules.maria.client`, in the schema the product itself calls `vision_ng`.

NEVER select `https_username`, `https_password`, `cli_password`, `snmp_*`, or
any other credential column on `device_access` from this file — full stop.
That restriction is specific to this tier; `device_probe.py` is the one place
in this module allowed to touch those columns, and only once the
`widget.device_probe` capability is confirmed enabled by its own router.

`polling_tasks` (`PollingTaskState` in kvision_data_polling_scheduler,
composite PK `(deviceKey, executorType)` in Java) is keyed by the device's
IP — `DeviceTaskExecutor.getDeviceKey()` defaults to `dto.getIp()`, confirmed
via that class's own test fixtures using bare IP strings — so a straight join
against `device_access.mgt_ip` is the working assumption, confirmed live
against the lab CC (2026-09-09).

COLUMN NAMES ARE snake_case ON THE WIRE, not the Java field names. Confirmed
live via `SHOW COLUMNS FROM vision_ng.polling_tasks`: Hibernate's default
naming strategy renders `deviceKey` as `device_key`, `executorType` as
`executor_type`, and so on — the SQL below uses the real column names, not
the JPA field names the doc/source research used as shorthand.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

SUCCESS, FAIL, UNKNOWN, NO_DATA = "SUCCESS", "FAIL", "UNKNOWN", "NO_DATA"

_KNOWN_STATUSES = {"ENQUEUED", "STARTED", "IN_PROGRESS", "SUCCESS", "FAIL"}


def classify_task_status(row: dict | None) -> str:
    """Pure: the five `TaskStatus` values collapse to what a diagnosis needs.

    `None` (no row at all for this device+executor) is NO_DATA — itself a
    distinct, useful finding: this device has never been polled for this
    executor, as opposed to having been polled and failed. ENQUEUED/STARTED/
    IN_PROGRESS report UNKNOWN rather than a guess at the eventual outcome —
    a task mid-flight has not yet succeeded or failed.
    """
    if row is None:
        return NO_DATA
    status = (row.get("task_status") or "").strip().upper()
    if status == "SUCCESS":
        return SUCCESS
    if status == "FAIL":
        return FAIL
    if status in _KNOWN_STATUSES:
        return UNKNOWN
    return UNKNOWN


def list_devices() -> list[dict]:
    """DefensePro management IPs for the scope picker.

    Deliberately minimal: only `mgt_ip`. There is no device-type filter here
    because the `device` <-> `device_access` join (through the intermediate
    `device_setup` table) was not confirmed against source in this pass — see
    the plan's open items — so this lists every device this account can see
    rather than guessing a WHERE clause that might silently exclude rows.
    """
    from modules.maria import client
    from config import settings
    try:
        rows, _truncated = client.run(
            "SELECT mgt_ip FROM device_access "
            "WHERE mgt_ip IS NOT NULL AND mgt_ip != '' ORDER BY mgt_ip",
            schema=settings.widgets_vision_schema)
    except client.MariaError as exc:
        logger.info("[widgets] device list unavailable: %s", exc)
        return []
    return [{"mgt_ip": r["mgt_ip"]} for r in rows]


def polling_status(device_ips: list[str], executor_type: str) -> list[dict]:
    """One entry per requested device IP, including ones with no matching row
    (classify_task_status(None) -> NO_DATA), so a caller never has to treat
    "no row" as a separate case from "row present"."""
    if not device_ips:
        return []
    from modules.maria import client
    from config import settings

    placeholders = ", ".join(["%s"] * len(device_ips))
    try:
        rows, _truncated = client.run(
            f"SELECT device_key, executor_type, task_status, last_update_time, "
            f"next_query_time, fire_time, task_info FROM polling_tasks "
            f"WHERE device_key IN ({placeholders}) AND executor_type = %s",
            params=(*device_ips, executor_type),
            schema=settings.widgets_vision_schema)
    except client.MariaError as exc:
        logger.info("[widgets] polling status unavailable: %s", exc)
        return [{"device_ip": ip, "task_status": None, "status": UNKNOWN,
                 "last_update_time": None, "task_info": str(exc)}
                for ip in device_ips]

    by_ip = {r["device_key"]: r for r in rows}
    return [
        {
            "device_ip": ip,
            "task_status": (by_ip.get(ip) or {}).get("task_status"),
            "status": classify_task_status(by_ip.get(ip)),
            "last_update_time": (by_ip.get(ip) or {}).get("last_update_time"),
            "next_query_time": (by_ip.get(ip) or {}).get("next_query_time"),
            "fire_time": (by_ip.get(ip) or {}).get("fire_time"),
            "task_info": (by_ip.get(ip) or {}).get("task_info"),
        }
        for ip in device_ips
    ]
