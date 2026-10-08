"""The local UI must be locked down, and cleaning through it must respect exclusions."""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from codex_tidy.env import resolve_codex_home
from codex_tidy.model import Settings
from codex_tidy.webui.server import Handler, UiContext, load_page, settings_with

from .fake_home import FakeHome, ThreadSpec


class OptionCoercionTest(unittest.TestCase):
    def test_string_false_does_not_turn_a_switch_on(self) -> None:
        """bool("0") is True in Python; the query parser must not fall for it."""
        from codex_tidy.webui.server import _options_from_query

        options = _options_from_query({"repair_thread_metadata": ["0"], "reveal": ["1"]})
        self.assertIs(options["repair_thread_metadata"], False)
        self.assertIs(options["reveal"], True)

    def test_unknown_keys_are_ignored(self) -> None:
        updated = settings_with(Settings(), {"session_age_days": "30", "backup_root": "/etc"})
        self.assertEqual(updated.session_age_days, 30)
        self.assertIsNone(updated.backup_root)

    def test_values_are_clamped(self) -> None:
        updated = settings_with(Settings(), {"title_limit": "1", "max_archive_gb": "-5"})
        self.assertEqual(updated.title_limit, 20)
        self.assertGreater(updated.max_archive_gb, 0)

    def test_preview_limit_cannot_undercut_title_limit(self) -> None:
        updated = settings_with(Settings(), {"title_limit": "500", "preview_limit": "100"})
        self.assertEqual(updated.preview_limit, 500)

    def test_garbage_is_dropped_rather_than_raising(self) -> None:
        updated = settings_with(Settings(), {"session_age_days": "not a number"})
        self.assertEqual(updated.session_age_days, Settings().session_age_days)


class PagePackagingTest(unittest.TestCase):
    def test_the_page_is_packaged_and_loadable(self) -> None:
        page = load_page().decode("utf-8")
        self.assertIn("<title>codex-tidy</title>", page)
        self.assertIn('lang="ko"', page)
        self.assertIn('id="reveal" checked', page)
        self.assertIn('id="toggle-children"', page)
        self.assertIn('id="session-search"', page)
        self.assertIn("계속할 프로젝트 Handoff", page)
        self.assertIn(".items input[data-key]", page)
        self.assertNotIn(".items input[type=checkbox]", page)
        self.assertIn("handoffSelectionInitialized", page)
        self.assertIn("handoff_required: [...selectedSessions]", page)
        self.assertIn("allow_without_handoff: true", page)
        self.assertIn('id="purge-archives"', page)
        self.assertIn('data-archive-key=', page)
        self.assertIn("영구 삭제", page)

    def test_the_page_fetches_nothing_from_outside(self) -> None:
        """The CSP forbids external hosts, so a stray CDN link would just break."""
        import re

        page = load_page().decode("utf-8")
        external = re.findall(r'(?:src|href)\s*=\s*["\'](https?:)?//[^"\']+', page)
        self.assertEqual(external, [], f"external resources referenced: {external}")

    def test_every_task_has_korean_copy(self) -> None:
        """A task with no entry in the UI's table would render as a bare code."""
        import re

        from codex_tidy.tasks import TASK_NAMES

        page = load_page().decode("utf-8")
        table = re.search(r"const TASK = \{(.*?)\};", page, re.S).group(1)
        for name in TASK_NAMES:
            self.assertIn(f'"{name}"', table.replace(f"{name}:", f'"{name}":'))


class WebUiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.root = base / "codex"
        self.home_fixture = FakeHome(
            self.root,
            threads=[
                ThreadSpec("old-1", age_days=40, size_bytes=4096),
                ThreadSpec("old-2", age_days=30, size_bytes=2048),
                ThreadSpec("recent", age_days=1, size_bytes=1024),
            ],
        ).build()
        self.addCleanup(self._tmp.cleanup)

        for target in (
            "codex_tidy.engine.codex_processes",
            "codex_tidy.webui.server.codex_processes",
            "codex_tidy.tasks.environment.codex_processes",
        ):
            patcher = mock.patch(target, return_value=[])
            patcher.start()
            self.addCleanup(patcher.stop)

        home = resolve_codex_home(self.root)
        settings = Settings(backup_root=base / "backups")
        self.context = UiContext(home, settings, load_page())
        for spec, expected_size in (("old-1", 4096), ("old-2", 2048), ("recent", 1024)):
            rollout = Path(self.home_fixture.thread_row(spec)["rollout_path"])
            content = (
                json.dumps({"type": "response_item", "payload": {
                    "type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": f"continue {spec}"}],
                }}) + "\n" +
                json.dumps({"type": "response_item", "payload": {
                    "type": "message", "role": "assistant", "phase": "final",
                    "content": [{"type": "output_text", "text": f"finished {spec}"}],
                }}) + "\n"
            )
            encoded = content.encode("utf-8")
            rollout.write_bytes(encoded + b" " * (expected_size - len(encoded)))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, context=self.context))
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def request(self, method, path, *, body=None, cookie=True, guard=True, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if cookie:
            headers["Cookie"] = f"codex_tidy_token={self.context.token}"
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
            if guard:
                headers["X-Codex-Tidy"] = "1"
        try:
            conn.request(method, path, body=payload, headers=headers)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def state(self, query: str = ""):
        status, _headers, raw = self.request("GET", "/api/state" + query)
        self.assertEqual(status, 200)
        return json.loads(raw)

    def rollout(self, thread_id: str) -> Path:
        return Path(self.home_fixture.thread_row(thread_id)["rollout_path"])

    def generate_for_cleanup(self):
        state = self.state()
        keys = [item["key"] for item in state["items"] if item["task"] == "sessions"]
        status, _headers, raw = self.request(
            "POST", "/api/handoffs", body={"session_keys": keys}
        )
        self.assertEqual(status, 200)
        result = json.loads(raw)
        self.assertTrue(result["ok"], result)
        return result


class AccessControlTest(WebUiTestCase):
    def test_page_needs_the_token(self) -> None:
        status, _h, _b = self.request("GET", "/", cookie=False)
        self.assertEqual(status, 403)

    def test_token_in_the_url_is_swapped_for_a_strict_cookie(self) -> None:
        status, headers, _b = self.request("GET", f"/?t={self.context.token}", cookie=False)
        self.assertEqual(status, 303)
        cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

    def test_a_wrong_token_is_refused(self) -> None:
        status, _h, _b = self.request("GET", "/?t=not-the-token", cookie=False)
        self.assertEqual(status, 403)

    def test_api_needs_the_cookie(self) -> None:
        status, _h, _b = self.request("GET", "/api/state", cookie=False)
        self.assertEqual(status, 403)

    def test_mutating_calls_need_the_guard_header(self) -> None:
        status, _h, raw = self.request("POST", "/api/apply", body={}, guard=False)
        self.assertEqual(status, 403)
        self.assertTrue(self.rollout("old-1").is_file(), "nothing may move without the header")

    def test_foreign_host_header_is_refused(self) -> None:
        status, _h, _b = self.request("GET", "/api/state", host="evil.example.com")
        self.assertEqual(status, 403)

    def test_unknown_paths_are_not_served(self) -> None:
        status, _h, _b = self.request("GET", "/../pyproject.toml")
        self.assertEqual(status, 404)


class StateTest(WebUiTestCase):
    def test_state_carries_verdict_items_and_options(self) -> None:
        state = self.state()
        # A few kilobytes of old sessions is real but not worth acting on, so the
        # verdict stays calm even though there are items to tick.
        self.assertEqual(state["assessment"]["verdict"], "fine")
        self.assertEqual(len(state["items"]), 2)
        self.assertEqual(state["operation_count"], 4)
        self.assertEqual(state["options"]["session_age_days"], 10)
        self.assertEqual(state["codex_running"], [])
        self.assertEqual(len(state["sessions"]), 3)

    def test_labels_are_pseudonymous_until_reveal_is_asked_for(self) -> None:
        hidden = json.dumps(self.state("?reveal=0"))
        self.assertNotIn("old-1", hidden)
        shown = json.dumps(self.state("?reveal=1"))
        self.assertIn("old-1", shown)

    def test_options_from_the_query_change_the_plan(self) -> None:
        self.assertEqual(len(self.state("?session_age_days=999")["items"]), 0)
        self.assertEqual(len(self.state("?session_age_days=0")["items"]), 3)

    def test_state_never_writes_to_the_home(self) -> None:
        before = sorted(p.name for p in self.root.rglob("*"))
        self.state()
        self.assertEqual(before, sorted(p.name for p in self.root.rglob("*")))


class CleanTest(WebUiTestCase):
    def test_excluded_items_are_left_alone(self) -> None:
        self.generate_for_cleanup()
        state = self.state()
        keep = next(i for i in state["items"] if i["bytes"] == 4096)
        move = next(i for i in state["items"] if i["bytes"] == 2048)
        original_keep = self.rollout("old-1")
        original_move = self.rollout("old-2")

        status, _h, raw = self.request("POST", "/api/apply", body={"excluded": [keep["key"]]})
        self.assertEqual(status, 200)
        result = json.loads(raw)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["planned"], 2, "only the unexcluded session should be planned")

        self.assertTrue(original_keep.is_file(), "an excluded session must not move")
        self.assertEqual(self.home_fixture.thread_row("old-1")["archived"], 0)
        self.assertFalse(original_move.exists(), "the selected session should have moved")
        self.assertEqual(self.home_fixture.thread_row("old-2")["archived"], 1)
        self.assertEqual(move["task"], "sessions")

    def test_excluding_everything_reports_nothing_to_do(self) -> None:
        self.generate_for_cleanup()
        keys = [i["key"] for i in self.state()["items"]]
        _s, _h, raw = self.request("POST", "/api/apply", body={"excluded": keys})
        self.assertTrue(json.loads(raw)["nothing_to_do"])
        self.assertTrue(self.rollout("old-1").is_file())

    def test_restore_through_the_ui(self) -> None:
        self.generate_for_cleanup()
        before = self.rollout("old-1")
        _s, _h, raw = self.request("POST", "/api/apply", body={"excluded": []})
        self.assertTrue(json.loads(raw)["ok"])
        self.assertFalse(before.exists())

        _s, _h, raw = self.request("POST", "/api/restore", body={})
        result = json.loads(raw)
        self.assertTrue(result["ok"], result)
        self.assertTrue(before.is_file())
        self.assertEqual(self.home_fixture.thread_row("old-1")["archived"], 0)

    def test_running_codex_blocks_the_clean_button_path(self) -> None:
        from codex_tidy.env import ProcInfo

        self.generate_for_cleanup()

        with mock.patch(
            "codex_tidy.engine.codex_processes",
            return_value=[ProcInfo(pid=1, name="codex", reason="holds state_5.sqlite")],
        ):
            _s, _h, raw = self.request("POST", "/api/apply", body={"excluded": []})
        result = json.loads(raw)
        self.assertFalse(result["ok"])
        self.assertTrue(result["blocked"])
        self.assertTrue(self.rollout("old-1").is_file())

    def test_cleanup_refuses_a_session_without_a_fresh_handoff(self) -> None:
        _s, _h, raw = self.request("POST", "/api/apply", body={"excluded": []})
        result = json.loads(raw)
        self.assertFalse(result["ok"])
        self.assertTrue(result["missing_handoffs"])
        self.assertTrue(self.rollout("old-1").is_file())

    def test_cleanup_allows_confirmed_archive_without_handoff(self) -> None:
        original = self.rollout("old-1")
        _s, _h, raw = self.request(
            "POST",
            "/api/apply",
            body={
                "excluded": [],
                "handoff_required": [],
                "allow_without_handoff": True,
            },
        )
        result = json.loads(raw)
        self.assertTrue(result["ok"], result)
        self.assertFalse(original.exists())

    def test_unprotected_archive_requires_explicit_confirmation(self) -> None:
        _s, _h, raw = self.request(
            "POST", "/api/apply", body={"excluded": [], "handoff_required": []}
        )
        result = json.loads(raw)
        self.assertFalse(result["ok"])
        self.assertTrue(result["without_handoff"])
        self.assertTrue(self.rollout("old-1").is_file())

    def test_recent_project_with_handoff_is_archived_when_explicitly_selected(self) -> None:
        state = self.state()
        recent = next(s for s in state["sessions"] if s["bytes"] == 1024)
        original = self.rollout("recent")
        status, _headers, raw = self.request(
            "POST", "/api/handoffs", body={"session_keys": [recent["key"]]}
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw)["ok"])
        excluded = [i["key"] for i in state["items"]]
        _s, _h, raw = self.request(
            "POST",
            "/api/apply",
            body={
                "excluded": excluded,
                "handoff_required": [recent["key"]],
                "allow_without_handoff": True,
            },
        )
        result = json.loads(raw)
        self.assertTrue(result["ok"], result)
        self.assertFalse(original.exists())
        self.assertEqual(self.home_fixture.thread_row("recent")["archived"], 1)

    def test_archive_list_and_permanent_delete_remove_exact_file_and_row(self) -> None:
        self.generate_for_cleanup()
        original = self.rollout("old-1")
        _s, _h, raw = self.request("POST", "/api/apply", body={"excluded": []})
        self.assertTrue(json.loads(raw)["ok"])
        archived_path = Path(self.home_fixture.thread_row("old-1")["rollout_path"])
        self.assertTrue(archived_path.is_file())
        archive = next(a for a in self.state("?reveal=1")["archives"] if a["bytes"] == 4096)

        _s, _h, raw = self.request(
            "POST",
            "/api/purge",
            body={"archive_keys": [archive["key"]], "confirmation": "삭제"},
        )
        self.assertFalse(json.loads(raw)["ok"])
        self.assertTrue(archived_path.is_file())

        _s, _h, raw = self.request(
            "POST",
            "/api/purge",
            body={"archive_keys": [archive["key"]], "confirmation": "영구 삭제"},
        )
        result = json.loads(raw)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["deleted"], 1)
        self.assertFalse(original.exists())
        self.assertFalse(archived_path.exists())
        self.assertEqual(self.home_fixture.thread_row("old-1"), {})
        self.assertTrue(
            any(a["bytes"] == 2048 for a in self.state("?reveal=1")["archives"]),
            "an unselected archive must remain",
        )

    def test_changed_transcript_makes_handoff_stale_and_blocks_cleanup(self) -> None:
        self.generate_for_cleanup()
        with self.rollout("old-1").open("ab") as handle:
            handle.write(b"\n")
        _s, _h, raw = self.request("POST", "/api/apply", body={"excluded": []})
        result = json.loads(raw)
        self.assertFalse(result["ok"])
        self.assertTrue(result["missing_handoffs"])


if __name__ == "__main__":
    unittest.main()
