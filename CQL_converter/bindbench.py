"""Terminology binding benchmark over the gold-set statements.

One number: of the clinical phrases the pipeline tries to look up, how many
resolve to the right code. It exists because binding failures were being found
one document at a time -- each run surfaced one or two, they were fixed, and
the next document surfaced two more. The benchmark shows them all at once, so
the PATTERN is visible instead of the instance, and so a retrieval change can
be checked against everything that already works rather than against the one
case that motivated it.

    python3 bindbench.py --build          # run the binder, write bindbench.csv
    python3 bindbench.py --score          # rate, and the failures by category

The rows come from the 45 gold statements (GOLD.md), so binding and
formalizability are measured on the SAME sample: for any statement you can ask
whether it failed because of the guideline or because we could not find a code,
and both halves are measured rather than estimated.

Hand-fill `expected_code` on the failures:
    a code           -- a code exists and we missed it   -> OUR bug
    NONE             -- no code exists in any vocabulary -> real residue
    (blank)          -- not yet adjudicated
"""
import argparse
import concurrent.futures as cf
import csv
import os
import sys

import bind

FIELDS = ["phrase", "kind", "unit", "n_statements", "example_statement",
          "bound", "system", "code", "display", "selector_reason",
          "expected_code", "verdict", "notes"]

def glossaries(rows, corpus="corpus") -> dict:
    """{doc_id: abbreviation glossary}. The pipeline threads this into every
    lookup (`bind_statement(..., doc_abbrevs=...)`), so a benchmark that omits
    it measures a retrieval path the product does not use: `APLA` returned no
    candidates at all purely because the abbreviation was never expanded."""
    import segment as sg
    out = {}
    for doc_id in sorted({r["doc_id"] for r in rows}):
        for ext in (".xml", ".html", ".pdf"):
            path = os.path.join(corpus, doc_id + ext)
            if not os.path.exists(path):
                continue
            blocks = (sg.load_pdf(path, doc_id) if ext == ".pdf" else
                      sg.load_html(open(path, "rb").read(), doc_id) if ext == ".html"
                      else sg.load_jats(path, doc_id))
            out[doc_id] = sg.extract_abbreviations(blocks)
            break
    return out

def _code_of(expected: str) -> str:
    """'SNOMED:274769005' -> '274769005'. The vocabulary prefix is for a human
    reading the sheet; comparison is on the code."""
    return expected.split(":")[-1].strip()

def retrieval_recall(path: str, offline_ok=True) -> int:
    """Does the EXPECTED code appear in the candidate set at all?

    Retrieval is what a query change controls; selection is the model's job.
    Separating them gives a metric that is free, deterministic and re-runnable,
    so a retrieval change can be justified by a number instead of by the one
    example that motivated it."""
    import demo_shim
    index, _, _ = demo_shim.wire(True)
    rows = [r for r in csv.DictReader(open(path, encoding="utf-8"))
            if r["verdict"] == "ours" and _code_of(r["expected_code"]) != "NONE"]
    gloss = glossaries(list(csv.DictReader(open("gold.csv", encoding="utf-8"))))
    hit = 0
    for r in rows:
        want = _code_of(r["expected_code"])
        m = bind.Mention(r["phrase"], r["kind"], 0, len(r["phrase"]),
                         r["unit"] or None, None)
        ab = {}
        for g in gloss.values():
            ab.update(g)
        cands = bind.search_all(index, m, 8, ab)
        got = any(c.concept.code == want for c in cands)
        hit += got
        print(f"  {'HIT ' if got else 'MISS'}  {r['phrase'][:34]:36s} want {want:14s}"
              f"  top: {cands[0].concept.term[:34] if cands else '(none)'}")
    print(f"\nretrieval recall on the fixable failures: {hit}/{len(rows)}")
    return 0

def phrases_from_gold(path: str, mention_llm) -> dict:
    """Distinct concept phrases across the real recommendations.

    NOT_A_RECOMMENDATION rows are skipped: binding a heading or an evidence
    summary measures nothing, and including them would inflate the denominator
    with lookups the pipeline should never perform."""
    rows = [r for r in csv.DictReader(open(path, encoding="utf-8"))
            if r["label"] != "NOT_A_RECOMMENDATION"]
    out: dict = {}

    def mentions(r):
        try:
            return r, bind.propose_mentions(r["text"], mention_llm)
        except Exception as exc:
            print(f"  {r['statement_id']}: mention extraction failed "
                  f"({type(exc).__name__})", file=sys.stderr)
            return r, []

    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        for r, ms in pool.map(mentions, rows):
            for m in ms:
                key = " ".join(m.text.lower().split())
                slot = out.setdefault(key, {"phrase": m.text, "kind": m.kind,
                                            "unit": m.unit or "",
                                            "n": 0, "example": r["statement_id"],
                                            "text": r["text"], "mention": m})
                slot["n"] += 1
    print(f"  {len(rows)} statements -> {len(out)} distinct phrases",
          file=sys.stderr)
    return out

def build(gold: str, out_path: str, live: bool) -> int:
    import demo_shim
    index, mention_llm, select_llm = demo_shim.wire(live)
    found = phrases_from_gold(gold, mention_llm)

    gloss = glossaries(list(csv.DictReader(open(gold, encoding="utf-8"))))
    merged = {}
    for g in gloss.values():
        merged.update(g)
    print(f"  {len(merged)} abbreviations across the corpus glossaries",
          file=sys.stderr)

    def do(item):
        key, slot = item
        try:
            r = bind.bind(slot["mention"], slot["text"], index, select_llm,
                          doc_abbrevs=merged)
        except Exception as exc:
            return key, slot, None, f"{type(exc).__name__}: {exc}"
        return key, slot, r, ""

    rows = []
    with cf.ThreadPoolExecutor(max_workers=6) as pool:
        for key, slot, r, err in pool.map(do, sorted(found.items())):
            row = {"phrase": slot["phrase"], "kind": slot["kind"],
                   "unit": slot["unit"], "n_statements": slot["n"],
                   "example_statement": slot["example"],
                   "expected_code": "", "verdict": "", "notes": err}
            if isinstance(r, bind.Binding):
                row.update(bound="yes", system=r.system.rstrip("/").split("/")[-1],
                           code=r.code, display=r.display,
                           selector_reason=(r.reason or "")[:160])
            else:
                row.update(bound="no", system="", code="", display="",
                           selector_reason=(getattr(r, "reason_text", "") or err)[:160])
            rows.append(row)

    rows.sort(key=lambda x: (x["bound"], x["phrase"].lower()))
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader(); w.writerows(rows)
    ok = sum(1 for r in rows if r["bound"] == "yes")
    print(f"\nwrote {out_path}: {ok}/{len(rows)} bound "
          f"({ok/max(1,len(rows)):.0%})")
    print("Hand-fill `expected_code` on the unbound rows: a code (we missed it), "
          "or NONE (no code exists).")
    return 0

def score(path: str) -> int:
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    ok = [r for r in rows if r["bound"] == "yes"]
    bad = [r for r in rows if r["bound"] != "yes"]
    # Three buckets, not two. "no code in our index" and "no code anywhere" are
    # different findings with different fixes, and collapsing them would make
    # an incomplete VOCABULARY LOAD look like a property of the guideline.
    ours = [r for r in bad if r["verdict"] == "ours"]
    vocab = [r for r in bad if r["verdict"] == "vocab_gap"]
    real = [r for r in bad if r["verdict"] == "residue"]
    todo = [r for r in bad if not r["verdict"].strip()]

    print(f"binding: {len(ok)}/{len(rows)} = {len(ok)/max(1,len(rows)):.0%}")
    print(f"  OUR RETRIEVAL   : {len(ours):2d}   the code is in our index and the query missed it")
    print(f"  VOCABULARY GAP  : {len(vocab):2d}   the code is not in the releases we load")
    print(f"  REAL RESIDUE    : {len(real):2d}   no faithful code in any standard vocabulary")
    print(f"  reachable ceiling: {len(ok)+len(ours)}/{len(rows)} = "
          f"{(len(ok)+len(ours))/max(1,len(rows)):.0%} by fixing retrieval alone")
    if todo:
        print(f"  not yet adjudicated                    : {len(todo)}")
    print()
    for label, group in (("OURS -- retrieval", ours), ("VOCABULARY GAP", vocab),
                         ("REAL RESIDUE", real), ("UNADJUDICATED", todo)):
        if not group:
            continue
        print(f"  --- {label}")
        for r in group:
            print(f"    {r['phrase'][:38]:40s} [{r['kind']:10s}] "
                  f"{r['selector_reason'][:60]}")
        print()
    # A phrase used in several statements costs more when it fails.
    heavy = sorted(bad, key=lambda r: -int(r["n_statements"] or 1))[:5]
    if heavy and int(heavy[0]["n_statements"] or 1) > 1:
        print("  most costly failures (used in the most statements):")
        for r in heavy:
            if int(r["n_statements"] or 1) > 1:
                print(f"    x{r['n_statements']}  {r['phrase']}")
    return 0

def ablate(path: str, out_path: str | None = None) -> int:
    """Retrieval ablation for the semantic index (RESULTS.md appendix A).

    Task: given a phrase as a guideline writes it, does the correct terminology
    code appear in the top-k candidates? Recall@k is the right metric because
    retrieval's job is to OFFER the concept -- the selector decides, and a
    concept never offered can never be selected.

    Evaluated on every row of the benchmark with a known target: the code the
    binder actually chose (75 rows) plus the hand-adjudicated expected code for
    the failures (12). Includes the successes deliberately -- an ablation that
    only measures the cases a change was built for cannot show what it broke."""
    import time
    import bind as b
    rows = [r for r in csv.DictReader(open(path, encoding="utf-8"))
            if (r["code"] or "").strip()
            or (r["expected_code"].strip() and r["verdict"] == "ours")]
    targets = [(r["phrase"], r["kind"], (r["code"] or "").strip()
                or _code_of(r["expected_code"]), r["verdict"]) for r in rows]
    ix = bind.SqliteIndex(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "terms.db"))
    gloss = {}
    for g in glossaries(list(csv.DictReader(open("gold.csv", encoding="utf-8")))).values():
        gloss.update(g)

    def recall(semantic_on: bool, k: int = 8):
        ix.semantic = semantic_on
        hit = miss_rows = 0
        t0 = time.time()
        for phrase, kind, want, _v in targets:
            m = bind.Mention(phrase, kind, 0, len(phrase))
            got = bind.search_all(ix, m, k, gloss)
            if any(c.concept.code == want for c in got):
                hit += 1
            else:
                miss_rows += 1
        return hit, len(targets), time.time() - t0

    lines = []
    for label, on in (("lexical only", False), ("lexical + semantic", True)):
        hit, n, dt = recall(on)
        lines.append(f"  {label:22s} recall@8 = {hit:3d}/{n} = {hit/n:.1%}"
                     f"   {dt:5.1f}s  ({dt/n*1000:.0f} ms/lookup)")
    ix.semantic = True
    print("\n".join(lines))
    if out_path:
        open(out_path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    return 0

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gold", default="gold.csv")
    ap.add_argument("--out", default="bindbench.csv")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--ablate", action="store_true",
                    help="lexical vs lexical+semantic recall@8 over every row "
                         "with a known target")
    ap.add_argument("--report", help="write the ablation table here")
    ap.add_argument("--retrieval", action="store_true",
                    help="does the expected code reach the candidate set? "
                         "offline, deterministic, no model calls")
    ap.add_argument("--offline", action="store_true",
                    help="fixture index and heuristics; measures nothing real")
    a = ap.parse_args(argv)
    if a.build:
        if os.path.exists(a.out):
            print(f"{a.out} exists -- refusing to overwrite adjudicated work.",
                  file=sys.stderr)
            return 1
        return build(a.gold, a.out, not a.offline)
    if a.ablate:
        return ablate(a.out, a.report)
    if a.retrieval:
        return retrieval_recall(a.out)
    if a.score:
        return score(a.out)
    ap.error("pass --build or --score")

if __name__ == "__main__":
    raise SystemExit(main())
