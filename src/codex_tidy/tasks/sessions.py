"""Archive old session transcripts out of the hot path.

Sessions are moved, never deleted, and the database row is repointed at the new
location in the same journalled run so disk and database cannot drift apart.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from ..db import quote_identifier
from ..env import canonical, human_bytes
from ..model import (
    LEVEL_INFO,
    LEVEL_NOTICE,
    LEVEL_WARN,
    OP_MOVE_PATH,
    OP_SQL_UPDATE,
    Finding,
    Operation,
    operation_key,
)
from .base import Context, Task

REQUIRED_COLUMNS = ("id", "title", "rollout_path")


@dataclass(frozen=True)
class Candidate:
    thread_id: str
    title: str
    source: Path
    relative: Path
    size: int
    updated_at: int | None


@dataclass
class Survey:
    candidates: list[Candidate] = field(default_factory=list)
    large_recent: list[Candidate] = field(default_factory=list)
    unknown_age: int = 0
    outside_sessions_root: int = 0
    pinned_skipped: int = 0

    @property
    def total_bytes(self) -> int:
        return sum(item.size for item in self.candidates)


def _survey(ctx: Context) -> Survey:
    assert ctx.conn is not None and ctx.schema is not None
    settings = ctx.settings
    columns = ctx.schema.cols("threads")
    selected = ["id", "title", "rollout_path"]
    has_updated_at = "updated_at" in columns
    if has_updated_at:
        selected.append("updated_at")

    sql = (
        f"select {', '.join(quote_identifier(c) for c in selected)} "
        f"from threads where {ctx.schema.active_threads_predicate()}"
    )
    rows = ctx.conn.execute(sql).fetchall()

    cutoff = int(time.time()) - settings.session_age_days * 86_400
    min_bytes = int(settings.session_min_mb * 1024 * 1024)
    sessions_root = canonical(ctx.home.sessions)
    survey = Survey()

    for row in rows:
        thread_id = str(row["id"])
        rollout = row["rollout_path"]
        if not rollout:
            continue
        if thread_id in ctx.pinned:
            survey.pinned_skipped += 1
            continue

        source = Path(str(rollout))
        if not source.is_file():
            continue  # Dangling row; the integrity task reports these.
        try:
            relative = canonical(source).relative_to(sessions_root)
        except ValueError:
            survey.outside_sessions_root += 1
            continue

        try:
            size = source.stat().st_size
        except OSError:
            continue

        updated_at = None
        if has_updated_at and row["updated_at"] is not None:
            try:
                updated_at = int(row["updated_at"])
            except (TypeError, ValueError):
                updated_at = None

        item = Candidate(thread_id, str(row["title"] or ""), source, relative, size, updated_at)

        if updated_at is None:
            # No trustworthy timestamp means we cannot judge staleness, so we
            # leave the session alone rather than guess.
            survey.unknown_age += 1
            continue
        if updated_at >= cutoff:
            if size >= max(min_bytes, 64 * 1024 * 1024):
                survey.large_recent.append(item)
            continue
        if size < min_bytes:
            continue
        survey.candidates.append(item)

    survey.candidates.sort(key=lambda item: item.size, reverse=True)
    survey.large_recent.sort(key=lambda item: item.size, reverse=True)
    return survey


def _operations_for(ctx: Context, candidates: list[Candidate]) -> list[Operation]:
    """Build the inseparable move + DB update pair for session candidates."""
    assert ctx.schema is not None
    archived_columns = ctx.schema.archived_columns
    destination_root = ctx.home.archived_sessions / f"codex-tidy-{ctx.stamp}"
    now = int(time.time())
    operations: list[Operation] = []
    for item in candidates:
        item_key = operation_key("sessions", item.thread_id)
        destination = destination_root / item.relative
        assignments: dict = {"rollout_path": str(destination)}
        undo: dict = {"rollout_path": str(item.source)}
        if "archived" in archived_columns:
            assignments["archived"] = 1
            undo["archived"] = 0
        if "archived_at" in archived_columns:
            assignments["archived_at"] = now
            undo["archived_at"] = None
        operations.extend(
            [
                Operation(
                    OP_MOVE_PATH,
                    "sessions",
                    f"archive {ctx.redact.path(item.source)}",
                    {"from": str(item.source), "to": str(destination)},
                    bytes=item.size,
                    key=item_key,
                ),
                Operation(
                    OP_SQL_UPDATE,
                    "sessions",
                    f"repoint {ctx.redact.thread(item.thread_id)}",
                    {
                        "table": "threads",
                        "key_column": "id",
                        "key": item.thread_id,
                        "set": assignments,
                        "undo": undo,
                    },
                    key=item_key,
                ),
            ]
        )
    return operations


def plan_selected(ctx: Context, selected_keys: set[str]) -> list[Operation]:
    """Archive explicitly selected active sessions regardless of age."""
    if not selected_keys:
        return []
    assert ctx.conn is not None and ctx.schema is not None
    columns = ctx.schema.cols("threads")
    selected = ["id", "title", "rollout_path"]
    if "updated_at" in columns:
        selected.append("updated_at")
    rows = ctx.conn.execute(
        f"select {', '.join(quote_identifier(c) for c in selected)} "
        f"from threads where {ctx.schema.active_threads_predicate()}"
    ).fetchall()
    sessions_root = canonical(ctx.home.sessions)
    candidates: list[Candidate] = []
    for row in rows:
        thread_id = str(row["id"])
        if operation_key("sessions", thread_id) not in selected_keys or thread_id in ctx.pinned:
            continue
        source = Path(str(row["rollout_path"] or ""))
        if not source.is_file():
            continue
        try:
            relative = canonical(source).relative_to(sessions_root)
        except ValueError:
            continue
        updated_at = int(row["updated_at"]) if "updated_at" in columns and row["updated_at"] is not None else None
        candidates.append(
            Candidate(thread_id, str(row["title"] or ""), source, relative, source.stat().st_size, updated_at)
        )
    return _operations_for(ctx, candidates)


class SessionsTask(Task):
    name = "sessions"
    title = "Old session transcripts"

    def unavailable(self, ctx: Context) -> str | None:
        reason = ctx.requires_threads(*REQUIRED_COLUMNS)
        if reason:
            return reason
        if not ctx.schema or not ctx.schema.archived_columns:
            return "threads has no archived/archived_at column to mark rows with"
        if not ctx.home.sessions.is_dir():
            return f"{ctx.home.sessions.name}/ does not exist"
        return None

    def scan(self, ctx: Context) -> list[Finding]:
        survey = _survey(ctx)
        settings = ctx.settings
        findings: list[Finding] = []

        if survey.candidates:
            findings.append(
                Finding(
                    self.name,
                    "old_candidates",
                    LEVEL_NOTICE,
                    f"{len(survey.candidates)} session(s) older than "
                    f"{settings.session_age_days}d, {human_bytes(survey.total_bytes)} in the hot path",
                    count=len(survey.candidates),
                    bytes=survey.total_bytes,
                )
            )
            for item in survey.candidates[: settings.limit]:
                findings.append(
                    Finding(
                        self.name,
                        "old_candidate",
                        LEVEL_INFO,
                        f"{human_bytes(item.size):>10}  {ctx.redact.thread(item.thread_id)}"
                        f"  {ctx.redact.snippet(item.title)}",
                        bytes=item.size,
                        detail={"path": ctx.redact.path(item.source)},
                    )
                )
        else:
            findings.append(
                Finding(
                    self.name,
                    "old_candidates",
                    LEVEL_INFO,
                    f"no active session older than {settings.session_age_days}d",
                    count=0,
                )
            )

        if survey.large_recent:
            total = sum(item.size for item in survey.large_recent)
            findings.append(
                Finding(
                    self.name,
                    "large_but_recent",
                    LEVEL_INFO,
                    f"{len(survey.large_recent)} large session(s) are still recent "
                    f"({human_bytes(total)}) and were left alone",
                    count=len(survey.large_recent),
                )
            )
        if survey.unknown_age:
            findings.append(
                Finding(
                    self.name,
                    "unknown_age",
                    LEVEL_NOTICE,
                    f"{survey.unknown_age} session(s) have no usable updated_at and were skipped",
                    count=survey.unknown_age,
                )
            )
        if survey.outside_sessions_root:
            findings.append(
                Finding(
                    self.name,
                    "outside_sessions_root",
                    LEVEL_WARN,
                    f"{survey.outside_sessions_root} session(s) point outside "
                    f"{ctx.home.sessions.name}/ and cannot be archived safely",
                    count=survey.outside_sessions_root,
                )
            )
        if survey.pinned_skipped:
            findings.append(
                Finding(
                    self.name,
                    "pinned_skipped",
                    LEVEL_INFO,
                    f"{survey.pinned_skipped} pinned session(s) are never touched",
                    count=survey.pinned_skipped,
                )
            )
        return findings

    def plan(self, ctx: Context) -> list[Operation]:
        survey = _survey(ctx)
        if not survey.candidates:
            return []
        return _operations_for(ctx, survey.candidates)
