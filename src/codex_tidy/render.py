"""Output rendering: readable text for people, stable JSON for scripts."""

from __future__ import annotations

import json
from typing import Any, Iterable

from .env import human_bytes
from .model import (
    LEVEL_BLOCKED,
    LEVEL_INFO,
    LEVEL_NOTICE,
    LEVEL_WARN,
    Finding,
    Plan,
    Settings,
)
from .tasks import ALL_TASKS

MARKERS = {
    LEVEL_BLOCKED: "[x]",
    LEVEL_WARN: "[!]",
    LEVEL_NOTICE: "[*]",
    LEVEL_INFO: "   ",
}
TASK_TITLES = {task.name: task.title for task in ALL_TASKS}
RULE = "-" * 72

# English copy for the language-neutral codes emitted by advice.py. The browser
# UI keeps its own Korean table against the same codes.
VERDICT_LINES = {
    "now": "Worth cleaning up now.",
    "soon": "Usable as is; clean up before long.",
    "fine": "No pressure yet; nothing here is affecting performance.",
}
SIGNAL_LABELS = {
    "hot_path": "old sessions in the hot path",
    "metadata": "longest thread display metadata",
    "logs": "log databases",
    "leftovers": "interrupted-write leftovers",
    "orphans": "unreferenced transcripts",
    "worktrees": "stale worktrees",
    "disk_free": "free disk space",
    "dangling": "thread rows with a missing transcript",
}
GUIDANCE_LINES = {
    "codex_running": "Close Codex first: {count} process(es) still hold its databases.",
    "handoff_before_archive": (
        "Write a handoff note for any of the {count} session(s) you may want to continue. "
        "Archiving keeps the file, but starting a fresh thread from a handoff is what "
        "actually keeps it fast."
    ),
    "dirty_worktrees_excluded": "{count} worktree(s) have uncommitted changes and were excluded.",
    "pinned_protected": "{count} pinned session(s) are never touched.",
    "large_but_recent": "{count} large session(s) are still recent and were left alone.",
    "dangling_rows": "{count} thread row(s) point at a transcript that is gone; cleaning will not fix that.",
    "append_only_not_undone": "{count} append-only operation(s) will not be undone by restore.",
    "task_declined": "{task} declined to act: {reason}",
    "backup_location": "Backups and the undo journal go to {path}.",
    "nothing_to_do": "No operations to run.",
}


def _bytes_or_count(signal) -> str:
    if signal.unit == "bytes":
        return human_bytes(signal.value)
    if signal.unit == "chars":
        return f"{signal.value:,} chars"
    return str(signal.value)


def render_assessment(assessment) -> list[str]:
    marks = {"high": "[!]", "watch": "[*]", "ok": "   "}
    lines = ["Verdict", f"  {VERDICT_LINES.get(assessment.verdict, assessment.verdict)}"]
    for signal in assessment.worst:
        if signal.severity == "ok":
            continue
        lines.append(
            f"  {marks[signal.severity]} {SIGNAL_LABELS.get(signal.code, signal.code)}: "
            f"{_bytes_or_count(signal)}"
        )
    if assessment.guidance:
        lines.append("Guidance")
        for item in assessment.guidance:
            template = GUIDANCE_LINES.get(item.code)
            if template is None:
                continue
            try:
                lines.append(f"      {template.format(**item.params)}")
            except (KeyError, IndexError):
                lines.append(f"      {item.code}")
    lines.append(RULE)
    return lines


def _group(findings: Iterable[Finding]) -> dict[str, list[Finding]]:
    grouped: dict[str, list[Finding]] = {}
    for finding in findings:
        grouped.setdefault(finding.task, []).append(finding)
    return grouped


def render_text(
    plan: Plan,
    *,
    command: str,
    settings: Settings,
    extra: list[str] | None = None,
    assessment=None,
    show_operations: bool = True,
) -> str:
    lines = [
        f"codex-tidy {command}",
        f"  codex home   {plan.codex_home}",
        f"  privacy      {'raw values shown (--reveal)' if settings.reveal else 'pseudonymous'}",
        f"  run          {plan.created}",
        RULE,
    ]
    if assessment is not None:
        lines.extend(render_assessment(assessment))

    grouped = _group(plan.findings)
    for task in ALL_TASKS:
        findings = grouped.get(task.name)
        if not findings:
            continue
        lines.append(TASK_TITLES.get(task.name, task.name))
        for finding in findings:
            lines.append(f"  {MARKERS.get(finding.level, '   ')} {finding.message}")
        lines.append("")

    if plan.skipped:
        lines.append("Skipped")
        for name, reason in sorted(plan.skipped.items()):
            lines.append(f"      {name}: {reason}")
        lines.append("")

    if plan.operations and show_operations:
        lines.append(f"Planned operations ({len(plan.operations)})")
        by_task: dict[str, list[int]] = {}
        for index, op in enumerate(plan.operations):
            by_task.setdefault(op.task, []).append(index)
        for task_name, indexes in by_task.items():
            moved = sum(plan.operations[i].bytes for i in indexes)
            suffix = f", {human_bytes(moved)}" if moved else ""
            lines.append(f"      {task_name}: {len(indexes)} operation(s){suffix}")
        non_invertible = [op for op in plan.operations if not op.invertible]
        if non_invertible:
            lines.append(
                f"      note: {len(non_invertible)} append-only operation(s) are not undone by restore"
            )
        lines.append("")

    lines.append("Summary")
    lines.append(f"      movable        {human_bytes(plan.reclaimable_bytes)}")
    blocked = [f for f in plan.findings if f.level == LEVEL_BLOCKED]
    warnings = [f for f in plan.findings if f.level == LEVEL_WARN]
    lines.append(f"      warnings       {len(warnings)}")
    if blocked:
        lines.append(f"      blocked        {blocked[0].message}")
    for note in extra or []:
        lines.append(f"      {note}")
    return "\n".join(lines).rstrip() + "\n"


def render_json(
    plan: Plan,
    *,
    command: str,
    settings: Settings,
    extra: dict[str, Any] | None = None,
    assessment=None,
) -> str:
    payload: dict[str, Any] = {
        "command": command,
        "reveal": settings.reveal,
        "assessment": assessment.to_dict() if assessment is not None else None,
        "items": [item.to_dict() for item in plan.items()],
        **plan.to_dict(),
        "summary": {
            "movable_bytes": plan.reclaimable_bytes,
            "operations": len(plan.operations),
            "warnings": sum(1 for f in plan.findings if f.level == LEVEL_WARN),
            "blocked": [f.message for f in plan.findings if f.level == LEVEL_BLOCKED],
            "tasks_touched": list(plan.tasks_touched),
        },
    }
    if extra:
        payload.update(extra)
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
