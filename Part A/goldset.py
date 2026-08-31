"""Build the hand-labelling sheet for the gold standard.

Segments the corpus and emits a CSV of candidate recommendations to label by
hand against the criteria in GOLD.md. It deliberately does NOT run the gate,
the binder or the formalizer: a criterion derived from a tool's output cannot
be used to measure that tool, and an annotator who has seen the answer is no
longer independent.

    python3 goldset.py --out gold.csv --per-doc 6
    python3 goldset.py --score gold.csv          # after labelling, and only then

Sampling is stratified by document and deterministic given `--seed`, so the
sheet is reproducible and the same statement is not silently re-drawn under a
different id.
"""
import argparse
import csv
import glob
import os
import random
import sys

import segment as sg

LABELS = ("ENCODE", "PARTIAL", "REFUSE", "NOT_A_RECOMMENDATION")
REASONS = ("vague_predicate", "defeasible", "not_patient_level",
           "unsupported_temporal", "unbound_concept", "beyond_dsl")
FIELDS = ["statement_id", "doc_id", "section", "text",
          "label", "reason", "holes", "deontic", "notes", "annotator"]

def load(path: str, doc_id: str):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return sg.load_pdf(path, doc_id)
    if ext in {".html", ".htm"}:
        return sg.load_html(open(path, "rb").read(), doc_id)
    return sg.load_jats(path, doc_id)

def build(corpus: str, per_doc: int, seed: int, classify=None) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(glob.glob(os.path.join(corpus, "*"))):
        if os.path.isdir(path):
            continue
        doc_id = os.path.splitext(os.path.basename(path))[0]
        try:
            blocks = load(path, doc_id)
        except Exception as exc:                       # a corpus file we cannot read
            print(f"  {doc_id}: SKIPPED ({type(exc).__name__})", file=sys.stderr)
            continue
        stmts, man = sg.segment(blocks, doc_id, llm=classify)
        keep = [s for s in stmts if s.is_recommendation and not s.duplicate_of]
        # Sample across the whole document rather than taking the top-scoring
        # statements: the high-scoring ones are the easy ones, and a gold set
        # of easy statements measures nothing.
        rng = random.Random(f"{seed}:{doc_id}")
        picked = sorted(rng.sample(keep, min(per_doc, len(keep))),
                        key=lambda s: s.statement_id)
        print(f"  {doc_id}: {len(keep)} recommendations -> {len(picked)} sampled"
              f"  [judge: {man['classifier']}]", file=sys.stderr)
        for s in picked:
            rows.append({"statement_id": s.statement_id, "doc_id": doc_id,
                         "section": " > ".join(s.section_path)[:80],
                         "text": " ".join(s.text.split()),
                         "label": "", "reason": "", "holes": "",
                         "deontic": "", "notes": "", "annotator": ""})
    return rows

def score(path: str) -> int:
    """Validate a labelled sheet and report the distribution. Run this before
    computing agreement -- a sheet with unlabelled or ill-formed rows produces
    an agreement figure that means nothing."""
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    problems, done = [], []
    for i, r in enumerate(rows, 2):
        lab, reason = r["label"].strip(), r["reason"].strip()
        if not lab:
            continue
        done.append(r)
        if lab not in LABELS:
            problems.append(f"row {i}: label {lab!r} not one of {LABELS}")
        if lab == "REFUSE" and reason not in REASONS:
            problems.append(f"row {i}: REFUSE needs a reason, got {reason!r}")
        if lab != "REFUSE" and reason:
            problems.append(f"row {i}: reason {reason!r} on a {lab} row")
        if lab == "PARTIAL" and not r["holes"].strip():
            problems.append(f"row {i}: PARTIAL needs the hole phrases")
        # The hole must be quotable from the statement -- the same
        # anti-synthesis rule the segmenter and binder use.
        for h in filter(None, (x.strip() for x in r["holes"].split(";"))):
            if h.lower() not in r["text"].lower():
                problems.append(f"row {i}: hole {h!r} is not verbatim in the text")
        if (lab != "NOT_A_RECOMMENDATION"
                and r["deontic"].strip() not in ("obligation", "permission")):
            problems.append(f"row {i}: deontic must be obligation|permission")

    print(f"{len(done)}/{len(rows)} rows labelled")
    for field in ("label", "reason", "deontic"):
        counts: dict = {}
        for r in done:
            counts[r[field] or "(blank)"] = counts.get(r[field] or "(blank)", 0) + 1
        print(f"  {field:8s}: {counts}")
    if problems:
        print(f"\n{len(problems)} problem(s):")
        for p in problems[:40]:
            print(f"  {p}")
    return 1 if problems else 0

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", default="corpus")
    ap.add_argument("--out", default="gold.csv")
    ap.add_argument("--per-doc", type=int, default=6)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--score", metavar="CSV",
                    help="validate a labelled sheet instead of building one")
    ap.add_argument("--live", action="store_true",
                    help="judge candidates with the model instead of the offline "
                         "heuristic (costs one call per candidate)")
    a = ap.parse_args(argv)

    if a.score:
        return score(a.score)

    if os.path.exists(a.out):
        print(f"{a.out} already exists -- refusing to overwrite labelled work.",
              file=sys.stderr)
        return 1
    classify = None
    if a.live:
        import llm
        classify = llm.classify_block
    rows = build(a.corpus, a.per_doc, a.seed, classify)
    with open(a.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"\nwrote {a.out}: {len(rows)} statements to label")
    print("Read GOLD.md first. Do NOT run the pipeline on these before labelling.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
