# Documentation — DRAFT PLAN (outline only, no content yet)

This is a skeleton for review, not the final doc. Once you approve/adjust the
shape, I'll fill each section in from the source files listed. Nothing below
is final wording — just what would go where and why.

Sources rolled up: BIND.md, CQL.md, DSL.md, GATE.md, GOLD.md, SEGMENT.md
(the five pipeline stages + gold labelling), plus NOTES.md and RESULTS.md
treated as *reference*, not fully inlined (reasons below). BACKLOG.md is
excluded entirely — it's a live issue tracker, not documentation.

---

## 1. Overview (new, ~1 paragraph + 1 diagram)
- One-paragraph pitch: Track A turns a guideline document into formally
  checkable rules, refusing rather than guessing where it can't.
- Pipeline-at-a-glance diagram: `Document → Segment → Bind → Gate → DSL → CQL`
- One-line purpose + source file per stage (segment.py, bind.py, gate.py,
  dsl.py, cql.py)

## 2. How It Works — one subsection per stage
Each stage gets: **the one idea** (already crisply stated in each source
doc) → **input/output** → **key mechanism bullets** (the pipeline steps,
e.g. segmenter's 5 stages, binder's 6-step flow, gate's evidence
aggregation, DSL's `phi = when -> then`, CQL's deterministic lowering).

- 2.1 Segmenter — document → `Statement[]` with provenance
- 2.2 Binder — phrases → `dsl.Registry` (closed vocabulary)
- 2.3 Gate — statement → encode / partial / refuse decision
- 2.4 DSL — the typed IR, `Rule`/`Refusal`, Z3 lowering
- 2.5 CQL — `Rule` → HL7 CQL library, deterministically

## 3. How To Use It (new, assembled from each doc's Quickstart)
- Setup: what's needed to run offline (no key/network) vs. live (DB, API
  keys) — pulled from each stage's test-run line and NOTES.md Environment
- Per-stage CLI + Python quickstart (condensed from the five Quickstart
  sections)
- One end-to-end worked thread: a sentence going
  segment → bind → gate → DSL → CQL, so the stages visibly compose
- Where tests live / how to run the whole suite

## 4. Glossary — definitions of terms
Flat, alphabetized-or-grouped list of every term introduced across the five
docs, one entry each. Grouping proposal below — flag if you'd rather it be
strictly alphabetical.

- **Pipeline data types**: Statement, Provenance, Block/Block kinds,
  Binding, Registry, VarDef, EventDef, Rule, Refusal, Triage, Evidence,
  Synthesis
- **Core logic concepts**: `phi = when -> then`, fragment (PROP / LIA /
  MTL), hole / open hole, canonical form, `plausible` range, deontic
  (obligation vs. permission), modal predicate
- **Decision vocabulary**: encode / partial / refuse, the six refusal
  reason codes (unbound_concept, not_patient_level, unsupported_temporal,
  beyond_dsl, vague_predicate, defeasible)
- **CQL-specific terms**: library, define, valueset/codeset, ELM,
  three-valued logic, `cql-to-elm`
- **Gold-standard labels**: ENCODE / PARTIAL / REFUSE /
  NOT_A_RECOMMENDATION, deontic label

## 5. Evaluation Methodology (from GOLD.md)
- What's being labelled and why (labelling before seeing tool output)
- The four labels + decision order, condensed to a short procedure list
- Agreement/adjudication procedure, in brief

## 6. Design Decisions Worth Keeping (curated, from NOTES.md + RESULTS.md)
This is the "brief but important" compression of a 2180-line findings log —
proposing to keep only the decisions that shape how someone should *use or
extend* the system, not the full experimental narrative. Candidates to
include (confirm or trim):
- null-vs-false for unbound predicates (three-valued CQL)
- canonicalization + the vacuity check (duplicate-concept tautology bug)
- deontic as an operator, not a guard (permission vs. obligation)
- semantic vs. lexical terminology retrieval
- time as flattened `Since(...)`, not yet time-indexed (open decision)
- the four deterministic-veto refusal reasons vs. the two model-judged ones
- Everything else in RESULTS.md (R1–R33) stays *in* RESULTS.md, linked, not
  duplicated here

## 7. Known Limitations (consolidated)
Merge of each stage doc's "Limits" section into one table/list, grouped by
stage, so a reader sees the whole residue picture in one place instead of
five.

## 8. Status (brief, no backlog)
- Short "where things stand" summary only (offline-only so far, no LLM
  wired in live, no compiler wired in, etc.) — no bug/task list, no
  pointer to BACKLOG.md; that file stays out of this doc entirely

## 9. Source Map
Table: topic → canonical source file, so DOCUMENTATION.md stays the brief
entry point and the original docs stay the detailed reference.

---

## Open questions for you before I write the real content
1. Section 6 — happy with that candidate list of "decisions worth keeping",
   or is there a specific R-numbered finding from RESULTS.md you want
   promoted in (or something above cut)?
2. Any target length (e.g. "under N lines") for the final brief doc?
