"""Thread title / preview metadata bloat.

Some Codex builds store an entire first user prompt as both the thread title and
the sidebar preview. Once those fields reach hundreds of thousands of characters,
rendering the thread list gets slow before any chat is even opened.

Trimming is strictly opt-in. The transcript itself is never touched -- only the
display strings -- and the original values are journalled so they can be put back.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..env import human_bytes, utc_iso
from ..model import (
    LEVEL_INFO,
    LEVEL_NOTICE,
    LEVEL_WARN,
    OP_APPEND_JSONL,
    OP_SQL_UPDATE,
    Finding,
    Operation,
    operation_key,
)
from .base import Context, Task

PREVIEW_COLUMN = "first_user_message"
ALARMING_PREVIEW_CHARS = 10_000


def bounded_text(value: str, limit: int) -> str:
    """Collapse whitespace and clamp to a display-sized string."""
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3].rstrip() + "..."


@dataclass(frozen=True)
class Repair:
    thread_id: str
    old_title: str
    new_title: str
    old_preview: str
    new_preview: str

    @property
    def saved_chars(self) -> int:
        return (len(self.old_title) - len(self.new_title)) + (
            len(self.old_preview) - len(self.new_preview)
        )


def _has_preview(ctx: Context) -> bool:
    return bool(ctx.schema and PREVIEW_COLUMN in ctx.schema.cols("threads"))


def _repairs(ctx: Context) -> list[Repair]:
    assert ctx.conn is not None and ctx.schema is not None
    settings = ctx.settings
    has_preview = _has_preview(ctx)
    preview_select = PREVIEW_COLUMN if has_preview else "''"
    size_clause = "length(title) > ?"
    params: list = [settings.title_limit]
    if has_preview:
        size_clause += f" or length({PREVIEW_COLUMN}) > ?"
        params.append(settings.preview_limit)

    rows = ctx.conn.execute(
        f"select id, title, {preview_select} as preview from threads "
        f"where {ctx.schema.active_threads_predicate()} and ({size_clause})",
        params,
    ).fetchall()

    repairs = []
    for row in rows:
        thread_id = str(row["id"])
        if thread_id in ctx.pinned:
            continue
        old_title = str(row["title"] or "")
        old_preview = str(row["preview"] or "") if has_preview else ""
        new_title = bounded_text(old_title, settings.title_limit)
        new_preview = bounded_text(old_preview, settings.preview_limit) if has_preview else ""
        if new_title != old_title or new_preview != old_preview:
            repairs.append(Repair(thread_id, old_title, new_title, old_preview, new_preview))
    repairs.sort(key=lambda item: item.saved_chars, reverse=True)
    return repairs


class ThreadMetadataTask(Task):
    name = "thread-meta"
    title = "Thread title / preview metadata"

    def unavailable(self, ctx: Context) -> str | None:
        return ctx.requires_threads("id", "title")

    def scan(self, ctx: Context) -> list[Finding]:
        assert ctx.conn is not None and ctx.schema is not None
        settings = ctx.settings
        has_preview = _has_preview(ctx)
        preview_len = f"length({PREVIEW_COLUMN})" if has_preview else "0"

        row = ctx.conn.execute(
            f"""
            select
              count(*)                                              as rows_active,
              coalesce(sum(length(title)), 0)                       as title_chars,
              coalesce(max(length(title)), 0)                       as max_title,
              coalesce(sum({preview_len}), 0)                       as preview_chars,
              coalesce(max({preview_len}), 0)                       as max_preview,
              coalesce(sum(case when length(title) > ? then 1 else 0 end), 0)   as title_over,
              coalesce(sum(case when {preview_len} > ? then 1 else 0 end), 0)   as preview_over,
              coalesce(sum(case when {preview_len} > ? then 1 else 0 end), 0)   as preview_alarming
            from threads
            where {ctx.schema.active_threads_predicate()}
            """,
            (settings.title_limit, settings.preview_limit, ALARMING_PREVIEW_CHARS),
        ).fetchone()

        total_chars = int(row["title_chars"]) + int(row["preview_chars"])
        findings = [
            Finding(
                self.name,
                "totals",
                LEVEL_INFO,
                f"{row['rows_active']} active thread(s) carry {total_chars:,} chars of "
                f"display metadata (~{human_bytes(total_chars)})",
                count=int(row["rows_active"]),
                detail={
                    "title_chars": int(row["title_chars"]),
                    "preview_chars": int(row["preview_chars"]),
                    "max_title_chars": int(row["max_title"]),
                    "max_preview_chars": int(row["max_preview"]),
                    "preview_column_present": has_preview,
                },
            )
        ]

        over = int(row["title_over"]) + int(row["preview_over"])
        if over:
            findings.append(
                Finding(
                    self.name,
                    "over_limit",
                    LEVEL_NOTICE,
                    f"{int(row['title_over'])} title(s) over {settings.title_limit} chars, "
                    f"{int(row['preview_over'])} preview(s) over {settings.preview_limit} chars",
                    count=over,
                )
            )
        if int(row["preview_alarming"]):
            findings.append(
                Finding(
                    self.name,
                    "pathological",
                    LEVEL_WARN,
                    f"{int(row['preview_alarming'])} preview(s) exceed "
                    f"{ALARMING_PREVIEW_CHARS:,} chars (longest {int(row['max_preview']):,}) -- "
                    "this is the shape that makes the thread list sluggish",
                    count=int(row["preview_alarming"]),
                )
            )
            if not ctx.settings.repair_thread_metadata:
                findings.append(
                    Finding(
                        self.name,
                        "repair_available",
                        LEVEL_INFO,
                        "add --repair-thread-metadata to include a reversible trim in the plan",
                    )
                )
        return findings

    def plan(self, ctx: Context) -> list[Operation]:
        if not ctx.settings.repair_thread_metadata:
            return []
        has_preview = _has_preview(ctx)
        operations: list[Operation] = []

        for repair in _repairs(ctx):
            assignments: dict = {"title": repair.new_title}
            undo: dict = {"title": repair.old_title}
            if has_preview:
                assignments[PREVIEW_COLUMN] = repair.new_preview
                undo[PREVIEW_COLUMN] = repair.old_preview

            operations.append(
                Operation(
                    OP_SQL_UPDATE,
                    self.name,
                    f"trim {ctx.redact.thread(repair.thread_id)} "
                    f"(-{repair.saved_chars:,} chars)",
                    {
                        "table": "threads",
                        "key_column": "id",
                        "key": repair.thread_id,
                        "set": assignments,
                        "undo": undo,
                    },
                    key=operation_key(self.name, repair.thread_id),
                )
            )
            if repair.new_title and repair.new_title != repair.old_title:
                # Codex records renames here; keeping it consistent avoids the
                # trimmed title being overwritten from the index later.
                operations.append(
                    Operation(
                        OP_APPEND_JSONL,
                        self.name,
                        f"index rename {ctx.redact.thread(repair.thread_id)}",
                        {
                            "path": str(ctx.home.session_index),
                            "record": {
                                "id": repair.thread_id,
                                "thread_name": repair.new_title,
                                "updated_at": utc_iso(),
                            },
                        },
                        key=operation_key(self.name, repair.thread_id),
                    )
                )
        return operations
