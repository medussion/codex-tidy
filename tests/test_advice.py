"""The verdict must follow the evidence, and item keys must group correctly."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_tidy import engine
from codex_tidy.advice import (
    SEVERITY_HIGH,
    SEVERITY_WATCH,
    VERDICT_FINE,
    VERDICT_NOW,
    VERDICT_SOON,
    assess,
)
from codex_tidy.env import resolve_codex_home
from codex_tidy.model import Settings

from .fake_home import FakeHome, ThreadSpec

MB = 1024**2


def scan(root: Path, **overrides) -> "engine.Plan":
    settings = Settings(**overrides)
    stamp = engine.fresh_stamp()
    home = resolve_codex_home(root)
    session = engine.open_session(
        home,
        settings,
        writable=False,
        stamp=stamp,
        backup_root=engine.resolve_backup_root(home, settings, stamp),
    )
    try:
        return engine.scan(session, with_operations=True)
    finally:
        session.close()


class VerdictTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "codex"
        self.addCleanup(self._tmp.cleanup)

    def _signal(self, assessment, code):
        return next((s for s in assessment.signals if s.code == code), None)

    def test_quiet_home_is_fine(self) -> None:
        FakeHome(self.root, threads=[ThreadSpec("t1", age_days=0, size_bytes=512)]).build()
        assessment = assess(scan(self.root))
        self.assertEqual(assessment.verdict, VERDICT_FINE)
        self.assertEqual(assessment.reclaimable_bytes, 0)

    def test_a_little_old_data_says_soon(self) -> None:
        FakeHome(
            self.root,
            threads=[ThreadSpec("old", age_days=40, size_bytes=120 * MB)],
        ).build()
        assessment = assess(scan(self.root))
        self.assertEqual(assessment.verdict, VERDICT_SOON)
        self.assertEqual(self._signal(assessment, "hot_path").severity, SEVERITY_WATCH)

    def test_a_lot_of_old_data_says_now(self) -> None:
        FakeHome(
            self.root,
            threads=[ThreadSpec("old", age_days=40, size_bytes=600 * MB)],
        ).build()
        assessment = assess(scan(self.root))
        self.assertEqual(assessment.verdict, VERDICT_NOW)
        self.assertEqual(self._signal(assessment, "hot_path").severity, SEVERITY_HIGH)

    def test_pathological_metadata_alone_says_now(self) -> None:
        FakeHome(
            self.root,
            threads=[ThreadSpec("bloated", age_days=0, preview="P" * 40_000)],
        ).build()
        assessment = assess(scan(self.root))
        self.assertEqual(assessment.verdict, VERDICT_NOW)
        signal = self._signal(assessment, "metadata")
        self.assertEqual(signal.severity, SEVERITY_HIGH)
        self.assertEqual(signal.value, 40_000)

    def test_dangling_rows_never_read_as_high(self) -> None:
        FakeHome(
            self.root,
            threads=[ThreadSpec("gone", age_days=0, write_rollout=False)],
        ).build()
        assessment = assess(scan(self.root))
        self.assertEqual(self._signal(assessment, "dangling").severity, SEVERITY_WATCH)
        self.assertIn("dangling_rows", [g.code for g in assessment.guidance])

    def test_handoff_guidance_appears_when_sessions_would_move(self) -> None:
        FakeHome(
            self.root, threads=[ThreadSpec("old", age_days=40, size_bytes=4096)]
        ).build()
        assessment = assess(scan(self.root))
        handoff = next(g for g in assessment.guidance if g.code == "handoff_before_archive")
        self.assertEqual(handoff.params["count"], 1)

    def test_pinned_sessions_are_reported_as_protected(self) -> None:
        FakeHome(
            self.root,
            threads=[ThreadSpec("pin", age_days=99, size_bytes=4096, pinned=True)],
        ).build()
        assessment = assess(scan(self.root))
        self.assertIn("pinned_protected", [g.code for g in assessment.guidance])
        self.assertEqual(assessment.verdict, VERDICT_FINE)


class PlanItemTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "codex"
        FakeHome(
            self.root,
            threads=[
                ThreadSpec("old-1", age_days=40, size_bytes=4096),
                ThreadSpec("old-2", age_days=30, size_bytes=2048),
            ],
        ).build()
        self.addCleanup(self._tmp.cleanup)

    def test_one_session_is_one_item_with_two_operations(self) -> None:
        plan = scan(self.root)
        items = plan.items()
        self.assertEqual(len(items), 2)
        self.assertTrue(all(item.operations == 2 for item in items))
        self.assertEqual(sorted(i.bytes for i in items), [2048, 4096])

    def test_keys_do_not_leak_identifiers(self) -> None:
        for item in scan(self.root).items():
            self.assertTrue(item.key.startswith("sessions:"))
            self.assertNotIn("old-1", item.key)
            self.assertNotIn("old-2", item.key)

    def test_excluding_an_item_drops_all_of_its_operations(self) -> None:
        plan = scan(self.root)
        victim = next(i for i in plan.items() if i.bytes == 4096)
        filtered = engine.filter_plan(plan, {victim.key})

        self.assertEqual(len(filtered.operations), 2)
        self.assertEqual(len(filtered.items()), 1)
        self.assertEqual(filtered.reclaimable_bytes, 2048)
        # The findings are untouched, so the report still explains the full picture.
        self.assertEqual(len(filtered.findings), len(plan.findings))

    def test_excluding_everything_leaves_nothing_to_do(self) -> None:
        plan = scan(self.root)
        filtered = engine.filter_plan(plan, {i.key for i in plan.items()})
        self.assertEqual(filtered.operations, [])


if __name__ == "__main__":
    unittest.main()
