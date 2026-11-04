#!/usr/bin/env bash
# One entry point for the local agent stack. Everything runs as systemd *user* units (lingering is
# on, so they start at boot without a login and restart on failure); this script just drives them.
#
#   ./agent.sh start [profile]   start/enable everything (optionally switch profile first)
#   ./agent.sh stop              stop the model stack (the web HUD stays up); watchdog pauses
#   ./agent.sh restart           restart llama-server (e.g. after editing a profile)
#   ./agent.sh chat [hermes args]   open Hermes in workspace/ (TUI), e.g. ./agent.sh chat -z "hi"
#   ./agent.sh status            health of every component
#   ./agent.sh use <profile>     switch the active model profile (profiles/<name>.env) and restart
#   ./agent.sh logs <unit>       follow a unit's log: model|embed|hud|relay|watchdog
#
# Web HUD: https://EXAMPLE_HOST/llm/  (local: http://127.0.0.1:8190/)
set -euo pipefail
ROOT=/mnt/data/local-agent
LOGS=$ROOT/logs
export HERMES_HOME=$ROOT/hermes-home
HERMES=$ROOT/hermes-agent/.venv/bin/hermes
STACK=(la-services.service la-relay.service la-embed.service la-model.service)
ALL=("${STACK[@]}" la-hud.service la-watchdog.timer)
sc() { systemctl --user "$@"; }

up() { curl -sf --noproxy '*' -m 3 "$1" >/dev/null 2>&1 && echo up || echo DOWN; }

use_profile() {
  [ -f "$ROOT/profiles/$1.env" ] || { echo "no such profile: $1"; ls "$ROOT/profiles"/*.env; exit 1; }
  ln -sfn "$1.env" "$ROOT/profiles/active.env"
  local ctx actx; ctx=$(grep -oP '^CTX=\K[0-9]+' "$ROOT/profiles/$1.env")
  actx=$(grep -oP '^AGENT_CTX=\K[0-9]+' "$ROOT/profiles/$1.env"); actx=${actx:-$ctx}   # one agent's share of the -kvu pool
  sed -i "s/^  context_length: .*/  context_length: $actx    # per-agent window = profile AGENT_CTX (pool CTX=$ctx is shared by all slots)/" "$HERMES_HOME/config.yaml"
  echo "active profile -> $1"
}

wait_model() {
  echo -n "waiting for llama-server"
  for _ in $(seq 1 900); do
    [ "$(up http://127.0.0.1:8181/health)" = up ] && { echo " ready"; return; }
    sc is-active --quiet la-model.service || { echo " FAILED"; tail -20 "$LOGS/model-server.log"; exit 1; }
    echo -n "."; sleep 1
  done
  echo " still loading (check ./agent.sh logs model)"
}

status() {
  printf '%-24s %s\n' "model server :8181" "$(up http://127.0.0.1:8181/health)  [$(sc is-active la-model.service)]"
  printf '%-24s %s\n' "embeddings :8182" "$(up http://127.0.0.1:8182/health)  [$(sc is-active la-embed.service)]"
  printf '%-24s %s\n' "searxng :8888" "$(up http://127.0.0.1:8888/healthz)"
  printf '%-24s %s\n' "crawl4ai :11235" "$(up http://127.0.0.1:11235/health)"
  printf '%-24s %s\n' "qdrant :6333" "$(up http://127.0.0.1:6333/healthz)"
  printf '%-24s %s\n' "sandbox relay :7898" "$(ss -ltn 'sport = :7898' | grep -q LISTEN && echo up || echo DOWN)  [$(sc is-active la-relay.service)]"
  printf '%-24s %s\n' "web HUD :8190" "$(up http://127.0.0.1:8190/healthz)  [$(sc is-active la-hud.service)]"
  printf '%-24s %s\n' "watchdog timer" "$(sc is-active la-watchdog.timer)$([ -f "$LOGS/STOPPED" ] && echo ' (paused: stack stopped)')"
  printf '%-24s %s\n' "active profile" "$(readlink "$ROOT/profiles/active.env")"
  printf '%-24s %s\n' "GPU memory" "$(nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader)"
}

case "${1:-}" in
  start)   [ -n "${2:-}" ] && use_profile "$2"
           rm -f "$LOGS/STOPPED"
           sc daemon-reload; sc enable --now "${ALL[@]}" >/dev/null 2>&1 || sc start "${ALL[@]}"
           wait_model; status ;;
  stop)    touch "$LOGS/STOPPED"; sc stop la-model.service la-embed.service la-relay.service la-services.service; status ;;
  restart) sc restart la-model.service; wait_model ;;
  chat)    [ "$(up http://127.0.0.1:8181/health)" = up ] || "$0" start
           mkdir -p "$ROOT/workspace"; cd "$ROOT/workspace"; exec "$HERMES" "${@:2}" ;;
  status)  status ;;
  use)     use_profile "${2:?profile name}"; sc restart la-model.service; wait_model ;;
  logs)    declare -A F=([model]=model-server.log [embed]=embed-server.log [hud]=hud.log [relay]=proxy-bridge.log [watchdog]=watchdog.log)
           exec tail -n 100 -f "$LOGS/${F[${2:-model}]}" ;;
  *)       sed -n '2,15p' "$0"; exit 1 ;;
esac
