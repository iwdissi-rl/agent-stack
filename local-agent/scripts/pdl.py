#!/usr/bin/env python3
"""Parallel, resumable, checksum-verified Hugging Face file downloader for this box.

Single connections through the local proxy crawl (~1-2 MB/s) and stall; many parallel
ranged requests to hf-mirror.com aggregate to 10+ MB/s. Each chunk is its own part file,
so a restart only re-fetches unfinished chunks. The result is verified against the LFS
sha256 published by the Hugging Face API.

    scripts/pdl.py <repo> <file-in-repo> <dest-dir> [--workers 12] [--endpoint https://hf-mirror.com]
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

CHUNK = 128 * 1024 * 1024
TOKEN_FILE = Path("/mnt/data/local-agent/hf-home/token")


def headers() -> dict:
    tok = os.environ.get("HF_TOKEN") or (TOKEN_FILE.read_text().strip() if TOKEN_FILE.exists() else "")
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def file_meta(repo: str, fname: str) -> tuple[int, str | None]:
    """Size and LFS sha256 from the official API (metadata is small; proxy is fine for it)."""
    r = httpx.post(f"https://huggingface.co/api/models/{repo}/paths-info/main", data={"paths": fname},
                   headers=headers(), timeout=60)
    r.raise_for_status()
    info = r.json()[0]
    lfs = info.get("lfs") or {}
    return int(lfs.get("size") or info["size"]), lfs.get("oid")


def fetch_chunk(url: str, part: Path, start: int, end: int, progress: list, lock: threading.Lock) -> None:
    want = end - start + 1
    for attempt in range(200):
        have = part.stat().st_size if part.exists() else 0
        if have >= want:
            return
        try:
            h = headers() | {"Range": f"bytes={start + have}-{end}"}
            with httpx.stream("GET", url, headers=h, timeout=httpx.Timeout(30, read=30), follow_redirects=True) as r:
                if r.status_code not in (200, 206):
                    raise RuntimeError(f"HTTP {r.status_code}")
                with open(part, "ab") as f:
                    for block in r.iter_bytes(1 << 20):
                        f.write(block)
                        with lock:
                            progress[0] += len(block)
        except Exception as exc:  # stall/reset/5xx: back off and resume this chunk
            time.sleep(min(30, 2 + attempt))
            if attempt % 10 == 9:
                print(f"  chunk @{start}: retry {attempt + 1} ({exc})", flush=True)
    raise RuntimeError(f"chunk {start}-{end} failed")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo"); ap.add_argument("file"); ap.add_argument("dest")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--endpoint", default="https://hf-mirror.com")
    a = ap.parse_args()

    dest = Path(a.dest); dest.mkdir(parents=True, exist_ok=True)
    out = dest / Path(a.file).name
    size, sha = file_meta(a.repo, a.file)
    if out.exists() and out.stat().st_size == size:
        print(f"{out} already complete ({size / 1e9:.2f} GB)"); return
    parts_dir = dest / f".{out.name}.parts"; parts_dir.mkdir(exist_ok=True)
    url = f"{a.endpoint}/{a.repo}/resolve/main/{a.file}"
    ranges = [(s, min(s + CHUNK, size) - 1) for s in range(0, size, CHUNK)]
    done0 = sum(min((parts_dir / f"{i:05d}").stat().st_size, e - s + 1)
                for i, (s, e) in enumerate(ranges) if (parts_dir / f"{i:05d}").exists())
    print(f"{a.file}: {size / 1e9:.2f} GB in {len(ranges)} chunks, {done0 / 1e9:.2f} GB already on disk, "
          f"{a.workers} workers via {a.endpoint}", flush=True)

    progress, lock, t0 = [0], threading.Lock(), time.time()
    stop = threading.Event()

    def report() -> None:
        while not stop.wait(30):
            got = done0 + progress[0]
            rate = progress[0] / max(1, time.time() - t0)
            eta = (size - got) / rate / 60 if rate else float("inf")
            print(f"  {got / 1e9:6.2f}/{size / 1e9:.2f} GB  {rate / 1e6:5.1f} MB/s  ETA {eta:5.0f} min", flush=True)

    threading.Thread(target=report, daemon=True).start()
    with ThreadPoolExecutor(a.workers) as pool:
        futs = [pool.submit(fetch_chunk, url, parts_dir / f"{i:05d}", s, e, progress, lock) for i, (s, e) in enumerate(ranges)]
        for f in as_completed(futs):
            f.result()
    stop.set()

    print("assembling + verifying sha256 ...", flush=True)
    h = hashlib.sha256()
    tmp = out.with_suffix(out.suffix + ".tmp")
    with open(tmp, "wb") as w:
        for i in range(len(ranges)):
            p = parts_dir / f"{i:05d}"
            with open(p, "rb") as r:
                while block := r.read(16 << 20):
                    h.update(block); w.write(block)
    if tmp.stat().st_size != size or (sha and h.hexdigest() != sha):
        sys.exit(f"VERIFY FAILED: size {tmp.stat().st_size} vs {size}, sha {h.hexdigest()} vs {sha} (parts kept)")
    tmp.rename(out)
    for p in parts_dir.iterdir():
        p.unlink()
    parts_dir.rmdir()
    print(f"DONE {out} ({size / 1e9:.2f} GB, sha256 OK) in {(time.time() - t0) / 60:.1f} min", flush=True)


if __name__ == "__main__":
    main()
