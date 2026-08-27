# The Track B Checker

Track A ([`../Part A/DSL.md`](../Part%20A/DSL.md)) turns a guideline document
into `dsl.Rule` objects: closed-vocabulary formulas, `phi = when -> then`,
validated and provenance-tagged. Track B is everything downstream of that --
given a rule, a patient, and a partial care plan, decide whether the plan
complies, and when it might not, produce a minimal, concrete witness instead
of a vague "maybe." This directory is Track B.

Track A isn't wired up yet. Until it is, [`real_rules.py`](real_rules.py)
hand-authors real guideline rules through the same DSL, so the rest of this
directory has real material to run against.

## The three verdicts

A rule is a formula, `phi = when -> then`. A *plan* is not a full patient --
it's whatever is currently known (a few lab values, a few drugs marked on or
off), leaving everything else free. Under a partial plan, "does this violate
the rule" doesn't have a yes/no answer in general -- it has three:

```
D   = domain axioms (physiological plausible ranges, dsl.domain_axioms)
P   = the partial plan, as constraints on the variables that are known
phi = when -> then, the rule's formula (dsl.rule_to_z3)

MUST-violate         if UNSAT(D and P and phi)
                      -- no completion of the unknowns can satisfy the rule
CERTIFIED-compliant  if UNSAT(D and P and NOT phi)
                      -- no completion of the unknowns can violate the rule
MAY-violate          otherwise
                      -- the model of (D and P and NOT phi) is the witness:
                         a concrete way to fill in the unknowns that violates
```

The single-call check `SAT(P and NOT phi)` is **not** a violation detector
under a partial plan: it comes back `sat` for both MUST-violate and
MAY-violate plans, and doesn't tell you which. Every script in this
directory that reports a verdict runs both `compliant` and `violating`
solver checks and reads the pair, not just one. `D` is not optional either
-- without domain axioms, a MAY-violate witness is free to set potassium to
-400 to make the arithmetic work.

## What you run

### `real_rules.py` -- the fixture library

A `dsl.Registry` of the variables and events these fixtures use (`potassium`,
`creatinine_clearance_ml_min`, `ace_inhibitor_on`, ...), and ten `dsl.Rule`
objects built against it -- ACE inhibitor + hyperkalemia, DOAC dosing by
renal function, drug-drug interaction flags, and more. Every rule is run
through `dsl.validate_rule` + `dsl.check_nondegenerate` at import time, so
importing this module raises rather than silently shipping a malformed or
vacuous rule.

`SpecClause` is a thin Track-B-shaped view over each validated rule: the
lowered Z3 predicate (`dsl.rule_to_z3`), a `var_refs` dict from variable name
to Z3 constant, and `Boundary` metadata (below) for the scripts in `tests/`
to consume. `REAL_RULES` is the list of these; `DOMAIN_AXIOMS` is the shared
list of physiological range constraints every other file in this directory
threads into its own solver calls.

**Going through the DSL instead of hand-rolled Z3 fixed three real bugs**
an earlier, pre-DSL version of this file had:

- **Action atom in `then`.** A contraindication is the contrapositive of "if
  the drug is on, the lab must be in range": `when` is the risk factor,
  `then` is what the care plan actually controls (`not on_drug`). The old
  version had it backwards -- an obligation over a lab value alone, which a
  plan can't act on, and which the DSL's `NO_ACTION_ATOM` check exists
  specifically to reject.
- **`Since` instead of a bare day-count.** Monitoring rules use
  `Since(event, days)`, which lowers to `<event>_occurred AND
  days_since_<event> <= days`. A bare `days_since_check <= 14` is satisfiable
  for a patient who was *never checked* -- the solver just picks a small
  number -- and "never checked at all" is the commonest real monitoring
  violation. `Since`'s occurrence flag makes that case reachable and
  distinct; see the `potassium_check_occurred: false` example in the UI.
- **One registry, not one variable per drug.** `potassium` and
  `creatinine_clearance_ml_min` are each a single shared variable/event (a
  patient has one kidney, not one per drug), so touching either one in
  `/check` correctly surfaces every rule that depends on it -- an
  ACE-inhibitor plan and a digoxin plan share the same
  `potassium_check_occurred` flag, because they're the same patient.

**Boundary metadata.** Six of the ten rules carry `Boundary` entries: the Z3
variable a threshold constrains, the comparison operator, the threshold
itself, a step size at the variable's realistic reporting precision, and a
`context` -- extra constraints that pin every *other* free variable so the
boundary's effect is isolated (e.g. testing the apixaban age criterion with
weight and creatinine fixed so age is the deciding vote in the 2-of-3 gate).
These are hand-derived by reasoning about each rule's compliance direction
(documented inline next to each one, e.g. the contraposition argument for
the ACE-inhibitor rule) -- they are not mechanically extracted from the
`when`/`then` tree, so a rule change that isn't mirrored in its `Boundary`
would silently drift. `tests/synthesize_boundary_tests.py` is what actually
consumes this metadata.

### `api.py` -- the Flask app

`GET /rules` lists the fixtures and the variable names each one takes.
`POST /check` takes a JSON `patient` + `plan` (merged into one set of known
values), pins whatever the request touches, and returns -- for every rule
with at least one touched variable -- the verdict above plus, for
MAY-violate, a **minimized counterexample**: the smallest subset of the
still-free values that alone forces a violation. That minimization is
assumption-dropping: take a witness model, then repeatedly drop one latent
assignment and re-check compliance-with-the-rest as a Z3 assumption list,
keeping the drop only if the remainder still can't be made compliant. It's
the same algorithm `tests/benchmark.py`'s `minimal_counterexample_core`
uses, generalized from generic drug/monitor indices to named fixture
variables.

```sh
python3 -m pip install -r ../requirements.txt
python3 api.py

curl -s localhost:8000/rules | python3 -m json.tool

curl -s -X POST localhost:8000/check -H 'Content-Type: application/json' -d '{
  "patient": {"potassium": 5.4},
  "plan": {"ace_inhibitor_on": true}
}' | python3 -m json.tool
```

### `ui.html` -- the click-through demo

Served from `GET /` by `api.py`. A dropdown of ten example patient/plan
scenarios (spanning all three verdicts, including one that pins
`potassium_check_occurred: false` to show a monitoring rule catching "never
checked at all"), two JSON textareas to paste your own, a Check button, and
a reference table of every rule's variable names pulled live from
`GET /rules`. Each result renders as a color-coded verdict badge and a
plain-English sentence -- for MAY-violate, the counterexample translated
into prose ("It becomes a certain violation if digoxin dose reduced =
false") instead of raw JSON, though the raw JSON is one click away behind a
`<details>` toggle. Single static file, no build step, no dependency beyond
the Flask app that serves it.

```sh
python3 api.py
# open http://localhost:8000/
```

## `tests/` -- checks against the checker, not needed to run it

Nothing in `tests/` is imported by `api.py` or `ui.html`. Each script here
verifies a different property of the checker above, and each writes its
results to a CSV alongside itself. Every script that imports `real_rules`
adds the parent directory to `sys.path` explicitly, so `python3
tests/whatever.py` works the same regardless of your working directory.

**`partial_spec_check.py`** -- the original minimal reproduction: one
hand-written rule (`metformin_on -> eGFR >= 30`), 20 random partial plans,
naive vs. three-valued classification counts. No dependency on anything
else in this directory; the whole checker fits in one screen here.

**`benchmark.py`** -- the larger reproducible benchmark: 200 synthetic
patients, 25 bounded-horizon clauses over generic `drug_N`/`lab_N` names
(not `real_rules.py`'s fixtures), partial plans covering 14 of 30 candidate
actions. Reports `naive_false_violation_rate` (the naive single-call check's
over-triggering rate against the three-valued classification) alongside
solve times and counterexample core sizes, at a scale meant to stress-test
solve performance rather than to demonstrate any one clinical rule.

**`synthesize_boundary_tests.py`** -- coverage-directed test generation: for
every `Boundary` declared on a `real_rules.py` fixture, solves -- with Z3,
not a hand-picked test table -- for a concrete patient bundle on each side of
the threshold (e.g. potassium = 5.0 vs 5.1, a 14-day vs 15-day monitoring
gap). The boundary arithmetic (threshold ± step) is the only hand-picked
part; every other free variable in the bundle, and the resulting verdict,
comes out of the solver. Where a boundary lands on MAY-violate, it also
produces a second bundle differing only in the one free variable that
separates a compliant completion from a violating one. This is the shared
generator the next two scripts both consume.

**`detection_benchmark.py`** -- does the checker actually catch violations?
`benchmark.py`'s `naive_false_violation_rate` only shows the checker doesn't
*over*-flag on random plans; every clause there is random, so there's no
ground truth for what *should* be flagged. This takes every bundle
`synthesize_boundary_tests.py` built (each one constructed to be either
compliant or violating by solving against `predicate` or `Not(predicate)`),
fully pins every variable, and re-derives the verdict independently. Reports
detection recall (does MUST-violate fire on every bundle built to violate)
and false-positive rate (does CERTIFIED-compliant fire on every bundle built
to comply), plus any individual mismatch.

**`differential_check.py`** -- a cheap stand-in for dual-LLM encoding. For
three fixtures, hand-writes a second, independently-worded Z3 predicate from
an alternate reading of the same guideline sentence -- the same kind of
inclusive/exclusive boundary-wording ambiguity ("above" vs "at or above",
"below" vs "or less") a second person, or a second model, might resolve
differently. Both predicates are evaluated on every bundle
`synthesize_boundary_tests.py` solved for; any bundle where they disagree is
printed with both source sentences and the bundle's values, for a human to
inspect. Boundary bundles are the highest-yield place to run this, since an
inclusivity slip shows up exactly at the threshold value -- which is exactly
where these bundles sit.

## Open questions

- **Track A isn't connected.** `real_rules.py` is a stand-in for what a
  document-extraction pipeline would eventually emit. Its `source_page` is a
  nominal placeholder, not a real citation, and its `source_sentence` fields
  are paraphrases, not verbatim extracted text.
- **`Boundary` metadata can drift.** It's derived by hand-reasoning about
  each rule's compliance direction, not read mechanically off the `when`/
  `then` tree, so nothing currently catches a rule edit that isn't mirrored
  in its boundaries.
- **The differential check covers 3 of 10 rules,** by hand. It's a proof of
  concept for what a real dual-encoding pass would automate, not a
  systematic sweep.
