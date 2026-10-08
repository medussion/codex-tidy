"""Move stale git worktrees out of the hot path.

Unlike the plain age check, this refuses to touch a worktree with uncommitted
work unless you opt in. Archiving is only a move, but a developer who cannot
find their dirty worktree has effectively lost it.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from ..env import dir_size, human_bytes
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

GIT_TIMEOUT_SECONDS = 15
MAX_GIT_PROBES = 50


@dataclass(frozen=True)
class Stale:
    path: Path
    size: int
    age_days: float
    dirty: bool | None  # None means "could not determine"


def _git_dirty(path: Path) -> bool | None:
    if not (path / ".git").exists():
        return None
    try:
        output = subprocess.check_output(
            ["git", "-C", str(path), "status", "--porcelain"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return bool(output.strip())


def _stale(ctx: Context) -> list[Stale]:
    root = ctx.home.worktrees
    cutoff = time.time() - ctx.settings.worktree_age_days * 86_400
    found: list[Stale] = []
    probes = 0
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        dirty: bool | None = None
        if probes < MAX_GIT_PROBES:
            dirty = _git_dirty(entry)
            probes += 1
        found.append(
            Stale(entry, dir_size(entry), (time.time() - mtime) / 86_400, dirty)
        )
    found.sort(key=lambda item: item.size, reverse=True)
    return found


class WorktreesTask(Task):
    name = "worktrees"
    title = "Stale git worktrees"

    def unavailable(self, ctx: Context) -> str | None:
        if not ctx.home.worktrees.is_dir():
            return f"{ctx.home.worktrees.name}/ does not exist"
        return None

    def scan(self, ctx: Context) -> list[Finding]:
        stale = _stale(ctx)
        if not stale:
            return [
                Finding(
                    self.name,
                    "candidates",
                    LEVEL_INFO,
                    f"no worktree older than {ctx.settings.worktree_age_days}d",
                    count=0,
                )
            ]

        movable = [item for item in stale if not item.dirty]
        dirty = [item for item in stale if item.dirty]
        findings = [
            Finding(
                self.name,
                "candidates",
                LEVEL_NOTICE,
                f"{len(stale)} worktree(s) older than {ctx.settings.worktree_age_days}d, "
                f"{human_bytes(sum(i.size for i in stale))} total",
                count=len(stale),
                bytes=sum(item.size for item in movable),
            )
        ]
        for item in stale[: ctx.settings.limit]:
            state = "dirty" if item.dirty else "clean" if item.dirty is False else "not a git worktree"
            findings.append(
                Finding(
                    self.name,
                    "candidate",
                    LEVEL_INFO,
                    f"{human_bytes(item.size):>10}  {item.age_days:.0f}d  {state:<20}"
                    f"  {ctx.redact.path(item.path)}",
                    bytes=item.size,
                )
            )
        if dirty:
            included = ctx.settings.archive_dirty_worktrees
            findings.append(
                Finding(
                    self.name,
                    "dirty",
                    LEVEL_WARN,
                    f"{len(dirty)} worktree(s) have uncommitted changes and are "
                    + ("INCLUDED (--archive-dirty-worktrees)" if included else "excluded from the plan"),
                    count=len(dirty),
                )
            )
        return findings

    def plan(self, ctx: Context) -> list[Operation]:
        destination_root = ctx.home.archived_worktrees / f"codex-tidy-{ctx.stamp}"
        operations = []
        for item in _stale(ctx):
            if item.dirty and not ctx.settings.archive_dirty_worktrees:
                continue
            operations.append(
                Operation(
                    OP_MOVE_PATH,
                    self.name,
                    f"archive {ctx.redact.path(item.path)}",
                    {"from": str(item.path), "to": str(destination_root / item.path.name)},
                    bytes=item.size,
                    key=operation_key(self.name, str(item.path)),
                )
            )
        return operations
