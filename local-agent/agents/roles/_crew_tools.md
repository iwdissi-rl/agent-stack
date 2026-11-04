# Crew tool reference

These tools come from the `crew` MCP server (they appear in your tool list with an `mcp__crew__`
prefix). Every call is mirrored on the owner's dashboard.

## Mission bookkeeping

- `plan_set(steps)` — set (replace) the mission plan. `steps` is a list of
  `{"id": "s1", "title": "short imperative", "detail": "optional one-liner"}`. Call it once you have
  reconnoitred, before you start executing; call it again if the plan changes materially. Keep it to
  3–12 steps; the owner reads it as the table of contents of what you are doing.
- `plan_update(step_id, status, note="")` — `status` ∈ `active | done | blocked | skipped | pending`.
  Mark a step `active` when you start it and `done` when you have **evidence** it is done. Timing per
  step is recorded automatically from these transitions.
- `progress(message)` — one-line status update for the timeline ("tests red: 3 failures in
  parser, delegating to debugger"). Use it at every meaningful transition, not every tool call.
- `clock()` — elapsed mission time, remaining run budget, turns used, how many model slots are
  free, how many crew members are running or queued.
- Any crew-tool result may carry **`owner_notice`** (a message the owner posted while you run, e.g. "search is
  back", "stop touching module X"). Read it, act on it, acknowledge it in your next `progress` note.
- `deliver(summary, paths, version="", how_to_run="")` — lead only. Hands the owner a **usable** snapshot:
  checkpoints, copies `paths` (files/directories; include the README with exact run/test commands) into
  `/workspace/outbox/deliveries/<mission>/<time>-vN/` with a DELIVERY.md, zips it (download card on the
  dashboard) and restarts the cadence countdown. When the mission has a cadence, `clock`/`wait`/`status`/
  `progress` results carry a `deliverable` field (`next_due_in_s`, `overdue`, `note`) — obey it.

## Safety rails, review and resumption

- `checkpoint(label)` — snapshot `/workspace` into the mission's shadow git (the HUD also does this
  at mission start and before every spawn). Checkpoint before risky edits and after every green run.
- `checkpoints()` — list them. `diff(from_checkpoint, to_checkpoint="", paths=[])` — unified diff;
  give a reviewer `diff(<mission start id>)` so it reviews the actual change against the request.
- `rollback(checkpoint, paths=[])` — restore files (or everything) to a checkpoint; a pre-rollback
  checkpoint is taken first. Prefer it to hand-reverting a member's bad work.
- `state_save(summary)` / `state_get()` — compaction and handoff: save goal, decisions, files, done /
  left, open problems, key commands, last good checkpoint. Saved state is prepended automatically to
  the next follow-up run of this mission. Save at ~70% budget, before long waits, and before the
  final report of any long mission (skill `mission-handoff`).
- `repo_map(path, max_chars, focus)` — symbol-level map of a directory (files, classes, functions
  with line numbers). Read it before reading files; then `search_files` and `read_file` with
  offset/limit (skill `repo-navigation`).

## Delegation

- `roles()` — the roster with each role's purpose, default turn limit and when to use it.
- `spawn(role, task, context="", files=[], label="", max_turns=0)` — start a crew member.
  Returns immediately with an `agent_id`; the member runs concurrently with you and with other
  members (up to the free slots; beyond that it queues and starts automatically).
  - `task`: the deliverable and the acceptance criteria. Imperative, specific, testable.
  - `context`: everything the member needs, because it knows **nothing** about this conversation:
    relevant paths, the decisions you already made, the interfaces it must respect, what it must
    not touch, what others are doing in parallel.
  - `files`: workspace paths it should start from (they are listed in its brief).
  - `label`: a short name for the dashboard ("parser tests"). Default: the role name.
  - `max_turns`: override the role default only when you know the task is tiny or huge.
- `status(agent_ids=[])` — live table of your crew members: state, elapsed, turns, tool calls,
  last tool used, tokens. Empty list = all of them.
- `wait(agent_ids=[], timeout_s=300)` — block until the listed members (default: all running) have
  finished or the timeout passes (max 300 s; call it alone, never in the same batch as other tools).
  A timeout is normal, not an error: it returns `timed_out` and the ids still running — wait again. Returns each member's state and, for finished ones, their full
  final report. Waiting costs you nothing (you generate no tokens while waiting; your slot goes to
  them). Call it in a loop with a shorter timeout if you want to interleave your own work.
- `result(agent_id)` — the full final report of a finished member (also any files it listed).
- `cancel(agent_id)` — stop a member that is stuck, redundant, or heading the wrong way.

Members cannot spawn members of their own. Only you hold the plan.

## Background tasks (outlive a turn, visible on the dashboard)

- `bg_start(name, command, timeout_s=3600)` — run a shell command in a fresh container of the
  sandbox image on the same `/workspace` (internet via the proxy). Returns a `task_id`. Use it for
  anything that takes more than a minute or two: full test suites, builds, installs, data jobs,
  scrapers, servers under test. It is **not** your own sandbox container: packages you installed
  there are absent. Background containers share their own persistent home (pytest is preinstalled;
  `pip install --user X` from one background task is there for the next), so prefix the command
  with what it needs, e.g. `pip install --user -q pytest requests && python -m pytest -q`, or use a
  project `.venv` under `/workspace`.
- `bg_status(task_id="")` — one task or all of them: running / exited, exit code, elapsed, the last
  lines of output.
- `bg_logs(task_id, tail=200)` — more output.
- `bg_stop(task_id)` — kill it.

Background tasks are *not* killed when your turn ends or when a member finishes; stop what you
start, or leave it running deliberately and say so in the report.

## Claude Code delegation (`claude_*`)

The owner's machine has **Claude Code** logged in (Anthropic's agentic coding CLI; far stronger at coding and
reasoning than the local model). You may hand it **one well-defined piece of work at a time** — the central,
hardest part of a mission: a core algorithm, a subtle bug, a large refactor, a design or security review.
**It only sees the files you list.** Each run gets a private copy of exactly the paths in `files` (files or
whole directories, workspace-relative; a path that doesn't exist yet = a file it should create), nothing else
from `/workspace`. List everything it needs — the module, its tests, the config it imports — or it will work
blind. Those files are screened like the brief: a file with a key/token/password in it, or a `.env`/`*.pem`
style secrets file, blocks the run (remove the secret first); max ~400 files / 8 MB, caches and `.git` skipped.
When it finishes, its changes are merged back into `/workspace` automatically. If you edited the same file
meanwhile, yours is kept and Claude's version lands next to it as `<file>.claude-<task_id>` (see `merge` in
the result) — reconcile it yourself.

- `claude_run(task, context="", files=[], model="opus", effort="high", mode="edit", label="", timeout_s=0, max_turns=0)`
  — returns at once with a `task_id`, the gate's `redactions`/`warnings`, and `brief_sent` (exactly what
  Claude received). `claude_wait(task_id)` for the result (max 300 s per call; a timeout is normal, call again).
- **Paths:** Claude works in a *staged copy* that contains only what you list in `files` (directories are allowed —
  list the directory it must see or edit) plus whatever it creates; its changes are merged back. Write paths
  workspace-relative (`src/app.py`), never `/workspace/...` (the gate rewrites those, but Claude cannot reach `/workspace`).
- `claude_check(...)` — the same gate as a free dry-run: nothing is sent; use it to fix a refused brief.
- `claude_status`, `claude_result(task_id)`, `claude_stop(task_id)`.
- `model`: **`opus`** for most coding and implementation work; **`fable`** for the deepest reasoning —
  architecture, hard algorithms, subtle correctness, maths. `effort`: `low` (mechanical), `medium`, `high`
  (default), `xhigh` (hard), `max` (hardest; slow). Match the *difficulty*, not the size of the task.
- `mode`: `edit` (it edits its copy of the listed files and runs tests inside a sandbox) or `readonly` (analysis,
  design, review — no file changes). Readonly is the safe choice for "tell me how" questions.

**The outbound gate.** Every request is rewritten into a fixed-shape brief and screened before it leaves:
secrets and personal data are redacted; words like "unfiltered" and profanity are removed; any phrasing that
steers or tests the assistant, talks about its policies or account bans, asks for permission-bypass flags, or
describes platform abuse / intrusion / harm is **blocked**; dual-use words (scrape, exploit, bypass, bot, …)
require one sentence stating the legitimate purpose and the local judge's approval. **A block means nothing
was sent** — read `blocked_by`, rephrase in plain engineering language, and retry. Never argue with the gate,
never try to smuggle wording past it, never put the owner's keys, emails or other people's details in a brief.

**Writing a brief that works.** It is a ticket for a strong contractor who knows nothing about this
conversation: the deliverable and acceptance criteria in `task`; paths, interfaces to respect, decisions made,
what not to touch in `context`; the starting files in `files`. One deliverable per run. Ask for tests.

**If Claude refuses.** A run can come back `refused` ("safeguards flagged"). That is Claude's own filter,
not the gate. Do **not** resend the same brief and do not try to reword around it: rewrite the task as a
plainer, smaller engineering ticket at most once, or do the work locally. After a few refusals the HUD pauses
delegation until the owner clears it — say so in your report. Every brief that goes out is visible to the
owner on the dashboard, so write only what you would be happy for the owner to read.

**After it returns.** Treat its report like a member report: read `changed_files`, `diff(<before>, <after>)`
(the ids are in the result), run the project's checks yourself, and only then mark the step done. If it went
wrong, `rollback(<checkpoint before>)`. Limits (runs per hour, concurrent runs) exist; batch related work
into one fuller brief rather than many small ones.

## Desktop, toolbox and Remote Control
- **Toolbox first.** `tb find <word>` before installing anything; install only via `tb install|pip|npm|env` (persists for
  every future chat); `tb register` anything you got working (a tool, a launch procedure, a test harness) with the exact
  run command and the gotchas. `tb show <name>` gives the recipe an earlier chat wrote. Uses are counted (`tb usage`; the
  owner sees them on the dashboard's Tools page), so a tool nobody uses is visible — register only things that work.
- **`host_run(tool, args, timeout_s, wait_s)`** runs a script from `/mnt/data/la-toolbox/host/` on the owner's real
  desktop (screen, GPU, Lunar Client). Existing: `lunar` (open Lunar with test mods), `desktop` (xdotool click/type/key
  for X11 windows), `mc-gpu` (mc-test on the GPU), `selftest`. New host scripts: write them from the sandbox, `chmod +x`,
  `tb register <name> --where host`. Long runs return a `run_id` → `host_run_status(run_id)`.
- **`screenshot(region)`** captures the owner's live screen into /workspace/outbox/screens/ → `vision_analyze` it.
  Typical GUI loop: host_run(lunar, wait_s=5) → wait ~40 s → screenshot → find the button → host_run(desktop, click X Y)
  → screenshot again. Coordinates are screen pixels of the full-size screenshot.
- **Minecraft mod tests:** `mc-test <loader:version> <jar>` in the sandbox (headless, software GL, slow first run while
  it downloads into the shared cache) or `host_run(mc-gpu, ...)` on the GPU. Exit 0 = title screen reached.
- **`remote_control(action)`** — Claude Code Remote Control ('home server', bypassPermissions). `status` any time;
  `start` if it is off; `restart` only when status says unhealthy (force=true only on the owner's explicit request).
  Logged out → tell the owner to run `claude auth login` on the desktop. Never run `claude auth login` in the sandbox.
