"""Prune dead project entries from config.toml.

Rewriting a user's config by hand is the riskiest thing this tool does, so the
result is parsed back with tomllib and compared against the original before the
operation is allowed into the plan. If anything outside ``[projects]`` would
change, or the rewrite does not parse, the task reports and plans nothing.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ..model import (
    LEVEL_INFO,
    LEVEL_NOTICE,
    LEVEL_WARN,
    OP_WRITE_TEXT,
    Finding,
    Operation,
    operation_key,
)
from .base import Context, Task

HEADER_RE = re.compile(r"^\s*\[\[?(?P<inner>[^\]]+)\]\]?\s*$")
PROJECT_PREFIX_RE = re.compile(r"^projects\.(?P<rest>.+)$")

# Scratch locations across platforms. Entries here are dead by construction.
TEMP_PATH_RE = re.compile(
    r"("
    r"[\\/]AppData[\\/]Local[\\/]Temp[\\/]"
    r"|[\\/]Temp[\\/](codex|spark)-"
    r"|^/tmp/"
    r"|^/private/var/folders/"
    r"|^/var/folders/"
    r")",
    re.IGNORECASE,
)


@dataclass
class Block:
    key: str | None
    lines: list[str] = field(default_factory=list)


@dataclass
class Prune:
    removed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    new_text: str | None = None
    problem: str | None = None


def _project_key(inner: str) -> str | None:
    """Resolve ``projects."/some/path"`` (and sub-tables) to the project path."""
    match = PROJECT_PREFIX_RE.match(inner.strip())
    if not match:
        return None
    try:
        parsed = tomllib.loads(f"[projects.{match.group('rest')}]\n")
    except tomllib.TOMLDecodeError:
        return None
    node = parsed.get("projects")
    if not isinstance(node, dict) or len(node) != 1:
        return None
    return next(iter(node))


def _blocks(lines: list[str]) -> list[Block]:
    blocks = [Block(key=None)]
    for line in lines:
        header = HEADER_RE.match(line)
        if header:
            blocks.append(Block(key=_project_key(header.group("inner"))))
        blocks[-1].lines.append(line)
    return blocks


def _is_dead(project_path: str) -> bool:
    if TEMP_PATH_RE.search(project_path):
        return True
    try:
        return not Path(project_path).exists()
    except OSError:
        return False


def _plan_prune(path: Path) -> Prune:
    result = Prune()
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        result.problem = f"could not read config: {exc}"
        return result
    try:
        original = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        result.problem = f"config.toml does not parse, refusing to touch it ({exc})"
        return result

    dead = {key for key in (original.get("projects") or {}) if _is_dead(key)}
    result.kept = sorted(set(original.get("projects") or {}) - dead)
    result.removed = sorted(dead)
    if not dead:
        return result

    kept_lines: list[str] = []
    for block in _blocks(text.splitlines()):
        if block.key is not None and block.key in dead:
            continue
        kept_lines.extend(block.lines)
    candidate = "\n".join(kept_lines).rstrip("\n") + "\n"

    try:
        rewritten = tomllib.loads(candidate)
    except tomllib.TOMLDecodeError as exc:
        result.problem = f"rewrite would not parse, refusing to apply ({exc})"
        return result

    outside_before = {k: v for k, v in original.items() if k != "projects"}
    outside_after = {k: v for k, v in rewritten.items() if k != "projects"}
    if outside_before != outside_after:
        result.problem = "rewrite would change settings outside [projects], refusing to apply"
        return result
    if sorted(rewritten.get("projects") or {}) != result.kept:
        result.problem = "rewrite would not leave exactly the surviving projects, refusing to apply"
        return result

    result.new_text = candidate
    return result


class ConfigTomlTask(Task):
    name = "config"
    title = "Dead project entries in config.toml"

    def unavailable(self, ctx: Context) -> str | None:
        if not ctx.home.config_toml.is_file():
            return "config.toml does not exist"
        return None

    def scan(self, ctx: Context) -> list[Finding]:
        result = _plan_prune(ctx.home.config_toml)
        if result.problem:
            return [Finding(self.name, "unsafe", LEVEL_WARN, result.problem)]
        if not result.removed:
            return [
                Finding(
                    self.name,
                    "candidates",
                    LEVEL_INFO,
                    f"all {len(result.kept)} project entr(ies) still resolve",
                    count=0,
                )
            ]
        findings = [
            Finding(
                self.name,
                "candidates",
                LEVEL_NOTICE,
                f"{len(result.removed)} dead project entr(ies) of "
                f"{len(result.removed) + len(result.kept)} total",
                count=len(result.removed),
            )
        ]
        for project in result.removed[: ctx.settings.limit]:
            findings.append(
                Finding(self.name, "candidate", LEVEL_INFO, f"  {ctx.redact.path(project)}")
            )
        return findings

    def plan(self, ctx: Context) -> list[Operation]:
        result = _plan_prune(ctx.home.config_toml)
        if result.problem or result.new_text is None:
            return []
        return [
            Operation(
                OP_WRITE_TEXT,
                self.name,
                f"prune {len(result.removed)} dead project entr(ies) from config.toml",
                {
                    "path": str(ctx.home.config_toml),
                    "content": result.new_text,
                    "backup": str(ctx.backup_root / "config.toml.pre-prune"),
                },
                key=operation_key(self.name, str(ctx.home.config_toml)),
            )
        ]
