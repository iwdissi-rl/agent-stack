"""local-agent HUD — remote control for the local Hermes stack, served at https://EXAMPLE_HOST/llm/.

nginx (127.0.0.1:8080) strips the /llm prefix and proxies here (127.0.0.1:8190).

Security model
- One owner account; argon2id hash in data/auth.json (mode 600). Set with set_password.py.
- Login throttling: 10 failures per IP per 15 min, 30 failures globally per hour -> temporary lockout
  (nginx adds its own 5 req/min limit in front).
- Session cookie: random 256-bit token, only its SHA-256 is stored; HttpOnly, Secure (behind https),
  SameSite=Strict, Path=/llm; 12 h idle / 7 d absolute expiry.
- Every state-changing request needs the per-session CSRF token header AND an allowed Origin.
- There is no raw shell endpoint. Commands start Hermes (its terminal/file/code/browser tools run in
  the Docker sandbox), the benchmark script, or the eval runner. Tuning writes only whitelisted keys
  with validated values to profiles/*.env.
- /internal/* is the crew API used by the `crew` MCP server inside Hermes: loopback only, no proxy
  headers allowed (so it is unreachable through nginx), 256-bit token in data/internal.token.

Crew model (2026-10-13)
- A job has a mode (chat | coding | research | data) and optionally a role. Modes other than chat run
  the `orchestrator` role (agents/roles/orchestrator.md) which can spawn crew members = more Hermes
  processes with a specialist system prompt (HERMES_EPHEMERAL_SYSTEM_PROMPT), all against the same
  llama-server, which runs PARALLEL slots (profile key) with a unified KV pool.
- The scheduler runs up to `slots` agent jobs at once; children are scheduled before new top-level
  jobs; bench/eval jobs run alone.
- Plan steps, progress notes and background tasks are attached to the mission root job and streamed
  to the UI as `mission` messages.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError
from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path("/mnt/data/local-agent")
HUD = ROOT / "hud"
DATA = HUD / "data"
PROFILES = ROOT / "profiles"
LOGS = ROOT / "logs"
HERMES = ROOT / "hermes-agent/.venv/bin/hermes"
HERMES_HOME = ROOT / "hermes-home"
WORKSPACE = ROOT / "workspace"
# System One decisions (decide/decide.py): local Clef-Flash on :8183 picks skills/effort per job (the Jev idea, run locally)
sys.path.insert(0, str(ROOT / "decide"))
try:
    import decide as decider
except Exception as _e:  # the HUD must start even if the router is broken
    decider = None
    print(f"decide module unavailable: {_e}", file=sys.stderr)
EVALS = ROOT / "evals"
RAG_PY = ROOT / "rag/.venv/bin/python"
UPLOADS = WORKSPACE / "uploads"          # = /workspace/uploads inside the sandbox
UPLOADS.mkdir(exist_ok=True)
SANDBOX_IMAGE = "nousresearch/hermes-sandbox:desktop"
# Background-task containers start from the bare image (the long-lived Hermes sandbox container is NOT shared),
# so they get their own persistent home: `pip install --user` there survives across tasks (pre-seeded with pytest).
BG_HOME = HERMES_HOME / "sandboxes/docker/bg-home"
# Same host mounts as the Hermes sandbox (hermes-home/config.yaml terminal.docker_volumes/docker_extra_args):
# all of /mnt/data read-write, this stack read-only, its secrets/state hidden, keys.env blanked.
MNT_MASK_EMPTY = ROOT / "data/masks/empty"
MNT_MOUNTS = ["-v", "/mnt/data:/mnt/data", "-v", f"{ROOT}:{ROOT}:ro", "-v", f"{WORKSPACE}:{ROOT}/workspace",
              "-v", f"{MNT_MASK_EMPTY}:/mnt/data/keys.env:ro",
              "--tmpfs", f"{ROOT}/hud/data:ro", "--tmpfs", f"{ROOT}/hermes-home:ro", "--tmpfs", f"{ROOT}/claude-runs:ro"]
BG_HOME.mkdir(parents=True, exist_ok=True)
# Safety rail: a shadow git repository over /workspace (no .git inside the workspace). Every mission start, every
# spawn and every explicit `checkpoint` call commits the tree; `diff` and `rollback` work against those commits.
CKPT_GIT = HERMES_HOME / "checkpoints/workspace.git"
CKPT_EXCLUDE = ["uploads/", ".venv/", "venv/", "node_modules/", "__pycache__/", ".pytest_cache/", ".mypy_cache/",
                ".ruff_cache/", "*.pyc", ".cache/", "dist/", "build/", "target/", ".next/", "*.log", ".crew/tmp/"]
CREW_DIR = WORKSPACE / ".crew"                       # mission state files the agents can read back
CREW_DIR.mkdir(exist_ok=True)

sys.path.insert(0, str(ROOT / "agents"))
import roles as rolelib  # noqa: E402  (agents/roles.py — role + mode library)
sys.path.insert(0, str(ROOT / "claude"))
import gate as cgate  # noqa: E402  (claude/gate.py — outbound gate for Claude Code delegation)
CLAUDE_BIN = os.environ.get("LA_CLAUDE_BIN") or str(Path.home() / ".npm-global/bin/claude")
LA_BWRAP = "/usr/local/libexec/la-claude/bwrap"

ALLOWED_ORIGINS = {"https://EXAMPLE_HOST", "https://EXAMPLE_HOST", "http://127.0.0.1:8190", "http://localhost:8190"}
COOKIE = "la_session"
IDLE_TTL, ABS_TTL = 12 * 3600, 7 * 24 * 3600
UNITS = ["la-model", "la-embed", "la-decide", "la-relay", "la-services", "la-hud", "la-school", "la-watchdog.timer"]
LOG_FILES = {"model": "model-server.log", "embed": "embed-server.log", "decide": "decide-server.log", "hud": "hud.log",
             "relay": "proxy-bridge.log", "watchdog": "watchdog.log"}
SKILLS = ["deep-research", "sandbox-coding", "document-analysis", "verify-side-effects"]
MAX_AGENTS = 10            # hard ceiling on concurrent agent processes, whatever the profile says
MAX_CHILDREN_PER_MISSION = 40
STEP_STATUSES = {"pending", "active", "done", "blocked", "skipped"}
EFFORTS = ["fast", "medium", "high", "xhigh", "max"]   # -> hermes --provider custom:local-<effort> (thinking controls in config.yaml)
DEFAULT_EFFORT = {"chat": "medium", "coding": "high", "research": "high", "data": "high", "member": "high"}
EFFORT_BRIEF = {
    "fast": "Effort: FAST. Answer directly and concisely; skip deliberation. Use tools only when the answer depends on them.",
    "medium": "",
    "high": "Effort: HIGH. Think the problem through before acting; check your answer once before replying.",
    "xhigh": ("Effort: EXTRA HIGH. Reason carefully and at length: break the problem down, consider at least two approaches, "
              "check edge cases, and verify facts or numbers with tools before you rely on them. Re-read your answer against "
              "the request before sending it."),
    "max": ("Effort: MAX (extended thinking). Take as long as the problem deserves. Think exhaustively before answering: restate the "
            "problem and the success criteria, enumerate approaches and pick the best with reasons, work step by step, actively "
            "look for flaws in your own reasoning, verify every factual claim and every number with tools (search, run code, read "
            "the source) rather than memory, and do a final self-review pass against the request (completeness, correctness, "
            "clarity) before replying. Prefer correctness over speed; state remaining uncertainty explicitly."),
}
FILE_EXCLUDE_TOP = {".crew"}                           # dirs under /workspace never offered for download
POOL_HEADROOM = 0.25              # share of llama-server's KV pool kept free: Hermes checks its window only between turns, so a
                                  # turn's tool results can overshoot AGENT_CTX by 15k+; without headroom every agent dies at once
MAX_BUDGET_S = 24 * 3600          # longest crew mission the composer may ask for
DELIVER_DEFAULT_S = 2 * 3600      # crew missions hand the owner a usable, zipped snapshot this often (per-mission; 0 = off)
DELIVER_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".crew"}
QUICK_URL = "http://127.0.0.1:8181/v1/chat/completions"
QUICK_MAX_CHARS = 400          # quick replies (Jev triage -> one short tool-free model call) only for messages this short
QUICK_MAX_TOKENS = 700
QUICK_HANDOFF = "[[AGENT]]"    # the quick reply's way of saying "this needs the real agent"
QUICK_SYSTEM = ("You are the owner's local assistant on their dashboard, answering in quick-reply mode: a single reply, no tools, "
                "no files, no web. Now: {now}. Reply briefly and directly in markdown, the way a friend who knows a lot would. "
                "If answering properly would need tools, live or current information (news, prices, weather, anything about today), "
                "the owner's files or earlier chats you cannot see here, running code, exact calculation, or more than a short "
                "answer, reply with exactly " + QUICK_HANDOFF + " and nothing else: the full agent takes over.")
AUTO_COMPACT_FRAC = 0.70       # a follow-up whose thread used more than this share of the context is compacted first
COMPACT_PROMPT = """[Compaction request from the dashboard — do not call tools]
Write a compact handoff summary of this ENTIRE conversation so that a fresh session with no memory of it can continue
seamlessly. Markdown, at most ~600 words, these sections in this order:
## Goal & context — what the owner is doing and why; constraints they stated.
## Key facts & decisions — everything established so far that still matters (numbers, names, paths, choices and why).
## Files & artifacts — paths created or changed, commands that work, URLs/sources used.
## Done — what is finished and verified (with the evidence line).
## In progress / next — what was being worked on and the next concrete steps, in order.
## Owner preferences & tone — how they like answers, anything they asked you to remember.
## Open questions — unresolved items, ambiguities, things that failed.
Be concrete and literal; keep exact identifiers, numbers and quotes; drop pleasantries and process narration."""

ph = PasswordHasher()

# ---------------------------------------------------------------- storage

_db_lock = threading.Lock()
db = sqlite3.connect(DATA / "hud.db", check_same_thread=False, isolation_level=None)
db.row_factory = sqlite3.Row
db.executescript("""
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, csrf TEXT, created REAL, last_seen REAL, ip TEXT, ua TEXT);
CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, status TEXT, created REAL,
  started REAL, finished REAL, title TEXT, params TEXT, parent_id INTEGER, session_id TEXT, final TEXT,
  error TEXT, tokens TEXT, exit_code INTEGER);
CREATE TABLE IF NOT EXISTS events (job_id INTEGER, seq INTEGER, ts REAL, type TEXT, data TEXT, PRIMARY KEY (job_id, seq));
CREATE TABLE IF NOT EXISTS audit (ts REAL, ip TEXT, action TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS steps (root_id INTEGER, step_id TEXT, ord INTEGER, title TEXT, detail TEXT, status TEXT,
  note TEXT, started REAL, finished REAL, updated REAL, PRIMARY KEY (root_id, step_id));
CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY AUTOINCREMENT, root_id INTEGER, job_id INTEGER, ts REAL, message TEXT);
CREATE TABLE IF NOT EXISTS bgtasks (id TEXT PRIMARY KEY, root_id INTEGER, job_id INTEGER, name TEXT, command TEXT,
  container TEXT, created REAL, finished REAL, state TEXT, exit_code INTEGER, timeout_s INTEGER, log_tail TEXT);
CREATE TABLE IF NOT EXISTS cruns (id TEXT PRIMARY KEY, root_id INTEGER, job_id INTEGER, created REAL, finished REAL, state TEXT,
  model TEXT, effort TEXT, mode TEXT, label TEXT, brief TEXT, verdict TEXT, pid INTEGER, exit_code INTEGER, final TEXT, usage TEXT,
  cost_usd REAL, served_model TEXT, session_id TEXT, ckpt_before TEXT, ckpt_after TEXT, files TEXT, n_tools INTEGER DEFAULT 0,
  last_tool TEXT, last_tool_ts REAL, error TEXT, timeout_s INTEGER, max_turns INTEGER, num_turns INTEGER, log_tail TEXT,
  out_offset INTEGER DEFAULT 0, denials TEXT);
CREATE TABLE IF NOT EXISTS cgate (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, root_id INTEGER, job_id INTEGER, action TEXT, ok INTEGER,
  model TEXT, effort TEXT, mode TEXT, reasons TEXT, redactions TEXT, judge TEXT, run_id TEXT, task_head TEXT);
CREATE TABLE IF NOT EXISTS notices (id INTEGER PRIMARY KEY AUTOINCREMENT, root_id INTEGER, ts REAL, expires REAL, message TEXT);
CREATE TABLE IF NOT EXISTS deliveries (id INTEGER PRIMARY KEY AUTOINCREMENT, root_id INTEGER, job_id INTEGER, ts REAL, n INTEGER,
  version TEXT, summary TEXT, how_to_run TEXT, path TEXT, zip TEXT, checkpoint TEXT, files INTEGER, bytes INTEGER, skipped TEXT);
""")
for _col, _typ in [("root_id", "INTEGER"), ("mode", "TEXT"), ("role", "TEXT"), ("depth", "INTEGER DEFAULT 0"),
                   ("relation", "TEXT"), ("label", "TEXT"), ("last_tool", "TEXT"), ("last_tool_ts", "REAL"),
                   ("n_tools", "INTEGER DEFAULT 0"), ("budget_s", "INTEGER"), ("max_turns", "INTEGER"), ("mission_state", "TEXT"), ("summary", "TEXT"), ("effort", "TEXT"), ("pid", "INTEGER"), ("out_offset", "INTEGER DEFAULT 0")]:
    try:
        db.execute(f"ALTER TABLE jobs ADD COLUMN {_col} {_typ}")
    except sqlite3.OperationalError:
        pass
try:
    db.execute("ALTER TABLE jobs ADD COLUMN profile_id INTEGER DEFAULT 1")   # chat profiles; everything before them is "Main"
except sqlite3.OperationalError:
    pass
db.execute("CREATE TABLE IF NOT EXISTS chat_profiles (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, slug TEXT, created REAL)")
db.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
db.execute("INSERT OR IGNORE INTO chat_profiles (id, name, slug, created) VALUES (1, 'Main', NULL, ?)", (time.time(),))
for _col, _typ in [("manifest", "TEXT"), ("merge", "TEXT")]:
    try:
        db.execute(f"ALTER TABLE cruns ADD COLUMN {_col} {_typ}")
    except sqlite3.OperationalError:
        pass
db.execute("UPDATE jobs SET root_id=id WHERE root_id IS NULL")
# a job that was running when the HUD died will never finish
def _pid_is_agent(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        return b"hermes" in Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False


# Agent processes survive HUD restarts (their output goes to data/job-N.out.jsonl, la-hud runs with KillMode=process);
# running agent jobs whose process is still alive are reattached in lifespan(); everything else really died.
_REATTACH = [r["id"] for r in db.execute("SELECT id, pid, kind FROM jobs WHERE status='running'").fetchall()
             if r["kind"] == "agent" and _pid_is_agent(r["pid"])]
db.execute(f"UPDATE jobs SET status='failed', error='HUD restarted while running', finished=? WHERE status='running'"
           f"{' AND id NOT IN (' + ','.join(map(str, _REATTACH)) + ')' if _REATTACH else ''}", (time.time(),))


def q(sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    with _db_lock:
        return db.execute(sql, args).fetchall()


def x(sql: str, args: tuple = ()) -> int:
    with _db_lock:
        return db.execute(sql, args).lastrowid


def audit(ip: str, action: str, detail: str = "") -> None:
    x("INSERT INTO audit VALUES (?,?,?,?)", (time.time(), ip, action, detail[:500]))


INTERNAL_TOKEN_FILE = DATA / "internal.token"
if not INTERNAL_TOKEN_FILE.exists():
    INTERNAL_TOKEN_FILE.write_text(secrets.token_urlsafe(32))
    INTERNAL_TOKEN_FILE.chmod(0o600)
INTERNAL_TOKEN = INTERNAL_TOKEN_FILE.read_text().strip()

# ---------------------------------------------------------------- auth

def auth_conf() -> dict:
    p = DATA / "auth.json"
    if not p.exists():
        raise HTTPException(503, "No owner password set. Run hud/set_password.py on the server.")
    return json.loads(p.read_text())


_fail_ip: dict[str, deque] = defaultdict(deque)
_fail_all: deque = deque()


def _prune(d: deque, window: float) -> None:
    now = time.time()
    while d and now - d[0] > window:
        d.popleft()


def client_ip(req: Request | WebSocket) -> str:
    return req.headers.get("x-real-ip") or (req.client.host if req.client else "?")


def check_origin(req: Request | WebSocket) -> None:
    origin = req.headers.get("origin")
    if origin not in ALLOWED_ORIGINS:
        raise HTTPException(403, "bad origin")


def session_for(req: Request | WebSocket) -> sqlite3.Row | None:
    tok = req.cookies.get(COOKIE)
    if not tok:
        return None
    h = hashlib.sha256(tok.encode()).hexdigest()
    rows = q("SELECT * FROM sessions WHERE token_hash=?", (h,))
    if not rows:
        return None
    s, now = rows[0], time.time()
    if now - s["last_seen"] > IDLE_TTL or now - s["created"] > ABS_TTL:
        x("DELETE FROM sessions WHERE token_hash=?", (h,))
        return None
    if now - s["last_seen"] > 60:
        x("UPDATE sessions SET last_seen=? WHERE token_hash=?", (now, h))
    return s


def require(req: Request, write: bool = False) -> sqlite3.Row:
    s = session_for(req)
    if not s:
        raise HTTPException(401, "not signed in")
    if write:
        check_origin(req)
        if not secrets.compare_digest(req.headers.get("x-csrf", ""), s["csrf"]):
            raise HTTPException(403, "bad csrf token")
    return s


def internal(req: Request) -> None:
    """Crew API guard: direct loopback connection (never via nginx) + shared token."""
    host = req.client.host if req.client else ""
    if host not in ("127.0.0.1", "::1") or any(h in req.headers for h in ("x-forwarded-for", "x-real-ip", "x-forwarded-proto")):
        raise HTTPException(404, "not found")
    if not secrets.compare_digest(req.headers.get("x-internal-token", ""), INTERNAL_TOKEN):
        raise HTTPException(403, "bad token")


# ---------------------------------------------------------------- live hub

class Hub:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()

    async def send(self, msg: dict) -> None:
        dead = []
        data = json.dumps(msg)
        for ws in list(self.clients):
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)


hub = Hub()

# ---------------------------------------------------------------- status

_status: dict[str, Any] = {}


def _read_proc() -> dict:
    mem = {l.split(":")[0]: int(l.split()[1]) for l in open("/proc/meminfo") if ":" in l}
    load = open("/proc/loadavg").read().split()[:3]
    return {"load": [float(v) for v in load], "ram_total_gib": round(mem["MemTotal"] / 2**20, 1),
            "ram_avail_gib": round(mem["MemAvailable"] / 2**20, 1), "cpus": os.cpu_count()}


async def _run(*cmd: str, timeout: float = 10, input_bytes: bytes | None = None) -> tuple[int, str]:
    p = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.PIPE if input_bytes else None,
                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(p.communicate(input_bytes), timeout)
    except asyncio.TimeoutError:
        p.kill()
        return 124, "timeout"
    return p.returncode, out.decode(errors="replace")


def read_profile(name: str) -> dict:
    path = PROFILES / f"{name}.env"
    vals = {}
    for line in path.read_text().splitlines():
        m = re.match(r"^([A-Z_]+)=(.*)$", line)
        if m:
            vals[m.group(1)] = m.group(2).strip().strip('"')
    return vals


def active_profile() -> str:
    return os.readlink(PROFILES / "active.env").removesuffix(".env")


def agent_ctx(prof: dict | None = None) -> int:
    """Context window ONE agent may use (= Hermes context_length). llama-server's unified KV pool (-kvu) holds CTX tokens
    shared by every slot, so the per-agent window must be a fraction of it: profile AGENT_CTX, else CTX / PARALLEL."""
    try:
        prof = prof or read_profile(active_profile())
        ctx, par = int(prof.get("CTX", 0)), max(1, int(prof.get("PARALLEL", "1")))
        return max(4096, min(ctx, int(prof.get("AGENT_CTX") or ctx // par)))
    except Exception:
        return 4096


def slots() -> int:
    """How many agent processes may run at once: llama-server slots (profile PARALLEL), but never more than fit in the shared
    KV pool side by side with POOL_HEADROOM spare (0.75*CTX // AGENT_CTX) — agents at 50-80k each blew the 262k pool twice
    and every running turn failed at once."""
    try:
        prof = read_profile(active_profile())
        fit = max(1, int(int(prof.get("CTX", 0)) * (1 - POOL_HEADROOM)) // agent_ctx(prof))
        return max(1, min(MAX_AGENTS, int(prof.get("PARALLEL", "1")), fit))
    except Exception:
        return 1


async def collect_status() -> dict:
    st: dict[str, Any] = {"ts": time.time(), "host": _read_proc()}
    rc, out = await _run("nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
                         "--format=csv,noheader,nounits")
    if rc == 0:
        u, mu, mt, t, pw = [v.strip() for v in out.strip().split(",")]
        st["gpu"] = {"util": int(u), "mem_used": int(mu), "mem_total": int(mt), "temp": int(t), "power": float(pw)}
    units = {}
    for u in UNITS:
        _, out = await _run("systemctl", "--user", "is-active", u if "." in u else f"{u}.service")
        units[u] = out.strip()
    st["units"] = units
    probes = {"model": "http://127.0.0.1:8181/health", "embed": "http://127.0.0.1:8182/health", "decide": "http://127.0.0.1:8183/health",
              "searxng": "http://127.0.0.1:8888/healthz", "crawl4ai": "http://127.0.0.1:11235/health",
              "qdrant": "http://127.0.0.1:6333/healthz"}
    health = {}
    async with httpx.AsyncClient(timeout=3, trust_env=False) as c:
        async def probe(k: str, url: str) -> None:
            try:
                r = await c.get(url)
                health[k] = "up" if r.status_code == 200 else ("loading" if r.status_code == 503 else f"http {r.status_code}")
            except Exception:
                health[k] = "down"
        await asyncio.gather(*(probe(k, u) for k, u in probes.items()))
        health["relay"] = "up" if await relay_ok() else "down"
        st["health"] = health
        model: dict[str, Any] = {"profile": active_profile()}
        try:
            prof = read_profile(model["profile"])
            model.update(ctx=int(prof["CTX"]), agent_ctx=agent_ctx(prof), file=Path(prof["MODEL"]).name, parallel=int(prof.get("PARALLEL", "1")))
        except Exception:
            pass
        try:
            m = (await c.get("http://127.0.0.1:8181/metrics")).text
            for key in ("prompt_tokens_total", "prompt_seconds_total", "tokens_predicted_total",
                        "tokens_predicted_seconds_total", "requests_processing", "requests_deferred"):
                mm = re.search(rf"^llamacpp:{key}\s+([0-9.eE+-]+)", m, re.M)
                if mm:
                    model[key] = float(mm.group(1))
            # lifetime averages since the server started (the *_seconds gauges read 0 between requests)
            if model.get("tokens_predicted_seconds_total"):
                model["avg_gen_tps"] = model["tokens_predicted_total"] / model["tokens_predicted_seconds_total"]
            if model.get("prompt_seconds_total"):
                model["avg_prefill_tps"] = model["prompt_tokens_total"] / model["prompt_seconds_total"]
            sl = (await c.get("http://127.0.0.1:8181/slots")).json()
            if sl:
                busy = [s for s in sl if s.get("is_processing")]
                s0 = busy[0] if busy else sl[0]
                nt = s0.get("next_token")
                nt = nt[0] if isinstance(nt, list) and nt else (nt or {})
                model["slot"] = {"processing": bool(busy), "n_ctx": s0.get("n_ctx"),
                                 "prompt_tokens": s0.get("n_prompt_tokens"), "prompt_done": s0.get("n_prompt_tokens_processed"),
                                 "decoded": nt.get("n_decoded")}
                model["slots"] = {"total": len(sl), "busy": len(busy)}
        except Exception:
            pass
        st["model"] = model
    jobs = q("SELECT status, COUNT(*) n FROM jobs WHERE kind!='external' GROUP BY status")
    st["queue"] = {r["status"]: r["n"] for r in jobs}
    running = q("SELECT id, kind, title, started, role, label, depth, root_id, parent_id FROM jobs WHERE status='running' ORDER BY id")
    st["running"] = dict(running[0]) if running else None
    st["running_all"] = [dict(r) for r in running]
    st["crew"] = {"slots": slots(), "busy": len([r for r in running if r["kind"] == "agent"]),
                  "queued": len(q("SELECT id FROM jobs WHERE status='queued' AND kind='agent'")),
                  "bg_running": len(q("SELECT id FROM bgtasks WHERE state='running'"))}
    return st


async def relay_ok() -> bool:
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection("172.17.0.1", 7898), 2)
        w.close()
        return True
    except Exception:
        return False


async def status_loop() -> None:
    global _status
    n = 0
    while True:
        try:
            _status = await collect_status()
            await hub.send({"type": "status", "data": _status})
            n += 1
            if n % 3 == 0:
                await bg_monitor()
        except Exception as e:  # never let the loop die
            print("status loop:", e, file=sys.stderr)
        await asyncio.sleep(2)


# ---------------------------------------------------------------- jobs

_queue_wakeup = asyncio.Event()
_running: dict[int, dict[str, Any]] = {}      # job id -> {"proc": Process, "task": Task}
_cancelled: set[int] = set()
_session_retry: dict[int, tuple[int, float]] = {}   # job id -> (attempts, not before): resume refused SESSION_NOT_OWNED
SESSION_RETRY_S, SESSION_RETRY_MAX = 30, 120              # the other owner gets up to an hour to let go
_job_done = asyncio.Condition()


def job_row(jid: int) -> dict:
    r = q("SELECT * FROM jobs WHERE id=?", (jid,))
    if not r:
        raise HTTPException(404, "no such job")
    d = dict(r[0])
    d["params"] = json.loads(d["params"] or "{}")
    d["tokens"] = json.loads(d["tokens"] or "null")
    return d


# ---------------------------------------------------------------- chat profiles
# A profile = its own chat list here + its own Hermes home (hermes-home/profiles/hud-<id>: fresh memories, sessions
# and session_search). Model, config, skills, plugins and tools are symlinked to the main home so they stay shared;
# /workspace is shared too. Profile 1 "Main" is hermes-home itself and owns every chat from before profiles existed.
PROFILES_DIR = HERMES_HOME / "profiles"
PROFILE_SHARED = ["config.yaml", ".env", "SOUL.md", "skills", "plugins", "tools", "hooks", "scripts"]


def chat_profile(pid: Any) -> dict | None:
    try:
        r = q("SELECT * FROM chat_profiles WHERE id=?", (int(pid),))
    except (TypeError, ValueError):
        return None
    return dict(r[0]) if r else None


def chat_profile_id() -> int:
    """The profile the dashboard last switched to (the default for new chats and lists)."""
    r = q("SELECT v FROM kv WHERE k='chat_profile'")
    pid = int(r[0]["v"]) if r and str(r[0]["v"]).isdigit() else 1
    return pid if chat_profile(pid) else 1


def chat_profiles_payload() -> dict:
    rows = q("SELECT p.id, p.name, p.created, (SELECT COUNT(*) FROM jobs j WHERE j.profile_id=p.id AND j.id=j.root_id AND j.kind='agent') AS chats,"
             " (SELECT COUNT(*) FROM jobs j WHERE j.profile_id=p.id AND j.status IN ('queued','running')) AS busy FROM chat_profiles p ORDER BY p.id")
    return {"active": chat_profile_id(), "profiles": [dict(r) for r in rows]}


def ensure_profile_home(slug: str) -> Path:
    """Create the Hermes profile on first use and (re)link the shared parts to the main home. Idempotent; a real
    file/dir found where a link belongs is moved aside, never deleted."""
    if not re.fullmatch(r"hud-[0-9]+", slug or ""):
        raise ValueError(f"bad profile slug {slug!r}")
    home = PROFILES_DIR / slug
    fresh = not home.is_dir()
    if fresh:
        r = subprocess.run([str(HERMES), "profile", "create", slug, "--no-alias", "--no-skills", "--description",
                            "HUD chat profile (separate chats and memory; shares config and skills with the main home)"],
                           env=os.environ | {"HERMES_HOME": str(HERMES_HOME)}, capture_output=True, text=True, timeout=120)
        if r.returncode != 0 or not home.is_dir():
            raise RuntimeError(f"hermes profile create failed: {(r.stderr or r.stdout).strip()[-300:]}")
    for name in PROFILE_SHARED:
        src, dst = HERMES_HOME / name, home / name
        if not src.exists() or (dst.is_symlink() and os.readlink(dst) == str(src)):
            continue
        if dst.is_symlink():
            dst.unlink()
        elif dst.exists() and fresh:            # Hermes' own seed files of a profile created just now
            shutil.rmtree(dst) if dst.is_dir() else dst.unlink()
        elif dst.exists():
            if dst.is_dir() and not any(dst.iterdir()):
                dst.rmdir()
            else:
                dst.rename(dst.with_name(f"{name}.local-{int(time.time())}"))
        dst.symlink_to(src)
    return home


def profile_home(pid: Any) -> Path:
    prof = chat_profile(pid or 1)
    return ensure_profile_home(prof["slug"]) if prof and prof.get("slug") else HERMES_HOME


def job_brief(r: sqlite3.Row | dict) -> dict:
    """Job without params/final/events (for lists)."""
    d = dict(r)
    d.pop("params", None); d.pop("final", None)
    if isinstance(d.get("tokens"), str):
        d["tokens"] = json.loads(d["tokens"] or "null")
    return d


async def add_event(jid: int, typ: str, data: Any) -> None:
    seq = q("SELECT COALESCE(MAX(seq), -1) + 1 FROM events WHERE job_id=?", (jid,))[0][0]
    ts = time.time()
    x("INSERT INTO events VALUES (?,?,?,?,?)", (jid, seq, ts, typ, json.dumps(data)))
    await hub.send({"type": "event", "job": jid, "seq": seq, "ts": ts, "etype": typ, "data": data})


async def set_job(jid: int, **kw: Any) -> None:
    cols = ", ".join(f"{k}=?" for k in kw)
    x(f"UPDATE jobs SET {cols} WHERE id=?", (*kw.values(), jid))
    await hub.send({"type": "job", "data": job_row(jid)})


async def mission_changed(root_id: int, what: str) -> None:
    await hub.send({"type": "mission", "root": root_id, "what": what})


def build_system_prompt(job: dict) -> str:
    role, mode, depth = job.get("role"), job.get("mode") or "chat", job.get("depth") or 0
    brief = EFFORT_BRIEF.get(job.get("effort") or "", "")
    if not role:
        base = rolelib.build_prompt(None, "chat") if job.get("kind") == "agent" else ""
    else:
        try:
            base = rolelib.build_prompt(role, mode, depth)
        except KeyError:
            base = ""
    extra = []
    if job.get("kind") == "agent":
        extra.append(toolbox_brief())
        if PDF_RE.search(str((job.get("params") or {}).get("prompt", ""))):
            extra.append(PDF_BRIEF)
    return "\n\n".join(x for x in (base, brief, _cadence_brief(job), *extra) if x)


def _cadence_brief(job: dict) -> str:
    """Lead-only section: the owner's deliverable cadence and budget for this mission (dynamic, so not in the role file)."""
    if (job.get("depth") or 0) != 0 or not job.get("role"):
        return ""
    try:
        root = job if job["id"] == job.get("root_id") else job_row(job["root_id"])
    except Exception:
        return ""
    every = int((root.get("params") or {}).get("deliver_every_s") or 0)
    if not every:
        return ""
    h, bh = every / 3600, (job.get("budget_s") or 0) / 3600
    return (f"# Deliverable cadence (set by the owner for this mission)\n"
            f"The owner wants a **usable product every {h:g} h** of mission time"
            + (f" inside a {bh:g} h run budget" if bh else "") + ", plus one before your final report.\n"
            "- **Usable** means: someone opens the delivered folder and runs it end to end — a README with the exact install/run/test "
            "commands, pinned dependencies, sample input, every check green, nothing half-wired (unfinished work stays behind a flag "
            "or out). A thin but complete product beats a thick broken one; the first window is often a walking skeleton.\n"
            "- **Plan in vertical slices** so each window ends runnable: pick the slice, build it, test it, document it, deliver, "
            "then widen. Never spend a whole window on one layer with nothing to show.\n"
            "- **The clock is in your tool results**: `clock`, `wait`, `status` and `progress` return a `deliverable` field with "
            "`next_due_in_s`. At `due in 20 min` start no new work: stabilise, run the checks, update the README, `state_save`. "
            "When due, call `deliver(summary, paths, version, how_to_run)`: it checkpoints, copies `paths` into "
            "`/workspace/outbox/deliveries/<mission>/<time>-vN/` with a DELIVERY.md, zips it (a download card on the owner's "
            "dashboard) and restarts the countdown. Put the zip path in a `progress` note.\n"
            "- A missed window shows as OVERDUE on the dashboard — treat it as a failure to avoid, not a soft target. "
            "If the product is genuinely not runnable at the deadline, deliver what runs (even a smaller slice) and say what is missing.")


async def route_job(job: dict) -> None:
    """Ask the decision model (System One) which skills to preload and/or which effort to use, before Hermes starts.
    Fail-open: any error leaves the job exactly as the owner configured it, with the error recorded in params.routing."""
    p = job["params"]
    if "routing" in p or p.get("compact") or not decider or not (p.get("auto_skills") or p.get("effort_auto")):
        return
    out: dict = {}
    try:
        if p.get("auto_skills"):
            r = await asyncio.wait_for(asyncio.to_thread(decider.route_skills, p["prompt"]), 45)
            already = set(p.get("skills", []))
            for row in q("SELECT params FROM jobs WHERE root_id=? AND id<>?", (job["root_id"], job["id"])):   # loaded earlier in this thread
                already |= set(json.loads(row["params"] or "{}").get("skills", []))
            added = [s for s in r["skills"] if s not in already]
            p["skills"] = list(p.get("skills", [])) + added
            out["skills"] = {"added": added, "picked": r["skills"], "need": r["need"], "top": r["top"], "ms": r["ms"]}
        if p.get("effort_auto"):
            e = await asyncio.wait_for(asyncio.to_thread(decider.route_effort, p["prompt"]), 30)
            job["effort"] = e["effort"]; p["effort"] = e["effort"]
            x("UPDATE jobs SET effort=? WHERE id=?", (e["effort"], job["id"]))
            out["effort"] = e
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"[:300]
    p["routing"] = out
    x("UPDATE jobs SET params=? WHERE id=?", (json.dumps(p), job["id"]))
    await hub.send({"type": "job", "data": job_row(job["id"])})


def _quick_chain(parent: dict | None) -> tuple[dict | None, list[dict]]:
    """Walk up past quick replies (answered without Hermes, so not in its session): -> (nearest real agent turn or None,
    the quick turns after it, oldest first)."""
    chain = []
    while parent and parent["params"].get("quick_reply"):
        chain.append(parent)
        parent = job_row(parent["parent_id"]) if parent.get("parent_id") and parent.get("relation") == "followup" else None
    return parent, chain[::-1]


def _thread_history(job: dict, max_turns: int = 12) -> tuple[list[dict], str]:
    """Earlier turns of this thread along the parent chain (oldest first) and the compaction summary they start from."""
    turns, summary = [], ""
    pid = job.get("parent_id") if job.get("relation") == "followup" else None
    while pid and len(turns) < max_turns:
        try:
            par = job_row(pid)
        except HTTPException:
            break
        if par["params"].get("compact"):
            summary = par.get("summary") or ""
            break
        turns.append({"prompt": par["params"].get("prompt") or "", "reply": par.get("final") or ""})
        pid = par.get("parent_id") if par.get("relation") == "followup" else None
    return turns[::-1], summary


async def quick_reply(job: dict) -> bool:
    """Jev triage: a trivial message ("hi", "is the sun yellow") gets one short, tool-free, no-thinking call to the main model
    instead of a Hermes run (no sandbox, no 20k-token system prompt, any effort setting). True = answered (job finished).
    False = the agent runs: not trivial, router down, or the quick reply itself handed the message over."""
    p, jid = job["params"], job["id"]
    if not p.get("quick_ok") or "triage" in p or p.get("compact") or not decider or len(p.get("prompt", "")) > QUICK_MAX_CHARS:
        return False
    turns, summary = _thread_history(job)
    try:
        tri = await asyncio.wait_for(asyncio.to_thread(decider.route_quick, p["prompt"], turns[-1]["reply"] if turns else ""), 35)
    except Exception as e:
        tri = {"quick": False, "error": f"{type(e).__name__}: {e}"[:200]}
    p["triage"] = tri

    async def save() -> None:
        x("UPDATE jobs SET params=? WHERE id=?", (json.dumps(p), jid))
        await hub.send({"type": "job", "data": job_row(jid)})
    await save()
    if not tri.get("quick"):
        return False
    msgs = [{"role": "system", "content": QUICK_SYSTEM.format(now=time.strftime("%A %d %B %Y, %H:%M %Z"))}]
    if summary:
        msgs.append({"role": "system", "content": "Summary of the earlier conversation:\n" + summary[:6000]})
    hist, budget = [], 12000
    for t in reversed(turns):
        pair = [{"role": "user", "content": t["prompt"][:2000]}, {"role": "assistant", "content": t["reply"][:3000] or "(no reply)"}]
        budget -= len(pair[0]["content"]) + len(pair[1]["content"])
        if budget < 0:
            break
        hist = pair + hist
    msgs += hist + [{"role": "user", "content": p["prompt"]}]
    body = {"model": "local", "messages": msgs, "stream": True, "max_tokens": QUICK_MAX_TOKENS, "reasoning_effort": "none",
            "chat_template_kwargs": {"enable_thinking": False}, "stream_options": {"include_usage": True}}
    _running[jid]["quick"] = True
    t0, text, usage, decided, buf, last = time.time(), "", None, False, [], time.time()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=5), trust_env=False) as c:
            async with c.stream("POST", QUICK_URL, json=body) as r:
                if r.status_code != 200:
                    raise RuntimeError(f"model server HTTP {r.status_code}")
                async for line in r.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if data == "[DONE]":
                        break
                    ev = json.loads(data)
                    usage = ev.get("usage") or usage
                    d = "".join((ch.get("delta") or {}).get("content") or "" for ch in ev.get("choices") or [])
                    if not d:
                        continue
                    text += d
                    if not decided:         # hold the first characters back until they cannot be the hand-off marker
                        head = text.lstrip()
                        if head.startswith(QUICK_HANDOFF[:7]):
                            break
                        if len(head) < len(QUICK_HANDOFF) and QUICK_HANDOFF.startswith(head):
                            continue
                        decided, d = True, text
                    buf.append(d)
                    if time.time() - last > 0.4:
                        await add_event(jid, "text", "".join(buf)); buf.clear(); last = time.time()
    except asyncio.CancelledError:
        _running[jid].pop("quick", None)
        if buf:
            await add_event(jid, "text", "".join(buf))
        await set_job(jid, status="cancelled", finished=time.time(), final=text.strip())
        return True
    except Exception as e:
        _running[jid].pop("quick", None)
        if not decided:             # nothing shown yet: let the agent answer instead
            tri["error"] = f"quick reply failed: {type(e).__name__}: {e}"[:200]; tri["handoff"] = True
            await save()
            return False
        if buf:
            await add_event(jid, "text", "".join(buf))
        await set_job(jid, status="failed", finished=time.time(), final=text.strip(), error=f"quick reply failed: {type(e).__name__}: {e}"[:300])
        return True
    _running[jid].pop("quick", None)
    if not decided and (not text.strip() or text.lstrip().startswith(QUICK_HANDOFF[:7])):
        tri["handoff"] = True; tri["ms_reply"] = round((time.time() - t0) * 1000)
        await save()
        return False
    if not decided:
        buf = [text]
    if buf:
        await add_event(jid, "text", "".join(buf))
    tri["ms_reply"] = round((time.time() - t0) * 1000)
    p["quick_reply"] = True
    base, _ = _quick_chain(job_row(job["parent_id"])) if job.get("parent_id") and job.get("relation") == "followup" else (None, [])
    u = usage or {}
    tokens = {"input": u.get("prompt_tokens") or 0, "output": u.get("completion_tokens") or 0, "total": u.get("total_tokens") or 0}
    x("UPDATE jobs SET params=? WHERE id=?", (json.dumps(p), jid))
    await set_job(jid, status="done", finished=time.time(), final=text.strip(), tokens=json.dumps(tokens), exit_code=0, n_tools=0,
                  session_id=(base or {}).get("session_id") if base and not base.get("summary") else None)
    return True


async def run_agent(job: dict) -> None:
    if await quick_reply(job):
        return
    await route_job(job)
    p = job["params"]
    qfile = DATA / f"job-{job['id']}.prompt.txt"
    imgs = [str(UPLOADS / f"{i}.jpg") for i in p.get("images", [])]
    prompt = p["prompt"]
    if p.get("files"):
        prompt = attach_prompt_block(p["files"]) + "\n\n" + prompt
    if (job.get("depth") or 0) == 0 and job.get("role"):
        await ckpt_create(job["root_id"], job["id"], "mission start" if job.get("relation") != "followup" else "follow-up start")
        if job.get("relation") == "followup":
            root = job_row(job["root_id"])
            if root.get("mission_state"):
                prompt = (f"[Mission state saved by the previous run — resume from it, do not redo finished work]\n{root['mission_state']}\n\n"
                          f"[New message from the owner]\n{prompt}")
    cmd = [str(HERMES), "chat", "--query-file", str(qfile), "--oneshot", "--format", "stream-json", "--yolo",
           "--max-turns", str(job.get("max_turns") or p.get("max_turns", 40)), "--source", "hud"]
    if job.get("budget_s"):
        cmd += ["--run-budget", str(int(job["budget_s"]))]
    if (job.get("effort") or "") in EFFORTS:
        cmd += ["--provider", f"custom:local-{job['effort']}"]
    if job.get("parent_id") and job.get("relation") == "followup":
        parent, quick = _quick_chain(job_row(job["parent_id"]))   # quick replies never reached Hermes: resume the turn before them
        pre = []
        if parent and parent.get("summary"):   # the parent was a compaction: start fresh, seeded with its summary
            pre.append(f"[Summary of the conversation so far — it was compacted to save context; treat it as the history]\n{parent['summary']}")
        elif parent and parent.get("session_id"):
            cmd += ["--resume", parent["session_id"]]
        if quick:
            pre.append("[Quick replies the dashboard sent without you since your last turn (not in your history); treat them as part of this conversation]\n"
                       + "\n".join(f"Owner: {qj['params'].get('prompt', '')}\nYou: {qj.get('final') or ''}" for qj in quick))
        if pre:
            prompt = "\n\n".join(pre) + f"\n\n[New message from the owner]\n{prompt}"
    qfile.write_text(prompt)
    skills = list(p.get("skills", []))
    if PDF_RE.search(str(p.get("prompt", ""))) and "pretty-pdf" not in skills:
        skills.append("pretty-pdf")          # owner rule: every PDF is extravagant (skill hermes-home/skills/productivity/pretty-pdf)
    for s in skills:
        cmd += ["--skills", s]
    if job.get("role"):                      # per-role toolsets (role front-matter): fewer tool schemas = smaller fixed prompt
        try:
            role_ts = rolelib.load_roles().get(job["role"], {}).get("toolsets") or []
        except Exception:
            role_ts = []
        if role_ts:
            cmd += ["--toolsets", ",".join(role_ts)]
    for img in imgs:  # native image parts; --image is repeatable via patches/hermes-repeatable-image.patch
        cmd += ["--image", img]
    home = await asyncio.to_thread(profile_home, job.get("profile_id"))
    env = os.environ | {"HERMES_HOME": str(home), "LA_JOB_ID": str(job["id"]), "LA_ROOT_ID": str(job["root_id"]),
                        "LA_DEPTH": str(job.get("depth") or 0), "LA_HUD_URL": "http://127.0.0.1:8190"}
    sp = build_system_prompt(job)
    if sp:
        env["HERMES_EPHEMERAL_SYSTEM_PROMPT"] = sp
    out_path, err_path = DATA / f"job-{job['id']}.out.jsonl", DATA / f"job-{job['id']}.err.log"
    # files, not pipes, and a plain Popen (asyncio subprocess transports kill their child when the loop closes):
    # the agent must outlive a HUD restart and is reattached afterwards.
    with open(out_path, "wb") as out, open(err_path, "wb") as err:
        proc = subprocess.Popen(cmd, cwd=WORKSPACE, env=env, stdout=out, stderr=err, stdin=subprocess.DEVNULL, start_new_session=True)
    x("UPDATE jobs SET pid=?, out_offset=0 WHERE id=?", (proc.pid, job["id"]))
    _running[job["id"]]["proc"] = proc
    _running[job["id"]]["pid"] = proc.pid
    await follow_agent(job, proc.pid, proc)


async def follow_agent(job: dict, pid: int, proc: Any = None) -> None:
    """Tail data/job-N.out.jsonl (stream-json) from the saved offset until the agent exits; works for a process we
    started (proc) and for one we reattached to after a HUD restart (pid only)."""
    jid = job["id"]
    p = job["params"]
    out_path, err_path = DATA / f"job-{jid}.out.jsonl", DATA / f"job-{jid}.err.log"
    row = job_row(jid)
    offset = int(row.get("out_offset") or 0)
    n_tools = int(row.get("n_tools") or 0)
    text_buf: list[str] = []
    last_flush = time.time()
    final, tokens, exit_code, error = "", None, None, None
    partial = b""
    buf_type = "text"                   # text_buf holds deltas of one kind: "text" (the answer) or "thinking" (reasoning)

    async def flush_text() -> None:
        nonlocal last_flush
        if text_buf:
            await add_event(jid, buf_type, "".join(text_buf))
            text_buf.clear()
        last_flush = time.time()

    def alive() -> bool:
        if proc is not None:
            return proc.poll() is None
        return _pid_is_agent(pid)

    while True:
        running = alive()
        try:
            with open(out_path, "rb") as f:
                f.seek(offset)
                chunk = f.read()
        except FileNotFoundError:
            chunk = b""
        if chunk:
            data = partial + chunk
            lines = data.split(b"\n")
            partial = lines.pop()               # incomplete last line (if any) waits for the next read
            offset += len(chunk)
            for raw in lines:
                if not raw.strip():
                    continue
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                t = ev.get("type")
                if t in ("text", "thinking"):
                    if t != buf_type:
                        await flush_text()
                        buf_type = t
                    text_buf.append(ev.get("text", ""))
                    if time.time() - last_flush > 0.4:
                        await flush_text()
                    continue
                await flush_text()
                if t == "system":
                    await set_job(jid, session_id=ev.get("session_id"))
                elif t == "tool_use":
                    n_tools += 1
                    name = ev.get("name")
                    await add_event(jid, "tool_use", {"name": name, "input": ev.get("input"), "id": ev.get("tool_call_id")})
                    x("UPDATE jobs SET last_tool=?, last_tool_ts=?, n_tools=? WHERE id=?", (name, time.time(), n_tools, jid))
                elif t == "tool_result":
                    await add_event(jid, "tool_result", {"name": ev.get("name"), "output": ev.get("output", "")[:5000],
                                                         "is_error": ev.get("is_error"), "ms": ev.get("duration_ms"), "id": ev.get("tool_call_id")})
                elif t == "result":
                    final, tokens, exit_code, error = ev.get("text", ""), ev.get("tokens"), ev.get("exit_code"), ev.get("error")
            x("UPDATE jobs SET out_offset=? WHERE id=?", (offset - len(partial), jid))
            offset -= len(partial); partial = b""
        elif text_buf and time.time() - last_flush > 0.4:
            await flush_text()
        if not running and not chunk:
            break
        await asyncio.sleep(0.25 if chunk else 0.5)
    await flush_text()
    rc = proc.wait() if proc is not None else None
    try:
        stderr = err_path.read_text(errors="replace")[-4000:]
    except OSError:
        stderr = ""
    if exit_code is None and rc is None:        # reattached and the agent ended without a result event
        exit_code = 0 if final else 1
    if not final and (exit_code or rc) and not error:
        error = stderr.strip()[-1500:] or f"exit {rc}"
    status = "cancelled" if jid in _cancelled else ("done" if (exit_code if exit_code is not None else rc) == 0 else "failed")
    extra = {}
    if status == "failed" and not final and "SESSION_NOT_OWNED" in stderr:
        # the chat is still held by another hermes process (another turn of this thread, or a terminal);
        # nothing ran, so put the turn back in the queue instead of failing it
        n = _session_retry.get(jid, (0, 0.0))[0] + 1
        if n <= SESSION_RETRY_MAX:
            _session_retry[jid] = (n, time.time() + SESSION_RETRY_S)
            await set_job(jid, status="queued", started=None, pid=None, out_offset=0, error=None)
            await add_event(jid, "log", f"chat is busy in another Hermes process; waiting to retry (attempt {n})")
            for fp in (out_path, err_path):
                try:
                    fp.unlink()
                except OSError:
                    pass
            return
    _session_retry.pop(jid, None)
    if p.get("compact") and status == "done" and len(final.strip()) > 80:
        extra["summary"] = final.strip()[:30000]
    await set_job(jid, status=status, finished=time.time(), final=final, tokens=json.dumps(tokens),
                  error=error, exit_code=exit_code if exit_code is not None else rc, n_tools=n_tools, **extra)
    for fp in (out_path, err_path):
        try:
            fp.unlink()
        except OSError:
            pass


async def reattach_agent(jid: int) -> None:
    job = job_row(jid)
    try:
        await follow_agent(job, int(job["pid"]), None)
    except Exception as e:
        await set_job(jid, status="failed", finished=time.time(), error=f"reattach failed: {type(e).__name__}: {e}")
    finally:
        _running.pop(jid, None)
        _cancelled.discard(jid)
        async with _job_done:
            _job_done.notify_all()
        await mission_changed(job["root_id"], "job")
        _queue_wakeup.set()


async def run_script(job: dict, cmd: list[str], cwd: Path) -> None:
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                                                start_new_session=True, env=os.environ | {"HERMES_HOME": str(HERMES_HOME)})
    _running[job["id"]]["proc"] = proc
    lines = []
    async for raw in proc.stdout:
        line = raw.decode(errors="replace").rstrip()
        lines.append(line)
        await add_event(job["id"], "log", line)
    rc = await proc.wait()
    status = "cancelled" if job["id"] in _cancelled else ("done" if rc == 0 else "failed")
    await set_job(job["id"], status=status, finished=time.time(), final="\n".join(lines[-40:]), exit_code=rc)


async def run_job(job: dict) -> None:
    try:
        if job["kind"] == "agent":
            await run_agent(job)
        elif job["kind"] == "bench":
            sizes = [str(n) for n in job["params"].get("contexts", [128, 16384])]
            await run_script(job, [str(RAG_PY), "-u", str(ROOT / "scripts/bench_server.py"), *sizes], ROOT)
        elif job["kind"] == "eval":
            args = ["-u", str(EVALS / "run_evals.py"), "--label", job["params"]["label"]]
            for k in job["params"].get("only", []):
                args += ["-k", k]
            await run_script(job, ["/usr/bin/python3", *args], EVALS)
    except Exception as e:
        await set_job(job["id"], status="failed", finished=time.time(), error=f"{type(e).__name__}: {e}")
    finally:
        _running.pop(job["id"], None)
        _cancelled.discard(job["id"])
        async with _job_done:
            _job_done.notify_all()
        await mission_changed(job["root_id"], "job")
        _queue_wakeup.set()


def _can_start(job: dict, running: list[dict]) -> bool:
    if job["kind"] != "agent":
        return not running                      # bench/eval need the model server to themselves
    if any(r["kind"] != "agent" for r in running):
        return False
    if len([r for r in running if r["kind"] == "agent"]) >= slots():
        return False
    if time.time() < _session_retry.get(job["id"], (0, 0.0))[1]:
        return False                            # its chat was held elsewhere a moment ago; back off
    if job.get("relation") == "followup" and job.get("parent_id"):
        if q("SELECT 1 FROM jobs WHERE id=? AND status IN ('queued','running')", (job["parent_id"],)):
            return False                        # a follow-up resumes the parent's session; wait for it
        # every turn of a thread resumes the same Hermes session, which takes one writer at a time: a follow-up
        # sent from an older turn (or while a sibling is still running) must wait for the running turn
        if any(r["id"] != job["id"] and r.get("root_id") == job.get("root_id") and r.get("relation") != "child"
               for r in running):
            return False
    return True


async def scheduler() -> None:
    """Start queued jobs while slots are free: crew members first (their lead is blocked on them), then
    older top-level jobs; bench/eval jobs run alone."""
    while True:
        running = [job_row(j) for j in list(_running)]
        for r in q("SELECT id FROM jobs WHERE status='queued' ORDER BY (depth IS NULL OR depth=0), id"):
            job = job_row(r["id"])
            if not _can_start(job, running):
                continue
            _running[job["id"]] = {"proc": None, "task": None}
            await set_job(job["id"], status="running", started=time.time())
            _running[job["id"]]["task"] = asyncio.create_task(run_job(job))
            running.append(job)
        _queue_wakeup.clear()
        try:
            await asyncio.wait_for(_queue_wakeup.wait(), 3)
        except asyncio.TimeoutError:
            pass


async def cancel_tree(jid: int) -> None:
    j = job_row(jid)
    for r in q("SELECT * FROM cruns WHERE job_id=? AND state='running'", (jid,)):
        await crun_stop(dict(r))
    for c in q("SELECT id FROM jobs WHERE parent_id=? AND relation='child' AND status IN ('queued','running')", (jid,)):
        await cancel_tree(c["id"])
    if j["status"] == "queued":
        await set_job(jid, status="cancelled", finished=time.time())
    elif j["status"] == "running" and jid in _running:
        _cancelled.add(jid)
        if _running[jid].get("quick") and _running[jid].get("task"):
            _running[jid]["task"].cancel()
            return
        proc = _running[jid].get("proc")
        pid = proc.pid if proc else _running[jid].get("pid")
        if pid:
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


# ---------------------------------------------------------------- background tasks (docker, same sandbox image)

def _bg_row(tid: str) -> dict:
    r = q("SELECT * FROM bgtasks WHERE id=?", (tid,))
    if not r:
        raise HTTPException(404, "no such background task")
    return dict(r[0])


async def bg_refresh(t: dict) -> dict:
    """Pull live state from docker for a running task; on exit store the log tail and remove the container."""
    if t["state"] != "running":
        return t
    rc, out = await _run("docker", "inspect", "-f", "{{.State.Status}} {{.State.ExitCode}}", t["container"], timeout=8)
    if rc != 0:
        x("UPDATE bgtasks SET state='lost', finished=? WHERE id=?", (time.time(), t["id"]))
        return _bg_row(t["id"])
    status, code = out.split()[:2]
    if status in ("running", "created"):
        return t
    _, logs = await _run("docker", "logs", "--tail", "300", t["container"], timeout=15)
    await _run("docker", "rm", "-f", t["container"], timeout=15)
    exit_code = int(code)
    state = "killed" if t.get("_killed") else ("timeout" if exit_code == 124 else ("done" if exit_code == 0 else "failed"))
    x("UPDATE bgtasks SET state=?, exit_code=?, finished=?, log_tail=? WHERE id=?", (state, exit_code, time.time(), logs[-60000:], t["id"]))
    await mission_changed(t["root_id"], "bg")
    return _bg_row(t["id"])


async def bg_monitor() -> None:
    for r in q("SELECT * FROM bgtasks WHERE state='running'"):
        await bg_refresh(dict(r))


def bg_view(t: dict, tail_lines: int = 12) -> dict:
    d = {k: t[k] for k in ("id", "name", "state", "exit_code", "created", "finished", "job_id", "timeout_s")}
    d["command"] = (t["command"] or "")[:300]
    d["elapsed_s"] = round((t["finished"] or time.time()) - t["created"], 1)
    tail = (t.get("log_tail") or "").splitlines()[-tail_lines:]
    d["tail"] = tail
    return d


async def bg_start(root_id: int, job_id: int, name: str, command: str, timeout_s: int) -> dict:
    tid = secrets.token_hex(4)
    container = f"la-bg-{tid}"
    timeout_s = max(10, min(int(timeout_s or 3600), 24 * 3600))
    proxy = "http://172.17.0.1:7898"
    cmd = ["docker", "run", "-d", "--name", container, "--label", "la-bg=1", "--label", f"la-root={root_id}",
           "--user", f"{os.getuid()}:{os.getgid()}", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
           "--pids-limit", "2048", "--memory", "8g", "--shm-size", "1g",
           "-v", f"{WORKSPACE}:/workspace", "-v", f"{BG_HOME}:/home/pn", *MNT_MOUNTS, "-w", "/workspace",
           "-e", "HOME=/home/pn", "-e", f"HTTP_PROXY={proxy}", "-e", f"HTTPS_PROXY={proxy}", "-e", f"http_proxy={proxy}",
           "-e", f"https_proxy={proxy}", "-e", "NO_PROXY=localhost,127.0.0.1", "-e", "no_proxy=localhost,127.0.0.1",
           SANDBOX_IMAGE, "timeout", "--signal=TERM", "-k", "20", str(timeout_s), "bash", "-lc", command]
    rc, out = await _run(*cmd, timeout=60)
    if rc != 0:
        return {"error": f"docker run failed: {out.strip()[-800:]}"}
    x("INSERT INTO bgtasks (id, root_id, job_id, name, command, container, created, state, timeout_s, log_tail) VALUES (?,?,?,?,?,?,?,?,?,?)",
      (tid, root_id, job_id, name, command, container, time.time(), "running", timeout_s, ""))
    await mission_changed(root_id, "bg")
    return {"task_id": tid, "name": name, "state": "running", "timeout_s": timeout_s}


async def bg_logs_text(t: dict, tail: int) -> str:
    if t["state"] == "running":
        _, out = await _run("docker", "logs", "--tail", str(tail), t["container"], timeout=15)
        return out
    return "\n".join((t.get("log_tail") or "").splitlines()[-tail:])


async def bg_stop(t: dict) -> dict:
    if t["state"] != "running":
        return {"task_id": t["id"], "state": t["state"], "note": "already finished"}
    await _run("docker", "kill", t["container"], timeout=15)
    t["_killed"] = True
    for _ in range(10):
        await asyncio.sleep(0.5)
        t2 = await bg_refresh(t)
        if t2["state"] != "running":
            return {"task_id": t["id"], "state": t2["state"], "exit_code": t2["exit_code"]}
    x("UPDATE bgtasks SET state='killed', finished=? WHERE id=?", (time.time(), t["id"]))
    await _run("docker", "rm", "-f", t["container"], timeout=15)
    return {"task_id": t["id"], "state": "killed"}


# ---------------------------------------------------------------- checkpoints (shadow git over /workspace)

_ckpt_lock = asyncio.Lock()


def _git(*args: str) -> list[str]:
    return ["git", f"--git-dir={CKPT_GIT}", f"--work-tree={WORKSPACE}", "-c", "user.name=crew", "-c", "user.email=crew@local",
            "-c", "core.quotepath=off", *args]


def _nested_repos() -> list[str]:
    """Workspace-relative dirs that are git repos of their own (contain .git), skipping vendored/cache dirs."""
    out = []
    for dirpath, dirnames, filenames in os.walk(WORKSPACE):
        if dirpath != str(WORKSPACE) and (".git" in dirnames or ".git" in filenames):
            out.append(os.path.relpath(dirpath, WORKSPACE))
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in CRUN_SKIP_DIRS and not os.path.islink(os.path.join(dirpath, d))]
    return out


async def ckpt_init() -> None:
    if not (CKPT_GIT / "HEAD").exists():
        CKPT_GIT.parent.mkdir(parents=True, exist_ok=True)
        await _run("git", "init", "-q", "--bare", str(CKPT_GIT), timeout=20)
        await _run(*_git("commit", "-q", "--allow-empty", "-m", "[root 0] init"), timeout=20)
    (CKPT_GIT / "info").mkdir(exist_ok=True)
    big = []
    for dirpath, dirnames, filenames in os.walk(WORKSPACE):
        dirnames[:] = [d for d in dirnames if d + "/" not in CKPT_EXCLUDE and d not in ("node_modules", ".venv", "venv", "__pycache__")]
        for f in filenames:
            fp = Path(dirpath) / f
            try:
                if fp.stat().st_size > 20 * 1024 * 1024:
                    big.append(str(fp.relative_to(WORKSPACE)))
            except OSError:
                pass
    (CKPT_GIT / "info/exclude").write_text("\n".join(CKPT_EXCLUDE + big) + "\n")


async def ckpt_create(root_id: int, job_id: int, label: str) -> dict:
    async with _ckpt_lock:
        await ckpt_init()
        # nested git repos (a project the crew `git init`ed) can't be indexed from outside: `add -A` aborts on one with no
        # commit and would otherwise record a gitlink only. Exclude them; they keep their own history.
        nested = await asyncio.to_thread(_nested_repos)
        rc, out = await _run(*_git("add", "-A", "--", ":/", *[f":(top,exclude){p}" for p in nested]), timeout=120)
        if rc != 0:
            return {"error": "git add failed: " + out.strip()[-500:]}
        rc, out = await _run(*_git("commit", "-q", "--allow-empty", "-m", f"[root {root_id}][job {job_id}] {label[:120]}"), timeout=120)
        if rc != 0:
            return {"error": out.strip()[-500:]}
        _, h = await _run(*_git("rev-parse", "--short", "HEAD"), timeout=10)
        _, stat = await _run(*_git("diff", "--shortstat", "HEAD~1", "HEAD"), timeout=30)
        d = {"checkpoint": h.strip(), "label": label, "changes": stat.strip() or "no changes", "ts": time.time()}
        await mission_changed(root_id, "checkpoint")
        return d


async def ckpt_list(root_id: int, limit: int = 30) -> list[dict]:
    if not (CKPT_GIT / "HEAD").exists():
        return []
    _, out = await _run(*_git("log", "--format=%h%x09%ct%x09%s", f"--grep=\\[root {root_id}\\]", "-n", str(limit)), timeout=20)
    rows = []
    for line in out.splitlines():
        try:
            h, ts, subj = line.split("\t", 2)
        except ValueError:
            continue
        rows.append({"checkpoint": h, "ts": float(ts), "label": re.sub(r"^\[root \d+\]\[job (\d+)\] ", "", subj),
                     "job_id": int(m.group(1)) if (m := re.match(r"^\[root \d+\]\[job (\d+)\]", subj)) else None})
    return rows


async def ckpt_diff(frm: str, to: str = "", paths: list[str] | None = None, max_chars: int = 60000) -> dict:
    async with _ckpt_lock:
        await ckpt_init()
        if not re.fullmatch(r"[0-9a-f]{4,40}", frm) or (to and not re.fullmatch(r"[0-9a-f]{4,40}", to)):
            return {"error": "checkpoint ids are short git hashes from checkpoints()"}
        rel = [p.replace("/workspace/", "", 1).lstrip("/") for p in (paths or []) if isinstance(p, str)]
        if to:
            cmd = _git("diff", "--stat", "-p", frm, to, "--", *rel) if rel else _git("diff", "--stat", "-p", frm, to)
        else:
            await _run(*_git("add", "-A", "--", ":/", *[f":(top,exclude){p}" for p in await asyncio.to_thread(_nested_repos)]), timeout=120)
            cmd = _git("diff", "--cached", "--stat", "-p", frm, "--", *rel) if rel else _git("diff", "--cached", "--stat", "-p", frm)
        rc, out = await _run(*cmd, timeout=60)
        if rc != 0:
            return {"error": out.strip()[-500:]}
        return {"from": frm, "to": to or "working tree", "diff": out[:max_chars], "truncated": len(out) > max_chars, "chars": len(out)}


async def ckpt_rollback(root_id: int, job_id: int, checkpoint: str, paths: list[str] | None = None) -> dict:
    if not re.fullmatch(r"[0-9a-f]{4,40}", checkpoint):
        return {"error": "checkpoint must be a short git hash from checkpoints()"}
    pre = await ckpt_create(root_id, job_id, f"before rollback to {checkpoint}")
    async with _ckpt_lock:
        rel = [p.replace("/workspace/", "", 1).lstrip("/") for p in (paths or []) if isinstance(p, str)]
        if rel:
            rc, out = await _run(*_git("checkout", checkpoint, "--", *rel), timeout=60)
        else:
            # index + work tree := the checkpoint's tree. Files that appeared after the checkpoint are in the index
            # (the pre-rollback checkpoint just added them) and get removed; anything git never tracked (excluded
            # paths, empty dirs) is left alone — never `git clean` here. HEAD keeps the full history.
            rc, out = await _run(*_git("read-tree", "--reset", "-u", checkpoint), timeout=120)
        if rc != 0:
            return {"error": out.strip()[-500:], "pre_rollback_checkpoint": pre.get("checkpoint")}
    x("INSERT INTO notes (root_id, job_id, ts, message) VALUES (?,?,?,?)", (root_id, job_id, time.time(), f"rolled back to checkpoint {checkpoint}" + (f" ({len(rel)} paths)" if rel else " (whole workspace)")))
    await mission_changed(root_id, "checkpoint")
    return {"restored": checkpoint, "paths": rel or "all", "pre_rollback_checkpoint": pre.get("checkpoint")}


# ---------------------------------------------------------------- mission views

def steps_of(root_id: int) -> list[dict]:
    return [dict(r) for r in q("SELECT * FROM steps WHERE root_id=? ORDER BY ord", (root_id,))]


def children_of(root_id: int) -> list[dict]:
    rows = q("SELECT * FROM jobs WHERE root_id=? AND relation='child' ORDER BY id", (root_id,))
    return [job_brief(r) for r in rows]


def member_view(j: dict) -> dict:
    now = time.time()
    return {"id": j["id"], "role": j.get("role"), "label": j.get("label") or j.get("role"), "status": j["status"],
            "parent_id": j.get("parent_id"), "created": j["created"], "started": j.get("started"), "finished": j.get("finished"),
            "elapsed_s": round(((j.get("finished") or now) - j["started"]), 1) if j.get("started") else 0,
            "n_tools": j.get("n_tools") or 0, "last_tool": j.get("last_tool"),
            "last_tool_ago_s": round(now - j["last_tool_ts"], 1) if j.get("last_tool_ts") else None,
            "max_turns": j.get("max_turns"), "tokens": j.get("tokens"), "error": (j.get("error") or "")[:300] or None,
            "title": j.get("title")}


async def mission_view(root_id: int) -> dict:
    root = job_row(root_id)
    thread = [job_brief(r) for r in q("SELECT * FROM jobs WHERE root_id=? AND (relation IS NULL OR relation='followup') ORDER BY id", (root_id,))]
    notes = [dict(r) for r in q("SELECT n.*, j.role, j.label, j.depth FROM notes n LEFT JOIN jobs j ON j.id=n.job_id WHERE n.root_id=? ORDER BY n.id", (root_id,))]
    bg = [bg_view(dict(r)) for r in q("SELECT * FROM bgtasks WHERE root_id=? ORDER BY created", (root_id,))]
    claude_runs = [crun_view(dict(r)) for r in q("SELECT * FROM cruns WHERE root_id=? ORDER BY created", (root_id,))]
    members = [member_view(c) for c in children_of(root_id)]
    started = min([j["started"] for j in thread if j.get("started")] or [root["created"]])
    active = any(j["status"] in ("queued", "running") for j in thread + members)
    last = max([j.get("finished") or 0 for j in thread + members] or [0])
    return {"root": root_id, "mode": root.get("mode") or "chat", "role": root.get("role"), "title": root["title"],
            "started": started, "elapsed_s": round((time.time() if active else (last or time.time())) - started, 1), "active": active,
            "steps": steps_of(root_id), "members": members, "notes": notes, "bg": bg, "claude": claude_runs, "thread": [job_brief(j) for j in thread],
            "checkpoints": await ckpt_list(root_id), "state": root.get("mission_state"),
            "deliveries": _deliveries(root_id), "deliverable": _deliverable_status(root_id)}


# ---------------------------------------------------------------- app

@asynccontextmanager
async def lifespan(_: FastAPI):
    for jid in _REATTACH:
        _running[jid] = {"proc": None, "pid": job_row(jid)["pid"], "task": None}
        _running[jid]["task"] = asyncio.create_task(reattach_agent(jid))
        print(f"reattached to running agent job {jid}", file=sys.stderr)
    for cid in _CRUN_REATTACH:
        _crun_tasks[cid] = asyncio.create_task(follow_crun(cid, None, crun_row(cid)["pid"]))
        print(f"reattached to running Claude run {cid}", file=sys.stderr)
    tasks = [asyncio.create_task(status_loop()), asyncio.create_task(scheduler())]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; "
       "font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")


@app.middleware("http")
async def headers(req: Request, call_next):
    resp = await call_next(req)
    resp.headers["Content-Security-Policy"] = CSP
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Frame-Options"] = "DENY"
    if "Cache-Control" not in resp.headers or not req.url.path.startswith("/api/uploads/"):
        resp.headers["Cache-Control"] = "no-store" if req.url.path.startswith("/api") else resp.headers.get("Cache-Control", "no-cache")
    return resp


@app.get("/healthz")
async def healthz():
    return PlainTextResponse("ok")


@app.get("/healthz/relay")
async def healthz_relay():
    if await relay_ok():
        return PlainTextResponse("ok")
    raise HTTPException(503, "relay down")


class Login(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


@app.post("/api/login")
async def login(body: Login, req: Request, resp: Response):
    check_origin(req)
    ip = client_ip(req)
    _prune(_fail_ip[ip], 900)
    _prune(_fail_all, 3600)
    if len(_fail_ip[ip]) >= 10 or len(_fail_all) >= 30:
        audit(ip, "login_locked")
        raise HTTPException(429, "Too many failed attempts. Try again later.")
    conf = auth_conf()
    ok = False
    try:
        ok = secrets.compare_digest(body.username, conf["username"]) & ph.verify(conf["password_hash"], body.password)
    except (VerifyMismatchError, VerificationError):
        ok = False
    if not ok:
        _fail_ip[ip].append(time.time())
        _fail_all.append(time.time())
        audit(ip, "login_failed", body.username)
        await asyncio.sleep(1.0)
        raise HTTPException(401, "Wrong username or password.")
    _fail_ip.pop(ip, None)
    tok, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    now = time.time()
    x("INSERT INTO sessions VALUES (?,?,?,?,?,?)",
      (hashlib.sha256(tok.encode()).hexdigest(), csrf, now, now, ip, req.headers.get("user-agent", "")[:200]))
    x("DELETE FROM sessions WHERE ? - last_seen > ? OR ? - created > ?", (now, IDLE_TTL, now, ABS_TTL))
    secure = req.headers.get("x-forwarded-proto") == "https"
    resp.set_cookie(COOKIE, tok, httponly=True, secure=secure, samesite="strict",
                    path=req.headers.get("x-forwarded-prefix", "") + "/" if secure else "/", max_age=ABS_TTL)
    audit(ip, "login_ok")
    return {"ok": True, "csrf": csrf}


@app.post("/api/logout")
async def logout(req: Request, resp: Response):
    s = require(req, write=True)
    x("DELETE FROM sessions WHERE token_hash=?", (s["token_hash"],))
    resp.delete_cookie(COOKIE, path=req.headers.get("x-forwarded-prefix", "") + "/" if req.headers.get("x-forwarded-proto") == "https" else "/")
    return {"ok": True}


def roles_payload() -> dict:
    rs = rolelib.load_roles()
    modes = []
    for m, (lead, sk, turns, budget) in rolelib.MODES.items():
        modes.append({"name": m, "lead": lead, "skills": sk, "max_turns": turns, "budget_s": budget,
                      "max_budget_h": MAX_BUDGET_S // 3600, "deliver_every_h": DELIVER_DEFAULT_S / 3600 if lead else 0})
    return {"modes": modes, "roles": rolelib.role_catalog(rs, exclude_lead=True), "lead": rolelib.LEAD_ROLE,
            "slots": slots(), "max_agents": MAX_AGENTS, "efforts": (["auto"] if decider else []) + EFFORTS, "default_effort": DEFAULT_EFFORT}


@app.get("/api/me")
async def me(req: Request):
    s = require(req)
    return {"user": auth_conf()["username"], "csrf": s["csrf"], "skills": SKILLS, "chat_profiles": chat_profiles_payload(), **roles_payload()}


class ChatProfileBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=40)


def _clean_profile_name(name: str) -> str:
    n = re.sub(r"\s+", " ", name).strip()
    if not n:
        raise HTTPException(400, "give the profile a name")
    return n


@app.get("/api/chat-profiles")
async def chat_profiles_list(req: Request):
    require(req)
    return chat_profiles_payload()


@app.post("/api/chat-profiles")
async def chat_profiles_create(body: ChatProfileBody, req: Request):
    """Create a profile and switch to it: an empty chat list and a fresh Hermes home (no memories, no past sessions)."""
    require(req, write=True)
    name = _clean_profile_name(body.name)
    if q("SELECT 1 FROM chat_profiles WHERE lower(name)=lower(?)", (name,)):
        raise HTTPException(400, "a profile with that name already exists")
    pid = x("INSERT INTO chat_profiles (name, slug, created) VALUES (?,?,?)", (name, None, time.time()))
    slug = f"hud-{pid}"
    try:
        await asyncio.to_thread(ensure_profile_home, slug)
    except Exception as e:
        x("DELETE FROM chat_profiles WHERE id=?", (pid,))
        raise HTTPException(500, f"could not create the Hermes profile: {e}")
    x("UPDATE chat_profiles SET slug=? WHERE id=?", (slug, pid))
    x("INSERT OR REPLACE INTO kv (k, v) VALUES ('chat_profile', ?)", (str(pid),))
    audit(client_ip(req), "profile_create", f"{pid} {name}")
    return chat_profiles_payload()


@app.post("/api/chat-profiles/{pid}/use")
async def chat_profiles_use(pid: int, req: Request):
    require(req, write=True)
    if not chat_profile(pid):
        raise HTTPException(404, "no such profile")
    x("INSERT OR REPLACE INTO kv (k, v) VALUES ('chat_profile', ?)", (str(pid),))
    audit(client_ip(req), "profile_use", str(pid))
    return chat_profiles_payload()


@app.post("/api/chat-profiles/{pid}/rename")
async def chat_profiles_rename(pid: int, body: ChatProfileBody, req: Request):
    require(req, write=True)
    if not chat_profile(pid):
        raise HTTPException(404, "no such profile")
    name = _clean_profile_name(body.name)
    if q("SELECT 1 FROM chat_profiles WHERE lower(name)=lower(?) AND id<>?", (name, pid)):
        raise HTTPException(400, "a profile with that name already exists")
    x("UPDATE chat_profiles SET name=? WHERE id=?", (name, pid))
    return chat_profiles_payload()


@app.get("/api/roles")
async def roles_api(req: Request):
    require(req)
    return roles_payload()


@app.get("/api/status")
async def status(req: Request):
    require(req)
    return _status or await collect_status()


# ---- jobs

class AgentJob(BaseModel):
    prompt: str = Field(min_length=1, max_length=200_000)
    images: list[str] = Field(default_factory=list, max_length=6)
    files: list[str] = Field(default_factory=list, max_length=20)   # attachment ids from /api/attach (code, zips, docs...)
    skills: list[str] = []
    max_turns: int = Field(0, ge=0, le=3000)
    budget_h: float = Field(0, ge=0, le=24)          # crew missions: wall-clock budget in hours (0 = mode default)
    deliver_every_h: float = Field(0, ge=0, le=24)   # crew missions: hand over a usable snapshot this often (0 = off; unset = default)
    parent_id: int | None = None
    mode: str = "chat"
    role: str | None = None
    effort: str | None = None
    auto_skills: bool = False                        # let the decision model preload the right skills for this request
    quick: bool = True                               # let Jev answer trivial messages with a quick reply instead of the agent
    profile_id: int | None = None                    # chat profile for a new chat (follow-ups stay in their thread's profile)


def known_skill_names() -> set[str]:
    names = set(SKILLS)
    if decider:
        try:
            names |= {s["name"] for s in decider.catalog()} | decider.NEVER_ROUTE
        except Exception:
            pass
    return names


@app.post("/api/jobs/agent")
async def new_agent_job(body: AgentJob, req: Request):
    require(req, write=True)
    rs = rolelib.load_roles()
    mode, role = body.mode, body.role or None
    if mode not in rolelib.MODES:
        raise HTTPException(400, "unknown mode")
    if role and (role not in rs or role == rolelib.LEAD_ROLE):
        raise HTTPException(400, "unknown role")
    known = known_skill_names()
    skills = [s for s in dict.fromkeys(body.skills) if s in known][:8]
    images = [i for i in body.images if re.fullmatch(r"[0-9a-f]{32}", i) and (UPLOADS / f"{i}.jpg").exists()]
    files = [m for m in (attach_meta(i) for i in dict.fromkeys(body.files)) if m and m.get("state") == "ready"]
    if len(files) < len(set(body.files)):
        raise HTTPException(400, "an attachment is missing or not finished uploading")
    title = body.prompt.strip().splitlines()[0][:120]
    parent, root_id, relation, depth = None, None, None, 0
    if body.parent_id:
        parent = job_row(body.parent_id)
        if parent["kind"] != "agent" or parent.get("relation") == "child":
            raise HTTPException(400, "can only continue a top-level agent conversation")
        mode, role = parent.get("mode") or "chat", parent.get("role")
        root_id, relation = parent["root_id"], "followup"
    lead_role, mode_skills, mode_turns, mode_budget = rolelib.MODES[mode]
    want_budget = int(round(body.budget_h * 3600)) if body.budget_h else 0
    deliver_every = None
    if role:                                     # a specialist run directly by the owner
        r = rs[role]
        max_turns = body.max_turns or r["max_turns"]
        budget = r["budget_s"]
        skills = list(dict.fromkeys(skills + r["skills"]))
    elif lead_role:                              # a crew mission
        role = lead_role
        budget = min(MAX_BUDGET_S, want_budget or mode_budget)
        # the mode's default turns are sized for its default budget; a longer mission gets proportionally more
        max_turns = body.max_turns or min(3000, max(mode_turns, int(mode_turns * budget / max(1, mode_budget))))
        skills = list(dict.fromkeys(skills + mode_skills))
        if "deliver_every_h" in body.model_fields_set:
            deliver_every = int(round(body.deliver_every_h * 3600))
        elif not parent:
            deliver_every = DELIVER_DEFAULT_S
    else:                                        # plain chat
        max_turns = body.max_turns or mode_turns
        budget = 0
    effort = body.effort if body.effort in EFFORTS else (parent.get("effort") if parent else None) or DEFAULT_EFFORT.get(mode, "medium")
    if effort == "max" and not body.max_turns:
        max_turns = max(max_turns, 120)
    params = {"prompt": body.prompt, "skills": skills, "max_turns": max_turns, "images": images, "effort": effort}
    if body.effort == "auto":
        params["effort_auto"] = True
    if body.auto_skills and decider:
        params["auto_skills"] = True
    if body.quick and decider and mode == "chat" and not role and not images and not files:
        params["quick_ok"] = True
    if files:
        params["files"] = [attach_public(m) for m in files]
    if deliver_every is not None:
        if parent:                               # a follow-up changes the cadence of the whole mission
            rp = job_row(root_id)["params"]
            rp["deliver_every_s"] = deliver_every
            x("UPDATE jobs SET params=? WHERE id=?", (json.dumps(rp), root_id))
        else:
            params["deliver_every_s"] = deliver_every
    parent_id = body.parent_id
    if parent and not parent["params"].get("compact") and parent["status"] == "done" and parent.get("session_id"):
        ctx = agent_ctx()
        if ctx and thread_ctx_used(parent) > AUTO_COMPACT_FRAC * ctx:   # auto-compact: the next turn would not fit well
            parent_id = _compact_job(parent)
            audit(client_ip(req), "auto_compact", f"{parent['id']} ({thread_ctx_used(parent)} tok)")
    prof_id = (parent.get("profile_id") or 1) if parent else (body.profile_id if chat_profile(body.profile_id) else chat_profile_id())
    jid = x("INSERT INTO jobs (kind,status,created,title,params,parent_id,root_id,mode,role,depth,relation,label,max_turns,budget_s,effort,profile_id)"
            " VALUES ('agent','queued',?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), title, json.dumps(params), parent_id, root_id, mode, role, depth, relation, None, max_turns, budget, effort, prof_id))
    if not root_id:
        x("UPDATE jobs SET root_id=? WHERE id=?", (jid, jid))
    audit(client_ip(req), "job_agent", f"[{mode}{'/' + role if role else ''}] {title}")
    _queue_wakeup.set()
    await hub.send({"type": "job", "data": job_row(jid)})
    return job_row(jid)


def _compact_job(parent: dict) -> int:
    """Queue a compaction turn that resumes the parent's session and writes the summary (stored in jobs.summary)."""
    params = {"prompt": COMPACT_PROMPT, "skills": [], "max_turns": 3, "images": [], "compact": True}
    jid = x("INSERT INTO jobs (kind,status,created,title,params,parent_id,root_id,mode,role,depth,relation,max_turns,budget_s,effort,profile_id)"
            " VALUES ('agent','queued',?,?,?,?,?,?,?,0,'followup',3,600,'medium',?)",
            (time.time(), "compact context", json.dumps(params), parent["id"], parent["root_id"], parent.get("mode") or "chat", parent.get("role"),
             parent.get("profile_id") or 1))
    return jid


def thread_ctx_used(parent: dict) -> int:
    base, _ = _quick_chain(parent)      # a quick reply's own tokens say nothing about the Hermes session's size
    tok = (base or parent).get("tokens") or {}
    return int(tok.get("input") or 0) if isinstance(tok, dict) else 0


@app.post("/api/jobs/{jid}/compact")
async def compact_api(jid: int, req: Request):
    require(req, write=True)
    parent = job_row(jid)
    if parent["kind"] != "agent" or parent.get("relation") == "child" or parent["status"] != "done" or not parent.get("session_id"):
        raise HTTPException(400, "can only compact a finished top-level conversation turn")
    if parent["params"].get("compact"):
        raise HTTPException(400, "already a compaction")
    cid = _compact_job(parent)
    audit(client_ip(req), "compact", str(jid))
    _queue_wakeup.set()
    await hub.send({"type": "job", "data": job_row(cid)})
    return job_row(cid)


class BenchJob(BaseModel):
    contexts: list[int] = [128, 16384]


@app.post("/api/jobs/bench")
async def new_bench_job(body: BenchJob, req: Request):
    require(req, write=True)
    ctxs = [n for n in body.contexts if 1 <= n <= 262144][:6] or [128, 16384]
    jid = x("INSERT INTO jobs (kind,status,created,title,params,profile_id) VALUES ('bench','queued',?,?,?,?)",
            (time.time(), f"benchmark {active_profile()} @ {', '.join(map(str, ctxs))}", json.dumps({"contexts": ctxs}), chat_profile_id()))
    x("UPDATE jobs SET root_id=? WHERE id=?", (jid, jid))
    _queue_wakeup.set()
    await hub.send({"type": "job", "data": job_row(jid)})
    return job_row(jid)


class EvalJob(BaseModel):
    only: list[str] = []


@app.post("/api/jobs/eval")
async def new_eval_job(body: EvalJob, req: Request):
    require(req, write=True)
    only = [k for k in body.only if re.fullmatch(r"[A-Z][0-9]?", k)]
    label = f"hud-{active_profile()}-{time.strftime('%Y%m%d-%H%M%S')}"
    jid = x("INSERT INTO jobs (kind,status,created,title,params,profile_id) VALUES ('eval','queued',?,?,?,?)",
            (time.time(), f"eval {label}" + (f" ({', '.join(only)})" if only else ""), json.dumps({"label": label, "only": only}), chat_profile_id()))
    x("UPDATE jobs SET root_id=? WHERE id=?", (jid, jid))
    _queue_wakeup.set()
    await hub.send({"type": "job", "data": job_row(jid)})
    return job_row(jid)


@app.get("/api/jobs")
async def list_jobs(req: Request, limit: int = 100, profile: int | None = None):
    require(req)
    pid = profile if chat_profile(profile) else chat_profile_id()
    rows = q("SELECT id, kind, status, created, started, finished, title, parent_id, root_id, session_id, exit_code, mode, role,"
             " depth, relation, label, last_tool, last_tool_ts, n_tools, max_turns, tokens, effort, profile_id, (summary IS NOT NULL) AS compacted"
             " FROM jobs WHERE profile_id=? ORDER BY id DESC LIMIT ?", (pid, min(limit, 500)))
    return [job_brief(r) for r in rows]


@app.get("/api/jobs/{jid}")
async def get_job(jid: int, req: Request):
    require(req)
    j = job_row(jid)
    j["events"] = [{"seq": r["seq"], "ts": r["ts"], "etype": r["type"], "data": json.loads(r["data"])}
                   for r in q("SELECT * FROM events WHERE job_id=? ORDER BY seq", (jid,))]
    return j


@app.get("/api/jobs/{jid}/mission")
async def get_mission(jid: int, req: Request):
    require(req)
    j = job_row(jid)
    return await mission_view(j["root_id"] or jid)


class Notice(BaseModel):
    message: str = Field(min_length=3, max_length=600)
    ttl_min: int = Field(30, ge=1, le=720)


@app.post("/api/jobs/{jid}/notice")
async def notice_api(jid: int, body: Notice, req: Request):
    """Owner → running crew: the text rides along in every crew-tool result (clock/wait/status/progress/plan_update) of
    this mission for ttl_min minutes. Agents are one-shot processes, so this is the only way to reach them mid-run."""
    require(req, write=True)
    j = job_row(jid)
    root_id = j["root_id"] or jid
    nid = post_notice(root_id, body.message.strip(), body.ttl_min * 60)
    audit(client_ip(req), "notice", f"{root_id}: {body.message[:120]}")
    await mission_changed(root_id, "note")
    return {"ok": True, "id": nid, "root": root_id}


@app.post("/api/jobs/{jid}/cancel")
async def cancel_job(jid: int, req: Request):
    require(req, write=True)
    job_row(jid)
    await cancel_tree(jid)
    audit(client_ip(req), "job_cancel", str(jid))
    return {"ok": True}


@app.post("/api/bg/{tid}/stop")
async def bg_stop_api(tid: str, req: Request):
    require(req, write=True)
    audit(client_ip(req), "bg_stop", tid)
    return await bg_stop(_bg_row(tid))


class RollbackBody(BaseModel):
    checkpoint: str = Field(pattern=r"^[0-9a-f]{4,40}$")


@app.post("/api/jobs/{jid}/rollback")
async def rollback_api(jid: int, body: RollbackBody, req: Request):
    require(req, write=True)
    j = job_row(jid)
    if any(r["status"] in ("queued", "running") for r in q("SELECT status FROM jobs WHERE root_id=?", (j["root_id"],))):
        raise HTTPException(409, "mission still running; stop it first")
    audit(client_ip(req), "rollback", f"{jid} -> {body.checkpoint}")
    return await ckpt_rollback(j["root_id"], jid, body.checkpoint, [])


@app.get("/api/jobs/{jid}/diff/{checkpoint}")
async def diff_api(jid: int, checkpoint: str, req: Request, to: str = ""):
    require(req)
    job_row(jid)
    if to and not re.fullmatch(r"[0-9a-f]{4,40}", to):
        raise HTTPException(400, "bad checkpoint")
    return await ckpt_diff(checkpoint, to, [], 120000)


@app.get("/api/bg/{tid}/logs")
async def bg_logs_api(tid: str, req: Request, tail: int = 300):
    require(req)
    t = _bg_row(tid)
    return {"task": bg_view(t), "log": await bg_logs_text(t, min(max(tail, 1), 2000))}


# ---- internal crew API (loopback + token; used by crew/crew_server.py inside Hermes)

class Internal(BaseModel):
    job: int = 0
    root: int = 0
    depth: int = 0
    model_config = {"extra": "allow"}


def _caller(body: Internal) -> dict:
    j = q("SELECT * FROM jobs WHERE id=?", (body.job,))
    if not j:
        raise HTTPException(404, "unknown caller job")
    return dict(j[0])


@app.post("/internal/adopt")
async def internal_adopt(body: Internal, req: Request):
    internal(req)
    title = str(getattr(body, "title", "") or "CLI session")[:120]
    jid = x("INSERT INTO jobs (kind,status,created,started,title,params,mode,role,depth,profile_id) VALUES ('external','external',?,?,?,'{}','cli',NULL,0,?)",
            (time.time(), time.time(), title, chat_profile_id()))
    x("UPDATE jobs SET root_id=? WHERE id=?", (jid, jid))
    await hub.send({"type": "job", "data": job_row(jid)})
    return {"job": jid}


@app.post("/internal/roles")
async def internal_roles(body: Internal, req: Request):
    internal(req)
    return {"roles": rolelib.role_catalog(), "slots": slots(), "note": "one slot is yours; members beyond the free slots queue"}


@app.post("/internal/spawn")
async def internal_spawn(body: Internal, req: Request):
    internal(req)
    caller = _caller(body)
    if (caller.get("depth") or 0) >= 1:
        return {"error": "crew members cannot spawn; report back to the lead"}
    rs = rolelib.load_roles()
    role = str(getattr(body, "role", "") or "").strip()
    if role not in rs or role == rolelib.LEAD_ROLE:
        return {"error": f"unknown role '{role}'; call roles() for the roster"}
    task = str(getattr(body, "task", "") or "").strip()
    if len(task) < 20:
        return {"error": "task is too short: state the deliverable and the acceptance criteria"}
    root_id = caller["root_id"] or caller["id"]
    n_children = q("SELECT COUNT(*) FROM jobs WHERE root_id=? AND relation='child'", (root_id,))[0][0]
    if n_children >= MAX_CHILDREN_PER_MISSION:
        return {"error": f"this mission already spawned {n_children} members (limit {MAX_CHILDREN_PER_MISSION})"}
    r = rs[role]
    context = str(getattr(body, "context", "") or "").strip()
    files = [str(f) for f in (getattr(body, "files", None) or []) if isinstance(f, str)][:40]
    label = str(getattr(body, "label", "") or "").strip()[:60] or role
    max_turns = int(getattr(body, "max_turns", 0) or 0) or r["max_turns"]
    max_turns = max(5, min(max_turns, 500))
    brief = [f"# Crew brief — role: {role}" + (f" ({label})" if label != role else ""), "", "## Task", task]
    if context:
        brief += ["", "## Context", context]
    if files:
        brief += ["", "## Files to start from"] + [f"- {f}" for f in files]
    brief += ["", "When finished, reply with the member report (Result / Files / Verification / Assumptions / Observations / Open issues)."]
    params = {"prompt": "\n".join(brief), "skills": r["skills"], "max_turns": max_turns, "images": [],
              "task": task, "context": context, "files": files}
    title = f"{label}: {task.splitlines()[0][:90]}"
    jid = x("INSERT INTO jobs (kind,status,created,title,params,parent_id,root_id,mode,role,depth,relation,label,max_turns,budget_s,effort,profile_id)"
            " VALUES ('agent','queued',?,?,?,?,?,'member',?,?,'child',?,?,?,?,?)",
            (time.time(), title, json.dumps(params), caller["id"], root_id, role, (caller.get("depth") or 0) + 1, label, max_turns, r["budget_s"],
             caller.get("effort") if caller.get("effort") in EFFORTS else "high", caller.get("profile_id") or 1))
    x("INSERT INTO notes (root_id, job_id, ts, message) VALUES (?,?,?,?)", (root_id, caller["id"], time.time(), f"spawned {role} #{jid} ({label})"))
    await ckpt_create(root_id, caller["id"], f"before {role} #{jid} ({label})")
    _queue_wakeup.set()
    await hub.send({"type": "job", "data": job_row(jid)})
    await mission_changed(root_id, "spawn")
    busy = len([j for j in _running.values()])
    return {"agent_id": jid, "role": role, "label": label, "max_turns": max_turns, "budget_s": r["budget_s"],
            "slots_free": max(0, slots() - busy), "note": "runs now" if busy < slots() else "queued until a slot frees"}


def _members_for(body: Internal, caller: dict, ids: list[int]) -> list[dict]:
    root_id = caller["root_id"] or caller["id"]
    if ids:
        rows = q(f"SELECT * FROM jobs WHERE root_id=? AND relation='child' AND id IN ({','.join('?' * len(ids))}) ORDER BY id", (root_id, *ids))
    else:
        rows = q("SELECT * FROM jobs WHERE root_id=? AND relation='child' ORDER BY id", (root_id,))
    return [dict(r) for r in rows]


def _report(j: dict) -> dict:
    final = j.get("final") or ""
    files = re.findall(r"^\s*[-*]\s*(/workspace/\S+)", final, re.M)
    return {"agent_id": j["id"], "role": j.get("role"), "label": j.get("label"), "status": j["status"],
            "elapsed_s": round(((j.get("finished") or time.time()) - j["started"]), 1) if j.get("started") else 0,
            "n_tools": j.get("n_tools") or 0, "report": final[:24000] if j["status"] in ("done", "failed", "cancelled") else None,
            "files": files[:40], "error": (j.get("error") or "")[:800] or None}


@app.post("/internal/status")
async def internal_status(body: Internal, req: Request):
    internal(req)
    caller = _caller(body)
    ids = [int(i) for i in (getattr(body, "agent_ids", None) or []) if str(i).isdigit()]
    return _with_deliverable(caller, {"members": [member_view(j) for j in _members_for(body, caller, ids)], "slots": slots(),
                                      "slots_busy": len(_running), "now": time.time()})


@app.post("/internal/wait")
async def internal_wait(body: Internal, req: Request):
    internal(req)
    caller = _caller(body)
    ids = [int(i) for i in (getattr(body, "agent_ids", None) or []) if str(i).isdigit()]
    timeout = max(5, min(int(getattr(body, "timeout_s", 300) or 300), 300))
    deadline = time.time() + timeout
    if not ids:
        ids = [j["id"] for j in _members_for(body, caller, []) if j["status"] in ("queued", "running")]
    while True:
        members = _members_for(body, caller, ids) if ids else []
        pending = [m for m in members if m["status"] in ("queued", "running")]
        if not pending or time.time() >= deadline:
            return _with_deliverable(caller, {"timed_out": bool(pending), "waited_s": round(timeout - max(0, deadline - time.time()), 1),
                                              "members": [_report(m) for m in members],
                                              "still_running": [m["id"] for m in pending]})
        async with _job_done:
            try:
                await asyncio.wait_for(_job_done.wait(), min(5, max(0.1, deadline - time.time())))
            except asyncio.TimeoutError:
                pass


@app.post("/internal/result")
async def internal_result(body: Internal, req: Request):
    internal(req)
    caller = _caller(body)
    ms = _members_for(body, caller, [int(getattr(body, "agent_id", 0) or 0)])
    if not ms:
        return {"error": "no such crew member in this mission"}
    j = ms[0]
    out = _report(j)
    out["brief"] = {"task": json.loads(j["params"] or "{}").get("task"), "files": json.loads(j["params"] or "{}").get("files")}
    if j["status"] in ("queued", "running"):
        out["note"] = "still running; use wait()"
    return out


@app.post("/internal/cancel")
async def internal_cancel(body: Internal, req: Request):
    internal(req)
    caller = _caller(body)
    ms = _members_for(body, caller, [int(getattr(body, "agent_id", 0) or 0)])
    if not ms:
        return {"error": "no such crew member in this mission"}
    await cancel_tree(ms[0]["id"])
    x("INSERT INTO notes (root_id, job_id, ts, message) VALUES (?,?,?,?)", (caller["root_id"], caller["id"], time.time(), f"cancelled #{ms[0]['id']} ({ms[0].get('label')})"))
    await mission_changed(caller["root_id"], "cancel")
    return {"agent_id": ms[0]["id"], "status": "cancelling"}


@app.post("/internal/plan_set")
async def internal_plan_set(body: Internal, req: Request):
    internal(req)
    caller = _caller(body)
    root_id = caller["root_id"] or caller["id"]
    steps = getattr(body, "steps", None) or []
    if not isinstance(steps, list) or not steps:
        return {"error": "steps must be a non-empty list of {id, title, detail?}"}
    existing = {s["step_id"]: s for s in steps_of(root_id)}
    keep, now = [], time.time()
    for i, s in enumerate(steps[:30]):
        if not isinstance(s, dict):
            continue
        sid = str(s.get("id") or f"s{i + 1}")[:24]
        title = str(s.get("title") or "")[:200].strip()
        if not title:
            continue
        detail = str(s.get("detail") or "")[:500]
        keep.append(sid)
        if sid in existing:
            x("UPDATE steps SET ord=?, title=?, detail=?, updated=? WHERE root_id=? AND step_id=?", (i, title, detail, now, root_id, sid))
        else:
            x("INSERT INTO steps (root_id, step_id, ord, title, detail, status, note, updated) VALUES (?,?,?,?,?,'pending','',?)",
              (root_id, sid, i, title, detail, now))
    if keep:
        x(f"DELETE FROM steps WHERE root_id=? AND step_id NOT IN ({','.join('?' * len(keep))})", (root_id, *keep))
    x("INSERT INTO notes (root_id, job_id, ts, message) VALUES (?,?,?,?)", (root_id, caller["id"], now, f"plan published: {len(keep)} steps"))
    await mission_changed(root_id, "plan")
    return {"ok": True, "steps": [{k: s[k] for k in ("step_id", "title", "status")} for s in steps_of(root_id)]}


@app.post("/internal/plan_update")
async def internal_plan_update(body: Internal, req: Request):
    internal(req)
    caller = _caller(body)
    root_id = caller["root_id"] or caller["id"]
    sid, status = str(getattr(body, "step_id", "") or "")[:24], str(getattr(body, "status", "") or "").lower()
    note = str(getattr(body, "note", "") or "")[:500]
    if status not in STEP_STATUSES:
        return {"error": f"status must be one of {sorted(STEP_STATUSES)}"}
    rows = q("SELECT * FROM steps WHERE root_id=? AND step_id=?", (root_id, sid))
    if not rows:
        return {"error": f"no step '{sid}' in the plan; call plan_set first", "steps": [s["step_id"] for s in steps_of(root_id)]}
    s, now = dict(rows[0]), time.time()
    started = s["started"] or (now if status == "active" else None)
    finished = now if status in ("done", "blocked", "skipped") else None
    if status in ("done", "blocked", "skipped") and not started:
        started = s["updated"] or now
    x("UPDATE steps SET status=?, note=?, started=?, finished=?, updated=? WHERE root_id=? AND step_id=?",
      (status, note or s["note"], started, finished, now, root_id, sid))
    x("INSERT INTO notes (root_id, job_id, ts, message) VALUES (?,?,?,?)", (root_id, caller["id"], now, f"step {sid} → {status}" + (f": {note}" if note else "")))
    await mission_changed(root_id, "plan")
    return _with_deliverable(caller, {"ok": True, "step": sid, "status": status, "elapsed_s": round((finished or now) - started, 1) if started else None})


@app.post("/internal/progress")
async def internal_progress(body: Internal, req: Request):
    internal(req)
    caller = _caller(body)
    root_id = caller["root_id"] or caller["id"]
    msg = str(getattr(body, "message", "") or "").strip()[:500]
    if not msg:
        return {"error": "empty message"}
    x("INSERT INTO notes (root_id, job_id, ts, message) VALUES (?,?,?,?)", (root_id, caller["id"], time.time(), msg))
    await mission_changed(root_id, "note")
    return _with_deliverable(caller, {"ok": True})


@app.post("/internal/clock")
async def internal_clock(body: Internal, req: Request):
    internal(req)
    caller = _caller(body)
    root_id = caller["root_id"] or caller["id"]
    now = time.time()
    members = children_of(root_id)
    counts = defaultdict(int)
    for m in members:
        counts[m["status"]] += 1
    budget = caller.get("budget_s") or 0
    started = caller.get("started") or caller["created"]
    bg = q("SELECT id, name, created FROM bgtasks WHERE root_id=? AND state='running'", (root_id,))
    return _with_deliverable(caller, {"now": time.strftime("%Y-%m-%d %H:%M:%S"), "your_elapsed_s": round(now - started, 1),
            "your_budget_s": budget, "your_budget_left_s": round(max(0, budget - (now - started)), 1) if budget else None,
            "your_max_turns": caller.get("max_turns"), "your_tool_calls": caller.get("n_tools") or 0,
            "mission_elapsed_s": round(now - (job_row(root_id).get("started") or job_row(root_id)["created"]), 1),
            "slots": {"total": slots(), "busy": len(_running), "free": max(0, slots() - len(_running))},
            "crew": dict(counts), "bg_running": [{"task_id": b["id"], "name": b["name"], "elapsed_s": round(now - b["created"], 1)} for b in bg]})


# ---- deliverables: periodic usable snapshots of the product, zipped for the owner (lead-only `deliver` crew tool)

def _deliveries(root_id: int) -> list[dict]:
    return [dict(r) for r in q("SELECT * FROM deliveries WHERE root_id=? ORDER BY n", (root_id,))]


def _deliverable_status(root_id: int) -> dict | None:
    """Countdown to the next due deliverable for a mission with a cadence (root params.deliver_every_s), else None."""
    try:
        root = job_row(root_id)
    except Exception:
        return None
    every = int((root.get("params") or {}).get("deliver_every_s") or 0)
    if not every:
        return None
    rows = _deliveries(root_id)
    now = time.time()
    last = rows[-1]["ts"] if rows else (root.get("started") or root["created"])
    due_in = last + every - now
    d = {"every_s": every, "count": len(rows), "last_at": rows[-1]["ts"] if rows else None,
         "last_zip": rows[-1]["zip"] if rows else None, "next_due_in_s": int(due_in), "overdue": due_in < 0}
    if due_in < 0:
        d["note"] = (f"DELIVERABLE OVERDUE by {int(-due_in // 60)} min: start no new work; stabilise what exists, run the checks, "
                     f"update the README, then call deliver(summary, paths, version, how_to_run).")
    elif due_in < 20 * 60:
        d["note"] = f"Deliverable due in {int(due_in // 60)} min: converge to a runnable state now, then call deliver(...)."
    return d


def _with_deliverable(caller: dict, d: dict) -> dict:
    """Annotate a crew-tool result: the lead's deliverable countdown, and any live owner notice for this mission
    (a message the owner posted while the agents run — the only channel into a running one-shot session)."""
    root_id = caller["root_id"] or caller["id"]
    if (caller.get("depth") or 0) == 0:
        ds = _deliverable_status(root_id)
        if ds:
            d["deliverable"] = ds
    now = time.time()
    rows = q("SELECT ts, message FROM notices WHERE (root_id=? OR root_id=0) AND expires > ? ORDER BY id", (root_id, now))
    if rows:
        d["owner_notice"] = [{"at": time.strftime("%H:%M", time.localtime(r["ts"])), "message": r["message"]} for r in rows[-3:]]
    return d


def post_notice(root_id: int, message: str, ttl_s: int = 1800) -> int:
    """Store a live notice that every crew-tool result for this mission (0 = all missions) carries for ttl_s seconds."""
    nid = x("INSERT INTO notices (root_id, ts, expires, message) VALUES (?,?,?,?)", (root_id, time.time(), time.time() + ttl_s, message[:600]))
    if root_id:
        x("INSERT INTO notes (root_id, job_id, ts, message) VALUES (?,?,?,?)", (root_id, None, time.time(), f"[owner notice] {message[:300]}"))
    return nid


def _ws_path(raw: str) -> Path:
    """A file or directory inside /workspace, from any of the spellings agents use."""
    raw = (raw or "").strip()
    for pre in ("sandbox:", "file://"):
        if raw.startswith(pre):
            raw = raw[len(pre):]
    for pre in ("/workspace", str(WORKSPACE)):
        if raw == pre or raw.startswith(pre + "/"):
            raw = raw[len(pre):].lstrip("/")
            break
    if raw.startswith("/") or ".." in Path(raw).parts:
        raise ValueError(f"outside the workspace: {raw}")
    root = WORKSPACE.resolve()
    p = (root / raw).resolve()
    if p != root and root not in p.parents:
        raise ValueError(f"outside the workspace: {raw}")
    if not p.exists():
        raise ValueError(f"does not exist: {raw or '.'}")
    return p


def _deliver_copy(paths: list[Path], dest: Path, meta: dict) -> tuple[int, int, list[str]]:
    """Copy the product files into the delivery folder (skipping VCS/venv/cache dirs and huge files), write DELIVERY.md."""
    root, files, total, skipped = WORKSPACE.resolve(), 0, 0, []
    budget = 500 * 1024 * 1024
    for src in paths:
        pairs = []
        if src.is_file():
            pairs.append((src, dest / src.relative_to(root)))
        else:
            for dp, dns, fns in os.walk(src):
                dns[:] = [d for d in dns if d not in DELIVER_SKIP_DIRS]
                for fn in fns:
                    fp = Path(dp) / fn
                    if fp.is_symlink():
                        continue
                    pairs.append((fp, dest / fp.relative_to(root)))
        for fp, tp in pairs:
            try:
                sz = fp.stat().st_size
            except OSError:
                continue
            if sz > 50 * 1024 * 1024 or total + sz > budget:
                skipped.append(str(fp.relative_to(root)))
                continue
            tp.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fp, tp)
            files += 1
            total += sz
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "DELIVERY.md").write_text(
        f"# Deliverable {meta['n']}{' ' + meta['version'] if meta['version'] else ''} — {meta['title']}\n\n"
        f"Delivered {time.strftime('%Y-%m-%d %H:%M:%S')} · checkpoint {meta['checkpoint'] or '?'} · mission #{meta['root_id']}\n\n"
        f"## What this is\n{meta['summary']}\n\n## How to run\n{meta['how_to_run'] or '(see README)'}\n\n"
        f"## Contents\n" + "\n".join(f"- {p.relative_to(root)}" for p in paths)
        + (f"\n\n## Skipped (too large)\n" + "\n".join(f"- {s}" for s in skipped[:50]) if skipped else "") + "\n")
    return files + 1, total, skipped


@app.post("/internal/deliver")
async def internal_deliver(body: Internal, req: Request):
    internal(req)
    caller = _caller(body)
    root_id = caller["root_id"] or caller["id"]
    if (caller.get("depth") or 0) != 0:
        return {"error": "only the mission lead delivers; put the runnable state in your report and let the lead call deliver"}
    summary = str(getattr(body, "summary", "") or "").strip()[:4000]
    how = str(getattr(body, "how_to_run", "") or "").strip()[:4000]
    version = re.sub(r"[^\w.\-]", "", str(getattr(body, "version", "") or ""))[:40]
    raw_paths = [str(p) for p in (getattr(body, "paths", None) or []) if isinstance(p, str) and p.strip()]
    if len(summary) < 20:
        return {"error": "summary too short (>= 20 chars): what works, what is not in yet, and the check results"}
    if not raw_paths:
        return {"error": "paths is empty: list the files/directories that make up the usable product (include the README)"}
    try:
        paths = [_ws_path(p) for p in raw_paths[:40]]
    except ValueError as e:
        return {"error": str(e)}
    deliv_root = (WORKSPACE / "outbox" / "deliveries").resolve()
    if any(p == deliv_root or deliv_root in p.parents or p == WORKSPACE.resolve() for p in paths):
        return {"error": "list the product's own files/directories, not /workspace itself or outbox/deliveries"}
    n = len(_deliveries(root_id)) + 1
    ck = await ckpt_create(root_id, caller["id"], f"deliverable {n}" + (f" {version}" if version else ""))
    name = time.strftime("%Y%m%d-%H%M") + f"-v{n}" + (f"-{version}" if version else "")
    dest = deliv_root / str(root_id) / name
    meta = {"n": n, "version": version, "title": job_row(root_id)["title"], "checkpoint": ck.get("checkpoint"), "root_id": root_id,
            "summary": summary, "how_to_run": how}
    try:
        files, nbytes, skipped = await asyncio.to_thread(_deliver_copy, paths, dest, meta)
        zip_path = await asyncio.to_thread(shutil.make_archive, str(dest), "zip", str(dest.parent), dest.name)
    except OSError as e:
        shutil.rmtree(dest, ignore_errors=True)
        return {"error": f"could not build the delivery: {e}"}
    rel, relzip = str(dest.relative_to(WORKSPACE)), str(Path(zip_path).relative_to(WORKSPACE))
    x("INSERT INTO deliveries (root_id, job_id, ts, n, version, summary, how_to_run, path, zip, checkpoint, files, bytes, skipped)"
      " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
      (root_id, caller["id"], time.time(), n, version, summary, how, "/workspace/" + rel, "/workspace/" + relzip, ck.get("checkpoint"),
       files, nbytes, json.dumps(skipped[:50])))
    x("INSERT INTO notes (root_id, job_id, ts, message) VALUES (?,?,?,?)",
      (root_id, caller["id"], time.time(), f"deliverable {n}{' ' + version if version else ''} ({files} files): {summary[:160]} → /workspace/{relzip}"))
    await mission_changed(root_id, "deliver")
    ds = _deliverable_status(root_id)
    return {"ok": True, "n": n, "path": "/workspace/" + rel, "zip": "/workspace/" + relzip, "checkpoint": ck.get("checkpoint"),
            "files": files, "bytes": nbytes, "skipped": skipped[:20], "next_due_in_s": ds["next_due_in_s"] if ds else None,
            "note": "Mention the zip path in a progress note and in your final report; the owner gets a download card."}


@app.post("/internal/checkpoint")
async def internal_checkpoint(body: Internal, req: Request):
    internal(req)
    caller = _caller(body)
    label = str(getattr(body, "label", "") or "checkpoint").strip()[:120]
    out = await ckpt_create(caller["root_id"] or caller["id"], caller["id"], label)
    if "checkpoint" in out:
        x("INSERT INTO notes (root_id, job_id, ts, message) VALUES (?,?,?,?)", (caller["root_id"] or caller["id"], caller["id"], time.time(), f"checkpoint {out['checkpoint']}: {label} ({out['changes']})"))
    return out


@app.post("/internal/checkpoints")
async def internal_checkpoints(body: Internal, req: Request):
    internal(req)
    caller = _caller(body)
    return {"checkpoints": await ckpt_list(caller["root_id"] or caller["id"])}


@app.post("/internal/diff")
async def internal_diff(body: Internal, req: Request):
    internal(req)
    _caller(body)
    return await ckpt_diff(str(getattr(body, "from_checkpoint", "") or ""), str(getattr(body, "to_checkpoint", "") or ""),
                           getattr(body, "paths", None) or [], int(getattr(body, "max_chars", 60000) or 60000))


@app.post("/internal/rollback")
async def internal_rollback(body: Internal, req: Request):
    internal(req)
    caller = _caller(body)
    if (caller.get("depth") or 0) >= 1:
        return {"error": "only the lead may roll back; report the problem instead"}
    return await ckpt_rollback(caller["root_id"] or caller["id"], caller["id"], str(getattr(body, "checkpoint", "") or ""), getattr(body, "paths", None) or [])


@app.post("/internal/state_save")
async def internal_state_save(body: Internal, req: Request):
    internal(req)
    caller = _caller(body)
    if (caller.get("depth") or 0) >= 1:
        return {"error": "only the lead saves mission state; members report in their final message"}
    root_id = caller["root_id"] or caller["id"]
    summary = str(getattr(body, "summary", "") or "").strip()[:20000]
    if len(summary) < 40:
        return {"error": "summary too short: goal, decisions, files touched, what is done, what is left"}
    stamped = f"(saved {time.strftime('%Y-%m-%d %H:%M')} by job #{caller['id']})\n{summary}"
    x("UPDATE jobs SET mission_state=? WHERE id=?", (stamped, root_id))
    try:
        (CREW_DIR / f"mission-{root_id}.md").write_text(f"# Mission {root_id} state\n\n{stamped}\n")
    except OSError:
        pass
    x("INSERT INTO notes (root_id, job_id, ts, message) VALUES (?,?,?,?)", (root_id, caller["id"], time.time(), "mission state saved (resume point)"))
    await mission_changed(root_id, "state")
    return {"ok": True, "file": f"/workspace/.crew/mission-{root_id}.md", "chars": len(summary)}


@app.post("/internal/state_get")
async def internal_state_get(body: Internal, req: Request):
    internal(req)
    caller = _caller(body)
    root = job_row(caller["root_id"] or caller["id"])
    return {"state": root.get("mission_state") or "", "file": f"/workspace/.crew/mission-{root['id']}.md"}


@app.post("/internal/chats_list")
async def internal_chats_list(body: Internal, req: Request):
    """The owner's conversations (top-level threads), for agents that need context from another chat."""
    internal(req)
    caller = _caller(body)
    qry = str(getattr(body, "query", "") or "").strip()
    limit = max(1, min(int(getattr(body, "limit", 20) or 20), 60))
    rows = q("SELECT r.id, r.title, r.mode, r.created, (SELECT COUNT(*) FROM jobs f WHERE f.root_id=r.id AND (f.relation IS NULL OR f.relation='followup')) AS turns,"
             " (SELECT MAX(COALESCE(f.finished, f.created)) FROM jobs f WHERE f.root_id=r.id) AS last FROM jobs r"
             " WHERE r.kind='agent' AND r.id=r.root_id AND r.relation IS NULL AND r.profile_id=? ORDER BY last DESC LIMIT 400",
             (caller.get("profile_id") or 1,))
    words = [w.lower() for w in re.findall(r"\w+", qry)][:8]
    out = []
    for r in rows:
        if r["id"] == (caller["root_id"] or caller["id"]):
            continue
        if words:
            text = " ".join((t["params"] or "") + " " + (t["final"] or "") for t in q("SELECT params, final FROM jobs WHERE root_id=? AND (relation IS NULL OR relation='followup')", (r["id"],))).lower()
            if not all(w in text for w in words):
                continue
        out.append({"chat_id": r["id"], "title": r["title"], "mode": r["mode"] or "chat", "turns": r["turns"],
                    "last": time.strftime("%Y-%m-%d %H:%M", time.localtime(r["last"] or r["created"]))})
        if len(out) >= limit:
            break
    return {"chats": out, "note": "use chat_read(chat_id) for the content"}


@app.post("/internal/chat_read")
async def internal_chat_read(body: Internal, req: Request):
    internal(req)
    caller = _caller(body)
    cid = int(getattr(body, "chat_id", 0) or 0)
    max_chars = max(2000, min(int(getattr(body, "max_chars", 20000) or 20000), 60000))
    root = q("SELECT root_id FROM jobs WHERE id=? AND kind='agent' AND profile_id=?", (cid, caller.get("profile_id") or 1))
    if not root:
        return {"error": "no such chat; call chats_list()"}
    rid = root[0]["root_id"] or cid
    turns = []
    for t in q("SELECT id, created, params, final, status, summary, mode, role FROM jobs WHERE root_id=? AND (relation IS NULL OR relation='followup') ORDER BY id", (rid,)):
        prm = json.loads(t["params"] or "{}")
        if prm.get("compact"):
            turns.append(f"### [context compacted — summary]\n{t['summary'] or t['final'] or ''}")
            continue
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(t["created"]))
        turns.append(f"### Owner ({when}, turn #{t['id']})\n{prm.get('prompt', '')}\n\n### Assistant ({t['status']})\n{t['final'] or '(no answer)'}")
    text, dropped = "\n\n".join(turns), 0
    while len(text) > max_chars and len(turns) > 1:
        turns.pop(0); dropped += 1; text = "\n\n".join(turns)
    if len(text) > max_chars:
        text = text[-max_chars:]
    return {"chat_id": rid, "turns_dropped_oldest": dropped, "text": text}


# ---- persistent toolbox, host runner, Claude Remote Control (2026-11-03) ----
# The toolbox (/mnt/data/la-toolbox, `tb` CLI) is shared by every chat/mission/profile and works in the sandbox and on
# the host. Host tools = scripts in la-toolbox/host/ run on the real desktop by scripts/host_run.py under bwrap (secrets
# hidden, home read-only except game dirs, own PID namespace). Remote Control = the owner's `claude-rc` user unit.
TOOLBOX = Path("/mnt/data/la-toolbox")
HOST_RUNS = TOOLBOX / "logs/host"
DESKTOP_ENV = {"XDG_RUNTIME_DIR": "/run/user/1000", "WAYLAND_DISPLAY": "wayland-1", "DISPLAY": ":0",
               "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"}
RC_UNIT = "claude-rc.service"


def toolbox_brief() -> str:
    """Short toolbox section for every agent prompt: what is already installed, so nothing gets re-installed."""
    try:
        tools = json.loads((TOOLBOX / "registry.json").read_text()).get("tools", {})
    except Exception:
        return ""
    host = sorted(n for n, t in tools.items() if t.get("where") == "host")
    names = sorted(n for n, t in tools.items() if t.get("where") != "host" and not n.startswith(("py-", "npm-")))
    return ("# Persistent toolbox (survives every chat, mission and sandbox restart)\n"
            "`/mnt/data/la-toolbox` is on PATH in your terminal (if not: `export PATH=/mnt/data/la-toolbox/bin:/mnt/data/la-toolbox/shims:$PATH:"
            "/mnt/data/la-toolbox/env/bin`). BEFORE installing or setting anything up, run `tb find <word>` / `tb show <name>` "
            "and reuse what an earlier chat already made. Install new things ONLY through the toolbox so they persist: "
            "`tb install <conda-forge pkg>`, `tb pip <pkg>` (shared venv, run with `tbpy`), `tb npm <pkg>`, `tb env <name> <pkgs>`; "
            "downloads/apps go under /mnt/data/la-toolbox/opt/. After anything works (a tool, a launch procedure, a test "
            "harness), `tb register <name> --desc ... --run ... --install ... --notes ...` so the next chat can repeat it. "
            "Desktop-only things (Lunar Client, a GPU Minecraft client, GUI apps, screenshots) run on the real screen via the "
            "crew tools `host_run(tool, args)` (scripts in /mnt/data/la-toolbox/host/) and `screenshot()`.\n"
            f"Installed: {', '.join(names)[:1500]}\nHost tools: {', '.join(host) or 'none yet'}\n"
            "Python libs (`tbpy`): numpy pandas polars scipy sklearn matplotlib plotly duckdb playwright httpx selectolax "
            "trafilatura pypdf pdfplumber portablemc pwntools. JDKs: `eval \"$(tb java 17|21|25 --export)\"`.")


PDF_RE = re.compile(r"\bpdfs?\b", re.I)
PDF_BRIEF = ("# PDFs: always extravagant\nThe owner wants EVERY PDF you make to be gorgeous. Whenever a PDF is requested or "
             "produced, follow the `pretty-pdf` skill (skill_view pretty-pdf): write the content as Markdown/HTML, render with "
             "`prettypdf` (themes: editorial, luxe, tech, academic, or auto), check the page previews it prints, and fix "
             "anything ugly before you deliver. Never hand over a plain/default-styled PDF.")


async def rc_status() -> dict:
    keys = "ActiveState,SubState,ExecMainStartTimestamp,NRestarts,UnitFileState,MainPID"
    rc, out = await _run("systemctl", "--user", "show", RC_UNIT, "-p", keys, timeout=10)
    st = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    _, auth = await _run("/home/user/.npm-global/bin/claude", "auth", "status", timeout=20)
    try:
        logged_in = bool(json.loads(auth).get("loggedIn"))
    except Exception:
        logged_in = None
    # only counts/keywords from the journal, never its text (it carries the owner's Claude sessions)
    _, jl = await _run("journalctl", "--user", "-u", RC_UNIT, "--since", "-3min", "--no-pager", "-o", "cat", timeout=15)
    low = jl.lower()
    connected = "capacity:" in low or "continue coding in the claude" in low
    errors = sum(low.count(k) for k in ("disconnected", "reconnect", "unauthorized", "401", "expired", "not logged in", "error:"))
    active = st.get("ActiveState") == "active"
    healthy = active and connected and logged_in is not False
    return {"unit": RC_UNIT, "active": active, "state": f"{st.get('ActiveState')}/{st.get('SubState')}",
            "enabled": st.get("UnitFileState"), "since": st.get("ExecMainStartTimestamp"), "restarts": st.get("NRestarts"),
            "logged_in": logged_in, "connected_recently": connected, "error_mentions_3min": errors, "healthy": healthy,
            "mode": "bypassPermissions (dangerously skip permissions)", "name": "home server",
            "note": ("online — sessions can be opened at https://claude.ai/code (environment 'home server')" if healthy else
                     "NOT healthy: " + ("unit not running" if not active else "not logged in — the owner must run `claude auth login` on the desktop"
                                         if logged_in is False else "no connection banner in the last 3 min"))}


@app.post("/internal/remote_control")
async def internal_remote_control(body: Internal, req: Request):
    internal(req)
    caller = _caller(body)
    action = str(getattr(body, "action", "status") or "status")
    force = bool(getattr(body, "force", False))
    reason = str(getattr(body, "reason", "") or "")[:300]
    before = await rc_status()
    if action == "status":
        return before
    if action not in ("start", "restart"):
        return {"error": "action must be status, start or restart"}
    if before["logged_in"] is False:
        return {**before, "error": "Claude is logged out; restarting will not help. Ask the owner to run `claude auth login` on the desktop "
                                   "(do NOT run it yourself — the sandbox copy of claude is a different install)."}
    if action == "start" or not before["active"]:
        await _run("systemctl", "--user", "enable", RC_UNIT, timeout=15)
        rc, out = await _run("systemctl", "--user", "start", RC_UNIT, timeout=30)
        done = "started" if not before["active"] else "already running (start is a no-op)"
    else:
        if before["healthy"] and not force:
            return {**before, "error": "Remote Control is healthy; restarting would cut off the owner's live Claude sessions. "
                                       "Pass force=true with a reason only if the owner asked for a restart."}
        rc, out = await _run("systemctl", "--user", "restart", RC_UNIT, timeout=30)
        done = "restarted"
    x("INSERT INTO notes (root_id, job_id, ts, message) VALUES (?,?,?,?)",
      (caller["root_id"] or caller["id"], caller["id"], time.time(), f"🛰 Remote Control {done}" + (f" — {reason}" if reason else "")))
    await asyncio.sleep(8)
    return {"done": done, "systemctl_rc": rc, "output": out.strip()[-400:], "status": await rc_status()}


def _host_run_result(rid: str) -> dict | None:
    f = HOST_RUNS / f"{rid}.json"
    if f.exists():
        try:
            tmp = f.with_suffix(".tmp")                 # the runner's own stderr; empty on a normal run
            if tmp.exists() and tmp.stat().st_size == 0:
                tmp.unlink()
            return json.loads(f.read_text())
        except Exception:
            return None
    return None


@app.post("/internal/host_run")
async def internal_host_run(body: Internal, req: Request):
    internal(req)
    caller = _caller(body)
    tool = str(getattr(body, "tool", "") or "")
    args = [str(a) for a in (getattr(body, "args", None) or [])][:50]
    timeout_s = max(5, min(int(getattr(body, "timeout_s", 600) or 600), 4 * 3600))
    wait_s = max(0, min(int(getattr(body, "wait_s", 120) or 0), 540))
    HOST_RUNS.mkdir(parents=True, exist_ok=True)
    rid = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
    out = HOST_RUNS / f"{rid}.json"
    env = os.environ | DESKTOP_ENV | {"HOST_RUN_TIMEOUT": str(timeout_s)}
    with open(out.with_suffix(".tmp"), "wb") as fh:
        subprocess.Popen(["/bin/sh", "-c", f'"$0" "$@" > "{out}.part" && mv "{out}.part" "{out}"', str(ROOT / "scripts/host_run.py"), tool, *args],
                         env=env, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True,
                         executable="/bin/sh")
    x("INSERT INTO notes (root_id, job_id, ts, message) VALUES (?,?,?,?)",
      (caller["root_id"] or caller["id"], caller["id"], time.time(), f"🖥 host_run {tool} {' '.join(args)[:200]}"))
    t0 = time.time()
    while time.time() - t0 < wait_s:
        r = _host_run_result(rid)
        if r is not None:
            return {"run_id": rid, **r}
        await asyncio.sleep(1)
    return {"run_id": rid, "state": "running", "hint": "still running — call host_run_status(run_id) later; take screenshot() to see the screen"}


@app.post("/internal/host_run_status")
async def internal_host_run_status(body: Internal, req: Request):
    internal(req)
    rid = re.sub(r"[^0-9a-f-]", "", str(getattr(body, "run_id", "") or ""))
    r = _host_run_result(rid)
    if r is not None:
        return {"run_id": rid, **r}
    if (HOST_RUNS / f"{rid}.json.part").exists() or (HOST_RUNS / f"{rid}.tmp").exists():
        logs = sorted(HOST_RUNS.glob("*.log"), key=lambda p: p.stat().st_mtime)[-3:]
        return {"run_id": rid, "state": "running", "recent_logs": [str(p) for p in logs]}
    return {"error": f"unknown run {rid}"}


@app.post("/internal/screenshot")
async def internal_screenshot(body: Internal, req: Request):
    """Screenshot of the owner's real desktop (or one output/region) into /workspace/outbox/screens/ for vision_analyze."""
    internal(req)
    d = WORKSPACE / "outbox/screens"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"screen-{time.strftime('%Y%m%d-%H%M%S')}.png"
    region = str(getattr(body, "region", "") or "")
    cmd = ["grim"] + (["-g", region] if re.fullmatch(r"\d+,\d+ \d+x\d+", region) else []) + [str(f)]
    p = await asyncio.create_subprocess_exec(*cmd, env=os.environ | DESKTOP_ENV, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.STDOUT)
    out, _ = await asyncio.wait_for(p.communicate(), 20)
    if p.returncode != 0:
        return {"error": out.decode(errors="replace")[-500:]}
    try:   # shrink for the vision model
        from PIL import Image
        im = Image.open(f)
        if im.width > 1920:
            im.thumbnail((1920, 1920))
            im.save(f)
        size = im.size
    except Exception:
        size = None
    return {"path": f"/workspace/outbox/screens/{f.name}", "size": size,
            "hint": "look at it with vision_analyze(image_url=<path>) — it is the owner's live screen"}


@app.post("/internal/bg/start")
async def internal_bg_start(body: Internal, req: Request):
    internal(req)
    caller = _caller(body)
    root_id = caller["root_id"] or caller["id"]
    name = str(getattr(body, "name", "") or "task").strip()[:80]
    command = str(getattr(body, "command", "") or "").strip()
    if not command:
        return {"error": "empty command"}
    running = q("SELECT COUNT(*) FROM bgtasks WHERE state='running'")[0][0]
    if running >= 12:
        return {"error": "12 background tasks already running on this box; stop one first"}
    out = await bg_start(root_id, caller["id"], name, command, int(getattr(body, "timeout_s", 3600) or 3600))
    if "task_id" in out:
        x("INSERT INTO notes (root_id, job_id, ts, message) VALUES (?,?,?,?)", (root_id, caller["id"], time.time(), f"background task started: {name} ({out['task_id']})"))
    return out


@app.post("/internal/bg/status")
async def internal_bg_status(body: Internal, req: Request):
    internal(req)
    caller = _caller(body)
    root_id = caller["root_id"] or caller["id"]
    tid = str(getattr(body, "task_id", "") or "")
    rows = q("SELECT * FROM bgtasks WHERE root_id=? AND (?='' OR id=?) ORDER BY created", (root_id, tid, tid))
    if tid and not rows:
        return {"error": "no such task in this mission"}
    return {"tasks": [bg_view(await bg_refresh(dict(r))) for r in rows]}


@app.post("/internal/bg/logs")
async def internal_bg_logs(body: Internal, req: Request):
    internal(req)
    caller = _caller(body)
    tid = str(getattr(body, "task_id", "") or "")
    rows = q("SELECT * FROM bgtasks WHERE root_id=? AND id=?", (caller["root_id"] or caller["id"], tid))
    if not rows:
        return {"error": "no such task in this mission"}
    t = await bg_refresh(dict(rows[0]))
    tail = min(max(int(getattr(body, "tail", 200) or 200), 1), 1000)
    return {"task": bg_view(t, 0), "log": (await bg_logs_text(t, tail))[-30000:]}


@app.post("/internal/bg/stop")
async def internal_bg_stop(body: Internal, req: Request):
    internal(req)
    caller = _caller(body)
    tid = str(getattr(body, "task_id", "") or "")
    rows = q("SELECT * FROM bgtasks WHERE root_id=? AND id=?", (caller["root_id"] or caller["id"], tid))
    if not rows:
        return {"error": "no such task in this mission"}
    return await bg_stop(dict(rows[0]))


# ---------------------------------------------------------------- Claude Code delegation (claude/gate.py + the owner's logged-in `claude` CLI)
# A "Claude run" is one `claude -p` process on /workspace, started only after the gate (claude/gate.py) has sanitised,
# formatted and (with the local judge) approved the brief. The exact brief that went out is stored in `cruns.brief`; every
# gate decision, blocked or not, is logged in `cgate`. Runs survive a HUD restart like agents do (stream-json to a file,
# reattached by pid on start). Checkpoints are taken before and after, so the workspace diff of each run is reviewable.

_crun_cancel: set[str] = set()
_crun_tasks: dict[str, Any] = {}
_crun_done = asyncio.Condition()


def _pid_is_claude(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        return b"claude" in Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False


def crun_row(cid: str) -> dict:
    r = q("SELECT * FROM cruns WHERE id=?", (cid,))
    if not r:
        raise HTTPException(404, "no such Claude run")
    return dict(r[0])


def crun_view(t: dict, tail_lines: int = 8, full: bool = False) -> dict:
    d = {k: t.get(k) for k in ("id", "state", "model", "effort", "mode", "label", "created", "finished", "job_id", "root_id", "timeout_s",
                                "max_turns", "num_turns", "exit_code", "cost_usd", "served_model", "n_tools", "last_tool", "ckpt_before", "ckpt_after")}
    d["elapsed_s"] = round((t.get("finished") or time.time()) - t["created"], 1)
    d["last_tool_ago_s"] = round(time.time() - t["last_tool_ts"], 1) if t.get("last_tool_ts") else None
    d["files"] = json.loads(t.get("files") or "[]")
    d["error"] = (t.get("error") or "")[:400] or None
    d["tail"] = (t.get("log_tail") or "").splitlines()[-tail_lines:] if tail_lines else []
    if full:
        d["brief"] = t.get("brief")
        d["final"] = t.get("final")
        d["verdict"] = json.loads(t.get("verdict") or "{}")
        d["usage"] = json.loads(t.get("usage") or "null")
        d["denials"] = json.loads(t.get("denials") or "[]")
        d["session_id"] = t.get("session_id")
    return d


# Each run works in its own staging dir (claude-runs/<id>) holding ONLY the files the brief names, never the whole workspace:
# whatever Claude reads goes to Anthropic too (and is what its safeguards judge), so the owner's other files must stay out
# of reach. Staged text is screened with the gate's block rules + a secrets scan before anything starts; afterwards the
# changes are merged back into /workspace (3-way against the staged originals; conflicts become <file>.claude-<id> sidecars).
CRUN_DIR = ROOT / "claude-runs"
CRUN_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", "dist", "build", ".next"}
CRUN_SECRET_NAMES = re.compile(r"(^|/)(\.env(\..*)?|.*\.pem|.*\.key|id_(rsa|ed25519|ecdsa)\w*|keys\.env|credentials\.json|\.netrc|\.npmrc|\.pypirc|auth\.json|.*\.token)$", re.I)
CRUN_SECRET_TEXT = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bsk-(ant-)?[A-Za-z0-9_\-]{20,}|\bghp_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}|\bAKIA[0-9A-Z]{16}\b"
    r"|\bxox[abprs]-[A-Za-z0-9\-]{10,}|\bAIza[0-9A-Za-z\-_]{35}\b|\bhf_[A-Za-z0-9]{30,}\b"
    r"|\b(api[_-]?key|secret|token|passw(or)?d)\b\s*[:=]\s*['\"][^'\"\s]{12,}['\"]", re.I)


def _sha(p: Path) -> str | None:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() and not p.is_symlink() else None
    except OSError:
        return None


def _crun_walk(base: Path) -> list[Path]:
    """Regular files under base (base itself if it is a file), skipping caches/vendored dirs and symlinks."""
    if base.is_symlink():
        return []
    if base.is_file():
        return [base]
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in CRUN_SKIP_DIRS and not os.path.islink(os.path.join(dirpath, d))]
        out += [Path(dirpath) / f for f in filenames if not os.path.islink(os.path.join(dirpath, f))]
    return out


def _crun_collect(files: list[str], cfg: dict) -> tuple[list[str], list[str], list[str]]:
    """Expand the brief's files to workspace-relative paths and screen their contents. Returns (paths, blocked_by, judge_snippets)."""
    rules = json.loads((ROOT / "claude" / "rules.json").read_text())
    block = [(cat, re.compile(p, re.I)) for cat, pats in rules.get("block", {}).items() for p in pats]
    judge_rx = re.compile("|".join(rules.get("judge", [])) or r"(?!)", re.I)
    max_files, max_bytes = int(cfg.get("stage_max_files", 400)), int(cfg.get("stage_max_bytes", 8_000_000))
    paths, blocked, snippets, total = [], [], [], 0
    ws = WORKSPACE.resolve()
    for f in files:
        src = (WORKSPACE / f).resolve()
        if src != ws and ws not in src.parents:
            blocked.append(f"file outside the workspace: {f}")
            continue
        for p in _crun_walk(src) if src.exists() else []:
            rel = str(p.relative_to(ws))
            if CRUN_SECRET_NAMES.search(rel):
                blocked.append(f"{rel}: looks like a secrets/credentials file; never send these (leave it out and describe what Claude needs instead)")
                continue
            try:
                raw = p.read_bytes()
            except OSError:
                continue
            total += len(raw)
            paths.append(rel)
            if b"\0" in raw[:4096]:
                continue  # binary: copied, not screened as text
            text = raw.decode("utf-8", "replace")
            if m := CRUN_SECRET_TEXT.search(text):
                blocked.append(f"{rel} line {text.count(chr(10), 0, m.start()) + 1}: contains what looks like a secret/key; remove it from the file first")
            for cat, rx in block:
                if m := rx.search(text):
                    blocked.append(f"{rel} line {text.count(chr(10), 0, m.start()) + 1}: matches the '{cat}' block rule ({m.group(0)[:40]!r})")
                    break
            for m in list(judge_rx.finditer(text))[:3]:
                a, b = text.rfind("\n", 0, max(0, m.start() - 150)) + 1, text.find("\n", m.end() + 150)
                snippets.append(f"[{rel}]\n{text[a:b if b > 0 else len(text)][:400]}")
    if len(paths) > max_files:
        blocked.append(f"{len(paths)} files after expanding directories (limit {max_files}); name narrower paths")
    if total > max_bytes:
        blocked.append(f"{total // 1000} KB of files (limit {max_bytes // 1000} KB); name narrower paths")
    return paths, blocked, snippets


def _crun_stage(cid: str, paths: list[str], files: list[str]) -> dict:
    """Copy the screened files into claude-runs/<cid>; returns the manifest {rel: sha256 of the workspace original or None}."""
    stage = CRUN_DIR / cid
    stage.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, str | None] = {}
    for rel in paths:
        dst = stage / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes((WORKSPACE / rel).read_bytes())
        manifest[rel] = _sha(dst)
    for f in files:  # named but missing = a file/dir Claude should create: make its parent exist
        if not (WORKSPACE / f).exists():
            (stage / f).parent.mkdir(parents=True, exist_ok=True)
    return manifest


def _crun_merge(cid: str, manifest: dict) -> dict:
    """Bring the staged run's changes back into /workspace. Only touches files whose workspace copy is unchanged since staging."""
    stage = CRUN_DIR / cid
    res: dict[str, list[str]] = {"updated": [], "created": [], "deleted": [], "conflicts": [], "skipped": []}
    if not stage.is_dir():
        return res
    seen = set()
    for p in _crun_walk(stage):
        rel = str(p.relative_to(stage))
        seen.add(rel)
        new, dst = _sha(p), WORKSPACE / rel
        if p.stat().st_size > 50_000_000:
            res["skipped"].append(f"{rel} (over 50 MB)")
            continue
        if rel in manifest and new == manifest[rel]:
            continue
        cur = _sha(dst)
        if cur == new:
            continue
        untouched = (cur == manifest[rel]) if rel in manifest else not dst.exists()
        if untouched:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(p.read_bytes())
            res["created" if rel not in manifest or manifest[rel] is None else "updated"].append(rel)
        else:  # the workspace copy changed meanwhile (or a new file collides): keep both
            side = dst.with_name(dst.name + f".claude-{cid}")
            side.parent.mkdir(parents=True, exist_ok=True)
            side.write_bytes(p.read_bytes())
            res["conflicts"].append(f"{rel} → {side.relative_to(WORKSPACE)}")
    for rel, orig in manifest.items():
        if rel not in seen and orig is not None:
            dst = WORKSPACE / rel
            if _sha(dst) == orig:
                dst.unlink()
                res["deleted"].append(rel)
            elif dst.exists():
                res["conflicts"].append(f"{rel} (deleted by Claude, but changed in the workspace meanwhile: kept)")
    return res


def _claude_settings(mode: str, cfg: dict, workdir: Path | None = None) -> dict:
    """The --settings JSON for one run: tool deny rules outside the run's staging dir, and the bubblewrap sandbox for Bash."""
    home = str(Path.home())
    workdir = workdir or WORKSPACE
    # Bash sandbox: deny the whole data disk and the home dir's secrets, re-open only the staging dir (narrower allow wins)
    deny_read = [f"{home}/.ssh", f"{home}/.aws", f"{home}/.config", f"{home}/.claude", f"{home}/.hermes", f"{home}/.gnupg", f"{home}/.docker",
                 f"{home}/.netrc", "/mnt/data"]
    allow_read = [str(workdir)]
    # Claude's own Read/Edit/Write tools follow permission rules, which cannot express exceptions: deny every sibling on the
    # path down to the staging dir explicitly (other projects on the data disk, every other dir of this stack incl. the
    # workspace, other runs' staging dirs) instead of /mnt/data itself.
    def siblings(parent: Path, keep: str) -> list[str]:
        try:
            return [str(parent / d) for d in os.listdir(parent) if d != keep]
        except OSError:
            return []
    if workdir.parent == CRUN_DIR:
        stack = siblings(ROOT, CRUN_DIR.name) + siblings(CRUN_DIR, workdir.name)
    else:
        stack = [str(ROOT / d) for d in ("hud", "hermes-home", "claude", "logs", "profiles", "scripts", "crew", "agents", "rag", "services", "evals", "patches", "hf-home", "models", "documents")]
    no_read = [f"{home}/.ssh", f"{home}/.aws", f"{home}/.config", f"{home}/.claude", f"{home}/.hermes", f"{home}/.gnupg", f"{home}/.docker",
               f"{home}/.netrc", f"{home}/.bash_history", *siblings(Path("/mnt/data"), ROOT.name), *stack]
    perm_deny = [f"Read(/{p}/**)" for p in no_read] + [f"Read(/{p})" for p in no_read] + [
        f"Edit(/{home}/**)", f"Write(/{home}/**)", "Edit(//mnt/data/local-agent/hud/**)", "Write(//mnt/data/local-agent/hud/**)",
        "Edit(//mnt/data/local-agent/hermes-home/**)", "Write(//mnt/data/local-agent/hermes-home/**)", "Edit(//mnt/data/local-agent/claude/**)",
        "Write(//mnt/data/local-agent/claude/**)", "Bash(sudo:*)", "Bash(su:*)", "Bash(docker:*)", "Bash(systemctl:*)", "Bash(ssh:*)", "Bash(scp:*)",
        "Bash(curl:*)", "Bash(wget:*)", "Bash(git push:*)", "Bash(git commit:*)", "Bash(crontab:*)", "Bash(nohup:*)"]
    # Claude Code's full toolset: with --permission-prompts none anything not explicitly allowed is silently denied, so the
    # research/navigation/planning tools are pre-approved for every run (edit tools only in edit mode, scoped to the run dir).
    full_tools = ["Read", "Glob", "Grep", "LS", "WebSearch", "WebFetch", "TodoWrite", "Agent", "Task", "Skill", "ToolSearch",
                  "TaskOutput", "TaskStop", "BashOutput", "KillShell"]
    perm_allow = list(full_tools) + ([f"Edit(/{workdir}/**)", f"Write(/{workdir}/**)", f"NotebookEdit(/{workdir}/**)", "Monitor"] if mode == "edit" else [])
    s: dict = {"permissions": {"deny": perm_deny, "allow": perm_allow}, "includeCoAuthoredBy": False}
    if mode == "edit" and cfg.get("sandbox", "required") != "off":
        s["sandbox"] = {"enabled": True, "autoAllowBashIfSandboxed": True, "allowUnsandboxedCommands": False,
                        # Ubuntu 26.04's AppArmor denies capabilities to /usr/bin/bwrap's children, which breaks Claude Code's
                        # nested seccomp namespace ("write /proc/self/setgroups: Permission denied" -> every Bash call fails).
                        # A root-owned copy with its own profile (/etc/apparmor.d/la-claude-bwrap) is used for these runs only.
                        **({"bwrapPath": LA_BWRAP} if Path(LA_BWRAP).exists() else {}),
                        "failIfUnavailable": cfg.get("sandbox") == "required",
                        "filesystem": {"denyRead": deny_read, "allowRead": allow_read},
                        "network": {"allowedDomains": list(cfg.get("network_domains") or [])}}
        if cfg.get("sandbox") == "auto":
            s["permissions"]["allow"] += [f"Bash({p}:*)" for p in cfg.get("bash_allow") or []]
    return s


def _claude_cmd(t: dict, cfg: dict) -> list[str]:
    mode = t["mode"]
    cmd = [CLAUDE_BIN, "-p", "--model", cgate.MODELS[t["model"]], "--effort", t["effort"], "--output-format", "stream-json", "--verbose",
           "--permission-prompts", "none", "--strict-mcp-config", "--max-turns", str(t["max_turns"]), "-n", f"la-claude-{t['id']}",
           "--settings", json.dumps(_claude_settings(mode, cfg, CRUN_DIR / t["id"]))]
    # only what cannot work in a headless one-shot (nobody to answer, no scheduler, nothing to push to) is removed
    disallowed = ["CronCreate", "CronDelete", "RemoteTrigger", "ScheduleWakeup", "PushNotification", "SendMessage", "Workflow", "EnterWorktree",
                  "ExitWorktree", "DesignSync", "AskUserQuestion", "EnterPlanMode", "ExitPlanMode", "SendUserFile", "Artifact"]
    if mode == "readonly":
        cmd += ["--permission-mode", "default"]
        disallowed += ["Edit", "Write", "NotebookEdit", "Bash", "Monitor"]
    else:
        cmd += ["--permission-mode", "acceptEdits"]
    cmd += ["--disallowedTools", *disallowed]
    return cmd


async def crun_gate(caller: dict, body: Any, dry: bool) -> tuple[Any, dict]:
    """Limits + gate (the judge call blocks for ~15 s, so it runs in a thread). Returns (verdict, params)."""
    cfg = cgate.load_config()
    task = str(getattr(body, "task", "") or "")
    context = str(getattr(body, "context", "") or "")
    files = [str(f) for f in (getattr(body, "files", None) or []) if isinstance(f, str)]
    model = str(getattr(body, "model", "") or "")
    effort = str(getattr(body, "effort", "") or "")
    mode = str(getattr(body, "mode", "") or "edit")
    label = str(getattr(body, "label", "") or "").strip()[:60]
    timeout_s = int(getattr(body, "timeout_s", 0) or 0) or int(cfg["timeout_s"])
    timeout_s = max(60, min(timeout_s, int(cfg["timeout_cap"])))
    max_turns = int(getattr(body, "max_turns", 0) or 0) or int(cfg["max_turns"])
    max_turns = max(2, min(max_turns, int(cfg["max_turns_cap"])))
    v = await asyncio.to_thread(cgate.gate, task, context, files, model, effort, mode, cfg, not dry)
    stage_paths: list[str] = []
    if v.ok:
        # the files go to Claude too: screen their contents with the same rules, and let the judge see dual-use excerpts
        stage_paths, fblocked, snippets = await asyncio.to_thread(_crun_collect, v.files, cfg)
        if fblocked:
            v.ok = False
            v.blocked_by += fblocked[:12]
        elif snippets and not dry and cfg.get("judge", "required") != "off":
            ex = "\n\n".join(snippets)[:12000]
            v2 = await asyncio.to_thread(cgate.gate, "Screening only (not the brief): files attached to this coding task: " + task[:1500],
                                         "Excerpts of the attached files around dual-use terms:\n" + ex, [], model, effort, mode, cfg, True)
            if not v2.ok:
                v.ok = False
                v.blocked_by += [f"attached file contents: {r}" for r in v2.blocked_by]
            v.warnings.append(f"judge reviewed {len(snippets)} file excerpt(s): {(v2.judge or {}).get('verdict', '?')}")
    if v.ok and not dry:
        running = q("SELECT COUNT(*) FROM cruns WHERE state='running'")[0][0]
        if running >= int(cfg["max_concurrent"]):
            v.ok = False
            v.blocked_by.append(f"{running} Claude run(s) already in progress (limit {cfg['max_concurrent']}): wait for one with claude_wait")
        recent = q("SELECT COUNT(*) FROM cruns WHERE created > ?", (time.time() - 3600,))[0][0]
        if recent >= int(cfg["max_per_hour"]):
            v.ok = False
            v.blocked_by.append(f"{recent} Claude runs started in the last hour (limit {cfg['max_per_hour']}); batch the work into fewer, fuller briefs")
    x("INSERT INTO cgate (ts, root_id, job_id, action, ok, model, effort, mode, reasons, redactions, judge, task_head) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
      (time.time(), caller["root_id"] or caller["id"], caller["id"], "check" if dry else "run", int(v.ok), v.model, v.effort, v.mode,
       json.dumps(v.blocked_by), json.dumps(v.redactions + v.warnings), json.dumps(v.judge), task[:200]))
    return v, {"label": label or (task.strip().splitlines()[0][:50] if task.strip() else "claude"), "timeout_s": timeout_s, "max_turns": max_turns, "cfg": cfg,
               "stage_paths": stage_paths}


async def crun_start(root_id: int, job_id: int, v: Any, params: dict) -> dict:
    cid = secrets.token_hex(4)
    cfg = params["cfg"]
    ck = await ckpt_create(root_id, job_id, f"before claude {cid} ({params['label']})")
    try:
        manifest = await asyncio.to_thread(_crun_stage, cid, params.get("stage_paths") or [], v.files)
    except OSError as e:
        return {"error": f"could not stage the files: {e}"}
    t = {"id": cid, "model": v.model, "effort": v.effort, "mode": v.mode, "max_turns": params["max_turns"]}
    prompt_path, out_path, err_path = DATA / f"claude-{cid}.prompt.txt", DATA / f"claude-{cid}.out.jsonl", DATA / f"claude-{cid}.err.log"
    prompt_path.write_text(v.brief)
    env = {k: val for k, val in os.environ.items() if not k.startswith(("LA_", "HERMES"))}
    env["HOME"] = str(Path.home())
    # socat (needed by Claude Code's sandbox) is unpacked user-space under bin/socat-root (apt-get download + dpkg -x)
    # the dedicated bwrap (AppArmor profile la-claude-bwrap) must win the PATH lookup: Claude Code ignores
    # sandbox.bwrapPath from --settings (managed settings only) and resolves "bwrap" via PATH.
    env["PATH"] = f"{Path(LA_BWRAP).parent}:{Path(CLAUDE_BIN).parent}:{ROOT / 'bin/socat-root/usr/bin'}:{env.get('PATH', '/usr/local/bin:/usr/bin:/bin')}"
    env.pop("CLAUDE_CODE_SUBPROCESS_ENV_SCRUB", None)      # it forces permission mode back to default
    try:
        with open(prompt_path, "rb") as pin, open(out_path, "wb") as out, open(err_path, "wb") as err:
            proc = subprocess.Popen(_claude_cmd(t, cfg), cwd=CRUN_DIR / cid, env=env, stdin=pin, stdout=out, stderr=err, start_new_session=True)
    except OSError as e:
        shutil.rmtree(CRUN_DIR / cid, ignore_errors=True)
        return {"error": f"could not start claude: {e}"}
    x("INSERT INTO cruns (id, root_id, job_id, created, state, model, effort, mode, label, brief, verdict, pid, ckpt_before, files, timeout_s, max_turns, log_tail,"
      " manifest) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
      (cid, root_id, job_id, time.time(), "running", v.model, v.effort, v.mode, params["label"], v.brief, json.dumps(v.to_dict() | {"brief": None}),
       proc.pid, ck.get("checkpoint"), "[]", params["timeout_s"], params["max_turns"], "", json.dumps(manifest)))
    x("UPDATE cgate SET run_id=? WHERE id=(SELECT MAX(id) FROM cgate WHERE job_id=?)", (cid, job_id))
    x("INSERT INTO notes (root_id, job_id, ts, message) VALUES (?,?,?,?)", (root_id, job_id, time.time(), f"Claude Code run {cid} started ({v.model}/{v.effort}, {v.mode}): {params['label']}"))
    _crun_tasks[cid] = asyncio.create_task(follow_crun(cid, proc, proc.pid))
    await mission_changed(root_id, "claude")
    return {"task_id": cid, "state": "running", "model": v.model, "effort": v.effort, "mode": v.mode, "timeout_s": params["timeout_s"],
            "max_turns": params["max_turns"], "checkpoint_before": ck.get("checkpoint"), "redactions": v.redactions, "warnings": v.warnings,
            "judge": v.judge, "brief_sent": v.brief}


async def follow_crun(cid: str, proc: Any, pid: int) -> None:
    """Tail data/claude-<id>.out.jsonl until the process exits (or the timeout / a stop), then checkpoint and record the result."""
    t = crun_row(cid)
    out_path, err_path, prompt_path = DATA / f"claude-{cid}.out.jsonl", DATA / f"claude-{cid}.err.log", DATA / f"claude-{cid}.prompt.txt"
    offset, partial = int(t.get("out_offset") or 0), b""
    n_tools, tail, last_push = int(t.get("n_tools") or 0), t.get("log_tail") or "", 0.0
    final, usage, cost, served, session, num_turns, denials, is_err = "", None, None, t.get("served_model"), t.get("session_id"), None, [], False
    deadline = t["created"] + int(t["timeout_s"] or 1800)
    timed_out = False

    def alive() -> bool:
        return proc.poll() is None if proc is not None else _pid_is_claude(pid)

    try:
        while True:
            running = alive()
            if running and time.time() > deadline and not timed_out:
                timed_out = True
                try:
                    os.killpg(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                with open(out_path, "rb") as f:
                    f.seek(offset)
                    chunk = f.read()
            except FileNotFoundError:
                chunk = b""
            if chunk:
                data = partial + chunk
                lines = data.split(b"\n")
                partial = lines.pop()
                offset += len(chunk) - len(partial)
                for raw in lines:
                    if not raw.strip():
                        continue
                    try:
                        ev = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    typ = ev.get("type")
                    if typ == "system" and ev.get("subtype") == "init":
                        served, session = ev.get("model"), ev.get("session_id")
                    elif typ == "assistant":
                        for c in (ev.get("message") or {}).get("content") or []:
                            if c.get("type") == "tool_use":
                                n_tools += 1
                                name = c.get("name")
                                inp = c.get("input") or {}
                                arg = inp.get("command") or inp.get("file_path") or inp.get("pattern") or inp.get("description") or ""
                                tail += f"\n[tool] {name} {str(arg)[:160]}"
                                x("UPDATE cruns SET last_tool=?, last_tool_ts=?, n_tools=? WHERE id=?", (name, time.time(), n_tools, cid))
                            elif c.get("type") == "text" and c.get("text"):
                                tail += "\n" + c["text"]
                    elif typ == "result":
                        final = ev.get("result") or ""
                        usage, cost, num_turns = ev.get("usage"), ev.get("total_cost_usd"), ev.get("num_turns")
                        session = ev.get("session_id") or session
                        is_err = bool(ev.get("is_error"))
                        denials = ev.get("permission_denials") or []
                        if is_err and not final:
                            errs = ev.get("errors") or []
                            final = "; ".join(str(e) for e in errs)[:2000] if errs else str(ev.get("error") or ev.get("subtype") or "")
                tail = tail[-20000:]
                x("UPDATE cruns SET out_offset=?, log_tail=?, served_model=?, session_id=? WHERE id=?", (offset, tail, served, session, cid))
                if time.time() - last_push > 1.5:
                    last_push = time.time()
                    await mission_changed(t["root_id"], "claude")
            if not running and not chunk:
                break
            await asyncio.sleep(0.3 if chunk else 0.7)
        rc = proc.wait() if proc is not None else None
        try:
            stderr = err_path.read_text(errors="replace")[-3000:]
        except OSError:
            stderr = ""
        merge = None
        if t.get("manifest") is not None:  # staged run: bring its changes back into /workspace, then drop the staging dir
            merge = await asyncio.to_thread(_crun_merge, cid, json.loads(t["manifest"]))
            shutil.rmtree(CRUN_DIR / cid, ignore_errors=True)
        ck = await ckpt_create(t["root_id"], t["job_id"], f"after claude {cid} ({t['label']})")
        files: list[dict] = []
        if t.get("ckpt_before") and ck.get("checkpoint"):
            _, ns = await _run(*_git("diff", "--name-status", t["ckpt_before"], ck["checkpoint"]), timeout=60)
            for line in ns.splitlines():
                parts = line.split("\t")
                if len(parts) >= 2:
                    files.append({"status": parts[0][:1], "path": parts[-1]})
        error = None
        refused = is_err and bool(re.search(r"safeguards flagged|legal/aup|Details: `\[", final or ""))
        if refused:
            # the model's own safeguards refused the session: record it and trip the breaker if it keeps happening
            n = q("SELECT COUNT(*) FROM cruns WHERE state='refused' AND created > ?", (time.time() - 86400,))[0][0] + 1
            cfg = cgate.load_config()
            if n >= int(cfg.get("max_refusals_per_day", 3)) and not cfg.get("paused"):
                cfg["paused"] = f"{n} safeguard refusals in 24 h (last: run {cid}); review the briefs under Claude Code runs and clear this in Settings"
                cgate.save_config(cfg)
                x("INSERT INTO notes (root_id, job_id, ts, message) VALUES (?,?,?,?)", (t["root_id"], t["job_id"], time.time(), "Claude delegation PAUSED: " + cfg["paused"]))
        if cid in _crun_cancel:
            state = "killed"
        elif refused:
            state, error = "refused", "Claude's safeguards refused this brief: " + re.sub(r"\s+", " ", final)[:300]
        elif timed_out:
            state, error = "timeout", f"stopped after {t['timeout_s']} s"
        elif is_err or (rc not in (0, None) and not final):
            state, error = "failed", (final or stderr.strip()[-800:] or f"exit {rc}")
        elif denials and not final:
            state, error = "failed", "every action was denied by the permission rules"
        else:
            state = "done"
        if denials:
            tail += "\n[denied] " + "; ".join(f"{d.get('tool_name')}({str(d.get('tool_input'))[:80]})" for d in denials[:10])
        if merge and merge["conflicts"]:
            tail += "\n[merge conflicts] " + "; ".join(merge["conflicts"][:10])
        x("UPDATE cruns SET state=?, finished=?, exit_code=?, final=?, usage=?, cost_usd=?, served_model=?, session_id=?, ckpt_after=?, files=?,"
          " n_tools=?, num_turns=?, error=?, log_tail=?, denials=?, merge=? WHERE id=?",
          (state, time.time(), rc, final[:60000], json.dumps(usage), cost, served, session, ck.get("checkpoint"), json.dumps(files), n_tools,
           num_turns, error, tail[-20000:], json.dumps(denials)[:8000], json.dumps(merge) if merge is not None else None, cid))
        x("INSERT INTO notes (root_id, job_id, ts, message) VALUES (?,?,?,?)",
          (t["root_id"], t["job_id"], time.time(), f"Claude Code run {cid} {state}: {len(files)} file(s) changed, {n_tools} tools, {num_turns or 0} turns"
           + (f", ${cost:.2f}" if cost else "") + (f", {len(merge['conflicts'])} merge conflict(s) kept as .claude-{cid} sidecars" if merge and merge["conflicts"] else "")
           + (f" — {error[:120]}" if error else "")))
        for fp in (out_path, err_path, prompt_path):
            try:
                fp.unlink()
            except OSError:
                pass
    except Exception as e:
        x("UPDATE cruns SET state='failed', finished=?, error=? WHERE id=?", (time.time(), f"follower crashed: {type(e).__name__}: {e}", cid))
    finally:
        _crun_tasks.pop(cid, None)
        _crun_cancel.discard(cid)
        async with _crun_done:
            _crun_done.notify_all()
        await mission_changed(t["root_id"], "claude")


async def crun_stop(t: dict) -> dict:
    if t["state"] != "running":
        return {"task_id": t["id"], "state": t["state"], "note": "already finished"}
    _crun_cancel.add(t["id"])
    try:
        os.killpg(int(t["pid"]), signal.SIGTERM)
    except (ProcessLookupError, TypeError, ValueError):
        pass
    for _ in range(40):
        await asyncio.sleep(0.5)
        t2 = crun_row(t["id"])
        if t2["state"] != "running":
            return {"task_id": t["id"], "state": t2["state"]}
    try:
        os.killpg(int(t["pid"]), signal.SIGKILL)
    except (ProcessLookupError, TypeError, ValueError):
        pass
    return {"task_id": t["id"], "state": "killing"}


def crun_report(t: dict) -> dict:
    d = crun_view(t, tail_lines=0, full=True)
    d.pop("brief", None); d.pop("usage", None); d.pop("verdict", None)
    d["report"] = (t.get("final") or "")[:40000]
    d["changed_files"] = [f"{f['status']} {f['path']}" for f in json.loads(t.get("files") or "[]")]
    d["diff_hint"] = f"diff('{t['ckpt_before']}', '{t['ckpt_after']}')" if t.get("ckpt_before") and t.get("ckpt_after") else None
    d["merge"] = json.loads(t["merge"]) if t.get("merge") else None
    return d


# reattach Claude runs that survived a HUD restart; the rest are lost
_CRUN_REATTACH = [r["id"] for r in db.execute("SELECT id, pid FROM cruns WHERE state='running'").fetchall() if _pid_is_claude(r["pid"])]
db.execute(f"UPDATE cruns SET state='lost', error='HUD restarted while running', finished=? WHERE state='running'"
           f"{' AND id NOT IN (' + ','.join('?' * len(_CRUN_REATTACH)) + ')' if _CRUN_REATTACH else ''}", (time.time(), *_CRUN_REATTACH))


@app.post("/internal/claude/check")
async def internal_claude_check(body: Internal, req: Request):
    internal(req)
    caller = _caller(body)
    v, params = await crun_gate(caller, body, dry=True)
    return {"ok": v.ok, "would_send": v.brief if v.ok else None, "blocked_by": v.blocked_by, "redactions": v.redactions, "warnings": v.warnings,
            "judge": v.judge, "model": v.model, "effort": v.effort, "mode": v.mode, "files": v.files,
            "note": "judge not consulted in check(); it runs when you call claude_run" if v.ok else "fix the reasons above and check again"}


@app.post("/internal/claude/run")
async def internal_claude_run(body: Internal, req: Request):
    internal(req)
    caller = _caller(body)
    v, params = await crun_gate(caller, body, dry=False)
    if not v.ok:
        return {"error": "blocked by the outbound gate — nothing was sent", "blocked_by": v.blocked_by, "redactions": v.redactions,
                "warnings": v.warnings, "judge": v.judge}
    return await crun_start(caller["root_id"] or caller["id"], caller["id"], v, params)


def _cruns_for(caller: dict, tid: str = "") -> list[dict]:
    root_id = caller["root_id"] or caller["id"]
    return [dict(r) for r in q("SELECT * FROM cruns WHERE root_id=? AND (?='' OR id=?) ORDER BY created", (root_id, tid, tid))]


@app.post("/internal/claude/status")
async def internal_claude_status(body: Internal, req: Request):
    internal(req)
    caller = _caller(body)
    tid = str(getattr(body, "task_id", "") or "")
    rows = _cruns_for(caller, tid)
    if tid and not rows:
        return {"error": "no such Claude run in this mission"}
    return {"runs": [crun_view(r) for r in rows]}


@app.post("/internal/claude/wait")
async def internal_claude_wait(body: Internal, req: Request):
    internal(req)
    caller = _caller(body)
    tid = str(getattr(body, "task_id", "") or "")
    timeout = max(5, min(int(getattr(body, "timeout_s", 300) or 300), 300))
    deadline = time.time() + timeout
    while True:
        rows = _cruns_for(caller, tid)
        if tid and not rows:
            return {"error": "no such Claude run in this mission"}
        pending = [r for r in rows if r["state"] == "running"]
        if not pending or time.time() >= deadline:
            return {"timed_out": bool(pending), "waited_s": round(timeout - max(0, deadline - time.time()), 1),
                    "runs": [crun_report(r) if r["state"] != "running" else crun_view(r) for r in rows], "still_running": [r["id"] for r in pending]}
        async with _crun_done:
            try:
                await asyncio.wait_for(_crun_done.wait(), min(5, max(0.1, deadline - time.time())))
            except asyncio.TimeoutError:
                pass


@app.post("/internal/claude/result")
async def internal_claude_result(body: Internal, req: Request):
    internal(req)
    caller = _caller(body)
    rows = _cruns_for(caller, str(getattr(body, "task_id", "") or ""))
    if not rows:
        return {"error": "no such Claude run in this mission"}
    return crun_report(rows[0]) if rows[0]["state"] != "running" else {"task_id": rows[0]["id"], "state": "running", "note": "still running; claude_wait for it"}


@app.post("/internal/claude/stop")
async def internal_claude_stop(body: Internal, req: Request):
    internal(req)
    caller = _caller(body)
    rows = _cruns_for(caller, str(getattr(body, "task_id", "") or ""))
    if not rows:
        return {"error": "no such Claude run in this mission"}
    return await crun_stop(rows[0])


@app.get("/api/claude/runs")
async def claude_runs_api(req: Request, limit: int = 50):
    require(req)
    rows = q("SELECT * FROM cruns ORDER BY created DESC LIMIT ?", (max(1, min(limit, 500)),))
    gate_rows = q("SELECT * FROM cgate ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),))
    return {"runs": [crun_view(dict(r)) for r in rows], "gate": [dict(r) for r in gate_rows]}


@app.get("/api/claude/runs/{cid}")
async def claude_run_api(cid: str, req: Request):
    require(req)
    return crun_view(crun_row(cid), tail_lines=200, full=True)


@app.post("/api/claude/runs/{cid}/stop")
async def claude_stop_api(cid: str, req: Request):
    require(req, write=True)
    audit(client_ip(req), "claude_stop", cid)
    return await crun_stop(crun_row(cid))


CLAUDE_OWNER_KEYS = {"enabled": bool, "default_model": str, "default_effort": str, "judge": str, "max_concurrent": int, "max_per_hour": int,
                     "max_turns": int, "timeout_s": int, "sandbox": str, "max_refusals_per_day": int, "paused": str}


@app.get("/api/claude/settings")
async def claude_settings_get(req: Request):
    require(req)
    cfg = cgate.load_config()
    return {"settings": {k: cfg.get(k) for k in CLAUDE_OWNER_KEYS}, "models": list(cgate.MODELS), "efforts": cgate.EFFORTS, "modes": cgate.MODES,
            "judge_modes": ["required", "optional", "off"], "sandbox_modes": ["required", "auto", "off"], "claude_bin": CLAUDE_BIN,
            "claude_present": Path(CLAUDE_BIN).exists()}


@app.post("/api/claude/settings")
async def claude_settings_set(req: Request):
    require(req, write=True)
    body = await req.json()
    cfg = cgate.load_config()
    for k, typ in CLAUDE_OWNER_KEYS.items():
        if k not in body:
            continue
        val = body[k]
        if typ is bool:
            val = bool(val)
        elif typ is int:
            try:
                val = max(0, int(val))
            except (TypeError, ValueError):
                raise HTTPException(400, f"{k} must be an integer")
        else:
            val = str(val)
            ok = {"default_model": cgate.MODELS, "default_effort": cgate.EFFORTS, "judge": ("required", "optional", "off"), "sandbox": ("required", "auto", "off")}.get(k)
            if ok is not None and val not in ok:
                raise HTTPException(400, f"{k} must be one of {list(ok)}")
        cfg[k] = val
    cfg.pop("_config_error", None)
    cgate.save_config(cfg)
    audit(client_ip(req), "claude_settings", json.dumps({k: body[k] for k in body if k in CLAUDE_OWNER_KEYS}))
    return {"settings": {k: cfg.get(k) for k in CLAUDE_OWNER_KEYS}}


# ---- files the agent hands over (download only, confined to /workspace)

def ws_file(path: str) -> Path:
    raw = (path or "").strip()
    for pre in ("sandbox:", "file://"):
        if raw.startswith(pre):
            raw = raw[len(pre):]
    if raw.startswith("/workspace/"):
        raw = raw[len("/workspace/"):]
    elif raw.startswith("/"):
        raise HTTPException(404, "only files under /workspace can be downloaded")
    root = WORKSPACE.resolve()
    p = (root / raw).resolve()
    if root not in p.parents or not p.is_file():
        raise HTTPException(404, "no such file")
    if p.relative_to(root).parts[0] in FILE_EXCLUDE_TOP:
        raise HTTPException(404, "no such file")
    return p


@app.get("/api/files")
async def download_file(path: str, req: Request):
    require(req)
    p = ws_file(path)
    audit(client_ip(req), "download", str(p.relative_to(WORKSPACE.resolve()))[:300])
    # always an attachment with a neutral type: never render agent-written HTML/SVG on this origin
    return FileResponse(p, media_type="application/octet-stream", filename=p.name,
                        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


class StatBody(BaseModel):
    paths: list[str] = Field(default_factory=list, max_length=60)


@app.post("/api/files/stat")
async def stat_files(body: StatBody, req: Request):
    require(req, write=True)
    out = []
    for raw in body.paths:
        try:
            p = ws_file(raw)
        except HTTPException:
            continue
        st = p.stat()
        out.append({"path": "/workspace/" + str(p.relative_to(WORKSPACE.resolve())), "name": p.name, "size": st.st_size, "mtime": st.st_mtime})
    return {"files": out}


# ---- usage history

@app.get("/api/usage")
async def usage(req: Request, days: int = 30):
    require(req)
    days = max(1, min(days, 365))
    now = time.time()
    since = now - days * 86400
    rows = q("SELECT id, status, created, started, finished, tokens, mode, role, relation, depth, effort, n_tools, parent_id"
             " FROM jobs WHERE kind='agent' AND created >= ? ORDER BY created", (since,))
    day_keys = [time.strftime("%Y-%m-%d", time.localtime(now - (days - 1 - i) * 86400)) for i in range(days)]
    per_day = {d: {"jobs": 0, "input": 0, "cached": 0, "output": 0, "agent_s": 0.0, "failed": 0} for d in day_keys}
    by_mode, by_role, by_effort, status = defaultdict(int), defaultdict(int), defaultdict(int), defaultdict(int)
    heat = [[0] * 24 for _ in range(7)]
    tot = {"jobs": 0, "input": 0, "cached": 0, "output": 0, "agent_s": 0.0, "tools": 0, "missions": 0, "members": 0}
    longest = None
    for r in rows:
        tok = json.loads(r["tokens"] or "null") or {}
        d = time.strftime("%Y-%m-%d", time.localtime(r["created"]))
        dur = (r["finished"] - r["started"]) if r["finished"] and r["started"] else 0
        day = per_day.get(d)
        if day is not None:
            day["jobs"] += 1; day["input"] += tok.get("input") or 0; day["cached"] += tok.get("cache_read") or 0
            day["output"] += tok.get("output") or 0; day["agent_s"] += dur; day["failed"] += r["status"] == "failed"
        tot["jobs"] += 1; tot["input"] += tok.get("input") or 0; tot["cached"] += tok.get("cache_read") or 0
        tot["output"] += tok.get("output") or 0; tot["agent_s"] += dur; tot["tools"] += r["n_tools"] or 0
        tot["missions"] += (r["relation"] is None and r["mode"] not in (None, "chat")); tot["members"] += r["relation"] == "child"
        by_mode[r["mode"] or "chat"] += 1; status[r["status"]] += 1
        if r["relation"] == "child" and r["role"]:
            by_role[r["role"]] += 1
        by_effort[r["effort"] or "medium"] += 1
        lt = time.localtime(r["created"]); heat[lt.tm_wday][lt.tm_hour] += 1
        if dur and (not longest or dur > longest["s"]):
            longest = {"id": r["id"], "s": round(dur)}
    tools = q("SELECT json_extract(e.data,'$.name') AS n, COUNT(*) AS c FROM events e JOIN jobs j ON j.id=e.job_id"
              " WHERE e.type='tool_use' AND j.created >= ? GROUP BY n ORDER BY c DESC LIMIT 14", (since,))
    m = (_status or {}).get("model", {})
    return {"days": days, "labels": day_keys, "per_day": [per_day[d] for d in day_keys], "totals": tot,
            "by_mode": by_mode, "by_role": by_role, "by_effort": by_effort, "status": status, "heat": heat,
            "tools": [{"name": t["n"], "count": t["c"]} for t in tools], "longest": longest,
            "model": {"avg_gen_tps": m.get("avg_gen_tps"), "avg_prefill_tps": m.get("avg_prefill_tps"),
                      "tokens_predicted_total": m.get("tokens_predicted_total"), "profile": m.get("profile")}}


# ---- image uploads

MAX_UPLOAD = 25 * 1024 * 1024


@app.post("/api/uploads")
async def upload(req: Request, file: UploadFile = File(...)):
    """Accept one image, re-encode it as a clean JPEG (drops EXIF/GPS and anything that isn't pixels)."""
    require(req, write=True)
    from io import BytesIO
    from PIL import Image, ImageOps
    try:  # iPhone HEIC/HEIF, if the browser didn't convert it client-side
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except ImportError:
        pass
    raw = await file.read(MAX_UPLOAD + 1)
    if len(raw) > MAX_UPLOAD:
        raise HTTPException(413, "image too large (max 25 MB)")
    Image.MAX_IMAGE_PIXELS = 60_000_000
    try:
        with Image.open(BytesIO(raw)) as im:
            if im.format not in {"JPEG", "PNG", "WEBP", "GIF", "BMP", "TIFF", "MPO", "HEIF", "AVIF"}:
                raise HTTPException(415, f"unsupported image type {im.format}")
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((2048, 2048))
            uid = secrets.token_hex(16)
            im.save(UPLOADS / f"{uid}.jpg", "JPEG", quality=90, optimize=True)
            w, h = im.size
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(415, "not a readable image")
    audit(client_ip(req), "upload", f"{uid} {w}x{h}")
    return {"id": uid, "width": w, "height": h}


@app.get("/api/uploads/{uid}")
async def get_upload(uid: str, req: Request):
    require(req)
    if not re.fullmatch(r"[0-9a-f]{32}", uid) or not (UPLOADS / f"{uid}.jpg").exists():
        raise HTTPException(404, "no such image")
    return FileResponse(UPLOADS / f"{uid}.jpg", media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


# ---- file attachments (code, zips, documents): chunked + resumable, archives unpacked safely
# The tunnel caps one request at ~100 MB and phones drop connections, so the browser sends 8 MB chunks
# (POST /api/attach/init -> /api/attach/<id>/chunk?offset=N ... -> /api/attach/<id>/finish). Everything lands in
# /workspace/uploads/files/<id>/ (visible to the sandbox at the same path; excluded from checkpoints).
ATTACH = UPLOADS / "files"
ATTACH.mkdir(exist_ok=True)
ATTACH_MAX = 1024 * 1024 * 1024          # one file
CHUNK_MAX = 9 * 1024 * 1024
UNPACK_MAX_BYTES = 4 * 1024 * 1024 * 1024
UNPACK_MAX_FILES = 50_000
ARCHIVE_EXT = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")
SKIP_PARTS = {"__MACOSX", ".DS_Store"}
_attach_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def _safe_name(name: str) -> str:
    n = Path(str(name).replace("\\", "/")).name
    n = re.sub(r"[\x00-\x1f\x7f]", "", n).strip().lstrip(".")
    n = re.sub(r"[^\w.\- ()+@,=]", "_", n)[:150]
    return n or "file"


def attach_meta(aid: str) -> dict | None:
    if not re.fullmatch(r"[0-9a-f]{32}", aid or ""):
        return None
    f = ATTACH / aid / "meta.json"
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return None


def _attach_save(m: dict) -> None:
    f = ATTACH / m["id"] / "meta.json"
    tmp = f.with_suffix(".tmp"); tmp.write_text(json.dumps(m)); tmp.replace(f)


def attach_public(m: dict) -> dict:
    keep = ("id", "name", "size", "kind", "path", "extracted", "files", "unpacked_bytes", "tree", "langs", "skipped", "error", "state", "received")
    return {k: m[k] for k in keep if k in m}


def _gunzip(src: Path, dst: Path, limit: int) -> None:
    import gzip
    n = 0
    with gzip.open(src, "rb") as fi, open(dst, "wb") as fo:
        while True:
            buf = fi.read(1 << 20)
            if not buf:
                break
            n += len(buf)
            if n > limit:
                raise ValueError("decompressed data is larger than announced")
            fo.write(buf)


def _is_archive(name: str) -> bool:
    return name.lower().endswith(ARCHIVE_EXT)


def _archive_stem(name: str) -> str:
    low = name.lower()
    for ext in sorted(ARCHIVE_EXT, key=len, reverse=True):
        if low.endswith(ext):
            return _safe_name(name[: -len(ext)]) or "archive"
    return "archive"


def _clean_member(name: str) -> str | None:
    """Archive member -> safe relative path, or None (absolute, '..', drive letters, macOS junk)."""
    parts = []
    for part in name.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == ".." or re.fullmatch(r"[A-Za-z]:", part) or part in SKIP_PARTS:
            return None
        parts.append(re.sub(r"[\x00-\x1f]", "_", part)[:200])
    return "/".join(parts) if parts else None


def _unpack(src: Path, dest: Path) -> dict:
    """Extract a zip/tar into dest without ever writing outside it: no links, no device files, no absolute or '..'
    paths, bounded file count and total size (zip bombs stop at the limit instead of filling the disk)."""
    import tarfile, zipfile, shutil
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    n, total, skipped = 0, 0, 0

    def target(rel: str) -> Path:
        t = (root / rel).resolve()
        if root != t and root not in t.parents:
            raise ValueError("path escapes the archive root")
        return t

    def copy_stream(fsrc, t: Path) -> None:
        nonlocal total
        t.parent.mkdir(parents=True, exist_ok=True)
        with open(t, "wb") as out:
            while True:
                buf = fsrc.read(1 << 20)
                if not buf:
                    break
                total += len(buf)
                if total > UNPACK_MAX_BYTES:
                    raise ValueError(f"archive unpacks to more than {_human(UNPACK_MAX_BYTES)}")
                out.write(buf)

    if src.name.lower().endswith(".zip"):
        with zipfile.ZipFile(src) as z:
            for info in z.infolist():
                rel = _clean_member(info.filename)
                mode = (info.external_attr >> 16) & 0o170000
                if rel is None or mode == 0o120000:            # traversal / junk / symlink
                    skipped += 1; continue
                if info.is_dir():
                    target(rel).mkdir(parents=True, exist_ok=True); continue
                n += 1
                if n > UNPACK_MAX_FILES:
                    raise ValueError(f"archive has more than {UNPACK_MAX_FILES} files")
                with z.open(info) as fsrc:
                    copy_stream(fsrc, target(rel))
                if (info.external_attr >> 16) & 0o111:
                    os.chmod(target(rel), 0o755)
    else:
        with tarfile.open(src) as tf:
            for mem in tf:
                rel = _clean_member(mem.name)
                if rel is None or not (mem.isfile() or mem.isdir()):   # links, devices, fifos are never extracted
                    skipped += 1; continue
                if mem.isdir():
                    target(rel).mkdir(parents=True, exist_ok=True); continue
                n += 1
                if n > UNPACK_MAX_FILES:
                    raise ValueError(f"archive has more than {UNPACK_MAX_FILES} files")
                fsrc = tf.extractfile(mem)
                if fsrc is None:
                    skipped += 1; continue
                with fsrc:
                    copy_stream(fsrc, target(rel))
                if mem.mode & 0o111:
                    os.chmod(target(rel), 0o755)
    # a single top-level folder inside the archive becomes the extraction root itself (no app/app/ nesting)
    kids = [k for k in dest.iterdir()]
    if len(kids) == 1 and kids[0].is_dir():
        tmp = dest.with_name(dest.name + ".flat")
        kids[0].rename(tmp); dest.rmdir(); tmp.rename(dest)
    return {"files": n, "unpacked_bytes": total, "skipped": skipped}


def _tree(base: Path, max_lines: int = 80) -> tuple[str, dict]:
    """Compact indented tree (dirs first, depth <= 3, heavy dirs collapsed) + file counts per extension."""
    langs: dict[str, int] = defaultdict(int)
    heavy = {"node_modules", ".git", ".venv", "venv", "__pycache__", "dist", "build", ".next", "target", ".gradle", ".idea"}
    for dp, dns, fns in os.walk(base):
        dns[:] = [d for d in dns if d not in heavy]
        for f in fns:
            langs[(Path(f).suffix.lower() or Path(f).name)[:12]] += 1
    lines: list[str] = []

    def walk(d: Path, depth: int) -> None:
        try:
            ents = sorted(d.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError:
            return
        for e in ents:
            if len(lines) >= max_lines:
                return
            pad = "  " * depth
            if e.is_dir():
                if e.name in heavy:
                    lines.append(f"{pad}{e.name}/ (skipped)"); continue
                cnt = sum(len(fs) for _, _, fs in os.walk(e))
                lines.append(f"{pad}{e.name}/ ({cnt} files)")
                if depth < 2:
                    walk(e, depth + 1)
            else:
                try:
                    sz = e.stat().st_size
                except OSError:
                    sz = 0
                lines.append(f"{pad}{e.name} ({_human(sz)})")
    walk(base, 0)
    if len(lines) >= max_lines:
        lines.append("… (truncated — use search_files / repo_map for the rest)")
    top = dict(sorted(langs.items(), key=lambda kv: -kv[1])[:10])
    return "\n".join(lines), top


def _human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} GB"


def _ws(p: Path) -> str:
    return "/workspace/" + str(p.relative_to(WORKSPACE))


INLINE_ONE, INLINE_TOTAL = 24_000, 60_000


def attach_prompt_block(files: list[dict]) -> str:
    """What the agent sees before the owner's message: where every attachment is, its tree, and (for small text
    files) the content itself, which saves the agent a read_file round trip."""
    out = ["[Attached files — uploaded by the owner with this message; they are already on disk at these paths]"]
    inlined = 0
    for f in files:
        if f.get("kind") == "archive":
            out.append(f"- {f['name']} ({_human(f.get('size', 0))} archive, {f.get('files', 0)} files, "
                       f"{_human(f.get('unpacked_bytes', 0))} unpacked) → extracted to {f['extracted']}/"
                       + (f"  ({f['skipped']} unsafe or junk entries skipped)" if f.get("skipped") else ""))
            if f.get("langs"):
                out.append("  file types: " + ", ".join(f"{k} ×{v}" for k, v in f["langs"].items()))
            if f.get("tree"):
                out.append("  tree:\n" + "\n".join("    " + ln for ln in f["tree"].splitlines()))
            out.append(f"  original archive: {f['path']}")
        elif f.get("kind") == "archive-failed":
            out.append(f"- {f['name']} ({_human(f.get('size', 0))}) → {f['path']} — could NOT be unpacked automatically: "
                       f"{f.get('error', '?')}. Unpack it yourself in the sandbox if it matters.")
        else:
            out.append(f"- {f['name']} ({_human(f.get('size', 0))}) → {f['path']}")
            fp = WORKSPACE / f["path"][len("/workspace/"):]
            if f.get("size", 0) <= INLINE_ONE and inlined + f.get("size", 0) <= INLINE_TOTAL:
                try:
                    raw = fp.read_bytes()
                    if b"\x00" not in raw:
                        text = raw.decode("utf-8")
                        lang = Path(f["name"]).suffix.lstrip(".")[:10]
                        fence = "````" if "```" in text else "```"
                        out.append(f"  content:\n{fence}{lang}\n{text.rstrip()}\n{fence}")
                        inlined += len(raw)
                except (OSError, UnicodeDecodeError):
                    pass
    out.append("Uploads are not covered by checkpoints/rollback: to change an uploaded project, copy it to "
               "/workspace/<project>/ first and work there. Hand results back as files under /workspace/outbox/.")
    return "\n".join(out)


class AttachInit(BaseModel):
    name: str = Field(min_length=1, max_length=400)
    size: int = Field(ge=0, le=ATTACH_MAX)              # bytes that will be sent (compressed size when encoding=gzip)
    encoding: str = Field("", pattern=r"^(|gzip)$")      # the browser gzips plain text/code before the slow tunnel
    orig_size: int = Field(0, ge=0, le=ATTACH_MAX)


def _attach_sweep(max_age_s: float = 48 * 3600) -> None:
    """Drop uploads that were started but never finished (closed tab, failed network) after two days."""
    now = time.time()
    for d in ATTACH.iterdir():
        m = attach_meta(d.name)
        if m and m.get("state") == "uploading" and now - m.get("created", now) > max_age_s:
            shutil.rmtree(d, ignore_errors=True)


@app.post("/api/attach/init")
async def attach_init(body: AttachInit, req: Request):
    require(req, write=True)
    await asyncio.to_thread(_attach_sweep)
    free = shutil.disk_usage(ATTACH).free
    if body.size * 3 + (2 << 30) > free:
        raise HTTPException(507, "not enough disk space for this upload")
    aid = secrets.token_hex(16)
    d = ATTACH / aid
    d.mkdir(mode=0o755)
    m = {"id": aid, "name": _safe_name(body.name), "size": body.size, "received": 0, "state": "uploading", "created": time.time(),
         "encoding": body.encoding, "orig_size": body.orig_size}
    (d / ".part").touch()
    _attach_save(m)
    audit(client_ip(req), "attach_init", f"{aid} {m['name']} {body.size}")
    return attach_public(m)


@app.post("/api/attach/{aid}/chunk")
async def attach_chunk(aid: str, offset: int, req: Request):
    require(req, write=True)
    async with _attach_locks[aid]:
        m = attach_meta(aid)
        if not m or m["state"] != "uploading":
            raise HTTPException(404, "no such upload in progress")
        data = bytearray()
        async for part in req.stream():
            data += part
            if len(data) > CHUNK_MAX:
                raise HTTPException(413, "chunk too large")
        part_f = ATTACH / aid / ".part"
        have = part_f.stat().st_size
        if offset + len(data) <= have:          # a retry of a chunk we already have
            return {"received": have}
        if offset != have:
            raise HTTPException(409, f"expected offset {have}")
        if have + len(data) > m["size"]:
            raise HTTPException(400, "more data than announced")
        with open(part_f, "ab") as f:
            f.write(data)
        m["received"] = have + len(data)
        _attach_save(m)
        return {"received": m["received"]}


@app.post("/api/attach/{aid}/finish")
async def attach_finish(aid: str, req: Request):
    require(req, write=True)
    async with _attach_locks[aid]:
        m = attach_meta(aid)
        if not m:
            raise HTTPException(404, "no such upload")
        if m["state"] == "ready":
            return attach_public(m)
        d = ATTACH / aid
        part_f = d / ".part"
        if part_f.stat().st_size != m["size"]:
            raise HTTPException(409, f"incomplete: {part_f.stat().st_size} of {m['size']} bytes")
        final = d / m["name"]
        if m.get("encoding") == "gzip":
            try:
                await asyncio.to_thread(_gunzip, part_f, final, m.get("orig_size") or ATTACH_MAX)
            except Exception as e:
                raise HTTPException(400, f"could not decompress the upload: {e}")
            part_f.unlink(missing_ok=True)
            m["wire_size"], m["size"] = m["size"], final.stat().st_size
        else:
            part_f.rename(final)
        m["path"] = _ws(final)
        m["kind"] = "file"
        if _is_archive(m["name"]):
            dest = d / _archive_stem(m["name"])
            if dest.exists():
                dest = d / (dest.name + "-unpacked")
            try:
                info = await asyncio.to_thread(_unpack, final, dest)
                tree, langs = await asyncio.to_thread(_tree, dest)
                m.update(kind="archive", extracted=_ws(dest), tree=tree, langs=langs, **info)
            except Exception as e:     # bad/encrypted/oversized archive: keep the file, tell the agent why
                await asyncio.to_thread(shutil.rmtree, dest, True)
                m.update(kind="archive-failed", error=str(e)[:300])
        m["state"] = "ready"
        _attach_save(m)
        audit(client_ip(req), "attach_ready", f"{aid} {m['kind']} {m.get('files', '')}")
        return attach_public(m)


@app.get("/api/attach/{aid}")
async def attach_get(aid: str, req: Request):
    require(req)
    m = attach_meta(aid)
    if not m:
        raise HTTPException(404, "no such upload")
    if m["state"] == "uploading":
        try:
            m["received"] = (ATTACH / aid / ".part").stat().st_size
        except OSError:
            pass
    return attach_public(m)


# ---- model control

TUNABLE = {
    "CTX": (int, 4096, 1048576), "AGENT_CTX": (int, 65536, 1048576), "NGL": (int, 0, 99), "N_CPU_MOE": (int, 0, 64), "THREADS": (int, 1, 96),
    "BATCH": ({512, 1024, 2048, 4096, 8192},), "UBATCH": ({256, 512, 1024, 2048, 4096, 8192},),
    "KV_TYPE": ({"f16", "q8_0", "q4_0"},), "TEMP": (float, 0.0, 2.0), "TOP_P": (float, 0.0, 1.0),
    "TOP_K": (int, 0, 200), "MIN_P": (float, 0.0, 1.0), "PARALLEL": (int, 1, MAX_AGENTS),
}


def validate_tune(vals: dict[str, Any]) -> dict[str, str]:
    out = {}
    for k, v in vals.items():
        if k not in TUNABLE:
            raise HTTPException(400, f"{k} is not tunable from the web")
        spec = TUNABLE[k]
        if len(spec) == 1:
            allowed = spec[0]
            v = int(v) if all(isinstance(a, int) for a in allowed) else str(v)
            if v not in allowed:
                raise HTTPException(400, f"{k} must be one of {sorted(allowed)}")
            out[k] = str(v)
        else:
            typ, lo, hi = spec
            try:
                v = typ(v)
            except (TypeError, ValueError):
                raise HTTPException(400, f"{k} must be a number")
            if not lo <= v <= hi:
                raise HTTPException(400, f"{k} must be between {lo} and {hi}")
            out[k] = str(v)
    return out


def profile_names() -> list[str]:
    return sorted(p.stem for p in PROFILES.glob("*.env") if p.name != "active.env")


def check_profile(name: str) -> str:
    if name not in profile_names():
        raise HTTPException(404, "no such profile")
    return name


@app.get("/api/profiles")
async def profiles(req: Request):
    require(req)
    act = active_profile()
    return {"active": act, "tunable": {k: (sorted(v[0]) if len(v) == 1 else {"min": v[1], "max": v[2]}) for k, v in TUNABLE.items()},
            "profiles": [{"name": n, "active": n == act, "values": read_profile(n),
                          "notes": [l[2:] for l in (PROFILES / f"{n}.env").read_text().splitlines() if l.startswith("# ")]}
                         for n in profile_names()]}


class Tune(BaseModel):
    values: dict[str, Any]
    restart: bool = True


async def restart_model() -> str:
    rc, out = await _run("systemctl", "--user", "restart", "la-model.service", timeout=60)
    return out if rc else "restarting"


async def sync_hermes_ctx(name: str) -> None:
    prof = read_profile(name)
    ctx = agent_ctx(prof)   # ONE agent's share of the llama-server pool (profile AGENT_CTX), not the whole -c
    cfg = HERMES_HOME / "config.yaml"
    s = cfg.read_text()
    s = re.sub(r"^  context_length: .*$", f"  context_length: {ctx}    # per-agent window = profile AGENT_CTX (pool CTX={prof['CTX']} is shared by all slots)", s, count=1, flags=re.M)
    cfg.write_text(s)


@app.put("/api/profiles/{name}")
async def tune(name: str, body: Tune, req: Request):
    require(req, write=True)
    check_profile(name)
    new = validate_tune(body.values)
    merged = read_profile(name) | new
    if int(merged["UBATCH"]) > int(merged["BATCH"]):
        raise HTTPException(400, "UBATCH must be <= BATCH")
    if int(merged.get("AGENT_CTX") or 0) > int(merged["CTX"]):
        raise HTTPException(400, "AGENT_CTX (one agent's window) must be <= CTX (the pool shared by all slots)")
    if int(merged.get("PARALLEL", 1)) > 4 and int(merged["UBATCH"]) > 2048:
        raise HTTPException(400, "with PARALLEL > 4 keep UBATCH <= 2048 (the prefill compute buffer would not fit in VRAM)")
    path = PROFILES / f"{name}.env"
    lines = path.read_text().splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^([A-Z_]+)=", line)
        if m and m.group(1) in new:
            lines[i] = f"{m.group(1)}={new.pop(m.group(1))}"
    lines += [f"{k}={v}" for k, v in new.items()]
    path.write_text("\n".join(lines) + "\n")
    audit(client_ip(req), "tune", f"{name}: {json.dumps(body.values)}")
    msg = "saved"
    if name == active_profile():
        await sync_hermes_ctx(name)
        if body.restart:
            msg = await restart_model()
    return {"ok": True, "result": msg, "values": read_profile(name)}


@app.post("/api/profiles/{name}/activate")
async def activate(name: str, req: Request):
    require(req, write=True)
    check_profile(name)
    link = PROFILES / "active.env"
    tmp = PROFILES / ".active.tmp"
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(f"{name}.env")
    tmp.replace(link)
    await sync_hermes_ctx(name)
    audit(client_ip(req), "activate", name)
    return {"ok": True, "result": await restart_model()}


@app.post("/api/model/restart")
async def model_restart(req: Request):
    require(req, write=True)
    audit(client_ip(req), "model_restart")
    return {"ok": True, "result": await restart_model()}


# ---- skills (catalog, registry search, imports via bin/skillport)

SKILLPORT = ROOT / "bin/skillport"
SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_skill_tasks: dict[str, dict] = {}


class SkillImport(BaseModel):
    source: str = Field(min_length=1, max_length=500)
    category: str | None = Field(default=None, max_length=60)
    name: str | None = Field(default=None, max_length=64)
    force: bool = False
    bundle: bool = False


class SkillEnable(BaseModel):
    enabled: bool


async def skillport_json(*args: str, timeout: float = 60) -> Any:
    rc, out = await _run(str(SKILLPORT), *args, timeout=timeout)
    start = min([i for i in (out.find("{"), out.find("[")) if i >= 0] or [-1])
    if rc or start < 0:
        raise HTTPException(500, f"skillport {args[0]} failed: {out[-400:]}")
    return json.loads(out[start:])


@app.get("/api/skills")
async def skills_api(req: Request):
    require(req)
    cat, bundles = await skillport_json("list", "--json"), await skillport_json("bundles", "--json")
    return {"skills": cat, "featured": SKILLS, "bundles": bundles}


@app.get("/api/skills/search")
async def skills_search(req: Request, q: str, source: str = ""):
    require(req)
    q = q.strip()[:200]
    if not q:
        return {"local": [], "remote": []}
    args = ["search", q, "--json", "--limit", "15"] + (["--source", source] if re.fullmatch(r"[a-z-]{1,20}", source or "") else [])
    return await skillport_json(*args, timeout=180)


@app.post("/api/skills/import")
async def skills_import(body: SkillImport, req: Request):
    require(req, write=True)
    src = body.source.strip()
    if "\n" in src or not src:
        raise HTTPException(400, "bad source")
    if body.bundle and not re.fullmatch(r"[a-z0-9-]{1,40}", src):
        raise HTTPException(400, "bad bundle name")
    if body.name and not SKILL_NAME_RE.fullmatch(body.name):
        raise HTTPException(400, "bad skill name")
    if body.category and not re.fullmatch(r"[a-z0-9][a-z0-9/_-]{0,59}", body.category):
        raise HTTPException(400, "bad category")
    tid = secrets.token_hex(6)
    log_path = DATA / f"skillport-{tid}.log"
    cmd = [str(SKILLPORT), "bundle" if body.bundle else "import", src, "-v"]
    cmd += (["--category", body.category] if body.category else []) + (["--name", body.name] if body.name else []) + (["--force"] if body.force else [])
    with open(log_path, "wb") as out:
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    _skill_tasks[tid] = {"proc": proc, "log": log_path, "source": src, "started": time.time()}
    for old, t in list(_skill_tasks.items()):            # keep the dict small: drop finished tasks older than an hour
        if t["proc"].poll() is not None and time.time() - t["started"] > 3600:
            _skill_tasks.pop(old, None); t["log"].unlink(missing_ok=True)
    audit(client_ip(req), "skill_import", ("bundle " if body.bundle else "") + src)
    return {"task": tid}


@app.get("/api/skills/tasks/{tid}")
async def skills_task(tid: str, req: Request):
    require(req)
    t = _skill_tasks.get(tid)
    if not t:
        raise HTTPException(404, "no such task")
    rc = t["proc"].poll()
    try:
        text = t["log"].read_text(errors="replace")[-20000:]
    except OSError:
        text = ""
    return {"done": rc is not None, "rc": rc, "log": text, "source": t["source"], "started": t["started"]}


@app.post("/api/skills/{name}/enable")
async def skills_enable(name: str, body: SkillEnable, req: Request):
    require(req, write=True)
    if not SKILL_NAME_RE.fullmatch(name):
        raise HTTPException(400, "bad skill name")
    cfg = HERMES_HOME / "config.yaml"
    text = cfg.read_text()
    blk = re.search(r"^skills:[ \t]*\n((?:(?:[ \t]+.*)?\n)*)", text, flags=re.M)   # the `skills:` block only (plugins: has its own disabled:)
    inner = re.search(r"^(  disabled: )(\[.*\])[ \t]*$", blk.group(1), flags=re.M) if blk else None
    if not inner:
        raise HTTPException(500, "config.yaml: skills.disabled line not found")
    start, end, prefix, listing = blk.start(1) + inner.start(), blk.start(1) + inner.end(), inner.group(1), inner.group(2)
    try:
        names = json.loads(listing)
    except ValueError:
        raise HTTPException(500, "config.yaml: skills.disabled is not a JSON-style list")
    names = [n for n in names if n != name] + ([] if body.enabled else [name])
    cfg.write_text(text[:start] + prefix + json.dumps(sorted(set(names))) + text[end:])
    audit(client_ip(req), "skill_enable" if body.enabled else "skill_disable", name)
    return {"ok": True, "enabled": body.enabled, "disabled": sorted(set(names))}


class DecideReq(BaseModel):
    prompt: str = Field(min_length=1, max_length=20_000)


@app.post("/api/decide/route")
async def decide_route(body: DecideReq, req: Request):
    """What would the router do for this request? (Skills page tester; nothing is launched.)"""
    require(req)
    if not decider:
        raise HTTPException(503, "decide module unavailable")
    try:
        sk, ef = await asyncio.gather(asyncio.to_thread(decider.route_skills, body.prompt), asyncio.to_thread(decider.route_effort, body.prompt))
    except Exception as e:
        raise HTTPException(502, f"decision model: {e}")
    return {"skills": sk, "effort": ef}


@app.post("/api/skills/{name}/remove")
async def skills_remove(name: str, req: Request):
    require(req, write=True)
    if not SKILL_NAME_RE.fullmatch(name):
        raise HTTPException(400, "bad skill name")
    rc, out = await _run(str(SKILLPORT), "remove", name, timeout=120)
    audit(client_ip(req), "skill_remove", f"{name}: {out.strip()[:200]}")
    if rc:
        raise HTTPException(500, out[-400:])
    return {"ok": True, "output": out.strip()}


# ---- evals & logs

@app.get("/api/evals")
async def evals(req: Request):
    require(req)
    out = []
    for p in sorted((EVALS / "results").glob("*/summary.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            s = json.loads(p.read_text())
            out.append({"label": s["label"], "overall": s["overall"], "n": s["n"], "by_category": s["by_category"],
                        "weakest": s["weakest"], "mtime": p.stat().st_mtime,
                        "results": [{k: r.get(k) for k in ("id", "category", "score", "note", "tool_calls", "wall_s")} for r in s["results"]]})
        except Exception:
            continue
    return out


@app.get("/api/logs/{name}")
async def logs(name: str, req: Request, lines: int = 200):
    require(req)
    if name not in LOG_FILES:
        raise HTTPException(404, "unknown log")
    p = LOGS / LOG_FILES[name]
    if not p.exists():
        return {"lines": []}
    with open(p, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 256 * 1024))
        tail = f.read().decode(errors="replace").splitlines()[-min(lines, 2000):]
    return {"lines": tail}


# ---- live socket

@app.websocket("/ws")
async def ws(sock: WebSocket):
    if sock.headers.get("origin") not in ALLOWED_ORIGINS or not session_for(sock):
        await sock.close(code=4401)
        return
    await sock.accept()
    hub.clients.add(sock)
    try:
        if _status:
            await sock.send_text(json.dumps({"type": "status", "data": _status}))
        while True:
            await sock.receive_text()  # keepalive pings from the client
    except WebSocketDisconnect:
        pass
    finally:
        hub.clients.discard(sock)


# ---- static SPA

@app.get("/")
async def index():
    return FileResponse(HUD / "static/index.html")



# ---- Toolbox page (2026-11-03): the agent's persistent toolbox — registry, usage counts, host runs, installs ----
TB_USAGE = TOOLBOX / "logs/usage.jsonl"
TB_CLI = TOOLBOX / "bin/tb"
TB_NAME_RE = re.compile(r"[a-zA-Z0-9_.+-]{1,64}")
TB_PKG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+=<>!,\[\]@/-]{0,80}")
TB_PROXY = {k: "http://127.0.0.1:7897" for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")} | {"NO_PROXY": "localhost,127.0.0.1", "no_proxy": "localhost,127.0.0.1"}
_tb_cache: dict = {}
_tb_tasks: dict[str, dict] = {}


class TbInstall(BaseModel):
    kind: str = Field(pattern=r"^(conda|pip|npm)$")
    pkgs: str = Field(min_length=1, max_length=400)


class TbRecipe(BaseModel):
    text: str = Field(max_length=20000)


def _tb_events() -> list[dict]:
    """logs/usage.jsonl parsed (every tb command, shimmed command, wrapper and host run), cached by size+mtime."""
    try:
        st = TB_USAGE.stat()
    except OSError:
        return []
    key = (st.st_size, st.st_mtime)
    if _tb_cache.get("ev_key") == key:
        return _tb_cache["ev"]
    ev = []
    with open(TB_USAGE, errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if isinstance(d, dict) and isinstance(d.get("ts"), (int, float)) and isinstance(d.get("tool"), str):
                ev.append(d)
    _tb_cache["ev_key"], _tb_cache["ev"] = key, ev
    return ev


def _tb_env() -> dict:
    return os.environ | {"PATH": f"{TOOLBOX}/bin:{TOOLBOX}/env/bin:{TOOLBOX}/npm/bin:" + os.environ.get("PATH", "")} | {k: v for k, v in TB_PROXY.items() if k not in os.environ}


async def _tb_doctor(force: bool = False) -> dict:
    if not force and time.time() - _tb_cache.get("doc_ts", 0) < 300 and "doc" in _tb_cache:
        return _tb_cache["doc"]
    def run():
        r = subprocess.run([str(HUD / ".venv/bin/python"), str(TB_CLI), "doctor"], capture_output=True, text=True, timeout=60, env=_tb_env())
        out = (r.stdout + r.stderr).strip()
        m = re.search(r"(\d+) tools, (\d+) missing", out)
        return {"ts": time.time(), "ok": r.returncode == 0, "tools": int(m.group(1)) if m else None, "missing": int(m.group(2)) if m else None, "output": out[-4000:]}
    try:
        _tb_cache["doc"] = await asyncio.to_thread(run)
    except Exception as e:
        _tb_cache["doc"] = {"ts": time.time(), "ok": False, "tools": None, "missing": None, "output": f"doctor failed: {e}"}
    _tb_cache["doc_ts"] = time.time()
    return _tb_cache["doc"]


async def _tb_disk() -> dict:
    if time.time() - _tb_cache.get("du_ts", 0) < 900 and "du" in _tb_cache:
        return _tb_cache["du"]
    def du():
        out = {}
        for d in ("env", "envs", "venvs", "opt", "mamba", "cache", "fonts", "npm"):
            if (TOOLBOX / d).exists():
                r = subprocess.run(["du", "-sb", str(TOOLBOX / d)], capture_output=True, text=True, timeout=120)
                out[d] = int(r.stdout.split()[0]) if r.stdout.strip() else 0
        return out
    try:
        _tb_cache["du"] = await asyncio.to_thread(du)
    except Exception:
        _tb_cache["du"] = {}
    _tb_cache["du_ts"] = time.time()
    return _tb_cache["du"]


def _tb_host_runs(limit: int = 30) -> list[dict]:
    runs = []
    for f in sorted(HOST_RUNS.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        args = ""
        try:
            first = Path(d.get("log", "")).read_text(errors="replace").split("\n", 1)[0]
            m = re.match(r"# host_run \S+ (\[.*\]) at", first)
            args = " ".join(json.loads(m.group(1).replace("'", '"'))) if m else ""
        except Exception:
            pass
        runs.append({"id": f.stem, "ts": f.stat().st_mtime, "tool": d.get("tool"), "exit": d.get("exit"), "seconds": d.get("seconds"), "args": args[:200]})
    return runs


@app.get("/api/toolbox")
async def toolbox_api(req: Request, days: int = 30):
    require(req)
    days = max(1, min(int(days), 365))
    try:
        reg = json.loads((TOOLBOX / "registry.json").read_text()).get("tools", {})
    except Exception:
        reg = {}
    ev = _tb_events()
    now = time.time()
    since = now - days * 86400
    per_tool: dict[str, dict] = {}
    per_day: dict[str, int] = {}
    kinds: dict[str, int] = {}
    where: dict[str, int] = {}
    installs = host_runs_range = 0
    for e in ev:
        st = per_tool.setdefault(e["tool"], {"total": 0, "range": 0, "last": 0, "last_args": "", "last_where": ""})
        st["total"] += 1
        if e["ts"] >= since:
            st["range"] += 1
            day = time.strftime("%Y-%m-%d", time.localtime(e["ts"]))
            per_day[day] = per_day.get(day, 0) + 1
            kinds[e.get("kind", "?")] = kinds.get(e.get("kind", "?"), 0) + 1
            where[e.get("where", "?")] = where.get(e.get("where", "?"), 0) + 1
            installs += e.get("kind") == "install"
            host_runs_range += e.get("kind") == "host"
        if e["ts"] >= st["last"]:
            st["last"], st["last_args"], st["last_where"] = e["ts"], str(e.get("args", ""))[:120], str(e.get("where", ""))
    labels = [time.strftime("%Y-%m-%d", time.localtime(now - i * 86400)) for i in range(min(days, 90) - 1, -1, -1)]
    zero = {"total": 0, "range": 0, "last": 0, "last_args": "", "last_where": ""}
    tools = []
    scripts = {q.name for q in (TOOLBOX / "bin").iterdir()} if (TOOLBOX / "bin").is_dir() else set()
    for n, t in sorted(reg.items()):
        exe = (t.get("run") or "").split()[0] if t.get("run") else ""
        if exe in reg or exe in scripts or exe in ("java", "eval", "export", "host_run"):
            exe = ""                                                  # `tbpy -c ...` / `java -jar ...` runs must not inherit the wrapper's count
        u = dict(per_tool.get(n) or per_tool.get(exe) or zero)      # conda tools are logged under their command name (rg, not ripgrep)
        if n in per_tool and exe and exe != n and exe in per_tool:
            o = per_tool[exe]
            u = {"total": u["total"] + o["total"], "range": u["range"] + o["range"], **({"last": o["last"], "last_args": o["last_args"], "last_where": o["last_where"]} if o["last"] > u["last"] else {"last": u["last"], "last_args": u["last_args"], "last_where": u["last_where"]})}
        tools.append({"name": n, **{k: t.get(k, "") for k in ("desc", "where", "run", "created", "updated", "by")}, "usage": u,
                      "recipe": (TOOLBOX / "recipes" / f"{n}.md").exists(), "shim": (TOOLBOX / "shims" / exe).exists() if exe else False})
    known = set(reg) | {(t.get("run") or "").split()[0] for t in reg.values() if t.get("run")}
    other = sorted(((n, s) for n, s in per_tool.items() if n not in known), key=lambda x: -x[1]["range"])
    top = sorted(((n, s["range"]) for n, s in per_tool.items() if s["range"]), key=lambda x: -x[1])[:14]
    recent = [{k: e.get(k) for k in ("ts", "tool", "kind", "args", "where", "exit", "seconds")} for e in ev[-40:]][::-1]
    return {"days": days, "tools": tools, "other": [{"name": n, "usage": s} for n, s in other[:20]], "events_total": len(ev),
            "labels": labels, "per_day": [per_day.get(d, 0) for d in labels], "top": top, "kinds": kinds, "where": where,
            "installs_range": installs, "host_runs_range": host_runs_range, "host_runs": _tb_host_runs(),
            "recent": recent, "disk": await _tb_disk(), "doctor": _tb_cache.get("doc") or await _tb_doctor(),
            "paths": {"toolbox": str(TOOLBOX), "usage": str(TB_USAGE)}}


@app.post("/api/toolbox/doctor")
async def toolbox_doctor(req: Request):
    require(req, write=True)
    return await _tb_doctor(force=True)


@app.get("/api/toolbox/recipe/{name}")
async def toolbox_recipe(name: str, req: Request):
    require(req)
    if not TB_NAME_RE.fullmatch(name):
        raise HTTPException(400, "bad tool name")
    rp = TOOLBOX / "recipes" / f"{name}.md"
    try:
        reg = json.loads((TOOLBOX / "registry.json").read_text()).get("tools", {})
    except Exception:
        reg = {}
    if name not in reg and not rp.exists():
        raise HTTPException(404, "no such tool")
    hp = TOOLBOX / "host" / name
    return {"name": name, "tool": reg.get(name), "recipe": rp.read_text(errors="replace")[:20000] if rp.exists() else "",
            "host_script": hp.read_text(errors="replace")[:20000] if hp.is_file() else None}


@app.post("/api/toolbox/recipe/{name}")
async def toolbox_recipe_save(name: str, body: TbRecipe, req: Request):
    require(req, write=True)
    if not TB_NAME_RE.fullmatch(name):
        raise HTTPException(400, "bad tool name")
    rp = TOOLBOX / "recipes" / f"{name}.md"
    rp.parent.mkdir(exist_ok=True)
    rp.write_text(body.text if body.text.endswith("\n") else body.text + "\n")
    audit(client_ip(req), "toolbox_recipe", name)
    return {"ok": True, "chars": len(body.text)}


@app.post("/api/toolbox/{name}/remove")
async def toolbox_remove(name: str, req: Request):
    require(req, write=True)
    if not TB_NAME_RE.fullmatch(name):
        raise HTTPException(400, "bad tool name")
    rc, out = await _run(str(HUD / ".venv/bin/python"), str(TB_CLI), "unregister", name, timeout=30)
    audit(client_ip(req), "toolbox_unregister", name)
    if rc != 0:
        raise HTTPException(400, out.strip()[-300:] or "unregister failed")
    return {"ok": True, "output": out.strip()[-300:]}


@app.post("/api/toolbox/install")
async def toolbox_install(body: TbInstall, req: Request):
    require(req, write=True)
    pkgs = body.pkgs.split()
    if not pkgs or len(pkgs) > 12 or any(not TB_PKG_RE.fullmatch(p) or p.startswith("-") for p in pkgs):
        raise HTTPException(400, "package names only (no flags), up to 12")
    if any(t["proc"].poll() is None for t in _tb_tasks.values()):
        raise HTTPException(409, "an install is already running")
    tid = secrets.token_hex(6)
    log_path = DATA / f"toolbox-{tid}.log"
    sub = {"conda": "install", "pip": "pip", "npm": "npm"}[body.kind]
    cmd = [str(HUD / ".venv/bin/python"), str(TB_CLI), sub, *pkgs]
    with open(log_path, "wb") as out:
        proc = subprocess.Popen(cmd, cwd=str(TOOLBOX), stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True, env=_tb_env())
    _tb_tasks[tid] = {"proc": proc, "log": log_path, "cmd": f"tb {sub} {' '.join(pkgs)}", "started": time.time()}
    for old, t in list(_tb_tasks.items()):
        if t["proc"].poll() is not None and time.time() - t["started"] > 3600:
            _tb_tasks.pop(old, None); t["log"].unlink(missing_ok=True)
    audit(client_ip(req), "toolbox_install", f"{body.kind} {' '.join(pkgs)}")
    return {"task": tid, "cmd": _tb_tasks[tid]["cmd"]}


@app.get("/api/toolbox/tasks/{tid}")
async def toolbox_task(tid: str, req: Request):
    require(req)
    t = _tb_tasks.get(re.sub(r"[^0-9a-f]", "", tid))
    if not t:
        raise HTTPException(404, "no such task")
    rc = t["proc"].poll()
    try:
        text = t["log"].read_text(errors="replace")[-20000:]
    except OSError:
        text = ""
    if rc is not None:
        _tb_cache.pop("doc_ts", None)                   # a finished install changes what doctor sees
    return {"done": rc is not None, "rc": rc, "log": text, "cmd": t["cmd"], "started": t["started"]}


@app.get("/api/toolbox/hostlog/{rid}")
async def toolbox_hostlog(rid: str, req: Request):
    require(req)
    rid = re.sub(r"[^0-9a-f-]", "", rid)
    d = _host_run_result(rid)
    if not d:
        raise HTTPException(404, "no such run")
    lp = Path(str(d.get("log", ""))).resolve()
    if lp.parent != HOST_RUNS.resolve() or not lp.is_file():
        return {"id": rid, "text": d.get("tail", "")}
    return {"id": rid, "text": lp.read_text(errors="replace")[-30000:]}


app.mount("/static", StaticFiles(directory=HUD / "static"), name="static")
