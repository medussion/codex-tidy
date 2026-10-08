"""Normalise Windows extended-length paths stored in the database.

Codex on Windows sometimes persists ``\\\\?\\C:\\...`` prefixed paths. Those do not
compare equal to the plain form, so the same folder can be treated as two
different projects. Each affected row becomes its own reversible update that
records the previous value.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ..db import declared_text_columns, quote_identifier
from ..model import (
    LEVEL_INFO,
    LEVEL_NOTICE,
    LEVEL_WARN,
    OP_SQL_UPDATE,
    Finding,
    Operation,
    operation_key,
)
from .base import Context, Task

EXTENDED_PREFIX = "\\\\?\\"
UNC_PREFIX = "\\\\?\\UNC\\"
MAX_ROWS_PER_COLUMN = 5_000


def normalize(value: str) -> str:
    if value.startswith(UNC_PREFIX):
        return "\\\\" + value[len(UNC_PREFIX) :]
    if value.startswith(EXTENDED_PREFIX):
        return value[len(EXTENDED_PREFIX) :]
    return value


@dataclass(frozen=True)
class Hit:
    table: str
    column: str
    rowid: int
    old: str
    new: str


def _hits(ctx: Context) -> tuple[list[Hit], list[str]]:
    assert ctx.conn is not None and ctx.schema is not None
    found: list[Hit] = []
    notes: list[str] = []
    for table in sorted(ctx.schema.tables):
        for column in declared_text_columns(ctx.conn, table):
            try:
                rows = ctx.conn.execute(
                    f"select rowid as rid, {quote_identifier(column)} as value "
                    f"from {quote_identifier(table)} "
                    f"where {quote_identifier(column)} like ? limit ?",
                    (EXTENDED_PREFIX + "%", MAX_ROWS_PER_COLUMN + 1),
                ).fetchall()
            except (sqlite3.Error, ValueError):
                continue  # WITHOUT ROWID tables and odd types are simply skipped.
            if len(rows) > MAX_ROWS_PER_COLUMN:
                notes.append(f"{table}.{column} has more than {MAX_ROWS_PER_COLUMN:,} affected rows")
                rows = rows[:MAX_ROWS_PER_COLUMN]
            for row in rows:
                value = row["value"]
                if not isinstance(value, str) or not value.startswith(EXTENDED_PREFIX):
                    continue
                replacement = normalize(value)
                if replacement != value:
                    found.append(Hit(table, column, int(row["rid"]), value, replacement))
    return found, notes


class WindowsPathsTask(Task):
    name = "winpaths"
    title = "Windows extended-length paths"

    def unavailable(self, ctx: Context) -> str | None:
        if ctx.conn is None or ctx.schema is None:
            return f"{ctx.home.state_db.name} not found"
        return None

    def scan(self, ctx: Context) -> list[Finding]:
        hits, notes = _hits(ctx)
        findings: list[Finding] = []
        if not hits:
            findings.append(
                Finding(self.name, "hits", LEVEL_INFO, "no extended-length paths stored", count=0)
            )
        else:
            per_column: dict[str, int] = {}
            for hit in hits:
                per_column[f"{hit.table}.{hit.column}"] = per_column.get(f"{hit.table}.{hit.column}", 0) + 1
            findings.append(
                Finding(
                    self.name,
                    "hits",
                    LEVEL_NOTICE,
                    f"{len(hits)} value(s) across {len(per_column)} column(s) use the "
                    f"extended-length prefix",
                    count=len(hits),
                    detail={"columns": per_column},
                )
            )
            for label, count in sorted(per_column.items(), key=lambda kv: -kv[1])[: ctx.settings.limit]:
                findings.append(Finding(self.name, "hit_column", LEVEL_INFO, f"  {count:>6}  {label}"))
        for note in notes:
            findings.append(Finding(self.name, "truncated", LEVEL_WARN, note))
        return findings

    def plan(self, ctx: Context) -> list[Operation]:
        hits, _ = _hits(ctx)
        return [
            Operation(
                OP_SQL_UPDATE,
                self.name,
                f"normalise {hit.table}.{hit.column} row {hit.rowid}",
                {
                    "table": hit.table,
                    "key_column": "rowid",
                    "key": hit.rowid,
                    "set": {hit.column: hit.new},
                    "undo": {hit.column: hit.old},
                },
                key=operation_key(self.name, f"{hit.table}.{hit.column}"),
            )
            for hit in hits
        ]
