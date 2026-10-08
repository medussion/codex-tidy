"""Interrupted-write leftovers.

Codex writes its global state with an atomic replace: write ``..name.tmp-<ms>-<uuid>``,
then rename over the real file. When the process dies between those two steps the
temp file stays forever. Nothing ever reads it again, and on a long-lived install
they pile up -- each one a full copy of the state file.

The same applies to scratch directories under ``tmp/``. Both are only ever moved,
never deleted, and only when older than the age threshold so an in-flight write is
never disturbed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from ..env import dir_size, human_bytes
from ..model import LEVEL_INFO, LEVEL_NOTICE, OP_MOVE_PATH, Finding, Operation, operation_key
from .base import Context, Task


@dataclass(frozen=True)
class Leftover:
    path: Path
    size: int
    age_days: float
    kind: str  # "temp file" | "scratch dir"


def _leftovers(ctx: Context) -> list[Leftover]:
    cutoff = time.time() - ctx.settings.leftover_age_days * 86_400
    found: list[Leftover] = []

    def consider(path: Path, kind: str, size: int) -> None:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return
        if mtime >= cutoff:
            return
        found.append(Leftover(path, size, (time.time() - mtime) / 86_400, kind))

    for path in ctx.home.leftover_files():
        try:
            consider(path, "temp file", path.stat().st_size)
        except OSError:
            continue

    scratch = ctx.home.scratch
    if scratch.is_dir():
        for entry in sorted(scratch.iterdir()):
            if entry.is_dir():
                consider(entry, "scratch dir", dir_size(entry))

    found.sort(key=lambda item: item.size, reverse=True)
    return found


class LeftoversTask(Task):
    name = "leftovers"
    title = "Interrupted-write leftovers"

    def unavailable(self, ctx: Context) -> str | None:
        if not ctx.home.leftover_files() and not ctx.home.scratch.is_dir():
            return "no leftover temp files or scratch directories"
        return None

    def scan(self, ctx: Context) -> list[Finding]:
        leftovers = _leftovers(ctx)
        if not leftovers:
            return [
                Finding(
                    self.name,
                    "candidates",
                    LEVEL_INFO,
                    f"no leftover older than {ctx.settings.leftover_age_days}d",
                    count=0,
                )
            ]

        total = sum(item.size for item in leftovers)
        findings = [
            Finding(
                self.name,
                "candidates",
                LEVEL_NOTICE,
                f"{len(leftovers)} leftover(s) from interrupted writes, {human_bytes(total)}, "
                f"oldest {max(item.age_days for item in leftovers):.0f}d",
                count=len(leftovers),
                bytes=total,
            )
        ]
        for item in leftovers[: ctx.settings.limit]:
            findings.append(
                Finding(
                    self.name,
                    "candidate",
                    LEVEL_INFO,
                    f"{human_bytes(item.size):>10}  {item.age_days:>4.0f}d  {item.kind:<12}"
                    f"  {ctx.redact.path(item.path)}",
                    bytes=item.size,
                )
            )
        if not ctx.settings.archive_leftovers:
            findings.append(
                Finding(
                    self.name,
                    "action",
                    LEVEL_INFO,
                    "add --archive-leftovers to move these aside reversibly",
                )
            )
        return findings

    def plan(self, ctx: Context) -> list[Operation]:
        if not ctx.settings.archive_leftovers:
            return []
        destination_root = ctx.home.archived_leftovers / f"codex-tidy-{ctx.stamp}"
        return [
            Operation(
                OP_MOVE_PATH,
                self.name,
                f"archive {item.kind} {ctx.redact.path(item.path)}",
                {"from": str(item.path), "to": str(destination_root / item.path.name)},
                bytes=item.size,
                key=operation_key(self.name, str(item.path)),
            )
            for item in _leftovers(ctx)
        ]
