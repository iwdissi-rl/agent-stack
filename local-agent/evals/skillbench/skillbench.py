#!/usr/bin/env python3
"""skillbench — does a skill actually make the local agent better at a real task?

Five realistic tasks, each graded by a machine (hidden tests, an executable check, a headless
browser), never by the agent's own claims:

  design    build a landing page from a brief          → Playwright: structure, responsive, a11y/contrast,
                                                         console errors + a vision-model design score
  bugfix    fix a billing library from a support ticket → hidden pytest suite (20 tests, incl. a bug the
                                                         ticket doesn't mention) + credit for a reproducing test
  reverse   crack a stripped, randomised binary         → the recovered key is executed against the binary
  review    review the last commit of a small service   → recall of 5 planted defects (anchored to the code)
  browser   finish a JS signup wizard, report the code  → exact match with a per-run salted code

Each task runs under three conditions, so the effect of the skills is isolated:

  base      the skills installed by skillport are disabled (the stack as it was before)
  preload   the task's relevant skills are force-loaded with `--skills`
  auto      everything enabled, nothing preloaded — does the agent find and load the skill itself?
  router    everything enabled; the local decision model (decide/decide.py, Clef-Flash on :8183) reads the
            prompt and picks which skills to preload — the production path the HUD uses ("auto skills")

Runs use a private HERMES_HOME (bench-home/<condition>: same config + skills dir, no MCP servers, empty
memories) so they never touch the owner's sessions or memories, and secrets/hidden tests live in
~/.local/share/skillbench (outside every path the Docker sandbox can mount).

    .venv/bin/python skillbench.py run --label first [--tasks design,bugfix] [--conds base,preload,auto,router] [-j 2] [--reps 1]
    .venv/bin/python skillbench.py report <label>
    .venv/bin/python skillbench.py regrade <label>
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures as cf
import json
import os
import random
import re
import shutil
import sqlite3
import string
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path("/mnt/data/local-agent")
HERE = Path(__file__).resolve().parent
HERMES = ROOT / "hermes-agent/.venv/bin/hermes"
REAL_HOME = ROOT / "hermes-home"
BENCH_HOMES = HERE / "bench-home"
RESULTS = HERE / "results"
EVAL_RESULTS = ROOT / "evals/results"                 # HUD Evals page reads <label>/summary.json here
WS_HOST = ROOT / "workspace/bench"                    # = /workspace/bench in the sandbox
WS_SANDBOX = "/workspace/bench"
PRIVATE = Path.home() / ".local/share/skillbench/private"
SECRETS = Path.home() / ".local/share/skillbench/secrets"
SANDBOX_IMAGE = "nousresearch/hermes-sandbox:desktop"
GRADER_IMAGE = "local-agent-grader:latest"
LLM = "http://127.0.0.1:8181/v1/chat/completions"
CHROME = sorted(Path.home().glob(".cache/ms-playwright/chromium-*/chrome-linux*/chrome"))
SKILLPORT = ROOT / "bin/skillport"
_print_lock = threading.Lock()
sys.path.insert(0, str(ROOT / "decide"))
try:
    import decide as decider          # the System One skill router (needs la-decide + la-embed up)
except Exception as _e:               # pragma: no cover
    decider = None


def say(*a):
    with _print_lock:
        print(time.strftime("[%H:%M:%S]"), *a, flush=True)


# ============================================================================ task: design
DESIGN_BRIEF = """Build the marketing landing page for **Lumen Loop**, a solar-charged bike light startup
(USB-C fallback, 40 h runtime, auto-brightness, theft alarm; 3 tiers: Loop $49, Loop Pro $89, Fleet $299
for 5 lights; audience: urban commuters who ride at night).

Deliverable: ONE self-contained file `{dir}/site/index.html` (inline CSS/JS; it is graded OFFLINE, so
no CDNs, web fonts, or remote images — use CSS, inline SVG, or emoji). Required content: a hero with a
headline and a primary call to action, at least three feature blocks, the three pricing tiers, one
customer testimonial, an FAQ with at least three questions, and a footer.
It must look like a real, distinctive product site (not a template), work on phones (375 px wide) and
desktops, and be accessible (contrast, semantics, alt text). When done, reply with a short summary."""


def design_setup(d: Path, rng: random.Random) -> dict:
    (d / "site").mkdir(parents=True, exist_ok=True)
    return {}


DESIGN_JS = r"""
() => {
  const vw = window.innerWidth, doc = document.documentElement;
  const lum = (r,g,b) => { const f = (c) => { c /= 255; return c <= 0.03928 ? c/12.92 : Math.pow((c+0.055)/1.055, 2.4); }; return 0.2126*f(r)+0.7152*f(g)+0.0722*f(b); };
  const parse = (s) => { const m = s.match(/rgba?\(([^)]+)\)/); if (!m) return null; const p = m[1].split(/[ ,\/]+/).filter(Boolean).map(Number); return {r:p[0],g:p[1],b:p[2],a:p.length>3?p[3]:1}; };
  const bgOf = (el) => { let e = el; while (e) { const c = parse(getComputedStyle(e).backgroundColor); if (c && c.a > 0.5 && getComputedStyle(e).backgroundImage === 'none') return c; if (getComputedStyle(e).backgroundImage !== 'none') return null; e = e.parentElement; } return {r:255,g:255,b:255,a:1}; };
  let checked = 0, pass = 0, small = 0; const fails = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const seen = new Set();
  while (walker.nextNode()) {
    const t = walker.currentNode; if (!t.textContent.trim()) continue;
    const el = t.parentElement; if (!el || seen.has(el)) continue; seen.add(el);
    const cs = getComputedStyle(el); if (cs.visibility === 'hidden' || cs.display === 'none' || el.offsetWidth === 0) continue;
    if (parseFloat(cs.fontSize) < 12) small++;
    const fg = parse(cs.color), bg = bgOf(el); if (!fg || !bg) continue;
    const L1 = lum(fg.r,fg.g,fg.b), L2 = lum(bg.r,bg.g,bg.b); const ratio = (Math.max(L1,L2)+0.05)/(Math.min(L1,L2)+0.05);
    const large = parseFloat(cs.fontSize) >= 24 || (parseFloat(cs.fontSize) >= 18.66 && parseInt(cs.fontWeight) >= 700);
    checked++; if (ratio >= (large ? 3 : 4.5)) pass++; else if (fails.length < 5) fails.push(`${el.tagName}:${t.textContent.trim().slice(0,30)} ${ratio.toFixed(2)}`);
  }
  const imgs = [...document.querySelectorAll('img')]; const svgs = [...document.querySelectorAll('svg')];
  const named = (e) => (e.innerText || e.getAttribute('aria-label') || e.getAttribute('title') || e.querySelector('img[alt]:not([alt=""])') || '').toString().trim().length > 0;
  const ctrls = [...document.querySelectorAll('a[href],button')];
  const firstCta = ctrls.find((e) => { const r = e.getBoundingClientRect(); return r.top < window.innerHeight && r.width > 0; });
  const text = document.body.innerText;
  const prices = new Set((text.match(/\$\s?\d{2,3}/g) || []).map((s) => s.replace(/\s/g, '')));
  const faqQs = (text.match(/\?/g) || []).length;
  return {
    overflow: doc.scrollWidth - vw, lang: !!doc.getAttribute('lang'),
    viewport: !!document.querySelector('meta[name=viewport]'), h1: document.querySelectorAll('h1').length,
    landmarks: ['header','main','footer'].filter((t) => document.querySelector(t)).length,
    headings: document.querySelectorAll('h2,h3').length, sections: document.querySelectorAll('section').length,
    imgs: imgs.length, imgsAlt: imgs.filter((i) => i.hasAttribute('alt')).length, svgs: svgs.length,
    ctrls: ctrls.length, ctrlsNamed: ctrls.filter(named).length, ctaAboveFold: !!firstCta,
    contrastChecked: checked, contrastPass: pass, contrastFails: fails, smallText: small,
    prices: [...prices], has49: /\$\s?49\b/.test(text), has89: /\$\s?89\b/.test(text), has299: /\$\s?299\b/.test(text),
    faq: /faq|frequently asked|questions/i.test(text), faqQs, testimonial: /[“"].{20,}[”"]|testimonial|— [A-Z]/.test(text),
    fonts: [...new Set([...document.querySelectorAll('h1,h2,p,body')].map((e) => getComputedStyle(e).fontFamily))],
    cssVars: [...document.styleSheets].some((s) => { try { return [...s.cssRules].some((r) => /--[\w-]+\s*:/.test(r.cssText)); } catch { return false; } }),
    words: text.split(/\s+/).length,
  };
}
"""

VISION_PROMPT = """You are a strict senior product designer reviewing a landing page for "Lumen Loop", a solar bike light.
You get a desktop screenshot (top of page) and a phone screenshot. Score the VISUAL DESIGN 1-10:
visual hierarchy, typography, colour and contrast, spacing/alignment consistency, distinctiveness (does it look
like a real brand, not a generic AI template: centered-everything, purple gradients, emoji soup, card grid only),
and polish. 5 = competent but generic, 8 = would ship at a good startup, 10 = award-level.
Reply with JSON only: {"score": <1-10>, "why": "<one sentence>"}"""


def vision_score(shots: list[bytes]) -> tuple[float, str]:
    import httpx
    content = [{"type": "text", "text": VISION_PROMPT}] + [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(b).decode()}} for b in shots]
    scores, whys = [], []
    for _ in range(2):   # two samples, averaged: the judge is the same local model, keep it cheap but less noisy
        try:
            r = httpx.post(LLM, json={"model": "local", "messages": [{"role": "user", "content": content}], "max_tokens": 200,
                                      "temperature": 0.3, "chat_template_kwargs": {"enable_thinking": False}}, timeout=240, trust_env=False)
            t = r.json()["choices"][0]["message"]["content"]
            m = re.search(r"\{.*\}", t, re.S); j = json.loads(m.group(0))
            scores.append(float(j["score"])); whys.append(str(j.get("why", ""))[:160])
        except Exception as e:
            whys.append(f"judge error {e}")
    return (sum(scores) / len(scores) if scores else 0.0), " | ".join(whys)


def design_grade(d: Path, secret: dict, run: dict) -> tuple[float, dict]:
    from playwright.sync_api import sync_playwright
    page_path = d / "site/index.html"
    if not page_path.exists():
        return 0.0, {"note": "no site/index.html"}
    det: dict = {}
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=str(CHROME[-1]), args=["--no-sandbox", "--no-proxy-server"])
        shots = []
        for name, vp in (("desktop", {"width": 1440, "height": 900}), ("mobile", {"width": 375, "height": 812})):
            ctx = b.new_context(viewport=vp, device_scale_factor=1)
            remote = []
            ctx.route("**/*", lambda route: (route.continue_() if route.request.url.startswith(("file:", "data:", "blob:"))
                                             else (remote.append(route.request.url), route.abort())))
            p = ctx.new_page(); errs = []
            p.on("pageerror", lambda e: errs.append(str(e)[:120]))
            p.on("console", lambda m: errs.append(m.text[:120]) if m.type == "error" else None)
            p.goto(page_path.as_uri(), wait_until="load", timeout=30000); p.wait_for_timeout(800)
            det[name] = p.evaluate(DESIGN_JS); det[name]["errors"] = errs[:5]; det[name]["remote"] = remote[:5]
            shot = p.screenshot(full_page=False); shots.append(shot)
            (d / f"shot-{name}.png").write_bytes(shot)
            p.screenshot(path=str(d / f"full-{name}.png"), full_page=True)
            ctx.close()
        b.close()
    D, M = det["desktop"], det["mobile"]
    structure = sum([D["h1"] == 1, D["lang"], D["viewport"], D["landmarks"] == 3, D["ctaAboveFold"],
                     D["has49"] and D["has89"] and D["has299"], D["faq"] and D["faqQs"] >= 3, D["testimonial"],
                     D["headings"] >= 5]) / 9
    responsive = sum([M["overflow"] <= 1, D["overflow"] <= 1, M["smallText"] <= 2, M["ctaAboveFold"]]) / 4
    contrast = D["contrastPass"] / D["contrastChecked"] if D["contrastChecked"] else 0
    a11y = 0.6 * min(1.0, contrast / 0.95) + 0.2 * (D["imgsAlt"] == D["imgs"]) + 0.2 * (D["ctrlsNamed"] == D["ctrls"])
    clean = 1.0 if not (D["errors"] or M["errors"] or D["remote"]) else 0.0
    vis, why = vision_score(shots)
    # responsive/a11y/clean are easy on an empty page, so they only count in proportion to the content that exists
    score = 0.25 * structure + (0.15 * responsive + 0.2 * a11y + 0.05 * clean) * structure + 0.35 * (vis / 10)
    return score, {"structure": round(structure, 2), "responsive": round(responsive, 2), "a11y": round(a11y, 2),
                   "contrast": round(contrast, 3), "contrast_fails": D["contrastFails"], "clean": clean, "remote": D["remote"],
                   "errors": D["errors"] + M["errors"], "vision": vis, "vision_why": why, "fonts": D["fonts"][:3],
                   "note": f"struct {structure:.2f} resp {responsive:.2f} a11y {a11y:.2f} vis {vis:.1f}/10"}


# ============================================================================ task: bugfix
BUGFIX_PROMPT = """Support escalated ticket #4412 for our billing library. The project is in `{dir}/ledger-project`
(package `ledger/`, tests in `tests/`, the ticket text is in `REPORT.md`). Fix the root causes of every problem
in the ticket — and anything else that is wrong with the library's documented behaviour — without changing
the public function signatures. Python 3 and `pip install pytest` work in the sandbox.
When you're done, reply with what was wrong and how you verified each fix."""


def bugfix_setup(d: Path, rng: random.Random) -> dict:
    shutil.copytree(PRIVATE / "bugfix", d / "ledger-project")
    return {}


def _grader(mounts: list[tuple[str, str]], cmd: str, timeout: int = 300) -> str:
    args = ["docker", "run", "--rm", "--network", "none", "--user", f"{os.getuid()}:{os.getgid()}", "--entrypoint", "bash"]
    for h, c in mounts:
        args += ["-v", f"{h}:{c}"]
    r = subprocess.run(args + [GRADER_IMAGE, "-lc", cmd], capture_output=True, text=True, timeout=timeout)
    return (r.stdout + r.stderr).strip()


def _pytest_counts(out: str) -> tuple[int, int]:
    p = re.search(r"(\d+) passed", out); f = re.findall(r"(\d+) (?:failed|error)", out)
    return (int(p.group(1)) if p else 0), sum(map(int, f))


def bugfix_grade(d: Path, secret: dict, run: dict) -> tuple[float, dict]:
    proj = d / "ledger-project"
    if not (proj / "ledger").exists():
        return 0.0, {"note": "project missing"}
    with tempfile.TemporaryDirectory(prefix="sb-grade-") as tmp:
        t = Path(tmp); shutil.copytree(proj / "ledger", t / "fixed/ledger")
        shutil.copy(PRIVATE / "hidden/test_hidden_bugfix.py", t / "test_hidden.py")
        out = _grader([(str(t), "/g")], "cd /g && cp test_hidden.py fixed/ && cd fixed && python3 -m pytest -q -p no:cacheprovider test_hidden.py 2>&1 | tail -3")
        passed, failed = _pytest_counts(out)
        hidden = passed / max(1, passed + failed)
        # did the agent leave a test that reproduces a bug? (its tests must FAIL against the original code)
        shutil.copytree(PRIVATE / "bugfix/ledger", t / "orig/ledger")
        if (proj / "tests").exists():
            shutil.copytree(proj / "tests", t / "orig/tests")
        out2 = _grader([(str(t), "/g")], "cd /g/orig && python3 -m pytest -q -p no:cacheprovider tests 2>&1 | tail -3")
        _, repro_fail = _pytest_counts(out2)
        out3 = _grader([(str(t), "/g")], "rm -rf /g/fx && cp -r /g/fixed /g/fx && cp -r /g/orig/tests /g/fx/ 2>/dev/null; cd /g/fx && python3 -m pytest -q -p no:cacheprovider tests 2>&1 | tail -3") if (t / "orig/tests").exists() else ""
        own_pass, own_fail = _pytest_counts(out3)
    repro = 1.0 if repro_fail >= 2 else 0.5 if repro_fail == 1 else 0.0
    score = 0.85 * hidden + 0.15 * repro * (1.0 if own_fail == 0 else 0.5)
    return score, {"hidden_passed": passed, "hidden_total": passed + failed, "repro_tests_failing_on_original": repro_fail,
                   "own_tests": f"{own_pass} pass / {own_fail} fail on fixed code",
                   "note": f"hidden {passed}/{passed + failed}, {repro_fail} repro tests"}


# ============================================================================ task: reverse
REVERSE_PROMPT = """`{dir}/crackme` is a stripped Linux x86-64 binary. Run as `./crackme <KEY>` it prints ACCESS DENIED
unless the key is right. Work out the valid key by analysing the binary (you have no source), then
1) write the key, alone on one line, to `{dir}/answer.txt`;
2) write `{dir}/NOTES.md`: how the check works and how you derived the key.
Brute-forcing the whole key space is not feasible. Reply with the key and a one-paragraph explanation."""

CRACKME_C = r"""#include <stdio.h>
#include <string.h>
static const unsigned char K[12] = {%(K)s};
static const unsigned char T[12] = {%(T)s};
static int check(const char *s) {
    if (strlen(s) != 12) return 0;
    unsigned int acc = 0;
    for (int i = 0; i < 12; i++) {
        unsigned char c = (unsigned char)s[i];
        if ((unsigned char)(((c ^ K[i]) + 7 * i) & 0xff) != T[i]) acc |= 1;
    }
    return acc == 0;
}
int main(int argc, char **argv) {
    if (argc < 2) { puts("usage: crackme KEY"); return 2; }
    if (check(argv[1])) { puts("ACCESS GRANTED"); return 0; }
    puts("ACCESS DENIED"); return 1;
}
"""


def reverse_setup(d: Path, rng: random.Random) -> dict:
    key = "".join(rng.choice(string.ascii_uppercase + string.digits) for _ in range(12))
    K = [rng.randrange(1, 256) for _ in range(12)]
    T = [((ord(c) ^ K[i]) + 7 * i) & 0xFF for i, c in enumerate(key)]
    with tempfile.TemporaryDirectory(prefix="sb-cc-") as tmp:
        src = Path(tmp) / "c.c"
        src.write_text(CRACKME_C % {"K": ",".join(map(str, K)), "T": ",".join(map(str, T))})
        r = subprocess.run(["docker", "run", "--rm", "--network", "none", "--user", f"{os.getuid()}:{os.getgid()}", "--entrypoint", "bash",
                            "-v", f"{tmp}:/s", SANDBOX_IMAGE, "-lc", "cd /s && gcc -O2 -s -o crackme c.c"], capture_output=True, text=True, timeout=300)
        if r.returncode:
            raise RuntimeError(r.stderr)
        shutil.copy(Path(tmp) / "crackme", d / "crackme")
    (d / "crackme").chmod(0o755)
    return {"key": key}


def reverse_grade(d: Path, secret: dict, run: dict) -> tuple[float, dict]:
    ans = (d / "answer.txt").read_text().strip().splitlines()[0].strip() if (d / "answer.txt").exists() and (d / "answer.txt").read_text().strip() else ""
    ok = False
    if ans and re.fullmatch(r"[ -~]{1,64}", ans):
        out = _grader([(str(d), "/g")], f"/g/crackme {json.dumps(ans)}", timeout=60)
        ok = "ACCESS GRANTED" in out
    notes = (d / "NOTES.md").read_text(errors="replace") if (d / "NOTES.md").exists() else ""
    explained = bool(re.search(r"xor|\^", notes, re.I)) and bool(re.search(r"\b12\b|twelve", notes))
    partial = sum(a == b for a, b in zip(ans, secret["key"])) / 12 if ans else 0
    score = 1.0 if ok else 0.4 * partial + 0.1 * explained
    return score, {"answer": ans, "correct": ok, "chars_right": round(partial * 12), "explained": explained,
                   "note": f"{'GRANTED' if ok else 'denied'} ({round(partial * 12)}/12 chars), notes {'ok' if explained else 'missing'}"}


# ============================================================================ task: review
REVIEW_PROMPT = """`{dir}/userservice` is a git repository. A teammate just pushed the last commit (HEAD) and asked for a
code review before it ships. Review the changes in HEAD against HEAD~1 and write your review to
`{dir}/userservice/REVIEW.md`: one section per finding with the file and line, severity, why it's a problem,
and the fix. Do not modify the code. Reply with a summary of the most important findings."""


def review_setup(d: Path, rng: random.Random) -> dict:
    repo = d / "userservice"; repo.mkdir(parents=True)
    env = {**os.environ, "GIT_AUTHOR_NAME": "dev", "GIT_AUTHOR_EMAIL": "dev@example.com", "GIT_COMMITTER_NAME": "dev",
           "GIT_COMMITTER_EMAIL": "dev@example.com", "GIT_AUTHOR_DATE": "2026-10-01T10:00:00", "GIT_COMMITTER_DATE": "2026-10-01T10:00:00"}
    g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], env=env, capture_output=True, check=True)
    g("init", "-q", "-b", "main")
    shutil.copy(PRIVATE / "review/base_app.py", repo / "app.py")
    (repo / "README.md").write_text("# userservice\n\nUser accounts for the admin console.\n")
    g("add", "."); g("commit", "-qm", "user service: login + lookup")
    shutil.copy(PRIVATE / "review/feature_app.py", repo / "app.py")
    g("add", "."); g("commit", "-qm", "admin console: search, paging, password reset, delete, export")
    return {}


def review_grade(d: Path, secret: dict, run: dict) -> tuple[float, dict]:
    rv = d / "userservice/REVIEW.md"
    if not rv.exists():
        return 0.0, {"note": "no REVIEW.md"}
    text = rv.read_text(errors="replace")
    bugs = json.loads((PRIVATE / "review/bugs.json").read_text())
    chunks = [c for c in re.split(r"\n(?=#{1,4} |\s*\d+[.)] |\s*[-*] \*\*)", text) if c.strip()]
    found, hits = [], {}
    for b in bugs:
        for c in chunks:
            lines = [int(x) for x in re.findall(r"(?:line[s]?\s*|L|:)(\d{1,3})\b", c, re.I)]
            anchored = b["fn"] in c or any(abs(n - b["line"]) <= 3 for n in lines)
            if anchored and re.search(b["kw"], c, re.I):
                found.append(b["id"]); hits[b["id"]] = c.strip()[:100]; break
    modified = subprocess.run(["git", "-C", str(d / "userservice"), "diff", "--quiet", "HEAD", "--", "app.py"]).returncode != 0
    score = len(found) / len(bugs) * (0.8 if modified else 1.0)
    return score, {"found": found, "missed": [b["id"] for b in bugs if b["id"] not in found], "code_modified": modified,
                   "findings": len(chunks), "note": f"{len(found)}/{len(bugs)} planted bugs" + (" (code modified!)" if modified else "")}


# ============================================================================ task: browser
BROWSER_PROMPT = """There is a small signup web app in `{dir}/app/` (open `index.html`; serve the folder with
`python3 -m http.server` if you prefer http). Use a real browser to sign up as **Ada Lovelace** on the
**Pro** plan, accept the terms, and submit. The app then shows a confirmation code like `XXXX-XXXX`.
Write exactly that code to `{dir}/code.txt` and reply with it."""

BROWSER_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Orbit signup</title>
<style>body{font:16px system-ui;max-width:520px;margin:40px auto}.step{display:none}.step.on{display:block}
.opt{border:1px solid #888;padding:10px;margin:6px 0;cursor:pointer;border-radius:8px}.opt.sel{background:#dde}
button{padding:8px 16px;margin-top:12px}#out{font:700 22px monospace;margin-top:16px}</style></head><body>
<h1>Orbit — create your account</h1>
<div class="step on" id="s1"><label>Full name <input id="name" autocomplete="off"></label><br><button id="n1">Next</button><p id="e1" style="color:#b00"></p></div>
<div class="step" id="s2"><p>Choose a plan</p><div role="listbox" id="plans">
<div class="opt" role="option" data-v="starter">Starter — free</div><div class="opt" role="option" data-v="pro">Pro — $12/mo</div>
<div class="opt" role="option" data-v="team">Team — $40/mo</div></div><button id="n2" disabled>Next</button></div>
<div class="step" id="s3"><label><input type="checkbox" id="agree"> I accept the terms</label><br><button id="go" disabled>Create account</button></div>
<div id="busy" style="display:none">Creating your account…</div><div id="out" aria-live="polite"></div>
<script>%(js)s</script></body></html>"""

BROWSER_JS = """(function(){var _0=[%(salt)s].reverse().map(function(c){return String.fromCharCode(c^%(x)d)}).join('');
function $(i){return document.getElementById(i)}var st={n:'',p:''};
function h(s){var x=0x811c9dc5;for(var i=0;i<s.length;i++){x^=s.charCodeAt(i);x=Math.imul(x,0x01000193)>>>0}return x}
function code(){var a=h(_0+'|'+st.n+'|'+st.p),b=h(st.p+'#'+_0+'#'+st.n);function f(v){return ('000000'+v.toString(36).toUpperCase()).slice(-4)}return f(a)+'-'+f(b)}
$('n1').onclick=function(){var v=$('name').value.trim().replace(/\\s+/g,' ');if(v.split(' ').length<2){$('e1').textContent='Please enter first and last name';return}st.n=v.toLowerCase();$('s1').classList.remove('on');$('s2').classList.add('on')};
Array.prototype.forEach.call(document.querySelectorAll('.opt'),function(o){o.onclick=function(){Array.prototype.forEach.call(document.querySelectorAll('.opt'),function(z){z.classList.remove('sel')});o.classList.add('sel');st.p=o.getAttribute('data-v');$('n2').disabled=false}});
$('n2').onclick=function(){$('s2').classList.remove('on');$('s3').classList.add('on')};
$('agree').onchange=function(){$('go').disabled=!this.checked};
$('go').onclick=function(){$('s3').classList.remove('on');$('busy').style.display='block';setTimeout(function(){$('busy').style.display='none';$('out').textContent='Confirmation: '+code()},1500)};
})();"""


def _fnv(s: str) -> int:
    x = 0x811C9DC5
    for ch in s:
        x ^= ord(ch); x = (x * 0x01000193) & 0xFFFFFFFF
    return x


def _b36(v: int) -> str:
    d = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"; s = ""
    while v:
        v, r = divmod(v, 36); s = d[r] + s
    return ("000000" + (s or "0"))[-4:]


def browser_setup(d: Path, rng: random.Random) -> dict:
    salt = "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(10)); x = rng.randrange(1, 120)
    js = BROWSER_JS % {"salt": ",".join(str(ord(c) ^ x) for c in reversed(salt)), "x": x}
    (d / "app").mkdir(parents=True)
    (d / "app/index.html").write_text(BROWSER_HTML % {"js": js})
    n, p = "ada lovelace", "pro"
    return {"code": _b36(_fnv(f"{salt}|{n}|{p}")) + "-" + _b36(_fnv(f"{p}#{salt}#{n}"))}


def browser_grade(d: Path, secret: dict, run: dict) -> tuple[float, dict]:
    got = (d / "code.txt").read_text().strip() if (d / "code.txt").exists() else ""
    got = (re.search(r"[0-9A-Z]{4}-[0-9A-Z]{4}", got) or [""])[0]
    ok = got == secret["code"]
    in_reply = secret["code"] in run.get("final", "")
    used_browser = any(re.search(r"browser|agent-browser|playwright|chrom", (t["name"] + json.dumps(t.get("input") or ""))[:4000], re.I) for t in run.get("tools", []))
    score = 1.0 if ok else 0.5 if in_reply else 0.0
    return score, {"got": got, "correct": ok, "in_reply": in_reply, "used_browser": used_browser,
                   "note": f"{'correct' if ok else 'wrong/none'} code{'' if used_browser else ' (no browser use seen)'}"}


# ============================================================================ hard tier
BUGFIX2_PROMPT = """The job-runner team filed incident notes against our `infra` package. The project is in
`{dir}/infra-project` (package `infra/`, tests in `tests/`, notes in `REPORT.md`). Find and fix the root causes of
every symptom — the module docstrings are the spec — without changing public signatures. Python 3 and
`pip install pytest` work in the sandbox. Reply with each root cause and how you verified the fix."""


def bugfix2_setup(d: Path, rng: random.Random) -> dict:
    shutil.copytree(PRIVATE / "bugfix2", d / "infra-project")
    return {}


def make_pytest_grade(project: str, pkg: str, hidden: str, orig: str):
    def grade(d: Path, secret: dict, run: dict) -> tuple[float, dict]:
        proj = d / project
        if not (proj / pkg).exists():
            return 0.0, {"note": "project missing"}
        with tempfile.TemporaryDirectory(prefix="sb-grade-") as tmp:
            t = Path(tmp); shutil.copytree(proj / pkg, t / f"fixed/{pkg}")
            shutil.copy(PRIVATE / "hidden" / hidden, t / "fixed/test_hidden.py")
            out = _grader([(str(t), "/g")], "cd /g/fixed && python3 -m pytest -q -p no:cacheprovider test_hidden.py 2>&1 | tail -3")
            passed, failed = _pytest_counts(out)
            shutil.copytree(PRIVATE / orig / pkg, t / f"orig/{pkg}")
            repro_fail = 0
            if (proj / "tests").exists():
                shutil.copytree(proj / "tests", t / "orig/tests")
                _, repro_fail = _pytest_counts(_grader([(str(t), "/g")], "cd /g/orig && python3 -m pytest -q -p no:cacheprovider tests 2>&1 | tail -3"))
        hidden_frac = passed / max(1, passed + failed)
        repro = 1.0 if repro_fail >= 2 else 0.5 if repro_fail == 1 else 0.0
        return 0.85 * hidden_frac + 0.15 * repro, {"hidden_passed": passed, "hidden_total": passed + failed, "repro_tests": repro_fail,
                                                   "note": f"hidden {passed}/{passed + failed}, {repro_fail} repro tests"}
    return grade


REVERSE2_PROMPT = """`{dir}/vault` is a stripped Linux x86-64 binary: `./vault <KEY>` prints ACCESS GRANTED for the right key.
Recover the key by analysing the binary (no source; brute force is infeasible). Beware of anything that looks too easy.
1) write the key alone on one line to `{dir}/answer.txt`;  2) explain the check in `{dir}/NOTES.md`.
Reply with the key and a short explanation."""

VAULT_C = r"""#include <stdio.h>
#include <string.h>
static const unsigned char E[16] = {%(E)s};
static const char DBG[] = "%(decoy)s";
static unsigned int seed;
static unsigned int lcg(void) { seed = seed * 1103515245u + 12345u; return (seed >> 16) & 0x7fff; }
static int legacy(const char *s) { return strcmp(s, DBG) == 0; }
static int check(const char *s) {
    if (strlen(s) != 16) return 0;
    unsigned char T[16]; seed = %(seed)su;
    for (int i = 0; i < 16; i++) T[i] = (unsigned char)lcg();
    unsigned int h = 0x1234; int ok = 1;
    for (int i = 0; i < 16; i++) {
        unsigned char c = (unsigned char)s[i];
        unsigned char r = (unsigned char)(((c << 3) | (c >> 5)) ^ T[i]);
        r = (unsigned char)(r + (h & 0xff));
        if (r != E[i]) ok = 0;
        h = h * 31u + c;
    }
    return ok;
}
int main(int argc, char **argv) {
    if (argc < 2) { puts("usage: vault KEY"); return 2; }
    if (legacy(argv[1])) { puts("ACCESS DENIED (legacy key revoked)"); return 1; }
    if (check(argv[1])) { puts("ACCESS GRANTED"); return 0; }
    puts("ACCESS DENIED"); return 1;
}
"""


def reverse2_setup(d: Path, rng: random.Random) -> dict:
    key = "".join(rng.choice(string.ascii_uppercase + string.digits) for _ in range(16))
    seed0 = rng.randrange(1, 2**31); sd = seed0; T = []
    for _ in range(16):
        sd = (sd * 1103515245 + 12345) & 0xFFFFFFFF; T.append(((sd >> 16) & 0x7FFF) & 0xFF)
    h, E = 0x1234, []
    for i, ch in enumerate(key):
        c = ord(ch); r = (((c << 3) | (c >> 5)) & 0xFF) ^ T[i]; r = (r + (h & 0xFF)) & 0xFF; E.append(r)
        h = (h * 31 + c) & 0xFFFFFFFF
    decoy = "VAULT-" + "".join(rng.choice(string.ascii_uppercase + string.digits) for _ in range(10))
    with tempfile.TemporaryDirectory(prefix="sb-cc-") as tmp:
        (Path(tmp) / "c.c").write_text(VAULT_C % {"E": ",".join(map(str, E)), "decoy": decoy, "seed": seed0})
        r = subprocess.run(["docker", "run", "--rm", "--network", "none", "--user", f"{os.getuid()}:{os.getgid()}", "--entrypoint", "bash",
                            "-v", f"{tmp}:/s", SANDBOX_IMAGE, "-lc", "cd /s && gcc -O2 -s -o vault c.c"], capture_output=True, text=True, timeout=300)
        if r.returncode:
            raise RuntimeError(r.stderr)
        shutil.copy(Path(tmp) / "vault", d / "vault")
    (d / "vault").chmod(0o755)
    return {"key": key, "decoy": decoy}


def reverse2_grade(d: Path, secret: dict, run: dict) -> tuple[float, dict]:
    a = d / "answer.txt"
    ans = a.read_text().strip().splitlines()[0].strip() if a.exists() and a.read_text().strip() else ""
    ok = bool(ans) and re.fullmatch(r"[ -~]{1,64}", ans) and "ACCESS GRANTED" in _grader([(str(d), "/g")], f"/g/vault {json.dumps(ans)}", timeout=60)
    notes = (d / "NOTES.md").read_text(errors="replace") if (d / "NOTES.md").exists() else ""
    understood = sum(bool(re.search(p, notes, re.I)) for p in (r"lcg|1103515245|linear congruential", r"rot|<< ?3|>> ?5", r"\b31\b|hash|chain"))
    partial = sum(x == y for x, y in zip(ans, secret["key"])) / 16 if ans else 0
    fooled = ans == secret["decoy"]
    score = 1.0 if ok else 0.4 * partial + 0.05 * understood
    return score, {"answer": ans, "correct": bool(ok), "fooled_by_decoy": fooled, "chars_right": round(partial * 16), "understood": understood,
                   "note": f"{'GRANTED' if ok else 'denied'} ({round(partial * 16)}/16){' DECOY' if fooled else ''}, notes {understood}/3"}


def review2_setup(d: Path, rng: random.Random) -> dict:
    repo = d / "userservice"; repo.mkdir(parents=True)
    env = {**os.environ, "GIT_AUTHOR_NAME": "dev", "GIT_AUTHOR_EMAIL": "dev@example.com", "GIT_COMMITTER_NAME": "dev",
           "GIT_COMMITTER_EMAIL": "dev@example.com", "GIT_AUTHOR_DATE": "2026-10-01T10:00:00", "GIT_COMMITTER_DATE": "2026-10-01T10:00:00"}
    g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], env=env, capture_output=True, check=True)
    g("init", "-q", "-b", "main")
    shutil.copy(PRIVATE / "review2/base_app.py", repo / "app.py")
    (repo / "README.md").write_text("# userservice\n\nUser accounts for the admin console. Timestamps are stored as ISO-8601 UTC.\n")
    g("add", "."); g("commit", "-qm", "user service: login + lookup")
    shutil.copy(PRIVATE / "review2/feature_app.py", repo / "app.py")
    g("add", "."); g("commit", "-qm", "sessions, admin tools, credits, exports, audit log, webhooks")
    return {}


def review2_grade(d: Path, secret: dict, run: dict) -> tuple[float, dict]:
    rv = d / "userservice/REVIEW.md"
    if not rv.exists():
        return 0.0, {"note": "no REVIEW.md"}
    text = rv.read_text(errors="replace")
    spec = json.loads((PRIVATE / "review2/bugs.json").read_text())
    chunks = [c for c in re.split(r"\n(?=#{1,4} |\s*\d+[.)] |\s*[-*] \*\*)", text) if c.strip()]
    found = []
    for b in spec["bugs"]:
        for c in chunks:
            lines = [int(x) for x in re.findall(r"(?:line[s]?\s*|L|:)(\d{1,3})\b", c, re.I)]
            if (b["fn"] in c or any(abs(n - b["line"]) <= 3 for n in lines)) and re.search(b["kw"], c, re.I):
                found.append(b["id"]); break
    false_alarms = [x["id"] for x in spec["decoys"] if any(x["fn"] in c and re.search(r"\b(critical|high)\b", c, re.I) for c in chunks)]
    modified = subprocess.run(["git", "-C", str(d / "userservice"), "diff", "--quiet", "HEAD", "--", "app.py"]).returncode != 0
    recall = len(found) / len(spec["bugs"])
    score = max(0.0, recall - 0.05 * len(false_alarms)) * (0.8 if modified else 1.0)
    return score, {"found": found, "missed": [b["id"] for b in spec["bugs"] if b["id"] not in found], "false_alarms": false_alarms,
                   "note": f"{len(found)}/{len(spec['bugs'])} bugs, {len(false_alarms)} decoys flagged high" + (" (code modified!)" if modified else "")}


# ============================================================================ registry
TASKS = {
    "design": {"prompt": DESIGN_BRIEF, "setup": design_setup, "grade": design_grade, "skills": ["frontend-design", "design-taste-frontend"],
               "accept": ["claude-design", "landing-page-design", "web-design-guidelines", "impeccable"], "max_turns": 60, "budget": 1500},
    "bugfix": {"prompt": BUGFIX_PROMPT, "setup": bugfix_setup, "grade": bugfix_grade, "skills": ["diagnosing-bugs", "tdd"],
               "accept": ["systematic-debugging", "sandbox-coding"], "max_turns": 60, "budget": 1500},
    "reverse": {"prompt": REVERSE_PROMPT, "setup": reverse_setup, "grade": reverse_grade, "skills": ["reverse-engineering"], "accept": ["reverse-engineer"],
                "max_turns": 70, "budget": 1800},
    "review": {"prompt": REVIEW_PROMPT, "setup": review_setup, "grade": review_grade, "skills": ["code-review"],
               "max_turns": 40, "budget": 1200},
    "browser": {"prompt": BROWSER_PROMPT, "setup": browser_setup, "grade": browser_grade, "skills": ["agent-browser"],
                "max_turns": 50, "budget": 1200},
}
TASKS.update({
    "bugfix2": {"prompt": BUGFIX2_PROMPT, "setup": bugfix2_setup, "grade": make_pytest_grade("infra-project", "infra", "test_hidden_bugfix2.py", "bugfix2"),
                "skills": ["diagnosing-bugs", "tdd"], "accept": ["systematic-debugging", "sandbox-coding"], "max_turns": 80, "budget": 1800},
    "reverse2": {"prompt": REVERSE2_PROMPT, "setup": reverse2_setup, "grade": reverse2_grade, "skills": ["reverse-engineering"],
                 "accept": ["reverse-engineer"], "max_turns": 80, "budget": 1800},
    "review2": {"prompt": REVIEW_PROMPT, "setup": review2_setup, "grade": review2_grade, "skills": ["code-review"],
                "max_turns": 40, "budget": 1500},
})
CONDS = ("base", "preload", "auto", "router")


# ============================================================================ harness
def new_skills() -> list[str]:
    """Skills added by skillport/hub (the 'treatment'): everything that isn't built-in or local-agent."""
    out = subprocess.run([str(SKILLPORT), "list", "--json"], capture_output=True, text=True, timeout=120).stdout
    return sorted({s["name"] for s in json.loads(out[out.find("["):]) if s["source"] in ("ported", "hub")})


def make_home(cond: str, extra_disabled: list[str]) -> Path:
    home = BENCH_HOMES / cond
    home.mkdir(parents=True, exist_ok=True)
    cfg = (REAL_HOME / "config.yaml").read_text()
    cfg = re.sub(r"^mcp_servers:\n(?:(?:[ \t]+.*|[ \t]*)\n)*", "mcp_servers: {}\n", cfg, flags=re.M)   # no crew/rag: plain agent
    m = re.search(r"^(skills:[ \t]*\n(?:(?:[ \t]+.*)?\n)*?  disabled: )(\[.*\])", cfg, flags=re.M)
    disabled = sorted(set(json.loads(m.group(2))) | set(extra_disabled))
    cfg = cfg[:m.start(2)] + json.dumps(disabled) + cfg[m.end(2):]
    (home / "config.yaml").write_text(cfg)
    for name in ("skills", "plugins"):
        link = home / name
        if not link.is_symlink():
            shutil.rmtree(link, ignore_errors=True); link.symlink_to(REAL_HOME / name)
    if (REAL_HOME / "SOUL.md").exists():
        shutil.copy(REAL_HOME / "SOUL.md", home / "SOUL.md")
    return home


def parse_stream(stdout: str) -> dict:
    run = {"tools": [], "final": "", "tokens": {}, "error": ""}
    pending, text = {}, []
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "tool_use":
            rec = {"name": ev.get("name", ""), "input": ev.get("input"), "output": "", "is_error": False}
            run["tools"].append(rec); pending[ev.get("tool_call_id") or rec["name"]] = rec
        elif ev.get("type") == "tool_result":
            rec = pending.pop(ev.get("tool_call_id") or ev.get("name"), None)
            if rec:
                rec["output"], rec["is_error"] = str(ev.get("output", ""))[:2000], ev.get("is_error", False)
        elif ev.get("type") == "text":
            text.append(ev.get("text", ""))
        elif ev.get("type") == "result":
            run["final"] = ev.get("text") or "".join(text); run["tokens"] = ev.get("tokens", {}); run["error"] = ev.get("error", "")
    run["final"] = run["final"] or "".join(text)
    return run


def skills_viewed(run: dict) -> list[str]:
    out = []
    for t in run["tools"]:
        if t["name"] == "skill_view":
            inp = t.get("input") or {}
            if isinstance(inp, str):
                try:
                    inp = json.loads(inp)
                except Exception:
                    inp = {}
            n = inp.get("name") if isinstance(inp, dict) else None
            if n and n not in out:
                out.append(n)
    return out


def run_one(task: str, cond: str, rep: int, label: str, homes: dict) -> dict:
    t = TASKS[task]
    rid = f"{label}-{task}-{cond}-{rep}"
    d = WS_HOST / rid
    shutil.rmtree(d, ignore_errors=True); d.mkdir(parents=True)
    seed = random.Random(f"{label}|{task}|{rep}").randrange(1 << 30)     # same task instance across conditions
    secret = t["setup"](d, random.Random(seed))
    SECRETS.mkdir(parents=True, exist_ok=True); (SECRETS / f"{rid}.json").write_text(json.dumps(secret))
    home = homes[cond]
    shutil.rmtree(home / "memories", ignore_errors=True)
    out = RESULTS / label; out.mkdir(parents=True, exist_ok=True)
    prompt = t["prompt"].format(dir=f"{WS_SANDBOX}/{rid}")
    qfile = out / f"{rid}.prompt.txt"; qfile.write_text(prompt)
    cmd = [str(HERMES), "chat", "--query-file", str(qfile), "--oneshot", "--format", "stream-json", "--yolo",
           "--max-turns", str(t["max_turns"]), "--run-budget", str(t["budget"]), "--source", "skillbench", "--provider", "custom:local-high"]
    routing = None
    if cond == "preload":
        for s in t["skills"]:
            cmd += ["--skills", s]
    elif cond == "router":
        try:
            routing = decider.route_skills(prompt)
        except Exception as e:                      # fail-open, exactly like the HUD: run with nothing preloaded
            routing = {"skills": [], "error": f"{type(e).__name__}: {e}"[:200]}
        for s in routing["skills"]:
            cmd += ["--skills", s]
        say(f"route {rid}: {routing['skills'] or 'nothing'}  need={routing.get('need')} top={routing.get('top', [])[:3]} {routing.get('ms', 0)} ms")
    say(f"start {rid}")
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=ROOT / "workspace", env=os.environ | {"HERMES_HOME": str(home)}, capture_output=True, text=True,
                           timeout=t["budget"] + 300)
        stdout, stderr = p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        stdout = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""); stderr = "TIMEOUT"
    wall = time.time() - t0
    (out / f"{rid}.jsonl").write_text(stdout); (out / f"{rid}.stderr.txt").write_text(stderr[-20000:])
    run = parse_stream(stdout)
    try:
        score, det = t["grade"](d, secret, run)
    except Exception as e:
        score, det = 0.0, {"note": f"grader error: {e!r}"[:300]}
    viewed = skills_viewed(run)
    preloaded = t["skills"] if cond == "preload" else (routing or {}).get("skills", [])
    rec = {"id": rid, "task": task, "cond": cond, "rep": rep, "score": round(score, 3), "wall_s": round(wall), "tool_calls": len(run["tools"]),
           "tool_errors": sum(t_["is_error"] for t_ in run["tools"]), "tokens": run["tokens"], "skills_viewed": viewed, "skills_preloaded": preloaded,
           "relevant_skill_loaded": any(s in preloaded or s in viewed for s in t["skills"] + t.get("accept", [])), "error": run["error"][:300],
           "routing": routing, "detail": det, "final": run["final"][-1200:]}
    (out / f"{rid}.result.json").write_text(json.dumps(rec, indent=2))
    say(f"done  {rid}: {score:.2f}  {det.get('note', '')}  [{round(wall)}s, {len(run['tools'])} tools, viewed {viewed}]")
    return rec


def hud_busy() -> int:
    try:
        c = sqlite3.connect(f"file:{ROOT}/hud/data/hud.db?mode=ro", uri=True)
        return c.execute("SELECT count(*) FROM jobs WHERE status='running'").fetchone()[0]
    except Exception:
        return 0


def cmd_run(a) -> None:
    tasks = a.tasks.split(",") if a.tasks else list(TASKS)
    conds = a.conds.split(",") if a.conds else list(CONDS)
    treatment = new_skills()
    homes = {"base": make_home("base", treatment), "preload": make_home("preload", []), "auto": make_home("auto", []), "router": make_home("router", [])}
    if "router" in conds and (not decider or not decider.health().get("ok")):
        sys.exit("condition 'router' needs decide/decide.py + la-decide (127.0.0.1:8183) + la-embed up")
    say(f"label {a.label}: {len(tasks)} tasks × {conds} × {a.reps} reps; {len(treatment)} new skills disabled in 'base'")
    jobs = [(t, c, r) for r in range(a.reps) for t in tasks for c in conds]
    results = []
    with cf.ThreadPoolExecutor(max_workers=a.j) as ex:
        futs = []
        for j in jobs:
            while hud_busy() + sum(not f.done() for f in futs) >= a.j + 1 and a.polite:   # yield to the owner's HUD jobs
                time.sleep(20)
            futs.append(ex.submit(run_one, *j, a.label, homes))
            time.sleep(5)
        for f in cf.as_completed(futs):
            try:
                results.append(f.result())
            except Exception as e:
                say(f"run crashed: {e!r}")
    report(a.label)


def load_results(label: str) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted((RESULTS / label).glob("*.result.json"))]


def report(label: str) -> None:
    rs = load_results(label)
    if not rs:
        print("no results"); return
    tasks = [t for t in TASKS if any(r["task"] == t for r in rs)]
    conds = [c for c in CONDS if any(r["cond"] == c for r in rs)]
    avg = lambda xs: sum(xs) / len(xs) if xs else float("nan")
    lines = [f"# skillbench {label}", "", "| task | " + " | ".join(conds) + " | best Δ vs base |", "|---|" + "---|" * (len(conds) + 1)]
    for t in tasks:
        cells = {c: avg([r["score"] for r in rs if r["task"] == t and r["cond"] == c]) for c in conds}
        d = max((cells[c] - cells.get("base", 0) for c in conds if c != "base"), default=0) if "base" in cells else float("nan")
        lines.append(f"| {t} | " + " | ".join(f"{cells[c]:.2f}" for c in conds) + f" | {d:+.2f} |")
    lines.append("| **mean** | " + " | ".join(f"**{avg([r['score'] for r in rs if r['cond'] == c]):.2f}**" for c in conds) + " | |")
    for c in ("auto", "router"):
        sub = [r for r in rs if r["cond"] == c]
        if sub:
            what = "agent found and loaded the relevant skill itself" if c == "auto" else "decision model preloaded the relevant skill"
            lines += ["", f"Skill discovery ({c}): {what} in {sum(r['relevant_skill_loaded'] for r in sub)}/{len(sub)} runs."]
    rt = [r for r in rs if r["cond"] == "router" and r.get("routing")]
    if rt:
        ms = sorted(r["routing"].get("ms", 0) for r in rt)
        lines += [f"Router: median {ms[len(ms) // 2]} ms per decision; picks: " + "; ".join(f"{r['task']} → {', '.join(r['routing']['skills']) or 'nothing'}" for r in rt) + "."]
    lines += ["", "| run | score | wall s | tools | skills preloaded | skills viewed | note |", "|---|---|---|---|---|---|---|"]
    lines += [f"| {r['task']}/{r['cond']}#{r['rep']} | {r['score']:.2f} | {r['wall_s']} | {r['tool_calls']} | {', '.join(r.get('skills_preloaded', [])) or '—'} | {', '.join(r['skills_viewed']) or '—'} | {r['detail'].get('note', '')[:80]} |" for r in rs]
    md = "\n".join(lines) + "\n"
    (RESULTS / label / "REPORT.md").write_text(md)
    print(md)
    for c in conds:   # HUD Evals page: one summary.json per condition
        cr = [r for r in rs if r["cond"] == c]
        by = {t: round(avg([r["score"] for r in cr if r["task"] == t]), 3) for t in tasks if any(r["task"] == t for r in cr)}
        o = EVAL_RESULTS / f"skillbench-{label}-{c}"; o.mkdir(parents=True, exist_ok=True)
        (o / "summary.json").write_text(json.dumps({
            "label": f"skillbench {label} · {c}", "n": len(cr), "overall": round(avg([r["score"] for r in cr]), 3), "by_category": by,
            "weakest": min(by, key=by.get), "results": [{"id": f"{r['task']}#{r['rep']}", "category": r["task"], "score": r["score"],
                                                         "note": r["detail"].get("note", ""), "tool_calls": r["tool_calls"], "wall_s": r["wall_s"]} for r in cr]}, indent=2))


def cmd_regrade(a) -> None:
    for p in sorted((RESULTS / a.label).glob("*.result.json")):
        r = json.loads(p.read_text())
        if a.tasks and r["task"] not in a.tasks.split(","):
            continue
        d = WS_HOST / r["id"]; secret = json.loads((SECRETS / f"{r['id']}.json").read_text())
        run = parse_stream((RESULTS / a.label / f"{r['id']}.jsonl").read_text())
        score, det = TASKS[r["task"]]["grade"](d, secret, run)
        say(f"{r['id']}: {r['score']:.2f} -> {score:.2f}  {det.get('note', '')}")
        r["score"], r["detail"] = round(score, 3), det
        p.write_text(json.dumps(r, indent=2))
    report(a.label)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("--label", required=True); r.add_argument("--tasks"); r.add_argument("--conds")
    r.add_argument("-j", type=int, default=2); r.add_argument("--reps", type=int, default=1)
    r.add_argument("--no-polite", dest="polite", action="store_false", help="don't wait for the owner's running HUD jobs")
    g = sub.add_parser("regrade"); g.add_argument("label"); g.add_argument("--tasks")
    p = sub.add_parser("report"); p.add_argument("label")
    a = ap.parse_args()
    {"run": cmd_run, "regrade": cmd_regrade, "report": lambda a: report(a.label)}[a.cmd](a)


if __name__ == "__main__":
    main()
