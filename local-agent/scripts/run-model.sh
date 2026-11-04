#!/usr/bin/env bash
# Foreground llama-server for the active profile (run by systemd: la-model.service).
set -euo pipefail
ROOT=/mnt/data/local-agent
PROFILE=${1:-$ROOT/profiles/active.env}
# shellcheck disable=SC1090
source "$PROFILE"
args=(-m "$MODEL" -c "$CTX" -ngl "$NGL" -fa on -ctk "$KV_TYPE" -ctv "$KV_TYPE"
      -b "$BATCH" -ub "$UBATCH" -np "${PARALLEL:-1}" -t "$THREADS"
      --temp "$TEMP" --top-p "$TOP_P" --top-k "$TOP_K" --min-p "$MIN_P"
      --jinja --alias local --host 127.0.0.1 --port 8181 --metrics --slots)
[ "${N_CPU_MOE:-0}" -gt 0 ] && args+=(--n-cpu-moe "$N_CPU_MOE")
# PARALLEL>1 = several agents at once (crew mode). Unified KV: all slots share ONE pool of CTX tokens,
# so VRAM stays the same as a single slot and any one sequence may still grow to the full context.
[ "${PARALLEL:-1}" -gt 1 ] && args+=(-kvu)
[ -n "${MMPROJ:-}" ] && [ -f "$MMPROJ" ] && args+=(--mmproj "$MMPROJ")
# EXTRA_ARGS is only editable on disk (the HUD never writes it)
# shellcheck disable=SC2206
[ -n "${EXTRA_ARGS:-}" ] && args+=($EXTRA_ARGS)
exec "$ROOT/llama.cpp/build/bin/llama-server" "${args[@]}"
