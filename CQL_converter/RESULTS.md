# Track A: measured results

Everything here was measured, not estimated. Each entry says what was run, on
what, the number, and — separately — **what it does not establish**, because
most of these are engineering measurements that constrain the design rather
than the paper's headline experiments. M1, M2 and E3 all still need human
labels and none of them are answered below.

**Environment.** Python 3.14.2 · `z3-solver` 5.1.0 · `lxml` 6.1.2 · `pymupdf`
1.28.2 · `anthropic` SDK 1.2.0 · model `claude-opus-5` · SNOMED CT
international `20250201` via `tx.fhir.org` · LOINC via NLM Clinical Tables ·
VSAC accessed 2026-08-28. Measurements taken 2026-08-27/28.

**Corpus.** Five 2025–26 open-access practice guidelines from PubMed Central,
fetched via Europe PMC:

| PMCID | guideline |
|---|---|
| PMC13276589 | ASH/ISTH 2026, anticoagulant prophylaxis in paediatric VTE |
| PMC13270348 | European interdisciplinary, pain in acute pancreatitis |
| PMC13151662 | ESO 2025, blood-pressure management in stroke |
| PMC13131131 | ESMO–EURACAN, uveal melanoma |
| PMC13054209 | Deprescribing in older people |

## Reproducing

```bash
python3 tests/test_dsl.py && python3 tests/test_bind.py && python3 tests/test_segment.py   # 86 tests, offline
python3 segment.py PMC13276589 --out ash.jsonl                            # fetch + segment
python3 segment.py --review ash.jsonl --top 20
python3 bind.py --db tx.db --remote --registry "<a recommendation sentence>"
python3 <scratchpad>/probe_sampling_variance.py --formalizer real --k 8    # ~$1.10
```

---

# R1 · Provenance survives real publisher markup

**Measured.** 1,629 blocks across all five guidelines. For every block: does
`tree.xpath(locator)` resolve to exactly one element, and is the block's text
recoverable from that element?

| | |
|---|---|
| blocks | **1,629** |
| locator resolution failures | **0** |
| text-slice mismatches (non-table) | **0** |

The only mismatches anywhere were `table_row` blocks, and their count equalled
the table-row count exactly on every document — those are header-folded
renderings by design, documented as the single exception to verbatim
extraction, with cell-level verbatimness asserted separately.

**Supports.** That per-clause provenance — the unit of clinician adjudication
and therefore the unit of the fidelity metric (M2) — is achievable on real
markup rather than only on curated fixtures.

**Does not establish.** That the *right* statements were extracted. This is a
soundness property of the provenance chain, not a recall measurement.

---

# R2 · A hard ceiling on the JATS-first strategy: tables shipped as images

**Measured.** Of 32 `<table-wrap>` elements across the corpus, **26 (81%)
contain no extractable text** — every cell is `<td><graphic src="*.jpg"/></td>`.
All 26 are in one document (ESO, PMC13151662), where **26 of 26 tables are
images**.

**Consequence.** That guideline's formal recommendation tables are unreachable
from XML. The segmenter finds 18 recommendations there, all of them prose
restatements; the graded tables need the PDF path. The loader now emits a
`figure` block carrying the caption so these land in `figures_uncovered`
(64 for that document) rather than silently producing nothing.

**Supports.** A real, quantified limit on the "publishers already tagged the
boxes" argument, and a concrete reason the PDF path is not optional. Belongs in
the paper as a property of the corpus, not a limitation of the tool.

**Does not establish.** The population rate — one document dominates the
statistic. A larger sample is needed before quoting 81% as a corpus property.

---

# R3 · Documents are wildly heterogeneous

**Measured.** Per-document pipeline behaviour at the default threshold (1.5):

| document | blocks | units | candidates | % of units | recs | duplicates | uncovered | image tables |
|---|---|---|---|---|---|---|---|---|
| PMC13054209 | 98 | 265 | 93 | 35% | 18 | 1 | 2 | 0/2 |
| PMC13131131 | 196 | 250 | 68 | 27% | 52 | 0 | 4 | 0/0 |
| PMC13151662 | 318 | 544 | 18 | **3%** | 18 | 0 | 64 | **26/26** |
| PMC13270348 | 517 | 687 | 287 | 42% | 52 | **95** | 3 | 0/2 |
| PMC13276589 | 500 | 808 | 663 | **82%** | 88 | 25 | 2 | 0/2 |

Prefilter pass rate ranges **3% to 82%**. The extremes are explained: ASH/ISTH
is dense GRADE prose where nearly every paragraph is normative; ESO's normative
content is in images.

**Supports.** That a fixed threshold is not a defensible design, and that
per-document extraction statistics must be reported rather than pooled. It also
sets the cost model: 663 classifier calls for a single guideline.

**Does not establish.** Which end of the range is "correct" — that needs the
gold set.

---

# R4 · Deduplication changes the denominator by a third

**Measured.** Duplicate statements collapsed: **95** (PMC13270348), 25
(PMC13276589), 1 (synthetic fixture). Guidelines restate recommendations in an
executive summary, a per-section summary, *and* a table.

**Supports.** M1's formalizability fraction is a ratio, and its denominator is
inflated by roughly a third on some documents without deduplication. Any
reported fraction must state whether restatements were collapsed.

**Does not establish.** That the collapsing rule (body wins over summary) is the
right one; it is a heuristic, and `also_at` preserves the alternatives.

---

# R5 · Terminology retrieval fails in ways that are silent and clinically severe

Four measured failures, all against **live** terminology services.

**5a — an abbreviation resolved to the wrong domain entirely.** Querying live
LOINC for `eGFR` returns *"EGFR gene exon 20 insertion [Presence] in Blood or
Tissue by Molecular genetics method"* — the epidermal growth factor receptor
gene — and, in the top-3, nothing about kidney function. Being the only
candidate, an early version of the selector bound it to a renal threshold as a
"clear lexical win." Fixes: positive evidence required before binding, and
query expansion (`eGFR` → *estimated glomerular filtration rate*, which returns
correct concepts).

**5b — minimal pairs are lexically indistinguishable.** LOINC `9318-7`
(albumin/creatinine **mass** ratio, mg/g) and `14585-4` (**molar** ratio,
mg/mmol) are the same clinical threshold a factor of ~8.84 apart. They score
**identically** under the retrieval scorer. The stated unit resolves it
deterministically; with no unit stated the binder refuses.

**5c — lexical ranking inverts a clinical opposite.** SQLite FTS5 BM25 on the
query `kalemia` ranks **Hypo**kalemia *above* **Hyper**kalemia.

**5d — a documented API is unusable against prose.** RxClass
`class/byName` requires the exact class name: `"ACE inhibitor"` → **0 hits**,
`"ACE INHIBITORS, PLAIN"` → 2 hits.

**Supports.** That "look codes up rather than generate them" is necessary but
nowhere near sufficient, and that the retrieval layer needs its own
deterministic guards. These are concrete instances of the terminology-grounding
failure mode named in arXiv:2502.12065.

**Does not establish.** A rate. These are existence proofs found while building,
not a sampled error measurement.

---

# R6 · The deterministic gate works, and curated value sets are 6× richer

**Measured.**

- `CodeSystem/$validate-code` against SNOMED: `73211009` → **True**
  (*Diabetes mellitus*); the invented `99999999009` → **False**, *"Unknown
  code"*. The anti-hallucination property is checkable, not asserted.
- **Curated vs computed value sets.** VSAC's expert-authored *Diabetes* value
  set (version `20250115`) contains **773 codes**. The computed SNOMED
  `isa/73211009` expansion contains **124**.
- SNOMED `29738008` is *Proteinuria*, **not** *Albuminuria* — an error in a
  draft of ours that the lookup caught. `274769005` is Albuminuria.

**Supports.** That codes must be retrieved and verified, that VSAC is worth the
UMLS licence for M2 (the AHRQ CDS Connect reference artifacts bind to VSAC
OIDs, so this is what makes that comparison like-for-like), and that version
pinning is not optional.

**Does not establish.** That the curated set is *correct* for any given rule —
that is a clinician judgement.

---

# R6b · VSAC is wired in, and reveals that "diabetes" has no canonical machine definition

**Measured**, with a live UMLS licence, on
*"We recommend an ACE inhibitor or ARB in adults with diabetes and a urine
albumin-to-creatinine ratio of 30 mg/g or higher."*

| phrase | bound to | size |
|---|---|---|
| ACE inhibitor | VSAC `2.16.840.1.113762.1.4.1078.9` *ACE inhibitors* v20241228 | **375 codes** |
| ARB | VSAC `2.16.840.1.113762.1.4.1078.10` *ARBs* v20241228 | **417 codes** |
| albumin-to-creatinine ratio | LOINC `9318-7` *[Mass Ratio]*, selected by the stated mg/g | 1 code |
| diabetes | — **refused** | — |

The drug classes bind to curated, steward-versioned value sets — the thing the
computed `isa` expansion could not give.

**The refusal is the finding.** VSAC publishes **40 value sets named exactly
"Diabetes"**, from at least two steward roots, with versions spanning 2017 to
2026 and materially different scopes. The selector declined to pick among eight
of them. So:

> A concept as basic as "diabetes" has no single canonical machine-readable
> definition. The choice among steward value sets is a clinical decision, not a
> lookup.

**Supports.** A sharper version of the paper's argument. The usual framing is
that the *unformalizable* half is where the difficulty lives; this shows
under-determination inside the **formalizable** half too — the phrase binds,
but to which of forty curated sets is a judgement no amount of retrieval
settles. It also strengthens the version-pinning argument: two of these
"Diabetes" sets differ by nine years.

**Does not establish.** How often this happens. One phrase, one observation.
Worth measuring across a statement sample — the rate of *steward multiplicity*
would be a genuinely novel number, and it is cheap to compute.

**Engineering note.** Refusing here is conservative but defensible; the
alternative is an arbitrary pick recorded as if it were determinate. A middle
path exists — expand the top-k candidates and treat heavy overlap as
redundancy rather than ambiguity — at the cost of k extra `$expand` calls.
Not implemented.

---

# R7 · Constrained decoding cannot express a recursive grammar

**Measured** by probing `claude-opus-5`'s structured-output validator directly:

| construct | verdict |
|---|---|
| acyclic `$ref`, `enum`, `const`, nullable unions, `anyOf`, `minItems: 1` | accepted |
| **circular `$ref`** (`expr → expr`) | **rejected** |
| `minimum` / `maximum` on `number` or `integer` | rejected |
| `minItems` other than 0 or 1 | rejected |
| `oneOf` | rejected |
| `$defs` alongside a top-level `anyOf` | rejected |

A DSL expression tree is recursive by construction, so it is **not directly
expressible**. This is the practical form of the DEV_PLAN's point that the
target grammar is context-free and FSA-based decoders are unsound for it — it
surfaces as a flat HTTP 400, not as silent breakage.

**Workaround, and why it is sound.** `dsl.decoding_schema()` unrolls the
recursion to a fixed depth; `dsl.json_schema()` keeps the honest recursive
version for documentation. Every constraint dropped for decoding (`minItems: 2`
on junctions, confidence bounds) is re-checked by `validate()`. The decoder is
a filter, not the oracle.

**Side effect worth reporting.** Because `oneOf` is rejected, rule-vs-refusal
became a discriminator field on one object — which makes refusal a branch of
*the same* constrained decode rather than a second call. That turned out to
matter (R9).

**Supports.** E5 ("what does constrained decoding actually buy"), with a
concrete answer: it buys a closed vocabulary, at the cost of a bounded grammar.

---

# R8 · Grammar size forces a per-statement vocabulary

**Measured.** Maximum expression nesting depth that compiles, by registry size:

| registry | depth 1 | depth 2 | depth 3 |
|---|---|---|---|
| 13 variables (whole document) | accepted | rejected | rejected |
| 3 variables (one statement's bindings) | accepted | **accepted** | rejected |

Leaves must also be defined once behind a `$ref`; inlining them per level
multiplies the compiled grammar by the depth and fails even at depth 2.

**Consequence — architectural, not cosmetic.** The registry handed to the
decoder should be the **statement's** bound vocabulary, which is exactly what
stage 2 produces, not the whole document's. It buys expressiveness *and* stops
the model reaching for a variable the statement never mentioned.

---

# R9 · The refusal gate: separation is by refusal, not by disagreement

**The headline result.** 20 statements (10 precise, 10 vague, hand-written),
K=8 samples each — **160 real `claude-opus-5` calls** at `effort=medium`.
Agreement = share of the K samples in the largest **semantic** equivalence
class, decided by Z3 over the domain axioms, not by string comparison.

| | mean agreement | refusals / K |
|---|---|---|
| precise (n=10) | **0.99** | 0.1 |
| vague (n=10) | **0.00** | **8.0** |
| AUC | **1.000** | |

Perfect separation — **but not by the predicted mechanism.** All 80 vague
samples were **refused**. There were no competing encodings to disagree about,
so the disagreement signal had nothing to measure:

- AUC from **refusal rate alone**: **1.000**
- statements producing more than one semantic reading: **0 of 10** that were
  encoded at all

**Consequence.** Offered refusal as a branch of the constrained decode, the
model declines vague statements outright. **One call does what the K-sample
estimator was supposed to do**, at 1/8 the cost. On this evidence VERIMED's
ambiguity-by-sampling-variance (arXiv:2605.13817) is *redundant* for this task:
the refusal gate should lead with refusal-under-constrained-decoding and hold
sampling variance in reserve for borderline statements, if at all.

**Also relevant.** `temperature` is rejected with a 400 on `claude-opus-5`,
`sonnet-5`, `opus-4-8` and `opus-4-7`; only `sonnet-4-6` and `haiku-4-5` still
accept it. So the probe measures the spread from re-calling at default
settings — what a deployment actually sees. Worth stating plainly: VERIMED's
estimator assumes a temperature control current frontier models no longer
expose.

**Does not establish** — and this matters:

- The statements *and* their labels are both ours, and the precise/vague split
  is obvious by construction. Real guideline prose is far more borderline.
- The model refusing is **not** evidence that refusing was *correct*. That is
  precisely what E3's clinician adjudication is for.
- Read as: *the mechanism works and the cheap version suffices* — **not** as a
  calibration result.

**Incidental.** The single refusal among the precise statements was *"Screen
for retinopathy annually in adults with diabetes"* — arguably correct, since
"annually" states no window.

---

# R9b · The confidence score is graded, not just bimodal

R9's statements were designed to be obviously precise or obviously vague, so
every score came back 0.00 or 1.00. That says nothing about whether the score
is usable as a *ranking*, which is what a risk-coverage curve needs.

**Measured** on three deliberately borderline statements — each with one
precise half and one soft half — at K=8 real calls:

| statement | refused | confidence | decision |
|---|---|---|---|
| "…eGFR below 60 **who are symptomatic**" | 8/8 | — | **partial** (hole `has_symptomatic`) |
| "Monitor serum potassium **at least annually**…" | **6/8** | **0.25** | refuse |
| "…eGFR below 45 **and the patient is frail**" | 8/8 | — | refuse (hole `has_frail`) |

**The 6/8 is the point.** The score takes intermediate values on borderline
input, so sorting statements by confidence produces a real ranking and E3's
risk–coverage curve is meaningful rather than a step function.

**It also exposed a bug.** The first statement was refused 8/8 under the strict
vocabulary but encoded cleanly once its hole was declared — and was being
reported at confidence 0.00, which would sort a perfectly good `partial` to the
bottom of the curve. Confidence now describes the attempt the decision came
from. Fixed and regression-tested.

**And a known-bad encoding.** *"at least annually"* became the boolean hole
`has_at_least_annually`. That is temporal vagueness being forced into a boolean;
it should route to `unsupported_temporal` or to a temporal hole. Not fixed.

**Cost.** $0.47 for 3 statements at K=8 — higher than R9's per-statement rate
because `partial` runs a second sampling round with the holes declared.

---

# R10 · Semantic equivalence was load-bearing; string comparison inverts the result

**Measured.** Of the 10 precise statements, **9 produced textually different
encodings that were semantically identical** — up to **6 distinct JSON strings
for one meaning**, collapsed to a single equivalence class by the solver.

A string-comparison metric would have scored those 9 as ambiguous and
**inverted R9's conclusion**.

**Supports.** That the semantic-difference oracle (`distinguishing_patient` /
`equivalence_classes`, decided by Z3 over the domain axioms) is a load-bearing
component and belongs in the DSL, not in an experiment script. It is also what
P4's differential validation requires.

---

# R11 · P3's AND-for-OR bug does not reproduce under structured decoding

**Measured.** *"Refer to nephrology when eGFR falls below 30 or ACR is 300 mg/g
or higher"* encodes as `Or`, not `And`, under `decoding_schema`. The original
P3 result — a disjunction silently rendered as a conjunction, giving the wrong
referral verdict for 69.5% of the plausible patient box — was produced by a DSL
that *could not express disjunction*.

**Supports.** The design lesson already drawn (expressiveness limits must route
to refusal, never to silent approximation), now with the positive control: once
the DSL can express it and the decoder is constrained to the DSL, the bug does
not recur. Useful for the demo's backup counterexample framing — it should be
presented as a historical result about an impoverished DSL, not as a live
failure of the current one.

---

# R12 · First end-to-end run on a real guideline

**Measured.** All 14 boxed GRADE recommendations from PMC13276589 (ASH/ISTH
2026, paediatric anticoagulant prophylaxis), through segmenter → binder (live
VSAC + SNOMED + RxNav) → gate, at `samples=1`. 72 calls, **$0.63**.

```
encode   1        buckets:  PROP              3
partial  2                  unbound_concept   8
refuse  11                  defeasible        2
                            vague_predicate   1
in_fragment_rate  0.214     open holes       18
```

**This is not a defensible M1 estimate, for three reasons.** The
`unbound_concept` bucket (8/14) is dominated by *retrieval* failures, not by
unformalizable content — `CVAD`, `solid tumors`, `trauma`, `noncardiac surgery`
and `anticoagulant prophylaxis` all exist in SNOMED or VSAC and our index
missed them (R5 is the same problem at a smaller scale). Two calls failed on
schema complexity and fell back to heuristics. And the corpus is one guideline
in a specialty whose recommendations are almost entirely *population*-scoped
("in pediatric patients with X, suggest Y") rather than threshold-scoped —
which is why all three encodable statements are `PROP` and none are `LIA` or
`MTL`. It measures **our pipeline's coverage today**, not the guideline's
formalizability.

## R12a · A false ENCODE: the failure the gate exists to prevent

One statement encoded cleanly and was **wrong**.

> **Source:** *"In pediatric patients with persistently positive **APLA**
> without a history of thrombosis, the panel suggests no primary anticoagulant
> prophylaxis."*
>
> **Encoded:** `has_thrombosis == false → on_primary_prophylaxis == false`

`APLA` did not bind, so the model **silently dropped the entire population
restriction**. The rule now applies to *anyone* without a thrombosis history —
drastically over-broad, clinically wrong, and it passed as `ENCODE` with no
warning, because what remained still typechecked.

This is precisely the "silently wrong checker" the project argues against, and
it was produced by our own pipeline on the first real document.

**The fix, and why it generalises.** A strict encode is unsafe whenever the
binder refused *anything*: the strict registry cannot express the missing
concept, so the rule either dropped it or mis-encoded it — and dropping is
invisible. Any binder refusal now forces the partial path, where the missing
concept is available as a declared hole; if the model still does not use it,
that is a refusal, not an encoding. Regression-tested.

**For the paper:** this is a concrete instance of why compile-success is not a
correctness signal, generated by the system itself rather than argued from the
literature. It is also an argument for the binder and the gate being coupled —
a gate that cannot see what failed to bind cannot detect this class of error.

## R12b · Four bugs, each found only by running real data

Getting one valid run took four attempts. Each failure was real, and none would
have surfaced on the fixtures:

| bug | effect | why the fixtures missed it |
|---|---|---|
| `MENTION_SCHEMA` missing one `additionalProperties: false` | the API rejected it, the call fell back to the offline heuristic, and **the model path never ran at all** — a whole guideline returned zeros that looked like clean results | tests never call the API |
| `SqliteIndex` shared a connection across threads | whole run died under concurrency | tests were single-threaded |
| `check_nondegenerate` crashed on an ill-typed rule (`has_x == 60`) | one bad model output killed the process | fixtures only feed it well-typed rules |
| `MISSING_PROVENANCE` required `source_page` | **JATS has no page numbers**, so every XML-sourced rule failed a hard error and `encode` was unreachable — this alone produced a 14/14 refusal | fixtures set a page |

Two of these — the schema and the provenance check — silently produced a
plausible-looking **0% formalizability**. Both would have been reported as a
finding about guidelines rather than a bug in the tool.

**The generalisable lesson: a silent fallback is worse than a crash.**
`llm.USAGE` now tracks failed calls and prints, next to any result,
*"N FAILED CALL(S) — these fell back to the offline heuristic, so any result
above is NOT the model's"*. That line is what caught two of the four.
`dsl.check_decoding_schema()` now constraint-checks every schema offline
against the rules learned in R7, so the first bug cannot recur.

---

# R13 · M2 cannot be run against either obvious reference corpus

M2 asks whether the determinism machinery buys **meaning** or only
compilation, by scoring generated clauses against hand-authored expert CQL. Its
premise is that public expert CQL exists which encodes guideline
recommendations clause-for-clause. **Measured: for the two obvious sources,
it does not.**

## R13a · The AHRQ CDS Connect artifact is not a rules artifact

The DEV_PLAN names AHRQ CDS Connect as the reference. Its own header says
otherwise:

> *"The CDS is not a direct representation of any one recommendation statement
> within the guideline. Instead, the CDS compiles clinical concepts mentioned
> throughout the guideline in one consolidated summary for clinician review."*
>
> *"In the process of authoring the logic, certain assumptions were made and
> details developed when the guideline did not provide enough specificity."*

Measured on `FactorsToConsiderInManagingChronicPainFHIRv401.cql` (52 KB,
89 defines): **33 are `Report*` display formatters, 13 are helper functions**,
and the only clinical logic is
`Is18orOlder and (chronic-pain condition or recent opioid or recent non-opioid)`.
The numbers in the file are lookback windows (180 days, 2 years) and an age
floor. There is **no MME threshold, no benzodiazepine co-prescribing rule, no
naloxone offering rule** — i.e. none of the CDC recommendations that carry
thresholds. It is a retrieve-and-display artifact.

Worth quoting for a different reason too: the expert authors **added
specificity the guideline lacked**, and documented doing so. That is exactly
what the refusal gate exists to prevent, done deliberately by humans, in the
artifact proposed as ground truth.

## R13b · eCQM measures are cohort definitions, not recommendations

The natural substitute is CMS eCQM content (`cqframework/ecqm-content-cms-2025`,
10 measure bundles, 116 CQL files). These *do* carry real clinical thresholds:

```cql
define "Has Systolic Blood Pressure Less Than 140":
  "Lowest Systolic Reading on Most Recent Blood Pressure Day" < 140 'mm[Hg]'
```

But an eCQM population description has **no obligation**. Run through the
pipeline, the formalizer refused, and its reason is the finding:

> *"This is a measure population/denominator definition of 'adequately
> controlled' blood pressure, not a recommendation with an obligation."*

The DSL is `when -> then` where `then` must name an action a care plan decides.
A measure numerator is a **filter over a cohort**, not a directive. So arm (c)
correctly declines the entire corpus — its F1 of 0.000 in the run below is a
refusal, not a bad encoding, and must not be read as one.

## R13c · The run, and why it is not a result

Four statements x three arms, 27 calls, $0.44.

| arm | F1 | P | R |
|---|---|---|---|
| (a) direct CQL, no DSL, no terminology gate | 0.143 | 0.321 | 0.092 |
| (b) + bounded repair against the CQL checker | 0.143 | 0.321 | 0.092 |
| (c) bind -> constrained DSL -> deterministic lowering | **0.000** | 0.000 | 0.000 |

**Three reasons not to report these numbers as M2:**

1. **(c) produced no output at all** (R13b) — a refusal, correctly given, on a
   corpus of non-recommendations.
2. **(a) and (b) are identical** because the structural checker never rejected
   anything, so the repair loop never fired. Without the real `cql-to-elm`
   translator (P6) the (b) arm is indistinguishable from (a) by construction —
   which means **M2 cannot separate its two baselines until P6 lands.**
3. **The reference scope is wrong.** Each statement describes one population,
   but it is scored against every clause in the whole measure library (12-14
   clauses covering all populations, exclusions and value sets). That alone
   caps recall near 0.09. The reference must be narrowed to the define subtree
   for the population under test.

## What M2 needs to become runnable

- **A reference of the right kind**: expert CQL encoding *recommendations with
  obligations*. Neither source examined has it. Worth checking the remaining
  CDS Connect artifacts (the browsable repository at cds.ahrq.gov is off the
  network allowlist and would need a grant).
- **Or a re-scope, which is defensible**: an eCQM population *is* an
  applicability condition, so score our `when` against theirs on exactly the
  fields M2 names — predicate, threshold, comparator, unit, value set — and
  drop the obligation half. That tests the part the corpus can test, and the
  paper says so.
- **P6**, without which arms (a) and (b) are the same experiment.
- **Binder coverage**: "systolic blood pressure" did not bind despite LOINC
  8480-6 being exactly that (R5 again).

**For the paper.** The DEV_PLAN calls M2 *"the experiment most likely to fail
and must be run first."* It is failing, but not in the predicted way: not
"the constrained pipeline is no better than the baseline", but **"there is no
public expert artifact of the right kind to compare against."** That is a
finding about the field's reference standards, and it is worth reporting on its
own — the absence of clause-level gold data is precisely why compile-success
became the field's default metric.

---

# Costs

| | |
|---|---|
| per formalization call (`effort=medium`, cached prefix) | **~$0.007** |
| cached prefix tokens on a 4-call smoke test | 7,284 |
| the 160-call R9 probe | **~$1.10** |
| projected: segmenter classifier, one guideline (663 calls) | ~$3–5, half on the Batch API |

Prompt caching is effective: the instruction prefix is identical across a run,
so only the per-call content is billed at full rate.

---

# Threats to validity

1. **Corpus is small and open-access-biased** (n=5). PMC OA over-represents
   journals that publish structured recommendation abstracts; NICE, ACC/AHA and
   KDIGO are not open access. State the sampling frame.
2. **One document dominates R2.** 26 of the 26 image-tables are in a single
   guideline.
3. **R9's labels are ours.** Both the statements and the precise/vague ground
   truth were authored by us, and the split is designed to be separable.
4. **No clinician has adjudicated anything.** Every judgement in this document
   is either deterministic or the model's own.
5. **The offline heuristics are conservative**, so refusal rates measured
   without a model are upper bounds, not the real residue.
6. **The Docling PDF path has never executed** and `load_html` has never seen
   real markup.

# Not yet measured

- **Segmenter recall** against a human-marked gold set. Until it exists,
  "9/9 on the fixture" is a regression guarantee, not evidence.
- **M1**, the formalizability spectrum, with clinician raters and κ on the
  in-fragment/out-of-fragment binary.
- **M2**, clause-level fidelity against the AHRQ CDS Connect expert CQL.
- **E3**, refusal-gate calibration: precision on "compilable", risk–coverage
  curve, inter-rater agreement.
- **Binding accuracy at scale** — R5 reports existence proofs, not a rate.
- **P4** decorrelation. Note the axis "CQL vs typed Python" collapses if both
  encoders target this DSL; the available axes are decomposition and model
  family.

# R14 · Coverage is not a safety metric: two of two "encoded" rules were unsound

Run: `python3 demo.py --live --doc PMC13270348 --max 4` (European
Interdisciplinary Guidelines on Pain Management in Acute Pancreatitis — UEG,
EPC, ESPGHAN et al.). Reported result: **2 encoded, 2 refused, coverage 50%,
0 refusals attributable to retrieval.** Reading the bundle by hand, the true
figure is **0 of 4 faithfully encoded**. Both rules are defective, and each
fails in a different way.

## R14a · A binding collision produced a tautology that certifies every patient

`PMC13270348:0110` lowered to

```
when: has_ap == true AND pca == true
then: patient_controlled_intravenous_analgesia == true
```

`pca` and `patient_controlled_intravenous_analgesia` are two DSL variables
bound to the **same** code, SNOMED `1351574007`. The rule therefore denotes
`(A ∧ B) → B`: valid under every interpretation, unfalsifiable, and it returns
CERTIFIED-compliant for every patient ever tested against it.

`check_nondegenerate` returned `[]`. That is not a solver failure —
`VACUOUS_RULE` compares variable *symbols*, and these are two distinct symbols.
The identity exists only in the terminology layer, and nothing was checking
there. **The degeneracy check must run after binding, over codes, not over
names.**

This is worse than a refusal. A refusal is a true statement about the tool's
limits; a tautology is a false statement about the patient.

## R14b · A deontic modal was reified into a patient-level predicate

`PMC13270348:0143` put `has_may` in its antecedent, manufactured from the
phrase *"may be considered"*. "May" quantifies the **recommendation's own
force**; it is not a proposition about a patient, and no FHIR resource can ever
populate it. The rule is unsatisfiable in practice and returns UNKNOWN for
every input. `HOLE_FILLER`'s population-word stripping (GATE.md) has no notion
that modal verbs are a different category from clinical predicates.

## R14c · Two further defects visible in the same bundle

- `has_close_monitoring` (a hole) coexists with `close_monitoring` (bound to
  SNOMED `182777000`). One concept, two registry entries, and the rule used the
  unbound one — so the rule is reported PARTIAL despite the binding having
  succeeded.
- `close_monitoring` expands to **374 codes**. The descendants of "Monitoring
  of patient" sweep in most of the SNOMED procedure hierarchy, so the predicate
  is satisfied by almost any recorded procedure. Coverage counts this as a
  successful binding; precision is not measured anywhere.

## R14d · The finding for the paper

Every one of the four statements in this guideline is of the form *"we suggest
X may be considered"* — permissive, unquantified, no threshold anywhere. The
correct output is 0/4 or 1/4 encoded. **A guideline can be essentially
100% defeasible, and that is a reportable property of the guideline**, but only
a system willing to return zero can report it. The observed failure mode is a
formalizer that manufactures vacuous rules rather than emitting a refusal.

Two consequences:

1. **`attributable_to_retrieval` is uninterpretable on a run with false
   encodes.** It read 0 here, not because retrieval succeeded but because the
   retrieval channel was never exercised.
2. **The evaluation is missing an axis.** Refusal quality is measured (R9);
   the *faithfulness of the rules actually emitted* is not measured at all, and
   coverage alone strictly rewards false encodes. A hand-audited faithfulness
   rate over encoded rules belongs next to coverage, and probably above it.

# R15 · A document-derived abbreviation glossary, and what it costs

The collision in R14a is upstream-preventable: this document defines
*"Patient-controlled analgesia (PCA)"* in its own prose. `segment.py` now runs
one Schwartz & Hearst (2003) pass over the whole document before any statement
is triaged, and publishes the result in the manifest.

Measured over the 9-document PMC corpus:

| Document | Abbreviations |
|---|---|
| PMC12355350 (CKD) | 4 |
| PMC12948374 (ADPKD) | 6 |
| PMC13054209 (deprescribing) | 10 |
| PMC13074134 (diabetes/bariatric) | 8 |
| PMC13090725 (T2D exercise) | 60 |
| PMC13131131 (uveal melanoma) | 46 |
| PMC13151662 (stroke BP) | 34 |
| PMC13270348 (pancreatitis pain) | 20 |
| PMC13276589 (ASH/ISTH) | 27 |
| **total** | **215** |

Hand-audited: the first pass produced **3 false definitions in 221**
(1.4%) — two where the matcher walked out of the phrase into surrounding prose
(`95% CI → "…0.23-0.76). There were 246 fewer recurrent thrombotic events"`,
`Ovid → "Ovid), Embase"`) and one wrong expansion of a statistics token
(`RR → "received anticoagulant prophylaxis"`; RR is risk ratio). All three are
removed by two guards — reject an expansion containing a bracket or a sentence
boundary, and stoplist measurement vocabulary (RR, OR, CI, HR, SD, IQR, …),
which is never a clinical concept and whose document-local definition is
actively misleading (`HR` is *heart rate* in a cardiology guideline whatever
this document defined). Residual audited errors after the guards: **0 of 215**.

Three design points worth stating, because each was a choice:

- **The glossary never rewrites statement text.** `Statement.text` is verbatim
  and its character offsets index the source document (R1); rewriting would
  break provenance and the classifier's verbatim-quote check. Expansion applies
  to *retrieval queries only*.
- **Only two signals are trusted to merge two concepts**: a definition stated
  in the document, and a shared terminology code. Both are ground truth. An
  LLM synonym pass was rejected — asked whether "acute pancreatitis" and
  "severe acute pancreatitis" mean the same thing, a model will say yes, and
  that distinction is the entire content of several recommendations.
- **Requiring one reuse after the definition** drops society acronyms defined
  once in an author list (ESDO, EAGEN) without a hand-written stoplist.

One pass suffices because abbreviations are defined once, on first use, before
every use — there is nothing a second pass would find. Segment is the only
stage that sees the whole document; every later stage works one statement at a
time, which is why the sweep has to live there.

**This is prevention, not detection.** It stops bad names being generated and
improves retrieval precision, but it cannot catch two *unbound* concepts that
mean the same thing (no code to compare), nor a model that simply picks
different wording than the document used. The post-binding degeneracy check of
R14a is still required, and it is the sound half of the fix.

# R16 · The fix: canonicalise, then re-use the vacuity check you already have

R14 is repaired by one pass plus one check that already existed. Nothing here
is novel — vacuity detection is standard in hardware model checking (Beer et
al. 1997; Kupferman & Vardi 1999), concept normalisation is what UMLS CUIs are
for, and GEM/BRIDGE-Wiz separate deontic force from the condition by schema
design. The contribution is *where* they now have to run: these are all
**authoring-time** controls in the classical systems, and machine generation
removes the authoring step, so they must be re-imposed as **verification over
the generated artifact**.

## What was added

`dsl.canonical_aliases(reg)` merges two variables into one concept on exactly
two ground-truth signals — never a judgement call:

1. **A shared terminology binding.** Two bound variables carrying the identical
   `system|code|version` are the same concept; the terminology server said so.
2. **A hole restating a bound variable.** `has_close_monitoring` (unbound)
   beside `close_monitoring` (bound) is a duplicate, not residue.

`check_nondegenerate` now aliases the solver environment before asking Z3
anything, so **every existing call site** gets the check with no signature
change. `bundle_to_dict` canonicalises at the serialisation boundary and
records what it merged in a `merged_concepts` field — a merge is a claim about
the guideline, and a claim a committee cannot see is a claim it cannot check.

`dsl.MODAL_PREDICATES` / `modal_only()` reject a variable whose name says only
that the guideline is hedging. A closed word list is defensible here precisely
because English modals are a **closed class**; clinical concepts are open-ended,
which is why abbreviations needed a document-derived pass instead (R15).
`gate.holes_from` will no longer mint a modal hole — a hole promises that some
data source could one day fill it, and nothing will ever populate `has_may` —
and a rule rejected for `MODAL_PREDICATE` is now reported as `defeasible`
rather than falling through to the generic reason.

## Track B re-verifies rather than trusting the file

`from_dsl.load_bundle` re-runs `validate_rule` and `check_nondegenerate` on
every rule it loads and returns the failures **separately** from the
guideline's refusals. A bundle is a file: it can be written by an older Track
A, hand-edited, or produced by a formalizer whose checks have since been
strengthened. Folding these into the residue would corrupt the one number the
residue report exists to make readable — they are our defect, not the
document's.

## Before and after on PMC13270348

| | before | after |
|---|---|---|
| reported coverage | 2/4 = 50% | **0/4 = 0%** |
| faithfully encoded (hand-audited) | 0/4 | 0/4 |
| unsound rules shipped to Track B | **2** | **0** |
| registry variables | 9 | 7 |

The tool's reported number now equals the audited number. The two defective
rules are reported under REJECTED BY VERIFICATION, with `VACUOUS_RULE` and
`MODAL_PREDICATE` named, and are not loaded into the checker.

**0% coverage is the correct result for this document.** All four statements
are of the form *"we suggest X may be considered"*. The finding is a property
of the guideline, and only a system willing to return zero can report it.

## The contrast worth stating in the paper

Stubbing an unbindable predicate to `false` (PathWISE) and shipping a vacuous
rule are **opposite unsoundnesses**: stubbing makes a rule silently *fail*, a
vacuity bug makes it silently *pass*. Neither system checks for the other's
failure mode. Both are invisible without a solver query that nobody currently
runs.

Test coverage: 192 (16 added).

# R17 · Second live run: the net held, and exposed a worse bug than R14

Re-running `--live --doc PMC13270348 --max 4` after the R16 fixes. The R14
tautology is gone: *"PCA should be used with caution"* now comes back refused
as `vague_predicate` rather than encoded as `(A AND B) -> B`.

## R17a · The artifact shipped was not the artifact verified

Rule `PMC13270348:0143` referenced `close_monitoring`; the registry shipped
beside it did not contain that variable. Cause: `demo._merge_registries`
**re-bound every statement from scratch** to build the bundle vocabulary
instead of carrying forward the registry the rule was written against. Binding
runs through a model selector, so the second call disagreed with the first --
"close monitoring" bound at triage and did not bind on the rebuild.

Two things worth recording:

- **The surface symptom was a CQL translator error**
  (`Could not resolve identifier close_monitoring`). The translator was right;
  the defect was three stages upstream.
- **Track B's load-time verification caught it** as `UNKNOWN_VAR` +
  `NOT_LOWERABLE` and refused to load the rule -- a failure mode nobody
  anticipated, stopped by a check added for a different reason. That is the
  argument for verifying the generated artifact rather than trusting the
  generator, stated by an accident rather than by design.

Fixed by adding `Triage.registry`: the vocabulary now travels with the rule.
Re-binding survives only as a fallback for a Triage with no registry attached.

## R17b · Permission encoded as obligation -- a missing OPERATOR, not a missing check

Both rules this run encode a permissive recommendation as an obligation:

| Rule | Source | `strength` recorded | Encoded as |
|---|---|---|---|
| `:0143` | "epidural analgesia **may be considered** when expertise is available" | `weak (suggest); may be considered` | `recommend` |
| `:0160` | "acupuncture ... **may be considered**" | `weak (suggestion; may be considered)` | `recommend` |

`Rule` denotes phi = `when -> then`, which **is** an obligation. The DSL has no
permission operator, so a permissive recommendation cannot be faithfully
encoded in it at all -- and encoding one anyway lets the checker report
MUST-violate against a patient who merely declined an optional intervention.
`:0160` escapes that only by accident: its hole forces MAY-violate.

`strength` is currently **carried, not honoured** -- populated by the model,
serialised into the bundle, copied into `LoadedRule`, and read by nothing. A
field is not a mechanism.

This is the deeper form of R14b. The `MODAL_PREDICATE` check catches a modal
that becomes a VARIABLE (`has_may`); it cannot catch a modal that is silently
DROPPED, leaving a permission dressed as an obligation. Distinguishing them
matters for the paper: R14b is a bug, R17b is an expressiveness limit.

## R17c · Single-run numbers on n=4 are not stable

The two live runs on the same four statements disagreed: `:0110` went
encode -> refuse, the acupuncture statement went refuse -> encode. Both runs
scored 0/4 hand-audited faithful, by entirely different failure modes. Report
repeated runs or the union of failure modes; do not quote one run.

# R17d · Mitigation: deontic force caps the verdict

`Rule` cannot express permission, so the verdict layer now honours what
`strength` already recorded. `dsl.is_permissive(strength, source_span)` reads
both the model's summary and the verbatim source span; when it fires, a rule
that would have returned MUST-violate returns **`OPTION-not-taken`** instead,
and the API response carries `deontic: "permission" | "obligation"`.

Demonstrated on the same predicate and the same patient (conditions met,
action absent):

| Source | verdict |
|---|---|
| "We recommend anticoagulant prophylaxis" | `MUST-violate` |
| "epidural analgesia **may be considered** when expertise is available" | `OPTION-not-taken` |

**GRADE conditional is deliberately not treated as permission.** "We suggest
anticoagulant prophylaxis" still recommends the action; the weakness is in the
evidence, not in the obligation. Only explicit permission language counts
("may be considered", "is an option", "at the discretion of"), which keeps the
downgrade conservative -- treating every weak recommendation as optional would
silence most of a guideline.

This is a mitigation, not a fix. The soundness property it buys is worth
stating precisely, because it is checkable: **no permissive rule can return
MUST-violate.** The remaining limitation is unchanged -- the DSL has no
permission operator, so the distinction lives in a guard at the verdict layer
rather than in the logic. Note that CQL carries no deontic content either, so
this can only ever live at the verdict layer; the CQL lowering cannot express it.

On novelty: the operator itself would not be new. Obligation/permission/
prohibition is von Wright (1951), standardised in LegalRuleML, and already
present in guideline systems -- PROforma represents recommendations as
candidates with arguments rather than obligations, Asbru models preferences
among plans, GEM carries an explicit deontic-modality element. What is
reportable here is the MEASUREMENT (how much of a real guideline is permissive
rather than obligatory) and the soundness property above.

# R18 · Permission as an operator, not a guard

R17d capped the verdict by inspecting the strength string. `Rule.deontic` now
carries the operator itself, and the guarantee becomes structural.

```
obligation   O(then | when)   violation = when AND NOT then
permission   P(then | when)   violation = FALSE, unconditionally
```

Prohibition is not a third case: `F(p) = O(NOT p)`, which the DSL already
writes as `action="avoid"` with a negated consequent.

## What changed, and why each piece

**`violation_condition(rule, env)`** is the formula Track B negates. For a
permission it is `false`, so the soundness property is checkable directly on
the formula rather than asserted about a verdict string: *there is no patient,
and no completion of the unknowns, for which a permission is violated.* Test:
`z3.Solver().add(violation_condition(permission)) == unsat`.

**`deontic` is REQUIRED in the decoding schema**, so the model must commit to a
reading rather than letting a default quietly promote a permission into an
obligation. `check_decoding_schema` passes clean.

**The wording is a backstop, and only in one direction.** `deontic_of()` takes
the explicit operator, but promotes obligation → permission when the source
says "may be considered". It never demotes. Guessing permission wrongly
silences an alert, which is recoverable; guessing obligation wrongly accuses a
clinician, which is not.

**A permission gets its own verdict lattice**, because compliance is the wrong
question for it. Same rule, same patient, dispatched on the operator:

| known | obligation | permission |
|---|---|---|
| conditions met, action absent | `MUST-violate` | `OPTION-not-taken` |
| conditions met, action present | `CERTIFIED-compliant` | `OPTION-taken` |
| conditions not met | `CERTIFIED-compliant` | `OPTION-not-applicable` |
| nothing specified | `MAY-violate` | `OPTION-available` |

None of the four permission outcomes is an accusation. That is the point.

## What the operator BUYS: deontic conflict detection

`dsl.conflicts(rules, reg)` finds statements that **permit an action another
rule requires be absent, for a patient who satisfies both antecedents**. This
check cannot be written without the distinction: collapse permission into
obligation and "epidural may be considered" and "avoid epidural in
coagulopathy" are simply two obligations about the same variable, and their
disagreement is invisible. With the operator it is a decidable query.

Reported through Track B's rejection channel as `DEONTIC_CONFLICT`, never
auto-resolved — a guideline that contradicts itself is a finding for the
committee, not something a compiler should quietly pick a side on.

## CQL cannot carry this, and that is a finding about CQL

CQL is an expression language with no deontic content: `Violated` means the
same thing whether the source required the action or merely offered it. So the
distinction **cannot live in the lowering** — only at the verdict layer. What
the library can do is refuse to lie: it emits `define "Deontic": 'permission'`
and defines `Violated` as `false`, plus `OptionTaken` / `OptionNotTaken`, so
even a consumer reading only `Violated` cannot turn a declined option into a
violation. Both variants compile clean through the real HL7 `cql-to-elm`
translator (0 errors).

## On novelty, stated plainly

The operator is not new. Obligation/permission/prohibition is von Wright
(1951), standardised in LegalRuleML, and already present in guideline systems:
PROforma represents recommendations as candidates with arguments rather than
obligations, Asbru models preferences among plans, GEM carries an explicit
deontic-modality element. Do not claim it.

Three things here are reportable:

1. **The measurement** — what fraction of a real guideline is permissive rather
   than obligatory. Asserted qualitatively by the classical systems; not, as far
   as we can tell, quantified on modern guidelines.
2. **The soundness property** — no permissive rule can yield MUST-violate,
   checked on the formula.
3. **The target-language limit** — CQL cannot express deontic force at all, so
   any pipeline that lowers to CQL and reports violations from it will
   misreport permissive recommendations. That is a property of the ecosystem,
   not of this implementation.

Test coverage: 208 (11 added).

# R19 · The gold standard, and two checks that report rather than reject

## R19a · GOLD.md was written before any tool output was consulted

45 statements, 5 per document across the 9-document corpus, sampled
deterministically (`goldset.py --seed 0`) rather than taken from the top of the
prefilter ranking — the high-scoring statements are the easy ones, and a gold
set of easy statements measures nothing.

The criteria fix a decision order, four labels (`ENCODE` / `PARTIAL` /
`REFUSE` / `NOT_A_RECOMMENDATION`) plus an independent `deontic` label, and the
rules of thumb that resolve the disagreements we actually hit:

- the recommended **action must survive** into `then`;
- **outcomes are not conditions** ("acupuncture relieves abdominal pain **in
  patients with AP**" — AP is the population, pain is the effect);
- do not supply a threshold the document does not state;
- population restrictions are load-bearing.

`NOT_A_RECOMMENDATION` is labelled rather than discarded, because it is the
segmenter's false-positive rate and belongs in its own denominator. The sample
already contains obvious instances ("EPs and EtD frameworks are available
online for recommendation 6"), so that rate is not zero and has never been
measured.

`goldset.py --score` validates a labelled sheet before any agreement figure is
computed: reason codes present only on REFUSE rows, hole phrases verbatim in
the statement, deontic set. An agreement number over a malformed sheet means
nothing.

**The pre-registered outcome**: report Cohen's κ on the four labels and on
`deontic`, whatever it is. A κ below ~0.6 would itself be the finding —
formalizability would not be a well-defined property of a statement, which is a
stronger claim than any accuracy number.

## R19b · Two faithfulness checks, deliberately non-blocking

`ACTION_TARGET_MISMATCH` (warn) fires when no action atom in `then` is
lexically related to `Rule.target`. It catches the real `:0143`:

```
"epidural analgesia may be considered when expertise is available, and strict
 aseptic techniques and close monitoring are essential"
  ->  has_expertise_available -> strict_aseptic_techniques AND close_monitoring
```

The epidural is absent; its **precondition** was promoted to be the
recommendation. `NO_ACTION_ATOM` passed on a technicality, because aseptic
technique is itself an action variable.

`UNUSED_BOUND_CONCEPT` (evidence) reports concepts that bound and were then not
used. `DROPPED_CONCEPT_RISK` covers only the case where the *binder* failed;
this covers the binder succeeding and the model not using the result — which is
how a population restriction goes missing from a rule that still typechecks.

**Both report; neither rejects.** That is a reversal of what we first proposed,
and the reason is worth recording: on review, the "dropped concept" in
`PMC13270348:0160` was not dropped in error. `has_abdominal_pain` is the
**outcome** of the intervention, not its indication, and forcing it into `when`
would have been the wrong encoding. A check that blocks on it would have made
the output worse.

Matching is by prefix, not token equality: `target` is a human gloss and
abbreviates. Exact matching fired on a correct rule ("ACEi" vs
`on_ace_inhibitor`) and was caught by an existing test.

The general principle, and it is the same one behind the gold standard:
**soundness properties may block; faithfulness judgements may only report.**
A vacuous rule is wrong under every reading, so `VACUOUS_RULE` rejects. Whether
a concept belongs in `when` needs judgement — so it goes in the report, where a
human decides.

## R19c · The executor now reads the deontic marker

Stage 5 printed `Applicable=None -> compliant` for a permission while Z3 said
`OPTION-available`. The two lowerings disagreed in vocabulary, so the 6/6
cross-check figure did not cover permissive rules. The executor now reads the
`Deontic` / `OptionTaken` / `OptionNotTaken` defines the library carries.

Test coverage: 214 (6 added).

# R20 · First pass over the gold set: a 36% ceiling and a 27% segmenter error

45 statements, 5 per document across the 9-document corpus, labelled against
GOLD.md's decision order.

**Provenance caveat, stated first.** This pass was labelled by the same model
family that produces the encodings. It is therefore **not independent**, and
agreement between these labels and the tool's output is inflated by an unknown
amount. It is a sanity check, not the paper's gold standard — that needs two
human annotators and a κ. Every number below should be read as
order-of-magnitude.

## What is in the segmenter's output

| | n | share |
|---|---|---|
| **Not a recommendation at all** | 12 | **27%** |
| Real recommendations | 33 | 73% |

**27% of what the segmenter hands downstream is not a recommendation.** Rationale
prose ("Understanding the recommendation—Given the benefit…"), evidence
summaries with odds ratios, list stems ("the following should be considered:"),
regulatory facts, and one pure administrative note ("EPs and EtD frameworks are
available online for recommendation 6").

This number has never been measured, and it sits upstream of everything else —
every formalizability fraction previously reported has been computed over a
denominator that is a quarter noise.

Add the five statements that are directive but not about a patient (guideline
process, health-system access, research recommendations) and **17 of 45 = 38%
of extracted spans are not actionable patient content.**

## The formalizability ceiling

Of the 33 real recommendations:

| label | n |
|---|---|
| ENCODE | 7 |
| PARTIAL | 5 |
| REFUSE | 21 |

**Ceiling = 12/33 = 36%.** A perfect formalizer, with perfect retrieval and no
bugs, encodes about a third of the recommendations in this corpus. The other
64% is a property of the guidelines.

Refusal reasons, and note that the largest bucket is not the one the DSL is
usually criticised for:

| reason | n |
|---|---|
| `vague_predicate` | 7 |
| `not_patient_level` | 5 |
| `unbound_concept` | 4 |
| `defeasible` | 3 |
| `unsupported_temporal` | 1 |
| `beyond_dsl` | 1 |

Only **1 of 21** refusals is `beyond_dsl` — expressiveness of the fragment is
almost never the binding constraint. Vagueness and non-patient-level content
are. Adding operators to the DSL would buy almost nothing; the residue is in the
prose.

## The deontic measurement (R18's reportable number)

**5 of 33 = 15% of real recommendations are permissive** rather than
obligatory. Small, but not negligible — and every one of them would be
misreported as a violation by a pipeline that lowers to CQL and reads
`Violated`, because CQL cannot express the distinction.

## Boundary cases the criteria did not settle

Recorded because they are where a second annotator will disagree, and because
they are how GOLD.md gets sharpened:

- **Institutional capability as a proviso.** *"We recommend epidural analgesia …
  provided local expertise is available"* (`PMC13270348:0024`). GOLD.md files
  institutional capability under `not_patient_level`, but here the statement IS
  patient-level and only the proviso is not. Labelled PARTIAL with the proviso
  as a hole; a second annotator could defensibly call it REFUSE.
- **"should be considered" vs "may be considered".** Ruled: the modal governs —
  `should` is obligation, `may` is permission. Applied consistently, but it is a
  convention, not a discovery.
- **Statements carrying two recommendations.** Four of the 45. Labelled on the
  first clause with the split noted. The segmenter should be splitting these.
- **`unbound_concept` vs genuine residue.** `PMC13074134:0007` turns on "start
  of LRD (liver reduction diet)", which probably has no code anywhere — but that
  is a guess, and GOLD.md asks for it to be flagged for checking rather than
  assumed.

## What this changes

1. **Segmenter precision is now the biggest single lever**, and it was not on
   the list. Fixing a quarter of the input being noise is worth more than any
   further work on the formalizer.
2. **Coverage must be reported over real recommendations**, with the
   not-a-recommendation count published beside it. A "coverage" figure over raw
   extracted spans is not comparable to anything.
3. **36% is the number to beat, and 100% is not the target.** A tool reporting
   much above 36% on this corpus is either encoding things it should refuse or
   inventing thresholds.

# R21 · The model classifier had never been switched on

`llm.classify_block` has existed since the segmenter was written. Its docstring
is the instruction for using it — `segment.segment(..., llm=llm.classify_block)`
— and **no caller ever passed it**. Both `demo.py` and `goldset.py` called
`sg.segment(blocks, doc_id)`, so every run, `--live` included, judged candidates
with the offline word-counting heuristic while the model judged mentions,
candidate codes and encodings. Three of four model hooks were wired; the first
one was not.

## Why it hid

- **`llm=None` is a valid, working mode, not an error.** The heuristic runs, the
  pipeline completes, statements come out. A silent fallback is
  indistinguishable from success — the same failure shape as the
  `MENTION_SCHEMA` bug (R7), where a missing `additionalProperties` meant the
  model path never ran at all.
- **The tests pass identically either way.** `test_segment` deliberately runs
  offline with no key, so it exercises the heuristic by design and can never
  notice that production does too.
- **Nobody had measured segmenter precision**, so no number looked wrong. It
  took building the gold set to produce a figure that did.
- **The manifest recorded the truth on every single run**
  (`"classifier": "heuristic"`) and nothing printed it. The evidence was present
  and invisible. The segmenter line now ends `[judge: heuristic|llm]`.

## Measured effect

The model classifier, run over the same 45 gold statements:

| | n |
|---|---|
| non-recommendations correctly rejected | **11 of 12** |
| real recommendations kept | 29 of 33 |
| false positives remaining | 1 |

The single remaining false positive — *"A deprescribing approach encourages
prescribers to consider the need for a medicine each time a prescription is
re-issued"* — was flagged as borderline when the gold label was assigned. It
reads as directive and as descriptive, and the disagreement is honest.

**All four "false negatives" are statements neither judge would encode.** Three
are ones the gold set called `REFUSE / not_patient_level` (guideline
methodology, meta-guidance to readers, a research agenda item) and one
`REFUSE / defeasible`. The model files them as "not a recommendation"; the gold
criteria file them as refusals. **The same decision, recorded in two different
places** — and it is a real gap in GOLD.md, because the two land in different
denominators.

## The denominator, before and after

| | heuristic | model |
|---|---|---|
| spans reaching the pipeline | 45 | 30 |
| of those, not a recommendation | 12 (27%) | 1 (3%) |
| encodable (ENCODE + PARTIAL) | 12 | 12 |
| **coverage over what reaches the pipeline** | **27%** | **40%** |

No encodable statement is lost — all 12 survive. The gain is entirely from not
passing noise downstream.

## The lesson worth generalising

Three of this project's most expensive bugs share one shape: **a fallback that
works.** `MENTION_SCHEMA` fell back to a heuristic; `_merge_registries` fell
back to re-binding; the classifier fell back to word-counting. None raised, none
failed a test, and each produced plausible output for weeks.

The countermeasure that would have caught all three is the same: **make the
artifact state which path produced it, and print it.** The manifest already knew.

# R22 · Three deterministic vetoes: 7 of 12 for free, 0 real statements lost

Rules applied before any judge sees a candidate, so the model is only asked
about spans that genuinely need judgement:

| veto | catches |
|---|---|
| `list_stem` | ends in a colon — a lead-in to a list, whose items are separate blocks |
| `evidence_summary` | odds/hazard ratios, `95% CI`, `n =`, p-values, "certainty of evidence" |
| `about_a_recommendation` | "recommendations **that** …" reports one; "recommendation 6" names one |

**Every rule is guarded by directive language.** A span that also states a real
obligation is never vetoed, because table rows fold the recommendation together
with its evidence grading — *"We recommend epidural analgesia …; Certainty of
evidence: Good practice Statement-ungraded"* — and a rule keyed on the grading
alone would delete the recommendation with it.

## Measured on the gold set

| | |
|---|---|
| non-recommendations vetoed for free | **7 of 12** |
| left for the model to judge | 5 |
| **real recommendations wrongly deleted** | **0** |

Across the full 9-document corpus: 166 spans vetoed (121 evidence summaries,
31 list stems, 14 pointers), against 395 recommendations kept.

## What the first version got wrong, and why the rule gave up yield

A looser `about_a_recommendation` — "a / the / this / conditional
recommendation" — caught 8 of 12 but deleted a real statement:

> *"Although the panel made a conditional recommendation for no primary
> anticoagulant prophylaxis for persistently positive APLA, panel members
> emphasized the importance of considering individual patient circumstances …"*

That is `defeasible` **residue**, not noise. And the asymmetry is the point:
**a vetoed span vanishes; a refused one is counted and explained.** Deleting it
removes it from the denominator and understates the residue, which is the one
number the refusal report exists to make readable. The rule was narrowed to the
two constructions that cannot introduce a clinical action, trading one catch for
zero false vetoes.

The prefilter score explains why these get in at all: the evidence sentences
score 3.00 on `grade_certainty`, because "low certainty" is exactly the
vocabulary a graded recommendation uses. The trap is structural, not incidental.

## Combined effect

| stage | non-recommendations passed |
|---|---|
| heuristic prefilter alone | 12 of 45 (27%) |
| + deterministic vetoes | 5 |
| + model classifier | ~1 |

Test coverage: 223 (9 added).

# R23 · Terminology binding benchmark: 77% now, 90% reachable

97 distinct clinical phrases, extracted by `propose_mentions` from the 33 real
recommendations in the gold set (the 12 `NOT_A_RECOMMENDATION` rows are
excluded — binding a heading measures nothing). Reproducible:
`python3 bindbench.py --build` / `--score`.

**This replaces an earlier "79/97" figure that was quoted from a session
transcript, computed over 4 documents, recorded in no file and reproducible by
no script.** It should not have been repeated, and it is not the same 97.

```
binding: 75/97 = 77%
  attributable to OUR retrieval : 12   (a code exists and we missed it)
  real residue                  : 10   (no faithful code in any vocabulary)
```

**Reachable ceiling: 87/97 = 90%.** Real terminology residue is 10%.

## The 12 failures are five patterns, not twelve problems

| pattern | phrases | fix |
|---|---|---|
| **over-specific descendants** — the generic parent is never offered | `blind` (only *word/snow/music blindness*), `epidural analgesia` (only `Analgesia`) | head-noun backoff, as for `mechanical thrombectomy` |
| **verb-phrase mentions** — prose names an action as a gerund, SNOMED as a noun | `BP lowering`, `volume optimisation`, `early mobilisation`, `glucose lowering therapies` | nominalise the query, or search the object noun |
| **in the index, simply unretrieved** | `albuminuria` (274769005), `kidney function` (`Renal function` 11953005) | ranking / kind-filter bug — these should already work |
| **abbreviation never expanded** | `APLA` → retrieval returned *nothing*; `Antiphospholipid antibody measurement` (82886000) is in the index | pass the document glossary (already built — see below) |
| **compound "A or B"** | `masked or nocturnal hypertension` | `split_compound` handles slashes only |

## The 10 that are real residue split three ways

- **Umbrella terms with no single referent** — `local complications` (of AP),
  `local therapy` (oncology). Correctly unbindable; they name a set.
- **Concepts SNOMED International lacks** — `Deprescribing`, `ADWEs`,
  `disease progression`, `Haematoma expansion`. Note `Medication review`
  (314529007) exists and is a *different act*; binding to it would be wrong.
- **Thin vocabulary in exercise medicine** — `weightlifting`, `moderate
  overtraining`. A specialty-coverage finding, not a pipeline finding.

Plus one that is neither: **`replaces >50% of the globe`** was proposed as a
*concept*. It is a numeric predicate over tumour extent and belongs in the
logic, not the vocabulary — a `propose_mentions` error counted here as residue,
which slightly overstates the residue figure.

## Two caveats that make 77% a FLOOR

1. **The harness does not pass the document abbreviation glossary.** `bind()`
   is called directly, so `doc_abbrevs` is empty — while the real pipeline
   threads it through `bind_statement`. `APLA` failed for exactly this reason
   and would likely bind in a live run. The benchmark under-measures.
2. **The run included the head-noun backoff** added while diagnosing
   PMC13151662. There is no before-number to compare against, so the delta from
   that change is unmeasured.

## Why this exists

Binding failures were being found one document at a time — each run surfaced
one or two, they were fixed, the next surfaced two more. Twelve failures seen
together are five patterns; seen one per document they are twelve separate
bugs and an apparently endless loop. The benchmark also makes a retrieval
change checkable against the 75 phrases that already work, which no previous
change was.

# R24 · Retrieval fixed, and the failure moves to dropped quantities

## R24a · What the benchmark bought

Three changes, each justified by a measurement rather than by the example that
motivated it:

| change | evidence |
|---|---|
| **`lab` gains a kind fallback** (`condition`, `procedure`) | `lab` had none, so a whole class was unreachable: SNOMED files `Albuminuria` (274769005) as a FINDING and `Antiphospholipid antibody measurement` (82886000) as a PROCEDURE. Retrieval recall on the fixable failures **1/12 → 3/10**. |
| **head-noun backoff** | +1 on the 75 phrases that already bind (57 vs 56), 0 regressions, and it fixed `mechanical thrombectomy` end to end. **No measurable gain on the fixable-failure set** — justified by one case, not by the benchmark. Recorded as such. |
| **`canonical` asks for the terminology's word**, not just an abbreviation expansion | The residual misses are clinical synonymy a keyword index cannot bridge: `kidney function`/`Renal function`, `volume optimisation`/`Fluid management`, `BP lowering`/`Antihypertensive therapy`. The model proposes a QUERY; the index still supplies the code. |

Benchmark buckets, after correcting two codes I had guessed from memory
(`182832007` is *Drug administration*, not antihypertensive therapy;
`706882009` is *Hypertensive crisis*, not masked hypertension — both caught by
verifying every claimed code against the index):

```
binding: 75/97 = 77%
  OUR RETRIEVAL   : 10   the code is in our index and the query missed it
  VOCABULARY GAP  :  2   the code is not in the releases we load
  REAL RESIDUE    : 10   no faithful code in any standard vocabulary
  reachable ceiling: 85/97 = 88% by fixing retrieval alone
```

`VOCABULARY GAP` is a third bucket added on evidence: "absent from our loaded
release" and "absent from every vocabulary" have different fixes, and merging
them would make an incomplete ATC seed look like a property of the guideline.

## R24b · The same bug had a second call site

`demo.py` stage 2 re-bound each statement from scratch to build the registry
for CQL generation — the identical mistake fixed in `_merge_registries` at
R17a, missed in the CQL path. The rule referenced `on_bp_lowering` while the
freshly-bound registry had `on_lowering_systolic_bp`, and cql-to-elm reported
*"Could not resolve identifier"*. Both libraries compile once the registry
travels with the rule.

**A non-idempotent step needs its output carried everywhere, not re-derived in
some places.** Fixing one call site did not fix the class.

## R24c · PMC13151662, before and after

| | before | after |
|---|---|---|
| encoded | 0/5 | **2/5** |
| refusals attributable to our retrieval | 4 | 3 |
| CQL compiles | — | 2/2 |
| `mechanical thrombectomy` | unbound | SNOMED 21710002 `Intracranial thrombectomy` |

## R24d · Both encoded rules are unfaithful, and nothing caught it

```
"We recommend against intensively lowering systolic BP < 140 mmHg in the
 first 24 h after successful mechanical thrombectomy"
   ->  mechanical_thrombectomy  ->  NOT lowering_systolic_bp
```

The **140 mmHg threshold** and the **24 h window** are both gone. The rule now
says *never lower systolic BP after thrombectomy*, which is not what the
guideline says and is clinically wrong — targets in the 140–180 range are
standard care.

```
"We advise against routine pre-hospital BP lowering in suspected stroke"
   ->  has_stroke  ->  NOT on_bp_lowering
```

"routine" and "pre-hospital" are gone, and `has_stroke` binds to
*Cerebrovascular accident* (230690007) though the source says **suspected**
stroke. The rule forbids BP lowering in stroke generally.

**No check fires.** `UNUSED_BOUND_CONCEPT` cannot see it, because a threshold
is a literal rather than a bound concept. `ACTION_TARGET_MISMATCH` passes — the
action is right. The rule typechecks, is non-vacuous, contains an action atom,
and compiles.

This is the fourth distinct failure class, and the pattern across all four is
now clear: **each fix removes a failure mode and reveals the next, and every
one of them is a form of the model silently dropping content that the checks
cannot see is missing.** The countermeasure that generalises is not another
special-case check but a comparison against the source: a source span with a
number and a unit whose rule contains no numeric comparison has dropped a
threshold, and that is deterministic and cheap to detect (`DROPPED_THRESHOLD`).
The same applies to a stated duration with no `Since` in the rule.

# R25 · `DROPPED_THRESHOLD`: comparing the rule against its source

Every other check inspects the rule in isolation and asks whether what is there
is correct. None can see what is **missing**, which is why the PMC13151662
rules passed all of them while forbidding BP lowering after thrombectomy
outright — the 140 mmHg threshold and the 24 h window were simply gone.

The only thing that can catch a deletion is a comparison against the source,
which the rule already carries in `source_span`. Lives in `validate_rule`, so
it runs at the gate, in the CQL repair loop, and again at Track B's load-time
re-verification.

## Behaviour

| source | rule | verdict |
|---|---|---|
| `< 140 mmHg … first 24 h` | `mt → NOT lowering` | **flagged**: 140 mmhg, 24 h |
| `< 140 mmHg … first 24 h` | `mt ∧ sbp < 140 → NOT lowering` | flagged: 24 h only |
| `the first 24 h` | `Since(mt, days=1)` | not flagged — same window, different unit |
| `a minimum of 150–240 min` | `mins >= 150` | not flagged — a dash is one quantity with two endpoints |
| `BP <185/110 mmHg` | `sbp < 185` | **flagged**: 110 mmhg — a slash is two measurements |
| `OR 0.97; 95% CI 0.87–1.08; 25 studies; n = 15,374` | anything | not flagged — evidence, not thresholds |

Three decisions worth recording, each made because the naive version was wrong:

- **Bare integers and percentages are excluded.** Only numbers with a clinical
  unit count. "25 studies", "30% of patients" and "(2A)" are evidence, and
  flagging them would bury the signal under every graded recommendation.
- **A dash and a slash mean different things.** `150–240 min` is one quantity
  with two endpoints, kept by either; `185/110 mmHg` is systolic AND diastolic,
  and dropping either is a real loss. Treating both as ranges would have missed
  a dropped diastolic; treating both as pairs would have false-alarmed on a
  correct `>= 150`.
- **Durations convert.** "the first 24 h" and `Since(event, days=1)` are the
  same window; without conversion the check fires on a correct encoding.

Measured over the 45 gold statements: 5 carry clinical quantities, and **no
statistical quantity is extracted from any of them** — including the
deprescribing evidence sentence with an odds ratio, a confidence interval, a
study count and an n.

## Why this one closes a class

The four failure modes found so far — the tautology, the modal predicate, the
re-derived registry, and now the dropped threshold — are all the same thing:
**content silently absent, invisible to checks that only look at what is
present.** `DROPPED_THRESHOLD` is the first check that asks the other question,
and it generalises: any dropped quantity, in any guideline, whatever the number.

It is a **warning**, per the rule established at R19b — soundness properties may
block, faithfulness judgements may only report. A guideline sentence can carry a
number that legitimately does not belong in the rule.

Test coverage: 231 (8 added).

# R26 · Lazy classification: 286 model calls to 15, same answer

Wiring the model classifier on (R21) made segmentation cost one call per
candidate, and a guideline has hundreds. PMC13270348 has **286 candidates**, so
`demo.py --live --max 5` spent 286 calls — several minutes and most of the run's
cost — before the first statement was formalized, 281 of them on statements the
caller had asked to skip. `--max` bounded formalization and not segmentation.

Candidates are now judged in descending prefilter order, stopping after
`limit * 2 + 5`. Measured on PMC13270348 with a judge that mirrors the offline
heuristic so the two passes are comparable:

| | classifier calls | top 5 |
|---|---|---|
| full pass | 286 | `0110, 0142, 0159, 0205, 0087` |
| `--max 5` | **15** | `0110, 0142, 0159, 0205, 0087` |

**Identical, and 95% cheaper.** The saving is legitimate only because the answer
is unchanged, and it is unchanged because candidates are judged in the same
descending-score order the caller selects in — `demo.py` already sorted by
prefilter score and took the top N.

Three properties, each pinned by a test:

- **The budget is absolute, not proportional.** Judging stops after
  `limit*2 + 5` however long the document is, so the saving grows with document
  size: 95% on a 286-candidate guideline, 50% on a 30-candidate fixture. The
  margin over `limit` covers what dedupe and the classifier's own rejections
  remove.
- **Statement ids stay positional.** They key to the candidate's place in the
  DOCUMENT, not to the order it was judged in, so a bundle built with `--max` is
  comparable to one built without.
- **The manifest says the pass was partial** (`limit`, `unclassified`), and the
  demo prints it. `recommendations` must never be read as a document property
  when only part of the document was judged — the same discipline as
  `[judge: llm]` and `vetoed_by_reason`.

Note the cost of the R21 fix was invisible until someone ran it: the heuristic
classifier was free, so nobody had ever paid per candidate. **A correctness fix
can be a performance regression, and neither the tests nor the manifest would
have said so.**

Test coverage: 237 (6 added).

## R25a · The new check broke a hand-written fixture, twice

Two bugs surfaced on the first real run after `DROPPED_THRESHOLD` landed.

**1. `real_rules.py` treated warnings as fatal.** Its import-time guard was
`if _issues:` — every issue, not every error. `OPEN_HOLE` and
`TAUTOLOGICAL_ANTECEDENT` had always been warnings, so the assertion was
over-strict before this check existed; adding an advisory check turned it into
an import crash that took down `api.py` and the demo's verify stage. Now blocks
on errors and prints warnings to stderr.

**2. The check itself was wrong about doses.** The fixture rule

```
when:   apixaban_on AND (>=2 of age>=80, weight<=60kg, creatinine>=1.5)
then:   apixaban_dose_reduced == true
target: "reduced apixaban dose (2.5 mg twice daily)"
```

is correct: the criteria are numeric comparisons, the action is a boolean, and
the dose is recorded in `target`. A **dose belongs to the action**, so flagging
2.5 mg was a false alarm on a hand-validated rule.

**And the first fix for that was immediately defeated by real output.** Allowing
any quantity found in `target` to discharge the check let
`PMC13151662:0002` pass — the model had written
`target="intensive lowering of systolic BP < 140 mmHg in the first 24 h after
successful mechanical thrombectomy"`, the whole sentence restated, so the
threshold and the window both appeared there. The check went green on the very
rule it was written to catch.

Narrowed to **dose units only** (`mg`, `mcg`, `ug`, `g`, `iu`, `unit`, `ml`),
excluding `kg` (body weight), `mmHg` and `mmol/L` (thresholds), `mg/dL` and
`mg/g` (lab values), and every duration. A threshold and a window are
*conditions* and must be in the logic; only a dose is discharged by naming it.

Both behaviours are now pinned by tests, including one that asserts a
sentence-restating target cannot launder a threshold.

**The general lesson**: an exemption written to fix a false positive is an
attack surface. The model writes `target`, so anything keyed on `target` is
keyed on model output — and the first version was defeated within one run.

# R27 · Audit for the `real_rules.py` bug class, and what it found

The bug at R25a — an advisory result consumed as a fatal one — prompted a sweep
of every site that consumes a mixed-severity or partial result.

## Severity handling: one offender, already fixed

Every other production consumer filters correctly:

| site | |
|---|---|
| `from_dsl.load_bundle` | `[i for i in ... if i.severity == "error"]` ✓ |
| `cql.synthesize` | filters before repairing, and again before compiling ✓ |
| `gate._best` | returns early on errors; caller re-filters ✓ |
| `demo.py` compile gate | `if i.severity == "error"` ✓ |
| `real_rules.py` import guard | **`if _issues:`** — fixed at R25a |

Twelve *tests* assert `validate_rule(...) == []`, which does break on any new
advisory check. Left as they are, and the distinction is worth stating: **a test
may assert total cleanliness; a production guard must not.** A test that breaks
when behaviour changes is doing its job at development time. `real_rules.py`
broke at *import* time, in a shipped module, taking `api.py` and the demo's
verify stage down with it.

## The same shape, in a worse place: swallowed transport failures

Three sites in `bind.py` wrapped a remote terminology lookup in
`except Exception: pass` / `return []`:

- `RemoteIndex.search` — SNOMED, LOINC and RxNav lookups
- `VsacIndex.search` — value-set lookups
- `CachedIndex` — the VSAC merge

**A dead endpoint returning nothing is indistinguishable from a vocabulary that
does not contain the concept.** The binder refuses, the refusal is labelled
`unbound_concept`, and `residue_report` attributes it to the GUIDELINE — so a
timeout, a 503, or an expired UMLS key is published as a finding about clinical
prose. Every "real residue" number in this document could have been inflated by
a network blip and nothing would have said so.

This is strictly worse than the `real_rules.py` bug: that one *crashed*, which
is loud. This one produces a plausible number.

**Fixed by recording rather than raising.** Every index now carries a
`TransportFailures` record (`SqliteIndex` too, so callers never type-test).
`gate.triage` compares the count before and after binding a statement and tags
any growth as `TERMINOLOGY_UNAVAILABLE`, which both `spectrum`'s
`retrieval_attributable` and `residue_report`'s `_OUR_FAULT` now recognise. The
demo prints a warning naming the failed lookups. One failed lookup does not
abort a run — the failure is recoverable — but it can no longer be invisible.

Pinned by a test that binds a statement against an index whose every lookup
raises `ConnectionError`, and asserts the resulting refusal is attributed to
retrieval rather than to the guideline.

## The pattern across R21, R24b, R25a and R27

Four bugs, one shape: **a failure that returns a usable value.**

| | swallowed | looked like |
|---|---|---|
| `MENTION_SCHEMA` (R7) | a 400 from the decoder | the model finding nothing |
| classifier off (R21) | `llm=None` | the heuristic working |
| re-derived registry (R17a/R24b) | a differing second binding | a valid registry |
| transport failures (R27) | `except Exception: pass` | an empty vocabulary |

None raised. None failed a test. Each produced plausible output for weeks.

The countermeasure is the same every time and is now applied in all four
places: **make the artifact state which path produced it, and print it** —
`[judge: llm]`, `vetoed_by_reason`, `merged_concepts`, `TERMINOLOGY_UNAVAILABLE`.
A silent fallback is a design decision to hide a failure; if a fallback is
correct to have, it is correct to announce.

Test coverage: 244 (3 added).

# R28 · Two reporting failures found by hand-auditing a good run

PMC12355350 produced the best output so far — three faithful rules, correct
De Morgan on "against A **or** B" (`¬A ∧ ¬B`, the R11 bug class), disjunctive
populations preserved, real VSAC expansions, zero retrieval-attributable
refusals. Auditing it by hand still found two defects, both in *reporting*
rather than soundness: nothing unsound shipped, but the residue was understated.

## R28a · `UNUSED_BOUND_CONCEPT` never ran on the PARTIAL path

```
"...weak recommendation in favour of SGLT-2 inhibitors or GLP-1 receptor
 agonists; AND a weak recommendation against finerenone in adults with CKD"
```

`on_finerenone` bound, reached the registry, and was never used — the second
clause was dropped in both `:0048` and `:0049`. The check written for exactly
this (R19b) only ran on the strict-encode branch, and both rules were PARTIAL.

**A check placed on one of two success paths covers half the cases.** The
partial path is where a rule is most likely to lose content, because it is
reached precisely when the first attempt already failed.

## R28b · Two non-recommendations reached the pipeline

Both were refused downstream, so nothing unsound shipped — but they inflate the
denominator, and one minted four junk holes
(`has_adequate_funding_resources`, `has_outcomes_critical_importance`), which
were half the run's reported "8 open holes".

| statement | why it escaped |
|---|---|
| *"Understanding the recommendation—Given the benefit … (high certainty evidence) …"* | `_EVIDENCE_MARKERS` required `certainty **of** evidence`; GRADE rationale prose writes "high certainty evidence" with no preposition. |
| *"Results indicated that patients generally preferred … to **avoid** combination therapy"* | The word `avoid` satisfied the directive guard. A reported preference contains directive vocabulary without being a directive. |

Both are now vetoed deterministically: the certainty pattern makes the
preposition optional, and a new `reports_research` veto fires on a
**sentence-initial** research frame ("Results indicated", "Studies show",
"In randomised trials") — checked *before* the directive guard, so `avoid`
cannot rescue it, and suppressed when the guideline speaks in its own voice
("we recommend", "is indicated"), so *"Studies show X; we therefore recommend
Y"* survives.

Deterministic vetoes on the gold set: **7 → 9 of 12, still 0 real
recommendations deleted.**

## R28c · Ordering bug the fix introduced

Widening the certainty pattern made it match *"Strong recommendation, moderate
certainty evidence"* — a **GRADE label**, which the segmenter absorbs and
attaches to its sibling recommendation as the strength. The veto ran first and
discarded it, losing the strength label.

Label absorption now runs before the veto. **A filter and an extractor competing
for the same text is an ordering question, and the extractor wins**: absorbing
carries information forward, vetoing destroys it.

Test coverage: 250 (5 added).

# R29 · PMC13074134: 0/5, an expressiveness limit, and a starvation bug

Perioperative diabetes (JBDS). **0 of 5 encoded** — and unlike the earlier 0/5,
most of it is correct.

## R29a · Dose titration is genuinely outside the fragment

Three of the five statements are relative dose adjustments:

```
"Reduce basal insulin by 50% at start of LRD"
"Reduce basal insulin dose by 35%-50% of pre-LRD dose"
"Reduce the total daily insulin dose by 50% at start LRD"
```

`Cmp(var, op, value)` can say `insulin_dose <= X`. It cannot say *reduce by 50%
of the pre-LRD dose*, which relates two states of the same variable across
time. The refusal `beyond_dsl` with reason "the recommendation's core action is
a quantitative dose adjustment" is exactly right.

This is the **first time `beyond_dsl` has been the operative reason on real
data** — the gold set had 1 of 21 refusals in that bucket, and R20 concluded
that expressiveness is almost never the binding constraint. This document is
the counterexample: perioperative medication guidance is largely titration, and
titration needs a before/after relation the fragment does not have. Worth
reporting as a specialty-dependent finding rather than a general one.

## R29b · Retrieval attribution was over-counting

The run reported **5 of 5** refusals attributable to our retrieval. Only 2 were.
`retrieval_attributable` counted any non-encodable statement carrying a binder
miss, without checking that the refusal REASON was retrieval-related — so a
`beyond_dsl` refusal that happened to contain one unbindable concept was
reported as our bug. Now requires `reason_code == "unbound_concept"`.

This one matters more than it looks: that number is the paper's claim about
whether residue belongs to the guideline or to us, and it was inflated.

## R29c · The FTS filter was applied AFTER the limit

`SqliteIndex.search` fetched the top 40 BM25 rows across **every kind and
system**, then filtered by kind in Python. For a common word the forty slots
fill with the wrong kind and nothing survives:

```
"insulin" (drug)  ->  1 candidate,  out of 171,231 drug rows
```

with `Insulin` (SNOMED 67866001) in the table under exactly that term. The
filters are now inside the SQL, so the LIMIT applies to matching rows: the same
query returns 6, and `epidural analgesia` returns epidural procedures rather
than bare `Analgesia`.

**Measured honestly: no change to recall on the benchmark** — 49/86 local
recall@8 before and after, and 3/10 on the fixable-failure set both ways. Those
failures are clinical *synonymy* (`kidney function`/`Renal function`), which
starvation was never the cause of. The fix is justified as correctness —
returning 1 of 171,231 matching rows is wrong however it scores — not as a
retrieval improvement. Recorded that way.

## R29d · Head-noun backoff caught injecting noise

`"premixed insulin regimen"` backed off to `"regimen"` and retrieved
*Transfeminine Regimens*. A head that names the FORM of a treatment rather than
the treatment discriminates nothing; `regimen`, `dose`, `protocol`, `schedule`,
`plan`, `strategy` and friends joined `_GENERIC_HEADS`.

## R29e · Empty stages printed nothing

With 0 rules encoded, stage 4 printed a header and no body and stage 5 vanished
entirely, which reads as a crash. Both now say why they are empty — and say
that a fully-refused document is an outcome rather than a failure.

# R30 · Relative change (`Delta`): the titration category becomes encodable

PMC13074134 scored 0/5 because three of five statements were dose adjustments:

```
"Reduce basal insulin by 50% at the start of the liver reduction diet"
"Reduce basal insulin dose by 35%-50% of the pre-LRD dose"
```

`Cmp(var, op, value)` compares a patient to a fixed number. These compare a
patient to their **own earlier state**, and the language had no way to name
"before". Every such statement was correctly but unavoidably `beyond_dsl`.

`Delta(var, baseline, direction, lo_pct, hi_pct)` closes that. `baseline` is an
EventDef -- the point in time measured from -- and lowers to a named variable
`<var>_at_<baseline>`.

| | |
|---|---|
| z3 | `dose <= baseline * 0.5`, plus a lower bound when a range is stated |
| CQL | the comparison, with the baseline emitted as a **named null** for a site to bind |
| verdicts | 40 → 30 = MUST-violate; 40 → 20 = CERTIFIED-compliant; baseline unknown = MAY-violate |
| fragment | LIA -- a ratio against a free baseline is linear arithmetic, not temporal logic |

Both CQL variants compile through the real HL7 translator.

Four decisions worth recording:

- **A zero baseline is excluded by a domain axiom.** With `baseline = 0` every
  value is half of it and the rule can never fail -- the vacuity class arriving
  through the back door. A prior dose is a real measurement: strictly positive.
- **The baseline is reported as an open hole.** It is free unless supplied, and
  naming it is the point: the statement is now EXPRESSED and one datum is
  missing, rather than the sentence being refused whole. Track B exposes it as
  a pinnable variable, so supplying the prior dose yields a real verdict.
- **CQL gets a named null, not a guess.** No portable FHIR retrieve expresses
  "the dose before the diet started" -- that is an institution-specific query
  over prior MedicationRequests. Arden Syntax made the same call with its curly
  braces; this is the same discipline as an unbound predicate.
- **A `Delta` counts as the action atom.** Requiring `role == "action"` on the
  dose variable would reject every titration rule, because a dose is also an
  observation of the patient.

Percentages are validated as fractions in (0, 1): `lo_pct=50` asks for a 5000%
reduction and would otherwise typecheck.

# R31 · Two ways a whole recommendation was being deleted

## R31a · A label absorbed a recommendation

`is_label_only` exists to absorb GRADE labels ("Strong recommendation, moderate
certainty evidence") into the recommendation they grade. It also swallowed

> *"and a weak recommendation against finerenone in adults with CKD."*

which is a **recommendation**, not a label -- and it vanished, with
`on_finerenone` bound, in the registry, and unused. That was the dropped clause
found by hand in R28a.

A label grades a recommendation; it does not contain one. Stripping the grading
vocabulary and requiring that no clinical content survives fixes it, and the
bare labels are still absorbed.

## R31b · Numbered recommendation headings were vetoed

ASH/ISTH prints `Recommendation 1` on its own line above the statement. The
label exemption required punctuation after the number, so
`\\brecommendation\\s+\\d` matched and the veto fired on **the actual numbered
recommendations of an entire guideline**. PMC13276589 went from 88 statements
to 37 before this was caught, and would have shipped that way.

Also: `_DIRECTIVE` recognised "we suggest" but not a bare "suggests", so *"the
ASH/ISTH guideline panel suggests"* -- a third-person panel voice -- read as
prose. PMC13276589 recovered 37 -> **61**.

**The lesson is about where the risk sits.** Both bugs were introduced by
filters added to improve precision, and both deleted true positives silently.
A veto destroys; an extractor carries forward. When they compete for the same
text, the extractor must win, and every veto needs a false-positive check
against a labelled set before it ships.

## R31c · Clause splitting

`split_clauses` divides a sentence at seams where the guideline restarts its
recommendation grammar (`"; and a weak recommendation ..."`), and deliberately
NOT at a semicolon inside a population (`"... without established CVD or CKD;
or established CVD ..."`), which is one rule and would be destroyed by
splitting.

# R32 · Semantic retrieval: matching on meaning

Ten of 22 binding failures were pairs with no shared words -- `kidney
function`/`Renal function`, `epidural analgesia`/`Epidural anaesthesia`. No
query rewriting reaches those, and the token-overlap filter that protects
against retrieval noise is precisely what makes them unreachable.

`semantic.py` embeds every term into a vector of its meaning and returns the
nearest. Separate module so `torch` never enters `bind.py`'s import path -- the
same reason Docling is lazy-imported in `segment.py`.

**Measured: retrieval recall on the fixable failures 3/10 -> 5/10.** The two
new hits are `kidney function -> Renal function` (0.954) and `epidural
analgesia -> Epidural anaesthesia` (0.857) -- exactly the synonymy class.

Four design decisions, each forced by a measurement:

- **Fallback, never replacement.** Lexical and semantic catch *different*
  failures: lexical alone catches `APLA` (an abbreviation the glossary
  expands), semantic alone catches the synonym pairs. The union is the result.
- **The trigger is "no EXACT term match", not "no results".** Lexical search
  rarely returns nothing; it returns the wrong thing. And the exactness test
  runs against the MENTION, not the expanded queries -- the head-noun backoff
  adds "analgesia", the generic concept `Analgesia` matched *that*, and the
  semantic search was suppressed on the very case it was built for.
- **Reserved slots, not merged scores.** A cosine similarity (0..1) and a
  lexical score (0..12) are not comparable. Sorting them together buried
  `Renal function` (0.954) below eight wrong lexical matches. Faking a score to
  interleave them would invent a comparison that does not exist.
- **`SemanticUnavailable` is raised, never returned as `[]`.** An unbuilt index
  and an absent concept must not look the same -- R27's failure class.

Model choice is measured rather than assumed. A general model (all-MiniLM-L6-v2)
handles morphology well and domain synonymy badly; a biomedical one
(PubMedBERT) is materially better on the domain pairs and 8x slower to index:

| pair | MiniLM | PubMedBERT |
|---|---|---|
| kidney function / Renal function | 0.954 | 0.934 |
| BP lowering / Antihypertensive therapy | 0.360 | **0.527** |
| CGM readings / Glucose measurement | 0.320 | **0.517** |
| weight regain / Weight gain | 0.629 | **0.768** |
| *unrelated baseline* | 0.111 | 0.086 |

Index build: 2.3 min (MiniLM) or 18 min (PubMedBERT) for 365k terms on MPS,
cached to disk, ~1 GB. Gitignored -- derived data, rebuildable from `terms.db`.
The five remaining misses are the domain pairs above, which the biomedical
model scores high enough to reach; that rebuild is the next measurement.

Test coverage: 273 (22 added across R30-R32).

# Appendix A · Semantic terminology retrieval: justification and ablation

## A.1 Why lexical retrieval cannot close this gap

Binding a guideline phrase to a terminology code is a retrieval problem with an
adversarial property: **the two vocabularies were written by different
communities for different purposes, and they do not share words.** A guideline
is written to be read by clinicians; SNOMED is written to be unambiguous under
composition. The result is systematic, not incidental:

| the guideline writes | the terminology calls it | shared tokens |
|---|---|---|
| kidney function | Renal function | 0 |
| epidural analgesia | Epidural anaesthesia | 1 of 2 |
| volume optimisation | Fluid management | 0 |
| BP lowering | Antihypertensive therapy | 0 |
| CGM readings | Glucose measurement | 0 |

An inverted index over terms cannot retrieve a document that shares no term
with the query -- this is a property of the index, not a tuning problem. Query
expansion does not help either: the expansions available (abbreviation
glossary, head-noun backoff, slash-splitting) are all *syntactic*, and none
turns "kidney" into "renal".

Worse, the defence against retrieval noise makes it strictly harder. R5
established that free-text terminology endpoints return arbitrary concepts on a
miss (`"central venous access device"` -> *Spine board*), so `search_all`
discards candidates sharing no stem with the query. That filter is correct and
it is exactly what makes a zero-overlap synonym unreachable even on the
occasions it is retrieved.

**Measured cost**: 10 of 22 binding failures on the benchmark (R23), each one a
refused statement, each one attributed to the guideline in the residue report
when it is in fact ours.

The standard remedy in clinical NLP is concept normalisation through UMLS
(MetaMap, cTAKES, QuickUMLS): map both sides to a CUI and compare identifiers.
It is the right tool and it needs the UMLS Metathesaurus release. Dense
retrieval reaches the same failures without that dependency, runs locally, and
is what is evaluated below.

## A.2 Method

Each terminology term is encoded into a unit-norm vector by a transformer, so
cosine similarity is a dot product against a preloaded matrix. The query is
encoded the same way; the nearest terms are returned. Terms are indexed per
`kind` (lab, condition, procedure, drug, drug_class) and cached to disk.

Three properties are load-bearing and were each fixed after a measurement:

1. **Fallback, not replacement.** Consulted only when no candidate's term is an
   exact match for the mention. Lexical and semantic catch disjoint failures --
   lexical alone catches `APLA` (an abbreviation the document glossary
   expands), semantic alone catches the zero-overlap synonyms.
2. **Exactness is tested against the MENTION, not the expanded queries.** The
   head-noun backoff issues "analgesia" for "epidural analgesia"; the generic
   concept `Analgesia` matched that query exactly, which read as success and
   suppressed the semantic search on the very case it was built for.
3. **Append, never evict.** A cosine similarity (0..1) and a lexical score
   (0..12) are not comparable, so they are not merged into one ranking. The
   first implementation reserved slots by truncating the lexical list, which
   dropped a correct candidate at position 7 -- `anticoagulant prophylaxis`
   regressed from hit to miss. Caught by the ablation below, which is the
   argument for running one.

## A.3 Ablation

**Task.** Given a phrase as a guideline writes it, does the correct terminology
code appear in the top-8 candidates? Recall@k is the appropriate metric:
retrieval's job is to OFFER the concept, and a concept never offered can never
be selected.

**Data.** All 85 phrases from the binding benchmark with a known target -- the
code the binder actually selected (73) plus the hand-adjudicated expected code
for the failures (12). The successes are included deliberately: an ablation
that measures only the cases a change was built for cannot show what it broke.

**Reproduce.** `python3 bindbench.py --ablate`

| condition | recall@8 | latency |
|---|---|---|
| lexical only | 63/85 = **74.1%** | 31 ms/lookup |
| lexical + semantic | 68/85 = **80.0%** | 226 ms/lookup |

**5 gained, 0 regressed**: `kidney function`, `epidural analgesia`,
`preoperative MRI`, `painful eye`, `renal cysts`.

The fallback fires on 59% of lookups and costs roughly 400 ms when it does;
lookups with an exact lexical match skip it entirely. Latency is dominated by
one 365k x 384 matrix product per query and is not optimised -- an approximate
index (FAISS, HNSW) would remove it, and is unnecessary at this scale.

## A.4 Model choice

Measured on the pairs the method exists to solve, higher is better except the
last row:

| pair | all-MiniLM-L6-v2 | PubMedBERT |
|---|---|---|
| kidney function / Renal function | 0.954 | 0.934 |
| weight regain / Weight gain | 0.629 | **0.768** |
| BP lowering / Antihypertensive therapy | 0.360 | **0.527** |
| CGM readings / Glucose measurement | 0.320 | **0.517** |
| *kidney function / acute pancreatitis* | 0.111 | **0.086** |

The general model handles **morphology** (kidney/renal, regain/gain); the
biomedical model additionally handles **domain synonymy**, and separates
unrelated pairs slightly better. It costs 8x more to index (18 min vs 2.3 min
for 365k terms on an M-series GPU). The ablation above uses MiniLM; the five
remaining misses are precisely the domain pairs PubMedBERT scores highest on,
so that rebuild is the obvious next measurement rather than a claim.

## A.5 Threats to validity

- **The targets for the 12 failures were adjudicated by hand, by us.** Two of
  the codes in the first pass were wrong (`182832007` is *Drug administration*,
  not antihypertensive therapy) and were caught only by verifying each against
  the index. The remaining ten are verified present but their *clinical*
  correctness is one annotator's judgement.
- **85 phrases from 9 guidelines** is a small sample, and the failure modes are
  specialty-dependent (R29).
- **Recall@8 is not binding accuracy.** It measures whether the concept was
  offered; whether the selector then picks it is a separate question, measured
  end to end by `bindbench --build`.
- **The index is built from our loaded releases**, so a concept absent from
  them is unreachable by either method -- the `vocab_gap` bucket, which this
  experiment does not address.

# R33 · Codebase sweep for the recurring failure classes

A file-by-file pass for the shapes this project keeps rediscovering: a failure
that returns a usable value, an advisory result read as fatal, a filter applied
after a limit, and type confusion around `bool`. Fourteen defects; the four
worst are below.

## R33a · A failed compile read as a clean one

`toolchain.cql_to_elm` parsed the translator's stderr for CQL errors and
returned the list. **An empty list means "compiles"** -- and it was reachable
by a JVM that never ran: `ClassNotFoundException`, an OOM, any stderr the CQL
error grammar does not match produces zero parsed errors and a printed
`cql-to-elm: COMPILES`. Exit 0 with no ELM written did the same.

The compile-pass claim in this document rests on that line. Now: a non-zero
exit with no parseable error raises, and so does success with no ELM on disk.

## R33b · `bool("false")` pinned a variable TRUE

`api._coerce` did `bool(value)` for boolean variables. Every non-empty string
is truthy in Python, so a form field or JSON string of `"false"` pinned the
variable to **True** -- capable of flipping a clinical verdict from compliant
to violating. Strings are now read explicitly, booleans are refused where a
number is expected, and an unreadable value is declined rather than guessed.

## R33c · `choice: true` selected candidate 1

`bind.select` guarded the model's candidate index with
`isinstance(choice, int)`. `isinstance(True, int)` is True and
`-1 <= True < len(cands)` is True, so a model returning `true` silently bound
the **second candidate** -- a wrong code chosen by type confusion, with a
plausible reason string attached.

**Third appearance of this exact trap** (VarDef typechecking, a probe mock,
here), which is why `dsl._is_number` exists. The lesson has to be a codebase
rule rather than a fix: in Python, `bool` is never a number.

## R33d · An empty value set reported as a lowering bug

VSAC answered successfully with a **zero-code expansion** for a live OID.
`expand_valuesets` wrote the empty set, every action looked absent, and
`crosscheck` reported `NO <-- LOWERING BUG` -- sending a reader to debug the
two lowerings when the fault was a terminology fetch. The crosscheck's own
value is that it needs no ground truth; misattributing its disagreements
destroys that.

Empty expansions now warn loudly, and the crosscheck separates **INCONCLUSIVE**
from a genuine disagreement: `2/3 agree, 1 inconclusive` rather than a false
accusation. Note this also qualifies the earlier 6/6 result -- that figure
depends on live VSAC availability, which is a reproducibility threat worth
stating.

## R33e · The rest

| defect | consequence |
|---|---|
| `semantic_index()` init unlocked | a race where the second thread reads `tried=True, index=None` and silently skips semantic retrieval for the whole run |
| `SemanticIndex.embed` hardcoded width 384 | correct for MiniLM, wrong for PubMedBERT (768); an empty batch produced a matrix that cannot multiply against the index |
| `_ensure_model` unlocked | two threads loading a 400 MB model, and a half-assigned `self._model` visible to the other |
| slot reservation in `search_all` | evicted a correct lexical candidate at position 7 -- `anticoagulant prophylaxis` regressed, caught by the A.3 ablation |
| `Usage.add` unlocked | `+=` is load-add-store; the binder and the K-sample formalizer both run in pools, so the **cost figure this paper quotes** was under-reported |
| ~30 `open()` calls with no `encoding=` | `gold.csv` and SNOMED terms carry non-ASCII; correct on macOS, broken on any non-UTF-8 locale -- "works on my machine" fragility |
| `next(iter(out.values()))` in demo stage 5 | an engine finding no patient produced an opaque `StopIteration` traceback rather than a message |

## R33f · Checked and NOT defects

Recorded so the next sweep does not re-litigate them: the two `len(a & b) /
len(a | b)` divisions are both guarded by non-empty checks; there are no bare
`except: pass` swallows left in the data path; no mutable default arguments;
`SqliteIndex` deliberately holds one long-lived connection with an `RLock`
(documented, and load-bearing under the thread pool).

## R33g · What the sweep says about the project

Nine of the fourteen are the **same shape**: something failed and returned a
value the caller could not distinguish from success. A JVM crash returning
`[]`. VSAC returning an empty expansion. A race returning `None`. `bool("false")`
returning `True`.

Every one of them was found by asking one question of each call site: *if this
fails, what does the caller see?* That question, not a linter, is what this
codebase needs applied routinely -- none of these produced a traceback, and the
test suite passed with all fourteen present.

## R34 · The titration arc: six runs, one defect, five symptoms

`Delta` was added to the DSL as one dataclass. It has **fifteen consumers** --
the decoding schema, the validator, the Z3 lowering, the CQL lowering, the
variable walker, the event walker, the hole stubber, the canonicaliser, the
quantity extractor, the serialiser, and Track B's loader and var-reference
check. Python raises nothing when an `isinstance` chain omits a new variant:
the walker falls through and returns as if the node were absent.

So every consumer that had not been updated surfaced **one live run later, as a
different symptom**, and each looked like a fresh unrelated bug. This section
records the arc because the arc, not any individual fix, is the finding.

| # | Run symptom | Root cause | Fix |
|---|---|---|---|
| 1 | `beyond_dsl`: "no delta construct exists" | no `EventDef` was ever created, so the decoder withheld the node | `gate.datable_events()` declares events from bound action/drug/procedure concepts |
| 2 | still `beyond_dsl`, same words | the `delta` leaf also requires a NUMERIC variable, and a medication binds as a boolean (*is it prescribed*), so there was no quantity to change | `gate.dose_companions()` declares `<drug>_dose` (real) when the sentence carries a relative-change cue |
| 3 | still `beyond_dsl`; model listed only booleans | **the run used stale code** -- `gate.py` was edited mid-run, and Python imports once at process start | process discipline; and `GRAMMAR_OFFERED` evidence now records what the decoder could actually emit, so this is never inferred again |
| 4 | `TYPE_ERROR: on_basal_insulin_dose is real but value is bool`, and the boolean missing from the shipped registry | `canonical_aliases` merges variables sharing a terminology code. `on_basal_insulin` (bool) and `on_basal_insulin_dose` (real) carry the same ATC code, so the boolean was collapsed into the real and every comparison written against it was retyped | merge only within a type; a rewrite that changes a variable's type is never safe |
| 4 | `UNKNOWN_EVENT`, bundle shipped `"events": []` | `demo.py` had its own event walker that handled `Since` and not `Delta` | one walker, `dsl.events_in`; the local copy deleted |
| 4 | `Identifier "..._at_..." is already in use` -- the library did not translate at all | a `Delta` baseline is a DERIVED name that `rule_to_cql` already emits as a named null; `demo.py` was also stubbing it into the registry, as `bool` | `dsl.hole_defs()` skips derived names; a derived name is the lowering's to declare |
| 5 | stage 4 crash: `expected a number, got the boolean True` | `_patients_for` guessed a variable's type by substring-matching its name (`"acr" not in n and "egfr" not in n`) and pinned everything else to `True` | patients are now SOLVED for -- a model of `domain and phi` and one of `domain and not phi`; z3 already knows every sort |
| 5 | 12 `CIRCULAR_BASELINE` warnings across one document | the only events on offer were the drug bindings themselves, so "reduce basal insulin by 50% **at start LRD**" could only anchor to *being on basal insulin* -- circular, satisfiable by any dose, and it typechecked | `EventDef.unbound` + `gate.anchor_events()`: the anchor the sentence names becomes a declared, uncoded event. `CIRCULAR_BASELINE` raised from warn to **error** |
| 5 | 0010 was circular and printed no caution | the PARTIAL branch computed the validator issues and kept only `blocked_codes`, discarding every warning | partial path records validator evidence (**third** time this branch has dropped evidence the strict one keeps -- see R28a) |

### R34a · Three more of the same bug, found without a run

After the fifth symptom, the codebase was scanned for every `isinstance` chain
over the expression union. **15 walkers; 8 did not name every variant.** Five
were deliberate. Three were live bugs that had not yet fired:

| Site | Silent on | Consequence |
|---|---|---|
| `dsl.rule_quantities` | `Delta` | a titration's percentage invisible to the dropped-quantity check |
| `patient_checker/real_rules._vars_and_events` | `Delta` | a delta rule's variable and baseline event invisible to Track B at load |
| `patient_checker/guidelines._events_in` | `Delta` | a **third copy** of the same walker, with the same `Since`-only bug |

### R34b · The measurement bug this exposed

`gate._attempt` caught **every** exception and returned
`Refusal("beyond_dsl", "encoding attempt failed")`. An auth error, a rate
limit or a malformed JSON body was therefore indistinguishable from the DSL
genuinely lacking a construct, and the formalizability residue -- a headline
number -- had been silently absorbing our own outages.

Infrastructure failures are now labelled `ENCODER FAILURE (ours, not the
guideline's)`. This is R33g's shape again: a failure that returns a usable
value.

### R34c · What now catches this class

Two harnesses, because unit tests over hand-built objects structurally cannot
find "a walker nobody told about `Delta`" -- they test what their author
already understood.

- **`test_exhaustive.py`** reads the source, finds every dispatch over the
  expression union, and fails if one does not name every variant. Deliberate
  omissions require a written reason. A NEW walker is covered the moment it is
  written, and adding a node to the DSL breaks the suite until every consumer
  is updated. Verified by re-introducing the `real_rules.py` bug: it names the
  file, line, function and missing node.
- **`cassette.py` + `test_replay.py`** record one real run's model responses
  and statement list, then replay the full pipeline offline. Every subsequent
  change is checked against a real guideline before anyone runs anything. K
  samples are stored as a list per prompt and replayed in order -- collapsing
  them would delete the sampling-variance signal the confidence score is built
  on. A stale tape **skips**, never passes.

**The DSL is now frozen.** `Delta` was the last node. Anything inexpressible
becomes a refusal with a named reason, which is the contribution anyway.

## R35 · Class-level terms: the binder was never shown the answer

Three statements in PMC13074134 refused as `unbound_concept` on
"Basal insulin regimen(s)" and "Premixed insulin regimen", while a nearly
identical sentence bound cleanly. The cassette settles it -- the selector's
reasoning was identical every time:

> *"No candidate represents that class... the specific agents are only members...
> Under-determined, so abstaining."*

It bound **only** when the mention proposer happened to emit
`canonical: "long-acting insulin"`, which lexically matches ATC
`Insulin, Long-Acting`. The non-determinism was one free-text field.

| Defect | Evidence | Fix |
|---|---|---|
| A category word decided the candidate list | `regimens` occurs in **6** concepts, `insulin` in **1,638**; BM25 rewards the rare token, so "Basal insulin regimen" returned five *Transfeminine Regimens* value sets and nothing else | the zero-overlap filter now requires overlap on a **content** token; `_GENERIC_HEADS` was previously consulted only for head-noun backoff |
| Reserved semantic slots spent on duplicates | `share = 2`, and the top two semantic hits were already in the lexical list, so `Insulin, Long-Acting` at rank 3 was cut | dedupe against the lexical list **before** truncating to the share |
| Fallback-kind semantic hits unpenalised | SNOMED `Insulin` (a *drug*, 0.78) outranked ATC `Insulin, Long-Acting` (a drug *class*, 0.61) | `FALLBACK_KIND_PENALTY` now applied on both retrieval paths, not just the lexical one |

**Measured honestly: the benchmark does not move** -- 4/10 on the fixable-failure
set before and after, A/B'd for each fix independently and together. The
already-binding regression check is 58/75, unchanged.

The reason is the finding: the 97 mentions come from the 45 gold statements,
and **none of them is an umbrella or class-level term**. The binding benchmark
cannot see this failure mode at all -- which is precisely where a guideline's
"Class of medication:" sections live. Justified as correctness (returning five
gender-affirming value sets for an insulin query is wrong however it scores),
pinned as a test rather than added to the gold set, so the benchmark number
stays honest.

## R36 · The outputs were not readable, and one was discarded

The pipeline emitted a JSON bundle (an interface artifact for Track B) and
terminal scrollback. Neither can be handed to a clinician.

| Gap | Fix |
|---|---|
| **The CQL was never saved** -- generated, printed as a one-line status, discarded | written to `patient_checker/cql/<bundle>/<statement_id>.cql` |
| Cautions never reached the bundle: only *refusals* carried evidence, so an encoded rule with a `CIRCULAR_BASELINE` looked clean in the artifact | optional `findings` key -- decision, holes and evidence per statement; additive, so an older consumer ignores it |
| No human-readable output at all | `report.py`: a self-contained HTML page, source sentence beside the rule it became |
| A rule was rendered as JSON or CQL, which nobody adjudicates | `dsl.to_english` / `rule_to_english` |
| **A "weak recommendation against finerenone" rendered as `THEN must: NOT finerenone`** | `dsl.deontic_force` reads GRADE strength as well as the deontic operator, and the report prints the guideline's own wording verbatim beside ours |

The report earns its keep immediately. In `PMC12355350:0048` the source ends
*"...and a weak recommendation against finerenone in adults with CKD"* and the
rule beside it is only about SGLT-2i/GLP-1. The dropped clause is visible at a
glance because the sentence sits next to the encoding; in the terminal it was a
`UNUSED_BOUND_CONCEPT` line that scrolled past.

## R37 · A wrong docstring propagated into every artifact derived from it

The header of `gate.py` carried a table mapping evidence to refusal codes. It
was drawn from an early design and never revised, and it was wrong in two ways
that read as guarantees the pipeline does not make:

| Claimed | Actual |
|---|---|
| `not_patient_level` ← "obligation names no action" *(deterministic)* | ← `is_patient_level`, the SEGMENTER'S CLASSIFIER, which is an LLM call on a live run. `NO_ACTION_ATOM` is a validator error that causes rejection and never sets this code. |
| `unsupported_temporal` ← "multi-visit predicate" *(deterministic)* | **No code path assigns it.** It exists only in the refusal schema enum, so the model is the only thing that can emit it -- which it did, twice across the real bundles. |
| `defeasible` *(model)* | Deterministic when `MODAL_PREDICATE` is among the blocking codes; the model may also name it. |
| `beyond_dsl` *(deterministic)* | Mixed: `VACUOUS_RULE` deterministically, otherwise the model's own code or the fallback. |

Also corrected: "most of the decision is already made by machinery stages 1-2
produce". Of the six codes, **one** is deterministic evidence from stages 1-2
(`unbound_concept`) and one more from this stage's validator (`defeasible`).
The deterministic machinery is a gate ON the model's answer, not a replacement
for it.

**Why this is worth a section.** The error was found by checking an
architecture diagram against the code -- the diagram was faithful to the
docstring, so it inherited every mistake. Prose in a codebase is an artifact
other artifacts are derived from, and nothing tests it. `test_gate.py`
now pins which codes are reachable with `formalize=None`, including an
assertion that fails if a deterministic `unsupported_temporal` path is ever
added without the table being updated.

This is R33g's shape once more, in a different medium: something was wrong and
returned a usable-looking value -- a table that reads as authoritative.

## R38 · One bad key, 789 transport failures, and a residue report nobody could read

Found by looking at a checker page for the JBDS-IP guideline
(`trial2-db48ab`, PMC13074134) and reading the banner it carried:

> `TERMINOLOGY_UNAVAILABLE` 26 terminology lookup(s) failed (transport or
> server error, not an absent concept) … 'Biguanide' (drug_class): HTTPError:
> HTTP Error 401: ; 'Thiazolidinediones' (drug_class): HTTPError: HTTP Error
> 401: ; …

Counted across the stored bundle: **789 recorded 401s, 157 of 160 findings
carrying the warning, and 48 of the document's 55 `unbound_concept` refusals
qualified by it.** The residue that document reports is largely not the
document's.

Two distinct faults were being reported through one channel.

**HTTP 401 — a rejected credential, reported once per phrase.** VSAC is the
only authenticated source in the binder (HTTP Basic, `apikey:$UMLS_API_KEY`);
SNOMED, LOINC and RxNav are anonymous. So a 401 can only mean our key was
refused, which is a fact about the run, not about the phrase — and it will be
the answer to every subsequent lookup too. Confirmed by construction: a junk
key reproduces `HTTP Error 401` exactly, and the key in `.env` succeeds on
30/30 live lookups including the three named in the banner. `.env` had not
been touched since 28 Aug; the bundle was built on 4 Sep. The mechanism was
`load_env`'s `os.environ.setdefault` — **an exported `UMLS_API_KEY` silently
won over the file on disk**, so a stale export from a forgotten shell
credentialled the whole run.

**HTTP 400 — a request we should never have made.** PMC13270348's 88 failures
were all 400, over exactly four phrases: `AP`, `ap`, `CT`, `ct`. VSAC's
`title:contains` rejects a search string under three characters. The mention
proposer legitimately emits two-letter abbreviations, so every one was issued,
rejected, and recorded as a *transport* fault qualifying that statement's
refusal. Reproduced today: `'ap'` → 400, `'abc'` → 200.

**Fixes.**

1. `VSAC_MIN_QUERY = 3`. A request the API defines as invalid is ours not to
   make, not a blip to report.
2. A 401 (or 403 — a lapsed licence) sets `_auth_rejected`, which makes
   `available` false. Every caller already guards on `available`, so one 401
   stops the whole run asking. It is recorded **once**, on a new run-level
   channel, not once per phrase.
3. `load_env` now overrides. A `.env` is the value the operator most recently
   chose; an export is usually left over from a shell they have forgotten
   about, and it fails silently — a wrong key looks exactly like a right one.
   Containers are unaffected: they ship no `.env`, so platform secrets stand.
   An empty assignment in the file is skipped rather than blanking a real
   variable.

**The subtler bug inside the fix.** Making the 401 stop the run breaks the
gate's warning, which is counted by *delta* on `len(index.failures)`: with no
further requests the delta is zero from the second statement on, and every
later refusal would go out unqualified — the exact failure `TransportFailures`
was built to prevent, in a different dress. Hence two channels with different
lifetimes: a per-lookup failure is a blip on one binding attempt, a **degraded
source** is true for the whole document and does not decay. `degraded_of(index)`
mirrors `getattr(index, "failures", ())` so a caller need not know which index
class it was handed, and the gate attaches the run-level notice only where
there is a refusal to qualify — a statement that bound cleanly needed nothing
from the degraded source.

Pinned by `test_bind.py` (the guard, the once-not-per-phrase 401, a genuine
timeout still recorded per phrase, propagation through `RemoteIndex` and
`CachedIndex`, and `.env` precedence) and `test_gate.py`
(`test_a_rejected_credential_qualifies_every_refusal_not_just_the_first`).

**Same shape as R33a and R33d.** An infrastructure failure returned a value
indistinguishable from a result — here twice over: a bad credential
indistinguishable from a vocabulary gap, and our own malformed request
indistinguishable from a server fault. The bundles built under the bad key
should be regenerated before any residue count from them is quoted.
