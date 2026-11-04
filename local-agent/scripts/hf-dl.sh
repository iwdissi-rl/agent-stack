#!/usr/bin/env bash
# Resilient Hugging Face download for this box: the local proxy silently stalls long-lived
# connections, and `hf download` then hangs forever. This loops `hf download` (which resumes
# from .incomplete files) and kills it whenever the target dir stops growing for STALL_SECS.
#
#   scripts/hf-dl.sh <repo> <local-dir> [file-or-pattern ...]
#   e.g. scripts/hf-dl.sh orcarouter/Qwen3.8-27B-Uncensored-GGUF models/q27 Qwen3.8-27B-Uncensored-Q4_K_M.gguf
set -u
ROOT=/mnt/data/local-agent
REPO=$1; DEST=$2; shift 2
STALL_SECS=${STALL_SECS:-90}
export HF_HOME=$ROOT/hf-home
export HF_TOKEN=$(cat "$ROOT/hf-home/token")
export HF_HUB_DOWNLOAD_TIMEOUT=30 HF_HUB_ETAG_TIMEOUT=30
mkdir -p "$DEST"

includes=()
for f in "$@"; do includes+=(--include "$f"); done

for attempt in $(seq 1 500); do
  echo "[$(date +%T)] attempt $attempt: $REPO -> $DEST"
  hf download "$REPO" "${includes[@]}" --local-dir "$DEST" &
  pid=$!
  last=-1; still=0
  while kill -0 "$pid" 2>/dev/null; do
    sleep 15
    size=$(du -sb "$DEST" | cut -f1)
    if [ "$size" = "$last" ]; then still=$((still + 15)); else still=0; last=$size; fi
    if [ "$still" -ge "$STALL_SECS" ]; then
      echo "[$(date +%T)] stalled ${still}s at $((size / 1000000)) MB; restarting"
      kill "$pid" 2>/dev/null; sleep 2; kill -9 "$pid" 2>/dev/null
      break
    fi
  done
  if wait "$pid" 2>/dev/null; then
    echo "[$(date +%T)] DONE $REPO ($(du -sh "$DEST" | cut -f1))"
    exit 0
  fi
  sleep 5
done
echo "giving up after $attempt attempts" >&2
exit 1
