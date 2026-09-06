# The segmenter

Stage 1 of Track A: a guideline document in, recommendation statements out,
each pointing at the exact span of source it came from. One file,
[`segment.py`](segment.py); tests in [`test_segment.py`](test_segment.py);
engineering decisions and gotchas in [`NOTES.md`](NOTES.md).

```
python3 tests/test_segment.py      # 33 tests, no pytest, no network, no API key
```

## The one idea

Finding sentences that look like recommendations is easy — you could grep for
"recommend". The job is producing statements that can be **pointed back at the
document**, because that is what makes every downstream claim checkable:

- a clinician will not adjudicate a formula, but will confirm or reject
  *"this rule came from this sentence"*;
- the fidelity metric is per-clause, so each clause needs a sentence;
- the UI highlights compiled spans in the source PDF;
- without it, "this CQL encodes the guideline" is a claim nobody can test.

So every `Statement` carries a `locator` that re-finds its element and offsets
that slice it out, and both are asserted in the tests.

## Quickstart

```bash
# find open-access guidelines
python3 segment.py --search 'PUB_TYPE:"Practice Guideline" AND FIRST_PDATE:[2025-01-01 TO 2026-12-31]'

# fetch one from Europe PMC (cached in corpus/) and segment it
python3 segment.py PMC13276589 --top 15

# save every statement, and measure what the prefilter threshold costs
python3 segment.py PMC13276589 --out ash.jsonl --measure-prefilter

# read a saved run; --all also shows what the classifier rejected
python3 segment.py --review ash.jsonl --top 20
```

From Python:

```python
import segment as sg

blocks = sg.load_jats("corpus/PMC13276589.xml", "ash")     # or load_html / load_pdf
statements, manifest = sg.segment(blocks, "ash")
recs = [s for s in statements if s.is_recommendation and not s.duplicate_of]
sg.write_jsonl(statements, "ash.jsonl")
```

## What it produces

### `Statement` — one candidate recommendation

| field | meaning |
|---|---|
| `statement_id` | `<doc_id>:<n>`, stable within a run |
| `text` | the recommendation, **verbatim** from the document (one exception below) |
| `kind` | which structure it came from — see *Block kinds* |
| `prov` | `Provenance`: where in the document |
| `section_path` | ancestor section titles, outermost first, incl. any box label |
| `strength_label` | `"conditional recommendation"`, `"1B"`, `"Class I"` … if present |
| `evidence_label` | `"very low certainty"`, `"Level of Evidence B"` … if present |
| `prefilter_score` | the cheap score that admitted it |
| `prefilter_features` | **which patterns fired** — every decision is explainable |
| `is_recommendation` | classifier verdict: directs clinical action? |
| `is_patient_level` | classifier verdict: about a patient, not a service or a research agenda? |
| `classifier_reason` | free text from the judge |
| `classifier` | `heuristic`, `llm`, `heuristic:quote_rejected`, `heuristic:llm_error` |
| `context_locators` | resolved within-document cross-references |
| `duplicate_of` | set on the copy that lost a dedupe |
| `also_at` | on the copy that won: provenance keys of the restatements |

### `Provenance` — the whole point

| field | meaning |
|---|---|
| `doc_id` | document identifier |
| `locator` | XPath for XML/HTML, `p<page>:b<n>` for PDF. **Re-finds the element.** |
| `char_start`, `char_end` | offsets into that element's text. **Slice out the statement.** |
| `page`, `bbox` | PDF only; the UI needs the box to draw a highlight |

Round-tripped on a real 2026 ASH/ISTH guideline:

```
locator : /article/body/sec[5]/sec[2]/boxed-text  ->  exactly one <boxed-text>
chars   : 1:329                                   ->  slices out of that element:
  "Recommendation 4 In infants, children, and adolescents considered for TPN for
   >60 days ..., the ASH/ISTH guideline panel suggests using anticoagulant
   prophylaxis rather than no anticoagulant prophylaxis (conditional
   recommendation based on very low certainty ...)"
matches the statement text exactly: True
```

### The manifest — the extraction-error report

Published **alongside** the formalizability number, so the extractor's error is
never conflated with the formalizer's.

| key | meaning |
|---|---|
| `blocks`, `blocks_by_kind` | layout elements found |
| `units` | after sentence splitting |
| `candidates` | units the prefilter admitted |
| `prefilter_rejected` | units it did not |
| `prefilter_threshold` | the score cutoff this run used |
| `statements`, `recommendations` | classified, and kept |
| `duplicates` | restatements collapsed — **not a nicety**: 95 on one real guideline |
| `labels_absorbed` | grading labels folded into the statement they label |
| `figures_uncovered` | **declared uncovered surface**: flowcharts, and tables shipped as images |
| `headings` | loaded for `section_path`, never statements |
| `classifier` | `heuristic` or `llm` |
| `prefilter_disagreements` | with `measure_prefilter=True`: rejected spans the classifier would have kept |

### JSONL

`write_jsonl` writes **every** statement, including the classifier's rejects
and the duplicates — they are the audit trail, which is why a real document
produces 663 records for 88 recommendations. Use `--review` to read it, or
filter on `is_recommendation and not duplicate_of`.

## How it works

### Stage 1 — loader → `Block[]`

Three loaders, one output type, so everything downstream is source-agnostic.

- **`load_jats`** — PMC Open Access XML. **The main path**: the publisher
  already tagged `<boxed-text>` and `<table-wrap>` at typesetting time, so the
  boxed-callout problem is solved structurally, with no layout model.
- **`load_html`** — USPSTF and similar. Callouts are divs with telltale classes.
- **`load_pdf`** — fallback. Optional layout model, plus a geometric pass.

The loader runs **two passes**: the first claims structural elements and marks
their whole subtree consumed, the second picks up loose `<p>`, `<list-item>`,
`<title>`. Without the consumed set, a `<p>` inside a box is emitted twice.

#### Block kinds

| kind | source | prefilter weight |
|---|---|---|
| `boxed_text` | `<boxed-text>`, a div with a callout class, or a PDF rectangle | +2.5 |
| `table_row` | one `<tr>`, header-folded | +1.5 |
| `list_item` | `<list-item>` / `<li>` | +0.5 |
| `section_text` | `<p>` | 0.0 |
| `heading` | `<title>` / `<h*>` — populates `section_path`, never a statement | — |
| `figure` | `<fig>`, and image-only tables — counted as uncovered | — |

#### The PDF path

Layout models are trained on DocLayNet, whose classes are Caption / Footnote /
Formula / List-item / Page-header / Page-footer / Picture / Section-header /
Table / Text / Title. **There is no callout class**, so a boxed recommendation
comes back as `Text`. But in a born-digital PDF the box is literally a stroked
or filled rectangle in the content stream, so `find_callout_rects` recovers it
*exactly* from `page.get_drawings()`: discard anything smaller than 60×18 pt
(rules and gridlines), discard anything over 85% of the page (the frame), then
relabel any block ≥90% contained in a surviving rect. Rects are sorted
smallest-first so a block in nested boxes is attributed to the inner one.

### Stage 2 — `blocks_to_units`: sentences, offsets intact

Normalization — whitespace collapse, de-hyphenating `partici-\npants`, `≥` to
`>=` — changes the string length, so every offset computed afterwards points at
the wrong place. This is where provenance usually dies.

The `Text` class builds the normalized string **and a per-character index map**
back to the raw text, so a sentence found in the clean string is always
reportable against the original.

`_ABBREV` stops the splitter cutting at `b.i.d.` or `e.g.`. It is a heuristic
and will miss abbreviations — which is why units carry `context` (their parent
element's full text) and the classifier quotes against *that*, so a bad split
is recoverable. Table rows skip this stage: the row is the atom.

### Stage 3 — prefilter

Regex scoring, no model, so most of a document never costs an API call.
Returns the score **and the features that fired**.

- **grading schemes** — GRADE verbal/certainty (+4/+3), KDIGO `1B` (+4),
  ACC/AHA Class + Level (+4), USPSTF (+3), SIGN (+2), numbering (+3)
- **deontic language** — "we recommend" (+3), "is recommended" (+3), "should
  be" (+2.5), "must be" (+2.5), NICE-style leading "Offer"/"Consider" (+2.5),
  weak forms (+1.5), a dose or threshold (+1)
- **table/imperative** — an `Action:` column (+2), a leading bare imperative (+2)
- **negatives** — cites a study (−2), "further research is needed" (−2.5),
  methods voice (−2.5), a Methods/Funding/References section (−4)

Threshold **1.5**, deliberately low.

**Recall comes from structure, not vocabulary.** `boxed_text` (+2.5) and
`table_row` (+1.5) clear the threshold on kind alone, so anything in a callout
or a recommendation table passes however it is worded; the word lists only
decide loose prose and bullets. `STRUCTURAL_FLOOR` then stops a negative
section weight sinking a structural element — a guideline printing its
recommendations in an appendix scored 2.5 − 4.0 = −1.5 and lost every one.

The threshold is a **cost dial, not a correctness boundary**. Set it to 0 and
vocabulary stops mattering entirely; you pay one classifier call per sentence.

### Stage 4 — classify

Two judgements per candidate: `is_recommendation` and `is_patient_level`. They
are separate because *"commissioners should ensure rapid-access clinics are
available"* is a real recommendation that is not about a patient — it maps to
the `not_patient_level` refusal bucket.

The judge is an **injected callable**, so the pipeline runs offline on a
heuristic and the tests need no API key:

```python
def my_llm(block: sg.Block) -> dict:
    ...                                  # sg.build_prompt(block), sg.CLASSIFIER_SCHEMA
    return {"quote": ..., "is_recommendation": ..., "is_patient_level": ...,
            "reason": ...}

statements, manifest = sg.segment(blocks, "doc", llm=my_llm)
```

Three guarantees, all tested:

- **the quote must be verbatim.** It is checked against the parent element; a
  paraphrase is rejected and the heuristic runs instead. A paraphrase has no
  source span, so it cannot be adjudicated — worse than a coarse decision.
- **a wider quote is allowed.** The model sees the whole element and may quote
  past the candidate's boundaries, so a bad sentence split is recoverable.
- **an exception does not lose the statement.** A 429 falls back rather than
  silently deleting a recommendation.

### Stage 5 — assembly

- **label absorption** — *"Strong recommendation, moderate certainty
  evidence."* on its own line is a label, not a recommendation. It becomes the
  box's `strength_label`.
- **cross-references** — *"See Section 4.2"* resolves within the document and
  attaches to every recommendation in the block. Link-following, not
  retrieval: the target is in the same document and both spans are cited.
- **dedupe** — normalized-text hash; the body copy wins over the executive
  summary, the loser's provenance goes in `also_at`.

## Three rules it is built around

**Recall over precision.** A false positive becomes a `not_patient_level`
refusal downstream: visible, cheap, in the residue report. A false negative is
invisible and shrinks the formalizability denominator, which is the paper's
headline number.

**Never synthesize.** Every statement is verbatim with offsets that prove it.
The **one documented exception** is `table_row`: a recommendation table splits
condition and action across columns, so no contiguous span of the document is
the rule. Rows are rendered header-folded, provenance points at the `<tr>`, and
the tests assert every cell *value* is verbatim.

**Provenance is not optional.** Locators re-find their element, offsets slice
out the statement, and both survive normalization. All asserted in tests, and
verified on 1,086 blocks of real publisher markup with zero failures.

## What real documents showed

Five 2025-26 open-access guidelines (see [`NOTES.md`](NOTES.md) for the full log):

- **Some journals ship every table as an image.** 26 of 26 `<table-wrap>` in a
  2025 ESO guideline are `<td><graphic src="*.jpg"/></td>`. Those
  recommendations are unreachable from JATS and need the PDF path; they are
  now counted in `figures_uncovered` rather than vanishing.
- **Prefilter pass rate swings 3%–72% by document** — dense GRADE prose versus a
  document whose normative content is in images. Budget the classifier
  accordingly: 663 calls for one guideline is a real number.
- **Duplication is heavy** — 95 restatements on one guideline. Without dedupe
  the M1 denominator inflates by roughly a third.

## Limits

- The **Docling path has never executed** here (not installed; lazy-imported
  behind `find_spec`). Written defensively, but unverified. The PyMuPDF path
  *is* tested, on a PDF the test generates itself.
- **`load_html` has never seen real USPSTF markup.** The class-name heuristic
  is a guess.
- **Recall on real documents is unmeasured.** Five have run without crashing,
  but nobody has read one and marked every recommendation by hand. Until that
  gold set exists, "9/9 on the fixture" is a regression guarantee, not
  evidence.
- **The model classifier is wired in now** (`demo.py`, `goldset.py` both pass
  `llm=llm.classify_block`) — but it wasn't for a while without anyone
  noticing, because the offline heuristic is a silently working fallback, not
  an error. See RESULTS.md R21: measured on the 45-statement gold set, the
  model classifier lifted coverage over what reaches the pipeline from 27% to
  40%, entirely by rejecting non-recommendations the heuristic was letting
  through — no encodable statement was lost. `prefilter_disagreements` is
  measured for real as of that run.
