# Guideline Autoformalization

Turns clinical guideline text into checkable rules, then checks a patient +
care plan against them and gives a real answer: definitely violates,
definitely compliant, or genuinely undetermined (with the exact missing
fact named) -- never a silent guess.

- **Part A** -- formalizes a guideline document into typed rules.
- **Part B** -- the checker: runs a patient/plan against those rules
  (or a set of ten hand-authored example rules, no upload needed).

This README covers running Part B, the part with a UI. See
[`Part B/CHECKER.md`](Part%20B/CHECKER.md) for how it works in detail.

## Setup

```sh
python3 -m venv .venv
.venv/bin/python3 -m pip install -r requirements.txt

cp .env.example .env
# edit .env, add your ANTHROPIC_API_KEY (only needed for uploading your own
# guideline live -- the ten example rules work with no key at all)
```

## Run it

```sh
cd "Part B"
../.venv/bin/python3 api.py
# open http://localhost:8000/
```

That's a dashboard of guidelines -- click one to check a patient against it,
or use "+ Upload guideline" to formalize your own document. New here? Open
`http://localhost:8000/demo` first for a 30-second live example, no setup.

## More detail

- [`Part B/CHECKER.md`](Part%20B/CHECKER.md) -- how the checker and its
  pages work, the three-valued verdict, uploading guidelines.
- [`Part A/DSL.md`](Part%20A/DSL.md) -- the rule language guideline text
  gets formalized into.
- [`Part A/BACKLOG.md`](Part%20A/BACKLOG.md) -- known open issues.
