#!/usr/bin/env python3
"""Run one registered toolbox host script on the real desktop, confined.

The agent normally lives in a Docker sandbox that cannot see the screen. For things that only work on the
desktop (Lunar Client, a real GPU Minecraft client, screenshots of a GUI) the HUD calls this runner with the
name of a script in /mnt/data/la-toolbox/host/. The agent can write those scripts (they are auditable files in
the toolbox, logged on every run); this file and the confinement below are NOT writable by the agent
(/mnt/data/local-agent is read-only in its sandbox).

Confinement (bwrap, the root-owned copy with an AppArmor userns profile, see README "Claude Code delegation"):
- the whole filesystem read-only; writable: /mnt/data (same masks as the sandbox), /tmp, and the game dirs in RW_HOME
- secrets/state hidden: keys.env, HUD data, hermes-home, claude-runs, ~/.claude*, ssh/gpg keys, browser profiles,
  chat-app tokens, docker socket, systemd user socket, Hyprland IPC socket (hyprctl dispatch exec = escape)
- own PID namespace (cannot signal the OneBlock server, the model server or anything else), new session
It is a guard rail, not a VM: the script runs as the owner's user with the display and the network.

usage: host_run.py <tool> [args...]   (env HOST_RUN_TIMEOUT seconds, default 600)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

TOOLBOX = Path("/mnt/data/la-toolbox")
HOST_DIR = TOOLBOX / "host"
LOGS = TOOLBOX / "logs/host"
ROOT = Path("/mnt/data/local-agent")
BWRAP = "/usr/local/libexec/la-claude/bwrap"
HOME = Path("/home/user")
EMPTY = ROOT / "data/masks/empty"
EMPTY_DIR = ROOT / "data/masks/empty-dir"

RW_HOME = [".lunarclient", ".config/lunarclient", ".minecraft", ".local/share/PrismLauncher", ".cache/la-host", ".java"]
HIDE_DIRS = [ROOT / "hud/data", ROOT / "hermes-home", ROOT / "claude-runs", HOME / ".claude", HOME / ".ssh",
             HOME / ".gnupg", HOME / ".mozilla", HOME / "snap/firefox", HOME / ".config/vesktop", HOME / ".config/discord",
             HOME / ".config/gh", HOME / ".config/clash-verge", HOME / ".local/share/io.github.clash-verge-rev.clash-verge-rev",
             HOME / ".local/share/keyrings", HOME / ".aws", HOME / ".docker", HOME / ".hermes", HOME / ".config/google-chrome",
             HOME / ".config/chromium", Path("/run/user/1000/hypr"), Path("/run/user/1000/gnupg"),
             Path("/run/user/1000/keyring"), Path("/run/user/1000/systemd"), Path("/etc/cloudflared")]
HIDE_FILES = [Path("/mnt/data/keys.env"), HOME / ".claude.json", HOME / ".netrc", HOME / ".git-credentials",
              HOME / ".npmrc", HOME / ".pypirc", Path("/run/docker.sock")]


def desktop_env() -> dict[str, str]:
    env = {"HOME": str(HOME), "USER": "user", "LOGNAME": "user", "LANG": os.environ.get("LANG", "C.UTF-8"),
           "XDG_RUNTIME_DIR": "/run/user/1000", "DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-1",
           "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus", "TOOLBOX": str(TOOLBOX),
           "PATH": f"{TOOLBOX}/bin:{TOOLBOX}/shims:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:{TOOLBOX}/env/bin",
           "HTTPS_PROXY": "http://127.0.0.1:7897", "HTTP_PROXY": "http://127.0.0.1:7897",
           "https_proxy": "http://127.0.0.1:7897", "http_proxy": "http://127.0.0.1:7897",
           "NO_PROXY": "localhost,127.0.0.1", "no_proxy": "localhost,127.0.0.1"}
    return env


def bwrap_cmd(script: Path, args: list[str]) -> list[str]:
    EMPTY_DIR.mkdir(parents=True, exist_ok=True)
    c = [BWRAP, "--ro-bind", "/", "/", "--dev-bind", "/dev", "/dev", "--proc", "/proc", "--unshare-pid",
         "--new-session", "--die-with-parent", "--bind", "/tmp", "/tmp", "--bind", "/mnt/data", "/mnt/data",
         "--ro-bind", str(ROOT), str(ROOT), "--bind", str(ROOT / "workspace"), str(ROOT / "workspace")]
    for rel in RW_HOME:
        p = (HOME / rel).resolve()      # ~/.minecraft etc. may be symlinks into /mnt/data
        p.mkdir(parents=True, exist_ok=True)
        c += ["--bind", str(p), str(p)]
    for d in HIDE_DIRS:
        if d.is_dir():
            c += ["--ro-bind", str(EMPTY_DIR), str(d)]
    for f in HIDE_FILES:
        if f.exists():
            c += ["--ro-bind", str(EMPTY), str(f)]
    c += ["--chdir", str(HOST_DIR), "--", str(script), *args]
    return c


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    name = sys.argv[1]
    if "/" in name or name.startswith("."):
        print(json.dumps({"error": "tool must be a plain file name in /mnt/data/la-toolbox/host/"}))
        return 2
    script = HOST_DIR / name
    if not script.is_file():
        avail = sorted(p.name for p in HOST_DIR.iterdir() if p.is_file() and os.access(p, os.X_OK))
        print(json.dumps({"error": f"no host tool {name!r}", "available": avail}))
        return 2
    if not os.access(script, os.X_OK):
        print(json.dumps({"error": f"{script} is not executable (chmod +x it)"}))
        return 2
    timeout = int(os.environ.get("HOST_RUN_TIMEOUT", "600"))
    LOGS.mkdir(parents=True, exist_ok=True)
    log = LOGS / f"{time.strftime('%Y%m%d-%H%M%S')}-{name}.log"
    t0 = time.time()
    with open(log, "wb") as fh:
        fh.write(f"# host_run {name} {sys.argv[2:]!r} at {time.ctime()}\n".encode())
        fh.flush()
        try:
            p = subprocess.run(bwrap_cmd(script, sys.argv[2:]), env=desktop_env(), stdout=fh, stderr=subprocess.STDOUT,
                               stdin=subprocess.DEVNULL, timeout=timeout)
            code = p.returncode
        except subprocess.TimeoutExpired:
            code = "timeout"
    text = log.read_text(errors="replace")
    try:
        with open(TOOLBOX / "logs/usage.jsonl", "a") as uf:        # the HUD Toolbox page counts host runs from this
            uf.write(json.dumps({"ts": round(time.time(), 1), "tool": name, "kind": "host", "args": " ".join(sys.argv[2:])[:120],
                                 "where": "host", "exit": code, "seconds": round(time.time() - t0, 1)}) + "\n")
    except OSError:
        pass
    print(json.dumps({"tool": name, "exit": code, "seconds": round(time.time() - t0, 1), "log": str(log),
                      "tail": text[-6000:]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
