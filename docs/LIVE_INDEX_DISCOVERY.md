# Live Index Discovery — building the "possible indices" catalog from a CC machine

Instructions for producing the complete list of **possible** KVision/CC Elasticsearch indices —
including families that have no live index yet — with the **correct slice duration**, **doc types**,
and **field mappings**, extracted entirely from a live machine. No source-code access required.

This is the authoritative replacement for the slice-length *guessing* currently done in
`routers/artificial.py` (name-token heuristics + `scripts/time_slice.sh` relative-closeness).
Everything below was validated against a live QA machine (`10.205.189.20`, cluster `vision-es`,
OpenSearch 1.3.14) and cross-checked against the KVision source (`kvision_libs`).

A generated reference snapshot lives in `data/possible_indices_catalog.json`;
`scripts/live_index_discovery.py` regenerates it against any machine.

---

## 1. Why this works

Every KVision data service registers its **index templates at startup** (Spring
`ElasticsearchIndexInitializerBase`, on `ContextRefreshedEvent`) — *regardless of whether any
data was ever written*. When a slice period rolls over, the writer just bulk-inserts to a new
index name; ES auto-creates the index and applies the matching template. Therefore:

- The **template list** = the complete universe of possible index families + their **field mappings**.
- The **runtime configuration** (slice sizes per family) is persisted in ES itself — the services
  run with `configRepository=es`, so all `ConfigurationRecord` sections live in an **`appconfig*`
  index** (named `appconfig2` on the reference machine).
- The **live indices** provide real example names and let you *measure* slice duration empirically.

## 2. Data sources (4 REST calls)

CC QA machines typically expose ES on `http://<ip>:9200` with **no auth**. Verify with `GET /`.

| # | Call | Gives you |
|---|------|-----------|
| 1 | `GET /_cat/templates?h=name,index_patterns&s=name` | All registered families (names + patterns) |
| 2 | `GET /_template` | Full template JSON: `index_patterns`, `settings`, **`mappings.properties` = the field list with types** |
| 3 | `GET /appconfig2/_search?size=50` (find the index via `GET /_cat/indices/*appconfig*`) | Live slice-size config records (defaults **and** overrides) |
| 4 | `GET /_cat/indices?h=index,creation.date&s=index` | Real index names + creation timestamps |

> OpenSearch 1.3 uses the **legacy** template API (`/_template`), not `/_index_template`.

## 3. Index name grammar

```
<prefix>[-<tenant>]-ty-<docType>-sid-<serverId>-sl-<sliceNumber>[-pt-<partition>]
```

- All lowercase; spaces in doc types become `__` (e.g. `behavioral__dos`, `anti-scanning`).
- `docType == prefix` for most families. Exceptions:
  - `dp-attack-raw` / `dpforensics`: docType = **attack category** — one template *per category*
    (29 on the reference machine: `acl`, `anomalies`, `anti-scanning`, `behavioral__dos`, `dos`,
    `dns__flood`, `application__protection`, `asn__feed__hit`, `flow__detector__*`,
    `geo__location`, `external__detector`, …). Enumerate them live from template names
    matching `dp-attack-index-template-ty-<category>`.
  - BDOS/DNS baseline raw families: docType is `baseline-edge` / `baseline-rate`
    (e.g. `dp-bdos-baseline-edge-ty-baseline-edge-…`).
- `sid` = HA server id: `0` (no HA), `1`/`2` (HA primary/secondary). Special **open** form exists:
  `…-sid-x-open-sl-0` (open/ongoing-attack indices).
- `sl` = `floor(timestampMs / (sliceMinutes * 60000))` — **epoch-based**, no offset.
- `-pt-<n>` partition suffix appears when an index hits its configured max size
  (seen live: `dp-traffic-agg-…-sl-688-pt-1`).
- A **tenant** segment appears between prefix and `-ty-` only in multi-tenant deployments.
- Some indices are **unsliced** (definition/config style, no markers or no `-sl-`):
  `dp-https-server`, `dp-auth-table-status`, `alert-sid-0`, `audit-log-sid-0`, `appconfig2`,
  `snapshot-definition`, `user-activity-log*`, `vrm-scheduled-report-*`, and the live
  `dpforensics` snapshot form (`dpforensics-1-ty-<category>-sid-0`).

## 4. Slice-duration resolution (precedence order)

1. **Empirical (best)** — from source #4. For each prefix with live indices:
   `sliceMinutes ≈ creation_date_ms / (slice_number * 60000)`, taking the **min** across the
   family's indices (guards against retro-created indices), snapped to the standard set
   `{5, 20, 60, 720, 1440, 10080, 20160, 43200, 129600, 259200, 525600}` minutes.
   With two indices: `Δcreation_date / Δslice_number` is exact.
2. **Config (authoritative for empty families)** — from source #3. Walk every hit's
   `configurationSections.*.configurationRecords`, keep records whose key matches
   `index` **and** `size|days|min`. Keys ending `*days` are in **days** (×1440 → minutes);
   `*minutes`/`*mins` are minutes. Section names map to families
   (`dpTraffic` → `dp-traffic-*`, `dpAttack` → `dp-attack-raw`, `genericAttackData` → `attack-data`,
   `connectionstatistics`, `outOfState`, `topTalkers`, `eaafAttackData`, `qdosStatus`, …).
3. **Code defaults (fallback)** — only for keys absent from appconfig (rare; e.g. the
   top-talkers agg sizes on the reference machine). See the static table in the toolkit
   (`kvision_index_slice_table.txt`) built from `kvision_libs` `*ConfigurationValues` classes.

Reference-machine values worth knowing (live): `dp-traffic-raw`=12h, `dp-attack-raw`=14d,
`dp-qdos-status-raw`=**5 min**, `dp-bdos-baseline-edge/rate`=**20 min** (override; code default 60),
raw stats mostly 1h, five-min aggs 1d/7d, hourly aggs 30d, daily aggs 180d,
`df-attackstory-*`=90d, `df-protected-object`/`aw-web-application`=365d.

## 5. Field mappings per index family

From source #2: each template's `mappings.properties` is the **complete field list with ES types**
for every index the family will ever create (mappings are `"dynamic": "false"` — fields not in the
template are stored but not indexed/searchable, so the template *is* the schema). Flatten nested
`properties` recursively into dotted paths (`latestBlockedSample.protocol: keyword`).

For an **existing** index, `GET /<index>/_mapping` returns the same shape — but prefer the
template: it covers families with no live index and is what new indices will get.

When creating artificial data for a not-yet-existing index there is **no need to create the index
or set mappings manually** — a correctly-formed name is matched by the template pattern and ES
applies settings + mappings automatically on first write.

## 6. Constructing a valid index name for a timestamp

```python
slice_number = timestamp_ms // (slice_minutes * 60_000)
name = f"{prefix}-ty-{doc_type}-sid-0-sl-{slice_number}"     # single-tenant, no HA
```

Worked example (2026-07-26, `adc-contained-hourly`, 30d slices):
`1785078804619 // (43200*60000)` = `688` → `adc-contained-hourly-ty-adc-contained-hourly-sid-0-sl-688`
(matches the live index).

## 7. Output contract

`scripts/live_index_discovery.py <es_host>` produces `possible_indices_catalog.json`:

```json
{
  "_meta": { "source": "...", "generated": "...", "name_formula": "..." },
  "<family-prefix>": {
    "index_pattern": "dp-attack-raw-ty-acl-*",
    "doc_types": ["acl", "anomalies", "..."],
    "slice_minutes": 20160,
    "slice_source": "empirical (live indices) | config ... | code default | unsliced ...",
    "live_example": "dp-attack-raw-ty-anomalies-sid-0-sl-1474",
    "field_count": 65,
    "fields": { "deviceIp": "keyword", "startTime": "date", "...": "..." }
  }
}
```

`slice_minutes: null` + `slice_source: "unsliced…"` marks definition-style indices; families whose
writer never persisted config on this machine and have no live index stay `null` — surface those
to the user rather than guessing.

## 8. Gotchas checklist

- [ ] Probe `GET /` first; fall back to `https` + basic auth if 9200 refuses plain HTTP.
- [ ] Find the appconfig index by pattern (`*appconfig*`) — the name carries a version suffix.
- [ ] Skip `.`-prefixed system indices and `sl-0` "open" indices when measuring slices.
- [ ] `-pt-` partition is the **last** token when present; strip it before parsing `sl`.
- [ ] **Template names are NOT a reliable docType source.** A product wiring bug
      (`DPElasticsearchIndexInitializer` injects self-referential `IndexDetails<...>` generics)
      registers some templates under an unrelated type — e.g. the out-of-state agg templates are
      named `…-ty-dp-attack-raw` while the actual indices use `-ty-dp-<agg>-out-of-state-ts`.
      For constructing names: use the **pattern's** `-ty-` token when present (per-category
      attack templates), otherwise **docType = prefix** (the writer default). Live index names
      always win when they exist.
- [ ] Doc types with spaces appear as `__` in both template names and index names.
- [ ] Slice sizes are **runtime-overridable** — always prefer live values over code defaults;
      re-run discovery per target machine, don't reuse a catalog across machines.
