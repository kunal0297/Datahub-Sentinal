import json

import httpx

from sentinel.agents.pr_impact.analyzer import PR_COMMENT_MARKER
from sentinel.agents.pr_impact.github_client import GitHubClient


def _client(handler) -> GitHubClient:
    return GitHubClient("fake-token", "acme/repo", transport=httpx.MockTransport(handler))


class TestGetChangedFiles:
    def test_paginates_and_excludes_removed_files(self):
        def handler(request: httpx.Request) -> httpx.Response:
            page = int(request.url.params.get("page", "1"))
            if page == 1:
                body = [{"filename": f"models/f{i}.sql", "status": "modified"} for i in range(100)]
                return httpx.Response(200, json=body)
            return httpx.Response(
                200,
                json=[
                    {"filename": "models/last.sql", "status": "modified"},
                    {"filename": "models/deleted.sql", "status": "removed"},
                ],
            )

        client = _client(handler)
        files = client.get_changed_files(42)
        assert len(files) == 101
        assert "models/last.sql" in files
        assert "models/deleted.sql" not in files


class TestUpsertPrComment:
    def test_creates_when_no_existing_comment(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.method)
            if request.method == "GET":
                return httpx.Response(200, json=[])
            assert request.method == "POST"
            body = json.loads(request.read())
            assert PR_COMMENT_MARKER in body["body"]
            return httpx.Response(201, json={"id": 1, "body": body["body"]})

        client = _client(handler)
        result = client.upsert_pr_comment(7, f"hello\n{PR_COMMENT_MARKER}")
        assert calls == ["GET", "POST"]
        assert result["id"] == 1

    def test_updates_existing_comment_instead_of_creating_new_one(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.method)
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json=[
                        {"id": 99, "body": f"stale\n{PR_COMMENT_MARKER}"},
                    ],
                )
            assert request.method == "PATCH"
            assert str(request.url).endswith("/repos/acme/repo/issues/comments/99")
            return httpx.Response(200, json={"id": 99, "body": "updated"})

        client = _client(handler)
        result = client.upsert_pr_comment(7, f"fresh content\n{PR_COMMENT_MARKER}")
        assert calls == ["GET", "PATCH"]
        assert result["id"] == 99

    def test_ignores_comments_without_marker_when_searching(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json=[{"id": 1, "body": "an unrelated human comment"}])
            assert request.method == "POST"
            return httpx.Response(201, json={"id": 2})

        client = _client(handler)
        result = client.upsert_pr_comment(7, f"content\n{PR_COMMENT_MARKER}")
        assert result["id"] == 2


class TestBranchAndPrCreation:
    def test_get_branch_sha(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert str(request.url).endswith("/repos/acme/repo/git/ref/heads/main")
            return httpx.Response(200, json={"object": {"sha": "abc123"}})

        client = _client(handler)
        assert client.get_branch_sha("main") == "abc123"

    def test_create_branch_posts_ref(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.read())
            assert body == {"ref": "refs/heads/sentinel/migrate-x", "sha": "abc123"}
            return httpx.Response(201, json={"ref": body["ref"]})

        client = _client(handler)
        client.create_branch("sentinel/migrate-x", "abc123")

    def test_create_or_update_file_creates_when_absent(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.method)
            if request.method == "GET":
                return httpx.Response(404)
            assert request.method == "PUT"
            body = json.loads(request.read())
            assert "sha" not in body
            assert body["branch"] == "sentinel/migrate-x"
            return httpx.Response(201, json={"content": {"sha": "new-sha"}})

        client = _client(handler)
        client.create_or_update_file(
            "models/f.sql", "select 1", branch="sentinel/migrate-x", message="msg"
        )
        assert calls == ["GET", "PUT"]

    def test_create_or_update_file_includes_sha_when_present(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json={"sha": "existing-sha"})
            body = json.loads(request.read())
            assert body["sha"] == "existing-sha"
            return httpx.Response(200, json={"content": {"sha": "updated-sha"}})

        client = _client(handler)
        client.create_or_update_file(
            "models/f.sql", "select 1", branch="sentinel/migrate-x", message="msg"
        )

    def test_create_pull_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.read())
            assert body["head"] == "sentinel/migrate-x"
            assert body["base"] == "main"
            return httpx.Response(201, json={"html_url": "https://github.com/acme/repo/pull/7"})

        client = _client(handler)
        pr = client.create_pull_request("title", "sentinel/migrate-x", "main", "body")
        assert pr["html_url"] == "https://github.com/acme/repo/pull/7"

    def test_get_pull_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert str(request.url).endswith("/repos/acme/repo/pulls/42")
            return httpx.Response(200, json={"merged": True})

        client = _client(handler)
        assert client.get_pull_request(42)["merged"] is True
