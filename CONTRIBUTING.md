# Contributing to DataHub Sentinel

Thanks for looking at this. The project is Apache-2.0 and built to be
extended — the Tier 3 hooks exist precisely so you don't have to fork it.

## Dev setup

```bash
make install          # python3 -m venv .venv + editable install with dev extras
make lint             # ruff check + ruff format --check (src, tests, seed)
make typecheck        # mypy — must stay clean on src/sentinel
make test             # unit suite: hermetic, no DataHub or network needed
```

For the live end-to-end path (needs Docker + ~8GB free RAM):

```bash
make datahub-up       # datahub docker quickstart
make seed             # populate the demo graph (add --heal to flip it healthy)
make test-integration # RUN_INTEGRATION_TESTS=1 pytest tests/integration
```

## Ground rules (from the project's build spec — they're enforced in review)

- **Never fabricate DataHub API behavior.** Before relying on an MCP tool or
  GraphQL mutation, verify it: introspect the running MCP server
  (`DataHubClient.verify_tool_surface`), inspect the installed
  `acryl-datahub` package, or cite the docs page. Record the verification in
  ARCHITECTURE.md's "DataHub API surface" section — that file is the ledger
  of what's verified vs. assumed.
- **Mock at one boundary.** Unit tests use `tests/conftest.py`'s
  `FakeDataHubClient` — extend it (keeping method signatures mirrored to
  `DataHubClient`) rather than ad-hoc mocking per test file.
- **Every DataHub write is logged** with enough detail to reconstruct what
  happened and why. No bare `except:`; every swallowed exception is logged.
- **Type hints everywhere in `core/` and agents**; docstrings explain *why*.
- One logical change per commit, with a message that explains it.

## Extending Sentinel

- **New notification channel**: implement
  `integrations/notifiers/base.py::NotifierPlugin` (two methods). `jira.py`
  and `teams.py` are documented stubs showing exactly what a real
  implementation needs. Wire it into the CLI's notifier lists.
- **New metadata source**: implement
  `integrations/connectors/base.py::ConnectorPlugin`;
  `example_stub.py` (CSV directory) is the worked pattern. Production-grade
  connectors belong upstream in DataHub's ingestion framework — see the
  `datahub-connector-planning` skill.
- **New quality check type**: add it to `agents/quality_checker/checker.py`
  (config model validator, both evaluators, the assertion mapping in
  `DataHubClient.write_assertion_result`) and cover both modes in tests.
- **New severity rule inputs**: extend `SeverityContext` and the whitelisted
  operators in `core/incident_engine.py` deliberately — the YAML rules file
  must never become an eval() vector.

## Reporting problems

Open a GitHub issue with the failing command, expected vs. actual, and (for
live-DataHub issues) the output of `datahub docker check`.
