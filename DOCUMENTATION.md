# GuidelineForge: Documentation

Two halves of one system. **CQL_converter** turns a clinical guideline
document into formally checkable rules — refusing rather than guessing
wherever it can't. **patient_checker** takes a rule, a patient and a partial
care plan and decides whether the plan complies — and when it might not,
produces a minimal, concrete witness instead of a vague "maybe."

---

## 1. Overview

Document → **Segment** → **Bind** → **Gate** (which synthesizes a DSL
`Rule`/`Refusal`) → **CQL**, then the checker.

**The pipeline** ([`CQL_converter/`](CQL_converter/)):

Document → **Segment** → **Bind** → **Gate** → **CQL**.

- [`segment.py`](CQL_converter/segment.py) — a document in, verbatim recommendation
  statements out, each with a provenance span.
- [`bind.py`](CQL_converter/bind.py) — a statement's clinical phrases in, a closed
  `dsl.Registry` of retrieved (never generated) codes out.
- [`dsl.py`](CQL_converter/dsl.py) — the typed intermediate representation every other
  stage reads or writes: `Registry`, `Rule`, `Refusal`, the Z3 lowering. Not
  a separate sequential stage — Bind produces a `dsl.Registry` and Gate's
  `triage()` produces a `dsl.Rule`/`Refusal` directly, which CQL then
  lowers; it's the shared substrate everything else is built from, not a
  fifth stage in a line.
- [`gate.py`](CQL_converter/gate.py) — per statement, decide encode / partial / refuse,
  aggregating deterministic evidence before ever asking a model.
- [`cql.py`](CQL_converter/cql.py) — a validated `dsl.Rule` in, an HL7 CQL library out, by
  a deterministic, re-runnable lowering.


**The checker** ([`patient_checker/`](patient_checker/)):


- [`real_rules.py`](patient_checker/real_rules.py) — ten hand-authored guideline rules
  through the real DSL, so there's always real material to check against.
- [`guidelines.py`](patient_checker/guidelines.py) — runs CQL_converter's actual pipeline
  (segment → `gate.triage`, which binds and formalizes per statement →
  `dsl.write_bundle`, then the real `cql-to-elm` translator) over an
  uploaded document and stores the result as another checkable guideline.
- [`api.py`](patient_checker/api.py) — the Flask app: lists rules, runs checks, serves the
  UI pages.
- [`ui.html`](patient_checker/ui.html) / [`demo.html`](patient_checker/demo.html) / [`dashboard.html`](patient_checker/dashboard.html)
  / [`demo-toy.html`](patient_checker/demo-toy.html) — the click-through checker, a no-setup
  live demo, the guideline library/upload page, and the toy page at
  `/demo/toy` (one hand-authored rule, three patients, three verdicts), kept
  because it isolates the three-valued verdict better than a real document
  can.

## 2. How It Works

### 2.1 Segmenter

Finding sentences that look like recommendations is easy; the job is
producing statements a clinician can point back at the source document.
Every `Statement` carries a `Provenance` (locator + character offsets) that
re-finds and slices out its own source text, verified on 1,086 blocks of
real publisher markup with zero locator failures.

Flow: loader (JATS / HTML / PDF) → `Block[]` → sentence splitting with an
offset map preserved through normalization → prefilter (cheap regex
scoring, threshold 1.5, plus three deterministic vetoes for list stems,
evidence summaries, and cross-references) → classify (`is_recommendation`,
`is_patient_level` — heuristic or an injected LLM, quote checked verbatim
against source) → assembly (label absorption, cross-references, dedupe).

**Loaders, and why JATS comes first.** PMC Open Access ships XML (JATS)
where the publisher already tagged `<boxed-text>` and `<table-wrap>` at
typesetting time, so the boxed-callout problem is solved *structurally* for
most of the corpus — `load_jats` (via `fetch_pmc(pmcid)`, which covers
essentially all of 3,129 open-access practice guidelines on Europe PMC) is
the main road, `load_pdf` the fallback. But XML *existing* is not the same
as XML being *complete*: one measured guideline (ESO stroke blood-pressure)
renders **26 of 26 tables as embedded JPEG images with zero extractable
text**, so the XML route reaches only the narrative restatements, never the
formal boxed ones — `_table_rows` emits a `figure` block carrying the
caption instead, landing the gap in `manifest["figures_uncovered"]` rather
than letting it vanish. The real, independently-typeset PDF for
that same document *does* carry the missing tables as selectable text — XML
and PDF can diverge in completeness — but `load_pdf`'s box-recovery path has
only ever run against synthetic fixtures, never against a real guideline PDF.

For table rows specifically: the condition and the action sit in different
columns, so no contiguous span of the document *is* the rule. Rows are
rendered header-folded ("Finding: eGFR below 30; Action: refer to
nephrology"), provenance points at the `<tr>`, and every cell *value* is
verbatim even though the assembled string is not — a documented exception,
not a bug.

### 2.2 Binder

It builds the closed
vocabulary the compiler is then constrained to, so whatever it cannot bind
becomes a `Refusal(unbound_concept)` instead. The
model is shown a numbered candidate list and returns an index (or ABSTAIN),
never a code string, so a hallucinated code cannot be expressed at all.

Flow: propose mentions → search (phrase ∪ abbreviation expansion, scoped by
kind) → rerank by unit (deterministic, before any model) → select (index or
ABSTAIN) → validate (chosen code must be in the index) → `Binding` or
`Refusal`.

**Sources** SNOMED via `tx.fhir.org`
(`$expand?filter=`, `?fhir_vs=isa/<code>`, `$validate-code`), LOINC via NLM
Clinical Tables, drugs and ATC classes via RxNav. A UMLS licence adds
**VSAC**'s curated, steward-versioned value sets — better than a computed
is-a expansion — routed ahead of SNOMED/ATC for the set-valued kinds
(`condition`, `drug_class`, `procedure`) and unioned with them; nothing is
blocked without it.

**Why a real vocabulary matters** "eGFR" searched
against live LOINC returned *only* an EGFR-gene genetics test — epidermal
growth factor receptor, not kidney function — and being the sole candidate,
a naive selector called it a "clear lexical win"; being the only candidate
is not evidence, so the selector now requires an exact term match, a unit
match, or full query-token coverage before binding, turning that case into
a safe refusal instead of a wrong answer. Two more deterministic fixes
followed the same shape: `NOT_AN_OBSERVATION` demotes grouper/value-set
codes that win on token overlap against a real result code, and units run
*before* any model — LOINC 9318-7 (mg/g) and 14585-4 (mg/mmol) are the same
threshold ~8.84 apart, so `rerank_by_unit` settles it with no judgement
required.

**Lexical retrieval has a hard ceiling, and semantic retrieval exists to close it.** 
Guideline prose and terminology vocabularies come from different
communities — "kidney function"/"Renal function" share zero or one token
with each other, and an inverted index cannot retrieve a document sharing
no term with the query, a property of the index, not a tuning problem.
`semantic.py` (a separate module, so `torch` never enters `bind.py`'s import
path) embeds terms into a meaning vector and searches nearest-neighbour, **as
a reserved fallback slot, never merged into the lexical ranking** — a cosine
similarity and a BM25 score aren't comparable, and interleaving them buried
a 0.954-similarity correct answer below eight wrong lexical matches in
testing. Measured on the fixable-failure set: lexical alone found 3/10,
lexical ∪ semantic found 5/10 — but **this layer needs a local index built
and cached to disk first** (`python3 semantic.py --build --fast`); without
it, `bind.semantic_index()` returns `None` and every lexical-only miss
stays a miss, a real gap in this environment, not a hypothetical one.

**Terminology retrieval starves silently if kind/system filters aren't inside the SQL.** 
Fetching the top-N BM25 rows across every kind and then
filtering in Python means a common word like "insulin" can fill every slot
with the wrong kind before the real candidate is seen — measured: 1
candidate returned out of 171,231 real drug rows, with the correct SNOMED
concept sitting in the table the whole time; filtering inside the query
fixed it. Separately, `drug`- and `drug_class`-kind searches are restricted
by design to specific terminology systems (RxNorm/VSAC and ATC/RxNorm/VSAC
respectively) — SNOMED's own drug hierarchy is loaded but deliberately
never searched for those kinds, which is correct architecture but means a
SNOMED-only concept can be unreachable until the "real" system for that
kind (chiefly ATC for drug *classes*) is populated, and there is currently
no bulk loader in this codebase for that system, unlike SNOMED/LOINC/RxNorm.

### 2.3 DSL

A rule is a formula. `Rule.when` / `Rule.then` denote
`phi = when -> then`; a patient violates the rule iff `phi` is false for
them, and a Z3 counterexample is a model of `NOT phi`. `action` / `target`
are a human-readable gloss with no semantics — branching on `rule.action` in
verification code is a sign something has gone wrong.

**The registry variables are a symbolic patient** 
`egfr` is not "some particular person's eGFR", it stands for
*any* patient's eGFR; when Z3 returns `sat`, the model it hands back **is**
a concrete patient (`egfr=29, has_diabetes=True, on_acei_arb=False, ...`).
One solver query is one patient — many patients is a loop, not a bigger
query, because no clinical guideline relates two patients to each other.

**Registry variable types**: `real`/`int`/`bool`/`code`/`date`, each with a
fixed set of legal comparison operators and a specific Z3 lowering (`bool`
→ `z3.Bool`, `code` → `z3.String` equality-only, `date` → ISO-8601 in the
DSL but `z3.Int` ordinals in the solver, `int` → `z3.Int` never `Real`, or
the solver hands back `days = 14.5`). `role` is `observation` (patient
state) or `action` (a care-plan decision); `validate_rule` rejects an
obligation naming no action atom, and patient_checker reads `reg.actions()` to
write frame axioms over actions a plan never mentions. `plausible` is a
hand-curated physiological range asserted at solve time so a counterexample
can't land at eGFR = 2.0, and it doubles as a validator warning on
unit-slip thresholds (the mg/g-vs-mg/mmol factor of ~8.84 above compiles
cleanly and silently without it).

**Expression nodes**: `Cmp(var, op, value)`, `Between(var, lo, hi, lo_inclusive, hi_inclusive)` — inclusivity is explicit and required, because
boundary slips (eGFR 29 vs 30) compile cleanly and are the single most
common silent bug — `Since(event, days, inclusive)`, and `Not`/`And`/`Or`
(junctions need ≥2 operands). `And`/`Or` are first-class rather than derived
because an expression language without disjunction doesn't prevent
disjunctions, it silently converts them to conjunctions — one measured
AND-for-OR substitution flipped the verdict for 69.5% of the plausible
patient population, and it typechecked the whole way.

**Time is flattened to derived state** `Since(event, days)`
lowers to `<event>_occurred AND days_since_<event> <= days`. The occurrence
flag is load-bearing: without it, a bare day-count is satisfiable for a
patient who was *never checked* — the solver just picks a small number —
and "never checked at all" is the commonest real monitoring violation.
Multi-visit predicates ("on two occasions ≥90 days apart" — the actual
definition of CKD) need more than one timepoint, genuinely out of this
fragment, and refuse as `unsupported_temporal`. Moving to time-indexed
variables (`var[t]` over a horizon) is additive — no existing rule would
need to change — but it changes the headline formalizability number, so
that's an explicit measurement decision not yet made, not an oversight.

**`Rule.deontic` is an operator**
```
obligation   O(then | when)    violation = when AND NOT then
permission   P(then | when)    violation = FALSE, unconditionally
```
*"We recommend anticoagulant prophylaxis"* is an obligation — a patient
meeting `when` without `then` is a real deviation — while *"Epidural
analgesia may be considered"* is a permission, where that same patient has
violated nothing. GRADE-conditional wording ("we suggest X") is **not**
treated as permission, since the weakness there is in the evidence, not the
obligation — only explicit permission language counts, or treating every
weak recommendation as optional would silence most of a guideline.
A permission gets its own four-outcome verdict lattice
(`OPTION-taken` / `OPTION-not-taken` / `OPTION-not-applicable` /
`OPTION-available`), none of them an accusation. Measured: dropping this
operator for a wording-only guard flips the verdict on 4 of 5
permissive/obligation pairs — the largest single effect measured across the
whole ablation study.

**Canonical form** A model-generated
registry can bind the same real concept under two different variable names
in a way a person never would — measured on a real guideline, the
formalizer wrote both `pca` and `patient_controlled_intravenous_analgesia`,
both bound to the identical SNOMED code, into one rule, producing
`(has_ap AND pca) -> patient_controlled_intravenous_analgesia`, i.e.
`(A AND B) -> B`. That's valid under every interpretation, so it certifies
every patient and can never fail — it doesn't look like a bug, it looks
like a rule that works. Only two signals are trusted to merge two names:
the same terminology `system|code|version`, or a hole that's a `has_`-
prefixed restatement of an already-bound variable — never a similarity
judgement, since a model asked whether "acute pancreatitis" and "severe
acute pancreatitis" are the same concept will say yes, and that distinction
is the entire clinical content of some recommendations. Canonicalization
runs automatically before the vacuity check below, and the bundle format
records every merge under `merged_concepts` rather than silently
collapsing it, since a merge is a claim about the guideline.

**Modal predicates** 
*"Epidural analgesia may be considered"* correctly
filled `Rule.strength` with `"suggest (conditional)"`, but also put the
modal itself into the antecedent as `has_may` — a rule that can never run,
since no FHIR resource populates "has may". `modal_only()` is a fixed list
(English modals are a genuinely closed class, unlike abbreviations)
requiring *every* token in a candidate hole to be modal, so a
hedged-but-real predicate like `has_appropriate_acupoints` still survives
as a legitimate hole. `validate_rule` reports a bare modal as the
`MODAL_PREDICATE` error, and the gate refuses the statement as `defeasible`
rather than shipping a hole nothing will ever populate.

**Type checking cannot detect a vacuous rule** A spec no
patient can ever violate passes every syntactic gate and typechecks
cleanly — it certifies nothing. This is exactly what the canonical-form
`pca` example above produces, and why canonicalizing before this check runs
matters: without it, the aliasing is invisible to every other validator,
which inspects the rule tree alone.

**The validator's diagnosis sets the refusal reason** `MODAL_PREDICATE` among the blocking issues means
the reason code is `defeasible` (the guideline hedged); `VACUOUS_RULE` means
`beyond_dsl` (no non-vacuous obligation could be extracted). 

**Issue codes** `validate_rule`/`check_nondegenerate` can raise — errors
block compilation, warnings are advisory:

| code | severity | catches |
|---|---|---|
| `UNKNOWN_VAR` / `UNKNOWN_EVENT` | error | name outside the closed vocabulary |
| `BAD_OP` / `BAD_NODE` | error | malformed tree |
| `TYPE_ERROR` | error | wrong literal type, op undefined on the type, fractional `int` threshold |
| `UNBOUND_CODE` | error | code outside the bound value set — a hallucinated code |
| `EMPTY_INTERVAL` | error | `lo > hi`, or `lo == hi` with an open bound |
| `DEGENERATE_JUNCTION` | error | `And`/`Or` with fewer than 2 operands |
| `BAD_WINDOW` | error | negative `Since` window |
| `MISSING_PROVENANCE` | error | no page or sentence span |
| `NO_ACTION_ATOM` | error | the obligation constrains no action — usually `not_patient_level` |
| `IMPLAUSIBLE_THRESHOLD` | warn | threshold outside physiological range — usually a unit slip |
| `EXACT_REAL_COMPARISON` | warn | `==` against a real threshold; guideline thresholds are inequalities |
| `UNSATISFIABLE_ANTECEDENT` | error | the rule can never fire — often an OR rendered as an AND |
| `UNSATISFIABLE_OBLIGATION` | error | every applicable patient violates by construction |
| `VACUOUS_RULE` | error | `NOT(when -> then)` is unsat: the rule forbids nothing |
| `TAUTOLOGICAL_ANTECEDENT` | warn | the rule applies to everyone |

**Fragments** `fragment_of(*exprs)` returns the weakest logical fragment
containing the expressions — `PROP` (booleans/code equality), `LIA` (any
numeric/ordered comparison), or `MTL` (any `Since`) — computed from the
tree itself, not inferred from variable names, so the headline
formalizability spectrum falls out of the code rather than out of an
annotator's judgement.

**Verification from patient-checker:**
```
env = make_env(registry)              # the symbolic patient
D   = domain_axioms(registry, env)    # physiology, well-formedness, frame axioms
P    = the care plan, as constraints on env

MUST-violate          if UNSAT(D and P and phi)
CERTIFIED-compliant   if UNSAT(D and P and NOT phi)
MAY-violate            otherwise -- the model of (D and P and NOT phi) is the witness
```
`D` is not optional — without the domain axioms, witnesses are adversarial
completions of unconstrained latent state, and a single-call
`SAT(P and NOT phi)` is *not* a violation detector under a partial plan.

### 2.4 Gate

Assigns a refusal reason, or clears a statement for formalization.

**Inputs.** Binder output (unresolved concepts), validator flags
(`MODAL_PREDICATE`, `VACUOUS_RULE`), segmenter classification.

**Order.** Deterministic evidence is consumed first; the model is called only
on statements that survive it. Two reason codes never require a model call.

| Reason code | Set by | Trigger |
|---|---|---|
| `unbound_concept` | deterministic | binder: selector abstained, or no candidate returned |
| `defeasible` | deterministic | validator: `MODAL_PREDICATE` |
| `beyond_dsl` | mixed | `VACUOUS_RULE`, else model or fallback |
| `not_patient_level` | model | segmenter classifier, upstream |
| `vague_predicate` | model | formalizer declines |
| `unsupported_temporal` | model | formalizer declines |

**Invariant.** No deterministic path assigns `unsupported_temporal`. Pinned by
`test_gate.py`, which fails if one does.

**Rationale.** Asking a model whether a statement is too vague is circular, and
weak in practice (~0.71 F1 on error categorisation, arXiv:2607.04655), so the
model is the last check rather than the first.

**One deliberate non-inference**: an empty binding is *never* labelled
`not_patient_level` on its own. *"Consider referral after an adequate
trial"* binds nothing and **is** patient-level — it's vague, not
out-of-scope. Distinguishing the two is the segmenter's job, passed in as
`is_patient_level`; without that signal the gate reports `unbound_concept`
with evidence `NO_BOUND_VOCABULARY` and says so plainly.

**The three decisions**: `encode` (every clause bound, validates,
non-degenerate), `partial` (some clauses bound, the rest declared as
free-variable holes), `refuse` (one of the six reason codes above).
`Triage.encodable` is `encode or partial` — the binary the gate is
calibrated on, deliberately independent of the exact fragment (two
independent labelling passes agreed 28/29 on this binary but only 71.8% on
the exact fragment).

**`partial` verdict** *"Start an ACE
inhibitor when eGFR falls below 30 or the patient is at high risk"* — one
clause binds, one doesn't. Stubbing the undefined clause to `false` (what a
prior baseline system does) is unsafe in the direction that matters:
measured on a patient with eGFR 45, not on an ACEi, stubbing `false`
returns `CERTIFIED-compliant` — silently clearing a patient the guideline
was merely silent about — while leaving the hole free returns `MAY-violate`
with a witness naming exactly the missing assumption
(`has_at_high_risk=True`). A hole is `VarDef(unbound=True)` — no binding, no
domain axiom, since constraining it would be a claim the guideline never
made — but a retrieval failure is *never* turned into one: "refer to
nephrology" is perfectly bindable, and a missed index is a coverage bug to
fix, not a finding to launder into a free variable.


**`samples=k`**

Number of formalization attempts per statement. Default `1`.

`confidence = 1 - (refusals / k)`.

***Measurement*** 20 hand-labelled statements (10 precise, 10 vague), K=8 real
calls. Semantic equivalence decided by Z3 over the domain axioms, not string
comparison.

| | result |
|---|---|
| Vague statements | 80/80 samples refused |
| Precise statements | 0 produced more than one semantic reading |
| Textual variation on precise statements | 9/10 varied, up to 6 distinct strings per meaning |

Separation comes from refusal, not sampling variance, so one constrained
decode does what the 8-sample estimator (arXiv:2605.13817) was built to do.
The score is graded rather than bimodal: a borderline statement ("Monitor
serum potassium at least annually") returned 6/8.

***When to raise k*** Only when building an evaluation curve. Production runs
use `samples=1`.

***Reporting*** `Triage.evidence` is a tuple of `Evidence(source, code, detail)` tagged by where it came from (`binder`/`validator`/`solver`/
`model`) — a clinician asking "why was this refused" gets those, not a
score. `spectrum(triages)` computes the six-bucket formalizability table
(`PROP`/`LIA`/`MTL` for what encoded, the six reason codes for what didn't)
from the outputs, not from hand annotation. `risk_coverage(triages, truth)`
sorts by confidence, sweeps a threshold, and reports precision on `encode`
against clinician labels at each coverage level — the operating point is
chosen for **precision**, because a wrongly-accepted statement is the
failure this whole project argues against.

### 2.5 CQL

The lowering from a validated `dsl.Rule` to CQL is a deterministic total
function with no judgement in it — the step a governance committee can
re-run and get byte-identical output, and the step where "an LLM wrote our
clinical logic" stops being true. A repair loop feeds compiler/validator
errors back to the model, bounded (`max_repairs`, default 2), then refuses
(`beyond_dsl`) rather than looping indefinitely — an unbounded loop
converges on whatever happens to compile, which is exactly the failure this
project argues against (LLM self-correction is measured to be unreliable in
general; arXiv:2502.12065).

**An unbound predicate lowers to CQL's native `null`**
CQL is natively three-valued (`true and null` is `null`, `false and null`
is `false`), so an unbound hole propagates exactly the uncertainty it
should — a rule's `"Compliant"` define evaluates to `null` rather than a
verdict, visible in the library header, not buried in pipeline metadata.
Stubbing to `false` silently narrows a rule so it stops firing for the exact
patients the missing predicate was about, while stubbing to `true`
over-fires; leaving it `null` isn't an invention, since the target language
already has three-valued logic, so the honest option is also the native
one.

**Time is kept in CQL** The DSL has two
different lowerings of the same `Since` node: for Z3 (no notion of a
patient timeline) it flattens to an occurrence flag plus a day count; for
CQL (which evaluates over the actual record) it becomes a genuine bounded
existential — `exists ([Observation: X] O where O.effective during Interval[Today() - 14 days, Today()])`. This is why deferring the
time-indexing decision was safe rather than merely convenient: the DSL
doesn't need time-indexed variables for the CQL to already be faithful.

**Units and version-pinned terminology survive into the emitted CQL verbatim** — `"urine ACR" >= 30 'mg/g'`, never a bare `30`, and
`valueset "Diabetes codes": 'urn:oid:...' version '20241228'`, because two
value sets named "Diabetes" in VSAC can differ by nine years, and that
pinned version is what a reviewer actually resolves against.

**`check_cql()` is structural** — brackets balance and quoted names
resolve — and is explicitly *not* a compiler. The real gate is `cql-to-elm`
(from `cqframework/clinical_quality_language`, Java, published on Maven
Central), wired in as an injected `compiler` callable; without it,
`COMPILER_UNAVAILABLE` is raised at warn severity, saying plainly that only
structural checks ran. That gate is now live rather than aspirational:
`patient_checker/engine/` builds the JVM+Node toolchain (`setup.sh`),
`toolchain.available()` reports it, `test_toolchain.py` compiles generated
libraries with the official translator and executes the resulting ELM
against FHIR bundles (`patient-violating.json` / `patient-compliant.json`),
and `guidelines._lower_and_compile` puts every rule from an upload through
the same translator. A missing toolchain is recorded as
`compiler_available: false`, never counted as a pass.

### 2.6 Checking a patient

A rule is `phi = when -> then`. A *plan* is partial — only some values are
known, the rest are free — so "does this violate the rule" has three
answers, not two:

```
MUST-violate         no completion of the unknowns can satisfy the rule
CERTIFIED-compliant  no completion of the unknowns can violate the rule
MAY-violate          otherwise — the solver returns a concrete witness
```

A single `SAT` check can't tell MUST-violate from MAY-violate apart, so
every verdict is two solver calls, not one, and both run against the same
physiological domain axioms every rule shares.

Those three are the verdicts for an **obligation**. Deontic force selects
the question, not just the wording of the answer: a permission cannot be
violated, so `check_rule` dispatches on `rule.deontic` to a four-outcome
applicability lattice instead, none of whose outcomes is an accusation.

```
OPTION-not-applicable  the patient cannot meet the conditions for the option
OPTION-taken           the conditions hold and the option was taken
OPTION-not-taken       the conditions hold and it was not
OPTION-available       applicability or the choice is still undetermined
```

A guideline is either the built-in `fixtures` (from `real_rules.py`) or
something uploaded and run through CQL_converter live. Formalizing a sentence
into a rule is a **model step** — without an API key, a document still
segments and triages (real statement counts, real refusals) but produces
**zero rules**, reported honestly as `needs_model_count` rather than as a
guideline that "can't be encoded." A rule can also be **partial**: an
undefined predicate becomes a declared free variable (a hole), not a
silent drop or a stub.

## 3. How To Use It

### 3.1 The pipeline

**Offline** (the default): every stage runs fully offline with a heuristic
or mock callable standing in for its model hook — no API key, no network.
Each stage's own test file (`test_segment.py`, `test_bind.py`,
`test_gate.py`, `test_dsl.py`, `test_cql.py`) exercises exactly this path,
alongside `test_cassette.py`, `test_evaluate.py`, `test_exhaustive.py` and
`test_replay.py`. `test_toolchain.py` is the one exception: it shells out to
a JVM and Node, so it skips loudly unless the toolchain is built.

**Live**: pass real callables for the injected hooks — `llm.classify_block`
(segmenter), `llm.propose_mentions` / `llm.select_candidate` (binder),
`llm.formalize` (gate) — and a populated or remote terminology index
(`SqliteIndex` / `RemoteIndex` / `CachedIndex`). `demo.py`, at the
repository root, shows the live wiring end to end.

One sentence, end to end:

```python
import segment as sg, bind, gate, cql

blocks = sg.load_jats("corpus/PMC13276589.xml", "doc")
statements, manifest = sg.segment(blocks, "doc")            # + llm=... for live
stmt = statements[0]

ix = bind.fixture_index()                                    # or a live index
t = gate.triage(stmt.text, ix, statement_id=stmt.statement_id,
                formalize=lambda text, reg: my_formalize(text, reg),
                is_patient_level=stmt.is_patient_level,
                source_page=None, source_span=stmt.text)

if t.decision in ("encode", "partial"):
    lib = cql.rule_to_cql(t.rule, t.registry)
    print(lib.text)
else:
    print(t.reason_code)                                      # counted, not discarded
```

Per-stage CLI:
```
python3 segment.py PMC13276589 --top 15
python3 segment.py --search 'PUB_TYPE:"Practice Guideline"'      # find OA guidelines
python3 bind.py --registry "In CKD with hyperkalemia, avoid an ACE inhibitor."
python3 tests/test_segment.py / tests/test_bind.py / tests/test_gate.py / tests/test_dsl.py / tests/test_cql.py
```

The full offline suite runs with no API key, and one real run replays
byte-for-byte from a recorded cassette (`cassette.py`, driven by
`test_replay.py`) — also no key needed, since prompts and responses are
stored as SHA-256 hashes. Responses are kept as a list per key rather than
collapsed to one value, because `gate` relies on K identical-prompt calls
genuinely disagreeing; a prompt with no recorded response raises
`CassetteMiss` rather than falling through to the network.

### 3.2 The checker

```sh
cd patient_checker
```

```sh
../.venv/bin/python3 -m pip install -r ../requirements.txt
../.venv/bin/python3 api.py
# open http://localhost:8000/        (dashboard)
# open http://localhost:8000/demo    (no setup, 18 live checks over 3 guidelines)
# open http://localhost:8000/demo/toy (one rule, three patients, three verdicts)
```

```sh
curl -s localhost:8000/guidelines/fixtures/rules | python3 -m json.tool

curl -s -X POST localhost:8000/guidelines/fixtures/check \
  -H 'Content-Type: application/json' \
  -d '{"patient": {"potassium": 5.4}, "plan": {"ace_inhibitor_on": true}}'
```

`GET /guidelines/<id>/residue` shows what CQL_converter refused to encode for
that guideline, and why — the other half of the deliverable: a checker
that only shows what compiled tells a reader the tool covered everything.

## 4. Glossary

**Checker verdicts**
- **MUST-violate / CERTIFIED-compliant / MAY-violate** — the three verdicts
  for an obligation (§2.6); MAY-violate always carries a minimized
  counterexample, the smallest set of still-free values that alone forces
  the violation.
- **OPTION-taken / OPTION-not-taken / OPTION-not-applicable /
  OPTION-available** — the four verdicts for a permission (§2.6). A permission
  is never violated, so none of these accuses anyone.
- **Fixtures** — the ten always-available rules in `real_rules.py`, as
  opposed to an uploaded guideline.

**Pipeline data types**
- `Statement` — one candidate recommendation: verbatim text + `Provenance`.
- `Provenance` — a locator (re-finds the source element) plus character
  offsets (slice out the statement).
- `Block` — one layout unit from a loader (`boxed_text`, `table_row`,
  `list_item`, `section_text`, `heading`, `figure`).
- `Binding` — a bound clinical concept: variable, system/code/version, the
  full candidate list shown (and what was rejected), the audit trail.
- `Registry` — the closed vocabulary a rule may reference; anything outside
  it is a hard error.
- `VarDef` / `EventDef` — one typed registry variable / one datable
  clinical event.
- `Rule` — `when -> then`, plus an action/target gloss, strength, deontic
  operator, and mandatory provenance.
- `Refusal` — a first-class non-encoding, with a reason code (below) —
  never a failure.
- `Triage` — the gate's per-statement verdict: decision, reason, fragment,
  confidence, evidence.
- `Evidence` — one signal that fired, tagged by source (`binder` /
  `validator` / `solver` / `model`).
- `Synthesis` — the CQL layer's own output record: rule, library, refusal,
  repair attempts.

**Core logic concepts**
- `phi = when -> then` — what a `Rule` denotes; a violation is `phi` false
  for a given patient.
- Fragment (`PROP` / `LIA` / `MTL`) — the weakest logical fragment a rule's
  tree needs, computed from the tree itself rather than annotated by hand.
- Hole / open hole — a named free variable for a predicate the source
  states but never defines: no binding, no domain axiom.
- Canonical form — merging two registry entries only on hard evidence
  (the same terminology code, or a hole restating a bound variable) — never
  on a similarity judgement.
- `plausible` range — a hand-curated physiological range per numeric
  variable; no terminology publishes this, and it is asserted at solve
  time.
- Deontic (obligation / permission) — an obligation's violation is
  `when AND NOT then`; a permission's is `false`, by construction. It is
  an operator on the rule rather than a label on the verdict;
  `guidelines.py` carries it out of the DSL and the checker dispatches
  on it.
- Modal predicate — a hedge word ("may", "appropriate") that is the
  guideline's own deontic force wearing a patient predicate's clothes; it
  is refused, never turned into a hole.

**Decision vocabulary**
- encode / partial / refuse — the gate's three outcomes.
- The six refusal reasons: `unbound_concept`, `not_patient_level`,
  `unsupported_temporal`, `beyond_dsl`, `vague_predicate`, `defeasible` —
  see §2.4 for which are actually deterministic (only two are:
  `unbound_concept`, `defeasible`).

**CQL-specific terms**
- library / define — a CQL module, and one named logical clause inside it.
- valueset / code declaration — a version-pinned reference to an external
  terminology set, or to a single code.
- ELM — Expression Logical Model; the typed JSON IR `cql-to-elm` emits; what an execution engine
  actually consumes (CQL text itself is not executable).
- Three-valued logic — CQL's native `true` / `false` / `null`, used so an
  unbound predicate propagates honest uncertainty instead of being stubbed.

**Labels**

| label | means |
|---|---|
| `ENCODE` | every concept nameable and bindable, every threshold numeric or boolean, the whole obligation fits `when → then` with nothing lost |
| `PARTIAL` | encodable except for one or more predicates the source states but never defines — only if the undefined phrase can be quoted **verbatim** from the statement; otherwise it's a REFUSE |
| `REFUSE` | no faithful encoding; pick exactly one of the six reason codes (§2.4) |
| `NOT_A_RECOMMENDATION` | the segmenter was wrong — background, methods, an evidence summary, a heading. Labelled, not discarded, since this is the segmenter's own error rate |

Decision order:
1. Is it a recommendation about a patient at all? No → `NOT_A_RECOMMENDATION`
   (a heading/evidence summary) or `not_patient_level` (about a service,
   institution, or research agenda — **institutional capability counts
   here**, e.g. "where expertise is available").
2. Is the action nameable as a variable? No → `unbound_concept`.
3. Does it need something the fragment can't express? →
   `unsupported_temporal` or `beyond_dsl`.
4. Is the obligation explicitly overridable by clinician judgement? →
   `defeasible`.
5. Is a threshold/qualifier used but never defined? Quotable verbatim, rest
   faithful → `PARTIAL`. Otherwise → `vague_predicate`.
6. Otherwise → `ENCODE`.

Rules of thumb: the recommended *action* must survive in the encoding, or
it isn't an encoding of that sentence — a rule capturing only the
preconditions is wrong, not partial. Outcomes are not conditions ("relieves
pain" is an effect, not a `when`-clause). Never invent a threshold the
source doesn't state, even when the usual clinical cutoff is common
knowledge. Population restrictions are load-bearing — dropping one changes
who the rule applies to. A sentence carrying two recommendations gets
split and each labelled separately.

Deontic label (independent of the four above, on every statement that
isn't `NOT_A_RECOMMENDATION`): **obligation** ("we recommend X", "X is
indicated", and — this is a deliberate ruling — "we suggest X", since
GRADE-conditional wording is weak *evidence*, not an optional action) or
**permission** ("X may be considered", "at the discretion of"). If a
sentence has both, the permissive wording governs.
