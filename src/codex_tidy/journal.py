"""Write-ahead journal.

Every operation is recorded *before* it runs and its undo payload is recorded
immediately after. If the process dies mid-apply, the journal still describes
exactly what was attempted and what already succeeded, so ``codex-tidy restore``
can put things back without guessing.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .env import utc_iso

KIND_HEADER = "header"
KIND_BEGIN = "begin"
KIND_DONE = "done"
KIND_FAIL = "fail"
KIND_SUMMARY = "summary"


class Journal:
    """Append-only JSONL writer that flushes and fsyncs every record."""

    def __init__(self, path: Path):
        self.path = path
        self._handle = None
        self._seq = 0

    def open(self, header: Mapping[str, Any]) -> "Journal":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8")
        self._write(KIND_HEADER, **dict(header))
        return self

    def _write(self, kind: str, **fields: Any) -> int:
        if self._handle is None:
            raise RuntimeError("journal is not open")
        self._seq += 1
        record = {"seq": self._seq, "ts": utc_iso(), "kind": kind, **fields}
        self._handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._handle.flush()
        try:
            os.fsync(self._handle.fileno())
        except OSError:
            pass  # Some filesystems refuse fsync; the flush still helps.
        return self._seq

    def begin(self, index: int, op_kind: str, task: str, label: str) -> int:
        return self._write(KIND_BEGIN, index=index, op=op_kind, task=task, label=label)

    def done(self, index: int, op_kind: str, task: str, undo: Mapping[str, Any] | None) -> int:
        return self._write(
            KIND_DONE,
            index=index,
            op=op_kind,
            task=task,
            undo=dict(undo) if undo else None,
        )

    def fail(self, index: int, op_kind: str, task: str, error: str) -> int:
        return self._write(KIND_FAIL, index=index, op=op_kind, task=task, error=error)

    def summary(self, **fields: Any) -> int:
        return self._write(KIND_SUMMARY, **fields)

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "Journal":
        return self

    def __exit__(self, *_exc_info) -> None:
        self.close()


@dataclass
class JournalFile:
    path: Path
    header: dict[str, Any] = field(default_factory=dict)
    records: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "JournalFile":
        loaded = cls(path=path)
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("kind") == KIND_HEADER:
                loaded.header = record
            loaded.records.append(record)
        return loaded

    def completed(self) -> list[dict[str, Any]]:
        """Operations that finished, oldest first."""
        return [r for r in self.records if r.get("kind") == KIND_DONE]

    def failures(self) -> list[dict[str, Any]]:
        return [r for r in self.records if r.get("kind") == KIND_FAIL]

    def interrupted(self) -> list[dict[str, Any]]:
        """Operations that began but never reported done or fail."""
        settled = {
            r.get("index")
            for r in self.records
            if r.get("kind") in (KIND_DONE, KIND_FAIL)
        }
        return [
            r
            for r in self.records
            if r.get("kind") == KIND_BEGIN and r.get("index") not in settled
        ]

    def undo_stack(self) -> list[dict[str, Any]]:
        """Undo payloads in reverse completion order."""
        stack = []
        for record in reversed(self.completed()):
            undo = record.get("undo")
            if undo:
                stack.append({**undo, "_source": record})
        return stack


def find_latest_journal(backup_root: Path) -> Path | None:
    if not backup_root.is_dir():
        return None
    found = sorted(backup_root.glob("*/journal.jsonl"))
    return found[-1] if found else None
