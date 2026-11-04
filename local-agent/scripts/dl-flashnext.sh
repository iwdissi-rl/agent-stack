#!/usr/bin/env bash
# Qwen3.8-Flash-Next abliterated (huihui-ai, open) UD-Q4_K_XL ≈ 112 GB, parallel + sha256-verified.
set -e
cd /mnt/data/local-agent
R=huihui-ai/Huihui-Qwen3.8-Flash-Next-abliterated-GGUF
D=models/qwen3.8-flash-next
for f in UD-Q4_K_XL/Qwen3.8-Flash-Next-UD-Q4_K_XL-0000{1,2,3,4}-of-00004.gguf mmproj-Qwen3.8-Flash-Next-BF16.gguf; do
  rag/.venv/bin/python scripts/pdl.py "$R" "$f" "$D" --workers 16
done
echo ALL-SHARDS-DONE
