#!/usr/bin/env bash
# Foreground CPU-only decision-model server (la-decide.service): Cloudflare Clef-Flash (Apache-2.0, open weights)
# behind llama.cpp's TypeSafe/Jev-compatible POST /v1/systemone (llama.cpp PR #29831). CUDA hidden so it never
# takes VRAM from the main model or the desktop. Clef evaluates the whole prompt in one micro-batch, so -ub must
# cover the largest request (every routable skill as a choice option ~ 6-7k tokens).
export CUDA_VISIBLE_DEVICES=
exec /mnt/data/local-agent/llama.cpp/build/bin/llama-server \
  -m /mnt/data/local-agent/models/clef-flash/Clef-Flash-Q4_K_M.gguf \
  -ngl 0 -t 64 -c 16384 -b 16384 -ub 16384 -np 1 \
  --alias clef-flash --host 127.0.0.1 --port 8183
