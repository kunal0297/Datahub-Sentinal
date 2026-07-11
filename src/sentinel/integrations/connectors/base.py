"""ConnectorPlugin — the Tier 3 extension hook for adding a new custom
metadata source to Sentinel's demo/seed pipeline.

This follows the same three-step shape DataHub's own
`datahub-connector-planning` skill (github.com/datahub-project/datahub-skills)
recommends for planning a real ingestion source: classify the source, map
its entities to the DataHub metamodel, emit MetadataChangeProposals. A real
production connector belongs in DataHub's ingestion framework
(`datahub.ingestion.api.source.Source`), not here — this hook exists so a
team extending Sentinel can feed a bespoke source (an internal registry, a
CSV drop, a niche vendor API) into the same demo graph Sentinel's agents
operate on, without forking the seed script.

One deliberate deviation from the original project spec: `extract()` yields
the DataHub SDK's `MetadataChangeProposalWrapper`, not
`sentinel.core.models.MetadataChangeProposal`. The latter is Sentinel's
human-gated *metadata edit proposal* (see core/proposal_engine.py) — a
different concept that happens to share a name. Connectors emit ingestion
metadata directly; routing bulk ingestion through a human-approval queue
would be both wrong and unusable. Noted in ARCHITECTURE.md.

See `example_stub.py` for a worked, deliberately trivial implementation
(a local CSV directory) showing exactly what to fill in.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from datahub.emitter.mcp import MetadataChangeProposalWrapper


class ConnectorPlugin(ABC):
    """Implement this to add a new custom metadata source.

    Lifecycle: Sentinel (or your own runner script) instantiates the
    connector, logs `classify_source()`, then emits everything `extract()`
    yields through a `DatahubRestEmitter` — see `run_connector` below for
    the reference runner.
    """

    name: str  # short id, e.g. "csv-directory", "internal-registry"

    @abstractmethod
    def classify_source(self) -> str:
        """One line describing what kind of source this is and how it maps
        to the DataHub metamodel (e.g. "file-based tabular source ->
        dataset entities with schemaMetadata"). The connector-planning
        skill's first step; also what shows up in logs before emission."""
        ...

    @abstractmethod
    def extract(self) -> Iterator[MetadataChangeProposalWrapper]:
        """Yield one or more MetadataChangeProposalWrappers per discovered
        entity. Yield rather than return a list: real sources can be large,
        and the emitter consumes lazily."""
        ...


def run_connector(connector: ConnectorPlugin, emitter) -> int:
    """Reference runner: classify, extract, emit, count. `emitter` is
    anything with `emit_mcp` (a `DatahubRestEmitter` in production, a
    recording fake in tests)."""
    import logging

    logger = logging.getLogger(__name__)
    logger.info("connector %s: %s", connector.name, connector.classify_source())
    emitted = 0
    for mcp in connector.extract():
        emitter.emit_mcp(mcp)
        emitted += 1
    logger.info("connector %s: emitted %d aspect(s)", connector.name, emitted)
    return emitted
