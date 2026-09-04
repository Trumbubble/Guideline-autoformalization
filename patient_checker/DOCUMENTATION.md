# patient_checker: Documentation

CQL_converter ([`../CQL_converter`](../CQL_converter)) turns a guideline into
`dsl.Rule` objects. This directory is patient_checker: given a rule, a patient, and
a partial care plan, decide whether the plan complies — and when it might
not, produce a minimal, concrete witness instead of a vague "maybe."

---

## 1. Overview

- [`real_rules.py`](real_rules.py) — ten hand-authored guideline rules
  through the real DSL, so there's always real material to check against.
- [`guidelines.py`](guidelines.py) — runs CQL_converter's actual pipeline
  (segment → gate.triage → bind → `dsl.write_bundle`) over an uploaded
  document and stores the result as another checkable guideline.
- [`api.py`](api.py) — the Flask app: lists rules, runs checks, serves the
  UI pages.
- [`ui.html`](ui.html) / [`demo.html`](demo.html) / [`dashboard.html`](dashboard.html)
  — the click-through checker, a no-setup live demo, and the guideline
  library/upload page.

## 2. How It Works

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

A guideline is either the built-in `fixtures` (from `real_rules.py`) or
something uploaded and run through CQL_converter live. Formalizing a sentence
into a rule is a **model step** — without an API key, a document still
segments and triages (real statement counts, real refusals) but produces
**zero rules**, reported honestly as `needs_model_count` rather than as a
guideline that "can't be encoded." A rule can also be **partial**: an
undefined predicate becomes a declared free variable (a hole), not a
silent drop or a stub.

## 3. How To Use It

```sh
python3 -m pip install -r ../requirements.txt
../.venv/bin/python3 api.py
# open http://localhost:8000/        (dashboard)
# open http://localhost:8000/demo    (no setup, three live checks)
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

- **MUST-violate / CERTIFIED-compliant / MAY-violate** — the three verdicts
  (§2); MAY-violate always carries a minimized counterexample, the smallest
  set of still-free values that alone forces the violation.
- **Hole** — a predicate the guideline states but never defines, kept as an
  open free variable rather than guessed.
- **Fixtures** — the ten always-available rules in `real_rules.py`, as
  opposed to an uploaded guideline.
