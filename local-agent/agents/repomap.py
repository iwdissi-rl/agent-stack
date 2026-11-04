"""Repo map: a compact symbol-level index of a directory tree (Aider-style), for agents to navigate a codebase
without reading whole files. Stdlib only. Python via `ast`; JS/TS/Go/Rust/Java/C/C#/Ruby/PHP/shell via regexes.

    python3 agents/repomap.py /mnt/data/local-agent/workspace [max_chars]
"""
from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path

SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
             ".ruff_cache", "dist", "build", ".next", ".cache", "uploads", ".tox", "target", ".idea", ".vscode", ".crew"}
EXTS = {".py", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".c", ".h", ".cc", ".cpp",
        ".hpp", ".cs", ".rb", ".php", ".sh", ".bash", ".sql", ".md", ".toml", ".yaml", ".yml", ".json", ".html", ".css"}
CODE_EXTS = EXTS - {".md", ".toml", ".yaml", ".yml", ".json", ".html", ".css", ".sql"}
MAX_FILE_BYTES = 1_500_000

_RX = {
    "js": re.compile(r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?(?:function\s*\*?\s*(?P<fn>[A-Za-z_$][\w$]*)|class\s+(?P<cls>[A-Za-z_$][\w$]*)|(?:const|let|var)\s+(?P<var>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>)", re.M),
    "go": re.compile(r"^func\s+(?:\([^)]*\)\s*)?(?P<fn>[A-Za-z_]\w*)|^type\s+(?P<cls>[A-Za-z_]\w*)\s+(?:struct|interface)", re.M),
    "rs": re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:fn\s+(?P<fn>[A-Za-z_]\w*)|(?:struct|enum|trait)\s+(?P<cls>[A-Za-z_]\w*)|impl(?:<[^>]*>)?\s+(?:[\w:]+\s+for\s+)?(?P<impl>[A-Za-z_][\w:]*))", re.M),
    "java": re.compile(r"^\s*(?:public|private|protected|static|final|abstract|\s)*\s*(?:class|interface|enum|record)\s+(?P<cls>[A-Za-z_]\w*)|^\s*(?:public|private|protected|static|final|synchronized|\s)+[\w<>\[\],\s]+\s+(?P<fn>[a-z]\w*)\s*\([^)]*\)\s*(?:throws[^{]*)?\{", re.M),
    "c": re.compile(r"^(?:[A-Za-z_][\w\s\*]*?)\s+\*?(?P<fn>[A-Za-z_]\w*)\s*\([^;]*\)\s*\{|^(?:typedef\s+)?(?:struct|enum|union)\s+(?P<cls>[A-Za-z_]\w*)\s*\{", re.M),
    "rb": re.compile(r"^\s*(?:def\s+(?P<fn>[\w.?!]+)|(?:class|module)\s+(?P<cls>[A-Z]\w*))", re.M),
    "php": re.compile(r"^\s*(?:(?:public|private|protected|static|abstract|final)\s+)*function\s+(?P<fn>\w+)|^\s*(?:abstract\s+|final\s+)?(?:class|interface|trait)\s+(?P<cls>\w+)", re.M),
    "sh": re.compile(r"^\s*(?:function\s+)?(?P<fn>[A-Za-z_]\w*)\s*\(\)\s*\{", re.M),
}
_LANG = {".js": "js", ".mjs": "js", ".cjs": "js", ".jsx": "js", ".ts": "js", ".tsx": "js", ".go": "go", ".rs": "rs",
         ".java": "java", ".kt": "java", ".c": "c", ".h": "c", ".cc": "c", ".cpp": "c", ".hpp": "c", ".cs": "java",
         ".rb": "rb", ".php": "php", ".sh": "sh", ".bash": "sh"}


def _py_symbols(src: str) -> list[tuple[int, str]]:
    out, consts = [], []
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [(e.lineno or 0, f"!! syntax error: {e.msg}")]
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [a.arg for a in node.args.args]
            out.append((node.lineno, f"def {node.name}({', '.join(args)})"))
        elif isinstance(node, ast.ClassDef):
            bases = [getattr(b, "id", getattr(b, "attr", "?")) for b in node.bases]
            out.append((node.lineno, f"class {node.name}" + (f"({', '.join(bases)})" if bases else "")))
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    args = [a.arg for a in sub.args.args if a.arg not in ("self", "cls")]
                    out.append((sub.lineno, f"    .{sub.name}({', '.join(args)})"))
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            n = node.targets[0].id
            if n.isupper() or n in ("app", "router", "main", "__all__"):
                consts.append((node.lineno, f"{n} = …"))
    if len(consts) > 6:
        consts = consts[:6] + [(consts[6][0], f"… {len(consts) - 6} more module constants")]
    return sorted(out + consts)


def _rx_symbols(src: str, lang: str) -> list[tuple[int, str]]:
    out = []
    rx = _RX[lang]
    for m in rx.finditer(src):
        line = src.count("\n", 0, m.start()) + 1
        d = m.groupdict()
        if d.get("cls"):
            out.append((line, f"class {d['cls']}"))
        elif d.get("fn"):
            out.append((line, f"fn {d['fn']}"))
        elif d.get("var"):
            out.append((line, f"fn {d['var']}"))
        elif d.get("impl"):
            out.append((line, f"impl {d['impl']}"))
    return out


def _md_symbols(src: str) -> list[tuple[int, str]]:
    return [(i + 1, l.strip()[:80]) for i, l in enumerate(src.splitlines()) if l.startswith("#")][:30]


def file_symbols(path: Path) -> list[tuple[int, str]]:
    ext = path.suffix.lower()
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    if ext == ".py":
        return _py_symbols(src)
    if ext in _LANG:
        return _rx_symbols(src, _LANG[ext])
    if ext == ".md":
        return _md_symbols(src)
    return []


def repo_map(root: str | Path, max_chars: int = 12000, focus: str = "") -> str:
    """Tree + symbols, trimmed to max_chars. `focus` (substring) ranks matching paths first."""
    root = Path(root)
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".git"))
        for f in sorted(filenames):
            p = Path(dirpath) / f
            if p.suffix.lower() in EXTS and p.stat().st_size <= MAX_FILE_BYTES:
                files.append(p)
    if not files:
        return f"(no source files under {root})"
    def rank(p: Path) -> tuple:
        rel = str(p.relative_to(root))
        return (0 if focus and focus.lower() in rel.lower() else 1, 0 if p.suffix.lower() in CODE_EXTS else 1, rel.count("/"), rel)
    files.sort(key=rank)
    lines = [f"# repo map of {root} — {len(files)} files (symbols with line numbers; read exact ranges, not whole files)"]
    total = len(lines[0])
    for p in files:
        rel = str(p.relative_to(root))
        try:
            n = sum(1 for _ in open(p, "rb"))
        except Exception:
            n = 0
        syms = file_symbols(p)
        block = [f"\n{rel}  ({n} lines)"] + [f"  {ln:>5}: {s}" for ln, s in syms[:60]]
        if len(syms) > 60:
            block.append(f"  … {len(syms) - 60} more")
        text = "\n".join(block)
        if total + len(text) > max_chars:
            room = max_chars - total
            if room > 200:                      # fit a truncated block rather than dropping the file
                kept = [block[0]]
                for b in block[1:]:
                    if len("\n".join(kept)) + len(b) + 40 > room:
                        break
                    kept.append(b)
                kept.append(f"  … {len(block) - len(kept)} more symbols")
                lines.append("\n".join(kept))
                total += len("\n".join(kept))
            rest = len(files) - files.index(p) - (1 if room > 200 else 0)
            if rest > 0:
                lines.append(f"\n… {rest} more files omitted (raise max_chars or pass focus=)")
            break
        lines.append(text)
        total += len(text)
    return "\n".join(lines)


if __name__ == "__main__":
    print(repo_map(sys.argv[1] if len(sys.argv) > 1 else ".", int(sys.argv[2]) if len(sys.argv) > 2 else 12000,
                   sys.argv[3] if len(sys.argv) > 3 else ""))
