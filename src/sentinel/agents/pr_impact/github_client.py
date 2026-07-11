"""Thin GitHub REST API v3 wrapper for PR Impact Analysis: listing a PR's
changed files and posting/updating its Sentinel comment. GitHub's REST API
is stable, long-standing, and well-documented — unlike the DataHub surface
this project treats so carefully, there's no version-drift risk here worth
a verification note.
"""

from __future__ import annotations

import logging

import httpx

from sentinel.agents.pr_impact.analyzer import PR_COMMENT_MARKER

logger = logging.getLogger(__name__)


class GitHubClient:
    def __init__(
        self,
        token: str,
        repo: str,
        base_url: str = "https://api.github.com",
        transport: httpx.BaseTransport | None = None,
    ):
        """`repo` is `"owner/name"`. `transport` is a test-only seam (inject
        `httpx.MockTransport`); production callers never pass it."""
        self.repo = repo
        self._http = httpx.Client(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get_changed_files(self, pr_number: int) -> list[str]:
        """Paginates through `GET /pulls/{n}/files` (GitHub caps each page
        at 100 entries) and returns every changed file's path."""
        paths: list[str] = []
        page = 1
        while True:
            resp = self._http.get(
                f"/repos/{self.repo}/pulls/{pr_number}/files",
                params={"per_page": 100, "page": page},
            )
            resp.raise_for_status()
            batch = resp.json()
            paths.extend(f["filename"] for f in batch if f.get("status") != "removed")
            if len(batch) < 100:
                break
            page += 1
        return paths

    def _find_existing_comment(self, pr_number: int) -> dict | None:
        resp = self._http.get(
            f"/repos/{self.repo}/issues/{pr_number}/comments", params={"per_page": 100}
        )
        resp.raise_for_status()
        for comment in resp.json():
            if PR_COMMENT_MARKER in comment.get("body", ""):
                return comment
        return None

    def upsert_pr_comment(self, pr_number: int, body: str) -> dict:
        """Updates Sentinel's existing comment on this PR if one exists
        (identified by `PR_COMMENT_MARKER`), otherwise creates one. This is
        what keeps re-runs (new pushes to the same PR) from spamming the
        thread with a fresh comment every time."""
        existing = self._find_existing_comment(pr_number)
        if existing:
            resp = self._http.patch(
                f"/repos/{self.repo}/issues/comments/{existing['id']}", json={"body": body}
            )
            action = "updated"
        else:
            resp = self._http.post(
                f"/repos/{self.repo}/issues/{pr_number}/comments", json={"body": body}
            )
            action = "created"
        resp.raise_for_status()
        logger.info("%s PR comment on %s#%s", action, self.repo, pr_number)
        return resp.json()

    def get_file_content(self, path: str, ref: str) -> str:
        resp = self._http.get(
            f"/repos/{self.repo}/contents/{path}",
            params={"ref": ref},
            headers={"Accept": "application/vnd.github.raw+json"},
        )
        resp.raise_for_status()
        return resp.text
