"""Irreversible deletion of explicitly selected archived session transcripts."""

from __future__ import annotations

import re
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .db import connect, probe
from .env import CodexHome, HomeLock, LockHeld, canonical, codex_processes
from .handoff import _session_index_names, display_title, list_sessions
from .model import operation_key

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


@dataclass(frozen=True)
class ArchiveItem:
    key: str
    path: Path
    size: int
    label: str
    updated_at: int
    thread_id: str | None
    project: str | None

    def to_dict(self, redact) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": redact.snippet(self.label, limit=120),
            "path": redact.path(self.path),
            "bytes": self.size,
            "updated_at": self.updated_at,
            "linked": self.thread_id is not None,
            "project": self.project if redact.reveal else None,
        }


def list_archives(home: CodexHome) -> list[ArchiveItem]:
    root = canonical(home.archived_sessions)
    if not root.is_dir():
        return []
    linked = {canonical(item.rollout_path): item for item in list_sessions(home) if item.archived}
    index_names = _session_index_names(home)
    found: list[ArchiveItem] = []
    for path in root.rglob("*.jsonl"):
        if path.is_symlink() or not path.is_file():
            continue
        resolved = canonical(path)
        try:
            resolved.relative_to(root)
            stat = path.stat()
        except (ValueError, OSError):
            continue
        session = linked.get(resolved)
        if session:
            label = display_title(session.name or session.title)
            thread_id = session.thread_id
            project = session.cwd.name if session.cwd else None
            updated_at = session.updated_at or int(stat.st_mtime)
        else:
            ids = UUID_RE.findall(path.name)
            label = next((index_names[item] for item in ids if item in index_names), "")
            label = display_title(label) if label else f"보관된 세션 · {path.name[:40]}"
            thread_id = None
            project = None
            updated_at = int(stat.st_mtime)
        found.append(
            ArchiveItem(
                key=operation_key("purge-sessions", str(resolved)),
                path=resolved,
                size=stat.st_size,
                label=label,
                updated_at=updated_at,
                thread_id=thread_id,
                project=project,
            )
        )
    found.sort(key=lambda item: (-item.size, -item.updated_at, item.label))
    return found


def _restore_staged(staged: list[tuple[Path, Path]]) -> None:
    for original, temporary in reversed(staged):
        if temporary.exists() and not original.exists():
            original.parent.mkdir(parents=True, exist_ok=True)
            temporary.replace(original)


def purge_selected(home: CodexHome, selected_keys: Iterable[str], confirmation: str) -> dict[str, Any]:
    if confirmation != "영구 삭제":
        return {"ok": False, "blocked": ["confirmation phrase did not match"]}
    requested = {str(key) for key in selected_keys}
    available = {item.key: item for item in list_archives(home)}
    missing = sorted(requested - set(available))
    if not requested:
        return {"ok": False, "blocked": ["no archive was selected"]}
    if missing:
        return {"ok": False, "blocked": ["one or more selected archives changed; refresh and retry"]}
    running = codex_processes(home)
    if running:
        return {"ok": False, "blocked": ["Codex must be closed before permanent deletion"]}

    items = [available[key] for key in requested]
    root = canonical(home.archived_sessions)
    staging = root / f".codex-tidy-purge-{uuid.uuid4().hex}"
    staged: list[tuple[Path, Path]] = []
    conn = None
    try:
        with HomeLock(home, active=True):
            for item in items:
                relative = item.path.relative_to(root)
                temporary = staging / relative
                temporary.parent.mkdir(parents=True, exist_ok=True)
                item.path.replace(temporary)
                staged.append((item.path, temporary))

            conn = connect(home.state_db, readonly=False)
            schema = probe(conn)
            conn.execute("pragma foreign_keys=on")
            conn.execute("begin immediate")
            ids = [item.thread_id for item in items if item.thread_id]
            for thread_id in ids:
                if schema.has("thread_spawn_edges", "parent_thread_id", "child_thread_id"):
                    conn.execute(
                        "delete from thread_spawn_edges where parent_thread_id=? or child_thread_id=?",
                        (thread_id, thread_id),
                    )
                # list_archives only assigns a thread_id to a row already marked
                # archived and pointing at this exact file.
                conn.execute("delete from threads where id=?", (thread_id,))
            conn.execute("commit")
    except (OSError, sqlite3.Error, LockHeld, ValueError) as exc:
        if conn is not None:
            try:
                conn.execute("rollback")
            except sqlite3.Error:
                pass
        _restore_staged(staged)
        return {"ok": False, "blocked": [str(exc)]}
    finally:
        if conn is not None:
            conn.close()

    failures = []
    deleted = 0
    deleted_bytes = 0
    for original, temporary in staged:
        try:
            size = temporary.stat().st_size
            temporary.unlink()
            deleted += 1
            deleted_bytes += size
        except OSError as exc:
            failures.append(f"{original.name}: {exc}")
    for directory in sorted((p for p in staging.rglob("*") if p.is_dir()), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass
    try:
        staging.rmdir()
    except OSError:
        pass
    return {
        "ok": not failures,
        "deleted": deleted,
        "deleted_bytes": deleted_bytes,
        "failed": failures,
        "irreversible": True,
    }
