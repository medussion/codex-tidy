"""Data model shared by scanning, planning, rendering and journalling."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

# Ordered by how loudly they should be surfaced.
LEVEL_INFO = "info"
LEVEL_NOTICE = "notice"
LEVEL_WARN = "warn"
LEVEL_BLOCKED = "blocked"
LEVEL_ORDER = (LEVEL_BLOCKED, LEVEL_WARN, LEVEL_NOTICE, LEVEL_INFO)

# Operation kinds the executor understands. Undo payloads reuse the same set,
# which is what makes restore a plain replay rather than bespoke logic.
OP_MOVE_PATH = "move_path"
OP_SQL_UPDATE = "sql_update"
OP_WRITE_TEXT = "write_text"
OP_RESTORE_FILE = "restore_file"
OP_DELETE_PATH = "delete_path"
OP_APPEND_JSONL = "append_jsonl"


@dataclass(frozen=True)
class Finding:
    """One observation. Messages are already redaction-safe."""

    task: str
    code: str
    level: str
    message: str
    count: int = 1
    bytes: int = 0
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "code": self.code,
            "level": self.level,
            "message": self.message,
            "count": self.count,
            "bytes": self.bytes,
            "detail": dict(self.detail),
        }


def operation_key(task: str, identity: str) -> str:
    """Stable, non-revealing id for the *thing* an operation acts on.

    Several operations can share one key -- archiving a session is a file move
    plus a row update -- so excluding a key from a plan drops the whole item, never
    half of it. Hashed so that a raw thread id or path never reaches the UI.
    """
    digest = hashlib.blake2s(identity.encode("utf-8", "replace"), digest_size=6)
    return f"{task}:{digest.hexdigest()}"


@dataclass(frozen=True)
class Operation:
    """One reversible unit of change."""

    kind: str
    task: str
    label: str
    payload: Mapping[str, Any]
    bytes: int = 0
    #: Identifies the item this operation belongs to; see operation_key().
    key: str = ""

    @property
    def invertible(self) -> bool:
        return self.kind != OP_APPEND_JSONL

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "task": self.task,
            "label": self.label,
            "payload": dict(self.payload),
            "bytes": self.bytes,
            "key": self.key,
        }


@dataclass
class Plan:
    stamp: str
    created: str
    codex_home: str
    backup_root: str
    findings: list[Finding] = field(default_factory=list)
    operations: list[Operation] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)

    @property
    def reclaimable_bytes(self) -> int:
        return sum(op.bytes for op in self.operations)

    @property
    def tasks_touched(self) -> tuple[str, ...]:
        seen: list[str] = []
        for op in self.operations:
            if op.task not in seen:
                seen.append(op.task)
        return tuple(seen)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "stamp": self.stamp,
            "created": self.created,
            "codex_home": self.codex_home,
            "backup_root": self.backup_root,
            "findings": [f.to_dict() for f in self.findings],
            "operations": [op.to_dict() for op in self.operations],
            "skipped": dict(self.skipped),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Plan":
        plan = cls(
            stamp=str(data.get("stamp", "")),
            created=str(data.get("created", "")),
            codex_home=str(data.get("codex_home", "")),
            backup_root=str(data.get("backup_root", "")),
            skipped=dict(data.get("skipped") or {}),
        )
        for raw in data.get("findings") or []:
            plan.findings.append(
                Finding(
                    task=str(raw.get("task", "")),
                    code=str(raw.get("code", "")),
                    level=str(raw.get("level", LEVEL_INFO)),
                    message=str(raw.get("message", "")),
                    count=int(raw.get("count", 1)),
                    bytes=int(raw.get("bytes", 0)),
                    detail=dict(raw.get("detail") or {}),
                )
            )
        for raw in data.get("operations") or []:
            plan.operations.append(
                Operation(
                    kind=str(raw.get("kind", "")),
                    task=str(raw.get("task", "")),
                    label=str(raw.get("label", "")),
                    payload=dict(raw.get("payload") or {}),
                    bytes=int(raw.get("bytes", 0)),
                    key=str(raw.get("key", "")),
                )
            )
        return plan

    def items(self) -> list["PlanItem"]:
        """Operations grouped into the user-facing items they act on."""
        grouped: dict[str, PlanItem] = {}
        for index, op in enumerate(self.operations):
            key = op.key or f"{op.task}:op{index}"
            item = grouped.get(key)
            if item is None:
                grouped[key] = PlanItem(
                    key=key, task=op.task, label=op.label, bytes=op.bytes, operations=1
                )
            else:
                item.bytes += op.bytes
                item.operations += 1
        return list(grouped.values())


@dataclass
class PlanItem:
    """One thing the user can tick or untick."""

    key: str
    task: str
    label: str
    bytes: int
    operations: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "task": self.task,
            "label": self.label,
            "bytes": self.bytes,
            "operations": self.operations,
        }


@dataclass
class Settings:
    """Thresholds and switches, resolved from the CLI once per run."""

    reveal: bool = False
    session_age_days: int = 10
    session_min_mb: float = 0.0
    worktree_age_days: int = 7
    log_rotate_mb: int = 64
    title_limit: int = 120
    preview_limit: int = 240
    repair_thread_metadata: bool = False
    archive_orphan_rollouts: bool = False
    archive_dirty_worktrees: bool = False
    archive_leftovers: bool = False
    leftover_age_days: int = 3
    compact: bool = False
    max_archive_gb: float = 20.0
    limit: int = 10
    only: frozenset[str] = frozenset()
    skip: frozenset[str] = frozenset()
    backup_root: Path | None = None
    wait_for_exit_seconds: int = 0

    def task_enabled(self, name: str) -> bool:
        if self.only and name not in self.only:
            return False
        return name not in self.skip
