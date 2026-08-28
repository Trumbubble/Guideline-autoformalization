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
python3 test_dsl.py && python3 test_bind.py && python3 test_segment.py   # 86 tests, offline
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
