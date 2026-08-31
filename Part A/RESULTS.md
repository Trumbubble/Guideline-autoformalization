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
