"""Role / mode library for the local-agent crew.

A *role* is a markdown file in agents/roles/<name>.md with a small front-matter block:

    ---
    name: coder
    class: engineering
    title: Implementation engineer
    summary: one line shown in the roles list
    when: one line on when a lead should pick this role
    max_turns: 120
    budget_s: 5400
    skills: sandbox-coding
    spawn: false
    ---
    <system prompt body>

A *mode* (agents/modes/<name>.md) is a brief appended to the lead's prompt. Both the HUD (hud/app.py)
and the crew MCP server (crew/crew_server.py) import this module; it depends on nothing but the stdlib.
"""
from __future__ import annotations

import re
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parent
ROLES_DIR = AGENTS_DIR / "roles"
MODES_DIR = AGENTS_DIR / "modes"

LEAD_ROLE = "orchestrator"
MODES = {  # mode -> (lead role, default skills, default max_turns, default run budget seconds)
    "chat": (None, [], 40, 0),
    "coding": (LEAD_ROLE, ["sandbox-coding"], 300, 12 * 3600),   # owner-adjustable up to 24 h in the composer
    "research": (LEAD_ROLE, ["deep-research"], 200, 4 * 3600),
    "data": (LEAD_ROLE, ["document-analysis"], 200, 4 * 3600),
}

_FM = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)


def _parse(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    meta: dict = {"name": path.stem, "class": "general", "title": path.stem, "summary": "", "when": "",
                  "max_turns": 120, "budget_s": 3600, "skills": [], "toolsets": [], "spawn": False}
    m = _FM.match(text)
    body = text
    if m:
        body = text[m.end():]
        for line in m.group(1).splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            k, v = k.strip(), v.strip()
            if k in ("max_turns", "budget_s"):
                meta[k] = int(v)
            elif k in ("skills", "toolsets"):
                meta[k] = [s.strip() for s in v.split(",") if s.strip()]
            elif k == "spawn":
                meta[k] = v.lower() in ("1", "true", "yes")
            else:
                meta[k] = v
    meta["body"] = body.strip()
    meta["path"] = str(path)
    return meta


def load_roles() -> dict[str, dict]:
    roles = {}
    for p in sorted(ROLES_DIR.glob("*.md")):
        if p.name.startswith("_"):
            continue
        r = _parse(p)
        roles[r["name"]] = r
    return roles


def role_catalog(roles: dict[str, dict] | None = None, exclude_lead: bool = True) -> list[dict]:
    """Compact listing for the model and the UI (no prompt bodies)."""
    roles = roles or load_roles()
    out = []
    for r in roles.values():
        if exclude_lead and r["name"] == LEAD_ROLE:
            continue
        out.append({k: r[k] for k in ("name", "class", "title", "summary", "when", "max_turns", "budget_s", "skills")})
    return sorted(out, key=lambda r: (r["class"], r["name"]))


def _common(name: str) -> str:
    p = ROLES_DIR / f"_{name}.md"
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


def catalog_markdown(roles: dict[str, dict] | None = None) -> str:
    lines = ["| role | class | use it for |", "|---|---|---|"]
    for r in role_catalog(roles):
        lines.append(f"| `{r['name']}` | {r['class']} | {r['summary']} — *{r['when']}* |")
    return "\n".join(lines)


def build_prompt(role: str | None, mode: str = "chat", depth: int = 0, roles: dict[str, dict] | None = None) -> str:
    """The ephemeral system prompt for one agent process.

    depth 0 + mode != chat -> the lead (orchestrator) with the mode brief and the crew reference.
    depth 0 + explicit role -> a specialist run directly by the owner (no crew spawning).
    depth >= 1               -> a crew member spawned by a lead.
    """
    roles = roles or load_roles()
    if not role:
        # plain chat: a short conversational brief + the dashboard's output formatting rules (tables, code, charts)
        chat = MODES_DIR / "chat.md"
        parts = [chat.read_text(encoding="utf-8").strip() if chat.exists() else "", _common("output_format")]
        return "\n\n".join(p for p in parts if p)
    r = roles[role]
    parts = [_common("preamble"), r["body"]]
    if role == LEAD_ROLE:
        if mode in MODES and (MODES_DIR / f"{mode}.md").exists():
            parts.append((MODES_DIR / f"{mode}.md").read_text(encoding="utf-8").strip())
        parts.append(_common("crew_tools"))
        parts.append(_common("output_format"))
        parts.append("## The crew roster\n\nRoles you can spawn right now (same model, different briefing; "
                     "details via the `roles` tool):\n\n" + catalog_markdown(roles))
    else:
        parts.append(_common("member_protocol"))
    return "\n\n".join(p for p in parts if p)


if __name__ == "__main__":  # quick check: python3 agents/roles.py [role] [mode]
    import json
    import sys

    rs = load_roles()
    if len(sys.argv) > 1:
        print(build_prompt(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "coding", depth=0 if sys.argv[1] == LEAD_ROLE else 1, roles=rs))
    else:
        print(json.dumps(role_catalog(rs, exclude_lead=False), indent=2))
        for n, r in rs.items():
            print(f"{n:14s} {len(r['body'].split()):6d} words")
