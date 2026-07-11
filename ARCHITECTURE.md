# Architecture

## Component diagram

```
                         +-------------------------------+
                         |   Open-source DataHub (OSS)    |
                         |  GMS + GraphQL + MCP Server     |
                         +---------------+-----------------+
                                         |
                         +---------------v-----------------+
                         |      core/datahub_client          |  <- thin, well-tested wrapper
                         |  (wraps MCP tool calls + the        |     around both MCP + GraphQL
                         |   GraphQL API + the SDK emitter)    |
                         +---------------+-----------------+
                                         |
              +--------------------------+--------------------------+
              |                          |                          |
   +----------v----------+   +-----------v-----------+   +----------v------------+
   | core/blast_radius     |   | core/incident_engine   |   | core/proposal_engine   |
   | (shared lineage-walk  |   | (severity, dedup,      |   | (Sentinel-owned         |
   |  + impact classifier  |<--|  owner resolution,     |   |  pending/accept/reject  |
   |  used by PR Impact &  |   |  notification routing) |   |  proposal lifecycle)    |
   |  ML Blast Radius)     |   +-----------+-----------+   +------------------------+
   +----------+-----------+               |
              |                +----------v----------+
              |                | integrations/notifiers|
              |                |  slack.py  (real)      |
              |                |  jira.py   (stub)       |
              |                |  teams.py  (stub)       |
              |                +-------------------------+
              |
   +----------v-------------------------------------------------------------+
   |                              Agents (Tier 1 = full, Tier 2 = MVP)         |
   |  agents/pr_impact/            Tier 1 - GitHub Action + webhook service   |
   |  agents/migration_copilot/    Tier 1 - CLI + codegen + PR opener          |
   |  agents/metadata_enrichment/  Tier 2 - CLI, drafts proposals              |
   |  agents/ml_blast_radius/      Tier 2 - CLI + scheduled check              |
   |  agents/quality_checker/      Tier 2 - CLI + scheduled check              |
   +---------------------------------------------------------------------------+
```

The Incident Automation Engine is core infrastructure, not "one of six agents" —
PR Impact Analysis, Migration Copilot, ML Blast Radius, and Quality Checking all
call into it. See `src/sentinel/core/incident_engine.py`.

## DataHub API surface — verification notes

This project treats DataHub's tool/mutation surface as something to confirm, not
assume. Findings as of this writing (2026-07), sources linked inline:

- **DataHub OSS bring-up**: the officially recommended path is
  `pip install acryl-datahub` then `datahub docker quickstart`, which manages its
  own internal docker-compose profile (14 containers: GMS, MySQL, OpenSearch,
  Kafka, Zookeeper, frontend, etc., ~8GB RAM recommended). Hand-rolling that
  compose file risks drifting from the maintained one, so this repo's
  `docker-compose.yml` is a thin layer for Sentinel-only services (nothing in
  Tier 1/2 requires a long-lived Sentinel service, but the Quality Checker's
  scheduled mode can use one) plus a `make demo` target that shells out to the
  `datahub` CLI's quickstart. See [DataHub Quickstart Guide](https://docs.datahub.com/docs/quickstart)
  and [Deploying with Docker](https://docs.datahub.com/docs/docker).
- **`mcp-server-datahub` tool surface** (verified via the acryldata GitHub repo
  and PyPI page): read tools `search`, `get_lineage`, `get_entities`,
  `list_schema_fields`, `get_lineage_paths_between`, `get_dataset_queries`;
  mutation tools (gated behind `TOOLS_IS_MUTATION_ENABLED=true`) `add_tags`,
  `remove_tags`, `add_terms`, `remove_terms`, `add_owners`, `remove_owners`,
  `set_domains`, `remove_domains`, `update_description`,
  `add_structured_properties`, `remove_structured_properties`; user tool `get_me`
  (behind `TOOLS_IS_USER_ENABLED=true`); document tools `search_documents`,
  `grep_documents`, `save_document`.
  **Correction versus an earlier draft of this project's spec**: there is no
  `accept_or_reject_proposals` tool in the current tool list. See "Known gap"
  below.
- **Incidents (GraphQL)**, verified against
  [Incidents API Tutorial](https://docs.datahub.com/docs/api/tutorials/incidents)
  and the [Mutations reference](https://docs.datahub.com/docs/graphql/mutations):
  `raiseIncident(input: RaiseIncidentInput!): String!` where
  `RaiseIncidentInput = { resourceUrn!, type!, customType, title, description }` —
  **no severity/priority field**. Supported `IncidentType` values: `OPERATIONAL`,
  `FRESHNESS`, `VOLUME`, `COLUMN`, `SQL`, `DATA_SCHEMA`, `CUSTOM`. Querying active
  incidents is done per-entity (`dataset(urn){ incidents(state: ACTIVE) {...} }`);
  resolving is `updateIncidentStatus(urn!, input: IncidentStatusInput!)` with
  `{ state: RESOLVED, message }`.
- **Deprecation (GraphQL)**: `updateDeprecation(input: UpdateDeprecationInput!)` and
  `batchUpdateDeprecation(input: BatchUpdateDeprecationInput!)`.
- **Known gap — governed metadata proposals**: the original project spec assumed
  DataHub exposes a "propose then human-approves" primitive (an
  `accept_or_reject_proposals` MCP tool). Neither the verified MCP tool list nor
  the GraphQL mutations reference contains any `propose*`/`accept*`/`reject*`
  proposal mutation in open-source DataHub. **Resolution**: `core/proposal_engine.py`
  owns the pending/accepted/rejected lifecycle itself (see
  `models.MetadataChangeProposal` / `models.ProposalStatus`), and only calls a real
  DataHub write (`update_description`, `add_tags`, `add_terms`, `updateDeprecation`)
  once a human accepts via `sentinel proposals accept <id>`. This preserves the
  human-in-the-loop guarantee the spec requires without depending on a DataHub
  primitive that doesn't exist in the OSS product. DataHub Cloud's managed
  "Proposals" governance feature is the closer match to what the spec originally
  described — noted here so a reader isn't left thinking this was missed rather
  than substituted.
- **ML entity URNs**, verified against the DataHub metamodel docs:
  `mlFeatureTable`: `urn:li:mlFeatureTable:(urn:li:dataPlatform:<platform>,<name>)`;
  `mlFeature`: `urn:li:mlFeature:(<featureNamespace>,<name>)` (no platform/env —
  feature identity is independent of any feature table); `mlModel`:
  `urn:li:mlModel:(urn:li:dataPlatform:<platform>,<name>,<env>)`.
- **Assertions**: current (non-deprecated) types are `FIELD`, `VOLUME`,
  `FRESHNESS`, `DATA_SCHEMA`, `SQL`. The Quality Checker (Tier 2) creates these
  natively via the SDK rather than depending on DataHub Cloud's managed
  Smart Assertions / Observe.

## MCP client design

`DataHubClient` (in `core/datahub_client.py`) exposes:

- **Sync** GraphQL methods (`raise_incident`, `update_incident_status`,
  `get_active_incidents`, `update_deprecation`) via `httpx`, since these don't
  need the MCP subprocess.
- **Async** MCP-backed methods (`search`, `get_lineage`, `get_entities`, ...), used
  as `async with client.mcp(): ...`, which spawns one `mcp-server-datahub`
  subprocess over stdio for the duration of the block and reuses it across calls
  — re-spawning per call would be needlessly slow for a multi-hop lineage walk.
- A sync **SDK emitter** factory (`make_rest_emitter`) for `seed/seed_datahub.py`'s
  bulk metadata emission, which is a distinct concern from agent-driven
  read/write and doesn't belong behind the MCP tool surface.

## Status notes

- **Live DataHub smoke test: deferred.** `seed/seed_datahub.py` is verified by
  constructing and serializing (`MetadataChangeProposalWrapper.make_mcp()`) all
  50 seeded aspects against the actually-installed `acryl-datahub` package — this
  catches signature/field-name mistakes, but hasn't yet been run against a live
  `datahub docker quickstart` instance, because the dev machine had ~145MB free
  RAM and 86% swap utilization at the time (the quickstart's 14-container stack
  recommends 8GB free). Run `make demo` once there's headroom to complete this
  check — see the Makefile's `datahub-up`/`seed` targets.

## Repository layout

See the file tree in the project's build spec (kept out of this file to avoid two
sources of truth drifting) — current layout matches it exactly; deviations will be
called out here as they're made.
