"""Rotate oversized local log databases by moving them aside."""

from __future__ import annotations

from ..env import human_bytes
from ..model import LEVEL_INFO, LEVEL_NOTICE, OP_MOVE_PATH, Finding, Operation, operation_key
from .base import Context, Task


class LogsTask(Task):
    name = "logs"
    title = "Local log databases"

    def unavailable(self, ctx: Context) -> str | None:
        if not ctx.home.log_dbs():
            return "no log database present"
        return None

    def scan(self, ctx: Context) -> list[Finding]:
        files = ctx.home.log_dbs()
        total = sum(path.stat().st_size for path in files)
        threshold = ctx.settings.log_rotate_mb * 1024 * 1024
        level = LEVEL_NOTICE if total >= threshold else LEVEL_INFO
        verdict = (
            f"over the {ctx.settings.log_rotate_mb} MB rotation threshold"
            if total >= threshold
            else f"under the {ctx.settings.log_rotate_mb} MB rotation threshold"
        )
        return [
            Finding(
                self.name,
                "size",
                level,
                f"{len(files)} log file(s), {human_bytes(total)} -- {verdict}",
                count=len(files),
                bytes=total,
                detail={"over_threshold": total >= threshold, "threshold_mb": ctx.settings.log_rotate_mb},
            )
        ]

    def plan(self, ctx: Context) -> list[Operation]:
        files = ctx.home.log_dbs()
        total = sum(path.stat().st_size for path in files)
        if total < ctx.settings.log_rotate_mb * 1024 * 1024:
            return []
        destination_root = ctx.home.archived_logs / f"codex-tidy-{ctx.stamp}"
        return [
            Operation(
                OP_MOVE_PATH,
                self.name,
                f"rotate {ctx.redact.path(path)}",
                {"from": str(path), "to": str(destination_root / path.name)},
                bytes=path.stat().st_size,
                key=operation_key(self.name, str(path)),
            )
            for path in files
        ]
