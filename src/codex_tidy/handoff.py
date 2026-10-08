"""Local, evidence-first handoff generation for Codex session transcripts.

The extractor never sends transcript content over the network.  It reads JSONL
with bounded records, ignores reasoning/world-state/tool-output bulk, and then
checks any repository mentioned by the session with read-only git commands.
Generated handoffs carry a fingerprint of the source transcript; a changed
transcript is stale and cannot satisfy the UI's archive gate.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .db import connect, probe, quote_identifier
from .env import CodexHome, canonical
from .model import operation_key
from .privacy import Redactor

MAX_RECORD_BYTES = 16 * 1024 * 1024
FULL_SCAN_BYTES = 256 * 1024 * 1024
HEAD_SCAN_BYTES = 8 * 1024 * 1024
TAIL_SCAN_BYTES = 256 * 1024 * 1024
MAX_TEXT = 4_000
MAX_SUMMARY = 80_000
MAX_MESSAGES = 160
MAX_FINALS = 100
MAX_COMMANDS = 100
MAX_ERRORS = 60
MAX_PATHS = 160

PATH_RE = re.compile(
    r"(?<![\w])(?:/[A-Za-z0-9._~+@%:=,()\[\]{}' -]+(?:/[A-Za-z0-9._~+@%:=,()\[\]{}' -]+)+"
    r"|[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.@+ -]+)+\.[A-Za-z0-9_-]{1,12})"
)
ERROR_RE = re.compile(
    r"\b(?:error|failed|failure|exception|traceback|timeout|timed out|permission denied)\b"
    r"|오류|에러|실패|타임아웃|권한 거부",
    re.I,
)
CONSTRAINT_RE = re.compile(
    r"하지 ?마|절대|보존|건드리|수정하지|삭제하지|승인|must\b|do not\b|don't\b|never\b|preserve\b",
    re.I,
)
OPEN_RE = re.compile(
    r"남(?:았|은)|아직|미완료|다음|해야|필요|보류|결정|todo\b|next\b|remaining\b|unfinished\b|pending\b",
    re.I,
)
COMMAND_KEYS = {"cmd", "command", "workdir", "path", "file", "target"}
CONTEXT_BLOCK_RE = re.compile(
    r"<(environment_context|recommended_plugins)>.*?</\1>", re.I | re.S
)
TAG_RE = re.compile(r"<[^>]{1,100}>")
WRAPPED_USER_RE = re.compile(
    r"\[\d+\]\s*user:\s*(.*?)(?=\n{2,}\[\d+\]\s*(?:user|assistant|tool)|\Z)",
    re.I | re.S,
)
WRAPPED_ASSISTANT_RE = re.compile(
    r"\[\d+\]\s*assistant:\s*(.*?)(?=\n{2,}\[\d+\]\s*(?:user|assistant|tool)|\Z)",
    re.I | re.S,
)
PASTED_REQUEST_RE = re.compile(r"##\s*My request:\s*(.+)", re.I | re.S)
PASTED_LABEL_RE = re.compile(r"##\s*[\"“](.+?)[\"”]:\s*[/~]", re.S)


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def default_handoff_root(home: CodexHome, backup_root: Path | None = None) -> Path:
    if backup_root is not None:
        return backup_root.parent / "codex-handoffs"
    documents = Path.home() / "Documents"
    if documents.is_dir():
        return documents / "Codex" / "codex-handoffs"
    return home.root / "handoffs"


@dataclass(frozen=True)
class TranscriptFingerprint:
    size: int
    mtime_ns: int

    @classmethod
    def read(cls, path: Path) -> "TranscriptFingerprint":
        stat = path.stat()
        return cls(size=stat.st_size, mtime_ns=stat.st_mtime_ns)

    def to_dict(self) -> dict[str, int]:
        return {"size": self.size, "mtime_ns": self.mtime_ns}


@dataclass(frozen=True)
class SessionInfo:
    key: str
    thread_id: str
    title: str
    name: str
    thread_source: str
    rollout_path: Path
    cwd: Path | None
    updated_at: int | None
    archived: bool
    pinned: bool
    size: int
    fingerprint: TranscriptFingerprint

    def to_dict(self, *, redact: Redactor, handoff_root: Path) -> dict[str, Any]:
        status = handoff_status(self, handoff_root)
        title = display_title(self.name or self.title)
        return {
            "key": self.key,
            "label": redact.snippet(title, limit=120),
            "path": redact.path(self.rollout_path),
            "cwd": redact.path(self.cwd) if self.cwd else None,
            "project": self.cwd.name if self.cwd and redact.reveal else None,
            "updated_at": self.updated_at,
            "archived": self.archived,
            "pinned": self.pinned,
            "is_child": self.thread_source in ("subagent", "guardian_review"),
            "thread_source": self.thread_source or "unknown",
            "bytes": self.size,
            "handoff": status,
            "importance": session_importance(self, status),
        }


def display_title(value: str) -> str:
    """Turn history-sized first prompts into a useful one-line session label."""
    decoded = html.unescape(value or "")
    candidate = decoded
    if decoded.startswith("The following is the Codex agent history"):
        user_messages = [" ".join(item.split()) for item in WRAPPED_USER_RE.findall(decoded)]
        if user_messages:
            generic = re.compile(r"^(?:오케이|응|네|진행해|계속해|마저 해|해줘|좋아)[ .!?]*$", re.I)
            candidate = max(
                user_messages,
                key=lambda item: min(len(item), 300) - (500 if generic.match(item) else 0),
            )
            if len(candidate) < 18 or generic.match(candidate):
                assistants = [
                    " ".join(item.split()) for item in WRAPPED_ASSISTANT_RE.findall(decoded)
                ]
                if assistants:
                    candidate = f"{candidate} — {assistants[0]}"
    elif decoded.lstrip().startswith("# Files pasted by the user"):
        request = PASTED_REQUEST_RE.search(decoded)
        if request and request.group(1).strip():
            candidate = request.group(1)
        else:
            label = PASTED_LABEL_RE.search(decoded)
            candidate = f"첨부 문서: {label.group(1)}" if label else "첨부 문서 기반 작업"
    candidate = re.sub(r"<image\b[^>]*>", "[이미지] ", candidate, flags=re.I)
    cleaned = CONTEXT_BLOCK_RE.sub(" ", candidate)
    cleaned = TAG_RE.sub(" ", cleaned)
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        cleaned = " ".join(decoded.split())
    if not cleaned:
        return "제목 없는 세션"
    # A sentence boundary is generally more useful than an arbitrary 120-char cut.
    first = re.split(r"(?<=[.!?。！？])\s+|\n+", cleaned, maxsplit=1)[0]
    return first[:500]


def _age_days(updated_at: int | None) -> int | None:
    if updated_at is None:
        return None
    stamp = updated_at / 1000 if updated_at > 10_000_000_000 else updated_at
    return max(0, int((time.time() - stamp) / 86_400))


def session_importance(session: SessionInfo, status: dict[str, Any]) -> dict[str, Any]:
    """Explain cleanup risk without pretending content-free heuristics are certainty."""
    age = _age_days(session.updated_at)
    if session.pinned:
        level, label, reason = "protect", "보호", "고정해 둔 세션"
    elif session.thread_source in ("subagent", "guardian_review"):
        level, label, reason = "internal", "내부 기록", "독립 사용자 작업이 아닌 자식 실행 기록"
    elif status.get("ready"):
        level, label, reason = "safe", "정리 가능", "신선한 Handoff가 준비됨"
    elif session.archived:
        level, label, reason = "archived", "보관됨", "이미 아카이브된 세션"
    elif age is not None and age <= 14:
        level, label, reason = "review", "중요 가능성", f"최근 {age}일 내 사용한 대화"
    elif session.cwd is not None:
        level, label, reason = "review", "확인 필요", "프로젝트 작업 경로가 연결됨"
    elif session.size >= 64 * 1024 * 1024:
        level, label, reason = "review", "확인 필요", "64MB가 넘는 대형 대화"
    elif age is not None and age >= 30 and session.size < 1024 * 1024:
        level, label, reason = "low", "정리 후보", "30일 넘은 소형 대화; 내용 확인 권장"
    else:
        level, label, reason = "unknown", "판단 필요", "내용 기반 Handoff가 아직 없음"
    return {"level": level, "label": label, "reason": reason, "age_days": age}


def _active_value(row: Any, columns: frozenset[str]) -> bool:
    if "archived" in columns and row["archived"] not in (None, 0, "0"):
        return True
    return "archived_at" in columns and row["archived_at"] is not None


def list_sessions(home: CodexHome, *, pinned: Iterable[str] = ()) -> list[SessionInfo]:
    """List every database-backed session whose transcript still exists."""
    conn = connect(home.state_db, readonly=True)
    try:
        schema = probe(conn)
        columns = schema.cols("threads")
        required = {"id", "title", "rollout_path"}
        if not required.issubset(columns):
            return []
        selected = ["id", "title", "rollout_path"]
        selected.extend(
            name
            for name in (
                "name",
                "thread_source",
                "is_pinned",
                "cwd",
                "updated_at",
                "archived",
                "archived_at",
            )
            if name in columns
        )
        rows = conn.execute(
            f"select {', '.join(quote_identifier(name) for name in selected)} from threads"
        ).fetchall()
    finally:
        conn.close()

    pinned_ids = set(pinned)
    index_names = _session_index_names(home)
    roots = tuple(
        canonical(root)
        for root in (home.sessions, home.archived_sessions)
        if root.exists()
    )
    if not roots:
        return []
    found: list[SessionInfo] = []
    for row in rows:
        raw_path = row["rollout_path"]
        if not raw_path:
            continue
        path = Path(str(raw_path))
        if not path.is_file():
            continue
        resolved = canonical(path)
        if roots and not any(_is_relative_to(resolved, root) for root in roots):
            continue
        try:
            fingerprint = TranscriptFingerprint.read(path)
        except OSError:
            continue
        thread_id = str(row["id"])
        cwd = None
        if "cwd" in columns and row["cwd"]:
            candidate = Path(str(row["cwd"])).expanduser()
            if candidate.is_dir():
                cwd = canonical(candidate)
        updated_at = None
        if "updated_at" in columns and row["updated_at"] is not None:
            try:
                updated_at = int(row["updated_at"])
            except (TypeError, ValueError):
                pass
        found.append(
            SessionInfo(
                key=operation_key("sessions", thread_id),
                thread_id=thread_id,
                title=str(row["title"] or ""),
                name=(
                    str(row["name"] or "") if "name" in columns else ""
                )
                or index_names.get(thread_id, ""),
                thread_source=(
                    str(row["thread_source"] or "") if "thread_source" in columns else ""
                ),
                rollout_path=path,
                cwd=cwd,
                updated_at=updated_at,
                archived=_active_value(row, columns),
                pinned=(
                    thread_id in pinned_ids
                    or ("is_pinned" in columns and row["is_pinned"] not in (None, 0, "0"))
                ),
                size=fingerprint.size,
                fingerprint=fingerprint,
            )
        )
    found.sort(
        key=lambda item: (
            item.thread_source in ("subagent", "guardian_review"),
            item.archived,
            -(item.updated_at or 0),
            -item.size,
        )
    )
    return found


def _session_index_names(home: CodexHome) -> dict[str, str]:
    """Latest user-visible rename wins, matching Codex's append-only index."""
    names: dict[str, str] = {}
    path = home.session_index
    if not path.is_file():
        return names
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if len(line) > 1024 * 1024:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                thread_id = row.get("id")
                name = row.get("thread_name")
                if thread_id and name:
                    names[str(thread_id)] = str(name)
    except OSError:
        return {}
    return names


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _artifact_paths(session: SessionInfo, root: Path) -> tuple[Path, Path]:
    digest = session.key.split(":", 1)[-1]
    return root / f"handoff-{digest}.md", root / f"handoff-{digest}.json"


def handoff_status(session: SessionInfo, root: Path) -> dict[str, Any]:
    document, metadata = _artifact_paths(session, root)
    status: dict[str, Any] = {
        "state": "missing",
        "ready": False,
        "document": str(document),
        "generated_at": None,
        "warnings": [],
    }
    if not document.is_file() or not metadata.is_file():
        return status
    try:
        raw = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        status["state"] = "invalid"
        return status
    status["generated_at"] = raw.get("generated_at")
    status["warnings"] = list(raw.get("warnings") or [])
    expected = raw.get("fingerprint") or {}
    current = session.fingerprint.to_dict()
    if expected != current:
        status["state"] = "stale"
        return status
    if raw.get("quality_ready") is not True:
        status["state"] = "incomplete"
        return status
    status["state"] = "ready"
    status["ready"] = True
    return status


def ready_session_keys(home: CodexHome, root: Path, *, pinned: Iterable[str] = ()) -> set[str]:
    return {
        session.key
        for session in list_sessions(home, pinned=pinned)
        if handoff_status(session, root)["ready"]
    }


@dataclass
class Extracted:
    cwd: Path | None = None
    branch_from_session: str | None = None
    users: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_MESSAGES))
    finals: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_FINALS))
    summaries: deque[str] = field(default_factory=lambda: deque(maxlen=3))
    commands: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_COMMANDS))
    errors: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_ERRORS))
    paths: set[str] = field(default_factory=set)
    skipped_large_records: int = 0
    invalid_records: int = 0
    sampled: bool = False


def _bounded(value: Any, limit: int = MAX_TEXT) -> str:
    text = " ".join(str(value or "").replace("\x00", "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return _bounded(content)
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        value = item.get("text") or item.get("input_text") or item.get("output_text")
        if isinstance(value, str):
            parts.append(value)
    return _bounded("\n".join(parts))


def _find_paths(text: str, target: set[str]) -> None:
    for match in PATH_RE.finditer(text):
        value = match.group(0).strip("'\".,:;)]}")
        if len(value) <= 500:
            target.add(value)
        if len(target) >= MAX_PATHS:
            return


def _command_text(payload: dict[str, Any]) -> str:
    raw = payload.get("input") or payload.get("arguments")
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except ValueError:
            return _bounded(raw)
    else:
        decoded = raw
    if isinstance(decoded, dict):
        bits = []
        for key, value in decoded.items():
            if key in COMMAND_KEYS and isinstance(value, (str, int, float)):
                bits.append(f"{key}={value}")
        return _bounded("; ".join(bits))
    return _bounded(decoded)


def _consume_record(record: dict[str, Any], out: Extracted) -> None:
    outer_type = record.get("type")
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return
    payload_type = payload.get("type")

    if outer_type == "session_meta":
        if not out.cwd and payload.get("cwd"):
            candidate = Path(str(payload["cwd"])).expanduser()
            if candidate.is_dir():
                out.cwd = canonical(candidate)
        git = payload.get("git")
        if isinstance(git, dict):
            out.branch_from_session = _bounded(git.get("branch"), 200) or None
        return

    if outer_type == "turn_context":
        summary = payload.get("summary")
        if isinstance(summary, str) and summary.strip():
            out.summaries.append(_bounded(summary, MAX_SUMMARY))
        if not out.cwd and payload.get("cwd"):
            candidate = Path(str(payload["cwd"])).expanduser()
            if candidate.is_dir():
                out.cwd = canonical(candidate)
        return

    if outer_type != "response_item":
        return
    if payload_type == "message":
        role = payload.get("role")
        text = _message_text(payload.get("content"))
        if not text or role == "developer":
            return
        _find_paths(text, out.paths)
        if role == "user":
            out.users.append(text)
        elif role == "assistant" and payload.get("phase") in (None, "final"):
            out.finals.append(text)
            if ERROR_RE.search(text):
                out.errors.append(text)
        return
    if payload_type in ("custom_tool_call", "function_call"):
        command = _command_text(payload)
        if command:
            out.commands.append(command)
            _find_paths(command, out.paths)
        return
    if payload_type in ("custom_tool_call_output", "function_call_output"):
        # Tool output is ignored unless its bounded prefix contains a failure.
        value = payload.get("output")
        prefix = _bounded(value, 2_000)
        if prefix and ERROR_RE.search(prefix):
            out.errors.append(prefix)


def _read_records(handle, out: Extracted, *, byte_limit: int | None = None) -> None:
    consumed = 0
    while byte_limit is None or consumed < byte_limit:
        allowed = MAX_RECORD_BYTES + 1
        if byte_limit is not None:
            allowed = min(allowed, max(1, byte_limit - consumed))
        raw = handle.readline(allowed)
        if not raw:
            break
        consumed += len(raw)
        complete = raw.endswith(b"\n")
        if not complete and len(raw) >= MAX_RECORD_BYTES:
            out.skipped_large_records += 1
            while raw and not raw.endswith(b"\n"):
                raw = handle.readline(MAX_RECORD_BYTES + 1)
            continue
        try:
            record = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            out.invalid_records += 1
            continue
        if isinstance(record, dict) and record.get("type") != "compacted":
            _consume_record(record, out)


def extract_transcript(path: Path) -> Extracted:
    out = Extracted()
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size <= FULL_SCAN_BYTES:
            _read_records(handle, out)
        else:
            out.sampled = True
            _read_records(handle, out, byte_limit=HEAD_SCAN_BYTES)
            start = max(0, size - TAIL_SCAN_BYTES)
            handle.seek(start)
            if start:
                # Discard the full partial record even when a pathological
                # compacted event itself is larger than MAX_RECORD_BYTES.
                partial = handle.readline(MAX_RECORD_BYTES + 1)
                while partial and not partial.endswith(b"\n"):
                    partial = handle.readline(MAX_RECORD_BYTES + 1)
            _read_records(handle, out)
    return out


def _run_git(repo: Path, *args: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
            check=False,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, _bounded(exc, 1_000)
    return result.returncode == 0, result.stdout.strip()


def inspect_repository(cwd: Path | None) -> dict[str, Any]:
    if cwd is None or not cwd.is_dir():
        return {"state": "unavailable", "reason": "session has no existing working directory"}
    ok, root = _run_git(cwd, "rev-parse", "--show-toplevel")
    if not ok:
        return {"state": "not_git", "cwd": str(cwd), "reason": _bounded(root, 500)}
    repo = canonical(Path(root.splitlines()[-1]))
    ok_branch, branch = _run_git(repo, "branch", "--show-current")
    ok_head, head = _run_git(repo, "log", "-1", "--format=%H%n%h %s")
    ok_status, status = _run_git(repo, "status", "--short", "--untracked-files=all")
    if not (ok_branch and ok_head and ok_status):
        return {"state": "error", "root": str(repo), "reason": "one or more git checks failed"}
    status_lines = status.splitlines()
    return {
        "state": "verified",
        "root": str(repo),
        "branch": branch or "(detached)",
        "head": head,
        "dirty": bool(status_lines),
        "status": status_lines[:250],
        "status_truncated": len(status_lines) > 250,
    }


def _unique(values: Iterable[str], limit: int, *, text_limit: int = MAX_TEXT) -> list[str]:
    found = []
    seen = set()
    for value in values:
        clean = _bounded(value, text_limit)
        if clean and clean not in seen:
            seen.add(clean)
            found.append(clean)
        if len(found) >= limit:
            break
    return found


def _bullets(values: Iterable[str], *, empty: str = "확인된 항목 없음") -> str:
    rows = list(values)
    return "\n".join(f"- {value}" for value in rows) if rows else f"- {empty}"


def _render_handoff(
    session: SessionInfo,
    extracted: Extracted,
    repo: dict[str, Any],
    document: Path,
    warnings: list[str],
) -> str:
    users = _unique(extracted.users, 40)
    finals = _unique(extracted.finals, 30)
    commands = _unique(extracted.commands, 60)
    errors = _unique(extracted.errors, 30)
    paths = sorted(extracted.paths)[:MAX_PATHS]
    constraints = _unique((text for text in users if CONSTRAINT_RE.search(text)), 20)
    open_items = _unique(
        (text for text in [*reversed(users), *reversed(finals)] if OPEN_RE.search(text)), 12
    )
    goal = users[-1] if users else "원문에서 사용자 목표를 추출하지 못함"

    if repo.get("state") == "verified":
        status = repo.get("status") or []
        repo_text = (
            f"- 저장소: `{repo['root']}`\n"
            f"- 브랜치: `{repo['branch']}`\n"
            f"- HEAD: `{_bounded(repo['head'], 500)}`\n"
            f"- 작업 트리: {'변경 있음' if repo['dirty'] else '깨끗함'}\n"
            + ("- 현재 변경:\n" + _bullets(f"`{line}`" for line in status) if status else "")
        )
    else:
        repo_text = f"- 검증 상태: `{repo.get('state')}`\n- 이유: {_bounded(repo.get('reason'), 1_000)}"

    next_steps = open_items[:7]
    if not next_steps:
        next_steps = [
            "이 문서와 현재 저장소 상태를 다시 확인한다.",
            "마지막 사용자 목표에서 아직 끝나지 않은 부분을 식별한다.",
            "변경 전 관련 테스트와 제약 조건을 재검증한다.",
        ]

    return f"""# Codex Session Handoff

> 새 작업 시작용 문서입니다. 대화 원문을 그대로 신뢰하지 말고 아래 Git 실측값과 현재 파일을 먼저 재확인하세요.

## Reactivation Prompt

```text
We are continuing from {document}. Read this handoff first, inspect the current repository state, verify every stale or uncertain claim, preserve existing uncommitted work, and continue from the numbered next steps without loading the old Codex session.
```

## Identity And Freshness

- 생성 시각: `{utc_iso()}`
- 세션 제목: `{_bounded(session.title, 500)}`
- 세션 키: `{session.key}`
- 원문 크기: `{session.size}` bytes
- 원문 수정시각(ns): `{session.fingerprint.mtime_ns}`
- 추출 방식: `{'대형 파일 구간 추출 + 최신 요약' if extracted.sampled else '전체 bounded streaming'}`

## Current Goal

{goal}

## Live Repository Verification

{repo_text}

## User Requests And Context

{_bullets(users)}

## Decisions, Outcomes, And Work Already Reported

{_bullets(finals)}

## Compacted Context Summaries

{_bullets(_unique(extracted.summaries, 3, text_limit=MAX_SUMMARY))}

## Files Touched Or Investigated

{_bullets(f'`{path}`' for path in paths)}

## Commands And Checks Seen In The Session

{_bullets(f'`{command}`' for command in commands)}

## Known Errors And Failed Checks

{_bullets(errors)}

## Constraints And Do-Not-Touch Areas

{_bullets(constraints)}

## Open Decisions And Unfinished Work

{_bullets(open_items)}

## Next Steps

{os.linesep.join(f'{index}. {value}' for index, value in enumerate(next_steps, 1))}

## Extraction Warnings

{_bullets(warnings, empty='경고 없음')}
"""


def generate_handoff(session: SessionInfo, root: Path) -> dict[str, Any]:
    """Generate one handoff and its freshness sidecar atomically."""
    before = TranscriptFingerprint.read(session.rollout_path)
    extracted = extract_transcript(session.rollout_path)
    after = TranscriptFingerprint.read(session.rollout_path)
    if before != after:
        return {
            "key": session.key,
            "ok": False,
            "error": "transcript changed while it was being read; retry after the session is idle",
        }

    # Transcript metadata is the execution-time source of truth.  The threads
    # table can retain the cwd from an earlier handoff/imported task.
    cwd = extracted.cwd or session.cwd
    repo = inspect_repository(cwd)
    warnings = []
    if extracted.sampled:
        warnings.append(
            "원문이 256MB를 넘어 앞부분과 최근 256MB 및 그 안의 최신 압축 요약을 사용했습니다."
        )
    if extracted.skipped_large_records:
        warnings.append(f"16MB를 넘는 레코드 {extracted.skipped_large_records}개를 건너뛰었습니다.")
    if extracted.invalid_records:
        warnings.append(f"해석할 수 없는 JSONL 레코드 {extracted.invalid_records}개가 있었습니다.")
    if repo.get("state") != "verified":
        warnings.append("연결된 Git 저장소를 실측 검증하지 못했습니다.")
    elif repo.get("status_truncated"):
        warnings.append("Git 변경 파일이 250개를 넘어 handoff에는 앞의 250개만 기록했습니다.")
    if not extracted.users:
        warnings.append("사용자 메시지를 추출하지 못했습니다.")
    if not extracted.finals and not extracted.summaries:
        warnings.append("완료 결과나 압축 요약을 추출하지 못했습니다.")
    if extracted.sampled and not extracted.summaries:
        warnings.append("대형 세션의 중간 구간을 대표할 압축 요약이 없어 정리 안전 기준을 충족하지 못했습니다.")
    quality_ready = bool(
        extracted.users
        and (extracted.finals or extracted.summaries)
        and (not extracted.sampled or extracted.summaries)
    )

    document, metadata = _artifact_paths(session, root)
    root.mkdir(parents=True, exist_ok=True)
    markdown = _render_handoff(session, extracted, repo, document, warnings)
    temp_document = document.with_suffix(".md.tmp")
    temp_metadata = metadata.with_suffix(".json.tmp")
    temp_document.write_text(markdown, encoding="utf-8")
    manifest = {
        "version": 1,
        "key": session.key,
        "thread_id_hash": hashlib.sha256(session.thread_id.encode()).hexdigest(),
        "generated_at": utc_iso(),
        "fingerprint": after.to_dict(),
        "document": str(document),
        "repository": repo,
        "warnings": warnings,
        "quality_ready": quality_ready,
    }
    temp_metadata.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp_document.replace(document)
    temp_metadata.replace(metadata)
    return {
        "key": session.key,
        "ok": quality_ready,
        "ready": quality_ready,
        "document": str(document),
        "warnings": warnings,
        "repository_state": repo.get("state"),
        "error": None if quality_ready else "handoff lacks enough conversation evidence",
    }


def generate_selected(
    home: CodexHome,
    root: Path,
    selected_keys: Iterable[str],
    *,
    pinned: Iterable[str] = (),
) -> dict[str, Any]:
    requested = {str(key) for key in selected_keys}
    sessions = {session.key: session for session in list_sessions(home, pinned=pinned)}
    results = []
    for key in requested:
        session = sessions.get(key)
        if session is None:
            results.append({"key": key, "ok": False, "error": "session is missing or unsafe"})
            continue
        try:
            results.append(generate_handoff(session, root))
        except (OSError, ValueError) as exc:
            results.append({"key": key, "ok": False, "error": _bounded(exc, 1_000)})
    return {
        "ok": bool(results) and all(item.get("ok") for item in results),
        "generated": sum(1 for item in results if item.get("ok")),
        "requested": len(requested),
        "results": results,
    }
