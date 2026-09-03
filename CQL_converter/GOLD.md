# The gold standard: labelling criteria

**Written before any tool output was consulted.** That ordering is the point.
Criteria derived from what a system already produces cannot be used to measure
it — they encode its behaviour as the definition of correct.

Do not run the pipeline on a statement before labelling it. If you have
already seen the tool's answer for a statement, mark it `PEEKED` and drop it
from the evaluation set.

## What is being labelled

One **statement** — a candidate recommendation as the segmenter extracted it.
The question is not "did the tool do well" but: *could a careful person encode
this faithfully in the DSL, and if not, why not?*

The DSL a statement must fit:

- A rule is `when → then`: an applicability condition and a consequent.
- Atoms are comparisons on variables (`eGFR < 30`, `has_diabetes == true`),
  intervals, and `Since(event, days)`.
- Variables must be bindable to a clinical code (LOINC / SNOMED / RxNorm /
  a value set) **or** declared as a named hole.
- `then` must contain the action the recommendation is actually about.
- `deontic` is `obligation` or `permission`.

## The four labels

### ENCODE
Every clinical concept is nameable and bindable, every threshold is stated
numerically or is a plain boolean, and the whole obligation fits `when → then`
with nothing lost.

> *"Avoid metformin if eGFR is below 30 mL/min/1.73m²."*

### PARTIAL
The statement is encodable **except** for one or more predicates that the
source states but never defines. Those become named holes. The rest of the
rule is faithful, and the action is present.

> *"Start an ACE inhibitor when eGFR falls below 30 **or the patient is at
> high risk**."* — `has_high_risk` is a hole; the rest encodes.

Label PARTIAL only if you can name the hole as a phrase **verbatim from the
statement**. If you cannot point at the words, it is a REFUSE.

### REFUSE
No faithful encoding exists. Pick exactly one reason:

| reason | when |
|---|---|
| `vague_predicate` | An unstated threshold or qualifier the logic cannot carry: "adequate", "significant", "with caution", "appropriate". |
| `defeasible` | The obligation is explicitly overridable by clinician judgement or patient preference. "based on individual assessment", "shared decision-making". |
| `not_patient_level` | About a service, an institution, a guideline process, or a research need — not about a patient. **Institutional capability counts here** ("where expertise is available", "in centres with"). |
| `unsupported_temporal` | Needs a temporal pattern beyond `Since(event, days)` — recurring schedules, ordering of three or more events, durations of states. |
| `unbound_concept` | A clinical concept with no code in any vocabulary we use. Note: if the concept HAS a code and retrieval merely failed, that is still `unbound_concept` — but flag it in `notes` as a retrieval gap, because it is our bug, not the guideline's. |
| `beyond_dsl` | Encodable in principle, but not in this fragment — arithmetic on several variables, quantification over a collection, dose titration. |

### NOT_A_RECOMMENDATION
The segmenter was wrong: this is background, a methods sentence, an evidence
summary, a heading. **Label it, do not discard it** — this is the segmenter's
error rate and it belongs in the denominator separately.

## Deontic label

Independent of the four labels above. Label every statement that is not
`NOT_A_RECOMMENDATION`.

- **obligation** — the guideline requires the action. "We recommend X."
  "X is indicated." **"We suggest X"** — GRADE-conditional is weak *evidence*,
  not an optional action.
- **permission** — the guideline offers the action. "X may be considered."
  "X is an option." "at the discretion of."

If a sentence contains both ("we suggest that X may be considered"), the
permissive wording governs: **permission**.

## Decision order

Work down the list; stop at the first that applies.

1. Is it a recommendation about a patient at all? No → `NOT_A_RECOMMENDATION`
   or `not_patient_level`. (A heading or an evidence summary is the former; a
   sentence about how a service should be organised is the latter.)
2. Is the action it is about nameable as a variable? No → `unbound_concept`.
3. Does the obligation need something the fragment cannot express? →
   `unsupported_temporal` or `beyond_dsl`.
4. Is the obligation explicitly overridable by judgement? → `defeasible`.
5. Is a threshold or qualifier used but never defined?
   - Can you quote the undefined phrase verbatim, with the rest still faithful?
     → `PARTIAL`.
   - Otherwise → `vague_predicate`.
6. Otherwise → `ENCODE`.

## Rules of thumb that resolve most disagreements

- **The action must survive.** If your encoding does not contain the thing the
  sentence recommends, it is not an encoding of that sentence. A rule that
  encodes only the *preconditions* for an action is wrong, not partial.
- **Outcomes are not conditions.** "acupuncture may be effective in relieving
  abdominal pain in patients with AP" — the population is AP; pain relief is
  the *effect*. Do not put an outcome in `when`.
- **Do not invent a threshold.** If the source says "significant renal
  impairment" and you know the usual cutoff, that knowledge is not in the
  document. `vague_predicate`.
- **Population restrictions are load-bearing.** Dropping "in patients with
  persistently positive antiphospholipid antibodies" changes who the rule
  applies to. If it cannot be encoded, the statement is not ENCODE.
- **One statement, one label.** If a sentence carries two recommendations,
  split it and label each; note the split.

## Procedure

1. Two annotators label independently. Do not discuss while labelling.
2. Report raw agreement and Cohen's κ **on the four labels** and separately on
   `deontic`.
3. Adjudicate disagreements in a third pass, recording *why* — the adjudication
   notes are how these criteria get sharpened.
4. Only then run the tool and compare.

Report the agreement figure whatever it is. A κ below ~0.6 on the four labels
is itself the finding: it would mean formalizability is not a well-defined
property of a statement, which is a stronger claim than any accuracy number.

## Fields

| field | values |
|---|---|
| `label` | `ENCODE` / `PARTIAL` / `REFUSE` / `NOT_A_RECOMMENDATION` |
| `reason` | one of the six reason codes, only when `label=REFUSE` |
| `holes` | the verbatim undefined phrases, `;`-separated, only when `label=PARTIAL` |
| `deontic` | `obligation` / `permission` |
| `notes` | free text; flag retrieval gaps, splits, and anything you were unsure of |
| `annotator` | your initials |
