from pathlib import Path

import pytest

from sentinel.core.models import ProposalStatus
from sentinel.core.proposal_engine import ProposalEngine

URN = "urn:li:dataset:(urn:li:dataPlatform:snowflake,staging.orders_cleaned,PROD)"


class TestSubmit:
    def test_submitted_proposal_is_pending_not_auto_published(self, tmp_path: Path, fake_datahub):
        """The Definition of Done's core requirement for Metadata
        Enrichment: proposals are pending, never applied blind."""
        engine = ProposalEngine(tmp_path / "proposals.json")
        proposal = engine.submit(
            target_urn=URN,
            change_type="description",
            proposed_value="Cleaned, deduplicated orders staged for the analytics mart.",
            rationale="grounded in schema + lineage",
            source_agent="metadata-enrichment",
        )
        assert proposal.status == ProposalStatus.PENDING
        assert proposal.decided_at is None
        # nothing was written to DataHub just by submitting
        assert fake_datahub.calls == []

    def test_submitted_proposal_appears_in_list_pending(self, tmp_path: Path):
        engine = ProposalEngine(tmp_path / "proposals.json")
        engine.submit(URN, "description", "desc", "rationale", "agent")
        pending = engine.list_pending()
        assert len(pending) == 1
        assert pending[0].target_urn == URN

    def test_list_pending_filters_by_target_urn(self, tmp_path: Path):
        engine = ProposalEngine(tmp_path / "proposals.json")
        engine.submit(URN, "description", "d1", "r", "agent")
        engine.submit(
            "urn:li:dataset:(urn:li:dataPlatform:x,other,PROD)", "description", "d2", "r", "agent"
        )
        assert len(engine.list_pending(target_urn=URN)) == 1


class TestPersistence:
    def test_reloading_from_disk_recovers_pending_proposals(self, tmp_path: Path):
        store = tmp_path / "proposals.json"
        engine = ProposalEngine(store)
        submitted = engine.submit(URN, "description", "desc", "rationale", "agent")

        reloaded = ProposalEngine(store)
        found = reloaded.get(submitted.id)
        assert found is not None
        assert found.status == ProposalStatus.PENDING
        assert found.proposed_value == "desc"


class TestAccept:
    @pytest.mark.asyncio
    async def test_accepting_a_description_proposal_writes_to_datahub(self, tmp_path, fake_datahub):
        engine = ProposalEngine(tmp_path / "proposals.json")
        proposal = engine.submit(URN, "description", "a good description", "rationale", "agent")

        accepted = await engine.accept(proposal.id, fake_datahub, decided_by="alice")

        assert accepted.status == ProposalStatus.ACCEPTED
        assert accepted.decided_by == "alice"
        assert accepted.decided_at is not None
        update_calls = [c for c in fake_datahub.calls if c[0] == "update_description"]
        assert len(update_calls) == 1
        assert update_calls[0][1]["urn"] == URN
        assert update_calls[0][1]["description"] == "a good description"

    @pytest.mark.asyncio
    async def test_accepting_a_column_description_proposal_passes_field_path(
        self, tmp_path, fake_datahub
    ):
        engine = ProposalEngine(tmp_path / "proposals.json")
        proposal = engine.submit(
            URN,
            "column_description",
            "the order's total",
            "rationale",
            "agent",
            field_path="total_amount",
        )
        await engine.accept(proposal.id, fake_datahub, decided_by="alice")
        update_calls = [c for c in fake_datahub.calls if c[0] == "update_description"]
        assert update_calls[0][1]["field_path"] == "total_amount"

    @pytest.mark.asyncio
    async def test_accepting_a_tags_proposal_splits_comma_separated_values(
        self, tmp_path, fake_datahub
    ):
        engine = ProposalEngine(tmp_path / "proposals.json")
        proposal = engine.submit(URN, "tags", "pii, finance", "rationale", "agent")
        await engine.accept(proposal.id, fake_datahub, decided_by="alice")
        tag_calls = [c for c in fake_datahub.calls if c[0] == "add_tags"]
        assert tag_calls[0][1]["tags"] == ["pii", "finance"]

    @pytest.mark.asyncio
    async def test_accepting_already_decided_proposal_raises(self, tmp_path, fake_datahub):
        engine = ProposalEngine(tmp_path / "proposals.json")
        proposal = engine.submit(URN, "description", "d", "r", "agent")
        await engine.accept(proposal.id, fake_datahub, decided_by="alice")
        with pytest.raises(ValueError, match="already decided"):
            await engine.accept(proposal.id, fake_datahub, decided_by="bob")

    @pytest.mark.asyncio
    async def test_accepting_unknown_proposal_raises_key_error(self, tmp_path, fake_datahub):
        engine = ProposalEngine(tmp_path / "proposals.json")
        with pytest.raises(KeyError):
            await engine.accept("ghost", fake_datahub, decided_by="alice")


class TestReject:
    def test_rejecting_does_not_write_to_datahub(self, tmp_path: Path, fake_datahub):
        engine = ProposalEngine(tmp_path / "proposals.json")
        proposal = engine.submit(URN, "description", "d", "r", "agent")

        rejected = engine.reject(proposal.id, decided_by="bob")

        assert rejected.status == ProposalStatus.REJECTED
        assert rejected.decided_by == "bob"
        assert fake_datahub.calls == []

    def test_rejected_proposal_no_longer_pending(self, tmp_path: Path):
        engine = ProposalEngine(tmp_path / "proposals.json")
        proposal = engine.submit(URN, "description", "d", "r", "agent")
        engine.reject(proposal.id, decided_by="bob")
        assert engine.list_pending() == []
