"""Synthetic Codex home so tests never touch a real installation."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

THREADS_SCHEMA = """
create table threads (
  id                 text primary key,
  title              text,
  name               text,
  thread_source      text,
  is_pinned          integer default 0,
  first_user_message text,
  rollout_path       text,
  updated_at         integer,
  archived           integer default 0,
  archived_at        integer
);
"""

PROJECTS_SCHEMA = """
create table project_paths (
  id   integer primary key,
  path text
);
"""


@dataclass
class ThreadSpec:
    thread_id: str
    title: str = "a thread"
    preview: str = "hello"
    age_days: float = 0.0
    size_bytes: int = 1024
    pinned: bool = False
    name: str = ""
    thread_source: str = "user"
    write_rollout: bool = True
    rollout_outside: bool = False


@dataclass
class FakeHome:
    root: Path
    threads: list[ThreadSpec] = field(default_factory=list)

    @property
    def state_db(self) -> Path:
        return self.root / "state_5.sqlite"

    def build(self) -> "FakeHome":
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "sessions").mkdir(exist_ok=True)
        (self.root / "worktrees").mkdir(exist_ok=True)

        conn = sqlite3.connect(self.state_db)
        conn.executescript(THREADS_SCHEMA)
        conn.executescript(PROJECTS_SCHEMA)

        pinned = []
        now = int(time.time())
        for spec in self.threads:
            rollout_path = None
            if spec.write_rollout:
                if spec.rollout_outside:
                    target = self.root / "elsewhere" / f"{spec.thread_id}.jsonl"
                else:
                    target = self.root / "sessions" / "2026" / "01" / f"{spec.thread_id}.jsonl"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"x" * spec.size_bytes)
                rollout_path = str(target)
            elif spec.rollout_outside is False:
                # Row points at a transcript that was never written: dangling.
                rollout_path = str(
                    self.root / "sessions" / "2026" / "01" / f"{spec.thread_id}.jsonl"
                )

            conn.execute(
                "insert into threads (id, title, name, thread_source, is_pinned, first_user_message, rollout_path, updated_at,"
                " archived, archived_at) values (?, ?, ?, ?, ?, ?, ?, ?, 0, null)",
                (
                    spec.thread_id,
                    spec.title,
                    spec.name,
                    spec.thread_source,
                    int(spec.pinned),
                    spec.preview,
                    rollout_path,
                    now - int(spec.age_days * 86_400),
                ),
            )
            if spec.pinned:
                pinned.append(spec.thread_id)

        conn.commit()
        conn.close()

        (self.root / ".codex-global-state.json").write_text(
            json.dumps({"pinned-thread-ids": pinned}), encoding="utf-8"
        )
        return self

    # -- convenience helpers -------------------------------------------------

    def add_orphan(self, name: str = "orphan.jsonl", size: int = 2048) -> Path:
        target = self.root / "sessions" / "2026" / "01" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"y" * size)
        return target

    def add_config(self, live_project: Path, dead_project: str) -> Path:
        config = self.root / "config.toml"
        config.write_text(
            "model = \"gpt-5\"\n"
            "\n"
            "[tui]\n"
            "theme = \"dark\"\n"
            "\n"
            f'[projects."{live_project.as_posix()}"]\n'
            "trust_level = \"trusted\"\n"
            "\n"
            f'[projects."{dead_project}"]\n'
            "trust_level = \"trusted\"\n",
            encoding="utf-8",
        )
        return config

    def add_worktree(self, name: str, age_days: float, size: int = 512) -> Path:
        target = self.root / "worktrees" / name
        target.mkdir(parents=True, exist_ok=True)
        (target / "file.txt").write_bytes(b"z" * size)
        stamp = time.time() - age_days * 86_400
        import os

        os.utime(target, (stamp, stamp))
        return target

    def add_log_db(self, size: int) -> Path:
        target = self.root / "logs_2.sqlite"
        target.write_bytes(b"l" * size)
        return target

    def add_extended_path_row(self, value: str) -> None:
        conn = sqlite3.connect(self.state_db)
        conn.execute("insert into project_paths (path) values (?)", (value,))
        conn.commit()
        conn.close()

    def thread_row(self, thread_id: str) -> dict:
        conn = sqlite3.connect(self.state_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute("select * from threads where id = ?", (thread_id,)).fetchone()
        conn.close()
        return dict(row) if row else {}
