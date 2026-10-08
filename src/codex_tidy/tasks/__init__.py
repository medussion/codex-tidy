"""Task registry.

Order matters for output and for execution: report-only context first, then
database-only edits, then filesystem moves, then the config rewrite last so a
failure earlier never leaves the config out of step with the rest.
"""

from __future__ import annotations

from .base import Context, Task
from .config_toml import ConfigTomlTask
from .environment import EnvironmentTask
from .integrity import IntegrityTask
from .leftovers import LeftoversTask
from .logs import LogsTask
from .sessions import SessionsTask
from .thread_meta import ThreadMetadataTask
from .winpaths import WindowsPathsTask
from .worktrees import WorktreesTask

ALL_TASKS: tuple[Task, ...] = (
    EnvironmentTask(),
    WindowsPathsTask(),
    ThreadMetadataTask(),
    IntegrityTask(),
    SessionsTask(),
    WorktreesTask(),
    LogsTask(),
    LeftoversTask(),
    ConfigTomlTask(),
)

TASK_NAMES = tuple(task.name for task in ALL_TASKS)

__all__ = ["ALL_TASKS", "TASK_NAMES", "Context", "Task"]
