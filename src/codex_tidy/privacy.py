"""Redaction so a report can be pasted into a ticket without leaking content.

Pseudonyms are stable within a run, so the same thread reads the same in every
line. Pass a fixed ``salt`` to keep them stable across runs; the default is
random per run so pseudonyms cannot be correlated after the fact.
"""

from __future__ import annotations

import hashlib
import secrets
from pathlib import Path

from .env import canonical

HOME_TOKEN = "<codex-home>"


class Redactor:
    def __init__(self, *, reveal: bool = False, home: Path | None = None, salt: str | None = None):
        self.reveal = reveal
        self.home = canonical(home) if home else None
        self.salt = salt if salt is not None else secrets.token_hex(8)

    def _tag(self, value: str) -> str:
        digest = hashlib.blake2s(
            f"{self.salt}:{value}".encode("utf-8", "replace"), digest_size=3
        )
        return digest.hexdigest()

    def thread(self, thread_id: str) -> str:
        if self.reveal:
            return str(thread_id)
        return f"thread:{self._tag(str(thread_id))}"

    def path(self, path: str | Path) -> str:
        target = Path(path)
        if self.reveal:
            return str(target)
        if self.home is None:
            return f"<path:{self._tag(str(target))}>"
        try:
            relative = canonical(target).relative_to(self.home)
        except ValueError:
            # Outside the Codex home: even the directory names may be private.
            return f"<external:{self._tag(str(target))}>"
        parts = list(relative.parts)
        if not parts:
            return HOME_TOKEN
        # Directory structure under the Codex home is generic (sessions/2026/08),
        # so keep it for orientation and pseudonymise only the leaf.
        leaf = Path(parts[-1])
        parts[-1] = f"{self._tag(parts[-1])}{leaf.suffix}"
        return "/".join([HOME_TOKEN, *parts])

    def snippet(self, text: str, limit: int = 48) -> str:
        text = " ".join((text or "").split())
        if not self.reveal:
            return f"<{len(text)} chars>"
        if len(text) <= limit:
            return text
        return text[: limit - 1].rstrip() + "…"

    def process(self, pid: int, name: str) -> str:
        if self.reveal:
            return f"pid {pid} {name}"
        return f"pid {pid} {name or 'process'}"
