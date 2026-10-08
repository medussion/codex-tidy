"""A WAL-mode database copied without its sidecars must still be readable.

SQLite builds differ here: some can open the copied database with ``mode=ro``
and create a fresh ``-shm`` when the directory is writable, while others fail
until ``immutable=1`` is used. The tests drive both branches explicitly and
still forbid the immutable fallback when a real ``-wal`` is pending.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_tidy import db
from codex_tidy.cli import main

from .fake_home import FakeHome, ThreadSpec


class WalDatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        source_root = Path(self._tmp.name) / "source"
        self.root = Path(self._tmp.name) / "codex"
        self.addCleanup(self._tmp.cleanup)

        source = FakeHome(
            source_root, threads=[ThreadSpec("old-1", age_days=40, size_bytes=4096)]
        ).build()
        conn = sqlite3.connect(source.state_db)
        conn.execute("pragma journal_mode=wal").fetchone()
        conn.execute("update threads set title = 'still here' where id = 'old-1'")
        conn.commit()
        conn.close()

        # Copy the home the way a partial backup would: main database only.
        shutil.copytree(
            source_root,
            self.root,
            ignore=shutil.ignore_patterns("*.sqlite-wal", "*.sqlite-shm"),
        )
        self.home = FakeHome(self.root)

    def test_the_database_copy_has_no_sidecars(self) -> None:
        self.assertFalse(Path(f"{self.home.state_db}-wal").exists())
        self.assertFalse(Path(f"{self.home.state_db}-shm").exists())

        # immutable=1 reads only the main file, which verifies that the copied
        # fixture is self-contained without assuming version-specific mode=ro
        # behaviour.
        target = f"{self.home.state_db.resolve().as_uri()}?mode=ro&immutable=1"
        conn = sqlite3.connect(target, uri=True)
        try:
            row = conn.execute("select title from threads where id = 'old-1'").fetchone()
            self.assertEqual(row[0], "still here")
        finally:
            conn.close()

    def test_readonly_connect_still_works(self) -> None:
        conn = db.connect(self.home.state_db, readonly=True)
        try:
            row = conn.execute("select title from threads where id = 'old-1'").fetchone()
            self.assertEqual(row["title"], "still here")
        finally:
            conn.close()
        # Some SQLite builds create a fresh -shm for the successful mode=ro
        # connection. That is safe and is not evidence that the fallback failed.

    def test_immutable_fallback_is_refused_when_a_wal_exists(self) -> None:
        """The fallback must never engage while a -wal could hold newer data.

        Driven by forcing the plain open to fail, because SQLite's own handling of a
        -wal without a -shm varies; what must hold is our branch: with a -wal
        present, re-raise rather than reach for immutable=1.
        """
        Path(f"{self.home.state_db}-wal").write_bytes(b"\x00" * 32)
        real_connect = sqlite3.connect
        attempts: list[str] = []

        def failing_connect(target, *args, **kwargs):
            if isinstance(target, str) and target.startswith("file:"):
                attempts.append(target)
                raise sqlite3.OperationalError("unable to open database file")
            return real_connect(target, *args, **kwargs)

        with mock.patch("codex_tidy.db.sqlite3.connect", side_effect=failing_connect):
            with self.assertRaises(sqlite3.Error):
                db.connect(self.home.state_db, readonly=True)

        self.assertEqual(len(attempts), 1, "it retried despite a pending -wal")
        self.assertNotIn("immutable", attempts[0])

    def test_immutable_fallback_engages_without_a_wal(self) -> None:
        real_connect = sqlite3.connect
        attempts: list[str] = []

        def recording_connect(target, *args, **kwargs):
            if isinstance(target, str) and target.startswith("file:"):
                attempts.append(target)
                if "immutable=1" not in target:
                    raise sqlite3.OperationalError("unable to open database file")
            return real_connect(target, *args, **kwargs)

        with mock.patch("codex_tidy.db.sqlite3.connect", side_effect=recording_connect):
            conn = db.connect(self.home.state_db, readonly=True)
        conn.close()

        self.assertEqual(len(attempts), 2, f"expected a retry, got {attempts}")
        self.assertNotIn("immutable", attempts[0])
        self.assertIn("immutable=1", attempts[1])

    def test_scan_works_on_a_home_without_sidecars(self) -> None:
        with mock.patch("sys.stdout") as stdout:
            code = main(["scan", "--codex-home", str(self.root), "--only", "thread-meta", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads("".join(call.args[0] for call in stdout.write.call_args_list))
        totals = next(f for f in payload["findings"] if f["code"] == "totals")
        self.assertEqual(totals["count"], 1, "the database was not actually read")
        self.assertEqual(payload["skipped"], {})

    def test_live_wal_is_read_through_mode_ro(self) -> None:
        """With both sidecars present, mode=ro succeeds and sees committed WAL data."""
        holder = sqlite3.connect(self.home.state_db)
        holder.execute("pragma journal_mode=wal").fetchone()
        holder.execute("update threads set title = 'newest' where id = 'old-1'")
        holder.commit()
        try:
            conn = db.connect(self.home.state_db, readonly=True)
            try:
                row = conn.execute("select title from threads where id = 'old-1'").fetchone()
                self.assertEqual(row["title"], "newest")
            finally:
                conn.close()
        finally:
            holder.close()


if __name__ == "__main__":
    unittest.main()
