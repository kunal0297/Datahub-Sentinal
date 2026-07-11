"""Sentinel's own pending/accepted/rejected lifecycle for metadata change
proposals — see ARCHITECTURE.md "Known gap" for why this exists instead of
delegating to a DataHub primitive: no propose*/accept*/reject*-proposal
mutation exists anywhere in open-source DataHub's verified GraphQL/MCP
surface. This engine owns that state (a JSON file, `proposals.json` by
default — inspectable, diffable, no database needed for a hackathon demo)
and only calls a real DataHub write once a human explicitly accepts.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from sentinel.core.models import MetadataChangeProposal, ProposalStatus

logger = logging.getLogger(__name__)


class ProposalBackend(Protocol):
    """The DataHub writes a proposal can resolve into once accepted."""

    async def update_description(
        self, urn: str, description: str, field_path: str | None = None
    ) -> Any: ...
    async def add_tags(self, urn: str, tags: list[str], field_path: str | None = None) -> Any: ...
    async def add_terms(self, urn: str, terms: list[str], field_path: str | None = None) -> Any: ...

    def update_deprecation(
        self, urn: str, deprecated: bool, note: str, replacement_urn: str | None = None
    ) -> bool: ...


class ProposalEngine:
    def __init__(self, store_path: Path):
        self.store_path = store_path
        self._proposals: dict[str, MetadataChangeProposal] = {}
        self._load()

    def _load(self) -> None:
        if not self.store_path.exists():
            return
        data = json.loads(self.store_path.read_text())
        self._proposals = {p["id"]: MetadataChangeProposal(**p) for p in data}

    def _save(self) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store_path.write_text(
            json.dumps(
                [json.loads(p.model_dump_json()) for p in self._proposals.values()], indent=2
            )
        )

    def submit(
        self,
        target_urn: str,
        change_type: str,
        proposed_value: str,
        rationale: str,
        source_agent: str,
        field_path: str | None = None,
    ) -> MetadataChangeProposal:
        proposal = MetadataChangeProposal(
            id=str(uuid.uuid4())[:8],
            target_urn=target_urn,
            change_type=change_type,
            field_path=field_path,
            proposed_value=proposed_value,
            rationale=rationale,
            source_agent=source_agent,
        )
        self._proposals[proposal.id] = proposal
        self._save()
        logger.info(
            "submitted pending proposal %s for %s (%s) -- awaiting human review",
            proposal.id,
            target_urn,
            change_type,
        )
        return proposal

    def list_pending(self, target_urn: str | None = None) -> list[MetadataChangeProposal]:
        return [
            p
            for p in self._proposals.values()
            if p.status == ProposalStatus.PENDING
            and (target_urn is None or p.target_urn == target_urn)
        ]

    def get(self, proposal_id: str) -> MetadataChangeProposal | None:
        return self._proposals.get(proposal_id)

    def _require_pending(self, proposal_id: str) -> MetadataChangeProposal:
        proposal = self._proposals.get(proposal_id)
        if proposal is None:
            raise KeyError(f"no proposal {proposal_id!r}")
        if proposal.status != ProposalStatus.PENDING:
            raise ValueError(
                f"proposal {proposal_id!r} was already decided ({proposal.status.value})"
            )
        return proposal

    async def accept(
        self, proposal_id: str, client: ProposalBackend, decided_by: str
    ) -> MetadataChangeProposal:
        """The only place a proposal's content actually reaches DataHub."""
        proposal = self._require_pending(proposal_id)

        if proposal.change_type == "description":
            await client.update_description(proposal.target_urn, proposal.proposed_value)
        elif proposal.change_type == "column_description":
            await client.update_description(
                proposal.target_urn, proposal.proposed_value, field_path=proposal.field_path
            )
        elif proposal.change_type == "tags":
            tags = [t.strip() for t in proposal.proposed_value.split(",") if t.strip()]
            await client.add_tags(proposal.target_urn, tags, field_path=proposal.field_path)
        elif proposal.change_type == "terms":
            terms = [t.strip() for t in proposal.proposed_value.split(",") if t.strip()]
            await client.add_terms(proposal.target_urn, terms, field_path=proposal.field_path)
        elif proposal.change_type == "deprecation":
            client.update_deprecation(
                proposal.target_urn, deprecated=True, note=proposal.proposed_value
            )
        else:
            raise ValueError(f"unknown proposal change_type {proposal.change_type!r}")

        proposal.status = ProposalStatus.ACCEPTED
        proposal.decided_by = decided_by
        proposal.decided_at = datetime.now(UTC)
        self._save()
        logger.info(
            "accepted proposal %s -> wrote %s to DataHub", proposal_id, proposal.change_type
        )
        return proposal

    def reject(self, proposal_id: str, decided_by: str) -> MetadataChangeProposal:
        proposal = self._require_pending(proposal_id)
        proposal.status = ProposalStatus.REJECTED
        proposal.decided_by = decided_by
        proposal.decided_at = datetime.now(UTC)
        self._save()
        logger.info("rejected proposal %s -- no DataHub write performed", proposal_id)
        return proposal
