"""Turns a rewritten consumer file into a reviewable change — a local unified
diff by default (the self-contained demo path; see `LocalPatchWriter`), or a
real GitHub PR when a live repo + token are configured (`GitHubPRWriter`).

Default is one PR (or one patch) per consumer file, not one per repo — per
spec 5.2 step 5, this is what's configurable and what's most reviewable;
"one PR per repo" would bundle unrelated consumers into a single diff a
reviewer can't reason about column-by-column.
"""

from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass
class ConsumerChange:
    consumer_urn: str
    file_path: str
    original_content: str
    rewritten_content: str


@dataclass
class PRRecord:
    consumer_urn: str
    file_path: str
    status: str  # "patch_generated" | "pr_opened" | "merged" | "verified"
    link: str | None = None  # patch file path (local mode) or PR URL (real mode)


def render_unified_diff(file_path: str, original_content: str, rewritten_content: str) -> str:
    """A real, `git apply`-able unified diff — this is the artifact the
    self-contained demo actually produces per consumer file, since opening a
    live GitHub PR isn't possible without a real external repo/token (see
    the project's self-contained-demo constraint)."""
    diff_lines = difflib.unified_diff(
        original_content.splitlines(keepends=True),
        rewritten_content.splitlines(keepends=True),
        fromfile=f"a/{file_path}",
        tofile=f"b/{file_path}",
    )
    return "".join(diff_lines)


class LocalPatchWriter:
    """Self-contained mode: writes one `.patch` file per consumer under
    `output_dir`. No real GitHub repo or token required — this is what
    `sentinel migrate` uses by default, and what the seeded demo exercises."""

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def write(self, change: ConsumerChange) -> PRRecord:
        diff = render_unified_diff(
            change.file_path, change.original_content, change.rewritten_content
        )
        safe_name = change.file_path.replace("/", "__")
        patch_path = self.output_dir / f"{safe_name}.patch"
        patch_path.write_text(diff)
        logger.info("wrote patch for %s -> %s", change.file_path, patch_path)
        return PRRecord(
            consumer_urn=change.consumer_urn,
            file_path=change.file_path,
            status="patch_generated",
            link=str(patch_path),
        )


class GitHubBackend(Protocol):
    def get_branch_sha(self, branch: str) -> str: ...
    def create_branch(self, branch: str, from_sha: str) -> dict: ...
    def create_or_update_file(self, path: str, content: str, branch: str, message: str) -> dict: ...
    def create_pull_request(self, title: str, head: str, base: str, body: str) -> dict: ...


class GitHubPRWriter:
    """Real mode: opens an actual PR against a GitHub repo via the REST API.
    Requires a real repo + a token with contents:write and
    pull_requests:write. Not exercised by the self-contained demo (see
    `LocalPatchWriter`) — this needs a live external repo the judge-runnable
    demo environment can't assume access to."""

    def __init__(self, github_client: GitHubBackend, base_branch: str = "main"):
        self.github = github_client
        self.base_branch = base_branch

    def write(self, change: ConsumerChange) -> PRRecord:
        branch_name = f"sentinel/migrate-{change.file_path.replace('/', '-')}"
        base_sha = self.github.get_branch_sha(self.base_branch)
        self.github.create_branch(branch_name, base_sha)
        self.github.create_or_update_file(
            change.file_path,
            change.rewritten_content,
            branch=branch_name,
            message=f"sentinel: migrate {change.file_path}",
        )
        pr = self.github.create_pull_request(
            title=f"Sentinel migration: {change.file_path}",
            head=branch_name,
            base=self.base_branch,
            body="Automated migration by DataHub Sentinel's Schema Migration Copilot.",
        )
        return PRRecord(
            consumer_urn=change.consumer_urn,
            file_path=change.file_path,
            status="pr_opened",
            link=pr["html_url"],
        )
