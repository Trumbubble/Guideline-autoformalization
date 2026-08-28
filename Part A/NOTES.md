# Track A engineering notes

Running log of what we depend on, what we decided, and what bit us. Each entry
says *why*, because the why is what stops it being undone six weeks from now.

## Environment

Python **3.14.2**, system interpreter, no venv.

| package | version | licence | why |
|---|---|---|---|
| `z3-solver` | 5.1.0 | MIT | DSL lowering, vacuity sweep, counterexamples |
| `lxml` | 6.1.2 | BSD | JATS/HTML parsing; `getpath()` is the provenance locator |
| `pymupdf` | 1.28.2 | AGPL-3.0 | PDF text, tables, and drawing primitives |
| `docling` | not installed | MIT | optional PDF layout-model path |

**`pymupdf` is AGPL-3.0.** Fine for research, but flag it before anything gets
redistributed as a hosted service. `pypdfium2` (BSD/Apache) is the escape hatch
if that ever matters — it has `get_drawings`-equivalent access to page objects.

## Library gotchas

- **`import pymupdf`, never `import fitz`.** The `fitz` spelling is deprecated
  and prints a warning on every import.
- **`page.find_tables()` prints** "Consider using the pymupdf_layout package…"
  to stderr. Harmless noise, not an error. It is also best-effort, so it is
  wrapped in `try/except` — a table-detection failure must not lose the page.
- **`lxml`: a child's `itertext()` excludes its own tail.** So
  `"".join(_itext(c) for c in children)` silently drops the whitespace *between*
  children and welds two sentences into one unsplittable run. Slice the parent's
  full `itertext()` instead. This cost us a real bug: `intervals.Strong`.
- **PMC JATS is usually un-namespaced** (DTD-driven), but not always. All tag
  matching goes through `_lname()`, which strips any `{ns}` prefix.
- **`isinstance(True, int)` is `True` in Python.** Booleans pass every naive
  numeric typecheck; `dsl._is_number` excludes them explicitly. `egfr > true`
  used to typecheck.

## Security

`load_jats` parses with `XMLParser(resolve_entities=False, load_dtd=False,
no_network=True)`. Guideline XML is third-party input; entity expansion is the
billion-laughs and XXE surface. `recover=True` because real PMC XML has
malformed corners and losing a whole document to one bad tag is worse than
parsing around it.

## Decisions

**Lazy-import Docling.** It pulls torch (multi-GB). The JATS path is the main
path and must not pay for a dependency it never calls. Gated behind
`importlib.util.find_spec`, so the module imports fine without it.

**JATS before PDF.** PMC Open Access ships XML where the publisher already
tagged `<boxed-text>` and `<table-wrap>` at typesetting time. The boxed-callout
problem is solved *structurally* for most of the corpus; PDF layout analysis is
the fallback, not the main road. It also means the prefilter and classifier can
be measured against real statements before a line of geometry is written.

**`lxml`'s `getpath()` as the locator.** Returns an absolute XPath
(`/article/body/sec[3]/boxed-text[1]`) that round-trips through `tree.xpath()`.
Tested: provenance that cannot re-find its span is not provenance.

**Offset-preserving normalization (`Text`).** De-hyphenation and whitespace
collapse shift every downstream offset, which is where provenance usually dies.
`Text` keeps a per-character index map back to the raw string, so a sentence
found in the normalized text is always reportable against the original — which
is what the UI highlights and what a clinician reads.

**Box recovery is geometry, not ML.** Layout models are trained on DocLayNet,
whose classes are Caption / Footnote / Formula / List-item / Page-header /
Page-footer / Picture / Section-header / Table / Text / Title — there is **no
callout class**. A boxed recommendation comes back as `Text`. But in a
born-digital PDF the box is literally a stroked or filled rectangle in the
content stream, so `page.get_drawings()` recovers it *exactly*. Thresholds:
`MIN_BOX_W/H` (60×18 pt) discards rules and gridlines, `MAX_BOX_PAGE_FRAC`
(0.85) discards the page frame, `CONTAINMENT` (0.90) is the block-area overlap
needed to call a block "inside". Rects are sorted smallest-first so a block in
nested boxes is attributed to the inner one.

**Recall over precision, everywhere.** A false positive becomes a
`not_patient_level` refusal downstream: visible, cheap, and reported in the
residue. A false negative is invisible and silently shrinks the
formalizability denominator, which is the paper's headline number.
`PREFILTER_THRESHOLD` is 1.5 and deliberately admits evidence prose that sits
inside a Recommendations section — the classifier resolves it. Tightening the
prefilter buys precision the pipeline already has and costs recall it cannot
recover.

**The classifier must quote, and we verify the quote.** A returned span that is
not a verbatim substring of the source block is rejected and the call falls
back to the heuristic. A paraphrase has no source span, so it cannot be
adjudicated — worse than a coarse decision. Same reason a raising LLM falls
back rather than dropping the statement: a 429 must not delete a
recommendation from the corpus.

**The LLM is an injected callable.** `classify(..., llm=None)` runs a heuristic,
so the whole pipeline and all 25 tests execute offline and deterministically
with no API key. `build_prompt` and `CLASSIFIER_SCHEMA` are exported for
whatever model gets wired in.

**Table rows are a rendering — the one synthesis exception.** A recommendation
table splits the condition and the action across columns, so *no contiguous
span of the document is the rule*. Rows are rendered header-folded
(`Finding: eGFR below 30; Action: refer to nephrology`), provenance points at
the `<tr>`, and the test asserts every cell *value* is verbatim even though the
assembled string is not. Documented in the module docstring so nobody "fixes"
it later.

**Grading labels are absorbed, not emitted.** "Strong recommendation, moderate
certainty evidence." on its own line is a label, not a recommendation; emitted
as a statement it is a recommendation that recommends nothing. `is_label_only`
routes it to the box's `strength_label`.

**Deduplicate against the executive summary.** Guidelines restate
recommendations in an abstract *and* the body. Counting both inflates the M1
denominator. Body wins (more context); the other provenance keys are kept in
`also_at`.

**Cross-references are a property of the block.** "See Section 4.2" qualifies
every recommendation in the box, not the sentence it happens to sit in. Units
from one block share a locator, so hits are pooled per locator. This is
within-document link-following, *not* retrieval: the target is in the same
document and both spans are cited, so provenance survives.

**Structure outranks vocabulary, and outranks negative sections too.** The
prefilter's recall does not come from the word lists: `boxed_text` scores +2.5
and `table_row` +1.5 against a 1.5 threshold, so anything in a callout or a
recommendation table passes on structure alone, whatever the wording. The
vocabulary only decides loose prose and list items. `STRUCTURAL_FLOOR` then
stops a negative section weight sinking a structural element -- a guideline
that prints its full recommendation set in an appendix used to score
2.5 - 4.0 = -1.5 and lose every one of them.

**The classifier quotes against the parent element, not the split sentence.**
`_ABBREV` is a heuristic and will miss abbreviations; when it does, the
splitter cuts mid-sentence and the model would otherwise be trapped inside the
truncated fragment with no way to recover the rest. Units carry
`Block.context` (the parent's full text) and `context_start`, the substring
check runs against that, and the resulting offsets are computed from the
parent. So a bad split is recoverable while provenance stays verifiable --
widening is not a licence to invent, because the quote must still be verbatim
*from a real document element*. This demotes `_ABBREV` from load-bearing to
advisory. Swapping in `pysbd` would still be worth doing; it is now the smaller
half of the fix.

**The prefilter threshold is a cost dial, not a correctness boundary.**
`measure_prefilter=True` runs the classifier over the REJECTED units and
reports where the two disagree. Adjudicate those by hand and the survivors are
the true false-negative rate. Reported as `prefilter_disagreements`, never as
false negatives, because the classifier is not ground truth: on the fixture it
flags "This work should be cited as an independent review" on the strength of
the word *should* alone. Off by default because it costs a full extra
classification pass.

**Flowcharts are counted, not chased.** They are PathWISE's substrate, and
prose is the stated differentiator. `manifest["figures_uncovered"]` is the
declared uncovered surface of the document.

**Generated PDF fixture.** `test_segment.py` builds its test PDF with PyMuPDF
(draw a rect, put text in it, draw a thin rule that must *not* read as a box).
Box recovery is verified deterministically and no copyrighted guideline PDF
enters the repo.

**No pytest.** Both test files are plain asserts with a `__main__` runner, to
match `test_dsl.py` and keep the dependency list short.

## DSL-side decisions (see DSL.md for the user-facing version)

- `Since` lowers to `<event>_occurred AND days_since_<event> <= n`. The
  occurrence flag is load-bearing: without it the solver satisfies a monitoring
  window for a patient who was never checked, which is the commonest real
  violation.
- `code` vars → `z3.String` (equality only); `date` vars → ISO-8601 in the DSL,
  `z3.Int` ordinals in the solver; `int` → `z3.Int`, not `Real`, or the solver
  returns `days = 14.5`.
- `VarDef.plausible` does double duty: validator warning on unit slips, and
  `domain_axioms()` so counterexamples stop coming back at eGFR = 2.0.
- `validate()` is dependency-free; `check_nondegenerate()` is where z3 is
  needed. Keeps the fast path importable without a solver.

## What running four real PMC guidelines taught us

Corpus (fetch with `python3 segment.py PMCxxxxxxx`), all 2025-26 OA practice
guidelines: **PMC13276589** ASH/ISTH paediatric anticoagulation, **PMC13151662**
ESO stroke blood-pressure, **PMC13131131** ESMO uveal melanoma, **PMC13054209**
deprescribing in older people, **PMC13270348** acute pancreatitis pain.

**Provenance held.** 0 locator failures across 1,086 blocks — every
`tree.getpath()` XPath re-resolved to exactly one element on real publisher
markup. Every block's text was a substring of its element except the table
rows, which are documented renderings; the mismatch count equalled the row
count exactly on both documents that had tables.

**Some journals ship every table as an image.** 26 of 26 `<table-wrap>` in the
ESO guideline are `<td><graphic src="*.jpg"/></td>` with no text in any cell.
The recommendations in them are simply unreachable from JATS and need the PDF
path. `_table_rows` now emits a `figure` block carrying the caption, so they
land in `figures_uncovered` instead of vanishing. This is a real constraint on
the JATS-first strategy and belongs in the paper: for that document the XML
route reaches only the narrative restatements of the recommendations, not the
formal boxed ones.

**Headings were being emitted as recommendations.** `<title>Recommendation
1</title>` scores +3.0 numbered_rec, +2.0 section:recommendation, -1.0 heading
= 4.0, so three of four documents produced "recommendations" whose entire
content was the words "Recommendation 1". Headings now populate `section_path`
and are never units (`NON_STATEMENT_KINDS`). Found only by running real
documents — the fixture's headings were all too plain to trip it.

**`fetch_pmc` sniffed bytes and was wrong.** Real PMC JATS may open with
`<?xml`, with `<!DOCTYPE article …>`, or with `<article>` directly; the
`startswith(b"<?xml")` guard rejected a perfectly good document. It now parses
and checks the root tag. Related: Europe PMC's `IN_EPMC:y` does **not** predict
`fullTextXML` availability — `HAS_FT:y` does, and `search_pmc` now sends it.

**Prefilter pass rate varies enormously by document**: 72% of units on ASH/ISTH
(a dense GRADE guideline where nearly every paragraph is normative) versus 3%
on ESO (where the normative content is in images). That range is the argument
for `measure_prefilter` rather than a fixed threshold — and for costing the LLM
classifier per document, since 663 calls for one guideline is a real number.

**Duplication is much heavier than the fixture suggested**: 95 duplicate
statements on the pancreatitis guideline, 25 on ASH/ISTH, versus 1 on the
fixture. Guidelines restate recommendations in an executive summary, in a
per-section summary, AND in a table. Deduplication is not a nicety; without it
the M1 denominator would be inflated by a third on some documents.

## Stage 2: what building the binder taught us

Every source is reachable with **no UMLS account**, all verified live:
SNOMED search/expand/validate via `tx.fhir.org` (`$expand?filter=`,
`?fhir_vs=isa/<code>`, `$validate-code`), LOINC via NLM Clinical Tables, drugs
and ATC classes via RxNav. A UTS licence buys VSAC's curated, steward-versioned
value sets -- better than a computed is-a expansion, and what the AHRQ CDS
Connect reference artifacts bind to -- but nothing is blocked without it.

**Live LOINC bound `eGFR` to the wrong gene.** The query "eGFR" returns
*"EGFR gene exon 20 insertion [Presence] ... by Molecular genetics method"* --
epidermal growth factor receptor -- and, being the only candidate, the selector
called it a "clear lexical win" and bound it to a kidney-function threshold.
Three fixes, all deterministic:

- **being the only candidate is not evidence.** `POSITIVE_EVIDENCE` requires an
  exact term match, a unit match, or full query-token coverage before the
  offline selector will bind anything. This alone turns the eGFR case from a
  wrong binding into a refusal, which is the safe outcome.
- **abbreviation expansion** (`ABBREVIATIONS`): search the phrase *and* its
  expansion, union the candidates. "eGFR" alone cannot be disambiguated;
  "estimated glomerular filtration rate" can.
- **grouper codes are penalised.** LOINC carries "HEDIS MY 2020 Value Set -
  Estimated Glomerular Filtration Rate" alongside the real result code, and it
  wins on token overlap. You cannot threshold a value set, so
  `NOT_AN_OBSERVATION` demotes value sets, panels, and deprecated codes.

**Plurals were silently refusing correct bindings.** Guideline prose says "an
ACE inhibitor"; ATC says "ACE INHIBITORS, PLAIN". Without stemming the two
share no evidence and a correct binding is refused. `_stem` is applied for
comparison only, never to the FTS query, where a prefix match already covers it.

**Units are a deterministic disambiguator and should run before any model.**
LOINC 9318-7 is mg/g and 14585-4 is mg/mmol -- the same clinical threshold a
factor of 8.84 apart. The sentence states the unit, so `rerank_by_unit` settles
it with no judgement required. Without the unit the two score *identically* and
the binder correctly refuses.

**`RxClass class/byName` needs the exact class name** -- "ACE inhibitor" gets 0
hits, "ACE INHIBITORS, PLAIN" gets 2. Useless against guideline prose. There
are only 1,319 ATC1-4 classes, so the fix is to seed them all into the local
index and search lexically rather than query the endpoint per phrase.

**NLM Clinical Tables returns no units.** `df=EXAMPLE_UCUM_UNITS` comes back
empty for every row, so unit matching cannot work against that source. The
fixture carries units and the tests exercise the path; real unit data needs the
licensed LOINC download.

**The offline heuristic refuses a lot on live terminology, and that is the
design.** It is the fallback, deliberately conservative; the LLM selector,
which sees the whole sentence, is what converts those abstentions into
bindings. Any abstention it cannot resolve is residue, which is a measured
deliverable rather than a failure.

## Wiring the model in: what constrained decoding actually allows

`llm.py` supplies the real implementations of the injected callables. No stage
imports it, and importing it makes no network call.

**The API's structured-output validator accepts a strict subset of JSON Schema.**
Probed directly against `claude-opus-5`:

| construct | verdict |
|---|---|
| acyclic `$ref`, `enum`, `const`, nullable unions, `anyOf`, `minItems: 1` | accepted |
| **circular `$ref`** (`expr -> expr`) | rejected |
| `minimum` / `maximum` on `number` or `integer` | rejected |
| `minItems` other than 0 or 1 | rejected |
| `oneOf` | rejected |
| `$defs` alongside a top-level `anyOf` (root must be an object) | rejected |

**A recursive expression grammar is therefore not directly expressible.** This
is the practical form of the DEV_PLAN's point that the target grammar is
context-free and FSA-based decoders are unsound for it -- the constraint shows
up as a flat 400, not as silent breakage. `dsl.decoding_schema()` unrolls the
recursion to a fixed depth; `dsl.json_schema()` keeps the honest recursive
version for documentation. Constraints dropped for decoding (`minItems: 2` on
junctions, the confidence bounds) are all re-checked by `validate()`, so
nothing is lost -- the decoder is a filter, not the oracle.

**Grammar size scales with the variable enum, and that has an architectural
consequence.** Measured: a 13-variable registry compiles only at nesting depth
1; a 3-variable one reaches depth 2; leaves must be defined once behind a
`$ref` rather than inlined per level, or even depth 2 fails. So the registry
handed to the decoder should be the STATEMENT's bound vocabulary -- what stage
2 produces for that sentence -- not the whole document's. Smaller, and it also
stops the model reaching for a variable the statement never mentioned.
`llm.formalize` steps the depth down on a grammar error rather than failing.

**Refusal is a first-class output of the same decode.** Because `oneOf` is
rejected, the rule/refusal choice is a discriminator field on one object; the
model must commit to one or the other in a single constrained decode. On the
smoke test it refused *"Consider referral after an adequate trial of
conservative therapy"* with `vague_predicate` rather than inventing a
threshold, and encoded *"eGFR below 30 OR ACR 300 mg/g or higher"* as `Or` --
the P3 AND-for-OR bug does not reproduce under structured decoding.

**Temperature is gone.** `claude-opus-5`, `sonnet-5`, `opus-4-8` and `opus-4-7`
reject `temperature`, `top_p` and `top_k` with a 400; only `claude-sonnet-4-6`
and `claude-haiku-4-5` still accept them. The sampling-variance probe therefore
measures the spread from re-calling at default settings -- which is what a
deployment actually sees, and arguably the more honest number. Worth stating in
the paper: VERIMED's ambiguity-by-sampling-variance estimator assumes a
temperature control current frontier models no longer expose.

**Costs, measured.** ~$0.007 per formalization call at `effort=medium` with the
instruction prefix cached (7.3k cached tokens on a 4-call smoke test). The
20-statement x K=8 probe is about $1.

## Sampling-variance probe: the result

20 statements (10 precise, 10 vague, hand-written), K=8 samples each, 160 real
`claude-opus-5` calls at `effort=medium`. Agreement = share of the K samples in
the largest **semantic** equivalence class, decided by Z3 over the domain
axioms (`dsl.equivalence_classes`), not by string comparison.

```
             mean agreement   refusals/K
precise           0.99            0.1
vague             0.00            8.0
AUC                                    1.000
```

**Perfect separation -- but not by the mechanism the design assumed.** Every
one of the 80 vague samples was REFUSED. There were no competing encodings to
disagree about, so the disagreement signal had nothing to measure. AUC from
refusal rate alone is also 1.000, and **0 of the 10 statements that got encoded
at all produced more than one semantic reading**.

The operational consequence is a simplification: offered refusal as a branch of
the constrained decode, the model declines vague statements outright. **One
call does what the K-sample estimator was supposed to do.** VERIMED's
ambiguity-by-sampling-variance is redundant on this evidence -- the gate should
lead with refusal-under-constrained-decoding and keep sampling variance in
reserve for borderline statements, if at all. It is also 8x cheaper.

**Semantic equivalence was load-bearing.** 9 of the 10 precise statements
produced textually different encodings -- up to **6 distinct strings for one
meaning** -- that the solver collapsed to a single class. A string-comparison
metric would have reported those as ambiguous and inverted the result. This is
the strongest argument for `distinguishing_patient` / `equivalence_classes`
living in the DSL rather than in the probe.

**What this does NOT establish.** The statements and their labels are both
mine, and the precise/vague split is obvious by construction; real guideline
prose is far more borderline. And the model refusing is not evidence that
refusing was *correct* -- that is exactly what E3's clinician adjudication is
for. Treat this as: the mechanism works and the cheap version suffices, not as
a calibration result.

One incidental finding worth keeping: the single refusal among the precise
statements was *"Screen for retinopathy annually in adults with diabetes"* --
arguably right, since "annually" states no window.

## VSAC (needs the UMLS licence)

`VsacIndex` queries `cts.nlm.nih.gov/fhir` with HTTP Basic auth, literal
username `apikey` and the UMLS API key as the password. Routed ahead of
SNOMED/ATC for the set-valued kinds (`condition`, `drug_class`, `procedure`)
and unioned with them, so the selector sees both a curated set and the single
concept it would otherwise expand itself.

Three fixes it forced:

- **Search `title:contains`, not `name=`.** Prose says "ACE inhibitor"; a
  steward titles the set "ACE Inhibitors" or "ACE Inhibitor or ARB Ingredient".
  Exact-name matching finds neither.
- **Ask for enough results to rank.** VSAC returns matches in no useful order,
  so `_count` is `k*6` and `_score` does the ranking. At `_count=10` the
  curated "Diabetes" sets never surfaced and an unrelated gestational-diabetes
  set won.
- **Normalise the query.** `"albumin-to-creatinine ratio"` returned **zero**
  LOINC candidates: hyphens and the infix "to" defeat the keyword endpoints.
  `normalise_query` strips both, and `expand_query` sends the original, the
  bare form, and any abbreviation expansion as a union.

And one prompt fix: the selector was abstaining on *redundancy* (two stewards
publishing the same concept) rather than only on under-determination. It is now
told that redundancy is not a reason to abstain, and to prefer a curated value
set over a single code for category phrases.

## Known gaps

- **The Docling path is written but untested here** — docling is not installed,
  so `_docling_blocks` has never executed. Its attribute access is defensive
  (`getattr` throughout) but treat it as unverified until it runs on a real PDF.
- **`load_html` is untested against a real USPSTF page.** The class-name
  heuristic (`recommend|callout|box|panel|highlight|key-?point`) is a guess
  until we look at actual markup.
- **The gold set is one synthetic fixture.** Real recall numbers need real
  documents; the fixture only prevents regressions.
- **Recall on real documents is still unmeasured.** Five have been through the
  pipeline and nothing crashed, but nobody has read one and marked every
  recommendation by hand. Until that exists, "9/9 on the fixture" is a
  regression guarantee, not evidence.
- **The ATC class seed is not implemented** -- `RemoteIndex` still calls
  `rxclass/class/byName`, which needs exact class names, so live drug-class
  binding is unreliable until the 1,319-class seed lands.
- ~~`bind.py` has never run with a real LLM selector~~ -- now wired via
  `llm.select_candidate`, but only smoke-tested, not measured at scale. The heuristic is deliberately
  conservative, so live-terminology refusal rates are an upper bound, not the
  real residue.
- The classifier has only ever run as the offline heuristic. No LLM has been
  wired in, so `prefilter_disagreements` has not been measured for real.
