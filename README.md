# DataHub Sentinel

DataHub Sentinel is a suite of agents that sit in the places where data
actually breaks — pull requests, schema deprecations, and production ML
pipelines — and use DataHub's context graph to catch problems before they
ship, then write the outcome back into DataHub (incidents, assertions,
deprecations, reviewed metadata) so the next engineer, or the next agent,
inherits the knowledge instead of rediscovering the breakage.

## See it work

The exact PR comment Sentinel posts (generated output, checked in at
[`examples/pr_comment_sample.md`](examples/pr_comment_sample.md)) when a PR
silently drops a column feeding two dashboards and a production fraud model:

> ## DataHub Sentinel: PR Impact Analysis — **CRITICAL**
> `models/orders_v1.sql` → `analytics.orders_v1` — `discount_pct`
> **removed (breaking)**, reaching 2 dashboards, 1 downstream dataset, and
> `fraud_detection_v3` (production ML model), with owners resolved from
> DataHub — and a linked incident raised on the dataset.

Every feature's real output is checked into [`examples/`](examples/), so you
can judge output quality before running anything. A screenshot/GIF tour will
be captured from the live demo for the final submission (see
[docs/demo_video_script.md](docs/demo_video_script.md)).

## Which hackathon challenges this addresses

Built for **"Build with DataHub: The Agent Hackathon"**, targeting:

- **"Agents That Do Real Work"** — PR Impact Analysis and the Schema
  Migration Copilot act on real repos/PRs and write incidents, assertions,
  and deprecation links back to DataHub.
- **"Metadata-Aware Code Generation & Development"** — the Migration Copilot
  generates SQL rewrites grounded strictly in DataHub-verified schemas and
  column mappings; the LLM is never allowed to invent a column name.
- Secondary: **"Production ML Agents"** — the ML Blast Radius agent traces
  unhealthy upstreams across ML lineage to production models and raises
  incidents on the model entity.

## Quickstart

Prereqs: Docker (~8 GB free RAM for DataHub's quickstart stack), Python 3.11+.

```bash
git clone https://github.com/kunal0297/Datahub-Sentinal
cd Datahub-Sentinal
cp .env.example .env        # defaults work for a local quickstart; add
                            # ANTHROPIC_API_KEY for migration codegen/enrichment
make demo                   # installs, starts DataHub OSS, seeds the demo graph
```

`make demo` leaves you with DataHub at http://localhost:9002 (user/pass
`datahub`/`datahub`) populated with a synthetic e-commerce + ML graph in a
deliberately unhealthy state. Then try each feature:

```bash
# Tier 1 — PR Impact Analysis (also packaged as a GitHub Action, see below)
.venv/bin/python -m sentinel.cli pr-impact --repo seed/sample_repo --base-ref HEAD~1

# Tier 1 — Schema Migration Copilot (orders_v1 -> orders_v2)
.venv/bin/python -m sentinel.cli migrate \
  --from "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.orders_v1,PROD)" \
  --to   "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.orders_v2,PROD)" \
  --repo seed/sample_repo
.venv/bin/python -m sentinel.cli migrate status --repo seed/sample_repo

# Tier 2 — ML Blast Radius (traces the seeded failing freshness signal to the model)
.venv/bin/python -m sentinel.cli ml-check \
  --urn "urn:li:dataset:(urn:li:dataPlatform:postgres,raw.orders,PROD)"

# Tier 2 — Quality Checking (fails on seeded data, raises an incident...)
.venv/bin/python -m sentinel.cli quality run
# ...then heal the data and watch the incident auto-resolve:
.venv/bin/python seed/seed_datahub.py --heal
.venv/bin/python -m sentinel.cli quality run

# Tier 2 — Metadata Enrichment (drafts grounded docs as PENDING proposals)
.venv/bin/python -m sentinel.cli enrich \
  --urn "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.customer_revenue_summary,PROD)"
.venv/bin/python -m sentinel.cli proposals list
.venv/bin/python -m sentinel.cli proposals accept <id>   # the ONLY path to a DataHub write
```

After each command, check the DataHub UI: incidents on `analytics.orders_v1`
and `fraud_detection_v3`, assertions on `raw.orders`, the deprecation banner
on `orders_v1` linking to `orders_v2`.

### Using the GitHub Action

PR Impact Analysis ships as a reusable Docker Action
([.github/actions/pr-impact-analysis](.github/actions/pr-impact-analysis)):

```yaml
- uses: kunal0297/Datahub-Sentinal/.github/actions/pr-impact-analysis@master
  with:
    datahub-gms-url: ${{ vars.DATAHUB_GMS_URL }}
    datahub-token: ${{ secrets.DATAHUB_TOKEN }}
    github-token: ${{ secrets.GITHUB_TOKEN }}
    # block_on_critical: "true"   # opt-in merge blocking; off by default
```

### Scheduling the Tier 2 checks

The Quality Checker and ML Blast Radius are cron-friendly CLIs by design
(exit 1 on failures), not daemons:

```yaml
on:
  schedule: [{ cron: "*/30 * * * *" }]
jobs:
  quality:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pip install -e . && sentinel quality run && sentinel ml-check --urn "$MODEL_URN"
        env:
          DATAHUB_GMS_URL: ${{ vars.DATAHUB_GMS_URL }}
          DATAHUB_GMS_TOKEN: ${{ secrets.DATAHUB_TOKEN }}
          MODEL_URN: urn:li:mlModel:(urn:li:dataPlatform:sagemaker,fraud_detection_v3,PROD)
```

## Architecture

One typed DataHub client (GraphQL + MCP tools + SDK emitter) under
`core/datahub_client.py`; three shared engines (blast radius, incidents,
proposals) that every agent composes; five agents on top. The key call:
**Automatic Incident Creation is core infrastructure, not "one of six
agents"** — PR Impact, Migration Copilot, ML Blast Radius, and Quality
Checking all route through one engine that owns severity rules, dedup,
owner fallback, and notification routing. Full design, plus verification
notes on every DataHub API this uses: [ARCHITECTURE.md](ARCHITECTURE.md).

## Feature status — deliberate tiering

Three features built fully, three built as honest MVPs, four shipped as
extension hooks. That split is a design decision, not unfinished work: a
broken flagship is worse than an honestly-scoped MVP.

| Tier | Feature | Status |
|---|---|---|
| 1 | PR Impact Analysis | ✅ full — GitHub Action + CLI, schema diff classifier, blast radius, idempotent comments, incidents |
| 1 | Schema Migration Copilot | ✅ full — mapping inference (reviewed, never blind), LLM rewrites from DataHub-verified schemas, PRs/patches, status tracker, deprecation write-back |
| 1 | Incident Automation Engine | ✅ full — YAML severity rules, dedup, owner fallback, routing, auto-resolve |
| 2 | Metadata Enrichment | 🟡 MVP — grounded drafting -> PENDING proposals; refuses on thin evidence |
| 2 | ML Blast Radius | 🟡 MVP — typed ML-lineage path tracing, incidents on the model entity |
| 2 | Quality Checking | 🟡 MVP — quality-as-code YAML, ingestion + warehouse modes, native assertions, auto-resolve |
| 3 | Notifier plugins | 🔌 hook — Slack real; Jira/Teams honest stubs that log their routing |
| 3 | Connector plugins | 🔌 hook — ABC + worked CSV-directory example |
| 3 | Kubernetes operator | 🔌 design doc + WIP manifests ([deploy/k8s/DESIGN.md](deploy/k8s/DESIGN.md)) |
| 3 | DataHub Skill | ✅ [skills/datahub-incident-guard](skills/datahub-incident-guard/SKILL.md), packaged for upstream contribution |

### Known limitations / roadmap (Tier 2, on purpose)

- **Metadata Enrichment**: one URN per invocation. No batch/scheduled
  catalog sweep and no "which undocumented table matters most" priority
  score yet — `# TODO(batch-mode)` in `enricher.py` marks where a scheduler
  plugs in.
- **ML Blast Radius**: on-demand check plus the cron snippet above, not a
  continuously-running service. Health signals are active incidents +
  latest assertion results; drift/performance metrics are out of scope.
- **Quality Checking**: no anomaly detection or adaptive thresholds — that
  is deliberately ceded to DataHub Cloud's managed Observe product, which
  this project intentionally does not depend on; the open equivalent here
  is fixed thresholds + a scheduler you own. `custom_sql` checks need a
  warehouse connection (sqlite implementation shipped; real warehouses are
  a two-method protocol away). No persistent scheduler daemon.

## How this uses DataHub

| Capability | Where | DataHub surface |
|---|---|---|
| Lineage walks (blast radius, consumers, ML paths) | `core/blast_radius.py`, both Tier 1 agents, ML Blast Radius | MCP `get_lineage`, `get_entities` |
| Schema reads (diff before-state, migration mappings) | PR Impact, Migration Copilot | MCP `list_schema_fields` |
| Sample queries + neighbors as grounding evidence | Metadata Enrichment | MCP `get_dataset_queries`, `get_lineage` |
| Profiling stats (credential-free quality mode) | Quality Checker | SDK `get_latest_timeseries_value` (DatasetProfile) |
| Assertion health signals | ML Blast Radius | GraphQL `dataset { assertions { runEvents } }` |
| Incident dedup lookups | Incident Engine | GraphQL `incidents(state: ACTIVE)` per entity |

**Every write-back to the graph** (the judging criterion that matters most):

1. **Incidents raised** — `raiseIncident` on datasets (PR Impact, Quality)
   and on ML models (ML Blast Radius), always with agent + reason + link in
   the description.
2. **Incidents updated/resolved** — `updateIncidentStatus` for dedup
   updates and for auto-resolution with an explanatory comment.
3. **Native assertions + run events** — Quality Checker emits FIELD/VOLUME/
   SQL assertion entities with per-run results (never the deprecated
   DATASET type).
4. **Deprecation links** — Migration Copilot marks the old asset deprecated
   with `replacementUrn` pointing at its successor via `updateDeprecation`.
5. **Human-approved metadata** — accepted enrichment proposals land via MCP
   `update_description` (asset- and column-level); the proposal lifecycle
   itself is Sentinel-owned because open-source DataHub has no proposal
   primitive (documented in ARCHITECTURE.md "Known gap").
6. **The seeded demo graph itself** — datasets, lineage, ML chain, owners,
   domains, tags, profiles, and assertions, all emitted through the SDK.

Plus an upstream contribution candidate: the
[`datahub-incident-guard`](skills/datahub-incident-guard/SKILL.md) skill
packages the Incident Engine's decision logic in the
`datahub-project/datahub-skills` convention.

## Demo video

Under 3 minutes, scripted at [docs/demo_video_script.md](docs/demo_video_script.md).
*(Link will be added here once recorded against the live demo — the script
is written first, per the submission plan.)*

## Development

```bash
make install    # venv + editable install
make lint       # ruff check + format check
make typecheck  # mypy (clean on src/sentinel)
make test       # 173 unit tests, hermetic (FakeDataHubClient)
make test-integration  # needs a live DataHub; RUN_INTEGRATION_TESTS=1
```

CI runs the unit suite on every push ([ci.yml](.github/workflows/ci.yml))
and the full seeded end-to-end scenario against a real DataHub quickstart
([demo-self-check.yml](.github/workflows/demo-self-check.yml)).

## License

Apache 2.0 — see [LICENSE](LICENSE).
