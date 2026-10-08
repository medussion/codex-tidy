"""Environment discovery: Codex home layout, processes, locking, disk space.

Everything in this module is read-only with respect to Codex state. The only
thing it can create is the run lock.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

STATE_DB_NAME = "state_5.sqlite"
LOG_DB_GLOB = "logs_2.sqlite*"
GLOBAL_STATE_NAME = ".codex-global-state.json"
SESSION_INDEX_NAME = "session_index.jsonl"
CONFIG_NAME = "config.toml"
LOCK_NAME = ".codex-tidy.lock"

# Databases Codex is known to keep in its home. Any of them being open means a
# mutating run is unsafe, not just the one we intend to edit.
CODEX_DB_NAMES = (
    STATE_DB_NAME,
    "logs_2.sqlite",
    "memories_1.sqlite",
    "goals_1.sqlite",
)

# Half-written state files Codex leaves behind when an atomic replace is
# interrupted. They accumulate indefinitely and nothing ever reads them again.
LEFTOVER_GLOBS = ("..*.tmp-*", ".*.tmp-*", "*.tmp-*")

# Copied verbatim into the backup folder before any mutation runs.
BACKUP_ITEMS = (
    GLOBAL_STATE_NAME,
    CONFIG_NAME,
    "history.jsonl",
    "installation_id",
    "models_cache.json",
    SESSION_INDEX_NAME,
    "version.json",
    "memories",
    "skills",
    "rules",
    "plugins",
    "automations",
)

# Build output we never want to drag into a backup.
COPY_IGNORE = (
    "node_modules",
    ".git",
    ".next",
    "dist",
    "build",
    ".venv",
    "__pycache__",
    ".pytest_cache",
)

SIZE_UNITS = (("TB", 1 << 40), ("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso() -> str:
    return utc_now().isoformat(timespec="milliseconds").replace("+00:00", "Z")


def run_stamp() -> str:
    return utc_now().strftime("%Y%m%dT%H%M%SZ")


def canonical(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError:
        return path.absolute()


def human_bytes(value: int | float) -> str:
    value = float(value)
    for label, scale in SIZE_UNITS:
        if abs(value) >= scale:
            return f"{value / scale:.2f} {label}"
    return f"{int(value)} B"


@dataclass(frozen=True)
class CodexHome:
    """Resolved Codex state directory plus the paths we care about."""

    root: Path

    @property
    def state_db(self) -> Path:
        return self.root / STATE_DB_NAME

    @property
    def config_toml(self) -> Path:
        return self.root / CONFIG_NAME

    @property
    def global_state(self) -> Path:
        return self.root / GLOBAL_STATE_NAME

    @property
    def session_index(self) -> Path:
        return self.root / SESSION_INDEX_NAME

    @property
    def sessions(self) -> Path:
        return self.root / "sessions"

    @property
    def worktrees(self) -> Path:
        return self.root / "worktrees"

    @property
    def archived_sessions(self) -> Path:
        return self.root / "archived_sessions"

    @property
    def archived_worktrees(self) -> Path:
        return self.root / "archived_worktrees"

    @property
    def archived_logs(self) -> Path:
        return self.root / "archived_logs"

    @property
    def archived_leftovers(self) -> Path:
        return self.root / "archived_leftovers"

    @property
    def scratch(self) -> Path:
        return self.root / "tmp"

    def log_dbs(self) -> list[Path]:
        return sorted(p for p in self.root.glob(LOG_DB_GLOB) if p.is_file())

    def other_databases(self) -> list[Path]:
        """Every SQLite file in the home, including ones we never modify."""
        found = {p for p in self.root.glob("*.sqlite") if p.is_file()}
        found |= {p for p in (self.root / "sqlite").glob("*.db") if p.is_file()}
        return sorted(found)

    def leftover_files(self) -> list[Path]:
        found: set[Path] = set()
        for pattern in LEFTOVER_GLOBS:
            found |= {p for p in self.root.glob(pattern) if p.is_file()}
        return sorted(found)

    def exists(self) -> bool:
        return self.root.is_dir()


def resolve_codex_home(explicit: str | os.PathLike[str] | None = None) -> CodexHome:
    """Explicit flag wins, then CODEX_HOME, then ~/.codex."""
    if explicit:
        return CodexHome(canonical(Path(explicit).expanduser()))
    from_env = os.environ.get("CODEX_HOME")
    if from_env:
        return CodexHome(canonical(Path(from_env).expanduser()))
    return CodexHome(canonical(Path.home() / ".codex"))


def default_backup_root(home: CodexHome) -> Path:
    """Prefer Documents so backups are visible and easy to delete by hand."""
    documents = Path.home() / "Documents"
    if documents.is_dir():
        return documents / "Codex" / "codex-tidy-backups"
    return home.root / "backups" / "codex-tidy"


def pinned_thread_ids(home: CodexHome) -> frozenset[str]:
    """Threads the user pinned are never candidates for anything."""
    try:
        data = json.loads(home.global_state.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    if not isinstance(data, dict):
        return frozenset()
    raw = data.get("pinned-thread-ids") or data.get("pinnedThreadIds") or []
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(str(item) for item in raw if item)


def dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def free_bytes(path: Path) -> int:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return 0


# --------------------------------------------------------------------------
# Processes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    name: str
    command: str = ""
    rss_bytes: int = 0
    #: Why this process is considered a Codex process.
    reason: str = ""


def _windows_process_rows() -> list[dict]:
    command = (
        "Get-CimInstance Win32_Process | "
        "Select-Object Name,ProcessId,CommandLine,WorkingSetSize | "
        "ConvertTo-Json -Compress"
    )
    output = subprocess.check_output(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        text=True,
        stderr=subprocess.DEVNULL,
    )
    if not output.strip():
        return []
    data = json.loads(output)
    return data if isinstance(data, list) else [data]


def _executable_name(args: str) -> str:
    """Best-effort program name from a full command line.

    Splitting on whitespace is wrong on macOS, where application bundles put
    spaces in the executable path ("Codex (Service)"). Splitting at the first
    argument flag keeps the whole path intact instead.
    """
    head = args.split(" -", 1)[0].strip() or args
    return Path(head).name


def _all_processes() -> list[ProcInfo]:
    try:
        if os.name == "nt":
            found = []
            for row in _windows_process_rows():
                try:
                    pid = int(row.get("ProcessId") or 0)
                except (TypeError, ValueError):
                    continue
                found.append(
                    ProcInfo(
                        pid=pid,
                        name=str(row.get("Name") or ""),
                        command=str(row.get("CommandLine") or ""),
                        rss_bytes=int(row.get("WorkingSetSize") or 0),
                    )
                )
            return found
        output = subprocess.check_output(
            ["ps", "-axo", "pid=,rss=,args="], text=True, stderr=subprocess.DEVNULL
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return []

    found = []
    for line in output.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            rss_kb = int(parts[1])
        except ValueError:
            continue
        found.append(
            ProcInfo(
                pid=pid,
                name=_executable_name(parts[2]),
                command=parts[2],
                rss_bytes=rss_kb * 1024,
            )
        )
    return found


# Exact program names. Anything else has to prove itself through its arguments.
CODEX_PROGRAM_NAMES = {"codex", "Codex", "codex.exe", "Codex.exe"}
CODEX_ARG_MARKERS = ("codex app-server", "openai.codex", "/codex.app/")


def _looks_like_codex(proc: ProcInfo) -> bool:
    if proc.name in CODEX_PROGRAM_NAMES:
        return True
    haystack = f"{proc.name} {proc.command}".lower()
    return any(marker in haystack for marker in CODEX_ARG_MARKERS)


def _lsof_holders(paths: list[Path]) -> dict[int, tuple[str, set[str]]]:
    """Map pid -> (program name, files held) for the given paths.

    This is the authoritative signal: it answers "is anything using this database
    right now", instead of guessing from process names. Unavailable on Windows and
    wherever lsof is absent, in which case we fall back to name matching.
    """
    existing = [str(path) for path in paths if path.exists()]
    if not existing or os.name == "nt" or shutil.which("lsof") is None:
        return {}
    try:
        # lsof exits 1 when any requested path has no open handle, which is the
        # normal case here, so the return code is not an error signal. Read stdout.
        completed = subprocess.run(
            ["lsof", "-F", "pcn", "--", *existing],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    output = completed.stdout

    holders: dict[int, tuple[str, set[str]]] = {}
    pid: int | None = None
    name = ""
    for line in output.splitlines():
        if not line:
            continue
        tag, value = line[0], line[1:]
        if tag == "p":
            try:
                pid = int(value)
            except ValueError:
                pid = None
            name = ""
        elif tag == "c":
            name = value
        elif tag == "n" and pid is not None:
            entry = holders.setdefault(pid, (name, set()))
            holders[pid] = (name or entry[0], entry[1] | {Path(value).name})
    return holders


def codex_state_files(home: "CodexHome") -> list[Path]:
    files: list[Path] = []
    for name in CODEX_DB_NAMES:
        base = home.root / name
        files.extend([base, Path(f"{base}-wal"), Path(f"{base}-shm")])
    return files


def is_default_home(home: "CodexHome") -> bool:
    """True when this is the Codex home a running Codex would actually use."""
    return home.root == resolve_codex_home(None).root


def codex_processes(home: "CodexHome | None" = None) -> list[ProcInfo]:
    """Processes that would make mutating this home unsafe.

    Two signals, reported separately so the user can see which one fired:

    * a process holding one of this home's databases open -- authoritative
    * a process that looks like Codex by name -- a fallback for platforms with no
      ``lsof``, and a hedge against Codex opening the database a moment from now

    The name fallback only applies to the default home. If you explicitly point at
    some other directory (a copy, a fixture, a colleague's exported state), a
    running Codex cannot be touching it, and only real file holders matter.
    """
    found: dict[int, ProcInfo] = {}
    self_pid = os.getpid()
    name_fallback = home is None or is_default_home(home)

    if home is not None:
        for pid, (name, files) in _lsof_holders(codex_state_files(home)).items():
            if pid == self_pid:
                continue  # Our own read-only connection is not a blocker.
            found[pid] = ProcInfo(
                pid=pid,
                name=name,
                reason="holds " + ", ".join(sorted(files)),
            )

    if not name_fallback:
        return [found[pid] for pid in sorted(found)]

    for proc in _all_processes():
        if proc.pid == self_pid or proc.pid in found or not _looks_like_codex(proc):
            continue
        found[proc.pid] = ProcInfo(
            pid=proc.pid,
            name=proc.name,
            command=proc.command,
            rss_bytes=proc.rss_bytes,
            reason="process name matches Codex",
        )
    return [found[pid] for pid in sorted(found)]


def heavy_dev_processes(limit: int = 10) -> list[ProcInfo]:
    """Informational only. This tool never signals or kills anything."""
    interesting = ("node", "esbuild", "vite", "next-server", "tsserver", "webpack")
    hits = [
        proc
        for proc in _all_processes()
        if any(word in proc.name.lower() for word in interesting)
    ]
    hits.sort(key=lambda proc: proc.rss_bytes, reverse=True)
    return hits[:limit]


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            output = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            return True  # Cannot tell: assume alive so we never break a live lock.
        return str(pid) in output
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


# --------------------------------------------------------------------------
# Locking
# --------------------------------------------------------------------------


class LockHeld(RuntimeError):
    def __init__(self, holder: dict | None):
        self.holder = holder or {}
        pid = self.holder.get("pid", "unknown")
        super().__init__(f"another codex-tidy run holds the lock (pid {pid})")


class HomeLock:
    """Exclusive lock so two mutating runs cannot interleave on one Codex home.

    Read-only commands pass ``active=False`` and take no lock at all.
    """

    def __init__(self, home: CodexHome, *, active: bool = True):
        self.path = home.root / LOCK_NAME
        self.active = active
        self.held = False

    def _holder(self) -> dict | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def __enter__(self) -> "HomeLock":
        if not self.active:
            return self
        for _ in range(2):
            try:
                handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                holder = self._holder()
                pid = holder.get("pid") if holder else None
                if isinstance(pid, int) and pid_alive(pid):
                    raise LockHeld(holder) from None
                # Stale lock from a killed run: reclaim it once.
                try:
                    self.path.unlink()
                except OSError:
                    raise LockHeld(holder) from None
                continue
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump({"pid": os.getpid(), "started": utc_iso()}, stream)
            self.held = True
            return self
        raise LockHeld(self._holder())

    def __exit__(self, *_exc_info) -> None:
        if self.held:
            try:
                self.path.unlink()
            except OSError:
                pass
            self.held = False
