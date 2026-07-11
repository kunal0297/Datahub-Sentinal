"""Migration tracking view: one JSON file (default `migration_status.json`)
listing every consumer of a migration, its PR/patch link, and status
(pending/pr_opened/merged/verified). `sentinel migrate status --from
<old_urn>` reloads this file, refreshes real-GitHub statuses if a
GitHubClient is available, and reprints it — the spec's stated floor for a
Tier 1 feature ("don't leave the tracker unusably manual") without building
a full webhook listener, which is explicitly out of scope for now.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

from sentinel.agents.migration_copilot.pr_writer import PRRecord

logger = logging.getLogger(__name__)


class PullRequestBackend(Protocol):
    def get_pull_request(self, pr_number: int) -> dict: ...


def _pr_number_from_url(url: str) -> int | None:
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


@dataclass
class MigrationTracker:
    old_urn: str
    new_urn: str
    records: list[PRRecord] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> MigrationTracker:
        data = json.loads(path.read_text())
        return cls(
            old_urn=data["old_urn"],
            new_urn=data["new_urn"],
            records=[PRRecord(**r) for r in data["records"]],
        )

    def save(self, path: Path) -> None:
        path.write_text(
            json.dumps(
                {
                    "old_urn": self.old_urn,
                    "new_urn": self.new_urn,
                    "records": [asdict(r) for r in self.records],
                },
                indent=2,
            )
        )

    def refresh(self, github_client: PullRequestBackend | None = None) -> None:
        """No-op in local-patch mode (nothing live to check). In real-GitHub
        mode, checks every `pr_opened` record and flips it to `merged` once
        GitHub reports the PR merged."""
        if github_client is None:
            return
        for record in self.records:
            if record.status != "pr_opened" or not record.link:
                continue
            pr_number = _pr_number_from_url(record.link)
            if pr_number is None:
                continue
            pr = github_client.get_pull_request(pr_number)
            if pr.get("merged"):
                record.status = "merged"
                logger.info("consumer %s merged (PR #%s)", record.file_path, pr_number)

    def summary_lines(self) -> list[str]:
        lines = [f"Migration: {self.old_urn} -> {self.new_urn}", ""]
        for r in self.records:
            lines.append(f"  [{r.status:>16}] {r.file_path}  ({r.consumer_urn})  -> {r.link}")
        return lines
