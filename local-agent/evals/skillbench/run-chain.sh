#!/usr/bin/env bash
# benchmark chain started 2026-10-29: router column for round 1, then the full hard tier with all 4 conditions
cd /mnt/data/local-agent/evals/skillbench
.venv/bin/python skillbench.py run --label r1 --tasks design,bugfix,reverse,review,browser --conds router -j 2 > results/r1-router.log 2>&1
.venv/bin/python skillbench.py run --label r2 --tasks bugfix2,reverse2,review2 -j 2 > results/r2.log 2>&1
echo CHAIN_DONE >> results/r2.log
