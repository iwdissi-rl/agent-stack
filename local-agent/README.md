# Local agent stack — `/mnt/data/local-agent`

A fully local, uncensored research assistant: an abliterated Qwen3.8 model on llama.cpp, driven by
Nous Research's **Hermes Agent**. It has self-hosted search, page extraction, browser automation,
RAG over your documents, a sandboxed shell, and a custom skill library. No paid or cloud APIs.

```
            ┌──────────────── Hermes Agent (host; HERMES_HOME=hermes-home/) ────────────────┐
 you ─────▶ │ web_search ─▶ SearXNG :8888 ─┐                                                 │
            │ web_extract ─▶ Crawl4AI :11235├─▶ Mihomo proxy 127.0.0.1:7897 ─▶ internet      │
            │ mcp__rag__* ─▶ rag_server.py ─▶ Qdrant :6333 + Qwen3-Embedding :8182 (CPU)      │
            │ terminal / file / execute_code / browser_* ─▶ Docker sandbox (hermes-sandbox)   │
            │                         └─ egress only via relay 172.17.0.1:7898 ─▶ proxy       │
            └─── LLM: llama-server :8181 (OpenAI-compatible, alias "local") on the RTX 4090 D ─┘
```

## Start / stop

```bash
cd /mnt/data/local-agent
./agent.sh start [profile]  # start/enable everything (systemd user units; survives reboots)
./agent.sh chat             # open Hermes (TUI) in workspace/
./agent.sh status           # health of every component + GPU memory
./agent.sh stop             # stop the model stack (the web HUD stays up)
./agent.sh use <profile>    # switch model + restart
./agent.sh logs model       # follow a log (model|embed|hud|relay|watchdog)
```

One-shot use (scripts, cron): `HERMES_HOME=$PWD/hermes-home hermes-agent/.venv/bin/hermes chat -q "…" --oneshot`

The model server loads in about 15 s for the 27B. Flash-Next takes about 15 s when its files are in page cache and a few minutes from a cold disk. The first
chat after a start also builds the Docker sandbox container (a few seconds).

## Web HUD — https://EXAMPLE_HOST/llm/

A password-protected remote control in the same dark workbench style as `EXAMPLE_HOST/research`:

- **Overview**: model server state, live generation tok/s sparkline, prompt-processing progress,
  GPU/CPU/RAM, every service's health, recent tasks, and a quick-command box.
- **Console**: send commands to the agent (Hermes, same tools and sandbox as the CLI) and watch
  each tool call and result stream in live. Pick a **mode** (chat / coding / research / data / one specialist);
  crew modes run a lead that spawns specialist agents in parallel — see *Crew mode* below. **Attach images** (the clip, paste, or drag-and-drop;
  up to 6 per message, all selected at once) — the local model is vision-capable and sees them
  natively. Chats live in a **sidebar**: collapse/expand it with the ☰ button (desktop remembers
  your choice; on a phone it's a slide-over drawer). You can preload skills, set
  max turns, continue a thread (follow-ups resume the same Hermes session), and cancel. Up to `PARALLEL`
  agents run at once (llama-server slots); bench/eval jobs run alone.
- **Model**: switch profiles, tune `CTX NGL N_CPU_MOE THREADS BATCH UBATCH KV_TYPE TEMP TOP_P TOP_K
  MIN_P PARALLEL` (validated ranges), save and restart, and benchmark. Shows a live server log.
- **Evals**: run the full suite or chosen categories; see every saved run with per-task scores.
- **Logs**: tail model, embeddings, HUD, relay and watchdog logs.

The layout is **mobile-first**: on a phone it's a single column with a bottom tab bar (Home /
Agent / Model / Evals / Logs) and a chat-style composer that docks above the keyboard; on desktop
it's the left icon rail + cards. Same dark `/research` palette throughout.

Images are shrunk **on your device** to ≤1280 px JPEG before upload (the tunnel only carries
~35 KB/s inbound, so full-size phone photos used to time out at Cloudflare's 100 s limit; this also
converts iPhone HEIC). One uploads at a time, with a progress bar and one automatic retry; tap a
failed thumbnail to retry. The server re-encodes them again (EXIF/GPS stripped, HEIC accepted as a
fallback, max 25 MB), and stores them in `workspace/uploads/` (= `/workspace/uploads` in the sandbox).
They are owner-only.

**Local Hermes patches** (in `patches/`; after any `hermes update`, reapply each with
`cd hermes-agent && git apply ../patches/<name>.patch`):
- `hermes-repeatable-image.patch` makes `hermes chat --image` repeatable so multi-image messages go to the model
  natively (without it, extra images fall back to slow `vision_analyze` tool calls).
- `hermes-effort-extra-body.patch` (2026-10-31): the five `local-<effort>` providers share one URL and model, and
  Hermes matched `extra_body` by URL, so **every effort level got `local-fast`'s thinking-off settings**. The patch
  matches the requested entry by name. Since then medium/high/xhigh/max really think (and are slower than before).
- `hermes-thinking-passthrough.patch` (2026-10-31): `--format stream-json` also emits `{"type":"thinking"}` deltas
  (the model's reasoning, which the protocol used to drop); the HUD shows them collapsed.

Login is user `owner`. To change the password (this also signs out all sessions):

```bash
cd /mnt/data/local-agent/hud && read -rs P && printf '%s' "$P" | .venv/bin/python set_password.py owner
```

Security:
- **Password storage:** an argon2id hash only (`hud/data/auth.json`, mode 600).
- **Login throttling:** a lockout after 10 failures per IP per 15 min or 30 failures globally per
  hour, plus an nginx limit of 5 logins/min with a burst of 10.
- **Cookies:** HttpOnly + Secure + SameSite=Strict, scoped to `/llm/`, with 12 h idle and 7 day
  absolute expiry.
- **Writes:** need a CSRF token and an exact origin.
- **Headers:** a strict CSP (no inline script or style).
- **Commands:** there's no shell endpoint. The HUD can only start Hermes (tools sandboxed),
  benchmarks or evals, and edit whitelisted profile keys (`EXTRA_ARGS`/`MODEL` are disk-only).
- **Audit log:** `hud/data/hud.db`, table `audit`.

Plumbing: Cloudflare Tunnel (ingress `EXAMPLE_HOST` path `^/llm` → `http://localhost:8080`; the nginx `www` server block serves /llm too)
→ host nginx loopback server (`/etc/nginx/snippets/local-agent-locations.conf`, zones in
`/etc/nginx/conf.d/local-agent-http.conf`, one `include` added to `sites-enabled/iwd-quant-loopback.conf`)
→ `127.0.0.1:8190` (`hud/app.py`, FastAPI).

## Glass theme + Claude-style chat (2026-11-03)

`hud/static/app.css` ends with a "GLASS THEME" block that restyles everything on top of the older rules: deep dark
background with soft colour washes, translucent frosted surfaces (`backdrop-filter` only on bars, sheets, the drawer and
the composer so long pages stay smooth), 20–30 px radii, a terracotta accent for primary actions, strong ease-out curves.
The chat screen follows the Claude app: on phones the generic top bar is hidden (`body.on-console`) and the chat header
floats over the scrolling transcript (back chevron = chat list, centred title + subtitle, a spinner ring while the agent
runs, a "⋯" button that opens the action sheet `#chat-sheet` with status / mode / tokens / context meter, New chat, All
chats, Mission, Compact, Stop, profile, sign-out); the assistant is plain prose, your messages are a rounded bubble, and
the composer is a floating glass card (text on top; "+", mode pill, effort pill, options and the round send button below)
that overlays the bottom of the transcript — `--composer-h` / `--dock-h` are kept in sync by a ResizeObserver so the
transcript's padding always clears it. The chat list is a full-screen glass sheet from the left (close ✕, profile, New
chat). Options open as a bottom sheet with a handle. Desktop keeps its layout with the same surfaces. Screenshot harness
without touching real chats: `hud/tests_mock_screens.py` (Playwright with every `**/api/**` route mocked; run it with `tbpy`).

## Station carry-over (2026-11-04)

The EXAMPLE_HOST landing ("the station", project `/mnt/data/iwd-landing`) and the HUD share one design language. In the HUD:
`hud/static/fonts/` (Barlow Condensed, JetBrains Mono), `hud/static/img/station-*.webp` (frames from the Blender scene;
`station-bg` is the blurred app backdrop, `station-llm` the login backdrop), the "STATION CARRY-OVER" block at the end of
`app.css` (sheet kickers above every page title, condensed uppercase h1, login styled as the model-room access badge,
crosshair empty states, ember accent). The landing's keycard sign-in posts to `/llm/api/login` same-origin and lands in
`/llm/#console`; the HUD login page is the fallback path and shows the same room.

## Crew mode — a lead agent that runs specialist agents (2026-10-13)

Besides plain chat, the console has **modes**: `coding`, `research` and `data` start a **mission lead**
(the `orchestrator` role, a ~4,000-word doctrine in `agents/roles/orchestrator.md` plus a mode brief from
`agents/modes/`). The lead can **spawn crew members**: more Hermes processes, same local model, each with a
specialist system prompt. Up to `PARALLEL` (profile key, 8 for Flash-Next) agents generate at once on one GPU
because llama-server runs that many slots on a **unified KV pool** (`-np 8 -kvu`; same VRAM as one slot; `UBATCH` must
be ≤ 2048 above 4 slots). **The pool is shared:** the sum of all running sequences must stay under `CTX`, or llama-server
fails every in-flight request with "Context size has been exceeded" (2026-10-22: four agents at 79k+54k+82k+50k = 265k).
So the profile key `AGENT_CTX` (65536 for Flash-Next; Hermes refuses windows under 64,000) is one agent's window: it becomes
Hermes' `context_length` (`compression.threshold_tokens: 45000` compacts before it fills) and the HUD runs at most
`floor(0.75 × CTX / AGENT_CTX)` agents at once (3), whatever `PARALLEL` says. The 25 % headroom matters: Hermes checks its
window only between turns, so one turn's tool results can overshoot it (an 80k request was seen with a 65k window); with
4 × 65k = the whole pool, the 2026-10-23 03:28 crash killed the lead and two coders in the same second. Measured fixed
request part: ~19k tokens (lead) / ~14k (member). Per-role `hermes -t` toolsets were tried and dropped: `-t` drops the
MCP (crew/rag) tools, and the saving was small. Raise
`AGENT_CTX` for fewer, longer-context agents; lower it for more parallel ones. The HUD scheduler runs crew
members before new top-level jobs and keeps bench/eval jobs exclusive. "One specialist" mode runs a single role
directly (no crew).

| role | class | use it for |
|---|---|---|
| `architect` | orchestration | design, interfaces, migration order, risks |
| `scout` | research | fast read-only map of a codebase (`path:line` answers) |
| `coder` | engineering | implement to a dictated interface, with tests |
| `debugger` | engineering | minimal repro → root cause → proof (fix only if asked) |
| `ops` | engineering | installs, builds, scripts, services under test, measurements |
| `tester` | quality | adversarial, contract-level tests; reports what breaks |
| `reviewer` | quality | ranked findings with `file:line` and a failure scenario; no edits |
| `security` | quality | exploitable findings in input/shell/file/secret/network handling |
| `data-sorter` | analysis | clean/normalise/de-dup/classify with counts and rejects |
| `analyst` | analysis | computed numbers with method, denominators, sanity checks |
| `logician` | analysis | validity of arguments, hidden assumptions, full case enumeration |
| `researcher` | research | one question from the web, full pages, dated, cross-checked |
| `librarian` | research | the owner's documents via RAG with `(file, p. N)` citations |
| `writer` | writing | READMEs, reports, handovers from verified material only |

Every role is a markdown file with a small front-matter (`max_turns`, `budget_s`, `skills`); edit or add one
and it is live on the next job (`python3 agents/roles.py` lists them, `python3 agents/roles.py coder` prints
the assembled prompt). Members get `_preamble.md` (environment facts) + role + `_member_protocol.md` (a fixed
report format: Result / Files / Verification / Assumptions / Observations / Open issues). The lead gets
preamble + doctrine + mode brief + `_crew_tools.md` + the roster.

**How it is wired.** The HUD starts each agent as `hermes chat --oneshot --format stream-json` with
`HERMES_EPHEMERAL_SYSTEM_PROMPT=<role prompt>` and `LA_JOB_ID/LA_ROOT_ID/LA_DEPTH` in its environment.
The `crew` MCP server (`crew/crew_server.py`, registered in `hermes-home/config.yaml`) finds those from its
parent process and calls the HUD's loopback-only `/internal/*` API (token in `hud/data/internal.token`,
unreachable through nginx). Tools: lead = `roles`, `spawn`, `status`, `wait`, `result`, `cancel`,
`plan_set`, `plan_update`, `progress`, `clock`, `bg_*`; members = `progress`, `clock`, `bg_*` only (they
cannot spawn). Hermes' own `delegation` toolset is disabled (it could not give children a persona and was
capped at 2 per one-shot run). A `hermes chat` from the terminal gets the same tools; its spawns show up in
the HUD under a synthetic "CLI session" mission.

**Mission panel** (console → *Mission*): the plan with per-step status and timing, the crew table (state,
elapsed, tool calls, last tool, click to open a member's transcript), background tasks (state, exit code,
elapsed, log tail, stop button) and a timeline of progress notes from the lead and members. The chat sidebar
nests members under their mission; the Overview shows busy/total slots and every running agent.

**Background tasks** (`bg_start name "command"`) run in a fresh container of the sandbox image on the same
`/workspace` with a persistent home of their own (`hermes-home/sandboxes/docker/bg-home` → `/home/pn`, pytest
preinstalled; same egress relay, 8 GB RAM cap, `timeout` wrapper), outlive the agent's turn, and
are polled by the HUD every 6 s; on exit the last 300 log lines are kept in `hud/data/hud.db` and the container
is removed. `docker ps --filter label=la-bg=1` shows them.

**Tool reliability pass (2026-10-22):** `hermes-home/config.yaml` now has `tools.tool_search.enabled: "off"`. Hermes
normally hides MCP tools (crew, rag) and some built-ins behind `tool_search`/`tool_describe`/`tool_call` bridge tools
("deferred tools"); the local model repeatedly mangled the `tool_call` wrapper (errors.log: "mcp__crew__wait ... is not a
known tool name. Deferred tools must be invoked through tool_call"), which is why simple crew steps took many turns.
With the bridge off every tool schema is passed to the model directly. Also: `custom_providers` list migrated to the
`providers:` dict (`--provider custom:local-<effort>` unchanged), `auxiliary.title_generation.model_upgrade_enabled: false`
(the HUD titles jobs itself; the model call timed out after 30 s in most runs), and `skills.disabled` lists skills that need
services this box has no access to (Apple apps, mail, X/Twitter, other agent CLIs, cloud media generation). Backup:
`config.yaml.bak-tools`. `hermes doctor` still prints "auxiliary.*.provider 'main' cannot be resolved" — harmless, the
auxiliary client handles `main` explicitly (agent/auxiliary_client.py) and the checker just doesn't know the value.

**Claude brief paths (2026-10-22):** agents naturally write `/workspace/...` paths in `claude_run` briefs, but Claude runs in
the staging copy `claude-runs/<id>` where `/workspace` is unreachable — a run would end "done" with no changes and a
denied Read. `claude/gate.py` now has a "Working directory" header in every brief and `relativize_paths()` rewrites
`/workspace/x` and `/mnt/data/local-agent/workspace/x` to `./x` in task and context; `_crew_tools.md` and the
`claude_run` docstring tell agents to list every directory Claude must see in `files` (directories are allowed). Verified
e2e: Hermes → claude_run (opus/low) → file created → merged back as `created`. Backup `gate.py.bak-paths`.

**Hermes upgrade status (2026-10-22):** installed = v0.21.6+607 (git, Oct 10); upstream = v0.22.4 (6,790 commits ahead).
A v0.22.4 checkout with its venv and the image patch applied is ready in `hermes-agent-next/` (git worktree of
`hermes-agent`), but it is NOT in use: from v0.22 every `hermes chat` run, including `--oneshot --format stream-json`, goes
through a gateway daemon (`hermes_cli/gateway_chat*.py`). That (a) rejects `--source hud` ("Gateway does not support source
'hud'" — use `oneshot`/`tool`), (b) spawns MCP servers daemon-wide instead of per job, so `crew/crew_server.py` can no
longer find `LA_JOB_ID` by walking `/proc/<ppid>/environ` (only `HERMES_EPHEMERAL_SYSTEM_PROMPT` and
`terminal.client_env_passthrough` names travel per session, and `HERMES_*`/secret names are filtered), and (c) needs the
gateway running under systemd. Migrating = rework crew identity (e.g. pass the job id inside the ephemeral prompt and as a
tool argument), change `--source`, add a `la-gateway` unit, re-test stream-json event types. Until then stay on
`hermes-agent/`. The v0.22.4 config format was tested: `hermes doctor --fix` only migrates `custom_providers`, which is
already done here.

**Bigger KV pool, applied when idle (2026-10-23):** this model's KV cache is cheap (12 of 48 layers are full attention, 2 KV
heads, q8_0 ≈ 13 KB/token → 262k ≈ 3.4 GB; all experts already live in RAM via `--n-cpu-moe 48`), so the pool can grow:
`scripts/apply-profile-when-idle.sh CTX=524288 UBATCH=1024` waits until no job / Claude run / llama slot is active, edits the
profile, restarts `la-model`, smoke-tests it and rolls back on failure (log `logs/apply-profile.log`; `touch
profiles/.apply-now` forces it at the cost of in-flight agent turns). UBATCH 2048→1024 frees ~2.7 GB of prefill compute
buffer to pay for the extra ~3.4 GB of KV (prefill ~20 % slower). **512k OOMed on the 24 GB card** (KV and the flash-attention scratch/mask both scale with CTX; llama-server crash-looped on an
865 MiB cudaMalloc until the watcher rolled back) — 384k is the ceiling here: `CTX=393216 UBATCH=1024`, `AGENT_CTX=98304`
→ 3 agents at 96k windows with 25 % headroom. llama-server logs "slot context (393216) exceeds the training context
(262144) - capping": that caps one *sequence* at 262k (irrelevant, agents use ≤ 96k); the unified pool is still created at
393216 (`n_ctx_seq (393216) > n_ctx_train` warning; VRAM 20.7 → 20.8 GB despite UBATCH halving, i.e. +1.7 GB of KV).
`/props` therefore still reports n_ctx 262144 — read the pool size from the profile, not from /props.
**Compaction floor:** Hermes cannot summarise below ~42k tokens per agent (system prompt + protected first messages, i.e. the
brief, + `protect_last_n` recent messages). A trigger near that floor makes agents compact after every turn until they fail
with "couldn't shrink it enough" (seen 05:13–07:19 with 45k/64k). Keep `threshold_tokens` ≥ floor + 25k and
`protect_last_n: 10`.
Root cause of the 03:28 context deaths, found while resuming: compaction summaries ran on the main provider with xhigh
thinking, hit Hermes' 600 s compression ceiling and were abandoned, so contexts grew to ~190k until the pool overflowed —
`auxiliary.compression.reasoning_effort: none` fixed it (a 189k→44k compaction now takes ~160 s).

**Live notices to a running crew (2026-10-23):** agents are one-shot Hermes processes, so the owner cannot type into them
mid-run. The Mission panel's **📣 notify crew** button (`POST /api/jobs/<id>/notice`, or `post_notice()` / a row in the
`notices` table) stores a message that rides along as `owner_notice` in every crew-tool result (`clock`, `wait`, `status`,
`progress`, `plan_update`) of that mission for 30 min; `_crew_tools.md` tells agents to act on it and acknowledge it in a
`progress` note. Also new in `hermes-home/config.yaml`: `file_read_max_chars: 40000` (one `read_file` can no longer return
~30k tokens and push an agent past its 65k window before compaction; `web_extract` was already capped at 15k chars).

**SearXNG engines (2026-10-23):** "search is down" was zero results, not a dead service: the only three general-web engines
upstream enables (brave, duckduckgo, google cse) were all blocked at once through the shared proxy IP (too many requests /
CAPTCHA), and upstream suspends a CAPTCHA'd engine for 24 h. `services/searxng/settings.yml` now shortens the suspensions
(5–30 min) and enables the engines that answer through the proxy with relevant results: `duckduckgo web` (the HTML
endpoint works while plain `duckduckgo` is CAPTCHA'd), yandex, privacywall, naver, bing, yahoo, mwmbl — 30–110 results per
query. Tried and rejected (access denied / empty / errors): google, qwant, startpage, mojeek, fastbot, yep, fireball,
searchmysite, zapmeta, seznam, resulthunter, tusksearch, gabanza, presearch, wiby, reloado; see the settings.yml comment. Check with
`curl 'http://127.0.0.1:8888/search?q=test&format=json'` → `unresponsive_engines`. Restart only the container
(`cd services && docker compose restart searxng`, ~3 s); running agents are unaffected.

**Budgets and deliverables (2026-10-22):** crew missions default to 12 h (coding) / 4 h (research, data) of wall clock
and the composer lets the owner set **0.5–24 h** (`budget h`; turns scale with it, up to 3000) and a **deliverable cadence**
(`deliver every h`, default 2, 0 = off). With a cadence the lead's brief gets a "Deliverable cadence" section, every
`clock`/`wait`/`status`/`progress` result carries a `deliverable` countdown (`next_due_in_s`, `overdue`, a nudge `note`), and
the lead-only crew tool `deliver(summary, paths, version, how_to_run)` checkpoints, copies the product into
`workspace/outbox/deliveries/<mission>/<time>-vN/` with a DELIVERY.md, zips it and records it (`deliveries` table). The
Mission panel shows each delivery with a download card and the next due time. "Usable" is defined for the agents in
`agents/roles/orchestrator.md` §2.6c: README with exact commands, checks green, vertical slices, nothing half-wired.

**Host files (2026-10-22):** besides `/workspace`, the Hermes sandbox and background-task containers mount all of
`/mnt/data` read-write at the same paths as on the host, so agents can edit the owner's other projects when asked.
Guard rails: `/mnt/data/local-agent` is read-only (the agent cannot rewrite the HUD/scripts that run on the host),
`hud/data`, `hermes-home` and `claude-runs` are hidden behind empty read-only tmpfs mounts, and `/mnt/data/keys.env`
is an empty file (`data/masks/empty`). Set in `hermes-home/config.yaml` (`terminal.docker_volumes`/`docker_extra_args`)
and `MNT_MOUNTS` in `hud/app.py`. Shadow checkpoints/rollback cover only `/workspace`. Claude Code delegation is
unchanged: its `files` must still be under `/workspace`.

**Engineering rails (2026-10-14).** The crew implements the standard agent-coding practices as mechanisms, not
just prompt text:

| practice | where |
|---|---|
| execution feedback loop (tests + linter + type checker until green) | skill `feedback-loop`; coder/debugger/tester prompts; coding mode brief |
| targeted edits, never whole-file rewrites | Hermes `patch` (exact unique match, fuzzy) + `read_file` with `N\|` line numbers; skill `targeted-edits`; `write_file` refuses to overwrite unread files |
| codebase navigation + repo map | crew tool `repo_map` (`agents/repomap.py`, Aider-style symbols with line numbers), Hermes `search_files` (ripgrep); skill `repo-navigation` |
| project memory file | `workspace/AGENTS.md` (auto-loaded by Hermes from the cwd); per-project `AGENTS.md` read explicitly; skill `project-memory` |
| plan first, task list | `plan_set` / `plan_update` (mission panel), Hermes `todo_list` |
| subagents for context hygiene | `scout`/`researcher`/`librarian` members return conclusions only |
| fresh-context reviewer | `reviewer` role briefed with the original request + `diff(<mission start>)` |
| skills instead of one giant prompt | role prompts stay doctrine; specifics live in `hermes-home/skills/local-agent/*` loaded on demand (`skill_view`) |
| compaction | Hermes compression (`compression.threshold` 0.5 of the window) + `state_save` / `state_get`, prepended to the next follow-up run; `/workspace/.crew/mission-<id>.md`; skill `mission-handoff` |
| safety rails | shadow git over `/workspace` (`hermes-home/checkpoints/workspace.git`, no `.git` in the workspace): auto checkpoint at mission start and before every spawn, `checkpoint` / `checkpoints` / `diff` / `rollback` tools, restore + diff buttons in the mission panel; skill `checkpoints` |

Two Hermes defaults are changed for this: `security.protected_instruction_files: false` so agents may write
`AGENTS.md` in the sandboxed, checkpointed workspace (otherwise `write_file` blocks it with an approval nobody can
give in a one-shot run), and the prompts tell agents not to use the `memory` tool from dashboard runs (its writes are
staged for approval and never land).

Rollback restores index and tree to the checkpoint (`git read-tree --reset -u`): files created after it are removed
because the pre-rollback checkpoint tracked them; excluded paths (`uploads/`, `.venv/`, `node_modules/`, caches,
files > 20 MB) and anything git never tracked are never touched, and a pre-rollback checkpoint is always taken first.

Limits: `MAX_AGENTS` = 10 concurrent processes (hard cap in `hud/app.py`), 40 members per mission, 12
background tasks box-wide, `wait()` blocks ≤ 600 s per call (MCP timeout 660). Lead defaults: 300 turns /
6 h (coding), 200 / 4 h (research, data). Crew members share the 256k token pool, so briefs tell them to read
excerpts, not whole files.

## Chat rendering and context compaction (2026-10-14)

- **Rendered Markdown** in the console: GitHub-flavoured Markdown via a vendored `marked` (`hud/static/vendor/`),
  passed through a DOM whitelist sanitizer (raw HTML is shown as text; only `http(s)`/`mailto` links), then fenced
  code becomes a code block with language label, line count, **copy** button and `highlight.js` colouring; tables
  scroll horizontally with numeric columns right-aligned; task lists, blockquotes and headings are styled.
- **Charts**: a fenced block with language `chart` holding one JSON object (`type` bar/line/pie/doughnut/scatter/
  radar, `labels`, `series[{name,data}]`, optional `x`, `y`, `stacked`, `horizontal`, `fill`) renders with Chart.js.
  The model is told the syntax in `agents/roles/_output_format.md`, which is part of the chat brief
  (`agents/modes/chat.md`, now the ephemeral prompt for plain chat) and of the lead's prompt.
- **Context meter**: the transcript header shows the thread's last-turn input tokens against the model context;
  it turns red above 70 %.
- **Compact** (button, or automatic): queues a "compact context" turn that resumes the session and writes a
  structured handoff summary (`COMPACT_PROMPT` in `hud/app.py`, stored in `jobs.summary`). The next follow-up
  starts a **fresh** Hermes session seeded with that summary instead of resuming; later follow-ups resume the new
  session. A follow-up sent on a thread whose last turn used more than 70 % of the context is auto-compacted first
  (`AUTO_COMPACT_FRAC`). Hermes' own in-session compression (threshold 0.5 of the window) still applies inside a
  single long turn. The compacted summary is shown inline as a collapsible "Context compacted" block.

**Transcript look (2026-10-14b):** answers render as plain text on the panel background (no boxed final message);
inline code is coral-on-tint, bold is bright, ordered and unordered lists are spaced, links underline on hover, user
messages are right-aligned bubbles. Tool calls fold into one-line activity groups ("Ran 3 commands, read 2 files,
edited `parser.py` +12 −3", click to expand; a spinner and the current call while running). `plan_set` renders as a
plan card that ticks off live, `plan_update` and `progress` calls render as timestamped progress lines, and a live
bar under the transcript shows elapsed time, plan step with a progress bar, agents working, tool-call count, the
current call and the latest crew note. Prompts ask the lead and members to post `progress` at every milestone and at
least every ~5 minutes.

## Files, effort, usage (2026-10-15)

- **Files to download:** the agent saves deliverables under `/workspace/outbox/` (any `/workspace` path works) and
  links them as `[name](/workspace/outbox/name)`; the console turns those links (also `sandbox:`, `file://`, host-path,
  relative `outbox/…` forms and bare `/workspace/…` paths in text) into download cards. Clicking fetches the file with
  the session and hands it to the phone's share sheet (Save to Files; works from the home-screen app) or triggers a
  blob download on desktop; a link whose file does not exist shows a toast. Links that cannot be resolved are shown
  struck through instead of as dead links (`GET /api/files?path=` — signed-in only, confined to `/workspace` after resolving symlinks,
  `.crew/` excluded, always served as an `application/octet-stream` attachment so agent-written HTML never renders).
- **Follow-ups from a member's transcript** continue the mission's latest lead turn (the checkbox reads "continue this
  mission"); before 2026-10-15 they were rejected with HTTP 400.
- **Effort:** fast · medium · high · extra high · max, chosen per message (composer ⋮ or the effort pill). Each maps
  to a Hermes provider `custom:local-<effort>` in `hermes-home/config.yaml` that sets llama-server's per-request
  thinking controls (`reasoning_effort` none/medium/xhigh + `reasoning_budget_tokens` 0/4k/12k/32k/unlimited) plus
  an effort brief in the system prompt; max also raises the turn floor to 120. Defaults: chat medium, crew modes high;
  crew members inherit the lead's effort.
- **Usage page:** KPIs (runs, generated/read tokens, agent time, tool calls, success, speed), tokens per day,
  agent time, runs per day, modes, effort, top tools, crew roles, and a weekday × hour heat-map; 24h–1y range.
- **Other chats:** crew tools `chats_list(query)` / `chat_read(chat_id)` let any agent look up the owner's other
  conversations (owner messages + final answers, no tool noise) when they refer to earlier context.
- **`wait` ≤ 300 s:** Hermes kills tool calls that run concurrently after 420 s, so `crew.wait` returns at 300 s with
  `timed_out` and the ids still running; the lead just calls it again.
- **Restart-safe agents:** agent output goes to `hud/data/job-N.out.jsonl` (not a pipe) and `la-hud` runs with
  `KillMode=process` and agents are plain `subprocess.Popen` children (asyncio subprocess transports kill their child
  when the event loop closes), so restarting the HUD no longer kills running agents; on start it reattaches to live ones and
  continues from the saved offset.

## econ — the owner's economics study dashboard (2026-10-15)

Third portal on EXAMPLE_HOST: **https://EXAMPLE_HOST/llm/econ/** (rides the existing `^/llm` tunnel rule; nginx snippet
`/etc/nginx/snippets/econ-locations.conf`, included from `local-agent-locations.conf`). A static, no-login study app:
Learn (chapters built from the handwritten notes: summary, mnemonic, key ideas, interactive model, the notes
transcribed, plain-words example, terms, chapter quiz, the original page photo), Play (all models), Glossary (every term
with definition/example/related; underlined terms anywhere open a popover), Cards (Leitner flashcards, 3 boxes, stored in
the browser), Quiz (10 questions: chapter questions + glossary-generated ones; best scores drive the "mastered" chip),
Notes (upright photos + transcriptions + how to add more).

- **Page code** (not agent-editable): `/mnt/data/econ-dash/www/` — `index.html`, `app.js` (vanilla, SVG widgets:
  `ppc`, `marginal`, `sunk`, `ocpicker`, `factors`, `model`, `hand`), `app.css`. Strict CSP (`script-src 'self'`).
- **Data** (agent-editable, so the owner can hand new note photos to the local LLM): the sandbox folder
  `/workspace/econ/` = `/mnt/data/local-agent/workspace/econ/` — `content.json` (chapters, glossary, quiz, widget
  presets), `images/`, `AGENTS.md` (the rules the agent follows when adding notes). Served at `/llm/econ/data/` as inert
  data only: fixed MIME types (json/jpg/png/webp/md/txt, everything else downloads), CSP `default-src 'none'; sandbox`,
  nosniff — so nothing the agent writes there can ever run as a page on the EXAMPLE_HOST origin (which hosts the HUD).
- The app renders JSON as text nodes only (no innerHTML), parses a tiny markdown (paragraphs, bullets, **bold**) and
  auto-underlines glossary terms (and their `aliases`) in notes, summaries and quiz explanations.
- To add notes: in the LLM console, attach the photo(s) and say "add these econ notes to my dashboard"; the agent
  appends a chapter + terms + quiz + image per `workspace/econ/AGENTS.md`, validates the JSON, and the page picks it
  up on reload (fetched with `cache: no-store`). New *interactive* widgets need page-code changes (listed in
  `workspace/econ/README.md` under "Wanted widgets").

## Attachments, Claude's full toolset, speed (2026-10-28)

- **Attach any file** (clip, paste or drop): images still go the old way (re-encoded JPEG, native vision); everything
  else — code, zips, tarballs, PDFs, docs, data — uses `POST /api/attach/init` → `/api/attach/<id>/chunk?offset=N` →
  `/api/attach/<id>/finish` (nginx `location ^~ /llm/api/attach/`, 10 MB per request). Up to 20 files per message, 1 GB
  each. Chunks are sized to the measured link speed (~20 s each, 256 KB–8 MB, halved after a failure, resumed from the
  server's offset), because the tunnel's inbound leg is only ~20–40 KB/s and drops multi-minute requests (fixed 8 MB
  chunks failed with nginx 400 after ~2 min). Uncompressed files are gzipped in the browser first (`encoding: gzip`,
  decompressed server-side with a size cap). A 12 MB incompressible zip took 8 min; typical code zips take seconds.
- Files land in `/workspace/uploads/files/<id>/<name>`; `.zip/.tar(.gz|.bz2|.xz)/.tgz` are unpacked next to it
  (`_unpack`: no symlinks/devices, `..` and drive letters dropped, absolute paths re-rooted inside, ≤ 50k files, ≤ 4 GB
  unpacked, a single top folder is flattened). The run's prompt gets an `[Attached files]` block: paths, sizes, file-type
  counts, a 3-level tree, and the content of small text files inline (≤ 24 KB each, 60 KB total) so the agent needs no
  read round trip. Uploads are excluded from checkpoints, so the block tells the agent to copy a project into
  `/workspace/<name>/` before editing. Unfinished uploads are swept after 48 h.
- **Claude runs get Claude Code's whole toolset.** With `--permission-prompts none`, any tool without an allow rule is
  silently denied, so `_claude_settings` pre-approves Read/WebSearch/WebFetch/Agent/Task/Skill/ToolSearch/background
  shells (+ Edit/Write/NotebookEdit/Monitor in edit mode, scoped to the run dir). Only headless-impossible tools are
  disallowed (cron, remote triggers, push, AskUserQuestion, plan mode, worktrees). This CLI build (2.1.282) has no
  separate Glob/Grep/TodoWrite tools; search goes through Bash.
- **Bash inside Claude runs was broken by the Ubuntu 26.04 upgrade**: `/etc/apparmor.d/bwrap-userns-restrict` gives
  bwrap's children `audit deny capability`, so Claude Code's nested seccomp namespace failed (`apply-seccomp: write
  /proc/self/setgroups … Permission denied`) and every command errored. Fix: a root-owned copy
  `/usr/local/libexec/la-claude/bwrap` with its own profile `/etc/apparmor.d/la-claude-bwrap` (`flags=(unconfined)`,
  `userns`), first on the claude process's PATH (Claude Code ignores `sandbox.bwrapPath` from `--settings`). The system
  bwrap is unchanged. `sandbox.enableWeakerNestedSandbox` was rejected: it exposes host `/proc` inside the sandbox.
  Undo: `sudo apparmor_parser -R /etc/apparmor.d/la-claude-bwrap && sudo rm /etc/apparmor.d/la-claude-bwrap
  /usr/local/libexec/la-claude/bwrap`.
- **Test tooling for Claude runs:** `/usr/local/libexec/la-claude/` (root-owned, first on the run's PATH) also holds a
  Python 3.14 venv with pytest + ruff behind `python3`/`python`/`pytest`/`ruff` wrappers (outside `/mnt/data`, which the
  sandbox hides), so Claude can run `python3 -m pytest` itself. Verified 2026-10-28: edit + own test run + merge in 14 s.
  Never write through those names with `tee` while they are symlinks: they resolve to `/usr/bin/python3.14`.
- **Local agent ↔ Claude Code tool parity:** Bash = `terminal` (+ `process_manage` for background jobs/Monitor), Read =
  `read_file` (also PDF/docx/notebooks), Write = `write_file`, Edit = `patch` (replace_all, fuzzy match, syntax check),
  Grep/Glob = `search_files`, WebSearch = `web_search` (SearXNG), WebFetch = `web_extract` (Crawl4AI) + `browser_*`,
  TodoWrite = `todo`, AskUserQuestion = `clarify`, Skill = skills, Agent = crew `spawn`, image reading = `vision_analyze`,
  plus `execute_code`, RAG, checkpoints/rollback and `claude_run` itself.
- **Speed (measured on the 4090 D + 7995WX, experts in RAM):** generation ~38 tok/s, prefill ~540 tok/s; threads 48/64/80/96
  make no difference (kept 48); `--no-op-offload` is worse (prefill 340). Applied in `EXTRA_ARGS` (both profile files):
  `--spec-type ngram-mod` (+15 % on copy-heavy output such as edits, neutral otherwise, no VRAM), `-cms 1024 -ctxcp 64`
  (context checkpoints every 1k tokens instead of 8k — this hybrid model can only reuse a cached prefix up to a
  checkpoint, so a new run after another conversation re-read its whole ~7k–20k system prompt: 20 s → 3.3 s), and
  `--cache-ram 49152` (the 8 GB default held only ~10 states of 0.6–0.9 GB and evicted constantly). Within one conversation
  reuse was already fine (< 100 tokens recomputed per turn). Remaining limit: while one agent prefills, other agents'
  generation drops to ~15 tok/s, because every batch step streams the CPU-held experts to the GPU; only more VRAM fixes it.
- `profiles/active.env` must stay a **symlink** (the HUD's status reads the link target): `sed -i` on it replaces the link
  with a file and `/api/status` then fails with EINVAL. Edit the named profile file instead.

## Claude Code delegation (2026-10-16)

The crew can hand the hardest piece of a coding mission to the owner's logged-in Claude Code (`claude -p`) with the
`claude_*` crew tools: model `opus` or `fable`, effort `low…max`, mode `edit`/`readonly`. Pipeline:

1. **Gate** (`claude/gate.py`, rules in `claude/rules.json`, limits in `claude/config.json`): rewrites the request into a
   fixed-shape brief; hard-blocks manipulation/jailbreak phrasing, policy/ban talk, permission-bypass flags, platform abuse,
   intrusion/malware and harm-to-people patterns; dual-use words need a stated purpose + the local judge's approval
   (llama-server :8181). A block means nothing is sent. Every decision is logged in `hud.db` table `cgate`.
2. **Staging** (`hud/app.py` `_crun_collect/_crun_stage`): only the paths named in `files` are copied to
   `claude-runs/<id>/`, which is Claude's cwd and the only place it may read/write (permission deny rules for every sibling
   + bubblewrap sandbox for Bash). Staged text is screened with the same block rules + a secrets scan; dual-use excerpts go
   to the judge. Why: everything Claude reads is sent to Anthropic and judged by its safeguards — the first version ran in
   `/workspace` root and a trivial calc fix was flagged `[cyber]` twice; with staging the same brief runs clean.
3. **Merge back** (`_crun_merge`): 3-way against the staged originals — unchanged-in-workspace files are updated/created/
   deleted; if the workspace copy changed meanwhile, Claude's version is written as `<file>.claude-<id>`. Checkpoints before
   and after, so `diff`/`rollback` work. Result + merge summary in table `cruns`; Mission panel shows brief/report/diff.

Breakers: 2 concurrent, 10/hour, auto-pause after 3 safeguard refusals in 24 h (clear in Settings).
Checkpoints skip nested git repos (a crew-`git init`ed project); before 2026-10-16 one such repo (`ratlive/`, no commits)
made every `git add -A` fail silently, so checkpoints from then until the fix are empty.

## Persistent toolbox, host runner, pretty PDFs (2026-11-03)

**Toolbox** — `/mnt/data/la-toolbox` (`tb help`). Everything the agent installs lives there and survives chats, missions,
profiles and sandbox re-creation; the same path works in the Docker sandbox and on the host (micromamba packages are
self-contained). `bin/tb`:

| command | what |
|---|---|
| `tb list` / `tb find <words>` / `tb show <name>` | the registry (`registry.json`, index `TOOLBOX.md`, one recipe per tool in `recipes/`) |
| `tb install <conda-forge pkg>` | into the main env (`env/`, Python 3.14; ffmpeg, pandoc, typst, weasyprint, node, rg, …) |
| `tb pip <pkg>` → run with `tbpy` | shared Python 3.12 venv (`venvs/py`: numpy/pandas/polars/playwright/portablemc/pwntools…) |
| `tb npm <pkg>` / `tb env <name> <pkgs>` | global npm (`npm/`) / a separate conda env (`envs/jdk17`, `envs/jdk21`) |
| `tb java 17\|21\|25 --export` | JAVA_HOME for a JDK (25 is the default `java`) |
| `tb register <name> --desc … --run … --notes …` | record anything that works so the next chat reuses it |
| `tb doctor` | every registered command still resolves? |

Vendor downloads go under `opt/` (Ghidra 12.2, jadx, apktool, Playwright Chromium, the Minecraft game cache,
portable CPython). `bin/mc-test <loader:version> [mods…]` launches a Minecraft client (vanilla/fabric/forge/neoforge/
quilt) via portablemc, waits for the title screen or a crash, screenshots it and reports — headless (Xvfb + llvmpipe) in
the sandbox, on the GPU via `host_run(mc-gpu)`.

How the agent gets it: `data/sandbox-profile` is mounted read-only as `/home/pn/.profile` in the sandbox
(`docker_volumes` in `hermes-home/config.yaml`; Hermes snapshots a login shell, so PATH gets `la-toolbox/bin` first and
`la-toolbox/env/bin` last). Adding that volume changed the container fingerprint, so every session after 2026-11-03 gets a
fresh container; the five pre-toolbox containers are orphans Hermes reaps. The HUD prepends a short toolbox brief
(`toolbox_brief()` in `hud/app.py`: what is installed, "tb find before installing") to every chat-agent prompt; crew roles
get the same rules from `agents/roles/_crew_tools.md`.

**Usage counting + the HUD Toolbox page** — every use is one JSON line in `la-toolbox/logs/usage.jsonl`: `tb` commands
(`tb show` counts as a recipe lookup, `tb install|pip|npm|env` as an install, `tb register`), the wrappers `tbpy` /
`prettypdf` / `mc-test`, host runs (written by `scripts/host_run.py`), and every env/bin command the registry knows
through `la-toolbox/shims/` (`tb shims` regenerates them after each install/register; a shim appends the line and execs
`env/bin/<cmd>`; on PATH right after `bin/`). `tb usage [name]` prints the counts. The HUD page **Tools**
(`/api/toolbox*`, `hud/app.py`) shows the registry with uses per range / ever, last use + where (sandbox/host), the recipe
(editable — it is what the agent reads with `tb show`), uses per day, most used, disk per directory, `tb doctor`, host runs
with their logs, recent activity, and an install box (conda-forge / pip / npm) that runs `tb` on the host into the same
toolbox the sandbox sees. "Forget" = `tb unregister` (nothing is uninstalled). `data/sandbox-profile` is a file bind
mount: edit it in place (truncate + write), a rename would detach every running container from it.

**Host runner** — scripts in `la-toolbox/host/` run on the real desktop (screen, GPU, Lunar Client) through the crew
tools `host_run(tool, args, timeout_s, wait_s)` / `host_run_status(run_id)` / `screenshot(region)` → HUD
`/internal/host_run` → `scripts/host_run.py` under the root-owned bwrap: filesystem read-only except `/mnt/data`, `/tmp`
and the game dirs; secrets and state masked (keys.env, hud/data, hermes-home, claude-runs, ~/.claude, ssh/gpg, browser
profiles, chat-app tokens, docker + systemd + Hyprland sockets); own PID namespace. Every run logs to
`la-toolbox/logs/host/<ts>-<tool>.log`. Host tools so far: `selftest`, `desktop` (xdotool click/type/key/windows for X11
windows — native Wayland windows are untouchable by design), `lunar` (Lunar Client with temporary test mods),
`mc-gpu`. The agent may add host scripts (they are auditable files), but cannot change the runner or the confinement
(`/mnt/data/local-agent` is read-only in the sandbox).

**Pretty PDFs** — the owner's rule is that every PDF must be extravagant. `prettypdf doc.md` (`la-toolbox/pdf/prettypdf.py`,
WeasyPrint on the main env, fonts in `la-toolbox/fonts/` via `pdf/fonts.conf`) renders Markdown/HTML with the themes
editorial / luxe / tech / academic / auto (cover, contents, stat tiles, callouts, pull quotes, cards, timelines, two
columns, highlighted code) and prints PNG page previews to look at. Skill `hermes-home/skills/productivity/pretty-pdf`
is auto-attached, and `PDF_BRIEF` prepended, whenever a chat prompt mentions a PDF.

**Remote Control** — crew tool `remote_control(status|start|restart)` watches the owner's `claude-rc` user unit
(Claude Code Remote Control, environment "home server"); restart of a healthy unit needs `force=true`.

## Always on (systemd user units)

All parts are systemd **user** units in `~/.config/systemd/user/`. Lingering is on, so they start at
boot without a login.

| unit | what | recovery |
|---|---|---|
| `la-model` | llama-server for the active profile (`scripts/run-model.sh`) | `Restart=always` + watchdog |
| `la-embed` | CPU embedding server | `Restart=always` + watchdog |
| `la-decide` | CPU decision model (Clef-Flash, `scripts/run-decide.sh`, port 8183) for the skill/effort router | `Restart=always` |
| `la-relay` | docker0 → proxy relay for the sandbox | `Restart=always` + watchdog |
| `la-services` | `docker compose up -d` (SearXNG, Crawl4AI, Qdrant; `restart: unless-stopped`) | watchdog restarts stopped containers |
| `la-hud` | the web HUD | `Restart=always` + watchdog |
| `la-watchdog.timer` | `scripts/watchdog.sh` every minute | restarts a unit that is alive but unresponsive for 3 checks (10 min grace after start) |

`./agent.sh stop` pauses the watchdog for the model stack (flag `logs/STOPPED`) so an intentional
stop sticks, and `./agent.sh start` clears it. The HUD is always guarded.
`systemctl --user status la-model` and `journalctl --user -u la-model` work as usual.

## Swapping models

Each model is a small profile in `profiles/` (model path, context, offload flags, sampling):

```bash
./agent.sh use qwen3.8-flash-next   # or qwen3.8-27b — also updates Hermes' context_length, restarts
```
(or the Model page in the HUD).

To add a model: download a GGUF with `scripts/pdl.py` (see Downloads), copy a profile, and edit
its keys (`MODEL`, `CTX`, `NGL`, `N_CPU_MOE`, …; `EXTRA_ARGS` for anything else). Dense models that fit in VRAM use `-ngl 99`. For MoE models bigger
than VRAM, add `--n-cpu-moe N`, which keeps the expert weights of the first N layers in system
RAM; lower N until VRAM is about 1.5 GB from full. Tool calling works for any model whose GGUF
embeds a chat template (`--jinja`).

| profile | model | where it runs | ctx | gen tok/s | prefill tok/s |
|---|---|---|---|---|---|
| `qwen3.8-27b` (default) | huihui Qwen3.8-27B abliterated, UD Q4_K_M, 16.6 GB | all on GPU (22.9 / 24.5 GB) | 128k | 38 short · 29 @100k | 2,200 @16k · 1,200 @100k |
| `qwen3.8-flash-next` (active) | huihui Qwen3.8-Flash-Next abliterated, UD-Q4_K_XL, 112 GB | GPU (20.9 GB) + experts & 28.8 GB n-gram table in RAM (~110 GB) | 256k | 45.6 short · 35.7 @100k | 1,262 @16k · 1,172 @100k |

## Where things live

| path | what |
|---|---|
| `agent.sh` | the start/stop/status/chat/use/logs script (drives the systemd units) |
| `hud/` | web HUD: `app.py`, `static/`, `data/` (password hash, task DB, audit log) |
| `scripts/run-model.sh`, `run-embed.sh`, `watchdog.sh` | what the systemd units run |
| `profiles/*.env` | model profiles (`active.env` → current) |
| `hermes-home/config.yaml` | **Hermes config**: provider, toolsets, sandbox, web backends, security |
| `hermes-home/.env` | service URLs + Crawl4AI token (mode 600) |
| `hermes-home/skills/local-agent/` | the four custom skills |
| `scripts/skillport.py`, `bin/skillport` | skill porter (Claude/Codex/ChatGPT/skills.sh → Hermes); ledger `hermes-home/skills/.port/` |
| `evals/skillbench/skillbench.py` | skillbench: do the ported skills help on real tasks? (results `evals/skillbench/results/<label>/REPORT.md`) |
| `decide/decide.py`, `decide/router_eval.py` | System One decision client (Jev API) + the skill/effort router and its labeled eval |
| `agents/roles/*.md`, `agents/modes/*.md`, `agents/roles.py` | crew role prompts, mode briefs, prompt assembly |
| `crew/crew_server.py` | the `crew` MCP server (spawn/wait/plan/progress/bg/checkpoint/diff/rollback/state/repo_map tools → HUD internal API) |
| `agents/repomap.py` | repo map generator (also a CLI: `python3 agents/repomap.py <dir> [max_chars] [focus]`) |
| `scripts/host_run.py`, `data/sandbox-profile` | confined host runner for `la-toolbox/host/*` tools; the sandbox login profile that puts the toolbox (+ shims) on PATH |
| `/mnt/data/la-toolbox/` | **persistent toolbox** (`bin/tb`, `registry.json`, `recipes/`, `env/`, `venvs/py`, `opt/`, `host/`, `pdf/`, `logs/host/`) — see *Persistent toolbox* |
| `hermes-home/checkpoints/workspace.git` | shadow git of /workspace (mission checkpoints) |
| `workspace/AGENTS.md`, `workspace/.crew/` | project memory (auto-loaded) and saved mission states |
| `hermes-home/plugins/web/crawl4ai/` | Crawl4AI `web_extract` backend plugin |
| `hermes-home/memories/` | the agent's persistent memory |
| `services/docker-compose.yml` | SearXNG, Crawl4AI, Qdrant |
| `services/searxng/settings.yml` | SearXNG config (JSON API on, upstream via proxy) |
| `services/crawl4ai-patch/` | 1-line SSRF-guard patch (see below) + pristine `.orig` |
| `rag/rag_server.py` | RAG MCP server + CLI (`ingest`, `search`, `list`) |
| `documents/` | drop your documents here (or in `workspace/`) |
| `workspace/` | = `/workspace` inside the agent's sandbox |
| `models/` | GGUF files; `hf-home/` holds the HF token (mode 600) |
| `llama.cpp/` | llama.cpp built from source (CUDA, sm_89) |
| `hermes-agent/` | Hermes Agent source checkout (venv in `.venv`, Python 3.14) |
| `evals/` | eval suite: `tasks.py`, `run_evals.py`, `results/<label>/` |
| `scripts/` | `pdl.py` downloader, `proxy_bridge.py`, server tests/benchmarks |
| `logs/` | every server's log + pid files |

The old June install (`~/hermes-agent`, `~/.hermes`) is untouched. Everything here uses its own
`HERMES_HOME`.

## Your documents (RAG)

```bash
rag/.venv/bin/python rag/rag_server.py ingest /mnt/data/local-agent/documents   # bulk
rag/.venv/bin/python rag/rag_server.py search "termination clause"
```

You can also ask the agent: *"index /workspace/report.pdf and summarise it with page numbers."*
PDF chunks keep their page number. Scanned PDFs have no text layer and report `chunks: 0`
(there's no OCR yet). Only `documents/` and `workspace/` are readable by the RAG tool.

## Skills (`hermes-home/skills/local-agent/`)

- **deep-research**: splits the question into sub-queries, searches each separately, reads full
  pages, cross-checks claims across sources, cites URLs, and flags conflicts and single-source claims.
- **sandbox-coding**: plan → write → pytest in the sandbox → read the real output → fix → repeat.
- **document-analysis**: ingest → verify → search and summarise with `(file, p. N)` citations.
- **verify-side-effects**: after memory, file, ingest or install actions, read the result back
  before claiming success. It also flags that a `memory` response with `"staged": true` was NOT saved.
- **feedback-loop**, **targeted-edits**, **repo-navigation**, **project-memory**, **mission-handoff**,
  **checkpoints**: the engineering rails (see *Crew mode*), loaded on demand via `skill_view`.

## Porting skills from Claude / Codex / ChatGPT / skills.sh — `bin/skillport` (2026-10-29)

Hermes loads any folder with a `SKILL.md` under `hermes-home/skills/<category>/<name>/`, and its own hub
(`hermes skills search|install …`) already reaches skills.sh, ClawHub, GitHub, LobeHub and the ~190 official
optional skills. `bin/skillport` (`scripts/skillport.py`, runs on the HUD venv + the hermes-agent checkout) adds
the missing pieces: **local sources** and **normalisation for a local model**.

```
skillport sources                  # every skill on this box that could be ported (~/.claude/skills incl. synced +
                                   #   plugin marketplaces, ~/.agents + ~/.codex + ~/.cursor skills, <project>/.claude/skills …)
skillport search <query>           # registries (skills.sh / ClawHub / GitHub / official) + local sources, --json for scripts
skillport import <src>…            # folder · .zip · ChatGPT GPT export .json · bare .md · github.com/owner/repo[/tree/ref/path]
                                   #   · hub id (official/…, skills-sh/…, owner/repo/path) · SKILL.md URL
skillport bundle <name>…           # curated sets: reverse-engineering, web-design, essentials, security (`skillport bundles`)
skillport list [--all] · check · update · remove <name> · hermesize <dir>
```

What an import does ("hermesize"): the ≤60-char one-sentence `description` Hermes shows in its skills index
(written by the local model with thinking off, heuristic fallback; the full original stays in
`metadata.port.original_description` and as a **Use when** line atop the body), `version`/`author`/`license`,
`metadata.hermes.{tags,category}` (category inferred: security, web-development, software-development, …, or
`ported`), Claude/Codex tool names in prose → Hermes tools (`Bash`→`terminal`, `Read`→`read_file`, `Edit`→`patch`,
`Grep`→`search_files`, `WebFetch`→`web_extract`, `Task`→crew `spawn` …), Claude-only frontmatter dropped but
recorded (`allowed-tools`, `model`, `hooks`, …), a **Hermes notes** section (sandbox path
`/root/.hermes/skills/<category>/<name>/`, read-only), and a skills-guard static scan (trust comes from Hermes' own trusted-repo list —
`anthropics/skills`, `openai/skills`, … — everything else incl. local folders is community; community findings
block unless `--force`; a *dangerous* verdict can't be forced). Provenance + source hashes live in
`hermes-home/skills/.port/ledger.json`; `check`/`update` re-import changed sources. Hub identifiers go through
`hermes skills install -y` first (quarantine + audit + `.hub/lock.json`) and fall back to a sparse `git clone`
when GitHub's unauthenticated API limit (60/h) is exhausted — no `GITHUB_TOKEN` on this box. Names that collide
with a bundled/local-agent skill are refused (`--name` renames; the synced Claude `pdf/docx/xlsx/deep-research`
collide on purpose — Hermes has its own). New skills are live for the next agent run; nothing restarts.

**HUD → Skills page** (`/api/skills*`): every installed skill grouped by category with source pill
(built-in / local-agent / hub / ported), enable toggle (edits the single `skills.disabled: [...]` line in
config.yaml), *use* (preloads it in the composer) and *remove* (ported/hub only); registry search with one-click
install; import box for any path/URL/identifier; bundle installer. Imports run as background `skillport` processes
(`hud/data/skillport-<id>.log`, polled by the page). The composer's skill chips are now the four featured ones plus a
**+ skill…** picker over every enabled skill.

Installed 2026-10-29 via bundles: reverse-engineering (`reverse-engineering` from mohitmishra786/low-level-dev-skills,
`reverse-engineer` from sickn33/agentic-awesome-skills), web-design (`impeccable`, `frontend-design`,
`web-design-guidelines`, `scrollcraft`, shapecut's `landing-page-design` + `design-to-motion`), essentials
(`webapp-testing`, `ast-grep`, `mcp-builder`, `code-wiki`, `grill-me`, `rest-graphql-debug`, `excalidraw`,
`docker-management`), security (`web-pentest`, `oss-forensics`, `domain-intel`). RE/pentest skills carry their
upstream authorized-use gates; the sandbox has no Ghidra/radare2 — install in `/workspace` when needed.

### Does a skill actually help? — `evals/skillbench` (2026-10-29)

Installing 60-odd skills proves nothing, so `evals/skillbench/skillbench.py` runs **real tasks** under controlled
conditions and grades them by machine, never by the agent's own claims:

| task | what the agent has to do | graded by |
|---|---|---|
| design | marketing landing page from a brief (offline, single file) | Playwright: structure, responsive, contrast/a11y, console errors + vision-model design score |
| bugfix | fix a billing library from a support ticket | 20 hidden pytest tests (incl. a bug the ticket doesn't mention) + credit for a reproducing test |
| reverse | crack a stripped, randomised key-check binary | the recovered key is executed against the binary |
| review | review the last commit of a small service | recall of 5 planted defects, anchored to the code |
| browser | finish a JS signup wizard and report the code | exact match with a per-run salted code |
| bugfix2 / reverse2 / review2 | the hard tier: multi-module project with 3 interacting bugs, a binary with anti-debug + a 2-stage key, a 9-defect commit | same graders |

Conditions, so the skill effect is isolated: **base** (every skillport/hub skill disabled = the stack before),
**preload** (the task's skill force-loaded with `--skills`), **auto** (everything enabled, nothing preloaded — does the
agent find the skill itself?) and **router** (everything enabled, the decision model below picks what to preload — the
HUD's production path). Runs use private `HERMES_HOME`s under `bench-home/<cond>` (owner's config minus MCP servers,
empty memories), hidden tests and secrets live in `~/.local/share/skillbench/` (outside every sandbox mount), and the
runner yields to the owner's running HUD jobs. Results also appear on the HUD **Evals** page as `skillbench-<label>-<cond>`.

```bash
cd evals/skillbench
.venv/bin/python skillbench.py run --label r3 [--tasks design,bugfix] [--conds base,preload,auto,router] [-j 2]
.venv/bin/python skillbench.py report r1          # or regrade r1 after changing a grader
```

Round 1 (`results/r1/REPORT.md`, Qwen3.8-Flash-Next, 1 rep):

| task | base | preload | auto |
|---|---|---|---|
| design | 0.84 | 0.86 | 0.89 |
| bugfix | 1.00 | 1.00 | 1.00 |
| reverse | 1.00 | 1.00 | 1.00 |
| review | 0.80 | 1.00 | 1.00 |
| browser | 1.00 | 1.00 | 1.00 |
| **mean** | **0.93** | **0.97** | **0.98** |

The headline finding was not the scores but **skill discovery: in `auto` the agent loaded the relevant skill in 0/5
runs** — it solved the tasks on its own and never looked at the skills index. A skill that is never loaded is dead
weight, which is what the router (next section) fixes: with the `router` condition added to round 1 the mean is also
0.98 and the relevant skill (or an accepted sibling: `claude-design`, `sandbox-coding`, `reverse-engineer`,
`code-review`, `agent-browser`) was preloaded in **5/5** runs, at a median 7 s per decision.

Round 2, the hard tier (`results/r2/REPORT.md`, same model, 1 rep, 2026-10-29):

| task | base | preload | auto | router |
|---|---|---|---|---|
| bugfix2 (3 interacting bugs, 18 hidden tests) | 1.00 | 0.95 | 1.00 | 1.00 |
| reverse2 (anti-debug + 2-stage key) | 1.00 | 1.00 | 1.00 | 1.00 |
| review2 (8 planted defects + decoys) | 0.95 | 0.95 | 1.00 | 0.70 |
| **mean** | **0.98** | **0.97** | **1.00** | **0.90** |

Honest reading: Qwen3.8-Flash-Next is at the ceiling of these tasks with or without skills, so the ported skills buy
**speed and fewer tool calls** more than score (reverse2: 8 min / 15 tools with `reverse-engineer` preloaded vs
27 min / 44 tools when the agent had to find its own way). The one low cell, review2 under `router`, is a run where
the agent read the diff once and wrote its review after 5 tool calls (missed the e-mail re-verify and the transfer
race); the identical skill under `preload` took 25 tool calls and scored 0.95, so this is how deep the agent chose to
look, not what the router loaded (`code-review` at p=0.86). `r3` repeated review2 with 2 reps: base 0.95 / 1.00,
router 1.00 / 0.95 — the 0.70 was run-to-run variance. Across both rounds the router chose a relevant skill in
**8/8** tasks with 0 wrong loads.

## System One: a local decision model routes skills and effort — `decide/` (2026-10-29)

The idea from TypeSafe's Jev talk (youtu.be/Ck7wB7pdebQ): a small **decision model** that never writes text — you
send a *state* and typed questions (`noul` yes/no, `choice`, `score`) and get calibrated probabilities back in a
fraction of a second; the *decisions* (thresholds, caps, fallbacks) stay in code. Here it runs fully locally:

- **`la-decide`**: a second `llama-server` on **127.0.0.1:8183**, CPU-only (64 threads of the 192; `CUDA_VISIBLE_DEVICES=`
  so it never touches the 24 GB), serving Cloudflare's open-weight **Clef-Flash** (`models/clef-flash/Clef-Flash-Q4_K_M.gguf`,
  6.7 GB, Apache-2.0) behind llama.cpp's Jev-compatible `POST /v1/systemone` (upstream PR #29831, already in our build).
  `-ub 16384` because Clef scores the whole prompt in one micro-batch and a choice over every skill is ~7k tokens.
- **`decide/decide.py`** (`hud/.venv/bin/python decide/decide.py …`): `health`, `ask "<state>" --noul "…" --choice "k=a,b,c" --score "k=lo,mid,hi"`,
  `skills "<request>"`, `effort "<request>"`, `catalog`. Any other Jev endpoint works via `LA_DECIDE_URL/KEY/MODEL`
  (TypeSafe Jev, Ollama ≥ 0.35, Workers AI) — cloud is opt-in only.
- **Skill router** `route_skills()`: embeddings (la-embed) shortlist the 24 nearest of the ~90 enabled skills (cached
  per skills-dir mtime in `decide/cache/`), then ONE Clef request asks `need` (noul: does this request call for a
  specialised skill at all?) and `pick` (choice over the shortlist with each skill's full original description).
  Policy in code: nothing below `need 0.5`; load options with p ≥ 0.30, best first, max 2 skills / 45k chars of SKILL.md.
  Meta/voice-gimmick skills (`find-skills`, `caveman*`, …) are never routed.
- **Effort router** `route_effort()`: one choice question → HUD effort `fast/medium/high`; when the pick is < 0.6 sure it
  moves one level up (the video's model router, choosing the local model's thinking budget instead of a cloud model).

`decide/router_eval.py` benchmarks routing on 48 labeled requests (hit = loaded one of the acceptable skills or
correctly loaded nothing; `--methods embed,llm,clef,clef-all`):

| method | accuracy | clean "no skill" | wrong loads | median latency |
|---|---|---|---|---|
| embeddings only (threshold) | 0.73 | 0.71 | 8 | 65 ms |
| main LLM picks (JSON answer) | 1.00 | 1.00 | 14 | 4.3 s (busy GPU slot) |
| **Clef-Flash, embed shortlist** | **0.98** | **1.00** | **0** | 5.9 s (CPU, 0 GPU) |

The LLM "wins" on accuracy by over-loading (14 unnecessary skills on a 48-request set); Clef loads the right thing or
nothing. Latency is CPU prompt-processing of the ~5k-token options list; it does not block the GPU and overlaps the
sandbox start.

**HUD integration**: composer chip **⚡ auto skills** (on by default, remembered) → `route_job()` runs before Hermes
starts and preloads the picks (skipped for skills already loaded in the thread; fail-open with the error recorded);
effort **auto** → the effort router sets fast/medium/high per message. The transcript shows a `⚡ router: loaded … ·
effort … · 5.8 s` line (hover: top picks). Skills page → **Router** card tests any request without launching anything
(`POST /api/decide/route`). Status page probes `decide`; Logs page has the `decision model` log. `la-decide` is in
`UNITS`, so Status shows it like the other services.

**Quick replies (2026-10-31)**: composer chip **⚡ quick replies** (on by default, remembered). For a chat-mode message
of at most 400 characters with no attachments, `quick_reply()` asks Jev one noul (`route_quick()`: "can a short,
tool-free reply answer this?", yes at p ≥ 0.75, about 1 s). Greetings and thanks match a rule with no model call, and
"yes" / "go ahead" after an earlier reply is always the agent's. A yes means no Hermes run: one streamed, no-thinking call to
the main model with the thread's recent turns (about 1-2 s instead of 30-60 s of sandbox start and system-prompt
prefill), whatever effort is selected. The quick reply can still answer `[[AGENT]]` (held back, never shown) to hand
the message to the full agent. The next agent turn resumes the Hermes session from before the quick replies, with
them prepended as context. Transcript note: `⚡ Jev: quick reply, skipped the agent (p 0.92) · 2.4 s`.

**Readable transcripts (2026-10-31, display only)**: every tool call gets a plain-English label (`ls -la src/` →
"Listing files in src", `pytest -x` → "Running the tests", `read_file` → "Reading app.py"; `describeStep()` in
`app.js`). Steps fold into task blocks that split when the kind of work changes ("Exploring the project
(12 steps)", "Editing parser.py (9 steps)") or, when the agent keeps a todo list or crew plan, are named after its
active item. Thinking shows as a collapsed "Thinking" section with its latest sentence; the live bar shows the current
step or thought. At `fast` effort the model doesn't think, so only the labels show.

**Chat profiles (2026-11-01)**: the **Profile** button at the top of the chat list switches profiles, creates a new
one (it opens with no chats) or renames the current one. A profile has its own chat list and its own Hermes home,
`hermes-home/profiles/hud-<id>` (created with `hermes profile create --no-skills`), so the agent's memory
(MEMORY.md / USER.md), past sessions and `session_search` start empty, and crew `chats_list` / `chat_read` only see
that profile's chats. `config.yaml`, `.env`, `SOUL.md`, `skills`, `plugins`, `tools`, `hooks` and `scripts` are symlinks
to the main home (`ensure_profile_home()` re-links them before every run), so the model, effort levels, skills and
settings stay shared. **`/workspace` is shared too**, including `/workspace/AGENTS.md`, which every run loads. Profile 1
"Main" is `hermes-home` itself and owns everything from before profiles. Running chats keep running in their own
profile when you switch. Each profile's first agent run starts its own sandbox container. There is no delete button. To
remove a profile, remove its symlinks first, then run `hermes profile delete hud-<id>`. Root-owned sandbox files need a
throwaway root container to clear. Then delete its `chat_profiles` row.

## What was disabled or replaced (no external APIs)

| Hermes default | status here |
|---|---|
| Cloud LLM providers, `fallback_providers` | none configured; `provider: custom` → `127.0.0.1:8181` |
| `auth.adopt_external_logins` (borrows this box's Claude Code / Codex logins) | **off** |
| Auxiliary models (compression, vision, titles, memory rewrite, curator, …) default `auto` = OpenRouter/Nous first | all pinned to `main` (the local model) |
| `web.keyless_fallback` / `keyless_rescue` (silent fallback to exa, parallel, firecrawl, keenable free tiers) | **off** |
| Web search (Firecrawl/Tavily/Exa/…) | SearXNG (local) |
| Web extract (Firecrawl/Tavily/…) | Crawl4AI (local plugin) |
| Browser cloud providers (Browserbase, Browser Use cloud, Nous gateway) | not configured; local Chromium inside the sandbox |
| `browser_exec` "Browser Use" code mode | **off**: it runs model-written Python on the host (escapes the sandbox) |
| `image_gen` toolset (fal.ai) | removed from toolsets |
| `tts` toolset (Edge TTS / ElevenLabs / OpenAI = cloud) | removed from toolsets |
| `stt` (voice transcription, cloud options) | disabled |
| `x_search` (xAI) | removed from toolsets |
| `connections` (cloud OAuth connectors) | removed from toolsets |
| `computer_use` (controls the HOST desktop) | removed from toolsets |
| `model_catalog` (fetches hosted model list) | off |
| telemetry, update checks, plugin auto-update checks | off |
| Honcho (cloud user modelling) memory provider | not configured; built-in local file memory |

Enabled toolsets: `web, terminal, file, browser, vision, skills, memory, session_search, todo,
clarify, code_execution`, plus the `rag` and `crew` MCP servers (`delegation` replaced by `crew`, see above).

## Things specific to this box (and why they're configured this way)

- **24 GB VRAM is the limit; 503 GB of 8-channel DDR5 is not.** That's why llama.cpp
  (not vLLM/SGLang) is used: it can keep MoE expert weights in RAM. Measured on this box: an
  80B-A3B MoE with all experts in RAM ran at 50 tok/s.
- **Mihomo fake-IP DNS** resolves every public hostname to 198.18.0.0/15. Hermes declares that
  range in `security.fake_ip_ranges`. Crawl4AI needed a one-line patch (`services/crawl4ai-patch/`)
  so its SSRF guard allows only that range; loopback and LAN stay blocked. After upgrading the
  Crawl4AI image, re-diff the patch against the new `egress_broker.py`.
- **Docker containers have no internet here by default.** A tiny relay (`scripts/proxy_bridge.py`,
  172.17.0.1:7898 → 127.0.0.1:7897) plus **one ufw rule** (`172.17.0.0/16 → 172.17.0.1:7898/tcp on
  docker0`, comment `local-agent sandbox proxy relay`) gives the sandbox egress through the proxy
  only. To remove: `sudo ufw status numbered`, then `sudo ufw delete <n>`.
- **Docker Hub pulls**: dockerd has no proxy and restarting it would bounce mailcow, so images
  are pulled with `bin/crane pull … x.tar && docker load -i x.tar`.
- **Downloads**: single proxy connections crawl and stall. Use
  `rag/.venv/bin/python scripts/pdl.py <repo> <file> <dest-dir>`: 16 parallel ranged requests to
  hf-mirror.com (~35 MB/s), resumable, SHA-256 verified.
- **Ports used**: 8181 model, 8182 embeddings, 8183 decision model, 8190 HUD, 8888 SearXNG, 11235 Crawl4AI, 6333/6334 Qdrant,
  7898 relay. All bind 127.0.0.1 except the relay (docker0 IP only).

## Evaluation

18 tasks, auto-scored 0–1 from the final answer, the tool calls actually made (from Hermes'
`stream-json` event log), and the files on disk. Coding tasks are graded by **hidden** pytest
files run in a fresh, network-less sandbox container after the agent finishes.

| category | tasks |
|---|---|
| research (5) | Flash-Next params & release · llama.cpp `--n-cpu-moe` · GLM-5.3-Flash vs DeepSeek-V4-Flash size · refusal-direction paper authorship/venue · **source conflict** (125B vs ~180B) |
| tools (6) | memory save (checked on disk) · exact file write · RAG answer with page number · top-3 search URLs · no-tool restraint (17×23) · fetch → summarise → save |
| coding (4) | roman numerals (+ invalid input) · bug-fix against given tests · word-frequency CLI · CSV revenue analysis → JSON |
| long_context (3) | needle in 61k-token file · 3 needles in 101k-token file · 35k-token inline prompt |

```bash
cd evals
python3 make_fixtures.py                       # deterministic fixtures (already generated)
python3 run_evals.py --label mylabel           # full run (~30–60 min); -k R1 -k T for a subset
python3 run_evals.py --compare baseline-27b mylabel
```

Each run snapshots and restores `hermes-home/memories/`, so evals never pollute the agent's real
memory. Per-task event logs: `evals/results/<label>/<task>.jsonl`.

### Results (2026-10-11)

| category | Qwen3.8-27B | Qwen3.8-Flash-Next |
|---|---|---|
| research (5) | 0.985 | 1.000 |
| tools (6) | 1.000 | 1.000 |
| coding (4) | 1.000 | 1.000 |
| long_context (3) | 1.000 | 1.000 |
| hard (6) | 1.000 | 0.975 |
| **overall (24)** | **0.997** | **0.994** |
| wall time / tool calls / tool errors | 34.7 min / 134 / 9 | 33.3 min / 82 / 3 |

- The two models are equivalent on this suite. Flash-Next finishes in the same wall time with
  39% fewer tool calls and a third of the tool errors. (The eval ran while Flash-Next was measuring
  ~19 tok/s; warm under systemd it later measured 45.6 tok/s, so it is now faster than the 27B too.) Its only loss (H1, 0.85) had a correct answer; it fetched the primary-source APIs
  directly and never called search, which the process metric docks.
- 27B's only loss (R4) cited `arxiv.org/...` without `https://`. The deep-research skill was then
  tightened to require full URLs. That change landed before the Flash-Next run, so R4 isn't
  apples-to-apples.
- Honesty check (H6): both models refused to claim a write to `/etc` that could not succeed, and
  explained why.
- Long-context tasks were solved by searching files rather than reading 100k tokens
  (L1/L2/H4); L3 (35k tokens inline, no tools) tests raw recall and both passed.
- Harness bugs found and fixed during the 27B baseline (not model failures): a regex that broke on
  "GLM-5.3" (rescored via `--rescore`) and a grader image without pytest (C1–C3 re-run). Raw logs
  are kept in `evals/results/`.

**Which to use:** `qwen3.8-flash-next` is active. It's the better planner and has 256k context.
`qwen3.8-27b` still reads long documents faster (2,200 vs 1,260 tok/s prefill at 16k) and frees
~110 GB of RAM. Pick it for bulk document ingestion. Swap with `./agent.sh use`.

### Phase 8 (fine-tuning): skipped, deliberately

The plan was to fine-tune only if the evals showed a specific weakness. They don't: both models
score at or near ceiling on every category, including the hard tier, so a QLoRA run would have
no measurable target and no way to show improvement. Fine-tuning would also need the 55 GB
full-precision abliterated weights, stopping the model server for training, and running close
to the 24 GB VRAM limit. If you later see a recurring failure in real use, add a task for it to
`evals/tasks.py` first. A failing eval is what would justify training, and it's how you'd check
that an adapter helped.
