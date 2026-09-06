# The refusal gate

Stage 3 of Track A: per statement, decide **encode / partial / refuse**, with a
reason and a score. One file, [`gate.py`](gate.py); tests in
[`test_gate.py`](test_gate.py); measured results in [`RESULTS.md`](RESULTS.md).

```
python3 tests/test_gate.py     # 19 tests, offline, no key, no network
```

## The one idea

It is **not** an LLM asked *"is this vague?"*. That is circular, and
arXiv:2607.04655 puts even purpose-built diagnostic models at ~0.71 F1 on error
categorisation. It is also unnecessary: **most of the decision is already made
by machinery stages 1–2 produce.** The gate aggregates that evidence, and only
then asks a model to try.

The cost is asymmetric, and everything follows from it:

> A false **refuse** costs coverage and appears honestly in the residue report.
> A false **encode** produces a silently wrong checker.

So the gate is thresholded for precision on `encode`, and whatever recall that
buys is what it buys.

## Quickstart

```python
import gate, bind, llm

ix = bind.fixture_index()                       # or a live CachedIndex
t = gate.triage(statement, ix, statement_id="s1",
                mention_llm=llm.propose_mentions,
                select_llm=llm.select_candidate,
                formalize=lambda text, reg: llm.formalize(text, reg),
                samples=1,                       # 8 when building the E3 curve
                is_patient_level=True,           # from the segmenter
                source_page=12, source_span=statement)

print(t.decision, t.reason_code, t.fragment, t.confidence, t.open_holes)
print(gate.spectrum([t]))                        # the M1 six-bucket table
```

Every model hook is injected, so the whole gate runs offline on heuristics —
which is how all 19 tests execute with no key.

## The six refusal reasons

The `reason_code` values are `dsl.Refusal`'s, so a refusal from the gate is the
same object the residue report and the DSL already understand.

| code | means | example | caught by |
|---|---|---|---|
| `unbound_concept` | a clinical concept resolves to no code | *"frailty"*, *"high-risk patients"* | **deterministic** — the binder: selector returns -1, no candidate survives, or a value set expands to no codes |
| `defeasible` | exceptions or discretion the logic cannot carry | *"unless clinically indicated"*, *"at the discretion of the MDT"* | **deterministic** — `MODAL_PREDICATE` among the blocking codes; the model may also name it |
| `beyond_dsl` | needs structure the DSL lacks | arithmetic, counting, nesting past the decoder's depth | **mixed** — `VACUOUS_RULE` deterministically, otherwise the model's own code or the fallback |
| `not_patient_level` | about a service, commissioner or research agenda, not a patient | *"Commissioners should ensure rapid-access clinics are available"* | **model** — the segmenter's classifier (`is_patient_level`), an LLM call on a live run; never derived inside the gate itself |
| `vague_predicate` | a word standing in for a number the guideline never gives | *"an adequate trial"*, *"significant impairment"* | **model** |
| `unsupported_temporal` | needs more than one timepoint | *"eGFR < 60 on two occasions ≥ 90 days apart"* — the definition of CKD | **model** — no deterministic check exists; the formalizer just declines. Seen twice across the real bundles, both model-authored |

**Only two of the six are deterministic** — `unbound_concept` (the binder) and
`defeasible` (the validator's `MODAL_PREDICATE` check); `beyond_dsl` is mixed.
The other three need the model, either to decline directly or — for
`not_patient_level` — to have classified the statement upstream. The
deterministic machinery is a gate *on* the model's answer, not a replacement
for it.

*(This corrects an earlier version of this table and its "four of the six are
free" claim, which was wrong the same way a stale docstring in `gate.py`
itself was wrong — see RESULTS.md R37. `test_gate.py` now pins which codes are
reachable with `formalize=None`, so the claim cannot drift again silently.)*

One deliberate non-inference: an empty binding is **never** labelled
`not_patient_level`. *"Consider referral after an adequate trial"* binds
nothing and **is** patient-level — it is vague. Distinguishing the two is the
segmenter's job, passed in as `is_patient_level`; without it the gate reports
`unbound_concept` and says so (`NO_BOUND_VOCABULARY`).

## The three decisions

```
encode   every clause bound, the encoding validates and is non-degenerate
partial  some clauses bound; the rest are declared FREE VARIABLES
refuse   no faithful encoding, with a reason code
```

`Triage.encodable` is `encode or partial` — **the binary the gate is calibrated
on**, and deliberately independent of `fragment`. Two labelling passes agreed
28/29 on this binary but only 71.8% on the exact fragment, so staking the
headline number on the fragment invites M1's falsifier.

## `partial` — the verdict that beats the baseline

*"Start an ACE inhibitor when eGFR falls below 30 **or the patient is at high
risk**."* One clause binds; one does not.

PathWISE stubs an uncomputable node as `false`. That is **unsafe in the
direction that matters**. Measured, on a patient with eGFR 45 not on an ACEi:

| encoding | verdict |
|---|---|
| **hole left free (ours)** | **MAY-violate** — witness names `has_at_high_risk = True` |
| stub `false` (PathWISE baseline) | **CERTIFIED-compliant** — silently clears the patient |
| stub `true` | MUST-violate — over-fires |

Stubbing `false` certifies a patient as compliant where the guideline is merely
silent, and nothing downstream can see it. Leaving the hole free makes the
verdict three-valued and **names the assumption the guideline never defined**.
That is E3's comparison, and it is asserted in `test_gate.py`.

### How a hole is made

`VarDef(unbound=True)`: no binding, no data source, and — importantly — **no
domain axiom**, since constraining a hole would be a claim the guideline never
made. `dsl.open_holes(rule, reg)` reports which ones a rule mentions;
`validate_rule` flags each as an `OPEN_HOLE` **warning**, not an error.

### Where holes come from, and where they must not

Two sources, and the distinction is load-bearing:

- **the model names it** — `Refusal.undefined_predicates`, copied **verbatim**
  from the statement and substring-checked, the same anti-synthesis rule the
  segmenter and binder use. A paraphrase is rejected.
- **a vague binder refusal** — the unbound phrase matches the vague lexicon.

**A retrieval failure is never a hole.** *"refer to nephrology"* is perfectly
bindable; if the index misses it that is a coverage gap to fix. Turning it into
a free variable would launder an index bug into a finding. The first version of
this file did exactly that, and a test now prevents it.

## The `k` samples, and where confidence comes from

`samples=k` asks the model the same question k times.

**What it was for.** VERIMED's idea (arXiv:2605.13817): a vague statement
admits several readings, so k samples should *disagree*, and disagreement is a
vagueness detector needing no ground truth.

**What actually happens.** Measured on 20 statements × K=8: the model does not
give 8 different encodings for a vague statement — it gives **8 refusals**.
All 80 vague samples were refused, and **0** statements produced more than one
semantic reading. There is nothing to disagree about.

**So k is now a counter.** `confidence = 1 − (refusals / k)`:

```
8/8 refused  → 0.00   almost certainly not encodable
6/8 refused  → 0.25   borderline
0/8 refused  → 1.00   almost certainly encodable
```

Measured on deliberately borderline statements, the score **is graded**, not
just bimodal — *"Monitor serum potassium at least annually"* came back 6/8.
That is what makes a risk–coverage curve meaningful rather than a step function.

**Confidence describes the attempt that decided it.** A statement refused 8/8
under the strict vocabulary but encoded cleanly once its hole was declared is
not low-confidence; reporting the strict rate would sort a good `partial` to
the bottom of the curve. Fixed, and tested.

**You only need k > 1 for the evaluation set.** In production `samples=1` — one
call, binary answer, an eighth of the cost. Hence the default.

## Reporting

**`spectrum(triages)`** — the M1 six-bucket formalizability table, computed from
the outputs rather than annotated by hand: `PROP` / `LIA` / `MTL` for what
encoded (via `fragment_of`), and the refusal reason codes for what did not.
Also reports `in_fragment_rate` and the total count of open holes.

**`risk_coverage(triages, truth)`** — E3's curve. Sort by confidence, sweep the
threshold, report precision on `encode` against clinician labels at each
coverage level. `truth[statement_id]` is the human judgement. The operating
point is chosen for **precision**, because a wrongly-accepted statement is the
failure the whole tool argues against.

## Auditability

`Triage.evidence` is a tuple of `Evidence(source, code, detail)` — every signal
that fired, tagged `binder` / `validator` / `solver` / `model`. A clinician
asking *"why was this refused"* gets those, not a score. Same discipline as
`prefilter_features` and `Binding.candidates`.

## Limits

- **Nothing is calibrated.** `risk_coverage` is implemented and untested against
  real labels; E3 needs two clinicians on a held-out guideline. Until then the
  confidence score is a number, not a calibrated probability.
- **It has now run end-to-end on real guidelines, repeatedly** — R12 was the
  first full run (14 statements, one document); by R20 the corpus is 9
  open-access PMC guidelines, 45 gold-labelled statements. What is *not* yet
  true: no clinician has adjudicated any output — the gold labels are the
  project's own — and single-run numbers on a small n are not yet shown to be
  stable (RESULTS.md R17c).
- **`partial` doubles the model calls** on statements that need it (a strict
  attempt, then a second with holes declared). Measured: $0.47 for 3 statements
  at k=8.
- **Temporal vagueness becomes a boolean hole.** *"at least annually"* produced
  `has_at_least_annually`, which is a poor encoding — it should route to
  `unsupported_temporal` or to a temporal hole. Known, not fixed.
- **`beyond_dsl` is under-detected.** It is only reached when validation fails
  or a decode errors, not predicted from the statement.

## Modal predicates are refused, not turned into holes

`holes_from` will not mint a hole from a phrase that reduces to a modal.
*"may be considered"* becomes `has_may`, which is the guideline's own deontic
force wearing a patient predicate's clothes.

Declaring it as a hole is **worse than refusing**. A hole is a promise that
some data source could one day fill it — that is what makes a PARTIAL encoding
meaningful, and what a governance reader relies on when deciding whether the
gap is closable. Nothing will ever populate "has may". The statement is
discretionary, and discretionary is a refusal.

The guard drops the modal without swallowing the real predicate: given
`["may be considered", "at high risk"]`, only `has_high_risk` survives.

## The validator's diagnosis now sets the refusal reason

When the model returns a rule and the validator rejects it, the reason code
comes from the defect rather than from the fallback:

| Defect | Reason code | Why |
|---|---|---|
| `MODAL_PREDICATE` | `defeasible` | The guideline hedged; the statement is discretionary |
| `VACUOUS_RULE` | `beyond_dsl` | No non-vacuous obligation could be extracted |

Previously both fell through to `unbound_concept` (if anything failed to bind)
or `beyond_dsl`, which told a governance reader to go looking for a missing
code when the real problem was that the sentence had no checkable content.
