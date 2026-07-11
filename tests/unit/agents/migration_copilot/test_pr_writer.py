from pathlib import Path

from sentinel.agents.migration_copilot.pr_writer import (
    ConsumerChange,
    GitHubPRWriter,
    LocalPatchWriter,
    render_unified_diff,
)


class TestRenderUnifiedDiff:
    def test_produces_a_real_unified_diff(self):
        diff = render_unified_diff(
            "models/orders_v1.sql",
            "select total_amount from orders_v1\n",
            "select total_amount_usd from orders_v2\n",
        )
        assert "--- a/models/orders_v1.sql" in diff
        assert "+++ b/models/orders_v1.sql" in diff
        assert "-select total_amount from orders_v1" in diff
        assert "+select total_amount_usd from orders_v2" in diff

    def test_identical_content_produces_empty_diff(self):
        diff = render_unified_diff("f.sql", "select 1\n", "select 1\n")
        assert diff == ""


class TestLocalPatchWriter:
    def test_writes_a_patch_file_per_consumer(self, tmp_path: Path):
        writer = LocalPatchWriter(tmp_path / "migration_output")
        change = ConsumerChange(
            consumer_urn="urn:li:dataset:(urn:li:dataPlatform:snowflake,x,PROD)",
            file_path="models/customer_revenue_summary.sql",
            original_content="select total_amount from orders_v1\n",
            rewritten_content="select total_amount_usd from orders_v2\n",
        )
        record = writer.write(change)

        assert record.status == "patch_generated"
        assert record.link is not None
        patch_path = Path(record.link)
        assert patch_path.exists()
        content = patch_path.read_text()
        assert "total_amount_usd" in content
        assert "models__customer_revenue_summary.sql.patch" == patch_path.name

    def test_creates_output_dir_if_missing(self, tmp_path: Path):
        target = tmp_path / "nested" / "output"
        assert not target.exists()
        LocalPatchWriter(target)
        assert target.exists()


class TestGitHubPRWriter:
    def test_opens_branch_commits_and_opens_pr(self):
        calls = []

        class FakeGitHub:
            def get_branch_sha(self, branch):
                calls.append(("get_branch_sha", branch))
                return "base-sha-123"

            def create_branch(self, branch, from_sha):
                calls.append(("create_branch", branch, from_sha))
                return {}

            def create_or_update_file(self, path, content, branch, message):
                calls.append(("create_or_update_file", path, branch))
                return {}

            def create_pull_request(self, title, head, base, body):
                calls.append(("create_pull_request", head, base))
                return {"html_url": "https://github.com/acme/repo/pull/99"}

        writer = GitHubPRWriter(FakeGitHub(), base_branch="main")
        change = ConsumerChange(
            consumer_urn="urn:li:dataset:(urn:li:dataPlatform:snowflake,x,PROD)",
            file_path="models/customer_revenue_summary.sql",
            original_content="old",
            rewritten_content="new",
        )
        record = writer.write(change)

        assert record.status == "pr_opened"
        assert record.link == "https://github.com/acme/repo/pull/99"
        assert calls[0] == ("get_branch_sha", "main")
        assert calls[1][0] == "create_branch"
        assert calls[2][0] == "create_or_update_file"
        assert calls[3][0] == "create_pull_request"
