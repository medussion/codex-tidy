"""Apply must be journalled, and every journalled apply must be undoable."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_tidy.cli import main

from .fake_home import FakeHome, ThreadSpec

NO_CODEX = {"codex_tidy.engine.codex_processes": [], "codex_tidy.cli.codex_processes": []}


def patch_no_codex():
    return mock.patch.multiple(
        "codex_tidy.engine", codex_processes=mock.Mock(return_value=[])
    ), mock.patch("codex_tidy.cli.codex_processes", return_value=[])


class ApplyTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.root = self.base / "codex"
        self.backups = self.base / "backups"
        self.addCleanup(self._tmp.cleanup)
        self._patchers = [
            mock.patch("codex_tidy.engine.codex_processes", return_value=[]),
            mock.patch("codex_tidy.cli.codex_processes", return_value=[]),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_cli(self, *args: str) -> int:
        with mock.patch("sys.stdout"):
            return main([*args, "--codex-home", str(self.root), "--backup-root", str(self.backups)])

    def journal(self) -> Path:
        found = sorted(self.backups.glob("*/journal.jsonl"))
        self.assertTrue(found, "no journal was written")
        return found[-1]


class SessionArchiveTest(ApplyTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.home = FakeHome(
            self.root,
            threads=[
                ThreadSpec("old-1", age_days=40, size_bytes=4096),
                ThreadSpec("old-2", age_days=20, size_bytes=2048),
                ThreadSpec("recent", age_days=1, size_bytes=1024),
                ThreadSpec("pinned-old", age_days=99, size_bytes=8192, pinned=True),
            ],
        ).build()

    def _rollout(self, thread_id: str) -> Path:
        return Path(self.home.thread_row(thread_id)["rollout_path"])

    def test_old_sessions_move_and_rows_follow(self) -> None:
        original = {tid: self._rollout(tid) for tid in ("old-1", "old-2", "recent", "pinned-old")}

        code = self.run_cli("apply", "--only", "sessions", "--yes", "--session-age-days", "10")
        self.assertEqual(code, 0)

        for tid in ("old-1", "old-2"):
            row = self.home.thread_row(tid)
            new_path = Path(row["rollout_path"])
            self.assertFalse(original[tid].exists(), f"{tid} was not moved")
            self.assertTrue(new_path.is_file(), f"{tid} is not at its new location")
            self.assertIn("archived_sessions", new_path.parts)
            self.assertEqual(row["archived"], 1)
            self.assertIsNotNone(row["archived_at"])

        for tid in ("recent", "pinned-old"):
            row = self.home.thread_row(tid)
            self.assertTrue(original[tid].exists(), f"{tid} should not have moved")
            self.assertEqual(row["archived"], 0)

    def test_backup_and_journal_are_written(self) -> None:
        self.run_cli("apply", "--only", "sessions", "--yes")
        run_dir = self.journal().parent
        self.assertTrue((run_dir / "state_5.sqlite").is_file(), "database was not backed up")
        self.assertTrue((run_dir / "plan.json").is_file())
        self.assertTrue((run_dir / ".codex-global-state.json").is_file())

        records = [json.loads(line) for line in self.journal().read_text().splitlines()]
        kinds = [r["kind"] for r in records]
        self.assertEqual(kinds[0], "header")
        self.assertEqual(kinds[-1], "summary")
        self.assertEqual(records[-1]["outcome"], "applied")
        # Every completed operation carries its own undo payload.
        for record in records:
            if record["kind"] == "done" and record["op"] != "append_jsonl":
                self.assertIsNotNone(record["undo"], f"{record['op']} has no undo payload")

    def test_restore_puts_everything_back(self) -> None:
        before = {tid: self.home.thread_row(tid) for tid in ("old-1", "old-2")}
        self.run_cli("apply", "--only", "sessions", "--yes")
        self.assertEqual(self.run_cli("restore", "--yes"), 0)

        for tid, row_before in before.items():
            row_after = self.home.thread_row(tid)
            self.assertEqual(row_after["rollout_path"], row_before["rollout_path"])
            self.assertEqual(row_after["archived"], 0)
            self.assertIsNone(row_after["archived_at"])
            self.assertTrue(Path(row_after["rollout_path"]).is_file())

    def test_lock_blocks_a_second_run(self) -> None:
        import os

        (self.root / ".codex-tidy.lock").write_text(
            json.dumps({"pid": os.getpid(), "started": "now"}), encoding="utf-8"
        )
        self.assertEqual(self.run_cli("apply", "--only", "sessions", "--yes"), 3)

    def test_stale_lock_is_reclaimed(self) -> None:
        (self.root / ".codex-tidy.lock").write_text(
            json.dumps({"pid": 999_999_999, "started": "long ago"}), encoding="utf-8"
        )
        self.assertEqual(self.run_cli("apply", "--only", "sessions", "--yes"), 0)
        self.assertFalse((self.root / ".codex-tidy.lock").exists())

    def test_running_codex_blocks_apply(self) -> None:
        from codex_tidy.env import ProcInfo

        with mock.patch(
            "codex_tidy.engine.codex_processes",
            return_value=[ProcInfo(pid=1, name="Codex", command="codex app-server")],
        ):
            self.assertEqual(self.run_cli("apply", "--only", "sessions", "--yes"), 3)
        self.assertTrue(self._rollout("old-1").is_file(), "nothing should have moved")

    def test_size_cap_blocks_apply(self) -> None:
        code = self.run_cli("apply", "--only", "sessions", "--yes", "--max-archive-gb", "0.000001")
        self.assertEqual(code, 3)
        self.assertTrue(self._rollout("old-1").is_file())

    def test_min_size_filter_excludes_small_sessions(self) -> None:
        code = self.run_cli(
            "apply", "--only", "sessions", "--yes", "--session-min-mb", "1", "--session-age-days", "10"
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.home.thread_row("old-1")["archived"], 0)


class ThreadMetadataTest(ApplyTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.long_title = "T" * 5_000
        self.long_preview = "P" * 40_000
        self.home = FakeHome(
            self.root,
            threads=[ThreadSpec("bloated", title=self.long_title, preview=self.long_preview)],
        ).build()

    def test_trim_is_opt_in(self) -> None:
        self.assertEqual(self.run_cli("apply", "--only", "thread-meta", "--yes"), 0)
        self.assertEqual(self.home.thread_row("bloated")["title"], self.long_title)

    def test_trim_and_restore(self) -> None:
        code = self.run_cli(
            "apply", "--only", "thread-meta", "--yes", "--repair-thread-metadata",
            "--title-limit", "40", "--preview-limit", "80",
        )
        self.assertEqual(code, 0)

        row = self.home.thread_row("bloated")
        self.assertEqual(len(row["title"]), 40)
        self.assertTrue(row["title"].endswith("..."))
        self.assertEqual(len(row["first_user_message"]), 80)
        # The rename is mirrored where Codex records renames.
        index = (self.root / "session_index.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(index[0])["id"], "bloated")

        self.assertEqual(self.run_cli("restore", "--yes"), 0)
        restored = self.home.thread_row("bloated")
        self.assertEqual(restored["title"], self.long_title)
        self.assertEqual(restored["first_user_message"], self.long_preview)


class WindowsPathTest(ApplyTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.home = FakeHome(self.root, threads=[ThreadSpec("t1")]).build()
        self.home.add_extended_path_row("\\\\?\\C:\\Users\\someone\\repo")
        self.home.add_extended_path_row("C:\\Users\\someone\\other")

    def _paths(self) -> list[str]:
        conn = sqlite3.connect(self.home.state_db)
        rows = [row[0] for row in conn.execute("select path from project_paths order by id")]
        conn.close()
        return rows

    def test_normalise_then_restore(self) -> None:
        self.assertEqual(self.run_cli("apply", "--only", "winpaths", "--yes"), 0)
        self.assertEqual(
            self._paths(), ["C:\\Users\\someone\\repo", "C:\\Users\\someone\\other"]
        )
        self.assertEqual(self.run_cli("restore", "--yes"), 0)
        self.assertEqual(
            self._paths(), ["\\\\?\\C:\\Users\\someone\\repo", "C:\\Users\\someone\\other"]
        )


class WorktreeTest(ApplyTestCase):
    def setUp(self) -> None:
        super().setUp()
        FakeHome(self.root, threads=[ThreadSpec("t1")]).build()
        self.home = FakeHome(self.root)
        self.stale = self.home.add_worktree("stale", age_days=30)
        self.fresh = self.home.add_worktree("fresh", age_days=0)

    def test_only_stale_worktrees_move(self) -> None:
        code = self.run_cli("apply", "--only", "worktrees", "--yes", "--worktree-age-days", "7")
        self.assertEqual(code, 0)
        self.assertFalse(self.stale.exists())
        self.assertTrue(self.fresh.exists())
        moved = list((self.root / "archived_worktrees").rglob("stale"))
        self.assertEqual(len(moved), 1)

    def test_dirty_worktree_is_excluded(self) -> None:
        with mock.patch("codex_tidy.tasks.worktrees._git_dirty", return_value=True):
            code = self.run_cli("apply", "--only", "worktrees", "--yes", "--worktree-age-days", "7")
        self.assertEqual(code, 0)
        self.assertTrue(self.stale.exists(), "a dirty worktree must not be archived by default")


class LogRotationTest(ApplyTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.home = FakeHome(self.root, threads=[ThreadSpec("t1")]).build()
        self.log = self.home.add_log_db(8192)

    def test_rotation_moves_log_databases(self) -> None:
        code = self.run_cli("apply", "--only", "logs", "--yes", "--log-rotate-mb", "0")
        self.assertEqual(code, 0)
        self.assertFalse(self.log.exists())
        self.assertEqual(len(list((self.root / "archived_logs").rglob("logs_2.sqlite"))), 1)

    def test_below_threshold_does_nothing(self) -> None:
        code = self.run_cli("apply", "--only", "logs", "--yes", "--log-rotate-mb", "64")
        self.assertEqual(code, 0)
        self.assertTrue(self.log.exists())


class OrphanTest(ApplyTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.home = FakeHome(self.root, threads=[ThreadSpec("t1", age_days=0)]).build()
        self.orphan = self.home.add_orphan("stray.jsonl", size=3072)

    def test_orphans_are_opt_in(self) -> None:
        self.assertEqual(self.run_cli("apply", "--only", "integrity", "--yes"), 0)
        self.assertTrue(self.orphan.exists())

    def test_orphans_move_and_restore(self) -> None:
        code = self.run_cli("apply", "--only", "integrity", "--yes", "--archive-orphan-transcripts")
        self.assertEqual(code, 0)
        self.assertFalse(self.orphan.exists())
        self.assertEqual(self.run_cli("restore", "--yes"), 0)
        self.assertTrue(self.orphan.exists())


class RollbackTest(ApplyTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.home = FakeHome(
            self.root,
            threads=[
                ThreadSpec("old-1", age_days=40, size_bytes=4096),
                ThreadSpec("old-2", age_days=30, size_bytes=2048),
            ],
        ).build()

    def test_failure_midway_rolls_everything_back(self) -> None:
        before = {tid: self.home.thread_row(tid) for tid in ("old-1", "old-2")}
        real_perform = None

        from codex_tidy import ops as ops_module

        real_perform = ops_module.perform
        calls = {"n": 0}

        def flaky(kind, payload, *, conn):
            calls["n"] += 1
            if calls["n"] == 3:  # succeed on the first session, fail on the second
                raise ops_module.OperationError("injected failure")
            return real_perform(kind, payload, conn=conn)

        with mock.patch("codex_tidy.engine.perform_operation", side_effect=lambda op, conn: flaky(op.kind, op.payload, conn=conn)):
            code = self.run_cli("apply", "--only", "sessions", "--yes")

        self.assertEqual(code, 4, "a failed apply must not report success")
        for tid, row_before in before.items():
            row_after = self.home.thread_row(tid)
            self.assertEqual(row_after["rollout_path"], row_before["rollout_path"])
            self.assertEqual(row_after["archived"], 0)
            self.assertTrue(Path(row_after["rollout_path"]).is_file(), f"{tid} was left moved")

        records = [json.loads(line) for line in self.journal().read_text().splitlines()]
        self.assertEqual(records[-1]["outcome"], "rolled_back")


class DoctorTest(ApplyTestCase):
    def setUp(self) -> None:
        super().setUp()
        FakeHome(self.root, threads=[ThreadSpec("t1")]).build()

    def test_doctor_reports_schema_and_task_availability(self) -> None:
        with mock.patch("sys.stdout") as stdout:
            code = main(["doctor", "--codex-home", str(self.root), "--json"])
        self.assertEqual(code, 0)
        payload = json.loads("".join(call.args[0] for call in stdout.write.call_args_list))
        self.assertIn("rollout_path", payload["threads_columns"])
        self.assertEqual(payload["tasks"]["sessions"], "available")
        self.assertIn("config.toml", payload["tasks"]["config"])


if __name__ == "__main__":
    unittest.main()
