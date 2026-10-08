"""Handoff extraction must stay local, bounded, fresh, and evidence based."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from codex_tidy.env import resolve_codex_home
from codex_tidy.handoff import (
    default_handoff_root,
    display_title,
    extract_transcript,
    generate_handoff,
    handoff_status,
    inspect_repository,
    list_sessions,
)
from codex_tidy.privacy import Redactor

from .fake_home import FakeHome, ThreadSpec


def message(role: str, text: str, *, phase: str | None = None) -> dict:
    payload = {"type": "message", "role": role, "content": [{"type": "input_text", "text": text}]}
    if phase:
        payload["phase"] = phase
    return {"type": "response_item", "payload": payload}


class HandoffTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.root = base / "codex"
        self.fixture = FakeHome(
            self.root,
            threads=[ThreadSpec("thread-1", title="Important work", age_days=30)],
        ).build()
        self.rollout = Path(self.fixture.thread_row("thread-1")["rollout_path"])
        records = [
            {"type": "session_meta", "payload": {"cwd": str(base / "repo"), "git": {"branch": "main"}}},
            message("user", "Preserve dirty work. Fix src/app.py and run tests."),
            {"type": "compacted", "payload": {"replacement_history": ["x" * 1000]}},
            {"type": "turn_context", "payload": {"summary": "summary:" + "S" * 6000 + ":END"}},
            {"type": "response_item", "payload": {"type": "custom_tool_call", "input": json.dumps({"cmd": "pytest tests", "workdir": str(base / "repo")})}},
            {"type": "response_item", "payload": {"type": "custom_tool_call_output", "output": "FAILED test_app timeout"}},
            message("assistant", "Implemented the parser. Remaining: fix the timeout.", phase="final"),
        ]
        self.rollout.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
        self.home = resolve_codex_home(self.root)
        self.handoff_root = default_handoff_root(self.home, base / "backups")
        self.addCleanup(self._tmp.cleanup)

    def test_extractor_keeps_evidence_and_discards_compaction_bulk(self) -> None:
        extracted = extract_transcript(self.rollout)
        self.assertIn("Preserve dirty work", extracted.users[0])
        self.assertIn("pytest tests", extracted.commands[0])
        self.assertIn("FAILED", extracted.errors[0])
        self.assertNotIn("replacement_history", " ".join(extracted.finals))

    def test_display_title_removes_injected_environment_blocks(self) -> None:
        raw = (
            "<environment_context><cwd>/private/project</cwd></environment_context> "
            "세션 정리 GUI를 구현해줘. 그 다음 문장"
        )
        self.assertEqual(display_title(raw), "세션 정리 GUI를 구현해줘.")

    def test_display_title_recovers_user_request_from_approval_history(self) -> None:
        raw = (
            "The following is the Codex agent history whose request action you are assessing.\n"
            ">>> TRANSCRIPT START\n[1] user: 커밋을 확인하고 다음 작업을 분석해줘\n\n"
            "[2] tool exec_command call: {}"
        )
        self.assertEqual(display_title(raw), "커밋을 확인하고 다음 작업을 분석해줘")

    def test_display_title_prefers_descriptive_request_over_generic_followup(self) -> None:
        raw = (
            "The following is the Codex agent history whose request action you are assessing.\n"
            ">>> TRANSCRIPT START\n[1] user: 오케이 진행해\n\n"
            "[2] assistant: 인증 모듈의 실패 복구 로직을 구현하겠습니다.\n\n"
            "[3] user: 인증 실패 시 롤백하고 회귀 테스트까지 추가해줘\n\n"
            "[4] tool exec_command call: {}"
        )
        self.assertEqual(display_title(raw), "인증 실패 시 롤백하고 회귀 테스트까지 추가해줘")

    def test_display_title_uses_pasted_document_label(self) -> None:
        raw = (
            '# Files pasted by the user:\n\n## "Sea Busters handoff": '
            "/tmp/pasted.txt\n\nPasted text contains the user's request.\n\n## My request:"
        )
        self.assertEqual(display_title(raw), "첨부 문서: Sea Busters handoff")

    def test_canonical_name_wins_and_child_source_is_exposed(self) -> None:
        Path(self.home.session_index).write_text(
            json.dumps({"id": "thread-1", "thread_name": "solar 메인 작업"}) + "\n",
            encoding="utf-8",
        )
        conn = __import__("sqlite3").connect(self.home.state_db)
        conn.execute("update threads set thread_source='subagent' where id='thread-1'")
        conn.commit()
        conn.close()
        shown = list_sessions(self.home)[0].to_dict(
            redact=Redactor(reveal=True, home=self.root), handoff_root=self.handoff_root
        )
        self.assertEqual(shown["label"], "solar 메인 작업")
        self.assertTrue(shown["is_child"])
        self.assertEqual(shown["importance"]["label"], "내부 기록")

    def test_transcript_cwd_wins_over_stale_database_cwd(self) -> None:
        session = list_sessions(self.home)[0]
        stale = Path(self._tmp.name) / "stale"
        stale.mkdir()
        object.__setattr__(session, "cwd", stale)
        result = generate_handoff(session, self.handoff_root)
        self.assertTrue(result["ok"], result)
        metadata = Path(result["document"]).with_suffix(".json")
        repository = json.loads(metadata.read_text(encoding="utf-8"))["repository"]
        self.assertNotEqual(repository.get("path"), str(stale))

    def test_generated_handoff_is_fresh_until_transcript_changes(self) -> None:
        session = list_sessions(self.home)[0]
        result = generate_handoff(session, self.handoff_root)
        self.assertTrue(result["ok"], result)
        document = Path(result["document"])
        text = document.read_text(encoding="utf-8")
        self.assertIn("## Reactivation Prompt", text)
        self.assertIn("Preserve dirty work", text)
        self.assertIn("pytest tests", text)
        self.assertIn(":END", text, "compacted context summaries must not be cut to message size")
        self.assertTrue(handoff_status(list_sessions(self.home)[0], self.handoff_root)["ready"])
        shown = list_sessions(self.home)[0].to_dict(
            redact=Redactor(reveal=True, home=self.root),
            handoff_root=self.handoff_root,
        )
        self.assertEqual(shown["importance"]["label"], "정리 가능")

        with self.rollout.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(message("user", "one more request")) + "\n")
        self.assertEqual(
            handoff_status(list_sessions(self.home)[0], self.handoff_root)["state"], "stale"
        )

    def test_a_document_without_conversation_evidence_does_not_unlock_cleanup(self) -> None:
        self.rollout.write_text('{"type":"world_state","payload":{}}\n', encoding="utf-8")
        session = list_sessions(self.home)[0]
        result = generate_handoff(session, self.handoff_root)
        self.assertFalse(result["ok"])
        self.assertTrue(Path(result["document"]).is_file())
        self.assertEqual(
            handoff_status(list_sessions(self.home)[0], self.handoff_root)["state"],
            "incomplete",
        )

    def test_repository_verification_reports_live_branch_head_and_dirty_state(self) -> None:
        repo = Path(self._tmp.name) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
        (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
        (repo / "tracked.txt").write_text("two\n", encoding="utf-8")

        evidence = inspect_repository(repo)
        self.assertEqual(evidence["state"], "verified")
        self.assertTrue(evidence["dirty"])
        self.assertIn("tracked.txt", "\n".join(evidence["status"]))


if __name__ == "__main__":
    unittest.main()
