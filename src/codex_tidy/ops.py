"""The operation executor.

There are only a handful of primitive operations, and every one of them returns
an undo payload that is itself a valid operation. Restore is therefore a replay
of undo payloads through this same function -- no separate rollback code path
that could drift out of sync.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from .db import quote_identifier
from .model import (
    OP_APPEND_JSONL,
    OP_DELETE_PATH,
    OP_MOVE_PATH,
    OP_RESTORE_FILE,
    OP_SQL_UPDATE,
    OP_WRITE_TEXT,
    Operation,
)


class OperationError(RuntimeError):
    pass


def perform(kind: str, payload: Mapping[str, Any], *, conn: sqlite3.Connection | None) -> dict | None:
    """Run one operation. Returns an undo payload, or None if not invertible."""
    handler = _HANDLERS.get(kind)
    if handler is None:
        raise OperationError(f"unknown operation kind: {kind}")
    return handler(payload, conn)


def perform_operation(op: Operation, *, conn: sqlite3.Connection | None) -> dict | None:
    return perform(op.kind, op.payload, conn=conn)


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------


def _move_path(payload: Mapping[str, Any], _conn) -> dict:
    source = Path(str(payload["from"]))
    dest = Path(str(payload["to"]))
    if not source.exists():
        raise OperationError(f"move source is gone: {source.name}")
    if dest.exists():
        raise OperationError(f"move destination already exists: {dest.name}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(dest))
    return {"kind": OP_MOVE_PATH, "payload": {"from": str(dest), "to": str(source)}}


def _sql_update(payload: Mapping[str, Any], conn: sqlite3.Connection | None) -> dict:
    """Update columns on one row, deriving the undo payload from live values.

    The plan may carry its own ``undo`` for review, but we deliberately ignore it
    and read the row first instead. Anything that changed between planning and
    applying is then still restored to what was actually there.
    """
    if conn is None:
        raise OperationError("sql_update requires a writable database connection")
    table_name = str(payload["table"])
    key_column_name = str(payload["key_column"])
    table = quote_identifier(table_name)
    key_column = quote_identifier(key_column_name)
    key = payload["key"]

    assignments = dict(payload["set"])
    if not assignments:
        raise OperationError("sql_update with an empty assignment set")
    columns = list(assignments)
    quoted = [quote_identifier(str(name)) for name in columns]

    row = conn.execute(
        f"select {', '.join(quoted)} from {table} where {key_column} = ?", (key,)
    ).fetchone()
    if row is None:
        raise OperationError(f"sql_update matched no row in {table_name}")
    previous = {column: row[index] for index, column in enumerate(columns)}

    conn.execute(
        f"update {table} set {', '.join(f'{col} = ?' for col in quoted)} where {key_column} = ?",
        [*assignments.values(), key],
    )
    return {
        "kind": OP_SQL_UPDATE,
        "payload": {
            "table": table_name,
            "key_column": key_column_name,
            "key": key,
            "set": previous,
        },
    }


def _write_text(payload: Mapping[str, Any], _conn) -> dict:
    target = Path(str(payload["path"]))
    content = str(payload["content"])
    backup = payload.get("backup")
    if target.exists():
        if not backup:
            raise OperationError(f"refusing to overwrite {target.name} without a backup path")
        backup_path = Path(str(backup))
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, backup_path)
        undo: dict = {
            "kind": OP_RESTORE_FILE,
            "payload": {"path": str(target), "backup": str(backup_path)},
        }
    else:
        undo = {"kind": OP_DELETE_PATH, "payload": {"path": str(target)}}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return undo


def _restore_file(payload: Mapping[str, Any], _conn) -> dict | None:
    target = Path(str(payload["path"]))
    backup = Path(str(payload["backup"]))
    if not backup.exists():
        raise OperationError(f"backup copy is missing: {backup.name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup, target)
    return None


def _delete_path(payload: Mapping[str, Any], _conn) -> dict | None:
    target = Path(str(payload["path"]))
    if target.is_dir():
        raise OperationError(f"refusing to delete a directory: {target.name}")
    try:
        target.unlink()
    except FileNotFoundError:
        pass
    return None


def _append_jsonl(payload: Mapping[str, Any], _conn) -> None:
    target = Path(str(payload["path"]))
    record = payload["record"]
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    # Append-only audit trail: harmless to leave in place, so no undo.
    return None


_HANDLERS = {
    OP_MOVE_PATH: _move_path,
    OP_SQL_UPDATE: _sql_update,
    OP_WRITE_TEXT: _write_text,
    OP_RESTORE_FILE: _restore_file,
    OP_DELETE_PATH: _delete_path,
    OP_APPEND_JSONL: _append_jsonl,
}
