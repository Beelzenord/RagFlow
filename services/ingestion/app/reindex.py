"""How far "Re-index all" has got, for the Dashboard.

A document keeps its status while it is re-indexed, because it goes on answering
from its old chunks, so the table alone cannot show that anything happened.

Kept in memory: ingestion runs as one replica and the run lives in its
background task, so a restart ends both together. What lasts is in
ingestion_jobs, one row per document rebuilt.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class ReindexRun:
    total: int
    rebuilt: int = 0
    # Nothing to rebuild from, or renamed meanwhile (the rename queues its own).
    skipped: int = 0
    failed: list[str] = field(default_factory=list)
    finished_at: datetime | None = None

    @property
    def handled(self) -> int:
        return self.rebuilt + self.skipped + len(self.failed)

    def finish(self) -> None:
        self.finished_at = datetime.now(timezone.utc)

    def as_dict(self) -> dict[str, Any]:
        return {
            "running": self.finished_at is None,
            "total": self.total,
            "done": self.handled,
            "rebuilt": self.rebuilt,
            "skipped": self.skipped,
            "failed": self.failed,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


_current: ReindexRun | None = None


def start(total: int) -> ReindexRun | None:
    """The new run, or None while one is going: a second would only embed
    everything twice."""
    global _current
    if _current is not None and _current.finished_at is None:
        return None
    _current = ReindexRun(total=total)
    return _current


def status() -> dict[str, Any]:
    """The latest run. Nothing since the last restart reads as not running."""
    return _current.as_dict() if _current else {"running": False, "total": 0}
