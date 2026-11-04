import json, sys, time
from playwright.sync_api import sync_playwright
NOW = time.time()
ME = {"csrf": "x", "skills": ["deep-research"], "modes": [{"name": "chat", "max_turns": 40}, {"name": "coding", "max_turns": 200, "budget_s": 43200, "skills": ["sandbox-coding"]}, {"name": "research", "max_turns": 120, "budget_s": 14400}],
      "roles": [{"name": "researcher", "class": "research", "title": "Researcher", "max_turns": 60, "budget_s": 1800, "summary": "finds and verifies sources"}], "slots": 3, "lead": "lead",
      "efforts": ["fast", "medium", "high", "xhigh", "max"], "chat_profiles": {"active": 1, "profiles": [{"id": 1, "name": "Main"}, {"id": 2, "name": "School"}]}}
def job(i, title, status, mode="chat", effort="high", ago=3600, tools=4):
    return {"id": i, "kind": "agent", "title": title, "status": status, "created": NOW - ago, "started": NOW - ago + 5, "finished": None if status == "running" else NOW - ago + 240,
            "mode": mode, "effort": effort, "relation": None, "role": None, "session_id": f"s{i}", "tokens": {"total": 14230, "input": 11800, "output": 2430}, "n_tools": tools, "profile_id": 1}
JOBS = [job(7, "Toolbox usage page for the dashboard", "running", ago=400), job(6, "Tool project recovery", "done", ago=5400), job(5, "Quadratic jump project — vertex form", "done", "chat", "medium", 86400),
        job(4, "Mccine fog render looks washed out", "done", "coding", "high", 172800, 23), job(3, "Scam aggregator feed health", "failed", "research", "xhigh", 260000, 9), job(2, "hi", "done", "chat", "fast", 300000, 0)]
PROMPT = "so it's functional now right, can you also add a page to look at the toolbox and see how many times each tool was used?"
FINAL = """Yes, it's functional, and the self-install path is now proven from inside a sandbox, not just on the host. The new **Tools** page is live.

**Self-install verified as the agent would do it.** I started a throwaway sandbox container with the same mounts and proxy the agent gets and ran the real commands:

```bash
tb install tree && tb pip tabulate
tb register tree --desc "directory tree listing" --run "tree -L 2"
```

**Usage counting.** Every use is one line in the toolbox's usage log:
- `tb` commands, with recipe lookups, installs and registrations counted separately.
- The wrappers `tbpy`, `prettypdf` and `mc-test`.
- Any registry command living in the conda env, through generated shims.

| page section | shows |
|---|---|
| KPIs | tools, uses, installs, host runs, disk, doctor |
| Table | uses in range / ever, last used, where |

One thing to know: the sandbox login profile is a file bind mount, so it must be edited in place."""
EVENTS = [{"seq": 1, "ts": NOW - 5390, "etype": "thinking", "data": "The user wants a page in the HUD. I should look at how the Skills page is built and mirror it. Let me check the API first."},
          {"seq": 2, "ts": NOW - 5380, "etype": "tool_use", "data": {"id": "c1", "name": "terminal", "input": {"command": "rg -n 'api/skills' hud/app.py | head"}}},
          {"seq": 3, "ts": NOW - 5379, "etype": "tool_result", "data": {"id": "c1", "name": "terminal", "output": "3799:@app.get(\"/api/skills\")\n3806:@app.get(\"/api/skills/search\")", "ms": 310}},
          {"seq": 4, "ts": NOW - 5370, "etype": "tool_use", "data": {"id": "c2", "name": "read_file", "input": {"path": "/mnt/data/la-toolbox/registry.json"}}},
          {"seq": 5, "ts": NOW - 5369, "etype": "tool_result", "data": {"id": "c2", "name": "read_file", "output": "{\"tools\": {...}}", "ms": 40}},
          {"seq": 6, "ts": NOW - 5300, "etype": "tool_use", "data": {"id": "c3", "name": "patch", "input": {"path": "hud/app.py", "diff": "+ toolbox api"}}},
          {"seq": 7, "ts": NOW - 5299, "etype": "tool_result", "data": {"id": "c3", "name": "patch", "output": json.dumps({"diff": "+++ a\n+1\n+2\n+3\n-1"}), "ms": 90}},
          {"seq": 8, "ts": NOW - 5200, "etype": "text", "data": FINAL}]
def detail(j, running=False):
    d = dict(j); d["params"] = {"prompt": PROMPT}
    if running:
        d["events"] = EVENTS[:5] + [{"seq": 9, "ts": NOW - 20, "etype": "thinking", "data": "Now I need to wire the chart. The usage page already has a helper for that, so I can reuse it."},
                                   {"seq": 10, "ts": NOW - 8, "etype": "tool_use", "data": {"id": "c9", "name": "terminal", "input": {"command": "node --check static/app.js"}}}]
        d["final"] = None
    else:
        d["events"] = EVENTS; d["final"] = FINAL
    return d
STATUS = {"ts": NOW, "model": {"profile": "qwen3.8-flash-next", "ctx": 262144, "agent_ctx": 65536, "slot": {"processing": False}, "tokens_predicted_total": 1000}, "health": {"model": "up"},
          "gpu": {"mem_used": 17800, "mem_total": 24576, "util": 15}, "running_all": [{"id": 7, "started": NOW - 395}], "crew": {"busy": 1, "slots": 3}, "services": {}, "host": {}}
TOOLBOX = {"days": 30, "events_total": 212, "labels": [f"2026-10-{d:02d}" if d <= 31 else f"2026-11-{d-31:02d}" for d in range(5, 35)], "per_day": [0,0,0,1,3,2,5,4,0,2,6,3,8,5,2,1,0,4,9,7,3,6,12,8,5,10,14,9,16,21],
           "top": [["rg", 48], ["tb", 40], ["ffmpeg", 22], ["tbpy", 19], ["prettypdf", 12], ["pandoc", 9], ["mc-test", 6], ["jadx", 4]], "kinds": {"cmd": 120, "tb": 50, "wrapper": 30, "install": 8, "host": 4}, "where": {"sandbox": 170, "host": 42},
           "installs_range": 8, "host_runs_range": 4, "host_runs": [{"id": "20261103-140818-8eed0c", "ts": NOW - 7000, "tool": "selftest", "exit": 0, "seconds": 0.1, "args": ""}, {"id": "20261103-141711-aa", "ts": NOW - 6500, "tool": "desktop", "exit": 0, "seconds": 0.3, "args": "windows"}],
           "recent": [{"ts": NOW - 60, "tool": "rg", "kind": "cmd", "args": "-n toolbox hud/app.py", "where": "sandbox"}, {"ts": NOW - 300, "tool": "tb", "kind": "tb", "args": "show prettypdf", "where": "sandbox"}, {"ts": NOW - 900, "tool": "prettypdf", "kind": "wrapper", "args": "report.md", "where": "sandbox"}],
           "disk": {"mamba": 8.57e9, "env": 4.99e9, "opt": 2.35e9, "cache": 1.54e9, "venvs": 1.48e9, "envs": 0.92e9, "fonts": 7e7}, "doctor": {"ts": NOW - 120, "ok": True, "tools": 49, "missing": 0, "output": "49 tools, 0 missing"},
           "other": [], "paths": {},
           "tools": [{"name": n, "desc": d, "where": w, "run": r, "created": "2026-11-03 14:17", "updated": "2026-11-03 14:17", "by": "agent", "usage": {"total": t, "range": rr, "last": NOW - l, "last_args": a, "last_where": "sandbox"}, "recipe": True, "shim": w != "host"}
                     for n, d, w, r, t, rr, l, a in [("ripgrep", "conda-forge ripgrep (main env) -> `rg`", "both", "rg", 48, 48, 60, "-n toolbox"), ("ffmpeg", "conda-forge ffmpeg (main env)", "both", "ffmpeg", 22, 22, 4000, "-i in.mp4"),
                                                   ("tbpy", "shared Python 3.12 venv: numpy pandas polars scipy sklearn matplotlib plotly duckdb playwright", "both", "tbpy script.py", 19, 19, 700, "chart.py"), ("prettypdf", "Markdown/HTML -> extravagant designed PDF (themes editorial/luxe/tech/academic/auto)", "both", "prettypdf doc.md", 12, 12, 900, "report.md"),
                                                   ("mc-test", "Launch MC client with mods, wait for title screen/crash, screenshot, report", "both", "mc-test fabric:1.21.5 mod.jar", 6, 6, 40000, "fabric:1.21.5"), ("lunar", "Open Lunar Client on the real desktop with optional test mods", "host", "host_run(tool='lunar')", 2, 2, 90000, "--minutes 15"),
                                                   ("ghidra", "Ghidra 12.2 reverse engineering (headless)", "both", "/mnt/data/la-toolbox/opt/ghidra_12.2_PUBLIC/support/analyzeHeadless", 0, 0, 0, "")]]}
def mock(route, req):
    u = req.url; p = u.split("/api/", 1)[1] if "/api/" in u else ""
    if p.startswith("me"): body = ME
    elif p.startswith("status"): body = STATUS
    elif p.startswith("jobs/7"): body = detail(JOBS[0], True)
    elif p.startswith("jobs/"): body = detail(next(j for j in JOBS if p.startswith(f"jobs/{j['id']}")))
    elif p.startswith("jobs"): body = JOBS
    elif p.startswith("skills"): body = {"skills": [{"name": "deep-research", "category": "local-agent", "enabled": True, "source": "local", "description": "multi-step research"}], "bundles": {}, "featured": ["deep-research"]}
    elif p.startswith("toolbox"): body = TOOLBOX
    elif p.startswith("mission"): body = {"root": 7, "steps": [{"title": "Read the HUD code", "status": "done"}, {"title": "Add the API", "status": "done"}, {"title": "Build the page", "status": "active"}, {"title": "Screenshots", "status": "pending"}], "members": [], "notes": [], "bg": [], "claude": [], "deliverables": [], "checkpoints": []}
    else: body = {}
    route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
def shoot(ctx, name, steps):
    pg = ctx.new_page(); errs = []
    pg.on("pageerror", lambda e: errs.append(str(e))); pg.on("console", lambda m: errs.append(m.text) if m.type == "error" and "WebSocket" not in m.text else None)
    pg.route("**/api/**", mock)
    pg.goto("http://127.0.0.1:8190/#console", wait_until="load"); pg.wait_for_timeout(900)
    for st in steps: st(pg); pg.wait_for_timeout(500)
    pg.screenshot(path=f"/tmp/hudshots/{name}.png"); print(name, "errors:", errs[:4]); pg.close()
with sync_playwright() as p:
    b = p.chromium.launch(args=["--no-proxy-server"])
    phone = b.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True)
    sel = lambda i: (lambda pg: pg.evaluate(f"document.querySelectorAll('#job-list .jrow')[{i}].click()"))
    shoot(phone, "p-chat", [sel(1), lambda pg: pg.wait_for_timeout(400), lambda pg: pg.evaluate("document.querySelector('#transcript').scrollTop = 0")])
    shoot(phone, "p-chat-bottom", [sel(1), lambda pg: pg.wait_for_timeout(400), lambda pg: pg.evaluate("document.querySelector('#transcript').scrollTop = 99999")])
    shoot(phone, "p-running", [sel(0)])
    shoot(phone, "p-sheet", [sel(1), lambda pg: pg.click("#tr-more")])
    shoot(phone, "p-chats", [sel(1), lambda pg: pg.click("#sidebar-btn")])
    shoot(phone, "p-options", [sel(1), lambda pg: pg.click("#opt-btn")])
    shoot(phone, "p-new", [lambda pg: pg.evaluate("document.querySelector('#new-thread').click()")])
    shoot(phone, "p-tools", [lambda pg: pg.evaluate("location.hash='#toolbox'"), lambda pg: pg.evaluate("document.querySelector('.rail .nav[data-page=toolbox]').click()"), lambda pg: pg.wait_for_timeout(900)])
    shoot(phone, "p-home", [lambda pg: pg.evaluate("document.querySelector('.rail .nav[data-page=overview]').click()")])
    desk = b.new_context(viewport={"width": 1400, "height": 900}, device_scale_factor=1)
    shoot(desk, "d-chat", [sel(1)])
    shoot(desk, "d-tools", [lambda pg: pg.evaluate("document.querySelector('.rail .nav[data-page=toolbox]').click()"), lambda pg: pg.wait_for_timeout(900)])
    b.close()
