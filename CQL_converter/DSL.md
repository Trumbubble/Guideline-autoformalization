# The GuidelineForge DSL

The typed intermediate representation between Track A (document → rules) and
Track B (rules → evidence). One file, [`dsl.py`](dsl.py); tests in
[`test_dsl.py`](test_dsl.py). Only dependency is `z3-solver`, and only for the
lowering and degeneracy checks — `validate()` is pure Python.

```
python3 tests/test_dsl.py      # 23 tests, no pytest needed
```

## Explanation of logic idea 

**A rule is a formula, not a label.** A `Rule` carries `when` (applicability)
and `then` (obligation), both expressions over the same closed registry, and
denotes

```
phi  =  when -> then
```

Everything downstream operates on `phi`: a patient violates the rule iff
`phi` is false for them, and a counterexample is a model of `NOT phi`. The
`action` and `target` fields are a human-readable gloss for the UI and for
governance review — **they carry no semantics**. If you find yourself
branching on `rule.action` in verification code, something has gone wrong.

## Mental model: what is indexed by what

Three different things get confused here, and only one of them is an index.

### The patient is not an index — the patient is the *model*

The registry variables are already a **symbolic patient**. `egfr` is not "some
particular person's eGFR", it is a free variable standing for *any* patient's
eGFR. When Z3 returns `sat`, the model it hands back **is** a concrete patient:

```
egfr = 29, acr_mg_g = 310, has_diabetes = True, on_acei_arb = False
   ^ this assignment is the patient
```

So you never write `egfr[patient]`. You would only need a patient index if some
rule related two patients to each other, and no clinical guideline does. Because
there are no cross-patient constraints, a formula over all patients decomposes
into independent per-patient formulas: solving them jointly gives identical
answers, more slowly, and returns a counterexample assigning values to everyone
at once instead of naming one patient.

**One solver query = one patient.** Many patients is a loop, or many models of the same formula.

### Statements are formulas, not an index

Each guideline statement compiles to its own formula over the **same** shared
env. The shape of a run is:

- **one env** per patient, built from the registry
- **m formulas** `phi_1 .. phi_m`, one per statement
- each query asserts the domain axioms, the care plan, and whichever `phi` is
  under test

`rule_id` is a dictionary key. It says which clause fired and which source
sentence to show the clinician — it is provenance, not a logical index. You
never write `phi[i]` in the logic; you look it up.

### Time is the only thing that would be an index

A single patient has many visits, and that *is* inside the logic: `egfr` at
visit 3 and `egfr` at visit 7 are different quantities a rule may need to
relate. Today the DSL is a **snapshot** — one value per variable, `Since`
flattened to derived state (see *Time*), so there is no index at all. Under
time-indexing, temporal variables would become `egfr[0..T]` while static ones
(sex, genotype) stay single constants. That distinction is a correctness
matter, not an optimisation: without it the solver is free to return a
counterexample in which the patient's sex changes at visit 7.

Note that a statement then gets *unrolled* over time at lowering. "Check
potassium within 14 days of starting an ACEi" really means

```
forall t.  start_acei[t]  ->  exists t' in [t, t+14].  potassium_checked[t']
```

and bounded unrolling emits one copy of that body per `t`. The statement is
still written **once**, with a temporal operator; the T-fold expansion happens
inside `to_z3`. That is the whole reason to have the operator rather than a
naming convention — the expansion is generated in one place instead of being
hand-written by the model. See *Open decision*.

### One verification run

```
env = make_env(registry)              # the symbolic patient
D   = domain_axioms(registry, env)    # physiology, well-formedness, frame axioms
P   = the care plan, as constraints on env

for each statement phi_i:
    MUST-violate         if UNSAT(D and P and phi_i)
    CERTIFIED-compliant  if UNSAT(D and P and NOT phi_i)
    MAY-violate          otherwise -- the model of (D and P and NOT phi_i)
                                      is the witness
```

## Quickstart

```python
import z3
from dsl import (CKD_REGISTRY, And, Cmp, Rule, Since, validate_rule,
                 check_nondegenerate, make_env, domain_axioms, rule_to_z3)

R = CKD_REGISTRY

rule = Rule(
    rule_id="kdigo-3.1",
    when=And([Cmp("has_diabetes", "==", True), Cmp("acr_mg_g", ">=", 30)]),
    then=Cmp("on_acei_arb", "==", True),
    action="recommend", target="ACEi or ARB",
    source_page=12, source_span="Treat with an ACEi or ARB ...")

assert validate_rule(rule, R) == []        # types, provenance, action atom
assert check_nondegenerate(rule, R) == []  # fires for someone, forbids something

env = make_env(R)
s = z3.Solver()
s.add(domain_axioms(R, env))               # physiological ranges + well-formedness
s.add(z3.Not(rule_to_z3(rule, env)))       # find a violating patient
print(s.check(), s.model())
```

## The registry

The registry is the **closed vocabulary**. Generation is constrained to these
names, so an unknown variable is a hard error rather than a new concept.

```python
from dsl import Registry, VarDef, EventDef

R = Registry([
    VarDef("egfr", "real", "mL/min/1.73m2", "estimated GFR",
           binding="LOINC:33914-3", plausible=(0, 200)),
    VarDef("on_acei_arb", "bool", None, "prescribed an ACEi or ARB",
           role="action"),
], events=[
    EventDef("potassium_check", "serum potassium measured", "LOINC:2823-3"),
])
```

**`type`** — `real`, `int`, `bool`, `code`, `date`.

| type | legal ops | DSL literal | lowers to |
|---|---|---|---|
| `real` | all six | number | `z3.Real` |
| `int` | all six | whole number | `z3.Int` |
| `bool` | `==` `!=` | `true` / `false` | `z3.Bool` |
| `code` | `==` `!=` | code string | `z3.String` |
| `date` | all six | ISO-8601 `"2024-01-31"` | `z3.Int` (ordinal) |

**`role`** — `observation` (patient state) or `action` (something a care plan
decides: a drug is on, a referral is placed). Two consequences: `validate_rule`
rejects an obligation that mentions no action atom, and Track B reads
`reg.actions()` to write frame axioms closing the world over actions the plan
never mentions.

**`binding`** — the terminology code, resolved by **retrieval over a local
index, never generated**. It lives on the variable, so the variable itself
stays a plain `bool`; `has_diabetes` bound to SNOMED is the pattern, not a
`code`-typed variable holding a diagnosis.

**`plausible`** — the physiological range, and it earns its keep twice. The
validator warns when a threshold falls outside it (the mg/g-vs-mg/mmol unit
slip is a factor of ~8.84 and produces a perfectly typed, silently wrong
rule), and `domain_axioms()` asserts it so the solver stops handing back
counterexamples at eGFR = 2.0 that no clinician will accept. **Set it on every
numeric variable.**

**`codes`** — for `code`-typed variables, the retrieved value set. A code
outside it is `UNBOUND_CODE`, and `domain_axioms()` constrains the variable to
the set — which turns "zero hallucinated codes" into a checkable property
rather than a claim.

**`events`** — datable clinical acts, referred to by `Since`. See *Time* below.

## Expression nodes

| node | means | notes |
|---|---|---|
| `Cmp(var, op, value)` | `var op value` | `op` ∈ `< <= > >= == !=` |
| `Between(var, lo, hi, lo_inclusive, hi_inclusive)` | interval | inclusivity is **explicit and required** |
| `Since(event, days, inclusive=True)` | event occurred in the last `days` days | see *Time* |
| `Not(e)` / `And([...])` / `Or([...])` | boolean structure | junctions need ≥ 2 operands |

`Between` is derivable from `And` of two `Cmp`s and is kept anyway, because it
**forces the open/closed decision to be made**. Inclusivity slips (eGFR 29 vs
30, a four- vs five-day overlap) compile cleanly and are the single most
common silent boundary bug; a node that cannot be written without answering
the question is worth the redundancy.

`And`/`Or` are first-class because an expression language without disjunction
does not prevent disjunctions — it converts them into conjunctions. In the P3
run, one AND-for-OR substitution gave the wrong referral verdict for 69.5% of
the plausible patient box, and it typechecked.

## Time

`Since(event, days)` lowers to

```
<event>_occurred  AND  days_since_<event> <= days
```

over a single visit. The occurrence flag is load-bearing: without it, a bare
`days_since_potassium_check <= 14` is satisfiable for a patient who was
**never checked** — the solver simply picks 3 — and "never checked at all" is
the commonest real monitoring violation.

Two properties this buys, both of which a naming convention (`days_since_*` as
ordinary registry variables) would lose:

- **The flattening is contained.** `days_since_*` is produced by this lowering,
  not hand-written by the model, so there is exactly one place it can be wrong.
- **The flattening is visible.** `fragment_of` reports `MTL` for any rule
  containing a `Since`, so the formalizability spectrum does not silently
  reclassify monitoring rules as plain linear arithmetic.

**What is not expressible.** Anything needing more than one timepoint:
*"eGFR < 60 on two occasions at least 90 days apart"* (the actual definition
of CKD), *"sustained for three months"*, *"after an adequate trial"*. These
must be refused with `unsupported_temporal`. See *Open decision* at the end.

## Rules and refusals

```python
Rule(rule_id, when, then, action, target,
     strength=None, source_page=None, source_span=None)

Refusal(rule_id, reason_code, reason_text,
        source_page=None, source_span=None, confidence=None)
```

Refusal is a **first-class output, not a failure**. The JSON schema is
`oneOf: [rule, refusal]`, so a single structured decode forces the model to
choose one or the other rather than approximating a statement it cannot encode.

`reason_code` ∈ `vague_predicate`, `defeasible`, `not_patient_level`,
`unsupported_temporal`, `unbound_concept`, `beyond_dsl`. `confidence` is the
refusal gate's score, needed for the risk-coverage curve.

Provenance (`source_page` + `source_span`) is **mandatory on rules** and
missing provenance is a hard error. It is the unit of clinician adjudication —
a clinician will not read SMT-LIB, but will confirm or reject "this clause came
from *this* sentence".

## The pipeline

```
validate_rule(rule, reg)        -> [Issue]   pure Python; types, provenance, action atom
check_nondegenerate(rule, reg)  -> [Issue]   needs z3; vacuity sweep
rule_to_z3(rule, env)           -> BoolRef   phi = when -> then
```

Feed `Issue.hint` verbatim back into the repair loop. Bound the repair
iterations and fall through to a `Refusal` rather than looping.

`errors(issues)` filters to blocking issues; warnings are advisory and should
surface in the report, not stop compilation.

### Issue codes

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
| `IMPLAUSIBLE_THRESHOLD` | **warn** | threshold outside physiological range — usually a unit slip |
| `EXACT_REAL_COMPARISON` | **warn** | `==` against a real threshold; guideline thresholds are inequalities |
| `UNSATISFIABLE_ANTECEDENT` | error | the rule can never fire — often an OR rendered as an AND |
| `UNSATISFIABLE_OBLIGATION` | error | every applicable patient violates by construction |
| `VACUOUS_RULE` | error | `NOT(when -> then)` is unsat: the rule forbids nothing |
| `TAUTOLOGICAL_ANTECEDENT` | **warn** | the rule applies to everyone |

`VACUOUS_RULE` is the check that a compiler cannot do for you. A spec no
patient can violate passes every syntactic gate and certifies nothing.

## Fragments

`fragment_of(*exprs) -> "PROP" | "LIA" | "MTL"` — the weakest fragment
containing the expressions, computed from the tree rather than inferred from
variable names. Booleans and code equality are `PROP`; any numeric or ordered
comparison is `LIA`; any `Since` is `MTL`.

Together with the `Refusal.reason_code` taxonomy this gives the full
six-bucket spectrum (PROP / LIA / MTL / defeasible / irreducibly vague /
non-patient-level) as a deterministic function of the output, so the headline
formalizability number falls out of the code instead of out of an annotator.

## JSON

```
to_dict(rule_or_refusal) -> dict      from_dict(d) -> Rule | Refusal
dumps([...]) -> str                   loads(s) -> [Rule | Refusal]
expr_to_dict(e) / expr_from_dict(d)
json_schema(reg) -> dict              # hand this to structured decoding
```

`json_schema(reg)` closes the variable and event enums by construction, so the
decoder cannot emit a name outside the registry. Note it requires
`lo_inclusive`/`hi_inclusive` on `between` — the decoder is not allowed to
default them.

Round-tripping is tested: `from_dict(to_dict(r)) == r`.

## Using it from Track B

```python
env = make_env(reg)                  # name -> z3 constant
D   = domain_axioms(reg, env)        # assert these in EVERY query
phi = rule_to_z3(rule, env)
```

`make_env` also creates, per registered event, `<event>_occurred` (`Bool`) and
`days_since_<event>` (`Int`, bounded by `HORIZON_DAYS`). Use the helpers
`occurred_var(e)` / `days_since_var(e)` rather than formatting the names
yourself.

Three-valued verdict against a partial plan `P`:

| verdict | query |
|---|---|
| MUST-violate | `UNSAT(D ∧ P ∧ phi)` |
| CERTIFIED-compliant | `UNSAT(D ∧ P ∧ ¬phi)` |
| MAY-violate | otherwise; the model of `D ∧ P ∧ ¬phi` is the witness |

The single-call check `SAT(P ∧ ¬phi)` is **not** a violation detector under a
partial plan, and `D` is not optional — without the domain axioms the witnesses
are adversarial completions of unconstrained latent state.

## Open decision

The model is a **snapshot**: one value per variable, one verdict about now. A
counterexample is therefore a *patient* ("eGFR 29, ACR 310, not referred"), not
a *trace* ("eGFR was 55 at visit 3 and 28 at visit 7, and no referral was placed
by visit 9").

Moving to time-indexed variables (`var[t]` over a horizon `T`) is what buys
traces and multi-visit predicates. It is a strictly additive change on this
side — `Since` acquires a new lowering and no already-written rule changes —
but it needs the registry to mark which variables are time-varying, new
operators for the multi-visit predicates, and a bundle format from Track B that
is a timeline rather than a row. Until that is decided, multi-visit statements
route to `unsupported_temporal`.

Under time-indexing `days_since_<event>` disappears: it stops being a quantity
anyone supplies and becomes derivable from the timeline, and `Since` becomes a
genuine bounded existential rather than a flattened comparison. That is what
makes the change additive — the rules you write today do not change, only what
`to_z3` does with them.

The choice also moves the headline formalizability number, so it is a
measurement decision rather than a feature toggle: *"eGFR < 60 on two occasions
at least 90 days apart"* is the definition of CKD, and under a snapshot model it
lands in the residue. Whichever way it goes, the paper has to say so explicitly
— otherwise a reviewer discovers that CKD's own definition is out of fragment
and concludes it was hidden rather than measured.

## Canonical form: when two names are one concept

A **generated** registry can contain the same concept twice; a hand-authored
one cannot, because a person does not write two names for one thing. This
matters more than it sounds: on a real guideline the formalizer wrote both
`pca` and `patient_controlled_intravenous_analgesia` — both bound to SNOMED
`1351574007` — into one rule, producing

```
(has_ap AND pca) -> patient_controlled_intravenous_analgesia
```

which denotes `(A AND B) -> B`. That is valid under every interpretation, so
the rule **certifies every patient** and can never fail. It is not a crash and
does not look like a bug; it looks like a rule that works.

```python
dsl.canonical_aliases(reg)      # {alias -> canonical}, on ground truth only
dsl.canonicalize(reg, rules)    # -> (reduced registry, rewritten rules, alias map)
```

Two signals are trusted to merge concepts, and only two:

| Signal | Why it is ground truth |
|---|---|
| Same `system\|code\|version` | The terminology server said they are one concept |
| A hole restating a bound variable (`has_X` / `X`) | A hole with a real binding available is a duplicate, not residue |

Similarity judgements are **not** used. Asked whether "acute pancreatitis" and
"severe acute pancreatitis" are the same, a model says yes, and that
distinction is the entire content of some recommendations.

`check_nondegenerate` canonicalises internally before it calls the solver, so
`VACUOUS_RULE` fires whether or not the caller canonicalised. `bundle_to_dict`
canonicalises on write and records what it collapsed under `merged_concepts` —
a merge is a claim about the guideline, so it is reported, never silent.

## Modal predicates: strength is not a patient fact

A recommendation carries two separable things: **facts about the patient**, and
**how hard the guideline is pushing**. The second is a property of the
document, identical for every patient, and `Rule.strength` is its slot.

When the formalizer read *"epidural analgesia may be considered"* it filled
`strength` correctly with `"suggest (conditional)"` — and *also* put the modal
into the antecedent as `has_may`. That rule cannot run: no FHIR resource
populates "has may", so it is UNKNOWN for every patient forever.

```python
dsl.modal_only("has_may")                    # True   -- deontic force
dsl.modal_only("has_appropriate_acupoints")  # False  -- vague, but clinical
```

`validate_rule` reports `MODAL_PREDICATE` as an **error**, and the gate refuses
the statement as `defeasible`. A fixed word list is the right tool here because
English modals are a closed class — about a dozen, with no new ones being
coined. (Abbreviations are open-ended, which is why those get a per-document
pass instead.) `modal_only` requires *every* token to be modal, so a hedged but
genuinely clinical predicate still becomes a hole.

## Deontic force: obligation vs permission

`Rule.deontic` is an operator, not a label.

```
obligation   O(then | when)    violation = when AND NOT then
permission   P(then | when)    violation = FALSE, unconditionally
```

*"We recommend anticoagulant prophylaxis"* is an obligation: a patient meeting
`when` without `then` is a real deviation. *"Epidural analgesia may be
considered"* is a permission: the same patient has violated nothing.

**GRADE conditional is not permission.** "We suggest X" still recommends the
action — the weakness is in the evidence, not the obligation. Only explicit
permission language counts. Treating every weak recommendation as optional
would silence most of a guideline.

Prohibition is not a third case: `F(p) = O(NOT p)`, written as
`action="avoid"` with a negated consequent.

```python
dsl.deontic_of(rule)                    # the operator, with a wording backstop
dsl.violation_condition(rule, env)      # what Track B negates
dsl.conflicts(rules, reg)               # permits what another rule forbids
```

`violation_condition` is the interface that matters. `rule_to_z3` still gives
`when -> then`, which describes the *content* of a permission, so degeneracy
checking still applies to it; what differs is what counts as a violation.

`deontic_of` prefers the explicit field but promotes obligation → permission
when the source says "may be considered". It never demotes — guessing
permission wrongly silences an alert, guessing obligation wrongly accuses a
clinician.

Track B dispatches on the operator, and a permission gets its own verdict
lattice: `OPTION-taken`, `OPTION-not-taken`, `OPTION-not-applicable`,
`OPTION-available`. None of them is an accusation.
