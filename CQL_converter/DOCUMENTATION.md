# CQL_converter: Documentation

Track A turns a clinical guideline document into formally checkable rules —
refusing rather than guessing wherever it can't. This file is self-contained:
everything below used to be split across `SEGMENT.md` / `BIND.md` / `GATE.md`
/ `DSL.md` / `CQL.md` / `GOLD.md` / `NOTES.md` / `RESULTS.md`; those files may
not travel with this one, so their load-bearing content is folded in here.

---

## 1. Overview

Document → **Segment** → **Bind** → **Gate** (which synthesizes a DSL
`Rule`/`Refusal`) → **CQL**.

- [`segment.py`](segment.py) — a document in, verbatim recommendation
  statements out, each with a provenance span.
- [`bind.py`](bind.py) — a statement's clinical phrases in, a closed
  `dsl.Registry` of retrieved (never generated) codes out.
- [`dsl.py`](dsl.py) — the typed intermediate representation every other
  stage reads or writes: `Registry`, `Rule`, `Refusal`, the Z3 lowering. Not
  a separate sequential stage — Bind produces a `dsl.Registry`, Gate's
  `triage()` calls an injected `formalize` hook that produces a `dsl.Rule`
  or `Refusal` directly, and CQL lowers that `Rule`. DSL is the shared
  substrate everything else is built from, not a fifth stage in a line.
- [`gate.py`](gate.py) — per statement, decide encode / partial / refuse,
  aggregating deterministic evidence before ever asking a model.
- [`cql.py`](cql.py) — a validated `dsl.Rule` in, an HL7 CQL library out, by
  a deterministic, re-runnable lowering.

## 2. How It Works

### 2.1 Segmenter

Finding sentences that look like recommendations is easy; the job is
producing statements a clinician can point back at the source document,
because that's what makes every downstream claim checkable. Every
`Statement` carries a `Provenance` (locator + character offsets) that
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
typesetting time — the boxed-callout problem is solved *structurally* for
most of the corpus, so `load_jats` is the main road and `load_pdf` is the
fallback, not the other way round. `fetch_pmc(pmcid)` downloads and caches
it; of 3,129 open-access practice guidelines on Europe PMC, essentially all
have retrievable XML. But XML *existing* is not the same as XML being
*complete* — one measured guideline (ESO stroke blood-pressure) renders
**26 of 26 tables as embedded JPEG images with zero extractable text**; the
XML route reaches only the narrative restatements of its recommendations,
never the formal boxed ones. `_table_rows` emits a `figure` block carrying
the caption in that case, so the gap lands in `manifest["figures_uncovered"]`
instead of silently vanishing. The real, independently-typeset PDF for that
same document *does* carry the missing tables as genuine selectable text —
XML and PDF are not two views of the same content, they can diverge in
completeness — but `load_pdf`'s box-recovery path is presently broken
(it calls `page.rect.get_area()`, a method the installed `pymupdf` no
longer has) and has never executed against a real guideline.

For table rows specifically: the condition and the action sit in different
columns, so no contiguous span of the document *is* the rule. Rows are
rendered header-folded (`Finding: eGFR below 30; Action: refer to
nephrology`), provenance points at the `<tr>`, and every cell *value* is
verbatim even though the assembled string is not — a documented exception,
not a bug.

### 2.2 Binder

It is not a lookup service the compiler calls — it builds the closed
vocabulary the compiler is then constrained to. Whatever it cannot bind
never becomes a variable, so it goes to `Refusal(unbound_concept)` instead
of a hallucinated code. The model is shown a numbered candidate list and
returns an index (or ABSTAIN) — never a code string — so a hallucinated
code cannot be expressed at all.

Flow: propose mentions → search (phrase ∪ abbreviation expansion, scoped by
kind) → rerank by unit (deterministic, before any model) → select (index or
ABSTAIN) → validate (chosen code must be in the index) → `Binding` or
`Refusal`.

**Sources, no UMLS account needed:** SNOMED via `tx.fhir.org`
(`$expand?filter=`, `?fhir_vs=isa/<code>`, `$validate-code`), LOINC via NLM
Clinical Tables, drugs and ATC classes via RxNav. A UMLS licence adds
**VSAC**'s curated, steward-versioned value sets — better than a computed
is-a expansion — routed ahead of SNOMED/ATC for the set-valued kinds
(`condition`, `drug_class`, `procedure`) and unioned with them; nothing is
blocked without it.

**Why a real vocabulary matters, measured directly.** "eGFR" searched
against live LOINC returned *only* an EGFR-gene genetics test — epidermal
growth factor receptor, not kidney function — and being the sole candidate,
a naive selector called it a "clear lexical win." Being the only candidate
is not evidence: the selector now requires an exact term match, a unit
match, or full query-token coverage before binding anything, which alone
turns that case into a safe refusal instead of a wrong answer. Two more
deterministic fixes followed the same shape: `NOT_AN_OBSERVATION` demotes
grouper/value-set codes that win on token overlap against a real result
code, and units run *before* any model — LOINC 9318-7 (mg/g) and 14585-4
(mg/mmol) are the same threshold ~8.84 apart, and the sentence states which
one, so `rerank_by_unit` settles it with no judgement required.

**Lexical retrieval has a hard ceiling, and semantic retrieval exists to
close it.** Guideline prose and terminology vocabularies are written by
different communities: "kidney function"/"Renal function" and "epidural
analgesia"/"Epidural anaesthesia" share zero or one token, and an inverted
index cannot retrieve a document sharing no term with the query — that is a
property of the index, not a tuning problem. `semantic.py` (a separate
module, so `torch` never enters `bind.py`'s import path) embeds terms into
a meaning vector and searches nearest-neighbour, **as a reserved fallback
slot, never merged into the lexical ranking** — a cosine similarity and a
BM25 score are not comparable, and interleaving them buried a 0.954-similarity
correct answer below eight wrong lexical matches in testing. Measured
effect on the fixable-failure set: lexical alone found 3/10, lexical ∪
semantic found 5/10. **This layer needs a local index built and cached to
disk before it does anything** (`python3 semantic.py --build --fast`); if
that hasn't been run, `bind.semantic_index()` returns `None` and every
lexical-only miss stays a miss — this is a real, live-observed gap in this
environment, not a hypothetical one.

**Terminology retrieval starves silently if kind/system filters aren't
inside the SQL.** Fetching the top-N BM25 rows across every kind and then
filtering in Python means a common word like "insulin" can fill every slot
with the wrong kind before the real candidate is ever seen — measured: 1
candidate returned out of 171,231 real drug rows, with the correct SNOMED
concept sitting in the table the whole time. Filtering inside the query
fixed it. Separately, `drug`-kind and `drug_class`-kind searches are
restricted to specific terminology systems (RxNorm/VSAC and ATC/RxNorm/VSAC
respectively) as a matter of design — SNOMED's own drug hierarchy exists in
a real, loaded terminology database but is deliberately never searched for
those kinds, which is correct architecture but means a concept that only
has a SNOMED-sourced entry can be genuinely unreachable until the "real"
system for that kind (chiefly ATC for drug *classes*) is populated — and
there is currently no bulk loader anywhere in this codebase for that
system, unlike SNOMED/LOINC/RxNorm, which do have one each.

### 2.3 DSL

A rule is a formula, not a label. `Rule.when` / `Rule.then` denote
`phi = when -> then`; a patient violates the rule iff `phi` is false for
them, and a Z3 counterexample is a model of `NOT phi`. `action` / `target`
are a human-readable gloss with no semantics — branching on `rule.action` in
verification code is a sign something has gone wrong.

**The registry variables are a symbolic patient, not an index over real
patients.** `egfr` is not "some particular person's eGFR", it stands for
*any* patient's eGFR; when Z3 returns `sat`, the model it hands back **is**
a concrete patient (`egfr=29, has_diabetes=True, on_acei_arb=False, ...`).
One solver query is one patient — many patients is a loop, not a bigger
query, because no clinical guideline relates two patients to each other.

**Registry variable types**: `real`/`int`/`bool`/`code`/`date`, each with a
fixed set of legal comparison operators and a specific Z3 lowering (`bool`
→ `z3.Bool`, `code` → `z3.String` equality-only, `date` → ISO-8601 in the
DSL but `z3.Int` ordinals in the solver, `int` → `z3.Int` never `Real`, or
the solver hands back `days = 14.5`). `role` is `observation` (patient
state) or `action` (something a care plan decides); `validate_rule` rejects
an obligation naming no action atom, and Track B reads `reg.actions()` to
write frame axioms over actions a plan never mentions. `plausible` is a
hand-curated physiological range asserted at solve time so a counterexample
can't land at eGFR = 2.0, and it doubles as a validator warning on
unit-slip thresholds (the mg/g-vs-mg/mmol factor of ~8.84 above compiles
cleanly and silently without it).

**Expression nodes**: `Cmp(var, op, value)`, `Between(var, lo, hi,
lo_inclusive, hi_inclusive)` — inclusivity is explicit and required, because
boundary slips (eGFR 29 vs 30) compile cleanly and are the single most
common silent bug — `Since(event, days, inclusive)`, and `Not`/`And`/`Or`
(junctions need ≥2 operands). `And`/`Or` are first-class rather than derived
because an expression language without disjunction doesn't prevent
disjunctions, it silently converts them to conjunctions — one measured
AND-for-OR substitution flipped the verdict for 69.5% of the plausible
patient population, and it typechecked the whole way.

**Time is flattened to derived state, not indexed.** `Since(event, days)`
lowers to `<event>_occurred AND days_since_<event> <= days`. The occurrence
flag is load-bearing: without it, a bare day-count is satisfiable for a
patient who was *never checked* — the solver just picks a small number —
and "never checked at all" is the commonest real monitoring violation.
Multi-visit predicates ("on two occasions ≥90 days apart" — the actual
definition of CKD) need more than one timepoint and are genuinely out of
this fragment; they refuse as `unsupported_temporal`. Moving to
time-indexed variables (`var[t]` over a horizon) is additive — no
already-written rule would need to change — but it changes the headline
formalizability number, so it's an explicit measurement decision that
hasn't been made, not an oversight.

**`Rule.deontic` is an operator, not a label.**
```
obligation   O(then | when)    violation = when AND NOT then
permission   P(then | when)    violation = FALSE, unconditionally
```
*"We recommend anticoagulant prophylaxis"* is an obligation — a patient
meeting `when` without `then` is a real deviation. *"Epidural analgesia may
be considered"* is a permission — that same patient has violated nothing.
GRADE-conditional wording ("we suggest X") is **not** treated as
permission — the weakness there is in the evidence, not the obligation;
only explicit permission language counts, or treating every weak
recommendation as optional would silence most of a guideline. Prohibition
isn't a third case: `F(p) = O(NOT p)`, already written as `action="avoid"`
with a negated consequent. `deontic` is a *required* field in the decoding
schema specifically so a default can't silently promote a permission into
an obligation, and `deontic_of()` only ever promotes obligation→permission
from wording, never the reverse — guessing permission wrongly silences an
alert (recoverable), guessing obligation wrongly accuses a clinician (not).
A permission gets its own four-outcome verdict lattice (`OPTION-taken` /
`OPTION-not-taken` / `OPTION-not-applicable` / `OPTION-available`); none of
them is an accusation. Measured: dropping this operator and falling back to
a wording-only guard flips the verdict on 4 of 5 permissive/obligation
pairs — the largest single effect measured across the whole ablation study.

**Canonical form: two names can be one concept.** A model-generated
registry can bind the same real concept under two different variable names
in a way a person never would. Measured, on a real guideline: the
formalizer wrote both `pca` and `patient_controlled_intravenous_analgesia`
— both bound to the identical SNOMED code — into one rule, producing
`(has_ap AND pca) -> patient_controlled_intravenous_analgesia`, i.e.
`(A AND B) -> B`. That's valid under every interpretation, so it certifies
every patient and can never fail — it doesn't look like a bug, it looks
like a rule that works. Only two signals are trusted to merge two names:
the same terminology `system|code|version`, or a hole that's a `has_`-
prefixed restatement of an already-bound variable. Never a similarity
judgement — a model asked whether "acute pancreatitis" and "severe acute
pancreatitis" are the same concept will say yes, and that distinction is
the entire clinical content of some recommendations. Canonicalization runs
automatically before the vacuity check (below), and the bundle format
records every merge under `merged_concepts` rather than silently
collapsing it — a merge is a claim about the guideline, so it's reported.

**Modal predicates are a closed word class, and they get refused, not
turned into holes.** *"Epidural analgesia may be considered"* correctly
filled `Rule.strength` with `"suggest (conditional)"` — and also put the
modal itself into the antecedent as `has_may`. That rule can never run: no
FHIR resource populates "has may". `modal_only()` is a fixed list (English
modals are a genuinely closed class, unlike abbreviations) requiring *every*
token in a candidate hole to be modal, so a hedged-but-real predicate like
`has_appropriate_acupoints` still survives as a legitimate hole.
`validate_rule` reports a bare modal as the `MODAL_PREDICATE` error, and the
gate refuses the statement as `defeasible` rather than shipping a hole
nothing will ever populate.

**`VACUOUS_RULE` is the check a type system cannot do for you.** A spec no
patient can ever violate passes every syntactic gate and typechecks
cleanly — it certifies nothing. This is exactly what the canonical-form
`pca` example above produces, and why canonicalizing before this check runs
matters: without it, the aliasing is invisible to every other validator,
which inspects the rule tree alone.

**The validator's diagnosis sets the refusal reason**, rather than falling
through to a generic one: `MODAL_PREDICATE` among the blocking issues means
the reason code is `defeasible` (the guideline hedged); `VACUOUS_RULE` means
`beyond_dsl` (no non-vacuous obligation could be extracted). Previously both
fell through to `unbound_concept` or a generic `beyond_dsl`, which told a
reader to go looking for a missing terminology code when the real problem
was that the sentence had no checkable content at all.

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

**Fragments.** `fragment_of(*exprs)` returns the weakest logical fragment
containing the expressions — `PROP` (booleans/code equality), `LIA` (any
numeric/ordered comparison), or `MTL` (any `Since`) — computed from the
tree itself, not inferred from variable names, so the headline
formalizability spectrum falls out of the code rather than out of an
annotator's judgement.

**Verification, from Track B:**
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

Most of the refusal decision is *not* "ask a model if this is vague" — that
is circular, and even purpose-built diagnostic models measure only ~0.71 F1
on this kind of error categorisation (arXiv:2607.04655). The gate
aggregates deterministic evidence the earlier stages already produced —
what the binder couldn't resolve, what the validator rejects, what the
solver shows to be degenerate — and only *then* asks a model to try.

Be precise about how much that buys, though: of the six refusal codes, only
**two** are actually deterministic. The full picture:

| reason_code | who decides | what actually sets it |
|---|---|---|
| `unbound_concept` | **deterministic** (binder) | `bind.bind()` when the selector returns ABSTAIN, no candidate survives, or a value set expands to no codes |
| `defeasible` | **deterministic** (validator) | `MODAL_PREDICATE` among the blocking codes — the guideline hedged, so the "predicate" is its deontic force wearing a patient fact's clothes. The model may also name it directly. |
| `beyond_dsl` | **mixed** | `VACUOUS_RULE` deterministically; otherwise the model's own declared refusal code, or the fallback when nothing bound-specific applies |
| `not_patient_level` | **model** (segmenter's classifier) | `is_patient_level is False` — an LLM call made *upstream*, on a live run; heuristic only when that call errors or its quote isn't verbatim. Never independently derived inside the gate. |
| `vague_predicate` | **model** | the formalizer declining |
| `unsupported_temporal` | **model** | the formalizer declining. No deterministic check produces it — there is no fragment test for "needs a second timepoint", only the model recognising one. |

An earlier version of this table claimed four of six were deterministic
("obligation names no action" for `not_patient_level`,
"multi-visit predicate" as a deterministic trigger for
`unsupported_temporal") — both wrong, and the error propagated into
anything drawn from it, since prose in a codebase is itself an artifact
nothing tests. The tests now pin exactly which codes are reachable with no
model at all, so the claim can't drift silently again.

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

**`partial` is the verdict that actually matters.** *"Start an ACE
inhibitor when eGFR falls below 30 or the patient is at high risk"* — one
clause binds, one doesn't. Stubbing the undefined clause to `false` (what a
prior baseline system does) is unsafe in the direction that matters:
measured on a patient with eGFR 45, not on an ACEi, stubbing `false`
returns `CERTIFIED-compliant` — silently clearing a patient the guideline
was merely silent about — while leaving the hole free returns `MAY-violate`
with a witness naming exactly the missing assumption
(`has_at_high_risk=True`). A hole is `VarDef(unbound=True)`: no binding, no
domain axiom (constraining it would be a claim the guideline never made).
A retrieval failure is *never* turned into a hole — "refer to nephrology"
is perfectly bindable, and if the index misses it that's a coverage bug to
fix, not a finding to launder into a free variable.

**Sample count (`samples=k`) is a confidence estimator, not an ambiguity
detector — and that took a real measurement to establish.** The original
idea (from arXiv:2605.13817) was that k repeated calls on a vague statement
should *disagree*, giving a vagueness signal without needing ground truth.
Measured on 20 hand-labelled statements (10 precise, 10 vague) at K=8 real
calls: the model doesn't produce 8 different encodings for a vague
statement, it produces 8 *refusals*. All 80 vague-statement samples were
refused; 0 of the encoded statements produced more than one semantic
reading (semantic equivalence decided by Z3 over the domain axioms, not
string comparison — 9 of 10 precise statements produced textually different
encodings, up to 6 distinct strings for one meaning, that the solver
correctly collapsed to one class). So there was nothing to disagree about:
separation comes from refusal, not from sampling variance, and one
constrained decode does what an 8-sample estimator was built to do, at an
eighth of the cost. `confidence = 1 - (refusals / k)` is a graded score,
not just bimodal — a deliberately borderline statement ("Monitor serum
potassium at least annually") came back 6/8. `samples=1` is the production
default; `k>1` is worth paying for only when building an evaluation curve.

**Reporting.** `Triage.evidence` is a tuple of `Evidence(source, code,
detail)` tagged by where it came from (`binder`/`validator`/`solver`/
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

**An unbound predicate lowers to CQL's native `null`, not `true`/`false`.**
CQL is natively three-valued (`true and null` is `null`, `false and null`
is `false`), so an unbound hole propagates exactly the uncertainty it
should — a rule's `"Compliant"` define evaluates to `null` rather than to a
verdict, and that null is visible in the library header, not buried in
pipeline metadata. Stubbing to `false` silently narrows a rule so it stops
firing for the exact patients the missing predicate was about; stubbing to
`true` over-fires. Leaving it `null` is not an invention — the target
language already has three-valued logic, so the honest option is also the
native one.

**Time is kept in CQL, not flattened the way Z3 needs it.** The DSL has two
different lowerings of the same `Since` node: for Z3 (no notion of a
patient timeline) it flattens to an occurrence flag plus a day count; for
CQL (which evaluates over the actual record) it becomes a genuine bounded
existential — `exists ([Observation: X] O where O.effective during
Interval[Today() - 14 days, Today()])`. This is why deferring the
time-indexing decision was safe rather than merely convenient: the DSL
doesn't need time-indexed variables for the CQL to already be faithful.

**Units and version-pinned terminology survive into the emitted CQL
verbatim** — `"urine ACR" >= 30 'mg/g'`, never a bare `30`, and
`valueset "Diabetes codes": 'urn:oid:...' version '20241228'`, because two
value sets named "Diabetes" in VSAC can differ by nine years, and that
pinned version is what a reviewer actually resolves against.

**`check_cql()` is structural only** — brackets balance and quoted names
resolve — and is explicitly *not* a compiler. The real gate is
`cql-to-elm` (from `cqframework/clinical_quality_language`, Java, published
on Maven Central), wired in as an injected `compiler` callable; without it,
`COMPILER_UNAVAILABLE` is raised at warn severity with text saying plainly
that only structural checks ran. Nothing has been run against a real
`cql-to-elm` translation or a FHIR bundle as of this writing — every
library that exists has passed the structural checks only, which is a much
weaker claim than "compiles."

## 3. How To Use It

**Offline** (the default): every stage runs fully offline with a heuristic
or mock callable standing in for its model hook — no API key, no network.
Each stage's own test file (`test_segment.py`, `test_bind.py`,
`test_gate.py`, `test_dsl.py`, `test_cql.py`) exercises exactly this path.

**Live**: pass real callables for the injected hooks — `llm.classify_block`
(segmenter), `llm.propose_mentions` / `llm.select_candidate` (binder),
`llm.formalize` (gate) — and a populated or remote terminology index
(`SqliteIndex` / `RemoteIndex` / `CachedIndex`). `demo.py` and
`goldset.py` show the live wiring end to end.

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
python3 test_segment.py / test_bind.py / test_gate.py / test_dsl.py / test_cql.py
```

The full offline suite runs with no API key, and one real run replays
byte-for-byte from a recorded cassette (`test_replay.py`) — also no key
needed, since prompts and responses are stored as SHA-256 hashes.

## 4. Glossary

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
  `when AND NOT then`; a permission's is `false`, by construction.
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
- ELM — the typed JSON IR `cql-to-elm` emits; what an execution engine
  actually consumes (CQL text itself is not executable).
- Three-valued logic — CQL's native `true` / `false` / `null`, used so an
  unbound predicate propagates honest uncertainty instead of being stubbed.

**Gold-standard labels** — the labels a human annotator assigns to a
statement, written *before* ever running the pipeline on it (criteria
derived from a tool's own output cannot be used to measure that tool). The
question for each statement: could a careful person encode this faithfully
in the DSL, and if not, why not?

| label | means |
|---|---|
| `ENCODE` | every concept nameable and bindable, every threshold numeric or boolean, the whole obligation fits `when → then` with nothing lost |
| `PARTIAL` | encodable except for one or more predicates the source states but never defines — only if the undefined phrase can be quoted **verbatim** from the statement; otherwise it's a REFUSE |
| `REFUSE` | no faithful encoding; pick exactly one of the six reason codes (§2.4) |
| `NOT_A_RECOMMENDATION` | the segmenter was wrong — background, methods, an evidence summary, a heading. Labelled, not discarded, since this is the segmenter's own error rate |

Decision order (work down the list, stop at the first that applies):
1. Is it a recommendation about a patient at all? No → `NOT_A_RECOMMENDATION`
   (a heading/evidence summary) or `not_patient_level` (about a service,
   institution, or research agenda — **institutional capability counts
   here**, e.g. "where expertise is available").
2. Is the action nameable as a variable? No → `unbound_concept` (note in
   `notes` whether the concept genuinely has no code anywhere, versus
   retrieval simply missing it — the latter is a bug in us, not the
   guideline).
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

## 5. Design Decisions Worth Keeping

- **Null, not false, for an unbound predicate.** CQL is natively
  three-valued; stubbing an uncomputable node to `false` silently clears
  patients the guideline was merely silent about — unsafe in the direction
  that matters. Leaving it `null` names the assumption instead of hiding it.
- **Canonicalize before the vacuity check.** Two names bound to the same
  terminology code (or a hole restating a bound variable) are collapsed to
  one concept before `check_nondegenerate` runs — otherwise a rule like
  `(A AND B) -> B`, where A and B are secretly identical, typechecks, is
  non-vacuous by every syntactic test, and certifies every patient by
  construction. Measured on a real guideline (§2.3).
- **Deontic force is an operator, not a guard on a verdict string.** A
  permission's violation condition is the constant `false`, so nothing
  downstream needs to remember to skip a check afterward. Measured: once a
  rule is permissive, dropping the operator flips the verdict on 4 of 5
  pairs — the largest effect measured in the whole ablation study.
- **Terminology retrieval is semantic where lexical retrieval structurally
  cannot close the gap** — different vocabularies for the same concept
  share no tokens ("kidney function"/"Renal function"), so no amount of
  query rewriting helps; it needs a meaning-embedding fallback, reserved
  rather than merged into lexical ranking (§2.2). This layer is real and
  measured but requires its local index to actually be built first
  (`python3 semantic.py --build --fast`) — unbuilt, it's silently inert.
- **Time is flattened, not yet time-indexed — an open decision, not an
  oversight.** `Since(event, days)` lowers to an occurrence flag plus a day
  count for Z3; genuinely multi-visit predicates ("on two occasions ≥90
  days apart" — the actual definition of CKD) are out of the fragment and
  refuse as `unsupported_temporal`. Moving to `var[t]` over a horizon is
  additive but changes the headline formalizability number, so it's a
  measurement decision, not a feature toggle.
- **Only two of six refusal reasons are actually deterministic.**
  `unbound_concept` (the binder) and `defeasible` (the validator's
  `MODAL_PREDICATE` check); `beyond_dsl` is mixed; the rest need the model,
  either to decline directly or to have classified the statement upstream.
  This corrects an earlier claim (once embedded in `gate.py`'s own
  docstring) that four were free-standing deterministic checks — an
  architecture description silently inherited a stale docstring's mistake,
  which is why `test_gate.py` now pins exactly which codes are reachable
  with no model at all.
- **A silent fallback is worse than a crash.** Twice, a real bug (a
  malformed JSON schema field; a hard requirement for a PDF-only page
  number on an XML-sourced rule) made an entire live run silently fall back
  to the offline heuristic or refuse everything — producing a
  plausible-looking result that was actually a self-inflicted outage,
  indistinguishable from a genuine finding about the guideline. Usage
  tracking now prints `"N FAILED CALL(S) — these fell back to the offline
  heuristic, so any result above is NOT the model's"` next to any result
  that needs it, and `dsl.check_decoding_schema()` constraint-checks every
  schema offline before it's ever sent live.
- **A model hook that is never wired in is a different failure from one
  that errors, and it hid for a long time.** The segmenter's own
  classifier existed, was documented, and was never actually passed to a
  live run — both `demo.py` and `goldset.py` called `segment(blocks,
  doc_id)` without `llm=...`, so every "live" run judged candidate
  statements with the offline heuristic regardless. It hid because
  `llm=None` is a valid, working mode (the pipeline completes, statements
  come out), the offline tests are designed to exercise exactly that path,
  and nobody had a number to compare against until the gold set existed.
  Once switched on: of 12 true non-recommendations in the gold set, the
  heuristic let all 12 through; the model correctly rejected 11.

## 6. Status, and the numbers behind it

Live runs have happened repeatedly against the 9-document PMC corpus with
real model calls at every wired hook (segmenter classifier, binder
mention-proposer and selector, gate's formalizer) — not just fixtures.

**The formalizability ceiling, measured against 45 gold-labelled
statements (5 per document)**: 27% of what the segmenter extracts is not a
recommendation at all (rationale prose, evidence summaries, list stems,
regulatory facts) — a number that sits upstream of every other figure and
had never been measured before it was. Of the 33 real recommendations that
remain: 7 ENCODE, 5 PARTIAL, 21 REFUSE — **a ceiling of 12/33 ≈ 36%**. A
perfect formalizer, with perfect retrieval and zero bugs, would still only
encode about a third of the recommendations in this corpus; the other 64%
is a property of the guidelines, not (only) of the tool. Refusal reasons
skew heavily toward vagueness (`vague_predicate` 7, `not_patient_level` 5,
`unbound_concept` 4, `defeasible` 3) and only 1 of 21 refusals was
`beyond_dsl` — expressiveness of the fragment is almost never the binding
constraint on this corpus; the residue is in the prose, not the logic
(though one specialty-specific document, built almost entirely around dose
titration, refused 3 of 5 statements as `beyond_dsl` for exactly that
reason — a real counterexample to "almost never," worth reporting as
specialty-dependent rather than universal). Separately, 5 of the 33 real
recommendations (15%) are permissive rather than obligatory — small, but
every one would be silently misreported as a violation by any pipeline
that lowers straight to a two-valued verdict.

What's still outstanding: `cql-to-elm` has never actually run, so no rule
has executed against a FHIR bundle; no clinician has adjudicated any
output; the corpus's open-access sampling frame is a real, stated
limitation, not yet a representative one; and getting even the first clean
end-to-end run on a real guideline took four genuine bugs found only by
running real data — none of which the fixtures could have caught, because
each depended on something (network-shaped API errors, thread concurrency,
ill-typed live model output, JATS having no page numbers) that a fixture by
construction doesn't have. Offline, the full test suite passes with no API
key, and one real run is replayable byte-for-byte from a recorded cassette,
also with no key needed.
