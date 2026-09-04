# Guideline Autoformalization

Turns clinical guideline text into checkable rules, then checks a patient +
care plan against them and gives a real answer: definitely violates,
definitely compliant, or genuinely undetermined (with the exact missing
fact named) -- never a silent guess.

- **CQL_converter** -- formalizes a guideline document into typed rules.
- **patient_checker** -- the checker: runs a patient/plan against those rules
  (or a set of ten hand-authored example rules, no upload needed).

This README covers running patient_checker, the part with a UI. See
[`patient_checker/CHECKER.md`](patient_checker/CHECKER.md) for how it works in detail.

## Setup

```sh
python3 -m venv .venv
.venv/bin/python3 -m pip install -r requirements.txt

cp .env.example .env
# edit .env, add your ANTHROPIC_API_KEY (only needed for uploading your own
# guideline live -- the ten example rules work with no key at all)
```

Optional, and only for the CQL half: the checker and the demo need none of
it, but compiling a rule to ELM and executing it against a FHIR bundle uses
HL7's Java translator and a Node engine.

```sh
cd patient_checker/engine && npm ci && cd ../..
```

Without it, `toolchain.available()` is False, `test_toolchain.py`'s two
execution tests fail, and an upload records `compiler_available: false`
rather than reporting its CQL as compiled -- an absent toolchain is never
counted as a pass.

## Run it

```sh
cd "patient_checker"
../.venv/bin/python3 api.py
# open http://localhost:8000/
```

That's a dashboard of guidelines -- click one to check a patient against it,
or use "+ Upload guideline" to formalize your own document. New here? Open
`http://localhost:8000/demo` first for a 30-second live example, no setup.

## Checking it works

```sh
cd CQL_converter
python3 -m pytest -q        # 325 pass offline, no API key
```

The demo page's sample patients exercise the checker end to end in a browser:
each button posts a real patient to `/guidelines/<id>/check` and shows the
verdict, including the undetermined case and what it is waiting on.

## More detail

- [`patient_checker/CHECKER.md`](patient_checker/CHECKER.md) -- how the checker and its
  pages work, the three-valued verdict, uploading guidelines.
- [`CQL_converter/DSL.md`](CQL_converter/DSL.md) -- the rule language guideline text
  gets formalized into.
- [`CQL_converter/BACKLOG.md`](CQL_converter/BACKLOG.md) -- known open issues.
