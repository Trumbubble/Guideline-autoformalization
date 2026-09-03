# DSL to CQL

Stage 4b of Track A: lower a validated `dsl.Rule` to an HL7 CQL library, and
repair it against a compiler. One file, [`cql.py`](cql.py); tests in
[`test_cql.py`](test_cql.py).

```
python3 test_cql.py     # 20 tests, offline, no JVM, no key, no network
```

## The one idea

**The lowering is deterministic.** The model's only job was to produce a
validated `dsl.Rule`; turning that into CQL is a total function with no
judgement in it. That is the point — it is the step a governance committee can
re-run and get byte-identical output, and the step where "an LLM wrote our
clinical logic" stops being true.

## Quickstart

```python
import cql, dsl

lib = cql.rule_to_cql(rule, registry)
print(lib.text)                       # a complete, self-contained CQL library
print(cql.check_cql(lib.text))        # structural issues, offline

out = cql.synthesize(statement, registry, formalize=my_llm,
                     statement_id="s1", compiler=my_cql_to_elm, max_repairs=2)
if out.ok:
    print(out.library.text)
else:
    print(out.refusal.reason_code, out.refusal.reason_text)
```

## What it emits

```cql
library Kdigo31 version '1.0.000'
using FHIR version '4.0.1'
include FHIRHelpers version '4.0.1' called FHIRHelpers

codesystem "LOINC": 'http://loinc.org'
valueset "ACE inhibitors codes": 'urn:oid:2.16.840.1.113762.1.4.1078.9' version '20241228'
valueset "Diabetes codes": 'urn:oid:2.16.840.1.113883.3.464.1003.103.12.1001' version '20250115'
code "Serum Potassium Measured codes": '2823-3' from "LOINC"

context Patient

/* kdigo-3.1
   action: recommend ACEi
   strength: 1B
   source: /article/body/sec[3]/boxed-text
   PARTIAL: 1 unbound predicate(s) left null: has_high_risk */

define "Diabetes":
  exists ([Condition: "Diabetes codes"])

// UNBOUND: the source states this predicate but does not define it.
// Left null so CQL's three-valued logic propagates the uncertainty.
define "at high risk":
  null as Boolean

define "Applicable":
  (("Diabetes" and "urine ACR" >= 30 'mg/g') or "at high risk")

define "Satisfied":
  ("ACE inhibitors"
  and exists ([Observation: "Serum Potassium Measured codes"] O
      where O.effective during Interval[Today() - 14 days, Today()]))

define "Compliant":  not "Applicable" or "Satisfied"
define "Violated":   "Applicable" and not "Satisfied"
```

Four things in there are load-bearing.

### 1. Version-pinned terminology is the artifact's contract

`valueset "…": 'urn:oid:…' version '20241228'` is what a reviewer resolves
against VSAC themselves. It is why the binder pins versions, and why an
unversioned binding is a warning there — two value sets named "Diabetes" in
VSAC differ by nine years.

Terminology names are deliberately **not** the define names. `[Condition: X]`
requires X to be a value set or code declaration; reusing the define's name
produces a self-referential library that looks plausible and is not valid CQL.
Hence `"Diabetes codes"` (the terminology) versus `"Diabetes"` (the define).

### 2. Holes become `null`, not `false`

**CQL is natively three-valued** — `true and null` is null, `false and null` is
false. So an unbound predicate propagates exactly the uncertainty it should,
and `"Compliant"` evaluates to null rather than to a verdict.

PathWISE stubs uncomputable nodes as `false`, which silently narrows the rule —
the checker stops firing for exactly the patients the predicate was about.
`true` over-fires instead. Leaving it null is the honest option, and it is not
an invention: the target language already carries three-valued logic, and the
two-valued stub throws that away.

The library header names every hole, so a `PARTIAL` encoding is visible in the
artifact rather than only in the pipeline's metadata.

### 3. Time is kept, not flattened

The DSL has **two lowerings of the same node**, and the difference is
deliberate:

| | `Since(potassium_check, 14)` becomes |
|---|---|
| `dsl.to_z3` | `potassium_check_occurred and days_since_potassium_check <= 14` |
| `cql.rule_to_cql` | `exists ([Observation: X] O where O.effective during Interval[Today() - 14 days, Today()])` |

Z3 has no notion of a patient timeline, so the temporal layer is flattened to
derived state. A CQL engine evaluates over the record, so **the retrieve plus a
temporal `where` IS the bounded existential** — no unrolling required.

The consequence matters: **the DSL does not need time-indexed variables for the
CQL to be faithful.** That is what made deferring the time-indexing decision
safe rather than merely convenient. Genuinely multi-visit predicates ("on two
occasions ≥ 90 days apart") are still out of scope and route to
`unsupported_temporal`.

### 4. Units survive into the comparison

`"urine ACR" >= 30 'mg/g'`, not `>= 30`. mg/g versus mg/mmol is a factor of
~8.84, and a bare number in CQL loses the only thing that distinguishes them.

## Checking

`check_cql(text)` — deterministic, offline, no JVM:

| code | catches |
|---|---|
| `MISSING_LIBRARY_HEADER` | no `library <name>` |
| `MISSING_CONTEXT` | no `context Patient` |
| `UNBALANCED_DELIMITER` | brackets that do not close |
| `UNDECLARED_IDENTIFIER` | a quoted name used but never declared |
| `EMPTY_DEFINE` | a define with no body |

And from `compile_cql`, which adds an injected compiler on top:

| code | severity | meaning |
|---|---|---|
| `CQL_COMPILE_ERROR` | error | the injected translator rejected the library; the message is passed through verbatim so the repair loop can feed it back |
| `COMPILER_UNAVAILABLE` | **warn** | the translator did not run at all — structural checks only, and explicitly *not* a compile pass |

**This is not a compiler.** It cannot see a type error, does not know the FHIR
model, and will happily pass a library that `cql-to-elm` rejects. It exists so
the obvious breakage is caught without a JVM, and so the repair loop has
something to push back on when no compiler is wired in.

## Wiring the real translator (DEV_PLAN P6)

`compile_cql(text, compiler=...)` takes `compiler(text) -> [error strings]`.
That seam is where the official **cql-to-elm** translator goes.

`cql-to-elm` (from `cqframework/clinical_quality_language`) parses CQL,
typechecks it against the FHIR model, and emits **ELM** — a typed JSON IR. It
is needed twice over:

- **as the compile oracle.** It is the only real syntactic and type gate, and
  the same critic PathWISE's 100%-compilation figure is measured against. E5
  ("what does constrained decoding actually buy?") is only meaningful with it
  in the loop, and with it injected the ablation is a one-argument change.
- **as the route to execution.** `cql-execution`, the JS engine Track B needs,
  does **not** parse CQL — it consumes ELM JSON only. So nothing runs against a
  FHIR bundle until the translator does.

It is Java (25 is installed here), published on Maven Central under
`info.cqframework`, and needs its full transitive dependency tree — which is
why it is a separate task rather than an import.

**An unavailable compiler is a warning, never a pass.** `COMPILER_UNAVAILABLE`
is raised at `warn` severity with the text *"structural checks only; this is
NOT a compile pass"*, because silence must not read as success.

## The repair loop

```
synthesize(statement, registry, formalize, compiler=None, max_repairs=2)
```

```
formalize -> validate_rule -> check_nondegenerate -> rule_to_cql -> compile_cql
     ^                                                                  |
     +------------------ errors fed back, BOUNDED ----------------------+
                                     |
                            exhausted -> Refusal(beyond_dsl)
```

Both DSL validation errors and compiler errors are fed back, verbatim, as
`CODE: message (hint)`. The retry prompt says: *fix exactly these problems; if
they cannot be fixed without inventing something the source does not state,
refuse instead.*

**Bounded, then refuse.** An unbounded repair loop converges on whatever
happens to compile, which is precisely the failure this project argues against
— self-correction is unreliable (arXiv:2502.12065), so the loop gets a small
budget and falls through to a refusal rather than grinding until something
passes. A model refusal short-circuits immediately; a decoder exception becomes
a refusal rather than a crash.

`Synthesis` carries `rule`, `library`, `refusal`, `attempts`, `repairs` (what
each round was told to fix) and `issues` — so a failed synthesis is
diagnosable, not just absent.

## Limits

- **`check_cql` is structural.** Until P6 lands, "compiles" means "brackets
  balance and names resolve", which is much weaker than it sounds.
- **The FHIR mapping is a convention, not a model.** `lab -> Observation`,
  `condition -> Condition`, `drug/drug_class -> MedicationRequest`,
  `procedure -> Procedure`. Real artifacts need more care — a drug class may
  need `MedicationStatement` as well, and lab retrieves ignore
  `Observation.category`.
- **`Last(... sort by effective)` is an assumption.** The most recent final
  result is usually what a guideline means, but not always — "on two occasions"
  needs something else, and that is `unsupported_temporal` today.
- **No execution.** Nothing here has been run against a FHIR bundle; that is
  the second half of P6 and Track B's execution service.
- **Never compiled.** Every library in the tests passes the structural checks
  only. The first real `cql-to-elm` run should be expected to find things.
