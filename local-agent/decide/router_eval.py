#!/usr/bin/env python3
"""Benchmark the skill router on labeled requests.

Each case is a realistic request plus the skills a good agent should load ("ok": any of them counts as a hit)
or none ("ok": []). Methods compared:

  clef          Clef-Flash decision model, embedding shortlist (the shipped router: decide.route_skills)
  clef-all      Clef-Flash choosing among every skill (no shortlist)
  embed         embedding nearest neighbour + a cosine threshold (no decision model)
  llm           the main local model (Qwen3.8 on :8181), thinking off, asked to name skills — "use the big model for it"

Metrics: hit@load (a correct skill was loaded), clean-none (nothing loaded when nothing should be),
wrong-load (something loaded that isn't acceptable), mean latency.

    hud/.venv/bin/python decide/router_eval.py [--methods clef,embed,llm] [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
import decide as D  # noqa: E402

CASES = [
    # --- design / frontend
    ("Build a landing page for my solar bike light startup, three pricing tiers and an FAQ.", ["landing-page-design", "frontend-design", "design-taste-frontend", "impeccable", "claude-design", "scrollcraft"]),
    ("Make the dashboard UI in app/components look less generic, it feels like a bootstrap template.", ["frontend-design", "design-taste-frontend", "impeccable", "emil-design-eng"]),
    ("Audit my site's HTML for accessibility and UX problems and list them by file and line.", ["web-design-guidelines", "impeccable", "dogfood"]),
    ("Our Next.js app re-renders the whole product list on every keystroke in the search box. Speed it up.", ["vercel-react-best-practices", "vercel-composition-patterns"]),
    ("Turn this static pricing card into a short looping animation I can post as a video.", ["design-to-motion", "hyperframes", "remotion-best-practices"]),
    ("Make a scroll-driven product story page where the phone rotates as you scroll.", ["scrollcraft", "landing-page-design", "frontend-design"]),
    # --- coding / debugging / testing
    ("The nightly billing job double-charges some customers but only in production. Find out why.", ["diagnosing-bugs", "systematic-debugging"]),
    ("Add a CSV export feature to the reports module, test-first please.", ["tdd", "test-driven-development"]),
    ("Review the changes in my last commit before I push to main.", ["code-review", "requesting-code-review", "caveman-review"]),
    ("Rename every call of getUser( to fetchUser( across the TypeScript codebase without touching strings or comments.", ["ast-grep"]),
    ("Our REST endpoint /v2/orders returns 401 even with a valid token. Help me debug it.", ["rest-graphql-debug", "diagnosing-bugs"]),
    ("Write an MCP server that exposes our Postgres read replica as tools for an agent.", ["mcp-builder"]),
    ("Click through the signup flow on localhost:3000 and tell me if the confirmation email step breaks.", ["webapp-testing", "agent-browser", "dogfood"]),
    ("Fill in the vendor form on this website and download the PDF it generates.", ["agent-browser", "webapp-testing"]),
    ("This Node service leaks memory over a few hours. Attach a debugger and find what holds the references.", ["node-inspect-debugger", "diagnosing-bugs", "systematic-debugging"]),
    ("Step through this Python script in a debugger, it crashes somewhere in parse_config.", ["python-debugpy", "diagnosing-bugs", "systematic-debugging"]),
    ("Generate developer docs with architecture diagrams for this repository.", ["code-wiki", "architecture-diagram", "codebase-inspection"]),
    ("Our modules are tangled; suggest where to deepen interfaces and draw the module boundaries.", ["improve-codebase-architecture", "codebase-design"]),
    ("Set up pre-commit hooks so prettier and the type checker run before every commit.", ["setup-pre-commit"]),
    ("Write a commit message for the staged changes.", ["caveman-commit"]),
    ("Optimise these slow Postgres queries and fix the missing indexes on the orders table.", ["supabase-postgres-best-practices"]),
    ("Write the Prisma query for users with more than 3 orders in the last month.", ["prisma-client-api"]),
    ("Clean up and simplify the code I changed today.", ["simplify-code"]),
    # --- security / RE
    ("Here is a stripped ELF crackme, recover the serial check algorithm.", ["reverse-engineering", "reverse-engineer"]),
    ("Disassemble this firmware update and figure out what changed between v1.2 and v1.3.", ["reverse-engineering", "reverse-engineer"]),
    ("We have written permission to pentest staging.example.com; do recon and test the login for injection.", ["web-pentest"]),
    ("A dependency we use got a suspicious new release on GitHub; investigate whether it was compromised.", ["oss-forensics"]),
    ("List the subdomains and certificate history for example.org.", ["domain-intel"]),
    # --- documents / data / research
    ("Convert this Word contract to PDF and extract every deadline and obligation into a table.", ["document-to-action-items", "docx", "pdf"]),
    ("Build a quarterly sales workbook with formulas and a chart from this CSV.", ["xlsx"]),
    ("Make a 10-slide deck summarising our Q3 results.", ["powerpoint"]),
    ("Find recent arXiv papers on speculative decoding and summarise the main ideas.", ["arxiv", "research", "deep-research"]),
    ("Research what the evidence says about four-day work weeks and cite sources.", ["deep-research", "research", "grounded-citations"]),
    ("Summarise this YouTube video into a blog post.", ["youtube-content"]),
    ("Reproduce the results of the paper in this repo: set up the environment and run the main experiment.", ["repo-intake-and-plan", "env-and-assets-bootstrap", "minimal-run-and-audit"]),
    ("Turn my meeting notes into decisions, owners and tickets.", ["meeting-action-items", "to-tickets"]),
    # --- planning / writing
    ("I want to rebuild our auth system. Grill me on the plan before we write any code.", ["grill-me", "grilling", "grill-with-docs"]),
    ("Break this feature spec into small implementable tickets.", ["to-tickets", "to-spec"]),
    ("Rewrite this paragraph so it doesn't sound like AI wrote it.", ["humanizer"]),
    ("Draw a hand-drawn style sequence diagram of our OAuth flow.", ["excalidraw", "architecture-diagram"]),
    ("Run and clean up the stopped Docker containers and dangling volumes on this box.", ["docker-management"]),
    # --- nothing needed
    ("What's 17 times 23?", []),
    ("hi! how's it going?", []),
    ("What's the capital of Australia?", []),
    ("Thanks, that worked.", []),
    ("Translate 'good morning' into French.", []),
    ("In one sentence, what is a hash map?", []),
    ("Remind me what the difference between TCP and UDP is.", []),
]


# ---------------------------------------------------------------- methods
def m_clef(prompt):
    return D.route_skills(prompt)["skills"]


def m_clef_all(prompt):
    return D.route_skills(prompt, use_embed=False)["skills"]


EMBED_MIN = 0.55


def m_embed(prompt):
    top = D.shortlist(prompt, D.catalog(), 2)
    return [s["name"] for s, c in top if c >= EMBED_MIN][:1]


def m_llm(prompt):
    cat = D.catalog()
    listing = "\n".join(f"- {s['name']}: {s['desc'][:120]}" for s in cat)
    msg = (f"Skills available to an agent:\n{listing}\n\nRequest: {prompt}\n\nWhich skills (at most 2) should the agent load to do this "
           "request well? Reply with JSON only: {\"skills\": [names]} — an empty list if none is needed.")
    r = httpx.post("http://127.0.0.1:8181/v1/chat/completions", json={"model": "local", "messages": [{"role": "user", "content": msg}],
                   "max_tokens": 80, "temperature": 0, "chat_template_kwargs": {"enable_thinking": False}}, timeout=180, trust_env=False)
    t = r.json()["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", t, re.S)
    names = {s["name"] for s in cat}
    return [n for n in (json.loads(m.group(0)).get("skills", []) if m else []) if n in names][:2]


METHODS = {"clef": m_clef, "clef-all": m_clef_all, "embed": m_embed, "llm": m_llm}


def run(methods: list[str]) -> dict:
    out = {}
    for name in methods:
        fn, rows = METHODS[name], []
        fn(CASES[0][0])                                   # warm caches (embeddings, prompt cache)
        for prompt, ok in CASES:
            t0 = time.perf_counter()
            try:
                got, err = fn(prompt), ""
            except Exception as e:
                got, err = [], str(e)[:120]
            ms = (time.perf_counter() - t0) * 1000
            hit = (not ok and not got) or (bool(ok) and any(g in ok for g in got))
            wrong = [g for g in got if g not in ok]
            rows.append({"prompt": prompt, "ok": ok, "got": got, "hit": hit, "wrong": wrong, "ms": round(ms), "err": err})
        pos = [r for r in rows if r["ok"]]; neg = [r for r in rows if not r["ok"]]
        out[name] = {"accuracy": round(sum(r["hit"] for r in rows) / len(rows), 3),
                     "hit@load": round(sum(r["hit"] for r in pos) / len(pos), 3),
                     "clean_none": round(sum(r["hit"] for r in neg) / len(neg), 3),
                     "wrong_loads": sum(len(r["wrong"]) for r in rows),
                     "ms_median": round(statistics.median(r["ms"] for r in rows)), "ms_p90": round(sorted(r["ms"] for r in rows)[int(0.9 * len(rows))]),
                     "errors": sum(bool(r["err"]) for r in rows), "rows": rows}
        s = out[name]
        print(f"{name:9} acc {s['accuracy']:.2f}  hit@load {s['hit@load']:.2f}  clean-none {s['clean_none']:.2f}  "
              f"wrong {s['wrong_loads']:2}  median {s['ms_median']} ms  p90 {s['ms_p90']} ms  errors {s['errors']}", flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--methods", default="clef,clef-all,embed,llm")
    ap.add_argument("--json")
    ap.add_argument("--misses", action="store_true", help="print every miss")
    a = ap.parse_args()
    res = run(a.methods.split(","))
    if a.misses:
        for m, s in res.items():
            print(f"\n== {m} misses")
            for r in s["rows"]:
                if not r["hit"] or r["wrong"]:
                    print(f"  {r['prompt'][:70]:70}  want {r['ok'][:3]}  got {r['got']}  {r['err']}")
    if a.json:
        Path(a.json).write_text(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
