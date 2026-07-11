# DataHub Sentinel

Agents that sit in the places where data actually breaks — pull requests, schema
deprecations, and production ML pipelines — using DataHub's context graph to catch
problems before they ship, and writing the outcome back into DataHub so the next
engineer (or agent) inherits the knowledge.

> **Status: Phase 3 (Schema Migration Copilot) done — all three Tier 1
> features are now built.** This README will be filled in fully (screenshots,
> quickstart, "How this uses DataHub" write-back mapping, demo video link) as
> polish work in [ARCHITECTURE.md](ARCHITECTURE.md)'s build plan lands. Done so
> far: repo scaffold, `core/` client + models + config, the seed script
> (verified via dry-run serialization against the installed `acryl-datahub`
> SDK — live DataHub bring-up still pending, see ARCHITECTURE.md "Status
> notes"), the Incident Automation Engine, PR Impact Analysis end to end
> (packaged as a GitHub Action, plus a `sentinel pr-impact` CLI for the
> self-contained local demo), and the Schema Migration Copilot end to end
> (column-mapping inference, LLM-backed rewrite generation, local patch
> writing or real GitHub PRs, migration tracking, deprecation write-back via
> `sentinel migrate` / `sentinel migrate status`) — 112 passing unit tests.

## Which hackathon challenge(s) this addresses

Built for **"Build with DataHub: The Agent Hackathon"**, targeting:

- **"Agents That Do Real Work"** — PR Impact Analysis and the Schema Migration
  Copilot both act autonomously on real GitHub repos and write incidents/proposals
  back to DataHub.
- **"Metadata-Aware Code Generation & Development"** — the Migration Copilot
  generates SQL/dbt rewrites grounded strictly in DataHub-verified schemas.
- Secondary hit on **"Production ML Agents"** via the ML Blast Radius MVP.

## Feature status

| Tier | Feature | Status |
|---|---|---|
| 1 | PR Impact Analysis | 🟡 built + tests done (GitHub Action + CLI); live DataHub demo pending |
| 1 | Schema Migration Copilot | 🟡 built + tests done (`sentinel migrate` / `migrate status`); live DataHub + live LLM demo pending |
| 1 | Automatic Incident Creation (Incident Automation Engine) | 🟡 core engine + tests done; live DataHub demo pending |
| 2 | Metadata Enrichment | 🚧 not started |
| 2 | ML Blast Radius | 🚧 not started |
| 2 | Quality Checking | 🚧 not started |
| 3 | Notifier plugins (Slack real, Jira/Teams stub) | ✅ built alongside the Incident Engine (Slack real, Jira/Teams stub) |
| 3 | Connector plugin interface | 🚧 not started |
| 3 | Kubernetes operator design | 🚧 not started |

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full system design and build plan.

## License

Apache 2.0 — see [LICENSE](LICENSE).
