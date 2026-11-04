#!/usr/bin/env python3
"""Run the eval suite against the live stack (./agent.sh start first).

    python3 run_evals.py --label baseline            # all tasks
    python3 run_evals.py --label baseline -k R1 -k T # only ids starting with R1 or T
    python3 run_evals.py --compare baseline tuned

Each task runs as a fresh one-shot Hermes session (`hermes chat -q --oneshot --format stream-json`).
Results go to results/<label>/: one .jsonl event log per task + summary.json + summary.md.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tasks import FIX, MEM, ROOT, TASKS, WS, Run  # noqa: E402

HERMES = ROOT / "hermes-agent/.venv/bin/hermes"
RESULTS = Path(__file__).parent / "results"


def run_task(t: dict, outdir: Path) -> dict:
    shutil.rmtree(WS, ignore_errors=True)
    WS.mkdir(parents=True)
    if t["setup"]:
        t["setup"]()
    prompt = t["prompt"]
    if "@@INLINE@@" in prompt:
        prompt = prompt.replace("@@INLINE@@", (FIX / "inline_context.txt").read_text())
    qfile = outdir / f"{t['id']}.prompt.txt"
    qfile.write_text(prompt)

    cmd = [str(HERMES), "chat", "--query-file", str(qfile), "--oneshot", "--format", "stream-json", "--yolo",
           "--max-turns", str(t["max_turns"]), "--run-budget", str(t["budget"]), "--source", "eval"]
    for s in t["skills"]:
        cmd += ["--skills", s]
    env = os.environ | {"HERMES_HOME": str(ROOT / "hermes-home")}
    t0 = time.time()
    log = outdir / f"{t['id']}.jsonl"
    try:
        p = subprocess.run(cmd, cwd=ROOT / "workspace", env=env, capture_output=True, text=True, timeout=t["budget"] + 120)
        stdout, stderr = p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        stdout, stderr = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or ""), "TIMEOUT"
    log.write_text(stdout)
    (outdir / f"{t['id']}.stderr.txt").write_text(stderr[-20000:])

    run = parse_events(stdout)
    try:
        score, note = t["check"](run)
    except Exception as e:  # a checker crash is a 0, not a harness crash
        score, note = 0.0, f"checker error: {e}"
    return {"id": t["id"], "category": t["category"], "score": round(float(score), 3), "note": note,
            "tool_calls": len(run.tools), "tool_errors": sum(1 for x in run.tools if x["is_error"]),
            "tools": [x["name"] for x in run.tools], "tokens": run.tokens,
            "wall_s": round(time.time() - t0, 1), "exit_code": run.exit_code, "error": run.error,
            "final": run.final[-1500:]}


# Checkers that only read the answer/tool log (no files on disk) can be re-graded from saved logs.
RESCORABLE = ("R", "L", "T3", "T4", "T5")


def parse_events(stdout: str) -> Run:
    run = Run()
    pending: dict[str, dict] = {}
    text_parts = []
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev["type"] == "tool_use":
            rec = {"name": ev["name"], "input": ev.get("input"), "output": "", "is_error": False}
            run.tools.append(rec)
            pending[ev.get("tool_call_id") or ev["name"]] = rec
        elif ev["type"] == "tool_result":
            rec = pending.pop(ev.get("tool_call_id") or ev["name"], None)
            if rec:
                rec["output"], rec["is_error"] = ev.get("output", ""), ev.get("is_error", False)
        elif ev["type"] == "text":
            text_parts.append(ev["text"])
        elif ev["type"] == "result":
            run.final = ev.get("text") or "".join(text_parts)
            run.tokens = ev.get("tokens", {})
            run.duration_ms = ev.get("duration_ms", 0)
            run.exit_code = ev.get("exit_code", 0)
            run.error = ev.get("error", "")
    if not run.final:
        run.final = "".join(text_parts)
    return run


def rescore(label: str) -> None:
    """Re-grade saved logs with the current checkers (after fixing a checker bug)."""
    out = RESULTS / label
    old = json.loads((out / "summary.json").read_text())["results"]
    by_id = {t["id"]: t for t in TASKS}
    for r in old:
        log = out / f"{r['id']}.jsonl"
        if r["id"].startswith(RESCORABLE) and r["id"] in by_id and log.exists():
            score, note = by_id[r["id"]]["check"](parse_events(log.read_text()))
            if round(score, 3) != r["score"]:
                print(f"{r['id']}: {r['score']} -> {round(score, 3)}  ({note})")
            r["score"], r["note"] = round(float(score), 3), note
    s = summarize(label, old)
    print(json.dumps({k: s[k] for k in ("overall", "by_category", "weakest")}, indent=2))


def summarize(label: str, results: list[dict]) -> dict:
    cats = defaultdict(list)
    for r in results:
        cats[r["category"]].append(r["score"])
    by_cat = {c: round(sum(v) / len(v), 3) for c, v in cats.items()}
    summary = {"label": label, "n": len(results), "overall": round(sum(r["score"] for r in results) / len(results), 3),
               "by_category": by_cat, "weakest": min(by_cat, key=by_cat.get), "results": results}
    md = [f"# Eval: {label}", "", f"Overall **{summary['overall']:.3f}** over {len(results)} tasks — weakest: "
          f"**{summary['weakest']}**", "", "| category | score |", "|---|---|"]
    md += [f"| {c} | {s:.3f} |" for c, s in sorted(by_cat.items())]
    md += ["", "| task | score | tools | wall s | note |", "|---|---|---|---|---|"]
    md += [f"| {r['id']} | {r['score']:.2f} | {r['tool_calls']} | {r['wall_s']} | {r['note'][:90]} |" for r in results]
    out = RESULTS / label
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    (out / "summary.md").write_text("\n".join(md) + "\n")
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="run")
    ap.add_argument("-k", action="append", default=[], help="run task ids starting with this prefix")
    ap.add_argument("--compare", nargs=2)
    ap.add_argument("--rescore", metavar="LABEL")
    a = ap.parse_args()

    if a.rescore:
        rescore(a.rescore)
        return

    if a.compare:
        s1, s2 = (json.loads((RESULTS / l / "summary.json").read_text()) for l in a.compare)
        print(f"{'':16}{a.compare[0]:>12}{a.compare[1]:>12}")
        for c in sorted(set(s1["by_category"]) | set(s2["by_category"])):
            print(f"{c:16}{s1['by_category'].get(c, 0):12.3f}{s2['by_category'].get(c, 0):12.3f}")
        print(f"{'overall':16}{s1['overall']:12.3f}{s2['overall']:12.3f}")
        return

    tasks = [t for t in TASKS if not a.k or any(t["id"].startswith(k) for k in a.k)]
    out = RESULTS / a.label
    out.mkdir(parents=True, exist_ok=True)
    mem_backup = out / "_memories_backup"
    if MEM.exists():
        shutil.rmtree(mem_backup, ignore_errors=True)
        shutil.copytree(MEM, mem_backup)
    results = []
    try:
        for t in tasks:
            if MEM.exists():  # every task starts from the pre-eval memory state
                shutil.rmtree(MEM); shutil.copytree(mem_backup, MEM)
            print(f"[{time.strftime('%H:%M:%S')}] {t['id']} ...", flush=True)
            r = run_task(t, out)
            results.append(r)
            print(f"    score={r['score']:.2f} tools={r['tool_calls']} wall={r['wall_s']}s  {r['note'][:110]}", flush=True)
    finally:
        if mem_backup.exists():
            shutil.rmtree(MEM, ignore_errors=True); shutil.copytree(mem_backup, MEM)
    s = summarize(a.label, results)
    print(json.dumps({k: s[k] for k in ("overall", "by_category", "weakest")}, indent=2))


if __name__ == "__main__":
    main()
