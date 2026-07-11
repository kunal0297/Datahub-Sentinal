"""Thin GitHub REST API v3 wrapper for PR Impact Analysis: listing a PR's
changed files and posting/updating its Sentinel comment. GitHub's REST API
is stable, long-standing, and well-documented — unlike the DataHub surface
this project treats so carefully, there's no version-drift risk here worth
a verification note.
"""

from __future__ import annotations

import base64
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

    # ------------------------------------------------------------------ #
    # Branch + PR creation — used by the Migration Copilot's real-GitHub
    # mode (agents/migration_copilot/pr_writer.py). Not exercised by the
    # self-contained demo, which uses LocalPatchWriter instead (see that
    # module's docstring) since opening a live PR needs a real external
    # repo + token, which Section 3's demo constraint rules out.
    # ------------------------------------------------------------------ #

    def get_branch_sha(self, branch: str) -> str:
        resp = self._http.get(f"/repos/{self.repo}/git/ref/heads/{branch}")
        resp.raise_for_status()
        return str(resp.json()["object"]["sha"])

    def create_branch(self, branch: str, from_sha: str) -> dict:
        resp = self._http.post(
            f"/repos/{self.repo}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": from_sha},
        )
        resp.raise_for_status()
        return resp.json()

    def create_or_update_file(self, path: str, content: str, branch: str, message: str) -> dict:
        """PUT contents API: creates the file if absent, updates it (using
        its current sha) if present."""
        existing_sha = None
        existing = self._http.get(f"/repos/{self.repo}/contents/{path}", params={"ref": branch})
        if existing.status_code == 200:
            existing_sha = existing.json().get("sha")

        body = {
            "message": message,
            "content": base64.b64encode(content.encode()).decode(),
            "branch": branch,
        }
        if existing_sha:
            body["sha"] = existing_sha
        resp = self._http.put(f"/repos/{self.repo}/contents/{path}", json=body)
        resp.raise_for_status()
        return resp.json()

    def create_pull_request(self, title: str, head: str, base: str, body: str) -> dict:
        resp = self._http.post(
            f"/repos/{self.repo}/pulls",
            json={"title": title, "head": head, "base": base, "body": body},
        )
        resp.raise_for_status()
        return resp.json()

    def get_pull_request(self, pr_number: int) -> dict:
        """Used by the Migration Copilot's tracker to refresh status (has a
        previously-opened PR merged yet?) without a webhook listener."""
        resp = self._http.get(f"/repos/{self.repo}/pulls/{pr_number}")
        resp.raise_for_status()
        return resp.json()
