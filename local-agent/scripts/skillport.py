#!/usr/bin/env python3
"""skillport — port agent skills from anywhere into Hermes (hermes-home/skills).

Sources it understands
  • local skill folders in any SKILL.md dialect: Claude Code (~/.claude/skills, plugin caches,
    <project>/.claude/skills), Codex / Cursor (~/.agents/skills, ~/.codex/skills, ~/.cursor/skills),
    OpenClaw / Clawdbot, agentskills.io, plain markdown or a ChatGPT custom-GPT export (.json)
  • a .zip of any of the above
  • a git URL (github.com/owner/repo[/tree/<ref>/<sub/path>])
  • a Skills Hub identifier (official/…, skills-sh/…, owner/repo/path, clawhub names) or a direct
    SKILL.md URL — delegated to `hermes skills install`, then normalised the same way

What "porting" does (hermesize)
  • frontmatter → Hermes conventions: ≤60-char one-sentence `description` (Hermes only shows the
    first 60 chars in the skills index; the original stays as `metadata.port.original_description`
    and as a "Use when" line at the top of the body), `version`, `author`, `license`,
    `metadata.hermes.{tags,category}`, provenance under `metadata.port`
  • drops fields Hermes ignores (allowed-tools, model, hooks, argument-hint, …) and records them
  • rewrites Claude/Codex tool names in prose to the Hermes tools (`Bash`→`terminal`, `Read`→
    `read_file`, `Edit`→`patch`, `Grep`→`search_files`, `WebFetch`→`web_extract`, …)
  • appends a "Hermes notes" section (where the skill's files live inside the sandbox)
  • static security scan with Hermes' own skills-guard before anything lands in hermes-home

State: hermes-home/skills/.port/ledger.json (what was ported from where, content hashes).
Run with the HUD venv python (pyyaml + httpx); the hermes-agent checkout is put on sys.path for
the skills-guard scanner. `bin/skillport` does that for you.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(os.environ.get("LA_ROOT", "/mnt/data/local-agent"))
HERMES_AGENT = ROOT / "hermes-agent"
HERMES = HERMES_AGENT / ".venv/bin/hermes"
HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(ROOT / "hermes-home")))
SKILLS = HERMES_HOME / "skills"
PORT_DIR = SKILLS / ".port"
LEDGER = PORT_DIR / "ledger.json"
# Clone cache must live OUTSIDE skills/: Hermes indexes every SKILL.md under skills/ (and mounts it into the sandbox).
GIT_CACHE = HERMES_HOME / "skillport-cache"
LOCK = SKILLS / ".hub" / "lock.json"
CONFIG = HERMES_HOME / "config.yaml"
LLM_URL = os.environ.get("LA_LLM_URL", "http://127.0.0.1:8181/v1/chat/completions")
DESC_LIMIT = 60
SANDBOX_SKILLS = "/root/.hermes/skills"   # read-only bind mount of hermes-home/skills inside the Docker sandbox

sys.path.insert(0, str(HERMES_AGENT))
try:
    import yaml  # hud venv
except ImportError:  # pragma: no cover
    yaml = None

# --------------------------------------------------------------------------- known local roots
HOME = Path.home()
LOCAL_ROOTS: list[tuple[str, str]] = [        # (kind, glob) — globs relative to / , expanded with Path.glob
    ("claude", str(HOME / ".claude/skills")),
    ("claude", str(HOME / ".claude/skills/synced/*")),
    ("claude", str(HOME / ".claude/plugins/cache/*/*/skills")),
    ("claude", str(HOME / ".claude/plugins/marketplaces/*/plugins/*/skills")),
    ("claude", str(HOME / ".claude/plugins/marketplaces/*/external_plugins/*/skills")),
    ("codex", str(HOME / ".codex/skills")),
    ("codex", str(HOME / ".agents/skills")),
    ("cursor", str(HOME / ".cursor/skills")),
    ("openclaw", str(HOME / ".openclaw/skills")),
    ("openclaw", str(HOME / ".clawdbot/skills")),
    ("gemini", str(HOME / ".gemini/skills")),
    ("claude", "/mnt/data/*/.claude/skills"),
    ("codex", "/mnt/data/*/.agents/skills"),
    ("codex", "/mnt/data/*/.codex/skills"),
    ("cursor", "/mnt/data/*/.cursor/skills"),
    ("agentskills", "/mnt/data/*/skills"),
]
SKIP_PARTS = {".trash", ".git", "node_modules", "__pycache__", ".venv", "venv", ".archive", ".hub", ".port",
              "references", "templates", "assets", "scripts", "hermes-home", "hermes-home-test", "hermes-agent",
              "hermes-agent-next"}

# --------------------------------------------------------------------------- tool-name mapping
# Only exact backticked tokens and "the X tool" phrasings are rewritten, so prose stays readable.
TOOL_MAP = {
    "Bash": "terminal", "BashTool": "terminal", "Shell": "terminal", "shell": "terminal",
    "Read": "read_file", "ReadFile": "read_file", "View": "read_file", "cat": "read_file",
    "Write": "write_file", "WriteFile": "write_file", "Create": "write_file",
    "Edit": "patch", "MultiEdit": "patch", "StrReplace": "patch", "str_replace_editor": "patch",
    "apply_patch": "patch",
    "Grep": "search_files", "Glob": "search_files", "LS": "search_files target='files'", "Find": "search_files",
    "WebFetch": "web_extract", "fetch": "web_extract", "WebSearch": "web_search",
    "Task": "spawn (crew)", "Agent": "spawn (crew)", "SubAgent": "spawn (crew)",
    "TodoWrite": "todo", "TodoRead": "todo", "AskUserQuestion": "clarify", "Skill": "skill_view",
    "NotebookEdit": "patch", "NotebookRead": "read_file", "computer": "browser_navigate",
}
DROP_KEYS = {"allowed-tools", "allowed_tools", "model", "context", "agent", "hooks", "paths", "argument-hint",
             "argument_hint", "disable-model-invocation", "user-invocable", "effort", "when_to_use", "risk",
             "source", "date_added", "compatibility", "openai_compat"}
CATEGORY_RULES = [
    ("security", r"reverse.?engineer|binary|ghidra|radare|malware|exploit|pentest|forensic|disassembl|decompil|ctf|vulnerab"),
    ("web-development", r"\bweb design|landing page|frontend|front-end|\bui\b|\bux\b|\bcss\b|tailwind|react|next\.?js|website|html|responsive|design system|typograph"),
    ("mcp", r"\bmcp\b|model context protocol"),
    ("data-science", r"jupyter|pandas|dataframe|notebook|data science|statistic|plot"),
    ("research", r"research|arxiv|paper|literature|search the web|osint|recon"),
    ("software-development", r"\bcode\b|coding|refactor|test|debug|\bgit\b|pull request|\bpr\b|typescript|python|rust|golang|api"),
    ("productivity", r"docx|xlsx|pptx|\bpdf\b|spreadsheet|document|slides|notes|email|calendar"),
    ("creative", r"design|art|poster|image|video|animation|music|font|diagram"),
    ("devops", r"docker|kubernetes|deploy|ci/cd|terraform|systemd|nginx"),
]
STOPWORDS = {"the", "a", "an", "and", "or", "for", "to", "of", "in", "with", "this", "that", "use", "when", "user",
             "skill", "skills", "you", "your", "any", "all", "it", "is", "are", "on", "as", "by", "from", "into",
             "complete", "system", "building", "build", "create", "high", "strict", "rules", "using", "based", "like",
             "about", "should", "used", "asks", "wants", "whenever", "including", "such", "other", "plus", "also"}


def log(msg: str = "") -> None:
    print(msg, flush=True)


def now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")
    return s[:64] or "skill"


# --------------------------------------------------------------------------- frontmatter I/O
def parse_frontmatter(text: str) -> tuple[dict, str]:
    text = text.removeprefix("﻿")
    if not text.startswith("---"):
        return {}, text
    m = re.search(r"\n---\s*\n", text[3:])
    if not m:
        return {}, text
    raw, body = text[3:m.start() + 3], text[m.end() + 3:]
    fm: dict = {}
    try:
        if yaml:
            loaded = yaml.safe_load(raw)
            if isinstance(loaded, dict):
                fm = loaded
            elif loaded is not None:
                raise ValueError("frontmatter is not a mapping")
        else:
            raise ValueError("no yaml")
    except Exception:
        for line in raw.strip().splitlines():
            if ":" in line and not line.startswith((" ", "\t", "-")):
                k, v = line.split(":", 1)
                fm[k.strip()] = v.strip().strip("'\"")
    return fm, body


def dump_frontmatter(fm: dict) -> str:
    if yaml:
        out = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, width=4096, default_flow_style=None)
    else:  # minimal emitter (strings only)
        out = "".join(f"{k}: {json.dumps(v) if not isinstance(v, str) else json.dumps(v)}\n" for k, v in fm.items())
    return f"---\n{out}---\n"


def read_skill(skill_dir: Path) -> tuple[dict, str]:
    return parse_frontmatter((skill_dir / "SKILL.md").read_text(encoding="utf-8", errors="replace"))


# --------------------------------------------------------------------------- ledger / lock / config
def load_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def ledger() -> dict:
    d = load_json(LEDGER, {"version": 1, "skills": {}})
    d.setdefault("skills", {})
    return d


def save_ledger(d: dict) -> None:
    PORT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(LEDGER)


def hub_installed() -> dict:
    return load_json(LOCK, {}).get("installed", {}) or {}


def disabled_names() -> set[str]:
    try:
        cfg = yaml.safe_load(CONFIG.read_text()) if yaml else {}
        return set((cfg.get("skills") or {}).get("disabled") or [])
    except Exception:
        return set()


def content_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(x for x in root.rglob("*") if x.is_file() and not any(s in x.parts for s in (".git", "__pycache__"))):
        h.update(str(p.relative_to(root)).encode()); h.update(b"\0"); h.update(p.read_bytes()); h.update(b"\0")
    return h.hexdigest()[:16]


# --------------------------------------------------------------------------- catalog of what Hermes has
def is_skill_dir(p: Path) -> bool:
    return (p / "SKILL.md").is_file()


def installed_catalog() -> list[dict]:
    """Every skill under hermes-home/skills with its owner (builtin / local / hub / ported)."""
    led, lock, dis = ledger()["skills"], hub_installed(), disabled_names()
    hub_paths = {str((SKILLS / v.get("install_path", "")).resolve()) for v in lock.values() if v.get("install_path")}
    out = []
    for md in sorted(SKILLS.rglob("SKILL.md")):
        if any(part.startswith(".") or part in ("references", "templates", "assets", "scripts") for part in md.relative_to(SKILLS).parts[:-1]):
            continue
        d = md.parent
        fm, _ = read_skill(d)
        name = str(fm.get("name") or d.name)
        rel = d.relative_to(SKILLS)
        category = "/".join(rel.parts[:-1]) or ""
        if name in led:
            source = "ported"
        elif str(d.resolve()) in hub_paths or name in lock:
            source = "hub"
        elif category.startswith("local-agent"):
            source = "local"
        else:
            source = "builtin"
        desc = str(fm.get("description") or "").strip()
        out.append({"name": name, "dir": d.name, "category": category, "path": str(d), "source": source,
                    "description": desc, "short": desc if len(desc) <= DESC_LIMIT else desc[:DESC_LIMIT - 3] + "...",
                    "enabled": name not in dis and d.name not in dis,
                    "tags": ((fm.get("metadata") or {}).get("hermes") or {}).get("tags") or fm.get("tags") or [],
                    "version": str(fm.get("version") or ""), "port": (fm.get("metadata") or {}).get("port")})
    return out


def find_installed(name: str) -> list[dict]:
    return [s for s in installed_catalog() if s["name"] == name or s["dir"] == name]


# --------------------------------------------------------------------------- local sources
def detect_kind(path: Path, hint: str = "") -> str:
    s = str(path)
    if hint and hint not in ("agentskills",):
        return hint
    if "/.claude/" in s or "claude" in s.lower():
        return "claude"
    if "/.codex/" in s or "/.agents/" in s:
        return "codex"
    if "/.cursor/" in s:
        return "cursor"
    if "clawdbot" in s or "openclaw" in s:
        return "openclaw"
    return hint or "agentskills"


def scan_sources(extra: list[str] | None = None, include_all: bool = False) -> list[dict]:
    seen: dict[str, dict] = {}
    roots = [(kind, Path(p)) for kind, pattern in LOCAL_ROOTS for p in sorted(Path("/").glob(pattern.lstrip("/")))]
    roots += [("agentskills", Path(p).expanduser()) for p in (extra or [])]
    have = {s["name"]: s for s in installed_catalog()}
    for kind, root in roots:
        if not root.is_dir() or any(part in SKIP_PARTS for part in root.parts if part != "skills"):
            continue
        if root == SKILLS or SKILLS in root.parents or root in SKILLS.parents:
            continue
        candidates = [root] if is_skill_dir(root) else [d for d in sorted(root.iterdir()) if d.is_dir() and is_skill_dir(d)]
        if not candidates and include_all:
            candidates = [md.parent for md in root.rglob("SKILL.md") if not any(p in SKIP_PARTS for p in md.relative_to(root).parts)]
        for d in candidates:
            if any(p in SKIP_PARTS for p in d.parts):
                continue
            try:
                fm, body = read_skill(d)
            except Exception:
                continue
            name = slugify(fm.get("name") or d.name)
            key = str(d.resolve())
            if key in seen:
                continue
            desc = str(fm.get("description") or "").strip().replace("\n", " ")
            status = "installed" if name in have and have[name]["source"] == "ported" else ("name-taken" if name in have else "new")
            seen[key] = {"name": name, "kind": detect_kind(d, kind), "path": key, "description": desc, "status": status,
                         "files": sum(1 for _ in d.rglob("*") if _.is_file())}
    return list(seen.values())


# --------------------------------------------------------------------------- description shortening
def heuristic_short(desc: str, name: str) -> str:
    d = re.sub(r"\s+", " ", desc).strip().strip("'\"")
    d = re.sub(r"^(use (this skill|it|when|whenever|for)|this skill|skill (that|to|for)|helps? (you |the user )?(to )?|"
               r"guidance for|guide (for|to)|toolkit for|a |an |the )\s*", "", d, flags=re.I)
    d = re.sub(r"^(whenever|when|if) (the )?(user|you|someone) (wants?|asks?|needs?|is trying) (to |for )?", "", d, flags=re.I)
    d = re.sub(r"^(to |for )", "", d, flags=re.I)
    first = re.split(r"(?<=[.!?])\s|[:;(]\s*", d, maxsplit=1)[0].strip().rstrip(".,;:")
    if not first:
        first = name.replace("-", " ")
    first = first[0].upper() + first[1:]
    if len(first) > DESC_LIMIT - 1:
        cut = first[:DESC_LIMIT - 1]
        cut = cut[:cut.rfind(" ")] if " " in cut[20:] else cut
        first = cut.rstrip(" ,;:-")
    return first + "."


def llm_short(desc: str, name: str, body: str, timeout: float = 45) -> str | None:
    try:
        import httpx
    except ImportError:
        return None
    prompt = (f"Write a one-sentence description of this agent skill for a skills index. HARD LIMIT: at most 55 characters. "
              f"Imperative or noun phrase, end with a period, no marketing words, do not repeat the name, no quotes.\n\n"
              f"Skill name: {name}\nOriginal description: {desc[:1200]}\n\nBody excerpt:\n{body[:1500]}\n\nReply with the sentence only.")
    try:
        r = httpx.post(LLM_URL, json={"model": "local", "messages": [{"role": "user", "content": prompt}], "max_tokens": 48,
                                      "temperature": 0, "chat_template_kwargs": {"enable_thinking": False}},
                       timeout=timeout, trust_env=False)
        text = (r.json()["choices"][0]["message"].get("content") or "").strip().strip('"\'`').split("\n")[0].strip()
    except Exception:
        return None
    if not text or len(text) > DESC_LIMIT or len(text) < 8:
        return None
    if not text.endswith("."):
        text += "."
    return text[0].upper() + text[1:]


def short_description(desc: str, name: str, body: str, use_llm: bool) -> tuple[str, str]:
    """-> (short, how). Keeps a short original untouched."""
    d = re.sub(r"\s+", " ", desc or "").strip().strip("'\"")
    if d and len(d) <= DESC_LIMIT:
        return (d if d.endswith((".", "!", "?")) else d + "."), "kept"
    if use_llm:
        s = llm_short(d, name, body)
        if s:
            return s, "llm"
    if not d:  # no description at all: first non-heading body line
        d = next((ln.strip() for ln in body.splitlines() if ln.strip() and not ln.lstrip().startswith(("#", "---", ">"))), name)
    return heuristic_short(d, name), "heuristic"


# --------------------------------------------------------------------------- body rewriting
def rewrite_tools(body: str) -> tuple[str, list[str]]:
    applied: set[str] = set()

    def sub_bt(m):
        tok = m.group(1)
        if tok in TOOL_MAP and TOOL_MAP[tok] != tok:
            applied.add(f"{tok}→{TOOL_MAP[tok]}")
            return f"`{TOOL_MAP[tok]}`"
        return m.group(0)
    out = re.sub(r"`([A-Za-z_]+)`", sub_bt, body)

    def sub_tool(m):
        tok = m.group(2)
        if tok in TOOL_MAP:
            applied.add(f"{tok}→{TOOL_MAP[tok]}")
            return f"{m.group(1)}`{TOOL_MAP[tok]}`{m.group(3)}"
        return m.group(0)
    out = re.sub(r"(\bthe\s+)([A-Z][A-Za-z]+)(\s+tool\b)", sub_tool, out)
    out = re.sub(r"(?<![\w/])~/\.claude/skills/[\w.-]+/?", "<skill-base-dir>/", out)
    out = re.sub(r"/mnt/skills/(?:public|user)/[\w.-]+/?", "<skill-base-dir>/", out)
    return out, sorted(applied)


def infer_category(name: str, desc: str, tags: list[str]) -> str:
    hay = " ".join([name, desc, " ".join(map(str, tags))]).lower()
    for cat, rx in CATEGORY_RULES:
        if re.search(rx, hay):
            return cat
    return "ported"


def infer_tags(name: str, desc: str, existing: list) -> list[str]:
    tags = [str(t).strip().lower() for t in (existing or []) if str(t).strip()]
    if not tags:
        words = [w.strip(".-") for w in re.findall(r"[a-z][a-z0-9+#.-]{2,}", (name.replace("-", " ") + " " + desc).lower())]
        for w in words:
            if w not in STOPWORDS and w not in tags:
                tags.append(w)
            if len(tags) >= 6:
                break
    return tags[:10]


def hermesize(skill_dir: Path, *, source: dict, use_llm: bool = True, category: str | None = None,
              name_override: str | None = None) -> dict:
    """Rewrite <skill_dir>/SKILL.md in place to Hermes conventions. Returns a report dict."""
    md = skill_dir / "SKILL.md"
    fm, body = parse_frontmatter(md.read_text(encoding="utf-8", errors="replace"))
    fm = dict(fm or {})
    original_name = str(fm.get("name") or skill_dir.name)
    name = slugify(name_override or original_name)
    meta = fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {}
    hermes_meta = dict(meta.get("hermes") or {})
    prior_port = dict(meta.get("port") or {})
    orig_desc = str(prior_port.get("original_description") or fm.get("description") or "").strip()
    short, how = short_description(orig_desc, name, body, use_llm)
    tags = infer_tags(name, orig_desc, hermes_meta.get("tags") or fm.get("tags") or meta.get("tags") or [])
    cat = category or hermes_meta.get("category") or infer_category(name, orig_desc, tags)
    dropped = {k: fm[k] for k in list(fm) if k in DROP_KEYS}
    new_body, tool_changes = rewrite_tools(body)
    if orig_desc and len(orig_desc) > DESC_LIMIT and "**Use when:**" not in new_body:
        new_body = f"> **Use when:** {re.sub(r'\\s+', ' ', orig_desc)}\n\n" + new_body.lstrip("\n")
    notes_marker = "## Hermes notes"
    if notes_marker not in new_body:
        rel = f"{cat}/{name}" if cat else name
        files = sorted(str(p.relative_to(skill_dir)) for p in skill_dir.rglob("*")
                       if p.is_file() and p.name != "SKILL.md" and not any(s in p.parts for s in (".git", "__pycache__")))
        lines = [f"\n\n{notes_marker}\n",
                 f"Ported into Hermes by skillport from {source.get('kind', 'an external')} skill `{original_name}` "
                 f"({source.get('ref', source.get('path', 'local'))}).",
                 f"- Tool names were mapped to Hermes tools: {', '.join(tool_changes) if tool_changes else 'none needed'}.",
                 f"- Read linked files with `skill_view(name=\"{name}\", file_path=\"<relative path>\")`.",
                 f"- Inside the sandbox this skill's folder is mounted read-only at `{SANDBOX_SKILLS}/{rel}/` "
                 f"(run helper scripts from there with `terminal`; copy them to `/workspace` first if they need to write next to themselves).",
                 ]
        if files:
            shown = files[:25]
            lines.append(f"- Files: {', '.join(f'`{f}`' for f in shown)}" + (f" … (+{len(files) - 25} more)" if len(files) > 25 else ""))
        if "$ARGUMENTS" in body:
            lines.append("- `$ARGUMENTS` placeholders mean the user's request text (Hermes has no slash-command arguments).")
        new_body = new_body.rstrip() + "\n".join(lines) + "\n"
    port_meta = {**prior_port, "source": source.get("ref", source.get("path", "")), "kind": source.get("kind", "unknown"),
                 "imported_at": now(), "original_name": original_name, "original_description": orig_desc,
                 "description_how": how, "dropped": {k: str(v)[:200] for k, v in dropped.items()} or prior_port.get("dropped", {}),
                 "tool_map": tool_changes}
    if source.get("hash"):
        port_meta["source_hash"] = source["hash"]
    new_fm: dict = {"name": name, "description": short,
                    "version": str(fm.get("version") or meta.get("version") or "1.0.0"),
                    "author": str(fm.get("author") or meta.get("author") or source.get("author") or "ported"),
                    "license": str(fm.get("license") or meta.get("license") or "see upstream")}
    if fm.get("platforms"):
        new_fm["platforms"] = fm["platforms"]
    for k, v in fm.items():
        if k not in new_fm and k not in DROP_KEYS and k not in ("metadata", "tags", "category", "triggers"):
            new_fm[k] = v
    if fm.get("triggers"):
        new_fm["triggers"] = fm["triggers"]
    other_meta = {k: v for k, v in meta.items() if k not in ("hermes", "port")}
    new_fm["metadata"] = {**other_meta, "hermes": {**hermes_meta, "tags": tags, "category": cat}, "port": port_meta}
    md.write_text(dump_frontmatter(new_fm) + "\n" + new_body.lstrip("\n"), encoding="utf-8")
    for p in list(skill_dir.glob("scripts/*")) + list(skill_dir.glob("scripts/**/*")):
        if p.is_file() and p.suffix in (".py", ".sh", "") and not os.access(p, os.X_OK):
            p.chmod(p.stat().st_mode | 0o111)
    return {"name": name, "category": cat, "description": short, "how": how, "tags": tags, "dropped": sorted(dropped),
            "tool_map": tool_changes}


# --------------------------------------------------------------------------- security scan
def scan(skill_dir: Path, source_id: str, force: bool) -> tuple[bool, str]:
    """skills-guard decides trust from *source_id* (an "owner/repo" id: Hermes' TRUSTED_REPOS such as
    anthropics/skills or openai/skills are trusted, everything else — including local folders — is community)."""
    try:
        from tools.skills_guard import format_scan_report, scan_skill, should_allow_install
    except Exception as e:  # pragma: no cover
        return True, f"(skills-guard unavailable: {e})"
    res = scan_skill(skill_dir, source=source_id)
    ok, reason = should_allow_install(res, force=force)
    report = format_scan_report(res) if res.findings else f"scan: {res.verdict} (0 findings)"
    return ok, report + ("" if ok else f"\nBLOCKED: {reason}")


# --------------------------------------------------------------------------- source materialisation
def materialise(src: str, tmp: Path) -> tuple[list[Path], dict]:
    """Turn a source spec into local skill dirs. -> (dirs, source_info)."""
    p = Path(src).expanduser()
    if p.exists():
        if p.is_dir():
            if is_skill_dir(p):
                return [p], {"kind": detect_kind(p), "path": str(p.resolve()), "ref": str(p.resolve())}
            subs = [d for d in sorted(p.iterdir()) if d.is_dir() and is_skill_dir(d)]
            if subs:
                return subs, {"kind": detect_kind(p), "path": str(p.resolve()), "ref": str(p.resolve())}
            raise SystemExit(f"{p}: no SKILL.md here or in its subfolders")
        if p.suffix.lower() == ".zip":
            out = tmp / "zip"; out.mkdir()
            with zipfile.ZipFile(p) as z:
                for m in z.infolist():
                    if m.filename.startswith("/") or ".." in Path(m.filename).parts:
                        raise SystemExit(f"unsafe path in zip: {m.filename}")
                z.extractall(out)
            dirs = sorted({md.parent for md in out.rglob("SKILL.md")})
            return dirs, {"kind": "zip", "path": str(p.resolve()), "ref": str(p.resolve())}
        if p.suffix.lower() == ".json":  # ChatGPT custom GPT export or {name, description, instructions}
            data = json.loads(p.read_text(encoding="utf-8"))
            g = data.get("gizmo", data) if isinstance(data, dict) else {}
            disp = g.get("display", g) if isinstance(g, dict) else {}
            name = slugify(disp.get("name") or g.get("name") or p.stem)
            desc = disp.get("description") or g.get("description") or ""
            instr = g.get("instructions") or g.get("prompt") or g.get("system_prompt") or ""
            starters = disp.get("prompt_starters") or g.get("conversation_starters") or []
            d = tmp / name; d.mkdir()
            md = f"# {disp.get('name') or name}\n\n{instr.strip()}\n"
            if starters:
                md += "\n## Example requests\n" + "".join(f"- {s}\n" for s in starters)
            (d / "SKILL.md").write_text(dump_frontmatter({"name": name, "description": desc}) + "\n" + md, encoding="utf-8")
            return [d], {"kind": "chatgpt", "path": str(p.resolve()), "ref": str(p.resolve())}
        if p.suffix.lower() in (".md", ".txt"):  # bare instructions file
            fm, body = parse_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
            name = slugify(fm.get("name") or p.stem)
            d = tmp / name; d.mkdir()
            if not fm:
                title = next((ln.lstrip("# ").strip() for ln in body.splitlines() if ln.startswith("#")), name)
                fm = {"name": name, "description": title}
            (d / "SKILL.md").write_text(dump_frontmatter(fm) + "\n" + body.lstrip("\n"), encoding="utf-8")
            return [d], {"kind": detect_kind(p, "markdown"), "path": str(p.resolve()), "ref": str(p.resolve())}
        raise SystemExit(f"{p}: don't know how to import this file type")
    m = re.match(r"^(?:https?://|git@)?(?:www\.)?(github\.com|gitlab\.com|codeberg\.org)[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?(?:/(?:tree|blob|-/tree)/([^/]+)(?:/(.*))?)?/?$", src)
    if m and not src.endswith("SKILL.md"):
        host, owner, repo, ref, sub = m.groups()
        url = f"https://{host}/{owner}/{repo}.git"
        dest = tmp / "git"
        cmd = ["git", "clone", "--depth", "1", "--quiet"] + (["--branch", ref] if ref else []) + [url, str(dest)]
        log(f"cloning {url}" + (f" @{ref}" if ref else "") + " …")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if r.returncode:
            raise SystemExit(f"git clone failed: {r.stderr.strip()[:400]}")
        base = dest / sub if sub else dest
        if not base.exists():
            raise SystemExit(f"{sub}: not in {owner}/{repo}")
        dirs = [base] if is_skill_dir(base) else sorted({md.parent for md in base.rglob("SKILL.md") if ".git" not in md.parts})
        kind = "claude" if (dest / ".claude-plugin").exists() or "claude" in repo.lower() else "agentskills"
        return dirs, {"kind": kind, "path": str(base), "ref": src, "author": owner, "repo_id": f"{owner}/{repo}"}
    return [], {"kind": "hub", "ref": src}


def github_materialise(identifier: str, tmp: Path) -> tuple[list[Path], dict]:
    """Fetch a hub-style identifier straight from GitHub with git (no REST API, so no rate limit):
    skills-sh/<owner>/<repo>/<skill>  → any folder named <skill> in the repo
    <owner>/<repo>/<path/to/skill>     → that path"""
    m = re.match(r"^(?:skills-sh/|github:)?([\w.-]+)/([\w.-]+)/(.+?)/?$", identifier)
    if not m or identifier.startswith(("official/", "http")):
        return [], {}
    owner, repo, rest = m.groups()
    by_name = identifier.startswith("skills-sh/")
    url = f"https://github.com/{owner}/{repo}.git"
    dest = GIT_CACHE / f"{owner}__{repo}"          # one blobless clone per repo, reused across imports (24 h)
    fresh = dest.exists() and (dest / ".git").exists() and (dt.datetime.now().timestamp() - (dest / ".git").stat().st_mtime) < 86400
    if not fresh:
        shutil.rmtree(dest, ignore_errors=True); dest.parent.mkdir(parents=True, exist_ok=True)
        log(f"  git clone {url} …")
        r = subprocess.run(["git", "clone", "--depth", "1", "--filter=blob:none", "--no-checkout", "--quiet", url, str(dest)],
                           capture_output=True, text=True, timeout=600)
        if r.returncode:
            log(f"  ✗ clone failed: {r.stderr.strip()[:300]}")
            return [], {}
        subprocess.run(["git", "-C", str(dest), "sparse-checkout", "init", "--no-cone"], capture_output=True, text=True, timeout=60)
        subprocess.run(["git", "-C", str(dest), "sparse-checkout", "set", "--no-cone", "/SKILL.md"], capture_output=True, text=True, timeout=60)
    pats = [f"{rest.rsplit('/', 1)[-1]}/"] if by_name else [f"/{rest}/", f"/{rest}"]
    subprocess.run(["git", "-C", str(dest), "sparse-checkout", "add", *pats], capture_output=True, text=True, timeout=120)
    r = subprocess.run(["git", "-C", str(dest), "checkout", "--quiet"], capture_output=True, text=True, timeout=600)
    if r.returncode:
        log(f"  ✗ checkout failed: {r.stderr.strip()[:300]}")
        return [], {}
    want = rest.rsplit("/", 1)[-1]
    cands = [md.parent for md in dest.rglob("SKILL.md") if ".git" not in md.parts]
    if by_name:
        cands = [d for d in cands if d.name == want or slugify(read_skill(d)[0].get("name") or "") == want]
        cands.sort(key=lambda d: (any(x in d.parts for x in ("examples", "test", "tests", "fixtures", "node_modules")), len(d.parts)))
    else:
        cands = [d for d in cands if str(d.relative_to(dest)).rstrip("/") == rest.rstrip("/")] or [d for d in cands if d.name == want]
    if not cands and by_name:
        # skills.sh names come from the frontmatter `name:`, which needn't match the folder: index every SKILL.md
        ls = subprocess.run(["git", "-C", str(dest), "ls-tree", "-r", "--name-only", "HEAD"], capture_output=True, text=True, timeout=120).stdout
        mds = [x for x in ls.splitlines() if x.endswith("/SKILL.md") or x == "SKILL.md"]
        subprocess.run(["git", "-C", str(dest), "sparse-checkout", "add", *[f"/{x}" for x in mds]], capture_output=True, text=True, timeout=120)
        subprocess.run(["git", "-C", str(dest), "checkout", "--quiet"], capture_output=True, text=True, timeout=600)
        hit = [x for x in mds if slugify(read_skill((dest / x).parent)[0].get("name") or "") == want] if mds else []
        hit.sort(key=lambda x: (any(y in x.split("/") for y in ("examples", "test", "tests", "fixtures")), x.count("/")))
        if hit:
            sub = str(Path(hit[0]).parent)
            subprocess.run(["git", "-C", str(dest), "sparse-checkout", "add", f"/{sub}/"], capture_output=True, text=True, timeout=120)
            subprocess.run(["git", "-C", str(dest), "checkout", "--quiet"], capture_output=True, text=True, timeout=600)
            cands = [dest / sub]
    if not cands:
        log(f"  ✗ no SKILL.md for '{want}' in {owner}/{repo}")
        return [], {}
    return cands[:1], {"kind": "github", "ref": identifier, "path": str(cands[0]), "author": owner, "repo_id": f"{owner}/{repo}",
                       "repo": f"https://github.com/{owner}/{repo}"}


def dest_for(name: str, category: str) -> Path:
    return SKILLS / category / name if category else SKILLS / name


def import_local(skill_dir: Path, info: dict, args) -> dict | None:
    fm, body = read_skill(skill_dir)
    name = slugify(args.name or fm.get("name") or skill_dir.name)
    led = ledger()
    taken = [s for s in find_installed(name) if s["source"] != "ported"]
    if taken and not args.force:
        log(f"  ✗ {name}: a {taken[0]['source']} skill of that name already exists ({taken[0]['path']}); use --name to rename or --force")
        return None
    src_hash = content_hash(skill_dir)
    prev = led["skills"].get(name)
    if prev and prev.get("source_hash") == src_hash and not args.force:
        log(f"  = {name}: unchanged since last import ({prev.get('imported_at')})")
        return prev
    stage = Path(tempfile.mkdtemp(prefix="skillport-stage-")) / name
    shutil.copytree(skill_dir, stage, symlinks=False, ignore=shutil.ignore_patterns(".git", "__pycache__", "node_modules", ".DS_Store"))
    rep = hermesize(stage, source={**info, "hash": src_hash}, use_llm=not args.no_llm, category=args.category, name_override=name)
    ok, report = scan(stage, info.get("repo_id") or "local", args.force)
    if args.verbose or not ok:
        log("  " + report.replace("\n", "\n  "))
    if not ok:
        shutil.rmtree(stage.parent, ignore_errors=True)
        log(f"  ✗ {name}: blocked by skills-guard (use --force to override)")
        return None
    dest = dest_for(name, rep["category"])
    if args.dry_run:
        log(f"  ~ {name}: would install to {dest.relative_to(SKILLS)}  [{rep['description']}]  ({rep['how']}; tools {', '.join(rep['tool_map']) or '—'})")
        shutil.rmtree(stage.parent, ignore_errors=True)
        return rep
    if prev and prev.get("install_path") and Path(prev["install_path"]).exists() and Path(prev["install_path"]) != dest:
        shutil.rmtree(prev["install_path"], ignore_errors=True)
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(stage), str(dest))
    shutil.rmtree(stage.parent, ignore_errors=True)
    entry = {"name": name, "kind": info.get("kind"), "source": info.get("ref"), "source_path": info.get("path"),
             "source_hash": src_hash, "install_path": str(dest), "category": rep["category"], "imported_at": now(),
             "description": rep["description"], "description_how": rep["how"], "tool_map": rep["tool_map"]}
    led["skills"][name] = entry
    save_ledger(led)
    log(f"  ✓ {name} → {dest.relative_to(SKILLS)}  [{rep['description']}]  ({rep['how']}; tools {', '.join(rep['tool_map']) or '—'})")
    return entry


_HUB_LIMITED = [False]   # set once the hub reports GitHub's API rate limit; later ids skip straight to git


def import_hub(identifier: str, args) -> dict | None:
    cmd = [str(HERMES), "skills", "install", identifier, "--yes"] + (["--force"] if args.force else []) \
        + (["--category", args.category] if args.category else []) + (["--name", args.name] if args.name else [])
    if args.dry_run:
        log(f"  ~ would run: {' '.join(cmd[1:])}")
        return None
    log(f"  hub install {identifier} …")
    before = {k: v.get("install_path") for k, v in hub_installed().items()}
    r = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, "HERMES_HOME": str(HERMES_HOME), "NO_COLOR": "1", "TERM": "dumb"}, timeout=900)
    text = re.sub(r"\x1b\[[0-9;]*m", "", (r.stdout or "") + (r.stderr or ""))
    if "rate limit" in text.lower():
        _HUB_LIMITED[0] = True
    if args.verbose:
        log("  " + text.strip().replace("\n", "\n  "))
    after = hub_installed()
    new = [k for k, v in after.items() if before.get(k) != v.get("install_path") or k not in before]
    if not new and "already installed" in text:
        log(f"  = {identifier}: already installed via the hub (use --force to reinstall)")
        return {"name": identifier, "existing": True}
    if not new:
        tail = "" if args.verbose else "\n  " + "\n  ".join([ln for ln in text.strip().splitlines() if ln.strip()][-6:])
        log(f"  ✗ {identifier}: nothing installed via the hub{tail}")
        return None
    out = None
    for k in new:
        d = SKILLS / after[k]["install_path"]
        if not is_skill_dir(d):
            continue
        rep = hermesize(d, source={"kind": "hub", "ref": identifier, "path": str(d), "author": identifier.split("/")[1] if identifier.count("/") >= 2 else None},
                        use_llm=not args.no_llm, category=args.category, name_override=None)
        d = relocate_hub(k, d, rep["category"])
        log(f"  ✓ {k} → {d.relative_to(SKILLS)}  [{rep['description']}]  ({rep['how']}; tools {', '.join(rep['tool_map']) or '—'})")
        out = {"name": k, "install_path": str(d), **rep}
    return out


def relocate_hub(name: str, d: Path, category: str) -> Path:
    """Hub installs without --category land flat in skills/<name>; move them under the inferred
    category and keep .hub/lock.json's install_path in sync (that's what `hermes skills uninstall` reads)."""
    if not category or d.parent != SKILLS:
        return d
    dest = SKILLS / category / d.name
    if dest.exists():
        return d
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(d), str(dest))
    lock = load_json(LOCK, {"version": 1, "installed": {}})
    if name in lock.get("installed", {}):
        lock["installed"][name]["install_path"] = dest.relative_to(SKILLS).as_posix()
        LOCK.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    return dest


def cmd_tidy(args):
    """Move flat hub installs (skills/<name>) under their inferred category."""
    moved = 0
    for name, e in hub_installed().items():
        d = SKILLS / e.get("install_path", "")
        if d.parent != SKILLS or not is_skill_dir(d):
            continue
        fm, _ = read_skill(d)
        hm = ((fm.get("metadata") or {}).get("hermes") or {})
        cat = hm.get("category") or infer_category(name, str(fm.get("description") or ""), hm.get("tags") or [])
        if cat and cat != "ported" or (cat == "ported"):
            nd = relocate_hub(name, d, cat)
            if nd != d:
                moved += 1
                log(f"  {name}: → {nd.relative_to(SKILLS)}")
    log(f"{moved} skill(s) moved")


# --------------------------------------------------------------------------- bundles
BUNDLES: dict[str, dict] = {
    "reverse-engineering": {
        "why": "binary triage, Ghidra/radare2 workflows, CTF-style RE (authorized use)",
        "items": ["skills-sh/mohitmishra786/low-level-dev-skills/reverse-engineering",
                  "skills-sh/sickn33/agentic-awesome-skills/reverse-engineer"]},
    "web-design": {
        "why": "frontend design taste + landing pages + UI review (pairs with bundled claude-design / popular-web-designs)",
        "items": ["official/creative/impeccable", "anthropics/skills/skills/frontend-design",
                  "skills-sh/vercel-labs/agent-skills/web-design-guidelines", "official/web-development/scrollcraft",
                  "/mnt/data/shapecut/.claude/skills/landing-page-design", "/mnt/data/shapecut/.claude/skills/design-to-motion"]},
    "essentials": {
        "why": "dramatic day-to-day improvements: browser testing, structural code search, MCP building, code wikis, plan interviews",
        "items": ["anthropics/skills/skills/webapp-testing", "official/software-development/ast-grep",
                  "anthropics/skills/skills/mcp-builder", "official/software-development/code-wiki",
                  "official/software-development/grill-me", "official/software-development/rest-graphql-debug",
                  "official/creative/excalidraw", "official/devops/docker-management"]},
    "skill-sets-top": {
        "why": "top of skill-sets.md/skills (Oct 2026) minus cloud-account-only (RunComfy, inference.sh, Azure, Lark) and deprecated entries",
        "items": ['skills-sh/mattpocock/skills/grill-with-docs',
                  'skills-sh/mattpocock/skills/improve-codebase-architecture',
                  'skills-sh/mattpocock/skills/tdd',
                  'skills-sh/mattpocock/skills/setup-matt-pocock-skills',
                  'skills-sh/mattpocock/skills/handoff',
                  'skills-sh/mattpocock/skills/triage',
                  'skills-sh/mattpocock/skills/prototype',
                  'skills-sh/mattpocock/skills/grilling',
                  'skills-sh/mattpocock/skills/domain-modeling',
                  'skills-sh/mattpocock/skills/teach',
                  'skills-sh/mattpocock/skills/codebase-design',
                  'skills-sh/mattpocock/skills/diagnosing-bugs',
                  'skills-sh/mattpocock/skills/code-review',
                  'skills-sh/mattpocock/skills/implement',
                  'skills-sh/mattpocock/skills/ask-matt',
                  'skills-sh/mattpocock/skills/wayfinder',
                  'skills-sh/mattpocock/skills/to-spec',
                  'skills-sh/mattpocock/skills/to-tickets',
                  'skills-sh/mattpocock/skills/research',
                  'skills-sh/mattpocock/skills/wizard',
                  'skills-sh/mattpocock/skills/to-questionnaire',
                  'skills-sh/mattpocock/skills/setup-pre-commit',
                  'skills-sh/mattpocock/skills/writing-beats',
                  'skills-sh/mattpocock/skills/writing-shape',
                  'skills-sh/mattpocock/skills/writing-fragments',
                  'skills-sh/mattpocock/skills/writing-for-agents',
                  'skills-sh/mattpocock/skills/wait-what',
                  'skills-sh/mattpocock/skills/loop-me',
                  'skills-sh/mattpocock/skills/scaffold-exercises',
                  'skills-sh/vercel-labs/skills/find-skills',
                  'skills-sh/vercel-labs/agent-browser/agent-browser',
                  'skills-sh/vercel-labs/agent-skills/vercel-react-best-practices',
                  'skills-sh/vercel-labs/agent-skills/vercel-composition-patterns',
                  'skills-sh/heygen-com/hyperframes/hyperframes',
                  'skills-sh/heygen-com/hyperframes/hyperframes-cli',
                  'skills-sh/heygen-com/hyperframes/hyperframes-core',
                  'skills-sh/heygen-com/hyperframes/hyperframes-animation',
                  'skills-sh/heygen-com/hyperframes/hyperframes-creative',
                  'skills-sh/heygen-com/hyperframes/hyperframes-registry',
                  'skills-sh/heygen-com/hyperframes/hyperframes-keyframes',
                  'skills-sh/heygen-com/hyperframes/media-use',
                  'skills-sh/heygen-com/hyperframes/general-video',
                  'skills-sh/heygen-com/hyperframes/motion-graphics',
                  'skills-sh/heygen-com/hyperframes/faceless-explainer',
                  'skills-sh/heygen-com/hyperframes/product-launch-video',
                  'skills-sh/remotion-dev/skills/remotion-best-practices',
                  'skills-sh/leonxlnx/taste-skill/design-taste-frontend',
                  'skills-sh/leonxlnx/taste-skill/high-end-visual-design',
                  'skills-sh/leonxlnx/taste-skill/minimalist-ui',
                  'skills-sh/leonxlnx/taste-skill/industrial-brutalist-ui',
                  'skills-sh/leonxlnx/taste-skill/full-output-enforcement',
                  'skills-sh/leonxlnx/taste-skill/gpt-taste',
                  'skills-sh/emilkowalski/skills/emil-design-eng',
                  'skills-sh/juliusbrussee/caveman/caveman',
                  'skills-sh/juliusbrussee/caveman/caveman-commit',
                  'skills-sh/juliusbrussee/caveman/caveman-review',
                  'skills-sh/juliusbrussee/caveman/caveman-compress',
                  'skills-sh/obra/superpowers/brainstorming',
                  'skills-sh/supabase/agent-skills/supabase-postgres-best-practices',
                  'skills-sh/prisma/skills/prisma-client-api',
                  'skills-sh/prisma/skills/prisma-cli',
                  'skills-sh/anthropics/skills/skill-creator',
                  'skills-sh/lllllllama/rigorpilot-skills/repo-intake-and-plan',
                  'skills-sh/lllllllama/rigorpilot-skills/env-and-assets-bootstrap',
                  'skills-sh/lllllllama/rigorpilot-skills/minimal-run-and-audit',
                  'skills-sh/lllllllama/rigorpilot-skills/paper-context-resolver']},
    "security": {
        "why": "authorized web pentest, OSS supply-chain forensics, passive domain recon",
        "items": ["official/security/web-pentest", "official/security/oss-forensics", "official/research/domain-intel"]},
}


def cmd_sources(args):
    rows = scan_sources(args.path, include_all=args.all)
    if args.json:
        print(json.dumps(rows, indent=2)); return
    if not rows:
        log("no local skills found"); return
    rows.sort(key=lambda r: (r["kind"], r["name"]))
    w = max(len(r["name"]) for r in rows)
    for r in rows:
        mark = {"installed": "✓", "name-taken": "≠", "new": " "}[r["status"]]
        log(f"{mark} {r['name']:<{w}}  {r['kind']:<10} {r['description'][:70]:<70}  {r['path']}")
    log(f"\n{len(rows)} skills · ✓ already ported · ≠ name used by a Hermes skill (import with --name)")


def cmd_search(args):
    q = " ".join(args.query)
    local = [r for r in scan_sources() if re.search(re.escape(q), r["name"] + " " + r["description"], re.I)]
    remote: list[dict] = []
    if not args.local:
        cmd = [str(HERMES), "skills", "search", q, "--json", "--limit", str(args.limit)] + (["--source", args.source] if args.source else [])
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=args.timeout,
                               env={**os.environ, "HERMES_HOME": str(HERMES_HOME), "NO_COLOR": "1"})
            m = re.search(r"\[.*\]", r.stdout, re.S)
            remote = json.loads(m.group(0)) if m else []
        except Exception as e:
            log(f"(registry search failed: {e})")
    if args.json:
        print(json.dumps({"local": local, "remote": remote}, indent=2)); return
    for r in local:
        log(f"local   {r['kind']:<10} {r['name']:<34} {r['description'][:60]:<60}  {r['path']}")
    for r in remote:
        log(f"{(r.get('source') or '')[:7]:<7} {(r.get('trust_level') or '')[:10]:<10} {r.get('name', '')[:34]:<34} {(r.get('description') or '')[:60]:<60}  {r.get('identifier')}")
    if not local and not remote:
        log("nothing found")


def cmd_import(args):
    done = 0
    for src in args.source:
        log(f"{src}")
        tmp = Path(tempfile.mkdtemp(prefix="skillport-"))
        try:
            dirs, info = materialise(src, tmp)
            if info.get("kind") == "hub" and not dirs:
                via = getattr(args, "via", "auto")
                if via == "hub" or (via == "auto" and not _HUB_LIMITED[0]):
                    if import_hub(src, args):
                        done += 1
                        continue
                dirs, info = github_materialise(src, tmp)
                if not dirs and src.startswith("official/"):           # upstream-maintained official skills fetch from GitHub too;
                    local = HERMES_AGENT / "optional-skills" / src.removeprefix("official/")   # the repo checkout has a copy
                    if is_skill_dir(local):
                        dirs, info = [local], {"kind": "hermes-optional", "ref": src, "path": str(local), "repo_id": "official", "author": "Nous Research"}
                        if not args.category:
                            args = argparse.Namespace(**{**vars(args), "category": "/".join(src.split("/")[1:-1])})
                if not dirs:
                    continue
                log(f"  falling back to {'the local optional-skills copy' if info.get('kind') == 'hermes-optional' else 'a direct git fetch'}")
            if len(dirs) > 1 and args.name:
                raise SystemExit("--name only makes sense for a single skill")
            if len(dirs) > 1:
                log(f"  {len(dirs)} skills found")
            for d in dirs:
                done += 1 if import_local(d, info, args) else 0
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    log(f"\n{done} skill(s) {'checked' if args.dry_run else 'imported'}. Hermes picks them up on its next run (no restart needed).")
    if done and not args.dry_run:
        cmd_doctor(args)


def cmd_list(args):
    cat = installed_catalog()
    if args.json:
        print(json.dumps(cat, indent=2)); return
    rows = [s for s in cat if args.all or s["source"] in ("ported", "hub")]
    for s in sorted(rows, key=lambda s: (s["source"], s["category"], s["name"])):
        flag = "" if s["enabled"] else "  (disabled)"
        log(f"{s['source']:<7} {s['category'] + '/' if s['category'] else '':<28}{s['name']:<32} {s['short']}{flag}")
    log(f"\n{len(rows)} skills" + ("" if args.all else " (ported + hub; --all for everything)"))


def cmd_check(args):
    led = ledger()["skills"]
    changed = 0
    for name, e in sorted(led.items()):
        sp = Path(e.get("source_path") or "")
        if e.get("kind") in ("github", "zip", "hub") or not sp.exists():
            if args.verbose:
                log(f"- {name}: {e.get('kind')} source ({e.get('source')}) — use `skillport update {name}` to refetch")
            continue
        h = content_hash(sp)
        if h != e.get("source_hash"):
            changed += 1
            log(f"! {name}: source changed ({sp})")
        elif args.verbose:
            log(f"= {name}: up to date")
    log(f"{changed} ported skill(s) have newer sources" + ("; run `skillport update`" if changed else ""))


def cmd_update(args):
    led = ledger()["skills"]
    names = args.name or sorted(led)
    args.source, args.force = [], args.force
    for n in names:
        e = led.get(n)
        if not e:
            log(f"{n}: not a ported skill"); continue
        sp = Path(e.get("source_path") or "")
        if sp.exists() and e.get("kind") not in ("github", "zip"):
            ns = argparse.Namespace(**{**vars(args), "name": n, "category": e.get("category")})
            log(n)
            import_local(sp, {"kind": e.get("kind"), "ref": e.get("source"), "path": str(sp)}, ns)
        elif e.get("source"):
            ns = argparse.Namespace(**{**vars(args), "source": [e["source"]], "name": None, "category": e.get("category"), "force": True})
            cmd_import(ns)
        else:
            log(f"{n}: source gone ({sp})")


def cmd_remove(args):
    led = ledger()
    for n in args.name:
        e = led["skills"].get(n)
        if e:
            p = Path(e["install_path"])
            if p.exists():
                shutil.rmtree(p)
            del led["skills"][n]
            save_ledger(led)
            log(f"removed ported skill {n} ({p})")
            continue
        if n in hub_installed():
            r = subprocess.run([str(HERMES), "skills", "uninstall", n], capture_output=True, text=True,
                               env={**os.environ, "HERMES_HOME": str(HERMES_HOME), "NO_COLOR": "1"})
            log(re.sub(r"\x1b\[[0-9;]*m", "", r.stdout + r.stderr).strip() or f"uninstalled {n}")
            continue
        log(f"{n}: not ported/hub-installed (builtin and local-agent skills are left alone)")


def cmd_bundles(args):
    if args.json:
        print(json.dumps(BUNDLES, indent=2)); return
    for k, b in BUNDLES.items():
        log(f"{k:<20} {b['why']}")
        for it in b["items"]:
            log(f"    {it}")


def cmd_bundle(args):
    for b in args.bundle:
        if b not in BUNDLES:
            raise SystemExit(f"unknown bundle {b}; see `skillport bundles`")
        log(f"== bundle {b}: {BUNDLES[b]['why']}")
        ns = argparse.Namespace(**{**vars(args), "source": BUNDLES[b]["items"], "name": None, "category": None})
        cmd_import(ns)


def cmd_doctor(args):
    """Health checks for the skills tree as Hermes sees it."""
    problems = 0
    stray = [md for md in SKILLS.rglob("SKILL.md") if any(part.startswith(".") and part not in (".hub",) for part in md.relative_to(SKILLS).parts[:-1])]
    if stray:
        problems += 1
        log(f"✗ {len(stray)} SKILL.md file(s) under hidden folders — Hermes indexes these too (e.g. {stray[0].relative_to(SKILLS)})")
    cat = installed_catalog()
    names: dict[str, list[str]] = {}
    for c in cat:
        names.setdefault(c["name"], []).append(c["path"])
    dups = {n: ps for n, ps in names.items() if len(ps) > 1}
    if dups:
        problems += 1
        log(f"✗ duplicate skill names (skill_view needs a path for these): " + ", ".join(f"{n} ×{len(ps)}" for n, ps in list(dups.items())[:10]))
    long_desc = [c["name"] for c in cat if c["source"] in ("ported", "hub") and len(c["description"]) > DESC_LIMIT]
    if long_desc:
        log(f"! {len(long_desc)} imported skill(s) have descriptions > {DESC_LIMIT} chars (truncated in the index): {', '.join(long_desc[:8])}")
    size = sum(f.stat().st_size for f in SKILLS.rglob("*") if f.is_file())
    if size > 200 * 2**20:
        problems += 1
    log(f"{'✗' if size > 200 * 2**20 else '✓'} skills dir {size / 2**20:.0f} MiB (bind-mounted read-only into every sandbox)")
    enabled = [c for c in cat if c["enabled"]]
    log(f"✓ {len(cat)} skills, {len(enabled)} enabled; index ≈ {sum(len(c['name']) + len(c['short']) + 8 for c in enabled)} chars")
    if problems == 0:
        log("no problems found")
    return problems


def cmd_hermesize(args):
    for d in args.dir:
        p = Path(d).expanduser()
        if not is_skill_dir(p):
            log(f"{p}: no SKILL.md"); continue
        rep = hermesize(p, source={"kind": "in-place", "ref": str(p)}, use_llm=not args.no_llm, category=args.category)
        log(f"✓ {rep['name']}: [{rep['description']}] ({rep['how']}; tools {', '.join(rep['tool_map']) or '—'})")


def main(argv=None):
    ap = argparse.Namespace(description=__doc__.split("\n\n")[0])
    g = argparse.ArgumentParser(add_help=False)
    g.add_argument("--json", action="store_true", help="machine-readable output where supported")
    g.add_argument("-v", "--verbose", action="store_true")
    ap = argparse.ArgumentParser(prog="skillport", parents=[g], description=ap.description, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True, parser_class=lambda **kw: argparse.ArgumentParser(parents=[g], **kw))

    s = sub.add_parser("sources", help="scan this box for skills that could be ported"); s.set_defaults(fn=cmd_sources)
    s.add_argument("path", nargs="*", help="extra folders to scan"); s.add_argument("--all", action="store_true", help="recurse deeper")

    s = sub.add_parser("search", help="search registries (skills.sh, ClawHub, GitHub, official) and local sources"); s.set_defaults(fn=cmd_search)
    s.add_argument("query", nargs="+"); s.add_argument("--limit", type=int, default=12); s.add_argument("--source")
    s.add_argument("--local", action="store_true", help="local sources only"); s.add_argument("--timeout", type=int, default=120)

    def common(p):
        p.add_argument("--category", help="Hermes category folder (default: inferred)")
        p.add_argument("--name", help="rename the skill on import")
        p.add_argument("--dry-run", action="store_true"); p.add_argument("--force", action="store_true", help="overwrite / ignore guard verdict")
        p.add_argument("--no-llm", action="store_true", help="don't ask the local model for the 60-char description")
        p.add_argument("--via", choices=["auto", "hub", "git"], default="auto", help="hub ids: Hermes hub first (auto), only hub, or straight to git")
    s = sub.add_parser("import", help="import local folder/zip/.json/.md, git URL, or hub identifier"); s.set_defaults(fn=cmd_import)
    s.add_argument("source", nargs="+"); common(s)
    s = sub.add_parser("list", help="installed skills (ported + hub; --all for every skill)"); s.set_defaults(fn=cmd_list); s.add_argument("--all", action="store_true")
    s = sub.add_parser("check", help="which ported skills have changed sources"); s.set_defaults(fn=cmd_check)
    s = sub.add_parser("update", help="re-import ported skills from their sources"); s.set_defaults(fn=cmd_update); s.add_argument("name", nargs="*"); common(s)
    s = sub.add_parser("remove", help="remove ported or hub-installed skills"); s.set_defaults(fn=cmd_remove); s.add_argument("name", nargs="+")
    s = sub.add_parser("bundles", help="list curated bundles"); s.set_defaults(fn=cmd_bundles)
    s = sub.add_parser("bundle", help="install curated bundle(s)"); s.set_defaults(fn=cmd_bundle); s.add_argument("bundle", nargs="+"); common(s)
    s = sub.add_parser("doctor", help="check the skills tree for duplicates, stray SKILL.md files, size"); s.set_defaults(fn=cmd_doctor)
    s = sub.add_parser("tidy", help="move flat hub installs under their inferred category"); s.set_defaults(fn=cmd_tidy)
    s = sub.add_parser("hermesize", help="(re)apply Hermes conventions to skill folders in place"); s.set_defaults(fn=cmd_hermesize)
    s.add_argument("dir", nargs="+"); s.add_argument("--category"); s.add_argument("--no-llm", action="store_true")

    args = ap.parse_args(argv)
    for k, v in (("name", None), ("category", None), ("dry_run", False), ("force", False), ("no_llm", False), ("via", "auto")):
        if not hasattr(args, k):
            setattr(args, k, v)
    SKILLS.mkdir(parents=True, exist_ok=True)
    args.fn(args)


if __name__ == "__main__":
    main()
