#!/bin/bash
# Apply llama-server profile changes without cutting a running mission: wait until no HUD job / Claude run is active and no
# llama slot is processing, then edit the active profile, restart la-model, verify, and roll back if it does not come up.
#   usage: apply-profile-when-idle.sh KEY=VALUE [KEY=VALUE ...]     (e.g. CTX=524288 UBATCH=1024)
#   force now (accepting that in-flight agent turns fail):  touch /mnt/data/local-agent/profiles/.apply-now
set -u
ROOT=/mnt/data/local-agent; LOG=$ROOT/logs/apply-profile.log; FORCE=$ROOT/profiles/.apply-now
PROF=$ROOT/profiles/$(readlink $ROOT/profiles/active.env)
log() { echo "$(date -Is) $*" | tee -a "$LOG"; }
idle() {
  local jobs; jobs=$(python3.12 - <<'PY'
import sqlite3; c=sqlite3.connect("/mnt/data/local-agent/hud/data/hud.db")
n=c.execute("select count(*) from jobs where status in ('running','queued')").fetchone()[0]
n+=c.execute("select count(*) from cruns where state='running'").fetchone()[0]
print(n)
PY
) || jobs=1
  local busy; busy=$(curl -s --noproxy '*' -m 5 http://127.0.0.1:8181/slots | python3.12 -c "import json,sys; print(sum(1 for s in json.load(sys.stdin) if s.get('is_processing')))" 2>/dev/null || echo 1)
  [ "${jobs:-1}" -eq 0 ] && [ "${busy:-1}" -eq 0 ]
}
healthy() { curl -sf --noproxy '*' -m 5 http://127.0.0.1:8181/health >/dev/null; }
smoke() { curl -s --noproxy '*' -m 120 http://127.0.0.1:8181/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"local","messages":[{"role":"user","content":"Reply with OK"}],"max_tokens":4,"reasoning_effort":"none"}' | grep -q '"content"'; }
log "waiting for idle to apply: $* (profile $PROF)"
quiet=0
while :; do
  if [ -f "$FORCE" ]; then rm -f "$FORCE"; log "forced apply requested"; break; fi
  if idle; then quiet=$((quiet+1)); [ $quiet -ge 2 ] && break; else quiet=0; fi
  sleep 30
done
cp "$PROF" "$PROF.bak-apply"
for kv in "$@"; do k=${kv%%=*}; v=${kv#*=}; grep -q "^$k=" "$PROF" && sed -i "s|^$k=.*|$k=$v|" "$PROF" || echo "$k=$v" >> "$PROF"; done
log "applied $* -> restarting la-model"
systemctl --user restart la-model
for i in $(seq 1 60); do sleep 10; healthy && break; done
if healthy && smoke; then
  log "la-model healthy with new profile: $(grep -E '^(CTX|UBATCH|PARALLEL|AGENT_CTX)=' "$PROF" | tr '\n' ' ') VRAM $(nvidia-smi --query-gpu=memory.used --format=csv,noheader)"
  curl -s --noproxy '*' -m 5 http://127.0.0.1:8190/healthz >/dev/null && log "HUD will pick up the new CTX on its next profile read (slots = 0.75*CTX/AGENT_CTX)"
  exit 0
fi
log "la-model did NOT come up healthy -> rolling back to $PROF.bak-apply"; tail -n 30 $ROOT/logs/model-server.log | grep -i -E "error|out of memory|failed" | tail -5 >> "$LOG"
cp "$PROF.bak-apply" "$PROF"; systemctl --user restart la-model
for i in $(seq 1 60); do sleep 10; healthy && { log "rolled back and healthy"; exit 1; }; done
log "ROLLBACK FAILED: la-model still down — check $ROOT/logs/model-server.log"; exit 2
