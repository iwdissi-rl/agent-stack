"""Deterministic fixtures for the eval suite. Re-run any time; output is identical (seeded).

Writes into evals/fixtures/ (pristine copies). run_evals.py copies them into the agent's
/workspace/eval before each run, so the agent can never corrupt the originals.
"""
from __future__ import annotations

import csv
import random
from pathlib import Path

FIX = Path(__file__).parent / "fixtures"
ROOT_DOCS = Path("/mnt/data/local-agent/documents/eval")
FIX.mkdir(exist_ok=True)
rng = random.Random(1234)

WORDS = ("system budget quarterly review analysis vendor shipment compliance audit schedule meeting "
         "infrastructure network latency throughput migration rollout incident postmortem roadmap "
         "milestone stakeholder procurement contract renewal forecast variance headcount onboarding "
         "training policy security patch firmware warehouse inventory logistics carrier invoice").split()


def filler_paragraph(i: int) -> str:
    n = rng.randint(60, 110)
    body = " ".join(rng.choice(WORDS) for _ in range(n))
    return f"Section {i}. {body.capitalize()}."


def long_doc(path: Path, n_paras: int, needles: dict[int, str]) -> None:
    paras = [needles.get(i, filler_paragraph(i)) for i in range(n_paras)]
    path.write_text("OPERATIONS ARCHIVE — internal log export\n\n" + "\n\n".join(paras) + "\n")


# ~60k tokens, one needle at ~70% depth (sizes checked with the model tokenizer)
long_doc(FIX / "long_report.txt", 590, {
    415: "Section 415. Facilities note: the access code for vault 7 is TANGERINE-4471. "
         "It rotates on the first Monday of each quarter.",
})

# ~100k tokens, three needles at ~10%, ~50%, ~90% depth
long_doc(FIX / "long_multi.txt", 980, {
    100: "Section 100. Personnel: the night-shift supervisor at the Rotterdam depot is Marguerite Okonjo.",
    490: "Section 490. Finance: the emergency procurement ceiling was raised to 38,500 euros.",
    880: "Section 880. Logistics: carrier code for the Lisbon route changed to KX-207.",
})

# Inline long-context prompt (~35k tokens) with one fact
paras = [filler_paragraph(i) for i in range(340)]
paras[235] = "Section 235. Reminder: the project's internal codename is BLUE HERON and its launch is set for 14 May."
(FIX / "inline_context.txt").write_text("\n\n".join(paras))

# Buggy module for the bug-fix task
(FIX / "stats.py").write_text('''"""Small statistics helpers."""


def mean(xs):
    return sum(xs) / len(xs)


def median(xs):
    s = sorted(xs)
    return s[len(s) // 2]


def mode(xs):
    """Most common value; on ties return the smallest."""
    counts = {}
    for x in xs:
        counts[x] = counts.get(x, 0) + 1
    return max(counts, key=counts.get)
''')
(FIX / "test_stats.py").write_text('''from stats import mean, median, mode


def test_mean():
    assert mean([1, 2, 3, 4]) == 2.5


def test_median_odd():
    assert median([3, 1, 2]) == 2


def test_median_even():
    assert median([4, 1, 3, 2]) == 2.5


def test_mode_tie_smallest():
    assert mode([3, 3, 1, 1, 2]) == 1
''')

# CSV for the data task
with open(FIX / "sales.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["date", "region", "product", "units", "unit_price"])
    for d in range(1, 31):
        for region in ("north", "south", "east", "west"):
            for product in ("widget", "gadget"):
                w.writerow([f"2026-09-{d:02d}", region, product, rng.randint(0, 40), {"widget": 12.5, "gadget": 30.0}[product]])

# Text for the word-frequency task
(FIX / "sample.txt").write_text(
    "The quick brown fox jumps over the lazy dog. The dog sleeps; the fox runs!\n"
    "A fox, a dog, and THE end. Quick quick QUICK.\n")

print("fixtures:", sorted(p.name for p in FIX.iterdir()))
for p in ("long_report.txt", "long_multi.txt", "inline_context.txt"):
    print(f"  {p}: {len((FIX / p).read_text()) // 4:,} tokens (approx)")

# 12-page PDF for the hard RAG task: the answer needs facts from page 4 AND page 9.
try:
    from fpdf import FPDF
    pdf = FPDF(); pdf.set_font("Helvetica", size=11)
    facts = {4: "Funding terms. Each tranche of the Meridian research grant is 1.35 million euros, "
                "released after the steering committee signs off the previous milestone.",
             9: "Payment schedule. The Meridian grant is paid out in 4 tranches between 2026 and 2028."}
    for page in range(1, 13):
        pdf.add_page()
        pdf.multi_cell(0, 7, f"Meridian programme report - page {page}\n\n" +
                       facts.get(page, " ".join(rng.choice(WORDS) for _ in range(180)).capitalize() + "."))
    pdf.output(str(ROOT_DOCS / "meridian.pdf"))
except ImportError:
    print("fpdf2 not installed in this interpreter; run with /mnt/data/local-agent/rag/.venv/bin/python")
