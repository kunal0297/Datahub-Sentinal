FROM python:3.12-slim

# Dependencies are pinned to match pyproject.toml's [project.dependencies].
# They're listed here (rather than COPYing the repo root's pyproject.toml)
# because a Docker container action's build context is scoped to its own
# directory (this one) — it cannot reach the repo root at build time. See
# entrypoint.sh for how the sentinel package itself gets installed.
RUN pip install --no-cache-dir \
    "acryl-datahub>=0.15.0" \
    "mcp-server-datahub>=0.4.0" \
    "mcp>=1.2.0" \
    "httpx>=0.27.0" \
    "pydantic>=2.6.0" \
    "pydantic-settings>=2.2.0" \
    "typer>=0.12.0" \
    "anthropic>=0.40.0" \
    "PyYAML>=6.0.1" \
    "rich>=13.7.0"

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
