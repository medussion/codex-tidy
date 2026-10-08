"""SQLite access with an explicit schema contract.

Codex owns this database and can change its shape between releases. Every task
declares what it needs; anything unmet is skipped with a stated reason instead
of raising halfway through a mutation.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .env import canonical

BUSY_TIMEOUT_MS = 10_000
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def quote_identifier(name: str) -> str:
    """Reject anything that is not a plain identifier before interpolating it."""
    if not IDENTIFIER_RE.match(name):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return f'"{name}"'


def connect(path: Path, *, readonly: bool) -> sqlite3.Connection:
    """Open the database, read-only by default.

    A read-only connection to a WAL-mode database needs the ``-shm`` file to already
    exist, because it is not permitted to create one. Plain ``mode=ro`` therefore
    fails outright whenever the ``-shm`` is missing: a home that was copied without
    its sidecars, a directory mounted read-only, or a database SQLite has since
    tidied up. Codex is unreachable through no fault of the user.

    When no ``-wal`` is present there is nothing pending outside the main file, so
    it is self-contained and ``immutable=1`` is a safe fallback -- it skips the
    shared-memory index entirely. If a ``-wal`` *does* exist we must not use
    ``immutable``, because we would silently read pre-WAL data, so the original
    error is re-raised instead.
    """
    if not readonly:
        # isolation_level=None: we drive BEGIN/COMMIT explicitly during apply.
        conn = sqlite3.connect(str(path), isolation_level=None)
    else:
        uri = canonical(path).as_uri()
        try:
            conn = sqlite3.connect(f"{uri}?mode=ro", uri=True)
            # sqlite3.connect is lazy; force the real open so the fallback can fire.
            conn.execute("select count(*) from sqlite_master")
        except sqlite3.Error:
            try:
                conn.close()
            except (sqlite3.Error, NameError, UnboundLocalError):
                pass
            if Path(f"{path}-wal").exists():
                raise
            conn = sqlite3.connect(f"{uri}?mode=ro&immutable=1", uri=True)
            conn.execute("select count(*) from sqlite_master")
    conn.row_factory = sqlite3.Row
    conn.execute(f"pragma busy_timeout={BUSY_TIMEOUT_MS}")
    return conn


@dataclass(frozen=True)
class Schema:
    tables: dict[str, frozenset[str]]

    def has_table(self, table: str) -> bool:
        return table in self.tables

    def cols(self, table: str) -> frozenset[str]:
        return self.tables.get(table, frozenset())

    def has(self, table: str, *columns: str) -> bool:
        return set(columns).issubset(self.cols(table))

    def missing(self, table: str, columns: tuple[str, ...]) -> tuple[str, ...]:
        if not self.has_table(table):
            return (f"table {table}",)
        return tuple(sorted(set(columns) - set(self.cols(table))))

    @property
    def archived_columns(self) -> tuple[str, ...]:
        return tuple(c for c in ("archived", "archived_at") if c in self.cols("threads"))

    def active_threads_predicate(self) -> str:
        """SQL predicate for "not archived".

        When both markers exist we require both to be clear. That errs toward
        treating a row as already archived, which is the safe direction: it
        shrinks the candidate set rather than growing it.
        """
        clauses = []
        if "archived" in self.cols("threads"):
            clauses.append("coalesce(archived, 0) = 0")
        if "archived_at" in self.cols("threads"):
            clauses.append("archived_at is null")
        return " and ".join(clauses) if clauses else "1 = 1"

    def text_columns(self, table: str) -> tuple[str, ...]:
        return tuple(sorted(self.cols(table)))


def probe(conn: sqlite3.Connection) -> Schema:
    tables: dict[str, frozenset[str]] = {}
    rows = conn.execute(
        "select name from sqlite_master where type = 'table' and name not like 'sqlite_%'"
    ).fetchall()
    for row in rows:
        name = row["name"]
        try:
            info = conn.execute(f"pragma table_info({quote_identifier(name)})").fetchall()
        except (sqlite3.Error, ValueError):
            continue
        tables[name] = frozenset(col["name"] for col in info)
    return Schema(tables=tables)


def declared_text_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Columns whose declared type is TEXT or untyped, which SQLite treats loosely."""
    try:
        info = conn.execute(f"pragma table_info({quote_identifier(table)})").fetchall()
    except (sqlite3.Error, ValueError):
        return []
    found = []
    for col in info:
        declared = (col["type"] or "").upper()
        if declared == "" or "CHAR" in declared or "TEXT" in declared or "CLOB" in declared:
            found.append(col["name"])
    return found


def online_backup(src: Path, dst: Path) -> bool:
    """Consistent copy of a live SQLite file via the backup API."""
    if not src.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    source = target = None
    try:
        source = connect(src, readonly=True)
        target = sqlite3.connect(str(dst))
        source.backup(target)
    finally:
        if target is not None:
            target.close()
        if source is not None:
            source.close()
    return True


def checkpoint_and_optimize(conn: sqlite3.Connection, *, compact: bool) -> list[str]:
    """Best-effort space reclaim after a mutation. Never fatal."""
    notes = []
    for pragma in ("wal_checkpoint(truncate)", "optimize"):
        try:
            conn.execute(f"pragma {pragma}")
        except sqlite3.Error as exc:
            notes.append(f"{pragma} skipped: {exc}")
    if compact:
        try:
            conn.execute("vacuum")
        except sqlite3.Error as exc:
            notes.append(f"vacuum skipped: {exc}")
    return notes
