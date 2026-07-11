from pathlib import Path

from sentinel.agents.migration_copilot.pr_writer import PRRecord
from sentinel.agents.migration_copilot.tracker import MigrationTracker

OLD_URN = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.orders_v1,PROD)"
NEW_URN = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.orders_v2,PROD)"


def _tracker() -> MigrationTracker:
    return MigrationTracker(
        old_urn=OLD_URN,
        new_urn=NEW_URN,
        records=[
            PRRecord(
                consumer_urn="urn:li:dataset:(urn:li:dataPlatform:snowflake,x,PROD)",
                file_path="models/customer_revenue_summary.sql",
                status="pr_opened",
                link="https://github.com/acme/repo/pull/42",
            )
        ],
    )


class TestSaveLoadRoundTrip:
    def test_round_trips_through_json(self, tmp_path: Path):
        tracker = _tracker()
        path = tmp_path / "migration_status.json"
        tracker.save(path)

        loaded = MigrationTracker.load(path)
        assert loaded.old_urn == OLD_URN
        assert loaded.new_urn == NEW_URN
        assert len(loaded.records) == 1
        assert loaded.records[0].status == "pr_opened"
        assert loaded.records[0].link == "https://github.com/acme/repo/pull/42"


class TestRefresh:
    def test_noop_in_local_mode_without_a_github_client(self):
        tracker = _tracker()
        tracker.refresh(github_client=None)
        assert tracker.records[0].status == "pr_opened"

    def test_flips_to_merged_when_github_reports_merged(self):
        class FakeGitHub:
            def get_pull_request(self, pr_number):
                assert pr_number == 42
                return {"merged": True}

        tracker = _tracker()
        tracker.refresh(github_client=FakeGitHub())
        assert tracker.records[0].status == "merged"

    def test_stays_pr_opened_when_not_yet_merged(self):
        class FakeGitHub:
            def get_pull_request(self, pr_number):
                return {"merged": False}

        tracker = _tracker()
        tracker.refresh(github_client=FakeGitHub())
        assert tracker.records[0].status == "pr_opened"

    def test_skips_records_without_a_link(self):
        tracker = MigrationTracker(
            old_urn=OLD_URN,
            new_urn=NEW_URN,
            records=[
                PRRecord(consumer_urn="x", file_path="f.sql", status="patch_generated", link=None)
            ],
        )

        class ExplodingGitHub:
            def get_pull_request(self, pr_number):
                raise AssertionError("should not be called for a non-pr_opened record")

        tracker.refresh(github_client=ExplodingGitHub())
        assert tracker.records[0].status == "patch_generated"


class TestSummaryLines:
    def test_includes_urn_and_every_record(self):
        tracker = _tracker()
        lines = "\n".join(tracker.summary_lines())
        assert OLD_URN in lines
        assert NEW_URN in lines
        assert "customer_revenue_summary.sql" in lines
        assert "pr_opened" in lines
