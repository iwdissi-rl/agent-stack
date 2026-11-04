#!/usr/bin/env bash
# Re-export code/config from the live install into this repo (no state, no secrets).
# Usage: ./sync.sh [/path/to/local-agent] [/path/to/la-toolbox]
set -euo pipefail
S=${1:-/mnt/data/local-agent}; X=${2:-/mnt/data/la-toolbox}; D=$(cd "$(dirname "$0")" && pwd)
rsync -a --delete --prune-empty-dirs \
  --exclude='*.bak' --exclude='*.bak-*' --exclude='__pycache__' --exclude='*.pyc' --exclude='.venv' --exclude='*.db' --exclude='*.db-*' --exclude='.env' --exclude='*.token' --exclude='auth.json' \
  --include='/agent.sh' --include='/README.md' \
  --include='/agents/***' --include='/claude/***' --include='/crew/***' --include='/patches/***' --include='/profiles/***' --include='/scripts/***' \
  --include='/bin/' --include='/bin/skillport' \
  --include='/decide/' --include='/decide/decide.py' --include='/decide/router_eval.py' --include='/decide/eval-baselines.json' \
  --include='/evals/' --include='/evals/tasks.py' --include='/evals/run_evals.py' --include='/evals/make_fixtures.py' --include='/evals/fixtures/***' --include='/evals/skillbench/' --include='/evals/skillbench/skillbench.py' --include='/evals/skillbench/run-chain.sh' \
  --include='/hud/' --include='/hud/app.py' --include='/hud/set_password.py' --include='/hud/tests_mock_screens.py' --include='/hud/static/***' \
  --include='/rag/' --include='/rag/rag_server.py' \
  --include='/services/' --include='/services/docker-compose.yml' --include='/services/searxng/***' --include='/services/crawl4ai-patch/***' \
  --exclude='*' "$S/" "$D/local-agent/"
rsync -a --delete --prune-empty-dirs \
  --exclude='*.bak' --exclude='*.bak-*' --exclude='__pycache__' --exclude='*.pyc' \
  --include='/TOOLBOX.md' --include='/registry.json' \
  --include='/bin/' --include='/bin/tb' --include='/bin/tbpy' --include='/bin/mc-test' --include='/bin/prettypdf' \
  --include='/shims/***' --include='/recipes/***' --include='/host/***' --include='/pdf/***' \
  --exclude='*' "$X/" "$D/toolbox/"
mkdir -p "$D/toolbox/skills"; touch "$D/toolbox/skills/.gitkeep"
# scrub the public hostname
grep -rlI 'iwd\.fish' "$D/local-agent" "$D/toolbox" 2>/dev/null | xargs -r sed -i 's#https://www\.iwd\.fish#https://EXAMPLE_HOST#g; s#https://iwd\.fish#https://EXAMPLE_HOST#g; s#www\.iwd\.fish#EXAMPLE_HOST#g; s#iwd\.fish#EXAMPLE_HOST#g'
echo "synced into $D"; git -C "$D" status --short | head
