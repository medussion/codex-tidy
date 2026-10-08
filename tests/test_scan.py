"""Scanning must be observably inert."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_tidy.cli import main

from .fake_home import FakeHome, ThreadSpec


def snapshot(root: Path) -> dict[str, tuple[int, str]]:
    state = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            data = path.read_bytes()
            state[str(path.relative_to(root))] = (len(data), hashlib.sha256(data).hexdigest())
    return state


class ScanIsReadOnlyTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "codex"
        self.home = FakeHome(
            self.root,
            threads=[
                ThreadSpec("old-1", age_days=40, size_bytes=4096),
                ThreadSpec("recent-1", age_days=1),
                ThreadSpec("pinned-1", age_days=99, pinned=True),
                ThreadSpec("dangling-1", age_days=50, write_rollout=False),
            ],
        ).build()
        self.home.add_orphan()
        # A live project must be somewhere that is neither missing nor a temp
        # location, or the prune task would rightly flag it.
        self.home.add_config(live_project=Path.home(), dead_project="/nope/does/not/exist")
        self.home.add_worktree("stale-tree", age_days=30)
        self.home.add_log_db(4096)
        self.home.add_extended_path_row("\\\\?\\C:\\Users\\someone\\repo")
        self.addCleanup(self._tmp.cleanup)

    def test_scan_changes_nothing(self) -> None:
        before = snapshot(self.root)
        code = main(["scan", "--codex-home", str(self.root)])
        after = snapshot(self.root)

        self.assertEqual(code, 0)
        self.assertEqual(before, after, "scan modified the Codex home")
        self.assertFalse((self.root / ".codex-tidy.lock").exists(), "scan took a lock")

    def test_scan_text_withholds_the_operation_list(self) -> None:
        with mock.patch("sys.stdout") as stdout:
            main(["scan", "--codex-home", str(self.root)])
        text = "".join(call.args[0] for call in stdout.write.call_args_list)
        self.assertIn("Verdict", text)
        self.assertNotIn("Planned operations", text)

        with mock.patch("sys.stdout") as stdout:
            main(["plan", "--codex-home", str(self.root)])
        self.assertIn(
            "Planned operations", "".join(c.args[0] for c in stdout.write.call_args_list)
        )

    def test_scan_json_is_structured(self) -> None:
        with mock.patch("sys.stdout") as stdout:
            code = main(["scan", "--codex-home", str(self.root), "--json"])
        self.assertEqual(code, 0)
        payload = json.loads("".join(call.args[0] for call in stdout.write.call_args_list))
        self.assertEqual(payload["command"], "scan")
        self.assertIn("summary", payload)
        # scan computes the operation list so the verdict can weigh it, and
        # publishes it in JSON; only the text output withholds it.
        self.assertTrue(payload["operations"])
        self.assertIsNotNone(payload["assessment"])
        codes = {f"{f['task']}.{f['code']}" for f in payload["findings"]}
        self.assertIn("sessions.old_candidates", codes)
        self.assertIn("integrity.orphan_transcripts", codes)
        self.assertIn("integrity.dangling_rows", codes)

    def test_scan_hides_identifiers_by_default(self) -> None:
        with mock.patch("sys.stdout") as stdout:
            main(["scan", "--codex-home", str(self.root)])
        text = "".join(call.args[0] for call in stdout.write.call_args_list)
        self.assertNotIn("old-1", text)
        self.assertIn("thread:", text)

    def test_reveal_shows_identifiers(self) -> None:
        with mock.patch("sys.stdout") as stdout:
            main(["scan", "--codex-home", str(self.root), "--reveal"])
        text = "".join(call.args[0] for call in stdout.write.call_args_list)
        self.assertIn("old-1", text)


class PlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "codex"
        FakeHome(
            self.root,
            threads=[
                ThreadSpec("old-1", age_days=40, size_bytes=4096),
                ThreadSpec("recent-1", age_days=1),
            ],
        ).build()
        self.addCleanup(self._tmp.cleanup)

    def test_plan_lists_operations_without_touching_anything(self) -> None:
        before = snapshot(self.root)
        out = Path(self._tmp.name) / "plan.json"
        with mock.patch("sys.stdout"):
            code = main(
                ["plan", "--codex-home", str(self.root), "--only", "sessions", "--out", str(out)]
            )
        self.assertEqual(code, 0)
        self.assertEqual(before, snapshot(self.root))

        plan = json.loads(out.read_text(encoding="utf-8"))
        kinds = [op["kind"] for op in plan["operations"]]
        self.assertEqual(kinds, ["move_path", "sql_update"])
        self.assertEqual(plan["operations"][0]["bytes"], 4096)

    def test_unknown_task_name_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            main(["scan", "--codex-home", str(self.root), "--only", "nonsense"])

    def test_preview_limit_must_not_undercut_title_limit(self) -> None:
        with self.assertRaises(SystemExit):
            main(["scan", "--codex-home", str(self.root), "--title-limit", "200", "--preview-limit", "50"])


class MissingHomeTest(unittest.TestCase):
    def test_missing_home_is_an_environment_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code = main(["scan", "--codex-home", str(Path(tmp) / "absent")])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
