"""Central settings, read from environment variables / a `.env` file.

Never hardcode secrets or model IDs here — see `.env.example` for the full
set of variables this reads and why each exists.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # DataHub
    datahub_gms_url: str = "http://localhost:8080"
    datahub_frontend_url: str = "http://localhost:9002"
    datahub_gms_token: str = ""
    tools_is_mutation_enabled: bool = True

    # Anthropic
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"

    # GitHub
    github_token: str = ""

    # Notifiers
    slack_webhook_url: str = ""
    slack_bot_token: str = ""
    slack_channel: str = "#data-incidents"

    # Behavior
    sentinel_severity_rules_path: str = "config/severity_rules.yml"
    sentinel_quality_checks_path: str = "quality_checks.yml"
    sentinel_lineage_hop_limit: int = 3
    # Off by default: a hackathon demo of a tool that blocks merges by
    # default reads as hostile, not helpful (per spec Section 5.1 step 7).
    # Opt in via the Action's `block_on_critical` input / this env var.
    sentinel_block_on_critical: bool = False


def get_settings() -> Settings:
    """Not cached: tests and CLI commands frequently monkeypatch env vars
    between calls, and settings construction is cheap."""
    return Settings()
