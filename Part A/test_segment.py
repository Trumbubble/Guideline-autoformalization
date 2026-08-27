"""Regression tests for the segmenter. Each test names the failure it prevents.

Run: python3 test_segment.py    (no pytest dependency)

The PDF fixture is GENERATED inside the test rather than committed, so box
recovery is exercised deterministically and no copyrighted guideline PDF ever
enters the repo.
"""
import json
import os
import tempfile

from lxml import etree

import segment as sg
from segment import (Block, Provenance, Text, blocks_to_units, dedupe, is_label_only,
                     load_jats, load_pdf, prefilter, read_jsonl, recover_boxes,
                     segment as run_segment, split_sentences, statement_from_dict,
                     statement_to_dict, write_jsonl)

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "guideline.jats.xml")

def _blocks(): return load_jats(FIXTURE, "ckd")
def _run(**kw): return run_segment(_blocks(), "ckd", **kw)
def _norm(s): return Text(s).s

# The gold set: every patient-level recommendation a human finds in the fixture.
# Recall against this is the number that matters -- a missed recommendation is
# invisible downstream and silently shrinks the formalizability denominator.
GOLD = [
    "We recommend treatment with an ACE inhibitor or ARB in adults with",
    "Serum potassium and creatinine should be checked within 14 days",
    "the choice of renal replacement therapy should be discussed",
    "eGFR below 30 mL/min/1.73m2; Action: Refer to nephrology",
    "ACR 300 mg/g or higher; Action: Refer to nephrology",
    "eGFR 30-59 mL/min/1.73m2 and stable; Action: Monitor annually",
    "Measure eGFR and ACR at least annually",
    "Offer a repeat eGFR within 14 days after starting an ACE inhibitor",
    "Do not offer routine bicarbonate testing",
]
# Prose a segmenter must NOT promote: the guideline talking about itself.
ANTI_GOLD = [
    "Smith et al. reported a prevalence",
    "We searched MEDLINE and Embase",
    "The evidence supporting this recommendation was drawn from four",
    "The authors declare",
]

# ------------------------------------------------------------------ loader

def test_jats_finds_every_structural_kind():
    kinds = {b.kind for b in _blocks()}
    assert {"boxed_text", "table_row", "section_text", "list_item",
            "heading", "figure"} <= kinds, kinds

def test_locators_round_trip_to_the_same_element():
    """Provenance is worthless if the locator cannot re-find the span."""
    tree = etree.parse(FIXTURE)
    for b in _blocks():
        found = tree.xpath(b.prov.locator)
        assert len(found) == 1, b.prov.locator
        assert _norm("".join(found[0].itertext()))[:20] in _norm(b.text) or \
               _norm(b.text)[:20] in _norm("".join(found[0].itertext()))

def test_nested_paragraphs_are_not_emitted_twice():
    """<list-item><p>..</p></list-item> is one statement, not two."""
    texts = [_norm(b.text) for b in _blocks()]
    assert len(texts) == len(set(texts)), "a block was emitted more than once"

def test_boxed_label_goes_to_the_section_path_not_the_sentence():
    box = next(b for b in _blocks() if b.kind == "boxed_text")
    assert "Recommendation 3.1" in " > ".join(box.section_path)
    assert not box.text.strip().startswith("Recommendation 3.1")

def test_adjacent_paragraphs_keep_their_separator():
    """Joining child itertexts drops inter-element whitespace and welds two
    sentences into one unsplittable run."""
    box = next(b for b in _blocks() if b.kind == "boxed_text")
    assert "intervals.Strong" not in _norm(box.text)

# ------------------------------------------------------------- provenance

def test_statement_offsets_point_at_the_real_source_text():
    """The whole fidelity argument rests on this: text[char_start:char_end] in
    the source element must BE the statement."""
    tree = etree.parse(FIXTURE)
    stmts, _ = _run()
    for s in stmts:
        raw = "".join(tree.xpath(s.prov.locator)[0].itertext())
        if s.kind == "table_row":
            # Assembled from cells, so no contiguous slice is the rule. Every
            # VALUE must still be verbatim from the row (see module docstring).
            for cell in s.text.split("; "):
                value = cell.split(": ", 1)[-1]
                assert _norm(value) in _norm(raw), f"{s.statement_id}: {value!r}"
            continue
        sliced = _norm(raw[s.prov.char_start:s.prov.char_end])
        assert _norm(s.text) in sliced or sliced in _norm(s.text), \
            f"{s.statement_id}: {sliced[:60]!r} vs {_norm(s.text)[:60]!r}"

def test_normalization_offsets_survive_dehyphenation():
    t = Text("adults with chronic kid-\n  ney disease and 12,000 partici-\npants.")
    assert "kidney disease" in t.s and "participants" in t.s
    i = t.s.index("kidney")
    a, b = t.raw_span(i, i + 6)
    assert t.raw[a:b].replace("-\n", "").replace(" ", "") == "kidney"

# -------------------------------------------------------------- sentences

def test_clinical_abbreviations_do_not_split_sentences():
    s = ("Give 2.5 mg b.i.d. for 4 weeks. Smith et al. reported a fall in eGFR. "
         "Use an ACEi, e.g. ramipril, in adults.")
    got = [s[a:b].strip() for a, b in split_sentences(s)]
    assert len(got) == 3, got
    assert got[0] == "Give 2.5 mg b.i.d. for 4 weeks."
    assert "e.g. ramipril" in got[2]

def test_numbered_list_markers_do_not_split():
    s = "1. Offer an ACE inhibitor. 2. Recheck potassium."
    assert len([s[a:b] for a, b in split_sentences(s)]) == 2

def test_table_rows_are_never_sentence_split():
    """The condition is in one column and the action in another: the ROW is the
    rule, and splitting it destroys the rule."""
    rows = [b for b in _blocks() if b.kind == "table_row"]
    units = blocks_to_units(rows)
    assert len(units) == len(rows)
    assert all("Action:" in u.text for u in units)

# -------------------------------------------------------------- prefilter

def test_prefilter_recall_is_total():
    """Recall over precision: nothing in the gold set may be dropped here,
    because a candidate the prefilter rejects is never seen again."""
    units = blocks_to_units(_blocks())
    for want in GOLD:
        u = next((u for u in units if _norm(want) in _norm(u.text)), None)
        assert u is not None, f"lost before prefilter: {want!r}"
        score, feats = prefilter(u)
        assert score >= sg.PREFILTER_THRESHOLD, (want, score, feats)

def test_prefilter_rejects_the_hard_negatives_only():
    """The prefilter is deliberately permissive: it screens out the guideline
    describing its own methods, funding and literature, and leaves the harder
    call -- evidence prose sitting inside a Recommendations section -- to the
    classifier. Tightening it here would cost recall, which is the one thing
    that cannot be recovered downstream."""
    units = blocks_to_units(_blocks())
    hard = ["Smith et al. reported a prevalence", "We searched MEDLINE and Embase",
            "The authors declare"]
    for unwanted in hard:
        u = next(u for u in units if _norm(unwanted) in _norm(u.text))
        assert prefilter(u)[0] < sg.PREFILTER_THRESHOLD, (unwanted, prefilter(u))
    soft = next(u for u in units if "evidence supporting this" in u.text)
    assert prefilter(soft)[0] >= sg.PREFILTER_THRESHOLD      # admitted on purpose
    stmts, _ = _run()                                         # and rejected later
    assert not any("evidence supporting this" in s.text and s.is_recommendation
                   for s in stmts)

def test_recall_on_the_gold_set_is_total():
    stmts, _ = _run()
    kept = [s for s in stmts if s.is_recommendation and not s.duplicate_of]
    missing = [g for g in GOLD if not any(_norm(g) in _norm(s.text) for s in kept)]
    assert not missing, f"missed {len(missing)}/{len(GOLD)}: {missing}"

def test_guideline_self_description_is_not_promoted():
    stmts, _ = _run()
    kept = [s for s in stmts if s.is_recommendation and not s.duplicate_of]
    leaked = [a for a in ANTI_GOLD if any(_norm(a) in _norm(s.text) for s in kept)]
    assert not leaked, leaked

# ---------------------------------------------------------------- assembly

def test_grading_label_is_absorbed_not_emitted_as_a_recommendation():
    assert is_label_only("Strong recommendation, moderate certainty evidence.")
    assert not is_label_only("We recommend an ACE inhibitor in adults with diabetes "
                             "and an ACR of 30 mg/g or higher (1B).")
    stmts, man = _run()
    assert man["labels_absorbed"] == 1
    assert not any(s.text.strip().startswith("Strong recommendation,") for s in stmts)
    box = next(s for s in stmts if "Serum potassium" in s.text)
    assert box.strength_label == "Strong recommendation"      # inherited from the box

def test_labels_are_read_off_the_statement_itself_when_present():
    stmts, _ = _run()
    assert next(s for s in stmts if "We recommend treatment" in s.text).strength_label == "1B"
    assert next(s for s in stmts if "renal replacement" in s.text).strength_label == "2D"

def test_executive_summary_duplicate_is_collapsed():
    """Counting the same recommendation twice inflates the M1 denominator."""
    stmts, man = _run()
    assert man["duplicates"] == 1
    kept = next(s for s in stmts if "We recommend treatment" in s.text and not s.duplicate_of)
    assert kept.kind == "boxed_text"                 # body wins over the abstract
    assert any("abstract" in a for a in kept.also_at)

def test_crossref_resolves_and_is_shared_across_the_box():
    """'See Section 4.2' qualifies every recommendation in the box, not just
    the sentence it happens to sit in."""
    stmts, _ = _run()
    box = [s for s in stmts if s.kind == "boxed_text" and "Recommendation 3.1" in
           " > ".join(s.section_path)]
    assert box and all(s.context_locators for s in box)
    tree = etree.parse(FIXTURE)
    for s in box:
        assert tree.xpath(s.context_locators[0])[0].text.strip() == "Monitoring intervals"

def test_figures_are_counted_not_chased():
    _, man = _run()
    assert man["figures_uncovered"] == 1

# -------------------------------------------------------------- classifier

def test_non_verbatim_quote_is_rejected():
    """A paraphrase has no source span, so it is worse than a coarse decision."""
    def liar(block): return {"quote": "Patients should get an ACE inhibitor.",
                             "is_recommendation": True, "is_patient_level": True,
                             "reason": "paraphrased"}
    stmts, man = _run(llm=liar)
    assert man["classifier"] == "llm"
    assert all(s.classifier == "heuristic:quote_rejected" for s in stmts)
    assert not any("Patients should get" in s.text for s in stmts)

def test_llm_failure_does_not_silently_drop_statements():
    def boom(block): raise RuntimeError("429")
    stmts, _ = _run(llm=boom)
    assert stmts and all(s.classifier == "heuristic:llm_error" for s in stmts)
    assert all(any(_norm(g) in _norm(s.text) for s in stmts) for g in GOLD)

def test_verbatim_quote_narrows_the_span_and_moves_provenance():
    quote = "Measure eGFR and ACR at least annually"
    def picky(block):
        return {"quote": quote if quote in block.text else block.text,
                "is_recommendation": True, "is_patient_level": True, "reason": "ok"}
    stmts, _ = _run(llm=picky)
    s = next(s for s in stmts if s.text == quote)
    assert s.classifier == "llm"
    tree = etree.parse(FIXTURE)
    raw = "".join(tree.xpath(s.prov.locator)[0].itertext())
    assert _norm(raw[s.prov.char_start:s.prov.char_end]).startswith("Measure eGFR")

def test_structural_floor_survives_a_negative_section():
    """A guideline that prints its recommendation set in an appendix must not
    lose all of them: boxed_text (+2.5) plus section:non_recommendation (-4.0)
    used to land at -1.5 and be dropped before anyone read it."""
    p = Provenance("d", "/x", 0, 29)
    box = Block("Anticoagulation is preferred.", "boxed_text", p, ("Appendix 2",))
    score, feats = prefilter(box)
    assert score >= sg.PREFILTER_THRESHOLD and "floor:structural" in feats
    # loose prose gets no floor -- structure is the thing being protected
    prose = Block("Anticoagulation is preferred.", "section_text", p, ("Appendix 2",))
    assert prefilter(prose)[0] < sg.PREFILTER_THRESHOLD

def test_classifier_can_widen_past_a_bad_sentence_split():
    """The abbreviation list is a heuristic and will miss abbreviations. When
    it does, the model must be able to quote the WHOLE sentence out of the
    parent element rather than being trapped inside the truncated fragment."""
    full = "Give 2.5 mg b.i.d. for 4 weeks."
    truncated = Block("Give 2.5 mg b.i.d.", "section_text",
                      Provenance("d", "/p", 0, 18), ("3 Dosing",),
                      context=full, context_start=0)
    widen = lambda b: {"quote": full, "is_recommendation": True,
                       "is_patient_level": True, "reason": "widened"}
    verdict, judge = sg.classify(truncated, 3.0, ("deontic_nice",), widen)
    assert judge == "llm" and verdict["quote"] == full

def test_widened_quote_still_has_to_be_verbatim():
    """Widening is not a licence to invent: the quote is checked against the
    parent element, so anything outside it is still rejected."""
    truncated = Block("Give 2.5 mg b.i.d.", "section_text",
                      Provenance("d", "/p", 0, 18), ("3 Dosing",),
                      context="Give 2.5 mg b.i.d. for 4 weeks.", context_start=0)
    invent = lambda b: {"quote": "Give 2.5 mg twice daily for a month.",
                        "is_recommendation": True, "is_patient_level": True,
                        "reason": "paraphrase"}
    assert sg.classify(truncated, 3.0, (), invent)[1] == "heuristic:quote_rejected"

def test_units_carry_their_parent_element():
    for u in blocks_to_units(_blocks()):
        assert u.source_text and u.text.strip() in u.source_text

def test_prefilter_cost_is_measurable():
    """The threshold is a cost dial, not a correctness boundary. This is the
    measurement that tells you where to set it."""
    _, plain = _run()
    assert "prefilter_disagreements" not in plain       # off by default: it costs a pass

    _, man = _run(measure_prefilter=True)
    assert man["prefilter_rejected"] > 0
    # The heuristic disagrees on exactly the case the docstring names: a
    # rejected span whose only signal is the word "should".
    assert man["prefilter_disagreements"] == 1
    assert "should be cited as an independent review" in man["prefilter_disagreement_texts"][0]

    # An LLM that accepts everything bounds the measurement from above.
    yes = lambda b: {"quote": b.text, "is_recommendation": True,
                     "is_patient_level": True, "reason": "accept all"}
    _, man2 = _run(llm=yes, measure_prefilter=True)
    assert man2["prefilter_disagreements"] == man2["prefilter_rejected"]

    # Lowering the threshold admits more, and never loses the gold set.
    low_stmts, low = run_segment(_blocks(), "ckd", threshold=0.0)
    assert low["candidates"] > man["candidates"]
    kept = [s for s in low_stmts if s.is_recommendation and not s.duplicate_of]
    assert not [g for g in GOLD if not any(_norm(g) in _norm(s.text) for s in kept)]

def test_image_only_table_is_counted_not_dropped():
    """Measured on real PMC: some journals ship EVERY table as a scanned image
    (26 of 26 in a 2025 ESO guideline). Those recommendations are unreachable
    from the XML, so they must show up as declared uncovered surface rather
    than silently producing nothing."""
    xml = b"""<article><body><sec><title>Referral</title>
      <table-wrap id="t1"><label>Table 1</label>
        <caption><title>Recommendations for referral</title></caption>
        <table><tbody>
          <tr><td><graphic xlink:href="t1.jpg"
                  xmlns:xlink="http://www.w3.org/1999/xlink"/></td></tr>
        </tbody></table></table-wrap>
    </sec></body></article>"""
    blocks = load_jats(xml, "img")
    tables = [b for b in blocks if b.kind == "table_row"]
    figs = [b for b in blocks if b.kind == "figure"]
    assert not tables and len(figs) == 1
    assert "Recommendations for referral" in figs[0].text
    _, man = run_segment(blocks, "img")
    assert man["figures_uncovered"] == 1

def test_text_tables_still_produce_rows():
    """The image-table guard must not swallow tables that do have text."""
    assert len([b for b in _blocks() if b.kind == "table_row"]) == 3

def test_headings_never_become_statements():
    """A <title>Recommendation 1</title> scores numbered_rec (+3.0) plus
    section:recommendation (+2.0) minus heading (-1.0) = 4.0, and was emitted
    as a recommendation whose entire content was the words 'Recommendation 1'
    on three of four real guidelines."""
    blocks = _blocks()
    assert any(b.kind == "heading" for b in blocks)          # they are still loaded
    assert not any(u.kind == "heading" for u in blocks_to_units(blocks))
    stmts, man = _run()
    assert man["headings"] > 0
    assert not any(len(s.text.split()) < 4 for s in stmts)
    # ...but they still populate section_path, which is what they are for
    assert any("Recommendation 3.1" in " > ".join(s.section_path) for s in stmts)

# ---------------------------------------------------------------------- io

def test_jsonl_round_trip():
    stmts, _ = _run()
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "s.jsonl")
        write_jsonl(stmts, p)
        assert read_jsonl(p) == stmts
    json.dumps([statement_to_dict(s) for s in stmts])
    assert statement_from_dict(statement_to_dict(stmts[0])) == stmts[0]

# --------------------------------------------------------------------- pdf

def _make_pdf(path):
    """A page with: loose prose, a stroked+filled callout containing a graded
    recommendation, and a thin horizontal rule that must NOT read as a box."""
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 90), "Chronic kidney disease is common in older adults.",
                     fontsize=11)
    page.draw_line(pymupdf.Point(72, 110), pymupdf.Point(540, 110), width=1.2)
    box = pymupdf.Rect(60, 140, 545, 235)
    page.draw_rect(box, color=(0, 0, 0), fill=(0.92, 0.95, 1.0), width=1.0)
    page.insert_textbox(pymupdf.Rect(70, 150, 535, 230),
                        "We recommend an ACE inhibitor or ARB in adults with diabetes "
                        "and a urine albumin-to-creatinine ratio of 30 mg/g or higher "
                        "(1B).", fontsize=11)
    page.insert_text((72, 280), "Further research is needed in younger adults.",
                     fontsize=11)
    doc.save(path); doc.close()

def test_pdf_box_recovery_finds_the_callout():
    """Layout models trained on DocLayNet have no 'callout' class, so the box is
    recovered from the PDF's own drawing primitives instead."""
    import pymupdf
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "g.pdf")
        _make_pdf(p)
        blocks = load_pdf(p, "pdffix", use_docling=False)
        boxed = [b for b in blocks if b.kind == "boxed_text"]
        assert len(boxed) == 1, [(b.kind, b.text[:40]) for b in blocks]
        assert "ACE inhibitor" in boxed[0].text
        assert boxed[0].box_id and boxed[0].prov.page == 1 and boxed[0].prov.bbox
        outside = [b for b in blocks if b.kind != "boxed_text"]
        assert any("Chronic kidney disease" in b.text for b in outside)
        assert any("Further research" in b.text for b in outside)
        # the thin rule is not a callout
        doc = pymupdf.open(p)
        assert len(sg.find_callout_rects(doc[0])) == 1

def test_pdf_statements_carry_page_and_bbox():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "g.pdf")
        _make_pdf(p)
        stmts, man = run_segment(load_pdf(p, "pdffix", use_docling=False), "pdffix")
        rec = next(s for s in stmts if "ACE inhibitor" in s.text)
        assert rec.kind == "boxed_text" and rec.strength_label == "1B"
        assert rec.prov.page == 1 and rec.prov.bbox is not None
        assert not any("Further research" in s.text
                       for s in stmts if s.is_recommendation)


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:
            failed.append(name)
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
