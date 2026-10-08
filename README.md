# codex-tidy

Local Codex state grows: session transcripts, log databases, stale worktrees,
oversized thread metadata, half-written temp files. `codex-tidy` finds what has
grown and moves it out of the way.

**The guarantee:** nothing is ever deleted, every change is written to a journal
*before* it happens, and any run can be undone with one command.

```
codex-tidy gui                               # browser UI: verdict, guidance, one Clean button
codex-tidy scan                              # read-only, writes nothing
codex-tidy plan                              # exactly what would change
codex-tidy apply --yes                       # backup, journal, execute
codex-tidy restore --yes                     # put it all back
```

## Install

Requires Python 3.11+ and nothing else. No third-party dependencies, on purpose:
there is no supply chain to audit before an internal rollout, and nothing to
install on a locked-down machine.

```bash
pip install --user /path/to/codex-tidy
```

Or run it straight from a checkout, no install at all:

```bash
PYTHONPATH=src python3 -m codex_tidy scan
```

## Browser UI

```bash
codex-tidy gui
```

Starts a local server, prints a URL, and opens it. The page answers three
questions: **is it worth cleaning yet**, **what exactly would be moved**, and
**what should I do first**. Untick anything you want kept; one button does the
rest, and another undoes it.

The page lists parent tasks as continuing-project candidates. Select only the
projects you intend to resume and press **Handoff 생성**. Handoffs are generated locally; no
transcript text is sent to an API. A handoff captures user requests, final
answers, compacted context summaries, commands, failures and file paths, then
cross-checks the session's working directory with read-only Git commands.

Each handoff stores the transcript size and modification time beside the
document. If that transcript changes afterwards, the handoff becomes stale.
The Clean button requires a fresh handoff only for projects selected as work to
continue. Completed or unwanted sessions can be archived without generating a
handoff for each one. The final confirmation reports both counts explicitly.
Archiving moves data into the backup/journal structure and remains reversible;
it is not permanent deletion.

The **세션 아카이브 영구 삭제** section lists archived transcripts by readable
title, date, size, project, and whether a database row still points to them.
Nothing is selected by default. Permanent deletion requires selecting exact
items and typing `영구 삭제`; it removes the JSONL and its linked archived thread
row and cannot be undone. Orphan archive files are listed too, without deleting
an unrelated database row.

Because this is a token-protected localhost UI, it shows normalized real session
titles by default. Turn **실제 이름 보기** off before sharing a screenshot. The
list replaces history-sized approval wrappers with the underlying user request
when possible and shows date, project, handoff state, and a conservative cleanup
label. **정리 가능** means a fresh handoff exists; labels such as **확인 필요**
are evidence prompts, not a claim that the conversation is disposable. CLI and
JSON reports remain pseudonymous unless `--reveal` is passed.

Small transcripts are streamed in full with bounded records. For transcripts
over 256 MB, the extractor reads the beginning and the most recent 256 MB,
including the latest compacted summary when present. Repeated
`compacted/replacement_history`, reasoning payloads and long tool output are not
copied into the handoff. Any sampling, malformed records or missing Git evidence
is stated in the generated document rather than hidden.

The UI text is Korean; the CLI, API and this README are English. Both render the
same language-neutral codes from `advice.py`, so the two can never drift apart —
there is a test asserting every task has UI copy.

**Verdict.** `now` when any single signal is severe or more than 1 GB would move,
`soon` when something is worth watching, `fine` otherwise. Each signal shows its
measured number against the threshold it was judged by, and the thresholds are
named constants in [`advice.py`](src/codex_tidy/advice.py) with the reasoning
attached — disagree and move them.

**It is locked down, because this process can move a developer's files:**

- bound to `127.0.0.1`, so nothing off-box can reach it
- a random per-run token, presented once in the URL we open ourselves
- swapped immediately for an `HttpOnly`, `SameSite=Strict` cookie, so the token
  does not linger in browser history and another tab cannot ride along
- mutating calls need a custom header, which a cross-origin form post cannot set
- the `Host` header must be a loopback name, blocking DNS rebinding
- settings from the browser are whitelisted, type-coerced and clamped; unknown
  keys are dropped
- one lock across mutating requests, so a second Clean cannot start mid-run
- a strict CSP and a fully inlined page: no fonts, scripts or images are fetched

Everything the UI does goes through the same engine, journal and safety checks as
the CLI. There is no path that skips them: if Codex is running, the Clean button
says so and stays disabled.

## Start here

`scan` is read-only. It opens the database with SQLite's `mode=ro`, takes no lock,
creates no files, and there is a test asserting the Codex home is byte-identical
afterwards. Run it as many times as you like.

```bash
codex-tidy scan
```

Output is pseudonymous by default — thread ids, titles and paths are replaced with
per-run tags, so a report can be pasted into a ticket. Add `--reveal` when you
need the real values.

When you want to see the exact operation list:

```bash
codex-tidy plan --out plan.json
```

Then apply it. The plan is rebuilt at apply time unless you pass `--plan plan.json`.

```bash
codex-tidy apply --yes
```

## What it looks at

| Task | Default behaviour | Opt-in |
| --- | --- | --- |
| `environment` | Reports sizes, disk headroom, every Codex database, processes | never changes anything |
| `sessions` | Archives active transcripts older than `--session-age-days` (10) | `--session-min-mb` to add a size floor |
| `thread-meta` | Reports oversized thread title / preview metadata | `--repair-thread-metadata` to trim it |
| `integrity` | Reports transcripts no row references, and rows whose transcript is gone | `--archive-orphan-transcripts` |
| `worktrees` | Archives worktrees older than `--worktree-age-days` (7), **skipping dirty ones** | `--archive-dirty-worktrees` |
| `logs` | Rotates log databases above `--log-rotate-mb` (64) | — |
| `leftovers` | Reports interrupted-write temp files and scratch dirs | `--archive-leftovers` |
| `config` | Prunes dead project entries from `config.toml` | — |
| `winpaths` | Normalises `\\?\`-prefixed paths stored in the database | — |

Narrow any run with `--only sessions,logs` or `--skip config`.

### Thread metadata

Some Codex builds store the entire first user prompt as both the thread title and
the sidebar preview. Once those fields reach tens of thousands of characters the
thread list gets slow to render before any chat is opened. `scan` reports the
sizes; trimming is opt-in, touches only the display strings, and never touches the
transcript.

### Worktrees

A dirty worktree is excluded from the plan by default. Archiving is only a move,
but a developer who cannot find their uncommitted work has effectively lost it.

## Safety model

This is the part worth reading before you hand the tool to a team.

**Backup first.** Before the first operation runs, `apply` copies `config.toml`,
`.codex-global-state.json`, `session_index.jsonl`, `history.jsonl`, and the
`memories` / `skills` / `rules` / `plugins` / `automations` folders into a stamped
backup directory, plus a consistent copy of `state_5.sqlite` taken through
SQLite's backup API.

**Journal before acting.** Every operation is appended to `journal.jsonl` before
it runs, and its undo payload is appended after it succeeds — flushed and fsynced
each time. If the process is killed mid-run, the journal still describes what was
attempted and what completed.

**Every operation commits on its own.** One large transaction would let the
database roll back while filesystem moves stayed done. That divergence between
disk and database is the exact failure this tool exists to prevent, so operations
are individually durable and individually reversible instead.

**Undo payloads are read from live state.** The reverse of a database update is
built by reading the row immediately before writing it, not from a value computed
at plan time. Anything that changed in between is still restored correctly.

**Restore is a replay, not separate code.** Undo payloads *are* operations, run
back through the same executor in reverse order. There is no second rollback code
path that could drift out of step.

**Refusals before any byte moves.** `apply` stops if Codex is running, if another
`codex-tidy` holds the lock, if the plan would move more than `--max-archive-gb`
(20 GB), or if the backup volume lacks room.

**Is Codex running?** Answered by asking who actually holds the Codex databases
open (`lsof`), not by guessing from process names. The report names the process
and the files it holds. Name matching is only the fallback where `lsof` is
unavailable.

**config.toml is treated as dangerous.** The rewrite is parsed back with `tomllib`
and compared against the original. If it would not parse, or would change anything
outside `[projects]`, the operation is dropped from the plan and the file is left
alone.

### What is not reversible

`restore` undoes moves, database updates and file rewrites. One thing it does not
undo: the append to `session_index.jsonl` that mirrors a trimmed title into
Codex's own rename log. It is append-only and harmless to leave. `plan` and
`apply` both say so explicitly when such an operation is included.

## Supported

- **Verified on:** macOS, Python 3.11, against a real `~/.codex` and synthetic fixtures.
- **Implemented for Windows** (PowerShell process listing, extended-length path
  normalisation) but not verified on Windows in this repository.
- **Codex schema:** every task declares the columns it needs. If Codex ships a
  schema this tool does not recognise, the affected task is skipped with a stated
  reason rather than failing part-way through. `codex-tidy doctor` shows exactly
  which tasks are available against your installation.
- **Databases modified:** only `state_5.sqlite`, and `logs_2.sqlite` is moved
  during rotation. `memories_1.sqlite`, `goals_1.sqlite` and anything under
  `sqlite/` are reported for size and never touched.

Run `codex-tidy doctor` first on any machine you have not used it on:

```bash
codex-tidy doctor
```

## Privacy

Reports are pseudonymous by default. Thread ids become `thread:ab12cd`, titles
become `<1234 chars>`, and paths keep only their generic structure under the
Codex home. Pseudonyms are stable within a run and random across runs, so two
reports cannot be correlated after the fact. `--reveal` opts out.

Backup folders are a different matter: they contain real Codex metadata by
design. Keep them local, and delete them by hand when you no longer need the
undo. `codex-tidy` reports how many backup runs have accumulated but never
removes one.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | success, or nothing to do |
| 2 | Codex home not found |
| 3 | blocked: Codex running, lock held, over cap, or low disk |
| 4 | apply failed and was rolled back |
| 5 | bad plan or journal file |

`--json` on any command emits a stable structure with `findings`, `operations`
and `summary`, suitable for a scheduled report.

## Recurring maintenance

Schedule `scan`, never `apply`. An automated run cannot know whether you still
need the conversations it is about to archive. Have it report and let a person
decide.

```bash
codex-tidy scan --json > "codex-tidy-$(date +%F).json"
```

## Development

```bash
python3 -m unittest discover -s tests -t .
```

The suite builds synthetic Codex homes and covers, among others: that `scan`
leaves the home byte-identical, that a mid-apply failure rolls everything back,
that every task's changes survive an apply/restore round trip, that a dirty
worktree is not archived, that a malformed `config.toml` is left untouched, that
a stale lock is reclaimed while a live one blocks, that a WAL database with no
sidecar files is still readable, that unticking an item in the UI drops both
halves of its change, and that the UI refuses every request missing any one of
its access controls.

## Layout

```
src/codex_tidy/
  cli.py         subcommands and flags
  engine.py      scan / plan / apply / restore orchestration
  advice.py      the verdict: language-neutral signals and guidance codes
  ops.py         the six primitive operations, each self-inverting
  journal.py     write-ahead journal and undo stack
  db.py          connections and the schema contract
  privacy.py     redaction
  env.py         Codex home layout, processes, locking, disk
  render.py      text and JSON output (English copy for advice codes)
  tasks/         one module per concern
  webui/         local server + the single-page Korean UI
```

Tasks only observe and propose. They never write. The executor is the only code
that mutates anything, which is also the only code the journal has to describe.

## License

MIT. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
