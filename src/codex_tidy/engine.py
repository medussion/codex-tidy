"""Orchestration: scan, plan, apply, restore.

Design commitments:

* ``scan`` opens the database read-only and takes no lock. It cannot write.
* ``apply`` backs up first, journals every operation before running it, and
  records an undo payload after. A mid-run failure rolls back by replaying those
  undo payloads in reverse -- the same code path ``restore`` uses later.
* Each operation commits on its own. A single large transaction would let the
  database roll back while filesystem moves stayed done, which is exactly the
  drift this tool exists to prevent.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import db as dbmod
from .env import (
    BACKUP_ITEMS,
    COPY_IGNORE,
    CodexHome,
    HomeLock,
    LockHeld,
    ProcInfo,
    codex_processes,
    default_backup_root,
    dir_size,
    free_bytes,
    human_bytes,
    run_stamp,
    utc_iso,
)
from .journal import Journal, JournalFile
from .model import LEVEL_BLOCKED, LEVEL_WARN, Finding, Operation, Plan, Settings
from .ops import OperationError, perform, perform_operation
from .privacy import Redactor
from .tasks import ALL_TASKS, Context

EXIT_OK = 0
EXIT_ENVIRONMENT = 2
EXIT_BLOCKED = 3
EXIT_APPLY_FAILED = 4
EXIT_BAD_INPUT = 5

DISK_HEADROOM_BYTES = 100 * 1024 * 1024


class EnvironmentError_(RuntimeError):
    """Raised when the Codex home is unusable."""


@dataclass
class Session:
    """An open working context plus the connection it owns."""

    ctx: Context
    conn: sqlite3.Connection | None
    skipped: dict[str, str] = field(default_factory=dict)

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None


def open_session(
    home: CodexHome, settings: Settings, *, writable: bool, stamp: str, backup_root: Path
) -> Session:
    if not home.exists():
        raise EnvironmentError_(f"Codex home not found: {home.root}")

    redactor = Redactor(reveal=settings.reveal, home=home.root)
    conn = None
    schema = None
    if home.state_db.is_file():
        try:
            conn = dbmod.connect(home.state_db, readonly=not writable)
            schema = dbmod.probe(conn)
        except sqlite3.Error as exc:
            if conn is not None:
                conn.close()
            raise EnvironmentError_(
                f"could not open {home.state_db} "
                f"({'read-only' if not writable else 'read-write'}): {exc}\n"
                "If this is a copied Codex home, copy the -wal and -shm files alongside "
                "the database: SQLite cannot open a WAL database read-only without them."
            ) from exc

    from .env import pinned_thread_ids

    ctx = Context(
        home=home,
        settings=settings,
        redact=redactor,
        backup_root=backup_root,
        stamp=stamp,
        pinned=pinned_thread_ids(home),
        schema=schema,
        conn=conn,
        writable=writable,
    )
    return Session(ctx=ctx, conn=conn)


def _active_tasks(session: Session):
    chosen = []
    for task in ALL_TASKS:
        if not session.ctx.settings.task_enabled(task.name):
            continue
        reason = task.unavailable(session.ctx)
        if reason:
            session.skipped[task.name] = reason
            continue
        chosen.append(task)
    return chosen


def new_plan(session: Session) -> Plan:
    return Plan(
        stamp=session.ctx.stamp,
        created=utc_iso(),
        codex_home=str(session.ctx.home.root),
        backup_root=str(session.ctx.backup_root),
    )


def scan(session: Session, *, with_operations: bool) -> Plan:
    plan = new_plan(session)
    for task in _active_tasks(session):
        plan.findings.extend(task.scan(session.ctx))
        if with_operations and not task.read_only:
            plan.operations.extend(task.plan(session.ctx))
    plan.skipped = dict(session.skipped)
    return plan


# --------------------------------------------------------------------------
# Backup
# --------------------------------------------------------------------------


def estimate_backup_bytes(home: CodexHome) -> int:
    total = dir_size(home.state_db)
    for name in BACKUP_ITEMS:
        total += dir_size(home.root / name)
    return total


def create_backup(home: CodexHome, destination: Path) -> list[str]:
    """Copy everything we might disturb. Returns human notes about what landed."""
    destination.mkdir(parents=True, exist_ok=True)
    notes = []
    for name in BACKUP_ITEMS:
        source = home.root / name
        if not source.exists():
            continue
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(
                source,
                target,
                ignore=shutil.ignore_patterns(*COPY_IGNORE),
                dirs_exist_ok=True,
            )
        else:
            shutil.copy2(source, target)
        notes.append(name)
    if dbmod.online_backup(home.state_db, destination / home.state_db.name):
        notes.append(home.state_db.name)
    return notes


# --------------------------------------------------------------------------
# Apply
# --------------------------------------------------------------------------


@dataclass
class ApplyResult:
    plan: Plan
    journal_path: Path
    backup_root: Path
    completed: int = 0
    failed: int = 0
    rolled_back: int = 0
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return EXIT_APPLY_FAILED if self.failed else EXIT_OK


def blocking_processes(home: CodexHome, settings: Settings) -> list[ProcInfo]:
    running = codex_processes(home)
    if running and settings.wait_for_exit_seconds > 0:
        deadline = time.monotonic() + settings.wait_for_exit_seconds
        while running and time.monotonic() < deadline:
            time.sleep(2)
            running = codex_processes(home)
    return running


def preflight(home: CodexHome, plan: Plan, settings: Settings) -> list[Finding]:
    """Refusals that must happen before a single byte moves."""
    problems: list[Finding] = []

    running = blocking_processes(home, settings)
    if running:
        detail = "; ".join(f"pid {proc.pid} {proc.name} ({proc.reason})" for proc in running[:5])
        problems.append(
            Finding(
                "preflight",
                "codex_running",
                LEVEL_BLOCKED,
                f"Codex is still running ({len(running)} process(es)). Close it, or pass "
                f"--wait-for-exit SECONDS. {detail}",
                count=len(running),
            )
        )

    cap_bytes = int(settings.max_archive_gb * 1024**3)
    if plan.reclaimable_bytes > cap_bytes:
        problems.append(
            Finding(
                "preflight",
                "over_cap",
                LEVEL_BLOCKED,
                f"plan would move {human_bytes(plan.reclaimable_bytes)}, over the "
                f"{settings.max_archive_gb} GB safety cap. Raise --max-archive-gb if that is intended.",
                bytes=plan.reclaimable_bytes,
            )
        )

    needed = estimate_backup_bytes(home) + DISK_HEADROOM_BYTES
    available = free_bytes(Path(plan.backup_root))
    if available and available < needed:
        problems.append(
            Finding(
                "preflight",
                "low_disk",
                LEVEL_BLOCKED,
                f"backup needs about {human_bytes(needed)} but only {human_bytes(available)} is free "
                "on the backup volume",
                bytes=needed,
            )
        )
    return problems


def apply_plan(home: CodexHome, plan: Plan, settings: Settings) -> ApplyResult:
    backup_root = Path(plan.backup_root)
    result = ApplyResult(plan=plan, journal_path=backup_root / "journal.jsonl", backup_root=backup_root)

    backed_up = create_backup(home, backup_root)
    result.notes.append(f"backed up {len(backed_up)} item(s) to {backup_root}")
    (backup_root / "plan.json").write_text(
        json.dumps(plan.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )

    conn = dbmod.connect(home.state_db, readonly=False) if home.state_db.is_file() else None
    journal = Journal(result.journal_path)
    undo_stack: list[dict] = []

    try:
        journal.open(
            {
                "tool": "codex-tidy",
                "stamp": plan.stamp,
                "codex_home": str(home.root),
                "backup_root": str(backup_root),
                "operations": len(plan.operations),
            }
        )
        for index, op in enumerate(plan.operations):
            journal.begin(index, op.kind, op.task, op.label)
            try:
                undo = perform_operation(op, conn=conn)
            except (OperationError, OSError, sqlite3.Error) as exc:
                journal.fail(index, op.kind, op.task, str(exc))
                result.failed += 1
                result.error = f"{op.label}: {exc}"
                break
            journal.done(index, op.kind, op.task, undo)
            if undo:
                undo_stack.append(undo)
            result.completed += 1

        if result.failed:
            # Put everything back before returning, newest change first.
            for undo in reversed(undo_stack):
                try:
                    perform(undo["kind"], undo["payload"], conn=conn)
                    result.rolled_back += 1
                except (OperationError, OSError, sqlite3.Error) as exc:
                    result.notes.append(f"rollback step failed: {exc}")
            journal.summary(
                outcome="rolled_back",
                completed=result.completed,
                rolled_back=result.rolled_back,
                error=result.error,
            )
        else:
            if conn is not None:
                result.notes.extend(dbmod.checkpoint_and_optimize(conn, compact=settings.compact))
            journal.summary(outcome="applied", completed=result.completed)
    finally:
        journal.close()
        if conn is not None:
            conn.close()
    return result


# --------------------------------------------------------------------------
# Restore
# --------------------------------------------------------------------------


@dataclass
class RestoreResult:
    journal_path: Path
    undone: int = 0
    failed: int = 0
    skipped: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return EXIT_APPLY_FAILED if self.failed else EXIT_OK


def restore(home: CodexHome, journal_path: Path) -> RestoreResult:
    loaded = JournalFile.load(journal_path)
    result = RestoreResult(journal_path=journal_path)

    interrupted = loaded.interrupted()
    if interrupted:
        result.notes.append(
            f"{len(interrupted)} operation(s) began without reporting an outcome; "
            "inspect those manually after this restore"
        )

    conn = dbmod.connect(home.state_db, readonly=False) if home.state_db.is_file() else None
    trail = Journal(journal_path.parent / "restore.jsonl")
    try:
        trail.open({"tool": "codex-tidy", "action": "restore", "source": str(journal_path)})
        for index, undo in enumerate(loaded.undo_stack()):
            kind = undo.get("kind")
            payload = undo.get("payload") or {}
            trail.begin(index, str(kind), "restore", str(payload.get("path") or payload.get("to") or ""))
            try:
                perform(str(kind), payload, conn=conn)
            except (OperationError, OSError, sqlite3.Error) as exc:
                trail.fail(index, str(kind), "restore", str(exc))
                result.failed += 1
                result.notes.append(f"could not undo {kind}: {exc}")
                continue
            trail.done(index, str(kind), "restore", None)
            result.undone += 1
        trail.summary(outcome="restored", undone=result.undone, failed=result.failed)
    finally:
        trail.close()
        if conn is not None:
            conn.close()
    return result


# --------------------------------------------------------------------------
# Helpers used by the CLI
# --------------------------------------------------------------------------


def filter_plan(plan: Plan, excluded_keys: set[str]) -> Plan:
    """Drop whole items from a plan.

    Operations belonging to one item share a key, so an exclusion can never leave
    half an item behind -- a session's file move and its row update go together.
    """
    if not excluded_keys:
        return plan
    filtered = Plan(
        stamp=plan.stamp,
        created=plan.created,
        codex_home=plan.codex_home,
        backup_root=plan.backup_root,
        findings=list(plan.findings),
        operations=[op for op in plan.operations if op.key not in excluded_keys],
        skipped=dict(plan.skipped),
    )
    return filtered


def resolve_backup_root(home: CodexHome, settings: Settings, stamp: str) -> Path:
    base = settings.backup_root or default_backup_root(home)
    return (base / f"codex-tidy-{stamp}").expanduser().resolve()


def fresh_stamp() -> str:
    return run_stamp()


__all__ = [
    "ApplyResult",
    "EXIT_APPLY_FAILED",
    "EXIT_BAD_INPUT",
    "EXIT_BLOCKED",
    "EXIT_ENVIRONMENT",
    "EXIT_OK",
    "EnvironmentError_",
    "HomeLock",
    "LockHeld",
    "RestoreResult",
    "Session",
    "apply_plan",
    "create_backup",
    "filter_plan",
    "fresh_stamp",
    "open_session",
    "preflight",
    "resolve_backup_root",
    "restore",
    "scan",
]
