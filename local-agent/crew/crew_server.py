#!/usr/bin/env python3
"""crew — MCP server that lets a Hermes agent run a crew of specialist agents through the local-agent HUD.

Hermes starts this as a stdio subprocess (hermes-home/config.yaml -> mcp_servers.crew). Every tool is a thin
call to the HUD's loopback-only internal API (hud/app.py, /internal/*), which actually spawns the child
Hermes processes, schedules them against the model's parallel slots, stores plans / progress / background
tasks, and shows all of it live on the dashboard.

Which agent is calling is discovered from the environment of the Hermes process that spawned us
(LA_JOB_ID / LA_ROOT_ID / LA_DEPTH, set by the HUD). A plain `hermes chat` from the terminal has none,
so the first call adopts a synthetic "CLI session" mission in the HUD.

Depth 0 (a lead) gets the full tool set; depth >= 1 (a crew member) only gets progress, clock and bg_*.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path("/mnt/data/local-agent")
HUD_URL = os.environ.get("LA_HUD_URL", "http://127.0.0.1:8190")
TOKEN_FILE = ROOT / "hud/data/internal.token"
MAX_WAIT = 300   # Hermes kills concurrently-run tool calls at 420 s (agent/tool_executor.py); stay well under


def _ancestor_env() -> dict[str, str]:
    """LA_* variables from the nearest ancestor process that has them (Hermes filters the env it gives us)."""
    found = {k: v for k, v in os.environ.items() if k.startswith("LA_")}
    if "LA_JOB_ID" in found:
        return found
    pid = os.getppid()
    for _ in range(8):
        try:
            raw = Path(f"/proc/{pid}/environ").read_bytes()
        except Exception:
            break
        env = dict(kv.split("=", 1) for kv in raw.decode(errors="replace").split("\0") if "=" in kv)
        if "LA_JOB_ID" in env:
            return {k: v for k, v in env.items() if k.startswith("LA_")}
        try:
            ppid = next(l for l in Path(f"/proc/{pid}/status").read_text().splitlines() if l.startswith("PPid:"))
            pid = int(ppid.split()[1])
        except Exception:
            break
        if pid <= 1:
            break
    return found


ENV = _ancestor_env()
JOB = int(ENV.get("LA_JOB_ID", 0) or 0)
ROOT_JOB = int(ENV.get("LA_ROOT_ID", JOB) or JOB)
DEPTH = int(ENV.get("LA_DEPTH", 0) or 0)


def _token() -> str:
    try:
        return TOKEN_FILE.read_text().strip()
    except Exception:
        return ""


def call(path: str, body: dict | None = None, timeout: float = 30) -> dict:
    global JOB, ROOT_JOB
    if not JOB and path != "/internal/adopt":
        r = call("/internal/adopt", {"pid": os.getppid(), "title": f"CLI session (pid {os.getppid()})"})
        JOB = ROOT_JOB = int(r["job"])
    body = dict(body or {})
    body.setdefault("job", JOB)
    body.setdefault("root", ROOT_JOB)
    body.setdefault("depth", DEPTH)
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as c:
            r = c.post(HUD_URL + path, json=body, headers={"x-internal-token": _token()})
    except httpx.HTTPError as e:
        return {"error": f"HUD unreachable ({type(e).__name__}): the dashboard service (la-hud) must be running"}
    try:
        data = r.json()
    except Exception:
        data = {"error": r.text[:500]}
    if r.status_code >= 400 and "error" not in data:
        data = {"error": data.get("detail") or r.text[:500], "status": r.status_code}
    return data


def J(x) -> str:
    return json.dumps(x, ensure_ascii=False, indent=1)


def build_server():
    from mcp.server.mcpserver import MCPServer

    lead = DEPTH == 0
    server = MCPServer(name="crew", instructions=(
        "Crew tools: run specialist agents in parallel (lead only), publish the mission plan and progress to the "
        "owner's dashboard, and manage background tasks that outlive a turn. All state lives in the HUD."))

    # ---- everyone
    @server.tool()
    def progress(message: str) -> str:
        """Post a one-line status update to the mission timeline on the owner's dashboard. Use it at meaningful
        transitions (plan step started/finished, wave spawned, tests red/green), not for every tool call."""
        return J(call("/internal/progress", {"message": message[:500]}))

    @server.tool()
    def clock() -> str:
        """Mission timing and capacity: elapsed time, remaining run budget and turns for this agent, free model
        slots, how many crew members are running or queued, and background tasks still running."""
        return J(call("/internal/clock"))

    @server.tool()
    def bg_start(name: str, command: str, timeout_s: int = 3600) -> str:
        """Start a long-running shell command in a fresh sandbox container on /workspace (same image, same mounts,
        internet via the proxy). Returns a task_id. It keeps running after your turn ends and is shown on the
        dashboard with timing and exit code. Use for test suites, builds, installs, data jobs, crawls. Stop it with
        bg_stop when you no longer need it."""
        return J(call("/internal/bg/start", {"name": name[:80], "command": command, "timeout_s": int(timeout_s)}, timeout=60))

    @server.tool()
    def bg_status(task_id: str = "") -> str:
        """State of one background task (or all tasks of this mission if task_id is empty): running/exited, exit
        code, elapsed seconds, and the last lines of output."""
        return J(call("/internal/bg/status", {"task_id": task_id}))

    @server.tool()
    def bg_logs(task_id: str, tail: int = 200) -> str:
        """The last `tail` lines of a background task's combined stdout/stderr."""
        return J(call("/internal/bg/logs", {"task_id": task_id, "tail": int(tail)}))

    @server.tool()
    def bg_stop(task_id: str) -> str:
        """Kill a background task."""
        return J(call("/internal/bg/stop", {"task_id": task_id}))

    @server.tool()
    def repo_map(path: str = "/workspace", max_chars: int = 12000, focus: str = "") -> str:
        """A compact symbol map of a directory (files with line counts; classes/functions with line numbers), like
        Aider's repo map. Use it BEFORE reading files: find the file and line range you need, then read_file with
        offset/limit. `focus` ranks paths containing that substring first; raise max_chars for big trees."""
        sys.path.insert(0, str(ROOT / "agents"))
        from repomap import repo_map as _rm
        host = Path(path.replace("/workspace", str(ROOT / "workspace"), 1) if path.startswith("/workspace") else path).resolve()
        if not str(host).startswith(str((ROOT / "workspace").resolve())):
            return "error: path must be under /workspace"
        if not host.is_dir():
            return f"error: {path} is not a directory"
        return _rm(host, max(2000, min(int(max_chars), 60000)), focus).replace(str(ROOT / "workspace"), "/workspace")

    @server.tool()
    def checkpoint(label: str) -> str:
        """Snapshot the whole /workspace into the mission's shadow git (no .git in the workspace). Do it before a
        risky edit, after a green test run, and before handing files to another member. Returns the checkpoint id
        and what changed since the previous one. The HUD also checkpoints automatically at mission start and before
        every spawn."""
        return J(call("/internal/checkpoint", {"label": label[:120]}, timeout=180))

    @server.tool()
    def checkpoints() -> str:
        """List this mission's checkpoints (id, time, label, which agent made it)."""
        return J(call("/internal/checkpoints"))

    @server.tool()
    def diff(from_checkpoint: str, to_checkpoint: str = "", paths: list[str] | None = None, max_chars: int = 60000) -> str:
        """Unified diff of the workspace between a checkpoint and now (or between two checkpoints), optionally
        limited to paths. This is how a reviewer sees exactly what a mission changed: diff(<mission-start id>)."""
        return J(call("/internal/diff", {"from_checkpoint": from_checkpoint, "to_checkpoint": to_checkpoint,
                                         "paths": paths or [], "max_chars": int(max_chars)}, timeout=120))

    @server.tool()
    def chats_list(query: str = "", limit: int = 20) -> str:
        """The owner's other conversations on this dashboard (newest first): id, date, mode, number of turns, title.
        `query` filters by words in the owner's messages or the answers. Use it when the owner refers to something
        from another chat ("like we discussed", "that project", "the list from yesterday"), then chat_read(id)."""
        return J(call("/internal/chats_list", {"query": query[:200], "limit": int(limit)}))

    @server.tool()
    def chat_read(chat_id: int, max_chars: int = 20000) -> str:
        """Read one earlier conversation: every turn's owner message and the final answer (tool noise left out),
        oldest first; compacted summaries included. Long threads are trimmed from the oldest turns."""
        return J(call("/internal/chat_read", {"chat_id": int(chat_id), "max_chars": int(max_chars)}))

    # ---- desktop + Remote Control (everyone; HUD /internal/{host_run,screenshot,remote_control}, 2026-11-03)
    @server.tool()
    def host_run(tool: str, args: list[str] | None = None, timeout_s: int = 600, wait_s: int = 120) -> str:
        """Run a toolbox HOST script on the owner's real desktop (screen, GPU, Lunar Client, ~/.minecraft) instead of
        the sandbox. `tool` = a file name in /mnt/data/la-toolbox/host/ (write it from the sandbox, chmod +x, then
        `tb register <name> --where host`). Confined: secrets hidden, home read-only except game dirs, own process
        space (it cannot kill other programs). Waits up to wait_s (≤540) and returns exit code + log tail; longer runs
        return a run_id for host_run_status. Pair with screenshot() to see what happened on screen."""
        return J(call("/internal/host_run", {"tool": tool, "args": args or [], "timeout_s": int(timeout_s),
                                             "wait_s": int(wait_s)}, timeout=600))

    @server.tool()
    def host_run_status(run_id: str) -> str:
        """Result of an earlier host_run that was still running (exit code, seconds, log path, last output)."""
        return J(call("/internal/host_run_status", {"run_id": run_id}))

    @server.tool()
    def screenshot(region: str = "") -> str:
        """Screenshot of the owner's live desktop (all monitors, or region "x,y WxH"), saved under
        /workspace/outbox/screens/. Look at it with vision_analyze. Use it to verify GUI work (did Lunar/Minecraft
        reach the title screen? did the mod's menu appear?)."""
        return J(call("/internal/screenshot", {"region": region}, timeout=60))

    @server.tool()
    def remote_control(action: str = "status", force: bool = False, reason: str = "") -> str:
        """Claude Code Remote Control on this machine (systemd user unit claude-rc, 'home server', permission mode
        bypassPermissions = dangerously skip permissions). action: "status" (health: running, logged in, connected),
        "start" (turn it on if it is off — safe), "restart" (only when it is unhealthy/offline; a healthy one is only
        restarted with force=true when the owner asked, because it cuts off live sessions). If it says logged out, tell
        the owner to run `claude auth login` on the desktop — never try to log in yourself."""
        return J(call("/internal/remote_control", {"action": action, "force": bool(force), "reason": reason[:300]}, timeout=90))

    # ---- Claude Code delegation (everyone; gated by claude/gate.py + the local judge, limits in claude/config.json)
    @server.tool()
    def claude_check(task: str, context: str = "", files: list[str] | None = None, model: str = "", effort: str = "", mode: str = "edit") -> str:
        """Dry-run the outbound gate WITHOUT sending anything: returns ok/blocked, the reasons, what would be redacted, and the
        exact brief that would go to Claude Code. Use it to fix a brief that claude_run refused. The local judge model is
        not consulted here (it runs inside claude_run)."""
        return J(call("/internal/claude/check", {"task": task, "context": context, "files": files or [], "model": model, "effort": effort, "mode": mode},
                      timeout=60))

    @server.tool()
    def claude_run(task: str, context: str = "", files: list[str] | None = None, model: str = "", effort: str = "", mode: str = "edit",
                   label: str = "", timeout_s: int = 0, max_turns: int = 0) -> str:
        """Delegate ONE well-defined piece of work to Claude Code (the owner's logged-in `claude` CLI, a much stronger coder),
        running on the same /workspace. Use it for the central, hardest piece of a mission — an algorithm, a tricky bug, a
        large refactor, a design review — not for routine edits you can do yourself. The request goes through a strict
        outbound gate first: secrets and personal data are redacted, descriptors and profanity removed, manipulation /
        policy talk / abuse patterns BLOCK it, and the local judge must approve. Blocked = nothing was sent; read
        `blocked_by`, rephrase, retry (claude_check is the free dry-run). Write the brief like a ticket for a contractor:
        `task` = the deliverable and acceptance criteria (plain engineering language, ≥ 40 chars); `context` = paths,
        interfaces to respect, decisions made, what not to touch; `files` = workspace paths (files or directories) to start from — Claude works in a staged copy holding ONLY these plus what it
        creates (merged back afterwards), so list every directory it must see; write paths workspace-relative (`src/app.py`), never `/workspace/...`.
        `model`: "opus" (default; strongest general coder) or "fable" (deepest reasoning; architecture, subtle bugs, maths).
        `effort`: low | medium | high (default) | xhigh | max — match the difficulty, not the size. `mode`: "edit" (it changes
        files in /workspace, runs tests in a sandbox) or "readonly" (analysis / design / review only; no file changes).
        Returns immediately with task_id, the gate's redactions/warnings and `brief_sent` (exactly what Claude received).
        Then claude_wait(task_id). The HUD checkpoints before and after, so diff(<before>, <after>) shows its changes."""
        return J(call("/internal/claude/run", {"task": task, "context": context, "files": files or [], "model": model, "effort": effort, "mode": mode,
                                               "label": label[:60], "timeout_s": int(timeout_s), "max_turns": int(max_turns)}, timeout=400))

    @server.tool()
    def claude_status(task_id: str = "") -> str:
        """State of one Claude run (or all runs of this mission if task_id is empty): running/done/failed/timeout/killed, elapsed,
        turns and tools used so far, the last tool, cost, and the last lines of its output."""
        return J(call("/internal/claude/status", {"task_id": task_id}))

    @server.tool()
    def claude_wait(task_id: str = "", timeout_s: int = 300) -> str:
        """Block until the Claude run (or all runs of this mission) finishes or timeout_s passes (max 300). A timeout is NOT
        an error: call again (alone, not batched with other tools). Finished runs come back with the full report, the list of
        changed files (status + path) and a diff_hint you can pass to diff() to review exactly what changed before you rely on it."""
        t = max(5, min(int(timeout_s), MAX_WAIT))
        return J(call("/internal/claude/wait", {"task_id": task_id, "timeout_s": t}, timeout=t + 30))

    @server.tool()
    def claude_result(task_id: str) -> str:
        """Full report of a finished Claude run: its Result / Files changed / Verification / Open issues, the changed-file list
        and the checkpoint ids to diff. Then verify its claims yourself (run the tests) before building on them."""
        return J(call("/internal/claude/result", {"task_id": task_id}))

    @server.tool()
    def claude_stop(task_id: str) -> str:
        """Stop a Claude run that is taking too long or heading the wrong way. Its partial changes stay in /workspace;
        rollback(<checkpoint before>) undoes them."""
        return J(call("/internal/claude/stop", {"task_id": task_id}))

    if not lead:
        return server

    @server.tool()
    def rollback(checkpoint: str, paths: list[str] | None = None) -> str:
        """Restore the workspace (or only `paths`) to a checkpoint. A 'before rollback' checkpoint is taken first,
        so this is itself reversible. Whole-workspace rollback also removes files created after the checkpoint
        (uploads, .venv, node_modules and other excluded paths are never touched). Lead only."""
        return J(call("/internal/rollback", {"checkpoint": checkpoint, "paths": paths or []}, timeout=180))

    @server.tool()
    def state_save(summary: str) -> str:
        """Save a compact mission state for resumption (compaction / handoff): goal, acceptance criteria, decisions
        made and why, files touched, what is verified done, what is left in order, open problems, the key commands.
        It is shown on the dashboard, written to /workspace/.crew/mission-<id>.md, and prepended automatically to
        the next follow-up run of this mission. Save it when ~70% of your budget or context is used, before a long
        wait, and always before your final report on a long mission."""
        return J(call("/internal/state_save", {"summary": summary}))

    @server.tool()
    def state_get() -> str:
        """The last saved mission state (what a previous run of this mission left for you)."""
        return J(call("/internal/state_get"))

    # ---- lead only
    @server.tool()
    def roles() -> str:
        """The crew roster: every role you can spawn, its class, what it is for, when to use it, and its default
        turn/time budget."""
        return J(call("/internal/roles"))

    @server.tool()
    def plan_set(steps: list[dict]) -> str:
        """Publish (replace) the mission plan shown on the dashboard. steps = [{"id": "s1", "title": "...",
        "detail": "optional"}]. 3-12 imperative, verifiable steps. Steps whose id already exists keep their status."""
        return J(call("/internal/plan_set", {"steps": steps}))

    @server.tool()
    def plan_update(step_id: str, status: str, note: str = "") -> str:
        """Set a plan step's status: pending | active | done | blocked | skipped. Timing per step is recorded from
        these transitions. Add a short note for blocked/skipped (why) or done (the evidence line)."""
        return J(call("/internal/plan_update", {"step_id": step_id, "status": status, "note": note[:500]}))

    @server.tool()
    def deliver(summary: str, paths: list[str], version: str = "", how_to_run: str = "") -> str:
        """Hand the owner a USABLE snapshot of the product (lead only). Required whenever the `deliverable` field in
        clock/wait/status/progress results says it is due, and once more before the final report. Call it only when the
        listed files run end to end from a clean copy with every check green. `summary` = what works, what is not in yet,
        the check results (>= 20 chars). `paths` = workspace files/directories that make up the product — include the
        README with exact install/run/test commands. `version` = short tag like v0.2. `how_to_run` = the exact commands.
        The HUD checkpoints, copies the paths to /workspace/outbox/deliveries/<mission>/<time>-vN/ (+ DELIVERY.md), zips
        it as a download card for the owner, and restarts the countdown. Returns the zip path — quote it in `progress`."""
        return J(call("/internal/deliver", {"summary": summary[:4000], "paths": paths or [], "version": version[:40],
                                             "how_to_run": how_to_run[:4000]}, timeout=300))

    @server.tool()
    def spawn(role: str, task: str, context: str = "", files: list[str] | None = None, label: str = "",
              max_turns: int = 0) -> str:
        """Start a crew member (a specialist agent) that works concurrently with you. Returns immediately with its
        agent_id. `task` = the deliverable and acceptance criteria; `context` = everything it needs to know (it
        knows nothing about this conversation): paths, decisions made, interfaces to respect, what not to touch,
        what others are doing. `files` = workspace paths to start from. `label` = dashboard name. Beyond the free
        model slots it queues and starts automatically."""
        return J(call("/internal/spawn", {"role": role, "task": task, "context": context, "files": files or [],
                                          "label": label[:60], "max_turns": int(max_turns)}, timeout=60))

    @server.tool()
    def status(agent_ids: list[int] | None = None) -> str:
        """Live table of your crew members (all of them if agent_ids is empty): state, elapsed, turns used, tool
        calls, last tool and how long ago, tokens."""
        return J(call("/internal/status", {"agent_ids": agent_ids or []}))

    @server.tool()
    def wait(agent_ids: list[int] | None = None, timeout_s: int = 300) -> str:
        """Block until the listed members (default: all running/queued ones) finish or timeout_s passes (max 300).
        Returns each member's state and, for finished ones, the full final report. A timeout is NOT an error: it
        returns timed_out=true with still_running ids — just call wait again (alone, not batched with other tools).
        Waiting costs no tokens and frees your model slot for them."""
        t = max(5, min(int(timeout_s), MAX_WAIT))
        return J(call("/internal/wait", {"agent_ids": agent_ids or [], "timeout_s": t}, timeout=t + 30))

    @server.tool()
    def result(agent_id: int) -> str:
        """Full final report of one crew member (works for finished, failed and cancelled members)."""
        return J(call("/internal/result", {"agent_id": int(agent_id)}))

    @server.tool()
    def cancel(agent_id: int) -> str:
        """Stop a crew member that is stuck, redundant or off-task."""
        return J(call("/internal/cancel", {"agent_id": int(agent_id)}))

    return server


def main() -> None:
    if sys.argv[1:] == ["--probe"]:
        print(J({"job": JOB, "root": ROOT_JOB, "depth": DEPTH, "clock": call("/internal/clock")}))
        return
    build_server().run("stdio")


if __name__ == "__main__":
    main()
