#!/usr/bin/env python3
"""decide — "System One" decisions for the local agent stack (the Jev idea, run locally).

A decision model never writes text. You send a `state` (text or JSON) and typed questions, it returns
calibrated probabilities in well under a second:

    noul    yes/no              -> probability of yes
    choice  pick one option     -> pick + a probability per option     (clef: up to 255 options)
    score   rate on your levels -> position + a probability per level

The API is TypeSafe's Jev API (POST /v1/systemone). By default it is served by Cloudflare's open-weight
Clef-Flash in a second llama-server on 127.0.0.1:8183 (unit la-decide), so nothing leaves the box. Any other
Jev-compatible endpoint works too (TypeSafe Jev, OpenAI Decisions via a shim, Ollama >= 0.35 nimble/tev1,
Workers AI): set LA_DECIDE_URL (+ LA_DECIDE_KEY / LA_DECIDE_MODEL). Cloud endpoints are opt-in only.

Following TypeSafe's guidance, the model makes *judgments* and this code makes the *decisions*
(thresholds, caps, fallbacks) — see route_skills() and route_effort().

    decide ask "state text" --noul "Is this spam?" --choice "team=billing,technical,other" --score "urgency=low,mid,high"
    decide skills "build me a landing page for my bike light startup"
    decide effort "what's 2+2?"
    decide health
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(os.environ.get("LA_ROOT", "/mnt/data/local-agent"))
URL = os.environ.get("LA_DECIDE_URL", "http://127.0.0.1:8183/v1/systemone")
KEY = os.environ.get("LA_DECIDE_KEY", "")
MODEL = os.environ.get("LA_DECIDE_MODEL", "clef-flash")
EMBED_URL = os.environ.get("LA_EMBED_URL", "http://127.0.0.1:8182/v1/embeddings")
SKILLPORT = ROOT / "bin/skillport"
CACHE = ROOT / "decide/cache"

# ---- decision policy (code, not model). Tuned on decide/router_eval.py — change them there, re-run, then here.
SKILL_NEED_MIN = 0.5        # noul "does the request need a specialised skill?" below this -> load nothing
SKILL_PICK_MIN = 0.30       # a skill is loaded when its choice probability is at least this
SKILL_MAX = 2               # never preload more than this many
SKILL_MAX_CHARS = 45_000    # nor more than this much SKILL.md text in total (big skills cost the local model real time)
SHORTLIST = 24              # embeddings shortlist fed to the choice question (clef allows 255; fewer = faster + sharper)
NEVER_ROUTE = {"find-skills", "ask-matt", "setup-matt-pocock-skills", "wait-what", "caveman", "caveman-commit", "caveman-review", "hermes-agent"}  # meta / voice-gimmick skills
QUICK_MIN = 0.75           # noul "can a short no-tools reply answer this?" at or above this -> quick reply (skip the agent)


class DecideError(RuntimeError):
    pass


# ============================================================================ client
def systemone(state, questions: dict, timeout: float = 60) -> dict:
    """One Jev-API call. Raises DecideError on any failure — a failed call is an error, never a 'no'."""
    body = {"model": MODEL, "state": state, "questions": questions}
    headers = {"Content-Type": "application/json"} | ({"Authorization": f"Bearer {KEY}"} if KEY else {})
    t0 = time.perf_counter()
    try:
        r = httpx.post(URL, json=body, headers=headers, timeout=timeout, trust_env=not URL.startswith("http://127."))
    except httpx.HTTPError as e:
        raise DecideError(f"decision endpoint unreachable ({URL}): {e}") from e
    if r.status_code != 200:
        raise DecideError(f"decision endpoint HTTP {r.status_code}: {r.text[:300]}")
    out = r.json()
    out["_ms"] = round((time.perf_counter() - t0) * 1000)
    return out


def health() -> dict:
    try:
        base = URL.rsplit("/v1/", 1)[0]
        h = httpx.get(base + "/health", timeout=5, trust_env=False).json()
        ans = systemone("The sky is blue.", {"q": {"type": "noul", "instructions": "Does the text mention a colour?"}}, timeout=30)
        return {"ok": True, "health": h, "probe": ans["answers"]["q"].get("noul"), "ms": ans["_ms"], "url": URL}
    except Exception as e:
        return {"ok": False, "error": str(e), "url": URL}


# ============================================================================ skill catalog
def catalog() -> list[dict]:
    """Enabled skills (name, category, full description) — cached on the skills dir's mtime signature."""
    CACHE.mkdir(parents=True, exist_ok=True)
    skills_dir = ROOT / "hermes-home/skills"
    cfg = ROOT / "hermes-home/config.yaml"
    sig = f"{max((p.stat().st_mtime for p in skills_dir.rglob('SKILL.md')), default=0)}-{cfg.stat().st_mtime}"
    cf = CACHE / "catalog.json"
    if cf.exists():
        c = json.loads(cf.read_text())
        if c.get("sig") == sig:
            return c["skills"]
    out = subprocess.run([str(SKILLPORT), "list", "--json"], capture_output=True, text=True, timeout=120).stdout
    rows = json.loads(out[out.find("["):])
    skills = []
    for s in rows:
        if not s["enabled"] or s["name"] in NEVER_ROUTE:
            continue
        full = ((s.get("port") or {}).get("original_description") or s["description"] or "").strip()
        size = (Path(s["path"]) / "SKILL.md").stat().st_size
        skills.append({"name": s["name"], "category": s["category"], "desc": re.sub(r"\s+", " ", full)[:400], "chars": size})
    cf.write_text(json.dumps({"sig": sig, "skills": skills}))
    return skills


def _embed(texts: list[str]) -> list[list[float]]:
    r = httpx.post(EMBED_URL, json={"input": texts, "model": "embed"}, timeout=120, trust_env=False)
    r.raise_for_status()
    return [d["embedding"] for d in r.json()["data"]]


def _cos(a, b) -> float:
    return sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)) + 1e-9)


def shortlist(prompt: str, skills: list[dict], k: int = SHORTLIST) -> list[tuple[dict, float]]:
    """Embedding nearest neighbours (the cheap recall stage; the decision model does the precise judgment)."""
    ef = CACHE / "skill-embeddings.json"
    emb = json.loads(ef.read_text()) if ef.exists() else {}
    key = lambda s: f"{s['name']}|{s['desc']}"
    missing = [s for s in skills if key(s) not in emb]
    if missing:
        vecs = _embed([f"{s['name'].replace('-', ' ')}: {s['desc']}" for s in missing])
        emb.update({key(s): v for s, v in zip(missing, vecs)})
        ef.write_text(json.dumps(emb))
    q = _embed([prompt[:4000]])[0]
    scored = sorted(((s, _cos(q, emb[key(s)])) for s in skills), key=lambda x: -x[1])
    return scored[:k]


# ============================================================================ routers
def route_skills(prompt: str, k: int = SHORTLIST, use_embed: bool = True) -> dict:
    """Which installed skills should be preloaded for this request? -> {skills, need, probs, ms, ...}.

    Judgments asked of the decision model (one request):
      need  (noul)   does the request call for a specialised skill/workflow at all?
      pick  (choice) which listed skill fits best?          (options = embedding shortlist, or every skill)
    Decision (code): load nothing when need < SKILL_NEED_MIN; else every option with p >= SKILL_PICK_MIN,
    best first, at most SKILL_MAX skills and SKILL_MAX_CHARS of SKILL.md text."""
    t0 = time.perf_counter()
    skills = catalog()
    if use_embed:
        cands = [s for s, _ in shortlist(prompt, skills, k)]
    else:
        cands = skills[:250]
    by = {s["name"]: s for s in cands}
    questions = {
        "need": {"type": "noul",
                 "instructions": "Does `request` ask for a task where a specialised skill guide would help (a specific tool, "
                                 "file format, workflow, design or engineering practice), rather than a short chat answer?",
                 "criteria": {"true": "A concrete task: build, fix, review, design, analyse, convert, research, test something",
                              "false": "Small talk, a quick fact, or a short question answered in a few sentences"}},
        "pick": {"type": "choice", "instructions": "Which skill guide is the best fit for carrying out `request`?",
                 "criteria": {s["name"]: s["desc"][:240] for s in cands}},
    }
    ans = systemone({"request": prompt[:6000]}, questions)
    need = float(ans["answers"]["need"]["noul"])
    probs = ans["answers"]["pick"].get("probabilities") or {}
    ranked = sorted(probs.items(), key=lambda kv: -kv[1])
    chosen, chars = [], 0
    if need >= SKILL_NEED_MIN:
        for name, p in ranked:
            if p < SKILL_PICK_MIN or len(chosen) >= SKILL_MAX:
                break
            size = by.get(name, {}).get("chars", 0)
            if chosen and chars + size > SKILL_MAX_CHARS:
                continue
            chosen.append(name); chars += size
    return {"skills": chosen, "need": round(need, 3), "top": [(n, round(p, 3)) for n, p in ranked[:5]],
            "candidates": len(cands), "decide_ms": ans["_ms"], "ms": round((time.perf_counter() - t0) * 1000), "model": ans.get("model")}


_QUICK_RULE = re.compile(r"^\s*(hi+|hello+|hey+|yo|sup|hiya|howdy|good (morning|afternoon|evening|night)|gm|gn|"
                         r"thanks?( you)?( so much| a lot)?|thx|ty|cheers|how are (you|u)( doing)?|what'?s up|wassup)"
                         r"[\s!.?,:;)(*^~-]*(there|again|man|bro|dude|buddy|mate)?[\s!.?,:;)(*^~-]*$", re.I)


_CONTINUE_RULE = re.compile(r"^\s*(y|ya|ye|yes+|yeah|yep|yup|sure|ok+|okay|k|go|go ahead|go for it|do it|proceed|continue|carry on|"
                            r"keep going|please|pls|plz|try again|retry|again|next|sounds good|lgtm|ship it|approved?)"
                            r"( please| pls| plz| it| that| then)?[\s!.?,]*$", re.I)


def route_quick(prompt: str, previous_reply: str = "") -> dict:
    """Is this message trivial enough for a quick reply (one short, tool-free model call) instead of the full agent?
    -> {quick, p, via, ms}. Greetings/thanks are matched by rule (no model call); everything else is one Jev noul.
    The quick reply itself can still hand the message to the agent, so a false yes costs one short call, not a bad answer."""
    t0 = time.perf_counter()
    if previous_reply and _CONTINUE_RULE.match(prompt or ""):    # "yes" / "go ahead" after a reply = continue the work
        return {"quick": False, "p": 0.0, "via": "rule", "ms": round((time.perf_counter() - t0) * 1000)}
    if _QUICK_RULE.match(prompt or ""):
        return {"quick": True, "p": 1.0, "via": "rule", "ms": round((time.perf_counter() - t0) * 1000)}
    state = {"request": prompt[:2000]}
    if previous_reply:
        state["previous_reply"] = previous_reply[-800:]
    q = {"quick": {"type": "noul",
                   "instructions": "Can `request` be answered completely right now in a short chat reply (a few sentences) from general "
                                   "knowledge alone, with no tools? A reply that approves, continues or changes earlier work "
                                   "(see `previous_reply`, e.g. 'yes', 'go ahead', 'do it', 'try again') is NOT quick.",
                   "criteria": {"true": "Small talk, a well-known fact, a definition, a yes/no question, simple mental arithmetic, a quick opinion or one-line rewrite",
                                "false": "Needs web search or anything current (news, prices, weather, dates), the owner's files, running or "
                                         "writing real code, exact calculation, several steps, careful analysis, or a long answer"}}}
    ans = systemone(state, q, timeout=30)
    p = float(ans["answers"]["quick"]["noul"])
    return {"quick": p >= QUICK_MIN, "p": round(p, 3), "via": "jev", "ms": round((time.perf_counter() - t0) * 1000)}


EFFORT_LEVELS = ["fast", "medium", "high"]


def route_effort(prompt: str) -> dict:
    """How much thinking does this request need? -> HUD effort fast/medium/high (the model router from the video,
    but choosing the local model's reasoning budget instead of a cloud model). Unsure -> one level up."""
    q = {"size": {"type": "choice", "instructions": "How much reasoning does answering `request` correctly need?",
                  "criteria": {"fast": "Trivial: greetings, arithmetic, a short fact, a one-line rewrite",
                               "medium": "Everyday: an explanation, a summary, short writing, a small simple script",
                               "high": "Hard: multi-step reasoning, debugging or building real software, research, long or subtle analysis"}}}
    ans = systemone({"request": prompt[:6000]}, q)
    a = ans["answers"]["size"]
    pick, probs = a["choice"], a.get("probabilities") or {}
    if probs.get(pick, 0) < 0.6 and pick != "high":
        pick = EFFORT_LEVELS[EFFORT_LEVELS.index(pick) + 1]
    return {"effort": pick, "probs": {k: round(v, 3) for k, v in probs.items()}, "ms": ans["_ms"]}


# ============================================================================ CLI
def _parse_q(spec: str, kind: str, i: int) -> tuple[str, dict]:
    if kind == "noul":
        return f"noul{i}", {"type": "noul", "instructions": spec}
    name, _, opts = spec.partition("=")
    items = [o.strip() for o in opts.split(",") if o.strip()]
    if kind == "choice":
        return name, {"type": "choice", "instructions": f"Which {name} applies?", "criteria": {o: None for o in items}}
    return name, {"type": "score", "instructions": f"Rate the {name}.", "criteria": items}


def main() -> None:
    ap = argparse.ArgumentParser(prog="decide", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("ask", help="ask typed questions about a state")
    a.add_argument("state"); a.add_argument("--noul", action="append", default=[]); a.add_argument("--choice", action="append", default=[])
    a.add_argument("--score", action="append", default=[]); a.add_argument("--json", help="raw questions JSON (overrides the flags)")
    s = sub.add_parser("skills", help="which skills would be preloaded for this request"); s.add_argument("prompt")
    s.add_argument("--all", action="store_true", help="offer every skill to the choice (no embedding shortlist)")
    e = sub.add_parser("effort", help="fast/medium/high for this request"); e.add_argument("prompt")
    qk = sub.add_parser("quick", help="would this get a quick reply instead of the agent?"); qk.add_argument("prompt")
    sub.add_parser("health")
    sub.add_parser("catalog", help="print the routable skill catalog")
    args = ap.parse_args()
    try:
        if args.cmd == "ask":
            qs = json.loads(args.json) if args.json else dict(
                [_parse_q(x, "noul", i) for i, x in enumerate(args.noul)] + [_parse_q(x, "choice", i) for i, x in enumerate(args.choice)]
                + [_parse_q(x, "score", i) for i, x in enumerate(args.score)])
            out = systemone(args.state, qs)
        elif args.cmd == "skills":
            out = route_skills(args.prompt, use_embed=not args.all)
        elif args.cmd == "effort":
            out = route_effort(args.prompt)
        elif args.cmd == "quick":
            out = route_quick(args.prompt)
        elif args.cmd == "catalog":
            out = catalog()
        else:
            out = health()
    except DecideError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr); sys.exit(2)
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
