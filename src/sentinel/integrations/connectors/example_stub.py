"""The worked ConnectorPlugin example: a local CSV directory.

Deliberately trivial — the point is the *pattern*, not the integration.
Reading this file should tell you exactly what a real connector needs:

1. `classify_source()` — say what the source is in metamodel terms.
2. `extract()` — walk the source, and for each discovered entity yield
   (a) a `DatasetPropertiesClass` (name + description) and
   (b) a `SchemaMetadataClass` (columns), each wrapped in a
   `MetadataChangeProposalWrapper` bound to the entity's URN.

A real connector for, say, an internal service registry swaps the
directory walk for API calls and adds whatever aspects the source can
support (ownership, lineage, tags). For anything production-grade, build a
proper `datahub.ingestion.api.source.Source` instead and contribute it
upstream — DataHub's `datahub-connector-planning` skill walks through that
process.
"""

from __future__ import annotations

import csv
import logging
from collections.abc import Iterator
from pathlib import Path

import datahub.emitter.mce_builder as builder
import datahub.metadata.schema_classes as models
from datahub.emitter.mcp import MetadataChangeProposalWrapper

from sentinel.integrations.connectors.base import ConnectorPlugin

logger = logging.getLogger(__name__)


class CsvDirectoryConnector(ConnectorPlugin):
    """Each `*.csv` file in `directory` becomes one DataHub dataset on the
    `file` platform; the header row becomes its schema (all columns typed
    as strings — a CSV genuinely doesn't know better, and guessing types
    from sampled values is real connector work, out of scope for a stub)."""

    name = "csv-directory"

    def __init__(self, directory: str | Path, env: str = "PROD"):
        self.directory = Path(directory)
        self.env = env

    def classify_source(self) -> str:
        return (
            f"file-based tabular source ({self.directory}) -> one dataset entity "
            "per CSV file, header row as schemaMetadata, no lineage/ownership signals"
        )

    def extract(self) -> Iterator[MetadataChangeProposalWrapper]:
        for csv_path in sorted(self.directory.glob("*.csv")):
            try:
                with open(csv_path, newline="") as f:
                    header = next(csv.reader(f), [])
            except (OSError, csv.Error) as exc:
                logger.warning("skipping unreadable CSV %s: %s", csv_path, exc)
                continue

            dataset_name = csv_path.stem
            urn = builder.make_dataset_urn(platform="file", name=dataset_name, env=self.env)

            yield MetadataChangeProposalWrapper(
                entityUrn=urn,
                aspect=models.DatasetPropertiesClass(
                    name=dataset_name,
                    description=f"CSV file ingested from {csv_path.name} by the "
                    f"{self.name} connector stub.",
                ),
            )
            yield MetadataChangeProposalWrapper(
                entityUrn=urn,
                aspect=models.SchemaMetadataClass(
                    schemaName=dataset_name,
                    platform=builder.make_data_platform_urn("file"),
                    version=0,
                    hash="",
                    platformSchema=models.OtherSchemaClass(rawSchema=""),
                    fields=[
                        models.SchemaFieldClass(
                            fieldPath=column,
                            type=models.SchemaFieldDataTypeClass(type=models.StringTypeClass()),
                            nativeDataType="string",
                        )
                        for column in header
                        if column
                    ],
                ),
            )
