from sentinel.core.config import Settings


def test_settings_defaults_require_no_env(monkeypatch):
    for var in list(Settings.model_fields):
        monkeypatch.delenv(var.upper(), raising=False)
    settings = Settings(_env_file=None)
    assert settings.datahub_gms_url == "http://localhost:8080"
    assert settings.anthropic_model == "claude-opus-4-8"
    assert settings.sentinel_lineage_hop_limit == 3


def test_settings_reads_env_override(monkeypatch):
    monkeypatch.setenv("DATAHUB_GMS_URL", "http://datahub-gms:8080")
    monkeypatch.setenv("SENTINEL_LINEAGE_HOP_LIMIT", "5")
    settings = Settings(_env_file=None)
    assert settings.datahub_gms_url == "http://datahub-gms:8080"
    assert settings.sentinel_lineage_hop_limit == 5
