# Guideline-autoformalization

## Synthetic verifier benchmark

The original minimal reproduction remains in `partial_spec_check.py`. The
larger reproducible benchmark is in `benchmark.py`:

```sh
python3 -m pip install -r requirements.txt
python3 benchmark.py
```

It generates 200 synthetic patients, 25 bounded-horizon clauses, and partial
plans covering 14 of 30 candidate actions. Per-check results are written to
`benchmark_results.csv`; aggregate rates, classifications, minimized latent
counterexample sizes, and solve times are printed as a CSV-style table.

For a MAY result, `D and P and not phi` is satisfiable, so a literal UNSAT/MUS
core cannot exist. The benchmark reports a minimized set of latent action
assignments that makes `phi` unsatisfiable, which is the corresponding
counterexample core for a MAY-violate result.

## Real rule fixtures (Track B)

`real_rules.py` hand-authors ten guideline rules beyond the metformin/eGFR
example in `partial_spec_check.py` -- ACE inhibitor + potassium monitoring,
DOAC dosing by renal function, drug-drug interaction flags, and more --
expressed as `dsl.Rule` objects against a shared `Registry`, per
[`../Part A/DSL.md`](../Part%20A/DSL.md). Each rule is validated with
`dsl.validate_rule` + `dsl.check_nondegenerate` at import time; the module
raises rather than shipping an unvalidated or vacuous rule. Going through
the DSL instead of hand-rolled Z3 fixed three real bugs the earlier version
had:

- **Action atom in `then`.** A contraindication is the contrapositive of
  "if the drug is on, the lab must be in range": `when` is the risk factor,
  `then` is what the care plan controls (`not on_drug`). The old version had
  it backwards -- an obligation over a lab value alone, which a plan can't
  act on and the DSL's `NO_ACTION_ATOM` check would reject.
- **`Since` instead of a bare day-count.** Monitoring rules now use
  `Since(event, days)`, which lowers to `<event>_occurred AND
  days_since_<event> <= days`. The old hand-rolled version had no occurrence
  flag, so "never checked at all" -- the commonest real monitoring
  violation -- wasn't reachable as a distinct case. It now is: see the
  `potassium_check_occurred: false` example in the UI.
- **One registry, not one variable per drug.** `potassium` and
  `creatinine_clearance_ml_min` are now each a single shared variable/event
  (a patient has one kidney, not one per drug), so touching either one in
  `/check` correctly surfaces every rule that depends on it -- e.g. a
  digoxin plan and an ACE-inhibitor plan share the same
  `potassium_check_occurred` flag.

`DOMAIN_AXIOMS` (physiological plausible ranges, from `dsl.domain_axioms`)
is threaded through every downstream file in this directory, so a
MAY-violate witness can no longer use potassium = -400.

```sh
python3 real_rules.py
```

## Coverage-directed boundary test synthesis

`synthesize_boundary_tests.py` walks every `Boundary` in `real_rules.py` and
asks Z3 -- not a hand-picked test table -- for a concrete patient bundle on
each side of the threshold (e.g. potassium=5.0 vs 5.1, a 14-day vs 15-day
monitoring gap). Each generated bundle is run through the same
MUST-violate/MAY-violate/CERTIFIED-compliant check `benchmark.py` uses on a
full plan, so every synthesized case comes with an expected verdict already
attached. Where a boundary lands on MAY-violate, the solver also produces a
second bundle differing only in the one free variable (e.g. whether the dose
was actually reduced) that separates a compliant completion from a violating
one. Results are written to `synthesized_cases.csv`:

```sh
python3 synthesize_boundary_tests.py
```

## Detection benchmark

`benchmark.py`'s `naive_false_violation_rate` already shows the checker
doesn't over-flag on fully random synthetic plans. It doesn't show the
checker catches real violations -- every clause in that benchmark is random,
so there's no ground truth for what *should* be flagged. `detection_benchmark.py`
closes that gap: it takes every bundle `synthesize_boundary_tests.py` built
(each one constructed to be either compliant or violating by solving against
`predicate` or `Not(predicate)`), fully pins every variable, and re-runs the
same three-valued check independently. It reports detection recall (does
MUST-violate fire on every bundle built to violate) and false-positive rate
(does CERTIFIED-compliant fire on every bundle built to comply), plus any
individual mismatches. Results are written to `detection_benchmark_results.csv`:

```sh
python3 detection_benchmark.py
```

## Differential check (dual-encoding stand-in)

`differential_check.py` hand-writes a second, independently-worded Z3
encoding for three fixtures -- the same kind of boundary-wording ambiguity
("above" vs "at or above", "below" vs "or less") a second person, or a
second LLM, might resolve differently -- referencing the same variables as
the primary encoding. Both predicates are evaluated on every fully-pinned
bundle from `synthesize_boundary_tests.py`; any bundle where they disagree
is printed with both source sentences and the bundle's values, for a human
to inspect. This is a cheap stand-in for the full dual-LLM-encoding idea
(encode the same sentence twice, independently, flag divergence) -- no
second model call, just a second reading. Boundary bundles are the
highest-yield place to run it, since inclusive/exclusive wording differences
show up exactly at the threshold value. Results are written to
`differential_check_results.csv`:

```sh
python3 differential_check.py
```

## Local API

`api.py` wraps the checker in a small Flask app so it can be called
interactively -- from a notebook, curl, or a UI -- instead of only as a
batch job. `GET /rules` lists the fixtures and the variable names each one
takes; `POST /check` takes a JSON `patient` + `plan` (merged into one set of
known values) and returns, for every rule whose variables were touched, the
same MUST-violate / MAY-violate / CERTIFIED-compliant verdict the rest of
this project computes, plus -- for MAY-violate -- a minimized
counterexample: the smallest set of still-free values that would force a
violation, via the same assumption-dropping minimization as
`benchmark.py`'s `minimal_counterexample_core`.

```sh
python3 -m pip install -r ../requirements.txt
python3 api.py

curl -s localhost:8000/rules | python3 -m json.tool

curl -s -X POST localhost:8000/check -H 'Content-Type: application/json' -d '{
  "patient": {"potassium": 5.4},
  "plan": {"ace_inhibitor_on": true}
}' | python3 -m json.tool
```

## Minimal UI

`ui.html`, served from `GET /` by `api.py`, is a click-through demo on top of
the same `/check` and `/rules` endpoints: a dropdown of ten example
patient/plan scenarios (spanning MUST-violate, MAY-violate, and
CERTIFIED-compliant, including one that pins `potassium_check_occurred:
false` to show a monitoring rule catching "never checked at all"), two JSON
textareas to paste your own, a Check
button, and a reference table of every rule's variable names. Each result
shows a color-coded verdict badge and a plain-English explanation --
including, for MAY-violate, the counterexample translated into a sentence
("It becomes a certain violation if digoxin dose reduced = false") instead
of raw JSON. No extra dependency: it's a single static HTML file with
inline JS that calls the same Flask app.

```sh
python3 api.py
# open http://localhost:8000/
```