"""Task protocol.

A task does two things and nothing else:

* ``scan``  -- observe, return findings, touch nothing
* ``plan``  -- return the operations it *would* perform

Tasks never execute anything themselves. That belongs to the executor, which is
the only code that writes, and the only code the journal has to describe.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ..db import Schema
from ..env import CodexHome
from ..model import Finding, Operation, Settings
from ..privacy import Redactor


@dataclass
class Context:
    home: CodexHome
    settings: Settings
    redact: Redactor
    backup_root: Path
    stamp: str
    pinned: frozenset[str] = frozenset()
    schema: Schema | None = None
    conn: sqlite3.Connection | None = None
    writable: bool = False

    def requires_threads(self, *columns: str) -> str | None:
        """Skip reason when the live schema cannot support a task."""
        if self.conn is None or self.schema is None:
            return f"{self.home.state_db.name} not found"
        missing = self.schema.missing("threads", columns)
        if missing:
            return "threads schema is missing " + ", ".join(missing)
        return None


class Task:
    name: str = ""
    title: str = ""
    #: Report-only tasks never contribute operations.
    read_only: bool = False

    def unavailable(self, ctx: Context) -> str | None:
        """Return a human reason to skip, or None to run."""
        return None

    def scan(self, ctx: Context) -> list[Finding]:
        return []

    def plan(self, ctx: Context) -> list[Operation]:
        return []
