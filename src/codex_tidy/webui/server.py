"""A local, single-user web UI served entirely from the standard library.

Access control, in layers, because this process can move a developer's files:

* the socket is bound to 127.0.0.1, so nothing off-box can reach it
* a random per-run token must be presented once, in the URL we open ourselves
* the token is then held in a ``SameSite=Strict``, ``HttpOnly`` cookie, so a
  malicious page in another tab cannot ride along
* mutating calls additionally require a custom header, which a cross-origin form
  post cannot set
* the ``Host`` header must be a loopback name, which blocks DNS rebinding

The server holds one lock across mutating requests: a second Clean cannot start
while the first is still running.
"""

from __future__ import annotations

import http.cookies
import json
import secrets
import threading
import webbrowser
from dataclasses import replace
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .. import engine
from .. import handoff
from .. import purge
from ..advice import assess
from ..env import (
    CodexHome,
    HomeLock,
    LockHeld,
    codex_processes,
    default_backup_root,
    pinned_thread_ids,
)
from ..journal import find_latest_journal
from ..model import Settings
from ..tasks import ALL_TASKS
from ..tasks.sessions import plan_selected as plan_selected_sessions

COOKIE_NAME = "codex_tidy_token"
GUARD_HEADER = "X-Codex-Tidy"
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
MAX_BODY_BYTES = 256 * 1024

# Only these may be set from the browser, each with a type and a sane range.
OPTION_SPEC: dict[str, tuple[type, Any, Any]] = {
    "reveal": (bool, None, None),
    "session_age_days": (int, 0, 3650),
    "session_min_mb": (float, 0.0, 100_000.0),
    "worktree_age_days": (int, 0, 3650),
    "log_rotate_mb": (int, 0, 1_000_000),
    "title_limit": (int, 20, 100_000),
    "preview_limit": (int, 20, 1_000_000),
    "leftover_age_days": (int, 0, 3650),
    "max_archive_gb": (float, 0.001, 10_000.0),
    "repair_thread_metadata": (bool, None, None),
    "archive_orphan_rollouts": (bool, None, None),
    "archive_dirty_worktrees": (bool, None, None),
    "archive_leftovers": (bool, None, None),
}


def _coerce(name: str, raw: Any) -> Any:
    kind, low, high = OPTION_SPEC[name]
    if kind is bool:
        return bool(raw)
    value = kind(raw)
    if low is not None:
        value = max(low, value)
    if high is not None:
        value = min(high, value)
    return value


def settings_with(base: Settings, options: dict[str, Any]) -> Settings:
    changes = {}
    for name, raw in (options or {}).items():
        if name not in OPTION_SPEC:
            continue  # Unknown keys are ignored, never passed through.
        try:
            changes[name] = _coerce(name, raw)
        except (TypeError, ValueError):
            continue
    updated = replace(base, **changes)
    if updated.preview_limit < updated.title_limit:
        updated = replace(updated, preview_limit=updated.title_limit)
    return updated


def _current_options(settings: Settings) -> dict[str, Any]:
    return {name: getattr(settings, name) for name in OPTION_SPEC}


def _options_from_query(query: dict[str, list[str]]) -> dict[str, Any]:
    """Query strings arrive as text, so booleans need explicit handling.

    bool("0") is True, which would silently turn every checkbox on.
    """
    options: dict[str, Any] = {}
    for name, (kind, _low, _high) in OPTION_SPEC.items():
        if name not in query:
            continue
        raw = query[name][0]
        options[name] = raw.lower() in ("1", "true", "yes") if kind is bool else raw
    return options


def build_state(home: CodexHome, settings: Settings) -> dict[str, Any]:
    """A full read-only picture: findings, plan preview, verdict, blockers."""
    stamp = engine.fresh_stamp()
    backup_root = engine.resolve_backup_root(home, settings, stamp)
    session = engine.open_session(
        home, settings, writable=False, stamp=stamp, backup_root=backup_root
    )
    try:
        plan = engine.scan(session, with_operations=True)
        redactor = session.ctx.redact
    finally:
        session.close()

    assessment = assess(plan)
    running = codex_processes(home)
    journal = find_latest_journal(settings.backup_root or default_backup_root(home))
    handoff_root = handoff.default_handoff_root(home, settings.backup_root)
    sessions = handoff.list_sessions(home, pinned=pinned_thread_ids(home))

    return {
        "home": str(home.root),
        "backup_root": str(backup_root),
        "reveal": settings.reveal,
        "options": _current_options(settings),
        "assessment": assessment.to_dict(),
        "items": [item.to_dict() for item in plan.items()],
        "sessions": [
            item.to_dict(redact=redactor, handoff_root=handoff_root) for item in sessions
        ],
        "archives": [item.to_dict(redactor) for item in purge.list_archives(home)],
        "archive_root": str(home.archived_sessions),
        "handoff_root": str(handoff_root),
        "findings": [f.to_dict() for f in plan.findings],
        "skipped": plan.skipped,
        "operation_count": len(plan.operations),
        "reclaimable_bytes": plan.reclaimable_bytes,
        "codex_running": [
            {"pid": p.pid, "name": p.name, "reason": p.reason} for p in running
        ],
        "last_journal": str(journal) if journal else None,
        "task_titles": {task.name: task.title for task in ALL_TASKS},
    }


def run_apply(
    home: CodexHome,
    settings: Settings,
    excluded: set[str],
    *,
    handoff_required: set[str] | None = None,
    allow_without_handoff: bool = False,
) -> dict[str, Any]:
    stamp = engine.fresh_stamp()
    backup_root = engine.resolve_backup_root(home, settings, stamp)
    try:
        with HomeLock(home, active=True):
            session = engine.open_session(
                home, settings, writable=False, stamp=stamp, backup_root=backup_root
            )
            try:
                plan = engine.scan(session, with_operations=True)
                if handoff_required is not None:
                    existing = {op.key for op in plan.operations if op.task == "sessions"}
                    plan.operations.extend(
                        op
                        for op in plan_selected_sessions(session.ctx, handoff_required)
                        if op.key not in existing
                    )
            finally:
                session.close()

            plan = engine.filter_plan(plan, excluded)
            handoff_root = handoff.default_handoff_root(home, settings.backup_root)
            ready = handoff.ready_session_keys(
                home, handoff_root, pinned=pinned_thread_ids(home)
            )
            session_keys = {op.key for op in plan.operations if op.task == "sessions"}
            protected = session_keys if handoff_required is None else session_keys & handoff_required
            missing_handoffs = sorted(protected - ready)
            if missing_handoffs:
                return {
                    "ok": False,
                    "blocked": [
                        f"{len(missing_handoffs)} selected session(s) need a fresh handoff before cleanup"
                    ],
                    "missing_handoffs": missing_handoffs,
                }
            without_handoff = sorted(session_keys - protected)
            if without_handoff and not allow_without_handoff:
                return {
                    "ok": False,
                    "blocked": [
                        f"{len(without_handoff)} session(s) were not marked as continuing projects; explicit archive confirmation is required"
                    ],
                    "without_handoff": without_handoff,
                }
            problems = engine.preflight(home, plan, settings)
            if problems:
                return {"ok": False, "blocked": [f.message for f in problems]}
            if not plan.operations:
                return {"ok": True, "completed": 0, "nothing_to_do": True}

            result = engine.apply_plan(home, plan, settings)
    except LockHeld as exc:
        return {"ok": False, "blocked": [str(exc)]}

    return {
        "ok": not result.failed,
        "completed": result.completed,
        "planned": len(plan.operations),
        "failed": result.failed,
        "rolled_back": result.rolled_back,
        "error": result.error,
        "backup_root": str(result.backup_root),
        "journal": str(result.journal_path),
        "notes": result.notes,
    }


def run_restore(home: CodexHome, settings: Settings, journal: str | None) -> dict[str, Any]:
    base = settings.backup_root or default_backup_root(home)
    path = Path(journal).expanduser() if journal else find_latest_journal(base)
    if path is None or not path.is_file():
        return {"ok": False, "blocked": ["no journal found"]}

    running = codex_processes(home)
    if running:
        return {"ok": False, "blocked": [f"Codex is running ({len(running)})"]}

    try:
        with HomeLock(home, active=True):
            result = engine.restore(home, path)
    except LockHeld as exc:
        return {"ok": False, "blocked": [str(exc)]}

    return {
        "ok": not result.failed,
        "undone": result.undone,
        "failed": result.failed,
        "journal": str(result.journal_path),
        "notes": result.notes,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "codex-tidy"
    sys_version = ""

    def __init__(self, *args, context: "UiContext", **kwargs):
        self.context = context
        super().__init__(*args, **kwargs)

    # -- plumbing ---------------------------------------------------------

    def log_message(self, *_args) -> None:
        pass  # The console belongs to the user, not to request logging.

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ALLOWED_HOSTS

    def _cookie_token(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(raw)
        except http.cookies.CookieError:
            return None
        morsel = jar.get(COOKIE_NAME)
        return morsel.value if morsel else None

    def _authorised(self) -> bool:
        supplied = self._cookie_token()
        return bool(supplied) and secrets.compare_digest(supplied, self.context.token)

    def _send(self, code: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        # Everything is inlined, so nothing may be fetched from anywhere.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "connect-src 'self'; form-action 'none'; base-uri 'none'",
        )
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > MAX_BODY_BYTES:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8")) or {}
        except (ValueError, UnicodeDecodeError):
            return {}

    # -- routes -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            self._json(403, {"error": "bad host"})
            return
        parsed = urlparse(self.path)

        if parsed.path == "/":
            supplied = parse_qs(parsed.query).get("t", [""])[0]
            if supplied and secrets.compare_digest(supplied, self.context.token):
                # Move the token out of the address bar and into a strict cookie.
                self._send(
                    303,
                    b"",
                    "text/plain",
                    {
                        "Location": "/",
                        "Set-Cookie": (
                            f"{COOKIE_NAME}={self.context.token}; Path=/; HttpOnly; "
                            "SameSite=Strict"
                        ),
                    },
                )
                return
            if not self._authorised():
                self._send(403, b"codex-tidy: open the URL printed in the terminal.", "text/plain; charset=utf-8")
                return
            self._send(200, self.context.page, "text/html; charset=utf-8")
            return

        if parsed.path == "/api/state":
            if not self._authorised():
                self._json(403, {"error": "unauthorised"})
                return
            options = _options_from_query(parse_qs(parsed.query))
            try:
                state = build_state(
                    self.context.home, settings_with(self.context.settings, options)
                )
            except engine.EnvironmentError_ as exc:
                self._json(200, {"error": str(exc)})
                return
            self._json(200, state)
            return

        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_ok():
            self._json(403, {"error": "bad host"})
            return
        if not self._authorised() or self.headers.get(GUARD_HEADER) != "1":
            self._json(403, {"error": "unauthorised"})
            return

        parsed = urlparse(self.path)
        body = self._read_body()

        if parsed.path == "/api/shutdown":
            self._json(200, {"ok": True})
            threading.Thread(target=self.context.stop, daemon=True).start()
            return

        if parsed.path not in ("/api/apply", "/api/restore", "/api/handoffs", "/api/purge"):
            self._json(404, {"error": "not found"})
            return

        if not self.context.busy.acquire(blocking=False):
            self._json(409, {"ok": False, "blocked": ["another run is already in progress"]})
            return
        try:
            settings = settings_with(self.context.settings, body.get("options") or {})
            if parsed.path == "/api/handoffs":
                result = handoff.generate_selected(
                    self.context.home,
                    handoff.default_handoff_root(self.context.home, settings.backup_root),
                    body.get("session_keys") or [],
                    pinned=pinned_thread_ids(self.context.home),
                )
            elif parsed.path == "/api/apply":
                excluded = {str(k) for k in (body.get("excluded") or [])}
                required = (
                    {str(k) for k in (body.get("handoff_required") or [])}
                    if "handoff_required" in body
                    else None
                )
                result = run_apply(
                    self.context.home,
                    settings,
                    excluded,
                    handoff_required=required,
                    allow_without_handoff=body.get("allow_without_handoff") is True,
                )
            elif parsed.path == "/api/purge":
                result = purge.purge_selected(
                    self.context.home,
                    body.get("archive_keys") or [],
                    str(body.get("confirmation") or ""),
                )
            else:
                result = run_restore(self.context.home, settings, body.get("journal"))
        except engine.EnvironmentError_ as exc:
            result = {"ok": False, "blocked": [str(exc)]}
        finally:
            self.context.busy.release()
        self._json(200, result)


class UiContext:
    def __init__(self, home: CodexHome, settings: Settings, page: bytes):
        self.home = home
        self.settings = settings
        self.page = page
        self.token = secrets.token_urlsafe(24)
        self.busy = threading.Semaphore(1)
        self.httpd: ThreadingHTTPServer | None = None

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()


def load_page() -> bytes:
    return resources.files(__package__).joinpath("index.html").read_bytes()


def _emit(message: str) -> None:
    # Flush explicitly: stdout is block-buffered when redirected to a file or a
    # pipe, and a long-running server that prints its URL only on exit is useless.
    print(message, flush=True)


def serve(
    home: CodexHome,
    settings: Settings,
    *,
    port: int = 0,
    open_browser: bool = True,
    printer=_emit,
) -> int:
    context = UiContext(home, settings, load_page())
    handler = partial(Handler, context=context)
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError as exc:
        printer(
            f"codex-tidy gui: could not listen on 127.0.0.1:{port} ({exc}).\n"
            "Something else is using that port. Omit --port to let one be chosen, "
            "or pass a different one."
        )
        return 3
    context.httpd = httpd
    bound_port = httpd.server_address[1]
    url = f"http://127.0.0.1:{bound_port}/?t={context.token}"

    printer(f"codex-tidy gui\n  codex home   {home.root}\n  url          {url}")
    printer("  stop         Ctrl-C, or the button in the page")
    if open_browser:
        webbrowser.open(url)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        printer("\nstopped")
    finally:
        httpd.server_close()
    return 0
