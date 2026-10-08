"""Readiness assessment: is it worth cleaning up yet?

Answers the question a size report does not: *should I act now, or keep working?*

Everything here is language-neutral on purpose. Signals and guidance are emitted
as stable codes plus numbers; the CLI renders them in English and the browser UI
renders them in Korean. No user-facing sentence lives in this module.

Every threshold below is a judgement call, so each one carries the reason it sits
where it does. They are constants, not magic numbers buried in conditionals, so a
team can disagree and move them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .model import LEVEL_BLOCKED, Plan

MB = 1024**2
GB = 1024**3

SEVERITY_OK = "ok"
SEVERITY_WATCH = "watch"
SEVERITY_HIGH = "high"

VERDICT_FINE = "fine"
VERDICT_SOON = "soon"
VERDICT_NOW = "now"

# --- Thresholds -----------------------------------------------------------
# Sessions still in the hot path. Codex reads this directory constantly, so the
# cost is ongoing rather than a one-off; 500 MB is where it stops being noise.
HOT_PATH_HIGH = 500 * MB
HOT_PATH_WATCH = 100 * MB

# The documented pathological shape: a preview long enough that rendering the
# thread list is slow before any chat is opened.
PREVIEW_PATHOLOGICAL_CHARS = 10_000

# Log databases are pure write-ahead history; nothing reads them back.
LOGS_HIGH = 256 * MB

# Leftovers are individually small, so size alone rarely justifies acting.
LEFTOVER_HIGH = 50 * MB

# Orphan transcripts are dead weight age-based cleanup can never reach.
ORPHAN_HIGH = 200 * MB

# Worktrees are big but often still wanted, so the bar is deliberately high.
WORKTREE_HIGH = 1 * GB

# Disk pressure changes the calculus entirely.
DISK_HIGH = 5 * GB
DISK_WATCH = 20 * GB

# Regardless of any single signal, this much movable data is worth a run.
TOTAL_NOW = 1 * GB


@dataclass(frozen=True)
class Signal:
    """One measured pressure, with the threshold it was judged against."""

    code: str
    severity: str
    value: int
    unit: str  # "bytes" | "chars" | "count"
    threshold: int = 0
    extra: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "value": self.value,
            "unit": self.unit,
            "threshold": self.threshold,
            "extra": dict(self.extra),
        }


@dataclass(frozen=True)
class Guidance:
    """Something the user should know or do, as a code the UI translates."""

    code: str
    params: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "params": dict(self.params)}


@dataclass
class Assessment:
    verdict: str
    signals: list[Signal] = field(default_factory=list)
    guidance: list[Guidance] = field(default_factory=list)
    reclaimable_bytes: int = 0
    blocked: bool = False

    @property
    def worst(self) -> list[Signal]:
        order = {SEVERITY_HIGH: 0, SEVERITY_WATCH: 1, SEVERITY_OK: 2}
        return sorted(self.signals, key=lambda s: (order[s.severity], -s.value))

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "reclaimable_bytes": self.reclaimable_bytes,
            "blocked": self.blocked,
            "signals": [s.to_dict() for s in self.worst],
            "guidance": [g.to_dict() for g in self.guidance],
        }


def _finding(plan: Plan, task: str, code: str):
    for finding in plan.findings:
        if finding.task == task and finding.code == code:
            return finding
    return None


def _grade(value: int, watch: int, high: int) -> str:
    if high and value >= high:
        return SEVERITY_HIGH
    if value >= watch:
        return SEVERITY_WATCH
    return SEVERITY_OK


def assess(plan: Plan) -> Assessment:
    """Turn a scanned plan into a verdict plus the reasons behind it."""
    signals: list[Signal] = []
    guidance: list[Guidance] = []

    sessions = _finding(plan, "sessions", "old_candidates")
    if sessions is not None:
        signals.append(
            Signal(
                "hot_path",
                _grade(sessions.bytes, HOT_PATH_WATCH, HOT_PATH_HIGH),
                sessions.bytes,
                "bytes",
                HOT_PATH_HIGH,
                {"count": sessions.count},
            )
        )
        if sessions.count:
            guidance.append(Guidance("handoff_before_archive", {"count": sessions.count}))

    meta = _finding(plan, "thread-meta", "totals")
    if meta is not None:
        max_preview = int(meta.detail.get("max_preview_chars", 0) or 0)
        max_title = int(meta.detail.get("max_title_chars", 0) or 0)
        worst = max(max_preview, max_title)
        over = _finding(plan, "thread-meta", "over_limit")
        severity = SEVERITY_OK
        if worst >= PREVIEW_PATHOLOGICAL_CHARS:
            severity = SEVERITY_HIGH
        elif over is not None and over.count:
            severity = SEVERITY_WATCH
        signals.append(
            Signal(
                "metadata",
                severity,
                worst,
                "chars",
                PREVIEW_PATHOLOGICAL_CHARS,
                {
                    "total_chars": int(meta.detail.get("title_chars", 0) or 0)
                    + int(meta.detail.get("preview_chars", 0) or 0),
                    "over_limit": over.count if over is not None else 0,
                },
            )
        )

    logs = _finding(plan, "logs", "size")
    if logs is not None:
        over_threshold = bool(logs.detail.get("over_threshold"))
        severity = SEVERITY_HIGH if logs.bytes >= LOGS_HIGH else (
            SEVERITY_WATCH if over_threshold else SEVERITY_OK
        )
        signals.append(Signal("logs", severity, logs.bytes, "bytes", LOGS_HIGH))

    leftovers = _finding(plan, "leftovers", "candidates")
    if leftovers is not None and leftovers.count:
        signals.append(
            Signal(
                "leftovers",
                SEVERITY_HIGH if leftovers.bytes >= LEFTOVER_HIGH else SEVERITY_WATCH,
                leftovers.bytes,
                "bytes",
                LEFTOVER_HIGH,
                {"count": leftovers.count},
            )
        )

    orphans = _finding(plan, "integrity", "orphan_transcripts")
    if orphans is not None and orphans.count:
        signals.append(
            Signal(
                "orphans",
                SEVERITY_HIGH if orphans.bytes >= ORPHAN_HIGH else SEVERITY_WATCH,
                orphans.bytes,
                "bytes",
                ORPHAN_HIGH,
                {"count": orphans.count},
            )
        )

    worktrees = _finding(plan, "worktrees", "candidates")
    if worktrees is not None and worktrees.count:
        signals.append(
            Signal(
                "worktrees",
                SEVERITY_HIGH if worktrees.bytes >= WORKTREE_HIGH else SEVERITY_WATCH,
                worktrees.bytes,
                "bytes",
                WORKTREE_HIGH,
                {"count": worktrees.count},
            )
        )

    disk = _finding(plan, "environment", "disk_free")
    if disk is not None:
        severity = SEVERITY_HIGH if disk.bytes < DISK_HIGH else (
            SEVERITY_WATCH if disk.bytes < DISK_WATCH else SEVERITY_OK
        )
        signals.append(Signal("disk_free", severity, disk.bytes, "bytes", DISK_HIGH))

    dangling = _finding(plan, "integrity", "dangling_rows")
    if dangling is not None and dangling.count:
        # Not a size problem, so never "high": it will not be fixed by cleaning.
        signals.append(Signal("dangling", SEVERITY_WATCH, dangling.count, "count"))
        guidance.append(Guidance("dangling_rows", {"count": dangling.count}))

    # --- verdict ----------------------------------------------------------
    reclaimable = plan.reclaimable_bytes
    if any(s.severity == SEVERITY_HIGH for s in signals) or reclaimable >= TOTAL_NOW:
        verdict = VERDICT_NOW
    elif any(s.severity == SEVERITY_WATCH for s in signals):
        verdict = VERDICT_SOON
    else:
        verdict = VERDICT_FINE

    # --- guidance ---------------------------------------------------------
    blocked = any(f.level == LEVEL_BLOCKED for f in plan.findings)
    if blocked:
        running = _finding(plan, "environment", "codex_running")
        guidance.insert(0, Guidance("codex_running", {"count": running.count if running else 1}))

    dirty = _finding(plan, "worktrees", "dirty")
    if dirty is not None and dirty.count:
        guidance.append(Guidance("dirty_worktrees_excluded", {"count": dirty.count}))

    pinned = _finding(plan, "sessions", "pinned_skipped")
    if pinned is not None and pinned.count:
        guidance.append(Guidance("pinned_protected", {"count": pinned.count}))

    recent = _finding(plan, "sessions", "large_but_recent")
    if recent is not None and recent.count:
        guidance.append(Guidance("large_but_recent", {"count": recent.count}))

    non_invertible = [op for op in plan.operations if not op.invertible]
    if non_invertible:
        guidance.append(Guidance("append_only_not_undone", {"count": len(non_invertible)}))

    unsafe = [f for f in plan.findings if f.code in ("unsafe", "outside_sessions_root", "truncated")]
    for finding in unsafe:
        guidance.append(Guidance("task_declined", {"task": finding.task, "reason": finding.message}))

    if not plan.operations:
        guidance.append(Guidance("nothing_to_do", {}))
    else:
        guidance.append(Guidance("backup_location", {"path": plan.backup_root}))

    return Assessment(
        verdict=verdict,
        signals=signals,
        guidance=guidance,
        reclaimable_bytes=reclaimable,
        blocked=blocked,
    )
