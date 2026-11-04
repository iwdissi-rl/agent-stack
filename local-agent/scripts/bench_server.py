#!/usr/bin/env python3
"""Throughput at short and long context, using llama-server's own timings (/completion)."""
import json, sys, httpx
BASE = "http://127.0.0.1:8181"
filler = open("/mnt/data/local-agent/evals/fixtures/long_multi.txt").read()
toks = httpx.post(f"{BASE}/tokenize", json={"content": filler}, timeout=600).json()["tokens"]
toks = toks * 3  # repeat the filler to reach 200k+ contexts
print(f"filler available: {len(toks):,} tokens")
rows = []
for n in [int(a) for a in sys.argv[1:]] or [128, 16384, 32768, 65536, 110000]:
    prompt = httpx.post(f"{BASE}/detokenize", json={"tokens": toks[:n]}, timeout=600).json()["content"]
    r = httpx.post(f"{BASE}/completion", json={"prompt": prompt + "\n\nSummary of the above:", "n_predict": 256,
                   "cache_prompt": False, "temperature": 0, "ignore_eos": True}, timeout=3600).json()
    t = r["timings"]
    rows.append((n, t["prompt_n"], t["prompt_per_second"], t["predicted_n"], t["predicted_per_second"], t["prompt_ms"] / 1000))
    print(f"ctx {n:>7,}: prefill {t['prompt_per_second']:8.1f} tok/s ({t['prompt_ms']/1000:6.1f}s)  |  gen {t['predicted_per_second']:6.1f} tok/s", flush=True)
json.dump(rows, open("/mnt/data/local-agent/logs/bench-last.json", "w"))
