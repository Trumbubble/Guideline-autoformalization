# The terminology binder

Stage 2 of Track A: clinical phrases in, a `dsl.Registry` out. One file,
[`bind.py`](bind.py); tests in [`test_bind.py`](test_bind.py); decisions and
gotchas in [`NOTES.md`](NOTES.md).

```
python3 tests/test_bind.py     # 22 tests, offline, no network, no UMLS account
```

## The one idea

It is **not** a lookup service the compiler calls. It **builds the closed
vocabulary the compiler is then constrained to.** Whatever it cannot bind never
becomes a variable, so the synthesizer never gets the chance to fudge it — the
concept goes to `Refusal(unbound_concept)` and into the measured residue.

**The model never types a code.** Asked for "the SNOMED code for albuminuria" a
model returns a well-formed, plausible, sometimes-wrong number, and nothing
downstream catches it: the CQL compiles, the solver runs, the verdict is
confidently wrong for the wrong patients. Here the selector is shown a numbered
candidate list and returns an **integer** — output space `0..k` plus ABSTAIN —
so a hallucinated code cannot be expressed.

## Quickstart

```bash
python3 bind.py --registry                      # offline fixture index
python3 bind.py --db tx.db --remote --registry "In CKD with hyperkalemia, avoid an ACE inhibitor."
```

```python
import bind, dsl

ix = bind.fixture_index()                       # or SqliteIndex("tx.db")
bindings, refusals = bind.bind_statement(sentence, ix)
for b in bindings:
    issues = bind.verify(b, ix)                 # deterministic re-check
reg = bind.registry_from_bindings(bindings, bind.FIXTURE_PLAUSIBLE)
```

## What it produces

### `Binding`

| field | meaning |
|---|---|
| `variable`, `var_type`, `role` | the `dsl.VarDef` this becomes; `role` is `action` or `observation` |
| `mention` | the guideline's own words, verbatim |
| `kind` | `lab` / `condition` / `drug` / `drug_class` / `procedure` |
| `system`, `code`, `display` | the bound concept |
| `version` | terminology version — **never optional** |
| `unit` | read off the sentence; part of the meaning for labs |
| `expansion` | value-set member codes, when the concept is a set |
| `candidates`, `selected_index`, `selector`, `reason` | the audit trail: everything shown, what was picked, why |
| `binding_uri` | version-pinned identifier that goes in `VarDef.binding` |

`candidates` is not debug output. It is how a clinician sees what was *rejected*,
and it is what makes a wrong binding diagnosable rather than mysterious.

### `dsl.Refusal(reason_code="unbound_concept")`

For phrases no value set defines — *"high-risk"*, *"frailty"*, *"an adequate
trial"* — and for genuine ambiguity the sentence does not settle. **Refusing is
a correct outcome**, and these are counted, not logged.

### `dsl.Registry`

`registry_from_bindings()` is the seam into stage 4. Variables carry `binding`,
`codes`, `unit`, `role`, and a caller-supplied `plausible`. A test writes a
`Rule` over the result, typechecks it, runs the vacuity sweep, and solves for a
violating patient — end to end from a sentence.

## How it works

```
sentence
  → propose_mentions   verbatim spans + a kind guess (spans are substring-checked)
  → search_all         phrase ∪ its abbreviation expansion, scoped by kind
  → rerank_by_unit     deterministic disambiguation, before any model
  → select             an INDEX or ABSTAIN, never a code
  → validate           the chosen code must be in the index
  → Binding | Refusal
```

### Four deterministic guards, all found by running real data

**Being the only candidate is not evidence.** Live LOINC returns
*"EGFR gene exon 20 insertion [Presence] … by Molecular genetics method"* for
the query `eGFR` — the epidermal growth factor receptor gene — and nothing else.
A selector reading "no competition" as confidence binds that to a
kidney-function threshold. `POSITIVE_EVIDENCE` now requires an exact term match,
a unit match, or full query-token coverage.

**Units settle minimal pairs with no judgement.** LOINC 9318-7 is mg/g,
14585-4 is **molar** ratio in mg/mmol — the same clinical threshold a factor of
~8.84 apart. They score *identically*; the sentence's unit decides. With no unit
stated, the binder refuses, because a coin-flip there is an 8.84× error nothing
downstream can detect.

**Abbreviations are expanded.** `eGFR` alone is unresolvable; searching
"estimated glomerular filtration rate" as well is deterministic and auditable.

**Grouper codes are demoted.** LOINC carries *"HEDIS MY 2020 Value Set –
Estimated Glomerular Filtration Rate"* beside the real result code, and it wins
on token overlap. You cannot threshold a value set.

Plus light stemming for comparison only (never the FTS query): prose says
"an ACE inhibitor", ATC says "ACE INHIBITORS, PLAIN", and without it a correct
binding is refused on a plural.

## Indexes

| class | use |
|---|---|
| `SqliteIndex` | local FTS5 cache. **The file is the version pin** — a reviewer re-running your pipeline gets your codes, which a live server cannot promise. |
| `RemoteIndex` | live lookups, routed by kind. For *building* a cache, not serving queries in a loop. |
| `CachedIndex` | SQLite first, remote on miss, write-through. |

### Sources — none need an account

| kind | source | endpoint |
|---|---|---|
| condition, procedure | SNOMED CT | `tx.fhir.org` — `$expand?filter=`, `?fhir_vs=isa/<code>`, `$validate-code` |
| lab | LOINC | NLM Clinical Tables |
| drug, drug_class | RxNorm / ATC | RxNav |

A UMLS/UTS licence adds **VSAC**'s curated, steward-versioned value sets —
better than a computed is-a expansion, and what the AHRQ CDS Connect reference
artifacts bind to, which is what makes an M2 comparison like-for-like. Nothing
is blocked without it; VSAC slots in as one more source feeding the same cache.

## `verify()` — the checkable property

Re-runs independently of the model, so "codes are retrieved, never generated"
is something you can assert rather than assert *about*.

| code | severity | catches |
|---|---|---|
| `CODE_NOT_IN_INDEX` | error | a code that does not exist — a stale cache, or a mis-wired index |
| `WRONG_SYSTEM_FOR_KIND` | error | a lab bound to SNOMED, a condition bound to LOINC |
| `UNVERSIONED_BINDING` | warn | no version — the verdict changes silently when the terminology updates |
| `MISSING_UNIT` | warn | a lab with no unit; mg/g vs mg/mmol both typecheck |
| `SINGLE_CODE_FOR_A_SET` | warn | a condition bound to one code — a patient recorded under a sibling term silently fails the rule |

## `plausible` is supplied, not looked up

No terminology publishes physiological ranges. It is load-bearing twice —
unit-slip warnings at compile time, and the solver's domain axioms, without
which counterexamples come back at eGFR = 2.0 — so it is hand-curated per
registry and passed to `registry_from_bindings`.

## Limits

- **The three hooks now do run live.** `demo.py` wires `llm.propose_mentions`
  and `llm.select_candidate` into every `bind.bind_statement` call, and the
  segmenter's classifier is live too (see SEGMENT.md). R12 onward measures
  against a 9-document PMC corpus with real API calls, not offline heuristics
  and mock callables — so refusal rates reported there are the measured
  residue, not an upper bound on it. What's still true: no clinician has
  adjudicated any of it, and coverage numbers predate whichever fixes in
  BACKLOG.md landed most recently, so re-check before citing one.
- **`propose_mentions` is a stub**: 15 hardcoded regexes. On a real ASH/ISTH
  sentence it finds **zero** concepts. It makes the pipeline runnable; it is not
  a design.
- **`RxClass class/byName` needs exact class names** — "ACE inhibitor" gets 0
  hits, "ACE INHIBITORS, PLAIN" gets 2 — so live drug-class binding is
  unreliable until the 1,319-class ATC seed lands.
- **NLM Clinical Tables returns no units** (`EXAMPLE_UCUM_UNITS` is empty on
  every row), so unit matching cannot work against that source. The fixture
  carries units; real unit data needs the licensed LOINC download.
- **VSAC is not wired in** — expansions are computed is-a sets, not curated
  value sets.
