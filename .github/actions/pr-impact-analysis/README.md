# PR Impact Analysis (GitHub Action)

Resolves changed `.sql` files in a pull request to DataHub dataset URNs, diffs
their schema against DataHub's current state, walks the downstream blast
radius, posts/updates a PR comment, and raises a DataHub incident on
HIGH/CRITICAL severity. See `src/sentinel/agents/pr_impact/` for the actual
logic — this directory is only the GitHub Action packaging around it.

## Usage

```yaml
on:
  pull_request:
    paths: ["**/*.sql"]

jobs:
  pr-impact-analysis:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: ./.github/actions/pr-impact-analysis
        with:
          datahub_gms_url: ${{ secrets.DATAHUB_GMS_URL }}
          datahub_gms_token: ${{ secrets.DATAHUB_GMS_TOKEN }}
          github_token: ${{ secrets.GITHUB_TOKEN }}
```

## Known limitations

- **Self-hosted-repo usage only, for now.** A GitHub Docker container
  action's build context is scoped to its own directory (this one) — it
  cannot `COPY` the repo root's `src/sentinel` at image-build time. Instead,
  `entrypoint.sh` installs the `sentinel` package from `GITHUB_WORKSPACE` at
  *container runtime*, which only contains `src/sentinel` when this action
  is invoked from within the `datahub-sentinel` repo's own workflows (its
  primary supported usage, and what the hackathon demo exercises). Making
  this a genuinely portable action for arbitrary external repos would mean
  publishing `datahub-sentinel` to PyPI and installing it at build time
  instead — noted as a roadmap item, not implemented here.
- **`block_on_critical` is off by default.** Turning it on fails this check
  (blocking merge under branch protection) on CRITICAL severity. Off by
  default deliberately — see `src/sentinel/core/config.py`.
- Only `.sql` files are analyzed (matching the seeded demo). Airflow/Prefect/
  Dagster DAG definitions mentioned in the original design are not yet
  wired into `_TRACKED_SUFFIXES` in `action_entrypoint.py`.
