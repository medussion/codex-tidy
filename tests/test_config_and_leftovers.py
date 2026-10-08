"""config.toml rewriting must be conservative, and leftovers must be recognised."""

from __future__ import annotations

import json
import os
import time
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from codex_tidy.cli import main
from codex_tidy.tasks.config_toml import _plan_prune

from .fake_home import FakeHome, ThreadSpec


class ConfigPruneTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.root = self.base / "codex"
        self.backups = self.base / "backups"
        self.home = FakeHome(self.root, threads=[ThreadSpec("t1")]).build()
        self.config = self.home.add_config(
            live_project=Path.home(), dead_project="/nope/does/not/exist"
        )
        self.addCleanup(self._tmp.cleanup)
        for patcher in (
            mock.patch("codex_tidy.engine.codex_processes", return_value=[]),
            mock.patch("codex_tidy.cli.codex_processes", return_value=[]),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_cli(self, *args: str) -> int:
        with mock.patch("sys.stdout"):
            return main([*args, "--codex-home", str(self.root), "--backup-root", str(self.backups)])

    def test_prune_keeps_everything_outside_projects(self) -> None:
        self.assertEqual(self.run_cli("apply", "--only", "config", "--yes"), 0)

        data = tomllib.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(data["model"], "gpt-5")
        self.assertEqual(data["tui"]["theme"], "dark")
        self.assertEqual(list(data["projects"]), [Path.home().as_posix()])

    def test_restore_brings_the_config_back(self) -> None:
        before = self.config.read_text(encoding="utf-8")
        self.assertEqual(self.run_cli("apply", "--only", "config", "--yes"), 0)
        self.assertNotEqual(self.config.read_text(encoding="utf-8"), before)
        self.assertEqual(self.run_cli("restore", "--yes"), 0)
        self.assertEqual(self.config.read_text(encoding="utf-8"), before)

    def test_unparseable_config_is_left_alone(self) -> None:
        self.config.write_text("this is [not valid toml\n", encoding="utf-8")
        result = _plan_prune(self.config)
        self.assertIsNone(result.new_text)
        self.assertIn("does not parse", result.problem or "")

        self.assertEqual(self.run_cli("apply", "--only", "config", "--yes"), 0)
        self.assertEqual(self.config.read_text(encoding="utf-8"), "this is [not valid toml\n")

    def test_project_subtables_are_removed_with_their_parent(self) -> None:
        self.config.write_text(
            'model = "gpt-5"\n'
            '\n'
            '[projects."/nope/does/not/exist"]\n'
            'trust_level = "trusted"\n'
            '\n'
            '[projects."/nope/does/not/exist".extra]\n'
            'flag = true\n'
            '\n'
            f'[projects."{Path.home().as_posix()}"]\n'
            'trust_level = "trusted"\n',
            encoding="utf-8",
        )
        result = _plan_prune(self.config)
        self.assertIsNotNone(result.new_text)
        self.assertNotIn("extra", result.new_text or "")
        parsed = tomllib.loads(result.new_text or "")
        self.assertEqual(list(parsed["projects"]), [Path.home().as_posix()])


class LeftoverTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.root = self.base / "codex"
        self.backups = self.base / "backups"
        FakeHome(self.root, threads=[ThreadSpec("t1")]).build()
        self.addCleanup(self._tmp.cleanup)
        for patcher in (
            mock.patch("codex_tidy.engine.codex_processes", return_value=[]),
            mock.patch("codex_tidy.cli.codex_processes", return_value=[]),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        self.old_leftover = self.root / "..codex-global-state.json.tmp-1784256439073-abc"
        self.old_leftover.write_bytes(b"o" * 4096)
        aged = time.time() - 30 * 86_400
        os.utime(self.old_leftover, (aged, aged))

        self.fresh_leftover = self.root / "..codex-global-state.json.tmp-9999999999999-def"
        self.fresh_leftover.write_bytes(b"n" * 4096)

        self.keep_bak = self.root / ".codex-global-state.json.bak"
        self.keep_bak.write_bytes(b"b" * 128)

    def run_cli(self, *args: str) -> int:
        with mock.patch("sys.stdout"):
            return main([*args, "--codex-home", str(self.root), "--backup-root", str(self.backups)])

    def test_scan_reports_only_aged_leftovers(self) -> None:
        with mock.patch("sys.stdout") as stdout:
            main(["scan", "--codex-home", str(self.root), "--only", "leftovers", "--json"])
        payload = json.loads("".join(call.args[0] for call in stdout.write.call_args_list))
        summary = next(f for f in payload["findings"] if f["code"] == "candidates")
        self.assertEqual(summary["count"], 1)
        self.assertEqual(summary["bytes"], 4096)

    def test_archiving_is_opt_in_and_reversible(self) -> None:
        self.assertEqual(self.run_cli("apply", "--only", "leftovers", "--yes"), 0)
        self.assertTrue(self.old_leftover.exists(), "leftovers must not move without opting in")

        self.assertEqual(
            self.run_cli("apply", "--only", "leftovers", "--yes", "--archive-leftovers"), 0
        )
        self.assertFalse(self.old_leftover.exists())
        self.assertTrue(self.fresh_leftover.exists(), "an in-flight write must not be disturbed")
        self.assertTrue(self.keep_bak.exists(), "the .bak safety net must never be touched")

        self.assertEqual(self.run_cli("restore", "--yes"), 0)
        self.assertTrue(self.old_leftover.exists())


if __name__ == "__main__":
    unittest.main()
