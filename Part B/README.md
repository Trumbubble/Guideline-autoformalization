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

`real_rules.py` hand-authors eight typed guideline rules beyond the
metformin/eGFR example in `partial_spec_check.py` -- ACE inhibitor +
potassium monitoring, DOAC dosing by renal function, drug-drug interaction
flags, and more. Each `SpecClause` bundles a Z3 predicate over realistically
named variables, the numeric threshold(s) it turns on, and the guideline
sentence it was authored from, so Track B has real material to check without
waiting on a document-extraction pipeline. Six of the eight rules also carry
`Boundary` metadata: the Z3 variable each numeric threshold constrains, the
comparison operator, and a step size at the variable's realistic reporting
precision.

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