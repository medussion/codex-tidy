"""Report-only observations: sizes, disk headroom, processes, old backups.

This task never contributes an operation. In particular it reports heavy dev
processes without signalling them -- deciding what to stop is the developer's
call, not a maintenance tool's.
"""

from __future__ import annotations

from ..env import (
    codex_processes,
    default_backup_root,
    dir_size,
    free_bytes,
    heavy_dev_processes,
    human_bytes,
)
from ..model import LEVEL_BLOCKED, LEVEL_INFO, LEVEL_NOTICE, Finding
from .base import Context, Task

TRACKED_DIRS = (
    "sessions",
    "archived_sessions",
    "worktrees",
    "archived_worktrees",
    "archived_logs",
    "archived_leftovers",
)
LOW_DISK_BYTES = 5 * 1024**3


class EnvironmentTask(Task):
    name = "environment"
    title = "Environment"
    read_only = True

    def scan(self, ctx: Context) -> list[Finding]:
        findings: list[Finding] = []

        for name in TRACKED_DIRS:
            path = ctx.home.root / name
            if path.exists():
                findings.append(
                    Finding(
                        self.name,
                        "dir_size",
                        LEVEL_INFO,
                        f"{human_bytes(dir_size(path)):>10}  {name}/",
                        detail={"dir": name},
                    )
                )

        history = ctx.home.root / "history.jsonl"
        if history.is_file():
            findings.append(
                Finding(
                    self.name,
                    "history_size",
                    LEVEL_INFO,
                    f"{human_bytes(history.stat().st_size):>10}  history.jsonl",
                )
            )

        # Codex keeps more than one database. We only ever modify state_5, but the
        # others are part of the size picture and are worth naming explicitly.
        for database in ctx.home.other_databases():
            managed = database.name in ("state_5.sqlite", "logs_2.sqlite")
            findings.append(
                Finding(
                    self.name,
                    "database",
                    LEVEL_INFO,
                    f"{human_bytes(database.stat().st_size):>10}  "
                    f"{database.relative_to(ctx.home.root)}"
                    f"{'' if managed else '  (not modified by this tool)'}",
                    bytes=database.stat().st_size,
                )
            )

        free = free_bytes(ctx.home.root)
        findings.append(
            Finding(
                self.name,
                "disk_free",
                LEVEL_NOTICE if free < LOW_DISK_BYTES else LEVEL_INFO,
                f"{human_bytes(free)} free on the Codex home volume",
                bytes=free,
            )
        )

        backup_root = ctx.settings.backup_root or default_backup_root(ctx.home)
        if backup_root.is_dir():
            runs = sorted(p for p in backup_root.iterdir() if p.is_dir())
            if runs:
                findings.append(
                    Finding(
                        self.name,
                        "backups",
                        LEVEL_INFO,
                        f"{len(runs)} previous backup run(s), {human_bytes(dir_size(backup_root))} "
                        f"in {ctx.redact.path(backup_root)} -- delete these by hand when you no longer need them",
                        count=len(runs),
                    )
                )

        running = codex_processes(ctx.home)
        if running:
            findings.append(
                Finding(
                    self.name,
                    "codex_running",
                    LEVEL_BLOCKED,
                    f"Codex is running ({len(running)} process(es)) -- apply is blocked until it exits",
                    count=len(running),
                )
            )
            for proc in running[: ctx.settings.limit]:
                findings.append(
                    Finding(
                        self.name,
                        "codex_process",
                        LEVEL_INFO,
                        f"  {ctx.redact.process(proc.pid, proc.name)} -- {proc.reason}",
                    )
                )

        heavy = heavy_dev_processes(ctx.settings.limit)
        for proc in heavy:
            findings.append(
                Finding(
                    self.name,
                    "dev_process",
                    LEVEL_INFO,
                    f"{human_bytes(proc.rss_bytes):>10}  {ctx.redact.process(proc.pid, proc.name)}",
                    bytes=proc.rss_bytes,
                )
            )
        return findings
