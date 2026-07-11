"""Tests for the Tier 3 connector hook's worked example. Light by design —
the deliverable is the pattern's clarity, but the stub still has to
actually work, or it teaches the wrong lesson."""

from __future__ import annotations

from sentinel.integrations.connectors.base import run_connector
from sentinel.integrations.connectors.example_stub import CsvDirectoryConnector


class RecordingEmitter:
    def __init__(self):
        self.mcps = []

    def emit_mcp(self, mcp) -> None:
        mcp.make_mcp()  # force full serialization, same trick the seed check uses
        self.mcps.append(mcp)


def test_csv_directory_yields_properties_and_schema(tmp_path):
    (tmp_path / "customers.csv").write_text("customer_id,email\n1,a@example.com\n")
    (tmp_path / "orders.csv").write_text("order_id,amount\n1,10.0\n")

    connector = CsvDirectoryConnector(tmp_path)
    emitter = RecordingEmitter()
    emitted = run_connector(connector, emitter)

    assert emitted == 4  # 2 files x (properties + schema)
    urns = {mcp.entityUrn for mcp in emitter.mcps}
    assert urns == {
        "urn:li:dataset:(urn:li:dataPlatform:file,customers,PROD)",
        "urn:li:dataset:(urn:li:dataPlatform:file,orders,PROD)",
    }
    schema_aspects = [m.aspect for m in emitter.mcps if hasattr(m.aspect, "fields")]
    field_paths = {f.fieldPath for aspect in schema_aspects for f in aspect.fields}
    assert field_paths == {"customer_id", "email", "order_id", "amount"}


def test_unreadable_csv_is_skipped_not_fatal(tmp_path):
    (tmp_path / "good.csv").write_text("a,b\n1,2\n")
    (tmp_path / "empty.csv").write_text("")

    connector = CsvDirectoryConnector(tmp_path)
    emitter = RecordingEmitter()
    emitted = run_connector(connector, emitter)

    # empty.csv has no header row -> empty schema, but doesn't crash the run
    assert emitted >= 2
    assert "tabular source" in connector.classify_source()
