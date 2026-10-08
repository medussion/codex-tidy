"""Command line interface.

    codex-tidy scan       observe only, never writes, never locks
    codex-tidy plan       show (or save) exactly which operations would run
    codex-tidy apply      back up, journal, execute, roll back on failure
    codex-tidy restore    replay a journal's undo payloads
    codex-tidy doctor     environment and schema compatibility

``scan`` is the default when no subcommand is given.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import engine, render
from .advice import assess
from .db import probe
from .engine import (
    EXIT_BAD_INPUT,
    EXIT_BLOCKED,
    EXIT_ENVIRONMENT,
    EXIT_OK,
    EnvironmentError_,
    HomeLock,
    LockHeld,
)
from .env import codex_processes, free_bytes, human_bytes, resolve_codex_home
from .journal import find_latest_journal
from .model import Plan, Settings
from .tasks import ALL_TASKS, TASK_NAMES

PROGRAM = "codex-tidy"


def _csv_set(value: str | None) -> frozenset[str]:
    if not value:
        return frozenset()
    names = {item.strip() for item in value.split(",") if item.strip()}
    unknown = names - set(TASK_NAMES)
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown task(s): {', '.join(sorted(unknown))}. Known: {', '.join(TASK_NAMES)}"
        )
    return frozenset(names)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--codex-home", help="Override the Codex home. Defaults to CODEX_HOME or ~/.codex.")
    common.add_argument("--backup-root", help="Where backup/journal folders are created.")
    common.add_argument("--json", action="store_true", dest="as_json", help="Emit machine-readable JSON.")
    common.add_argument(
        "--reveal",
        action="store_true",
        help="Show raw thread ids, titles and paths. Off by default so output is safe to paste.",
    )
    common.add_argument("--only", type=_csv_set, help=f"Run only these tasks: {', '.join(TASK_NAMES)}")
    common.add_argument("--skip", type=_csv_set, help="Skip these tasks.")
    common.add_argument("--limit", type=int, default=10, help="Rows shown per list. Default 10.")

    tuning = argparse.ArgumentParser(add_help=False)
    tuning.add_argument("--session-age-days", type=int, default=10)
    tuning.add_argument(
        "--session-min-mb",
        type=float,
        default=0.0,
        help="Only consider sessions at least this large. Default 0 (no size floor).",
    )
    tuning.add_argument("--worktree-age-days", type=int, default=7)
    tuning.add_argument("--log-rotate-mb", type=int, default=64)
    tuning.add_argument("--title-limit", type=int, default=120)
    tuning.add_argument("--preview-limit", type=int, default=240)
    tuning.add_argument(
        "--repair-thread-metadata",
        action="store_true",
        help="Include a reversible trim of oversized thread title/preview metadata.",
    )
    tuning.add_argument(
        "--archive-orphan-transcripts",
        action="store_true",
        dest="archive_orphan_rollouts",
        help="Move transcript files that no thread row references.",
    )
    tuning.add_argument(
        "--archive-dirty-worktrees",
        action="store_true",
        help="Also archive worktrees with uncommitted changes. Excluded by default.",
    )
    tuning.add_argument(
        "--archive-leftovers",
        action="store_true",
        help="Move interrupted-write temp files and scratch directories aside.",
    )
    tuning.add_argument(
        "--leftover-age-days",
        type=int,
        default=3,
        help="Only treat leftovers older than this as dead. Default 3.",
    )
    tuning.add_argument(
        "--max-archive-gb",
        type=float,
        default=20.0,
        help="Refuse to apply a plan that moves more than this. Default 20 GB.",
    )

    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="Journalled, restore-first maintenance for local Codex state.",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser(
        "scan", parents=[common, tuning], help="Report only. Writes nothing, takes no lock."
    )

    plan_parser = subparsers.add_parser(
        "plan", parents=[common, tuning], help="Show the operations an apply would run."
    )
    plan_parser.add_argument("--out", help="Also write the plan to this JSON file.")

    apply_parser = subparsers.add_parser(
        "apply", parents=[common, tuning], help="Back up, then execute a plan under a journal."
    )
    apply_parser.add_argument("--plan", dest="plan_file", help="Apply a plan saved by `plan --out`.")
    apply_parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    apply_parser.add_argument(
        "--wait-for-exit",
        type=int,
        default=0,
        metavar="SECONDS",
        help="Wait up to N seconds for Codex to close before giving up.",
    )
    apply_parser.add_argument("--compact", action="store_true", help="Also VACUUM the database afterwards.")

    restore_parser = subparsers.add_parser(
        "restore", parents=[common], help="Undo an apply using its journal."
    )
    restore_parser.add_argument("--journal", help="Journal file. Defaults to the most recent one.")
    restore_parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")

    subparsers.add_parser("doctor", parents=[common], help="Environment and schema compatibility.")

    gui_parser = subparsers.add_parser(
        "gui", parents=[common, tuning], help="Open the local browser UI (127.0.0.1 only)."
    )
    gui_parser.add_argument("--port", type=int, default=0, help="Fixed port. Default: pick a free one.")
    gui_parser.add_argument("--no-browser", action="store_true", help="Print the URL instead of opening it.")
    return parser


def settings_from_args(args: argparse.Namespace) -> Settings:
    return Settings(
        reveal=bool(getattr(args, "reveal", False)),
        session_age_days=getattr(args, "session_age_days", 10),
        session_min_mb=getattr(args, "session_min_mb", 0.0),
        worktree_age_days=getattr(args, "worktree_age_days", 7),
        log_rotate_mb=getattr(args, "log_rotate_mb", 64),
        title_limit=getattr(args, "title_limit", 120),
        preview_limit=getattr(args, "preview_limit", 240),
        repair_thread_metadata=getattr(args, "repair_thread_metadata", False),
        archive_orphan_rollouts=getattr(args, "archive_orphan_rollouts", False),
        archive_dirty_worktrees=getattr(args, "archive_dirty_worktrees", False),
        archive_leftovers=getattr(args, "archive_leftovers", False),
        leftover_age_days=getattr(args, "leftover_age_days", 3),
        compact=getattr(args, "compact", False),
        max_archive_gb=getattr(args, "max_archive_gb", 20.0),
        limit=max(1, getattr(args, "limit", 10)),
        only=getattr(args, "only", None) or frozenset(),
        skip=getattr(args, "skip", None) or frozenset(),
        backup_root=Path(args.backup_root).expanduser() if getattr(args, "backup_root", None) else None,
        wait_for_exit_seconds=getattr(args, "wait_for_exit", 0),
    )


def _validate(parser: argparse.ArgumentParser, settings: Settings) -> None:
    if settings.title_limit < 20:
        parser.error("--title-limit must be at least 20")
    if settings.preview_limit < settings.title_limit:
        parser.error("--preview-limit must be greater than or equal to --title-limit")
    if settings.max_archive_gb <= 0:
        parser.error("--max-archive-gb must be positive")
    overlap = settings.only & settings.skip
    if overlap:
        parser.error(f"task(s) in both --only and --skip: {', '.join(sorted(overlap))}")


def _confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print("Refusing to proceed without --yes in a non-interactive shell.", file=sys.stderr)
        return False
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_scan(args: argparse.Namespace, settings: Settings, *, command: str) -> int:
    home = resolve_codex_home(args.codex_home)
    stamp = engine.fresh_stamp()
    backup_root = engine.resolve_backup_root(home, settings, stamp)
    session = engine.open_session(home, settings, writable=False, stamp=stamp, backup_root=backup_root)
    try:
        # Both commands build the operation list: the verdict is partly a function
        # of how much there is to move, so scan cannot skip it and stay honest.
        # Only `plan` prints it.
        plan = engine.scan(session, with_operations=True)
    finally:
        session.close()

    if command == "plan" and getattr(args, "out", None):
        target = Path(args.out).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(plan.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    notes = []
    if command == "scan":
        notes.append("next           codex-tidy plan")
    elif plan.operations:
        notes.append("next           codex-tidy apply")
        if getattr(args, "out", None):
            notes.append(f"plan saved     {args.out}")
    else:
        notes.append("next           nothing to do")

    assessment = assess(plan)
    if args.as_json:
        sys.stdout.write(
            render.render_json(plan, command=command, settings=settings, assessment=assessment)
        )
    else:
        sys.stdout.write(
            render.render_text(
                plan,
                command=command,
                settings=settings,
                extra=notes,
                assessment=assessment,
                show_operations=command != "scan",
            )
        )
    return EXIT_OK


def cmd_apply(args: argparse.Namespace, settings: Settings) -> int:
    home = resolve_codex_home(args.codex_home)
    stamp = engine.fresh_stamp()
    backup_root = engine.resolve_backup_root(home, settings, stamp)

    with HomeLock(home, active=True):
        if args.plan_file:
            raw = json.loads(Path(args.plan_file).expanduser().read_text(encoding="utf-8"))
            plan = Plan.from_dict(raw)
            if plan.codex_home and Path(plan.codex_home) != home.root:
                print(
                    f"Plan targets {plan.codex_home} but this run resolves {home.root}.",
                    file=sys.stderr,
                )
                return EXIT_BAD_INPUT
            plan.backup_root = str(backup_root)
        else:
            session = engine.open_session(
                home, settings, writable=False, stamp=stamp, backup_root=backup_root
            )
            try:
                plan = engine.scan(session, with_operations=True)
            finally:
                session.close()

        problems = engine.preflight(home, plan, settings)
        if problems:
            plan.findings.extend(problems)
            sys.stdout.write(
                render.render_json(plan, command="apply", settings=settings)
                if args.as_json
                else render.render_text(plan, command="apply", settings=settings)
            )
            return EXIT_BLOCKED

        if not plan.operations:
            sys.stdout.write(
                render.render_json(plan, command="apply", settings=settings)
                if args.as_json
                else render.render_text(
                    plan, command="apply", settings=settings, extra=["result         nothing to do"]
                )
            )
            return EXIT_OK

        if not args.as_json:
            sys.stdout.write(render.render_text(plan, command="apply", settings=settings))
        question = (
            f"Apply {len(plan.operations)} operation(s), moving "
            f"{human_bytes(plan.reclaimable_bytes)}? Backups and an undo journal go to {backup_root}."
        )
        if not _confirm(question, args.yes):
            return EXIT_OK

        result = engine.apply_plan(home, plan, settings)

    notes = [
        f"backup         {result.backup_root}",
        f"journal        {result.journal_path}",
        f"completed      {result.completed}/{len(plan.operations)}",
    ]
    if result.failed:
        notes.append(f"failed         {result.error}")
        notes.append(f"rolled back    {result.rolled_back} operation(s)")
    else:
        notes.append(f"undo with      codex-tidy restore --journal {result.journal_path}")
    notes.extend(result.notes)

    if args.as_json:
        sys.stdout.write(
            render.render_json(
                plan,
                command="apply",
                settings=settings,
                extra={
                    "apply": {
                        "completed": result.completed,
                        "failed": result.failed,
                        "rolled_back": result.rolled_back,
                        "error": result.error,
                        "backup_root": str(result.backup_root),
                        "journal": str(result.journal_path),
                        "notes": result.notes,
                    }
                },
            )
        )
    else:
        sys.stdout.write("\n".join(f"      {note}" for note in notes) + "\n")
    return result.exit_code


def cmd_restore(args: argparse.Namespace, settings: Settings) -> int:
    home = resolve_codex_home(args.codex_home)
    if args.journal:
        journal_path = Path(args.journal).expanduser()
    else:
        from .env import default_backup_root

        base = settings.backup_root or default_backup_root(home)
        found = find_latest_journal(base)
        if found is None:
            print(f"No journal found under {base}.", file=sys.stderr)
            return EXIT_BAD_INPUT
        journal_path = found

    if not journal_path.is_file():
        print(f"Journal not found: {journal_path}", file=sys.stderr)
        return EXIT_BAD_INPUT

    running = codex_processes(home)
    if running:
        print(
            f"Codex is running ({len(running)} process(es)). Close it before restoring.",
            file=sys.stderr,
        )
        return EXIT_BLOCKED

    if not _confirm(f"Undo the operations recorded in {journal_path}?", args.yes):
        return EXIT_OK

    with HomeLock(home, active=True):
        result = engine.restore(home, journal_path)

    if args.as_json:
        sys.stdout.write(
            json.dumps(
                {
                    "command": "restore",
                    "journal": str(result.journal_path),
                    "undone": result.undone,
                    "failed": result.failed,
                    "notes": result.notes,
                },
                indent=2,
            )
            + "\n"
        )
    else:
        print(f"codex-tidy restore\n  journal      {result.journal_path}")
        print(f"  undone       {result.undone}")
        print(f"  failed       {result.failed}")
        for note in result.notes:
            print(f"  note         {note}")
    return result.exit_code


def cmd_doctor(args: argparse.Namespace, settings: Settings) -> int:
    home = resolve_codex_home(args.codex_home)
    report: dict = {
        "python": sys.version.split()[0],
        "codex_home": str(home.root),
        "codex_home_exists": home.exists(),
        "state_db": str(home.state_db) if home.state_db.is_file() else None,
        "log_databases": len(home.log_dbs()),
        "config_toml": home.config_toml.is_file(),
        "codex_running": len(codex_processes(home)),
        "free_bytes": free_bytes(home.root),
        "tasks": {},
        "threads_columns": [],
    }

    if not home.exists():
        report["error"] = "Codex home not found"
    else:
        stamp = engine.fresh_stamp()
        session = engine.open_session(
            home,
            settings,
            writable=False,
            stamp=stamp,
            backup_root=engine.resolve_backup_root(home, settings, stamp),
        )
        try:
            if session.conn is not None:
                report["threads_columns"] = sorted(probe(session.conn).cols("threads"))
            for task in ALL_TASKS:
                reason = task.unavailable(session.ctx)
                report["tasks"][task.name] = reason or "available"
        finally:
            session.close()

    if args.as_json:
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
        return EXIT_OK if home.exists() else EXIT_ENVIRONMENT

    print("codex-tidy doctor")
    print(f"  python            {report['python']}")
    print(f"  codex home        {report['codex_home']}  {'ok' if report['codex_home_exists'] else 'MISSING'}")
    print(f"  state database    {'found' if report['state_db'] else 'missing'}")
    print(f"  log databases     {report['log_databases']}")
    print(f"  config.toml       {'found' if report['config_toml'] else 'missing'}")
    print(f"  codex running     {report['codex_running']} process(es)")
    print(f"  free space        {human_bytes(report['free_bytes'])}")
    if report["threads_columns"]:
        print(f"  threads columns   {', '.join(report['threads_columns'])}")
    print("  tasks")
    for name, state in report["tasks"].items():
        print(f"      {name:<14} {state}")
    return EXIT_OK if home.exists() else EXIT_ENVIRONMENT


def cmd_gui(args: argparse.Namespace, settings: Settings) -> int:
    home = resolve_codex_home(args.codex_home)
    if not home.exists():
        print(f"Codex home not found: {home.root}", file=sys.stderr)
        return EXIT_ENVIRONMENT
    from .webui import serve

    return serve(home, settings, port=args.port, open_browser=not args.no_browser)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    if args.command is None:
        args = parser.parse_args(["scan", *(argv if argv is not None else sys.argv[1:])])

    settings = settings_from_args(args)
    _validate(parser, settings)

    try:
        if args.command == "scan":
            return cmd_scan(args, settings, command="scan")
        if args.command == "plan":
            return cmd_scan(args, settings, command="plan")
        if args.command == "apply":
            return cmd_apply(args, settings)
        if args.command == "restore":
            return cmd_restore(args, settings)
        if args.command == "doctor":
            return cmd_doctor(args, settings)
        if args.command == "gui":
            return cmd_gui(args, settings)
    except EnvironmentError_ as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ENVIRONMENT
    except LockHeld as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_BLOCKED
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_BLOCKED

    parser.error(f"unknown command: {args.command}")
    return EXIT_BAD_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
