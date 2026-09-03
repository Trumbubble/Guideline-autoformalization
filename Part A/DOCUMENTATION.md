# Part A: Documentation

Track A turns a clinical guideline document into formally checkable rules —
refusing rather than guessing wherever it can't. This is the brief entry
point; each linked file in §8 has the full detail this page compresses.

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
real publisher markup with zero failures.

Flow: loader (JATS / HTML / PDF) → `Block[]` → sentence splitting with an
offset map preserved through normalization → prefilter (cheap regex
scoring, threshold 1.5, plus three deterministic vetoes for list stems,
evidence summaries, and cross-references) → classify (`is_recommendation`,
`is_patient_level` — heuristic or an injected LLM, quote checked verbatim
against source) → assembly (label absorption, cross-references, dedupe).

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

### 2.3 DSL
A rule is a formula, not a label. `Rule.when` / `Rule.then` denote
`phi = when -> then`; a patient violates the rule iff `phi` is false for
them, and a Z3 counterexample is a model of `NOT phi`. `action` / `target`
are a human-readable gloss with no semantics.

Registry variables are a symbolic patient, not an index over real
patients — one solver query is one patient. `Since(event, days)` flattens a
temporal predicate to `<event>_occurred AND days_since_<event> <= days` for
Z3 (a solver has no patient timeline), while CQL keeps it as a genuine
bounded existential over the record (§2.5). Multi-visit predicates ("on two
occasions ≥90 days apart") are still out of the fragment — see §5.

`Rule.deontic` is an operator, not a label: an obligation's violation
condition is `when AND NOT then`; a permission's is the constant `false`,
so no patient can ever violate one by construction (§5).

### 2.4 Gate
Most of the refusal decision is *not* "ask a model if this is vague"
(circular, and weak even purpose-built: ~0.71 F1 on error categorisation).
The gate aggregates deterministic evidence the earlier stages already
produced, and only then asks a model to try. Only **two** of the six
refusal reasons are actually deterministic (`unbound_concept`,
`defeasible`); one (`beyond_dsl`) is mixed; the other three trace to a
model decision somewhere upstream or in the formalizer itself — see the
corrected table in [GATE.md](GATE.md) and §5.

Decisions: `encode` (every clause bound, validates, non-degenerate),
`partial` (some clauses bound, the rest declared as free-variable holes),
`refuse` (with one of six reason codes — §4).

### 2.5 CQL
The lowering from a validated `dsl.Rule` to CQL is a deterministic total
function with no judgement in it — the step a governance committee can
re-run and get byte-identical output. An unbound predicate lowers to
`null`, not `true` / `false` — CQL is natively three-valued, so the
uncertainty a stubbed boolean would hide instead propagates honestly. A
repair loop feeds compiler/validator errors back to the model, bounded,
then refuses (`beyond_dsl`) rather than looping indefinitely.

## 3. How To Use It

**Offline** (the default): every stage runs fully offline with a heuristic
or mock callable standing in for its model hook — no API key, no network.
Each stage's own test file (`test_segment.py`, `test_bind.py`,
`test_gate.py`, `test_dsl.py`, `test_cql.py`) exercises exactly this path.

**Live**: pass real callables for the injected hooks — `llm.classify_block`
(segmenter), `llm.propose_mentions` / `llm.select_candidate` (binder),
`llm.formalize` (gate) — and a populated or remote terminology index
(`SqliteIndex` / `RemoteIndex` / `CachedIndex`). [`demo.py`](../demo.py) and
[`goldset.py`](goldset.py) show the live wiring end to end.

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
  see [GATE.md](GATE.md) for which are actually deterministic (only two
  are: `unbound_concept`, `defeasible`).

**CQL-specific terms**
- library / define — a CQL module, and one named logical clause inside it.
- valueset / code declaration — a version-pinned reference to an external
  terminology set, or to a single code.
- ELM — the typed JSON IR `cql-to-elm` emits; what an execution engine
  actually consumes (CQL text itself is not executable).
- Three-valued logic — CQL's native `true` / `false` / `null`, used so an
  unbound predicate propagates honest uncertainty instead of being stubbed.

**Gold-standard labels** (from [GOLD.md](GOLD.md))
- ENCODE / PARTIAL / REFUSE / NOT_A_RECOMMENDATION — the four labels a
  human annotator assigns, blind to the tool's own output, following a
  decision order (patient-level? → nameable? → expressible? → overridable?
  → threshold defined?) down to whichever applies first.
- Deontic label — obligation / permission, labelled independently of the
  four above.

## 5. Design Decisions Worth Keeping

- **Null, not false, for an unbound predicate.** CQL is natively
  three-valued; stubbing an uncomputable node to `false` (as PathWISE does)
  silently clears patients the guideline was merely silent about — this is
  unsafe in the direction that matters. Leaving it `null` names the
  assumption instead of hiding it.
- **Canonicalize before the vacuity check.** Two names bound to the same
  terminology code (or a hole restating a bound variable) are collapsed to
  one concept before `check_nondegenerate` runs — otherwise a rule like
  `(A AND B) -> B`, where A and B are secretly identical, typechecks, is
  non-vacuous by every syntactic test, and certifies every patient by
  construction.
- **Deontic force is an operator, not a guard on a verdict string.** A
  permission's violation condition is the constant `false`, so nothing
  downstream needs to remember to skip a check afterward. Measured: once a
  rule is permissive, dropping the operator flips the verdict to an alert
  on 4 of 5 pairs — the largest effect measured in the ablation study.
- **Terminology retrieval is semantic where lexical retrieval fails.**
  Lexical (FTS/BM25) matching cannot close every gap on its own — see
  RESULTS.md R32 and its appendix for the ablation and its threats to
  validity.
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
  This corrects an earlier claim that four were free — see
  [GATE.md](GATE.md) and RESULTS.md R37.

## 6. Known Limitations

**Segmenter**
- The PDF/Docling path has never executed; the PyMuPDF path is tested only
  on a self-generated PDF.
- `load_html` has never seen real USPSTF markup.
- Recall on real documents is unmeasured by hand — several documents have
  run without crashing, but nobody has manually marked every recommendation
  in one.

**Binder**
- `propose_mentions` is 15 hardcoded regexes — it finds zero concepts
  unaided on a real ASH/ISTH sentence; it makes the pipeline runnable, not
  a design.
- `RxClass class/byName` needs exact class names, so live drug-class
  binding is unreliable until a fuller ATC seed lands.
- NLM Clinical Tables returns no units, so unit matching only works against
  the fixture or a licensed LOINC download.
- VSAC is wired in as one source, not the only one — expansions elsewhere
  are computed is-a sets, not curated value sets.

**Gate**
- Nothing is calibrated: `risk_coverage` has no real clinician labels
  behind it yet.
- `partial` doubles the model calls on statements that need it.
- Temporal vagueness still becomes a boolean hole rather than routing to
  `unsupported_temporal` or a temporal hole — known, not fixed.
- `beyond_dsl` is under-detected — only reached on a validation failure or
  a decode error, not predicted from the statement text itself.

**DSL**
- Snapshot model only: one value per variable, no patient timeline.
  Multi-visit predicates route to `unsupported_temporal` (§5).

**CQL**
- `check_cql` is structural only — brackets balance and names resolve;
  nothing has been run through `cql-to-elm` yet, so "compiles" is a much
  weaker claim than it sounds.
- The FHIR resource mapping (`lab -> Observation`, etc.) is a convention,
  not a validated model.
- `Last(... sort by effective)` is an assumption; "on two occasions" needs
  something else.
- No execution against a FHIR bundle has happened.

**Project-wide**
- No clinician has adjudicated any output; the gold labels are the
  project's own.
- Corpus is 9 open-access PMC guidelines — PMC OA over-represents journals
  that publish structured recommendation abstracts; NICE, KDIGO and
  ACC/AHA guidelines are not open access.
- Single-run numbers on a small n are not yet shown to be stable
  (RESULTS.md R17c).

## 7. Status

Live runs have happened, repeatedly, against a 9-document PMC corpus with
real model calls at every wired hook (segmenter classifier, binder
mention-proposer and selector, gate's formalizer) — not just fixtures.
What's still outstanding: `cql-to-elm` has never actually run, so no rule
has executed against a FHIR bundle; no clinician has adjudicated any
output; and the corpus's open-access sampling frame is a real, stated
limitation, not yet a representative one. Offline, the full test suite
passes with no API key, and one real run is replayable byte-for-byte from a
recorded cassette, also with no key needed.

## 8. Source Map

| Topic | Canonical source |
|---|---|
| Document segmentation | [SEGMENT.md](SEGMENT.md) |
| Terminology binding | [BIND.md](BIND.md) |
| Refusal gate | [GATE.md](GATE.md) |
| The DSL / intermediate representation | [DSL.md](DSL.md) |
| CQL lowering | [CQL.md](CQL.md) |
| Gold-standard labelling criteria | [GOLD.md](GOLD.md) |
| Engineering decisions & gotchas | [NOTES.md](NOTES.md) |
| Measured results, R1–R37 | [RESULTS.md](RESULTS.md) |

`BACKLOG.md` is intentionally excluded — it's a live issue tracker, not
documentation; read it directly for current open bugs.
