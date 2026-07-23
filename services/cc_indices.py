"""
CC Index Catalog — built from real CC Elasticsearch indices.
Index naming pattern: {prefix}-ty-{type}-sid-{serverId}-sl-{sliceId}
"""

CC_INDEX_CATALOG = {
    # ── DP Attack indices ──────────────────────────────────────────────────
    "attack-data": {
        "display": "DP Attacks (Raw)",
        "category": "DP Attacks",
        "color": "#e74c3c",
        "description": "Open/ongoing DefensePro security attacks. One document per attack, upserted while the attack is active.",
        "key_fields": ["attackIpsId", "startTime", "endTime", "status", "category",
                       "deviceIp", "sourceAddress", "destAddress", "name", "risk"],
    },
    "attack-five-min-data": {
        "display": "DP Attacks 5-Min Aggregation",
        "category": "DP Attacks",
        "color": "#e67e22",
        "description": "5-minute rolled-up attack time-series.",
        "key_fields": ["startTime", "category", "deviceIp", "count"],
    },
    "attack-hourly-data": {
        "display": "DP Attacks Hourly Aggregation",
        "category": "DP Attacks",
        "color": "#d35400",
        "description": "Hourly rolled-up attack-by-category time-series.",
        "key_fields": ["startTime", "category", "deviceIp", "count"],
    },
    "attack-daily-data": {
        "display": "DP Attacks Daily Aggregation",
        "category": "DP Attacks",
        "color": "#c0392b",
        "description": "Daily rolled-up attack-by-category time-series.",
        "key_fields": ["startTime", "category", "deviceIp", "count"],
    },

    # ── DP Traffic indices ────────────────────────────────────────────────
    "dp-traffic-raw": {
        "display": "DP Traffic Raw",
        "category": "DP Traffic",
        "color": "#27ae60",
        "description": "Raw DP traffic utilization records per protection/port.",
        "key_fields": ["deviceIp", "protection", "startTime"],
    },
    "dp-traffic-agg": {
        "display": "DP Traffic Hourly Aggregation",
        "category": "DP Traffic",
        "color": "#2ecc71",
        "description": "Hourly DP traffic utilization aggregates per protection/port.",
        "key_fields": ["deviceIp", "protection", "startTime", "inBandwidth", "outBandwidth"],
    },
    "dp-traffic-five-min-agg": {
        "display": "DP Traffic 5-Min Aggregation",
        "category": "DP Traffic",
        "color": "#82e0aa",
        "description": "5-minute DP traffic utilization aggregates.",
        "key_fields": ["deviceIp", "protection", "startTime", "inBandwidth", "outBandwidth"],
    },
    "dp-traffic-dailyagg": {
        "display": "DP Traffic Daily Aggregation",
        "category": "DP Traffic",
        "color": "#1e8449",
        "description": "Daily DP traffic utilization aggregates.",
        "key_fields": ["deviceIp", "protection", "startTime"],
    },
    "traffic-data": {
        "display": "Generic Traffic Data",
        "category": "DP Traffic",
        "color": "#58d68d",
        "description": "Generic traffic data records.",
        "key_fields": ["deviceIp", "startTime"],
    },
    "traffic-five-min-data": {
        "display": "Generic Traffic 5-Min",
        "category": "DP Traffic",
        "color": "#a9dfbf",
        "description": "5-minute generic traffic aggregation.",
        "key_fields": ["deviceIp", "startTime"],
    },
    "traffic-hourly-data": {
        "display": "Generic Traffic Hourly",
        "category": "DP Traffic",
        "color": "#d5f5e3",
        "description": "Hourly generic traffic aggregation.",
        "key_fields": ["deviceIp", "startTime"],
    },
    "traffic-daily-data": {
        "display": "Generic Traffic Daily",
        "category": "DP Traffic",
        "color": "#1e8449",
        "description": "Daily generic traffic aggregation.",
        "key_fields": ["deviceIp", "startTime"],
    },

    # ── DP Applications indices ───────────────────────────────────────────
    "dp-five-min-applications": {
        "display": "DP Applications 5-Min",
        "category": "DP Applications",
        "color": "#5dade2",
        "description": "5-minute per-protection application-level traffic data.",
        "key_fields": ["deviceIp", "protection", "startTime", "appName"],
    },
    "dp-hourly-applications": {
        "display": "DP Applications Hourly",
        "category": "DP Applications",
        "color": "#3498db",
        "description": "Hourly application traffic aggregation.",
        "key_fields": ["deviceIp", "protection", "startTime", "appName"],
    },
    "dp-daily-applications": {
        "display": "DP Applications Daily",
        "category": "DP Applications",
        "color": "#2980b9",
        "description": "Daily application traffic aggregation.",
        "key_fields": ["deviceIp", "protection", "startTime", "appName"],
    },

    # ── DP Baseline indices ───────────────────────────────────────────────
    "dp-baseline-portion": {
        "display": "DP Baseline Portions (Raw)",
        "category": "DP Baselines",
        "color": "#9b59b6",
        "description": "Raw traffic baseline portions from DefensePro.",
        "key_fields": ["deviceIp", "protection", "startTime"],
    },
    "dp-five-min-baseline-portion": {
        "display": "DP Baseline Portions 5-Min",
        "category": "DP Baselines",
        "color": "#a569bd",
        "description": "5-minute aggregated baseline portions.",
        "key_fields": ["deviceIp", "protection", "startTime"],
    },
    "dp-hourly-baseline-portion": {
        "display": "DP Baseline Portions Hourly",
        "category": "DP Baselines",
        "color": "#8e44ad",
        "description": "Hourly aggregated baseline portions.",
        "key_fields": ["deviceIp", "protection", "startTime"],
    },
    "dp-daily-baseline-portion": {
        "display": "DP Baseline Portions Daily",
        "category": "DP Baselines",
        "color": "#6c3483",
        "description": "Daily aggregated baseline portions.",
        "key_fields": ["deviceIp", "protection", "startTime"],
    },
    "dp-baseline-controller": {
        "display": "DP Baseline Controller",
        "category": "DP Baselines",
        "color": "#7d3c98",
        "description": "Baseline controller data.",
        "key_fields": ["deviceIp", "protection"],
    },
    "dp-bdos-baseline-edge": {
        "display": "BDoS Baseline Edge",
        "category": "DP Baselines",
        "color": "#c39bd3",
        "description": "BDoS edge baseline data.",
        "key_fields": ["deviceIp", "protection", "startTime"],
    },
    "dp-bdos-baseline-rate": {
        "display": "BDoS Baseline Rate",
        "category": "DP Baselines",
        "color": "#d2b4de",
        "description": "BDoS rate baseline data.",
        "key_fields": ["deviceIp", "protection", "startTime"],
    },
    "dp-five-min-bdos-baseline-edge":  {"display": "BDoS Baseline Edge 5-Min",  "category": "DP Baselines", "color": "#bb8fce", "description": "5-min BDoS edge baseline.", "key_fields": []},
    "dp-hourly-bdos-baseline-edge":    {"display": "BDoS Baseline Edge Hourly",  "category": "DP Baselines", "color": "#af7ac5", "description": "Hourly BDoS edge baseline.", "key_fields": []},
    "dp-daily-bdos-baseline-edge":     {"display": "BDoS Baseline Edge Daily",   "category": "DP Baselines", "color": "#a569bd", "description": "Daily BDoS edge baseline.", "key_fields": []},
    "dp-five-min-bdos-baseline-rate":  {"display": "BDoS Baseline Rate 5-Min",   "category": "DP Baselines", "color": "#c39bd3", "description": "5-min BDoS rate baseline.", "key_fields": []},
    "dp-hourly-bdos-baseline-rate":    {"display": "BDoS Baseline Rate Hourly",  "category": "DP Baselines", "color": "#d2b4de", "description": "Hourly BDoS rate baseline.", "key_fields": []},
    "dp-daily-bdos-baseline-rate":     {"display": "BDoS Baseline Rate Daily",   "category": "DP Baselines", "color": "#e8daef", "description": "Daily BDoS rate baseline.", "key_fields": []},

    # ── DP Statistics ─────────────────────────────────────────────────────
    "dp-connection-statistics": {
        "display": "DP Connection Statistics",
        "category": "DP Statistics",
        "color": "#f39c12",
        "description": "DefensePro connection/traffic statistics.",
        "key_fields": ["deviceIp", "protection", "startTime", "category"],
    },
    "dp-five-min-connection-statistics": {"display": "DP Conn. Stats 5-Min",  "category": "DP Statistics", "color": "#f5b041", "description": "5-min connection statistics.", "key_fields": []},
    "dp-hourly-connection-statistics":   {"display": "DP Conn. Stats Hourly", "category": "DP Statistics", "color": "#f8c471", "description": "Hourly connection statistics.", "key_fields": []},
    "dp-daily-connection-statistics":    {"display": "DP Conn. Stats Daily",  "category": "DP Statistics", "color": "#fad7a0", "description": "Daily connection statistics.", "key_fields": []},
    "dp-concurrent-connections": {
        "display": "DP Concurrent Connections",
        "category": "DP Statistics",
        "color": "#e67e22",
        "description": "Concurrent connection counts per protection.",
        "key_fields": ["deviceIp", "protection", "startTime"],
    },
    "dp-five-min-concurrent-connections":  {"display": "DP Concurrent Conn. 5-Min",  "category": "DP Statistics", "color": "#eb984e", "description": "5-min concurrent connections.", "key_fields": []},
    "dp-hourly-concurrent-connections":    {"display": "DP Concurrent Conn. Hourly",  "category": "DP Statistics", "color": "#f0a868", "description": "Hourly concurrent connections.", "key_fields": []},
    "dp-daily-concurrent-connections":     {"display": "DP Concurrent Conn. Daily",   "category": "DP Statistics", "color": "#f5cba7", "description": "Daily concurrent connections.", "key_fields": []},

    # ── DNS indices ───────────────────────────────────────────────────────
    "dp-dns-baseline-edge": {"display": "DP DNS Baseline Edge",      "category": "DP DNS", "color": "#17a589", "description": "DNS edge baseline data.", "key_fields": []},
    "dp-dns-baseline-rate": {"display": "DP DNS Baseline Rate",      "category": "DP DNS", "color": "#1abc9c", "description": "DNS rate baseline data.", "key_fields": []},
    "dp-dns-edge-five-min-baseline":  {"display": "DNS Edge 5-Min Baseline",  "category": "DP DNS", "color": "#48c9b0", "description": "5-min DNS edge baseline.", "key_fields": []},
    "dp-dns-edge-hourly-baseline":    {"display": "DNS Edge Hourly Baseline",  "category": "DP DNS", "color": "#76d7c4", "description": "Hourly DNS edge baseline.", "key_fields": []},
    "dp-dns-edge-daily-baseline":     {"display": "DNS Edge Daily Baseline",   "category": "DP DNS", "color": "#a3e4d7", "description": "Daily DNS edge baseline.", "key_fields": []},
    "dp-dns-rate-five-min-baseline":  {"display": "DNS Rate 5-Min Baseline",   "category": "DP DNS", "color": "#48c9b0", "description": "5-min DNS rate baseline.", "key_fields": []},
    "dp-dns-rate-hourly-baseline":    {"display": "DNS Rate Hourly Baseline",  "category": "DP DNS", "color": "#76d7c4", "description": "Hourly DNS rate baseline.", "key_fields": []},
    "dp-dns-rate-daily-baseline":     {"display": "DNS Rate Daily Baseline",   "category": "DP DNS", "color": "#a3e4d7", "description": "Daily DNS rate baseline.", "key_fields": []},

    # ── HTTPS ─────────────────────────────────────────────────────────────
    "dp-https-server": {
        "display": "DP HTTPS Server Data",
        "category": "DP HTTPS",
        "color": "#5dade2",
        "description": "HTTPS protection server-level data.",
        "key_fields": ["deviceIp", "protection", "startTime"],
    },

    # ── EAAF indices ──────────────────────────────────────────────────────
    "eaaf-attack-data": {
        "display": "EAAF Attack Data (Raw)",
        "category": "EAAF",
        "color": "#1abc9c",
        "description": "Raw per-attack EAAF (Enhanced Attack Analysis & Forensics) data samples — the base index the hourly/daily rollups are built from.",
        "key_fields": ["deviceIp", "startTime", "category"],
    },
    "eaaf-attack-hourly-data": {
        "display": "EAAF Attack Data Hourly",
        "category": "EAAF",
        "color": "#16a085",
        "description": "Hourly EAAF attack data samples.",
        "key_fields": ["deviceIp", "startTime", "category"],
    },
    "eaaf-attack-daily-data": {
        "display": "EAAF Attack Data Daily",
        "category": "EAAF",
        "color": "#148f77",
        "description": "Daily EAAF attack data samples.",
        "key_fields": ["deviceIp", "startTime", "category"],
    },

    # ── DF (DefenseFlow) indices ───────────────────────────────────────────
    "df-activation": {
        "display": "DF Activations",
        "category": "DefenseFlow",
        "color": "#1abc9c",
        "description": "DefenseFlow activation records.",
        "key_fields": ["deviceIp", "startTime", "endTime", "status"],
    },
    "df-daily-activation": {
        "display": "DF Activations Daily",
        "category": "DefenseFlow",
        "color": "#17a589",
        "description": "Daily DefenseFlow activation aggregation.",
        "key_fields": ["deviceIp", "startTime"],
    },
    "df-attackstory-activation": {
        "display": "DF Attack Story Activations",
        "category": "DefenseFlow",
        "color": "#45b39d",
        "description": "Attack story activation links.",
        "key_fields": ["activationId", "startTime"],
    },
    "df-attackstory-protection": {
        "display": "DF Attack Story Protections",
        "category": "DefenseFlow",
        "color": "#76d7c4",
        "description": "Attack story protection records.",
        "key_fields": ["protectionId", "startTime"],
    },

    # ── ADC / Alteon indices ──────────────────────────────────────────────
    "adc-monitoring-raw": {
        "display": "ADC Monitoring Raw",
        "category": "ADC",
        "color": "#7fb3d3",
        "description": "Raw Alteon ADC monitoring data.",
        "key_fields": ["deviceIp", "timestamp"],
    },
    "adc-monitoring-contained": {
        "display": "ADC Monitoring Contained",
        "category": "ADC",
        "color": "#5499c7",
        "description": "Alteon monitoring contained/sampled data.",
        "key_fields": ["deviceIp", "timestamp"],
    },
    "adc-monitoring-hourly": {
        "display": "ADC Monitoring Hourly",
        "category": "ADC",
        "color": "#2e86c1",
        "description": "Hourly Alteon monitoring aggregation.",
        "key_fields": ["deviceIp", "timestamp"],
    },
    "adc-contained-hourly": {
        "display": "ADC Contained Hourly",
        "category": "ADC",
        "color": "#1a5276",
        "description": "Hourly ADC contained data.",
        "key_fields": ["deviceIp", "timestamp"],
    },
    "adc-network-raw":    {"display": "ADC Network Raw",    "category": "ADC", "color": "#85c1e9", "description": "Raw ADC network data.", "key_fields": []},
    "adc-network-hourly": {"display": "ADC Network Hourly", "category": "ADC", "color": "#5dade2", "description": "Hourly ADC network data.", "key_fields": []},
    "adc-system-raw":     {"display": "ADC System Raw",     "category": "ADC", "color": "#aed6f1", "description": "Raw ADC system data.", "key_fields": []},
    "adc-system-hourly":  {"display": "ADC System Hourly",  "category": "ADC", "color": "#7fb3d3", "description": "Hourly ADC system data.", "key_fields": []},

    # ── AppWall ───────────────────────────────────────────────────────────
    "aw-web-application": {
        "display": "AppWall Web Application",
        "category": "AppWall",
        "color": "#f1948a",
        "description": "AppWall web application event data.",
        "key_fields": ["deviceIp", "startTime", "category"],
    },

    # ── DP Auth ───────────────────────────────────────────────────────────
    "dp-auth-table": {
        "display": "DP Auth Table",
        "category": "DP Other",
        "color": "#aab7b8",
        "description": "DefensePro authentication table records.",
        "key_fields": ["deviceIp"],
    },

    # ── Alerts ────────────────────────────────────────────────────────────
    "alert-sid": {
        "display": "Alerts",
        "category": "Alerts",
        "color": "#e74c3c",
        "description": "CC system alert records.",
        "key_fields": ["timestamp", "severity", "message"],
    },

    # ── Audit & Config ────────────────────────────────────────────────────
    "audit-log-sid": {
        "display": "Audit Log",
        "category": "System",
        "color": "#bdc3c7",
        "description": "System audit log entries.",
        "key_fields": ["timestamp", "user", "action"],
    },
    "appconfig2": {
        "display": "App Configuration",
        "category": "System",
        "color": "#aab7b8",
        "description": "Application configuration store.",
        "key_fields": [],
    },
    "vrm-scheduled-report-result": {
        "display": "VRM Report Results",
        "category": "System",
        "color": "#95a5a6",
        "description": "VRM scheduled report result storage.",
        "key_fields": ["reportId", "timestamp"],
    },
}

# Ordered category list for sidebar grouping
CATEGORIES = [
    "DP Attacks",
    "DP Traffic",
    "DP Applications",
    "DP Baselines",
    "DP Statistics",
    "DP DNS",
    "DP HTTPS",
    "EAAF",
    "DefenseFlow",
    "ADC",
    "AppWall",
    "Alerts",
    "DP Other",
    "System",
]


def resolve_prefix(index_name: str) -> dict | None:
    """
    Given a concrete index name (e.g. 'attack-data-ty-attack-data-sid-0-sl-123'),
    find its catalog entry by matching against all known prefixes.
    """
    # Strip the CC slice/server suffix pattern: -ty-...-sid-...-sl-...
    clean = index_name.split("-ty-")[0] if "-ty-" in index_name else index_name
    # Try exact prefix match first
    for prefix, meta in CC_INDEX_CATALOG.items():
        if clean == prefix or clean.startswith(prefix):
            return {"prefix": prefix, **meta}
    return None

