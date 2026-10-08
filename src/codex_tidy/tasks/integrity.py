"""Cross-check the database against what is actually on disk.

Two drift shapes matter and neither is visible from a size report alone:

* **orphan transcripts** -- files under ``sessions/`` that no thread row points at.
  Dead weight that age-based cleanup never reaches, because there is no row to
  read a timestamp from.
* **dangling rows** -- thread rows pointing at a transcript that is gone. Reported
  only; rewriting them is Codex's business, not ours.
"""

from __future__ import annotations

from pathlib import Path

from ..env import canonical, human_bytes
from ..model import (
    LEVEL_INFO,
    LEVEL_NOTICE,
    LEVEL_WARN,
    OP_MOVE_PATH,
    Finding,
    Operation,
    operation_key,
)
from .base import Context, Task


def _referenced(ctx: Context) -> set[Path]:
    assert ctx.conn is not None
    referenced: set[Path] = set()
    for row in ctx.conn.execute("select rollout_path from threads").fetchall():
        value = row["rollout_path"]
        if value:
            referenced.add(canonical(Path(str(value))))
    return referenced


def _orphans(ctx: Context) -> list[tuple[Path, int]]:
    root = ctx.home.sessions
    if not root.is_dir():
        return []
    referenced = _referenced(ctx)
    orphans = []
    for item in root.rglob("*"):
        try:
            if not item.is_file():
                continue
            if canonical(item) in referenced:
                continue
            orphans.append((item, item.stat().st_size))
        except OSError:
            continue
    orphans.sort(key=lambda pair: pair[1], reverse=True)
    return orphans


def _dangling(ctx: Context) -> int:
    assert ctx.conn is not None
    count = 0
    for row in ctx.conn.execute(
        "select rollout_path from threads where rollout_path is not null"
    ).fetchall():
        value = str(row["rollout_path"])
        if value and not Path(value).exists():
            count += 1
    return count


class IntegrityTask(Task):
    name = "integrity"
    title = "Database / disk consistency"

    def unavailable(self, ctx: Context) -> str | None:
        return ctx.requires_threads("id", "rollout_path")

    def scan(self, ctx: Context) -> list[Finding]:
        findings: list[Finding] = []
        orphans = _orphans(ctx)
        total = sum(size for _, size in orphans)
        if orphans:
            findings.append(
                Finding(
                    self.name,
                    "orphan_transcripts",
                    LEVEL_NOTICE,
                    f"{len(orphans)} transcript file(s) ({human_bytes(total)}) are not "
                    "referenced by any thread row",
                    count=len(orphans),
                    bytes=total,
                )
            )
            for path, size in orphans[: ctx.settings.limit]:
                findings.append(
                    Finding(
                        self.name,
                        "orphan_transcript",
                        LEVEL_INFO,
                        f"{human_bytes(size):>10}  {ctx.redact.path(path)}",
                        bytes=size,
                    )
                )
            if not ctx.settings.archive_orphan_rollouts:
                findings.append(
                    Finding(
                        self.name,
                        "orphan_action",
                        LEVEL_INFO,
                        "add --archive-orphan-transcripts to move these aside reversibly",
                    )
                )
        else:
            findings.append(
                Finding(
                    self.name, "orphan_transcripts", LEVEL_INFO, "no orphan transcripts", count=0
                )
            )

        dangling = _dangling(ctx)
        if dangling:
            findings.append(
                Finding(
                    self.name,
                    "dangling_rows",
                    LEVEL_WARN,
                    f"{dangling} thread row(s) point at a transcript that no longer exists "
                    "(reported only -- resuming those threads will not work)",
                    count=dangling,
                )
            )
        return findings

    def plan(self, ctx: Context) -> list[Operation]:
        if not ctx.settings.archive_orphan_rollouts:
            return []
        destination_root = ctx.home.archived_sessions / f"orphans-{ctx.stamp}"
        sessions_root = canonical(ctx.home.sessions)
        operations = []
        for path, size in _orphans(ctx):
            try:
                relative = canonical(path).relative_to(sessions_root)
            except ValueError:
                continue
            operations.append(
                Operation(
                    OP_MOVE_PATH,
                    self.name,
                    f"archive orphan {ctx.redact.path(path)}",
                    {"from": str(path), "to": str(destination_root / relative)},
                    bytes=size,
                    key=operation_key(self.name, str(path)),
                )
            )
        return operations
