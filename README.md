# agent-stack

A fully local LLM agent stack and the toolbox that backs it. Two parts, one repo:

- `local-agent/` — the runtime: llama.cpp model server profiles, Nous Hermes Agent configuration, a web HUD (Flask) with chat profiles, crew mode (14 role prompts behind an MCP server), a Qdrant RAG server, SearXNG + Crawl4AI search services, a Claude Code delegation gate, evals and a watchdog. See `local-agent/README.md`.
- `toolbox/` — `tb`, a persistent tool manager (micromamba env, pinned binaries, shims, recipes) plus host helpers such as `mc-test` and `prettypdf`. See `toolbox/TOOLBOX.md`.

<!-- hero: docs/images/hud.png -->

## What is and is not here

This repo holds code and configuration only. Model weights, Hermes home directories, the HUD database, logs, virtualenvs, the micromamba environment and every `.env` file are excluded by `.gitignore` and are never committed. The public hostname of the reference deployment is replaced by `EXAMPLE_HOST`.

Paths in the scripts assume the stack is installed at `/mnt/data/local-agent` and `/mnt/data/la-toolbox`; adjust `ROOT` in `local-agent/hud/app.py` and the paths in `toolbox/bin/tb` if you install elsewhere.

## Quick start

```sh
# 1. services (search, page extraction, vector DB)
cd local-agent/services && docker compose up -d

# 2. model server: pick a profile, then run the agent wrapper
cat local-agent/profiles/*.env | head
./local-agent/agent.sh

# 3. HUD
python3 local-agent/hud/set_password.py
python3 local-agent/hud/app.py          # http://127.0.0.1:8190

# 4. toolbox
toolbox/bin/tb doctor
toolbox/bin/tb list
```

## Keeping the repo in sync

`./sync.sh` re-exports the code/config subset from a live install into this repo (defaults to the paths above), scrubs the hostname, and leaves state and secrets behind.

## License

MIT. See `LICENSE`. Third-party components (llama.cpp, Hermes Agent, SearXNG, Crawl4AI, Qdrant, micromamba) are not vendored here and keep their own licences.
