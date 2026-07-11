---
name: datahub-incident-guard
description: >
  Turn a detected data problem into a well-formed, deduplicated,
  correctly-routed DataHub incident — and resolve it automatically once the
  underlying condition clears. Use whenever an agent or workflow detects
  that something is wrong with a DataHub asset (a breaking schema change, a
  failing quality check, a stale upstream feeding a production ML model)
  and needs to record and route that finding through DataHub's incident
  API instead of a fire-and-forget chat message.
---

# DataHub Incident Guard

You are managing incidents on DataHub entities via the GraphQL API. Your
job is to make each incident *actionable* (states which agent/check raised
it and why, on the entity the responsible human actually cares about) and
*non-duplicative* (repeated detection of the same condition updates one
incident; it never piles up copies).

## When to use this skill

- A quality/freshness/volume check you ran has failed for a DataHub dataset.
- A schema diff shows a breaking change reaching downstream consumers.
- A production ML model sits downstream of an asset with a failing signal.
- A previously-raised condition has cleared and its incident should resolve.

Do NOT raise an incident for informational findings (a column lacking a
description, a suggestion). Incidents page humans; proposals and comments
don't.

## Core workflow

### 1. Choose the resource entity deliberately

Raise the incident on the entity whose owner must act, which is not always
the entity where the signal fired. A failing freshness check on
`raw.orders` that endangers `fraud_detection_v3` (a production model)
should raise the incident **on the model**, with the dependency path in the
description — the ML on-call has never heard of `raw.orders`.

### 2. Compute a dedup key and check for an existing incident

Build a stable key from `(resource urn, incident type, root-cause signal)`
— never include timestamps or run ids, or every rerun mints a "new"
problem. Embed the key in the incident description as an HTML comment
marker (`<!-- guard:dedup_key=... -->`), because `RaiseIncidentInput` has
no custom-metadata field to carry it.

Query the entity's active incidents first:

```graphql
query getActiveIncidents($urn: String!) {
  dataset(urn: $urn) {
    incidents(state: ACTIVE, start: 0, count: 100) {
      incidents { urn incidentType title description status { state } }
    }
  }
}
```

(The `incidents` field exists on dataset, dashboard, chart, dataJob,
dataFlow, and mlModel root queries.) If an active incident carries your
dedup key, update it instead of raising a new one:

```graphql
mutation updateIncidentStatus($urn: String!, $input: IncidentStatusInput!) {
  updateIncidentStatus(urn: $urn, input: $input)  # { state: ACTIVE, message: "recurred: ..." }
}
```

### 3. Classify severity from the graph, not vibes

Severity is your business logic — open-source DataHub's
`RaiseIncidentInput` has **no priority/severity field** — so encode it as a
`[CRITICAL]`/`[HIGH]`/`[MEDIUM]`/`[LOW]` title prefix and spell out the
reasoning in the description. Classify from facts the context graph gives
you, in this order:

1. Asset (or anything it reaches) carries a production-critical tag/domain → CRITICAL
2. A production-tagged ML model is downstream → CRITICAL
3. Any dashboard/chart is downstream → HIGH
4. Multiple downstream datasets → MEDIUM
5. Otherwise → LOW

Get the downstream facts from lineage (MCP `get_lineage`, or the lineage
GraphQL) before classifying — never guess reach.

### 4. Raise with a full audit trail

```graphql
mutation raiseIncident($input: RaiseIncidentInput!) {
  raiseIncident(input: $input)
}
```

with `type` one of `OPERATIONAL | FRESHNESS | VOLUME | COLUMN | SQL |
DATA_SCHEMA | CUSTOM` (match the signal: freshness failure → FRESHNESS,
row-count → VOLUME, breaking PR change → OPERATIONAL), and a description
that always contains:

- **who**: which agent/check raised it (`Raised by <agent> because: ...`)
- **why**: the concrete signal, with observed values
- **reach**: affected downstream assets and their owners
- **link**: the PR/run/dashboard URL that triggered detection
- the dedup-key marker comment

"Quality check failed" with no context is the failure mode this skill
exists to prevent.

### 5. Route to a resolvable owner

Resolve the asset's owners from DataHub; if it has none, walk up to its
domain's owners; only then fall back to a configured default triage owner.
Every incident must land on *someone*.

### 6. Auto-resolve when the condition clears

When a later run of the same check passes, find the active incident by the
same dedup key and resolve it with a message explaining why:

```graphql
# IncidentStatusInput: { state: RESOLVED, message: "check X passed on re-run: <reason>" }
```

Never resolve silently, and never resolve an incident whose dedup key you
don't own — a human may have raised it for a reason you can't see.

## Reference implementation

This skill's decision logic is implemented, with tests for the dedup,
severity-rules, and owner-fallback behaviors, in
[DataHub Sentinel](https://github.com/kunal0297/Datahub-Sentinal)
(`src/sentinel/core/incident_engine.py`) — an Apache-2.0 project built for
the DataHub Agent Hackathon.
