# Guideline Autoformalization

Turns clinical guideline text into checkable rules, then checks a patient +
care plan against them and gives a real answer: definitely violates,
definitely compliant, or genuinely undetermined (with the exact missing
fact named).

## The problem

Turning a guideline into machine-checkable rules is usually scored on
*coverage*: the fraction of statements successfully translated into a rule.
That rewards exactly the failure that matters most: a default substituted for
an undefined predicate, or two clinical concepts silently merged into one
symbol, both produce a rule that compiles cleanly and raises the coverage
number while asserting something false about the patient.

## The approach

This project treats **refusal as a measured output**: every run reports the rule set and a per-statement account of
what it declined to encode, with a reason and the source sentence. Three
things make that possible, and each is also why this differs from the
alternatives (substituting a default, or asking a model if it's
unsure): terminology codes are always **retrieved** instead of generated, 
so it doesn't hallucinated code; an undefined predicate stays a **named free variable** 
instead of a silent stub; and **deontic force is an operator** in the rule language itself, 
so a permission has no violating patient by construction. 
Every rule is also checked twice -- on write and on load -- and a patient check returns one of three
verdicts (violates, compliant, genuinely undetermined), giving more information
than a simple yes/no.

## Run it

**Live product link:** [guidelineforge.fly.dev](https://guidelineforge.fly.dev/).
Its
[demo page](https://guidelineforge.fly.dev/demo) is a pre-loaded live example
requiring no setup. No server-side Anthropic API key is configured on this deployment, so
formalizing your own document live there asks you to paste one -- used for
that one upload only and is never stored (see `_temporary_api_key` in
`patient_checker/guidelines.py`). The dashboard, the demo page and the
built-in fixtures all need no key at all.

**Locally:**

***Setup***

```sh
python3 -m venv .venv
.venv/bin/python3 -m pip install -r requirements.txt

cp .env.example .env
# edit .env, add your ANTHROPIC_API_KEY (only needed for uploading your own
# guideline live -- the ten example rules work with no key at all)
```

***Running***

```sh
cd "patient_checker"
../.venv/bin/python3 api.py
# open http://localhost:8000/
```

That's a dashboard of guidelines -- click one to check a patient against it,
or use "+ Upload guideline" to formalize your own document.
`http://localhost:8000/demo` has a working example with no guideline to
pick.

## Tech stack

![Tech stack overview](media/Tech_Stack.svg)

- **Formalization pipeline** (`CQL_converter/`) -- Python. Anthropic's Claude
  models propose candidate concept mentions, select a terminology code among
  retrieved candidates, and formalize a statement into the rule language.
- **Terminology** -- SNOMED CT, LOINC, RxNorm and ATC/VSAC, cached locally in
  a SQLite index (`terms.db`) built once from licensed releases, with the
  free NIH Clinical Tables, RxNav, tx.fhir.org and VSAC FHIR endpoints as a
  live fallback. An optional semantic layer (`numpy` over precomputed
  embeddings) catches synonyms the lexical index misses; its absence just
  degrades to lexical-only retrieval, never a crash.
- **Document ingestion** -- `lxml` for JATS/XML, `pymupdf` for PDF (including
  geometric box-recovery for boxed recommendations tables don't carry as text).
- **Checker** (`patient_checker/`) -- Flask, with Z3 (`z3-solver`) as the SMT
  solver behind every verdict: compliant, violating and "genuinely
  undetermined" are literal solver queries, not string-matched logic.
- **Frontend** -- plain HTML/CSS/JS, no framework and no build step; every
  page is one self-contained file Flask serves directly.
- **Deployment** -- Docker, deployed to Fly.io

## Documentation

- [`patient_checker/DOCUMENTATION.md`](patient_checker/DOCUMENTATION.md) -- how the
  checker and its pages work, the three-valued verdict, uploading guidelines.
- [`CQL_converter/DOCUMENTATION.md`](CQL_converter/DOCUMENTATION.md) -- the
  formalization pipeline in detail: segmenting, binding, the DSL, the gate, CQL.
