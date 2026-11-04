#!/usr/bin/env bash
# Foreground CPU-only embedding server (la-embed.service). CUDA hidden: with it visible,
# llama-server grabs ~5 GB VRAM for batch buffers even at -ngl 0.
export CUDA_VISIBLE_DEVICES=
exec /mnt/data/local-agent/llama.cpp/build/bin/llama-server \
  -m /mnt/data/local-agent/models/embed/Qwen3-Embedding-0.6B-Q8_0.gguf \
  --embedding --pooling last -ngl 0 -t 32 -c 8192 -b 8192 -ub 8192 \
  --alias embed --host 127.0.0.1 --port 8182
