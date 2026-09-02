# Backlog

Open issues surfaced during the 2026-09-01 investigation into PMC13074134's
run-to-run ENCODE/PARTIAL/REFUSE instability, and the follow-up gold-set
comparison run on PMC13151662. Kept here so the next session doesn't have to
re-derive them from scratch. See RESULTS.md for the historical record of
what's already fixed; this file is only what's still open.

## Resolved

### R1. `terms.db` had almost no real-world coverage, and zero conditions -- FIXED 2026-09-01

`Part A/terms.db` (local, gitignored) had **59 rows total**: `drug` 23,
`drug_class` 11, `lab` 23, `procedure` 2, and **zero** `condition` rows --
confirmed by direct query, not inference. Every `condition`-kind lookup
failed unconditionally, and `procedure`/most `lab` lookups did too outside
glucose/insulin/IGF-1. Confirmed on PMC13151662:0002 ("systolic BP",
"mechanical thrombectomy") and PMC12948374 ("ABPM", "cyst", "urinary tract
infections", "formal transition planning") -- all failed to bind.

The user rebuilt `terms.db` (a full SNOMED/LOINC/RxNorm load): **634,459
rows** -- `condition` 278,042, `drug` 171,231, `procedure` 104,245, `lab`
80,842, `drug_class` 99. Re-ran the exact same four failing lookups plus
PMC12948374:0000 ("ABPM") and :0016 ("urinary tract infections"):

- PMC12948374:0000 -- REFUSE -> **PARTIAL** (`when: has_adpkd & has_age_5`,
  `then: abpm`). Was a hard REFUSE before; now a working (if slightly
  imprecise -- age modeled as a boolean hole rather than a numeric
  comparison) rule.
- PMC12948374:0016 -- **now matches gold exactly**: REFUSE/`vague_predicate`,
  verdict, reason code, and substantive reasoning all correct, now that
  "urinary tract infections" binds and the pipeline can reach the real
  judgment instead of stopping at "nothing bound."
- PMC12948374:0008 -- still correctly REFUSEs, and the reasoning text now
  explicitly says "the operative statement is an epistemic caution" (the
  `beyond_dsl`-flavored judgment both gold and this session's own read
  expected) -- though the terse `reason_code` still says `unbound_concept`
  rather than `beyond_dsl`. Later fixed -- see R5 below, this exact
  statement is its worked example.
- PMC13151662:0002 -- binding now fully succeeds for both concepts, but
  formalization then fails validation (`NO_ACTION_ATOM`). Later fixed --
  see R3 below -- a new, different, previously-hidden issue the coverage
  gap was masking.
- PMC12948374:0005 -- REFUSE -> **PARTIAL**, essentially an exact match to
  gold (`when: has_beginning_adolescence`, `then: formal_transition_planning`,
  same hole gold named). "Formal transition planning" now binds.

**Confirmed fixed as a data-coverage problem.** Don't re-diagnose
`unbound_concept` refusals as this issue going forward -- check
`Part A/terms.db`'s row count first (`SELECT COUNT(*) FROM concept`); if
it's back down near 59, the database was reset/rebuilt again.

### R2. `between` could be offered against a boolean-bound variable -- FIXED 2026-09-01

PMC13090725:0513 -- gold's own "cleanest statement in the set" (T2D+MASLD
population, numeric 150-240 min/week threshold, bindable action) -- failed
`TYPE_ERROR`, "between requires a numeric var." The exercise/activity
concept bound as a boolean (the same way a drug or procedure always does),
so `between(var, 150, 240)` had nothing numeric to attach the range to.
`dose_companions()` (`gate.py`) only fired on dose-CHANGE language
(`_DOSE_CHANGE`: reduce/increase/titrate by a %), not a stated absolute
range, and only declared companions for `drug`/`drug_class` bindings, not
`procedure`.

Fixed both halves:
- `gate.py`: added `_QUANTITY_RANGE`, a regex requiring a stated numeric
  RANGE (two numbers joined by a dash/"to", e.g. "150-240 min") -- not a
  bare single number, so it doesn't fire on "the dose was 5 mg" (see
  `test_the_dose_cue_needs_a_verb_and_a_quantity`, which still passes).
  `dose_companions()` now fires on `_DOSE_CHANGE` OR `_QUANTITY_RANGE`, and
  declares companions for `procedure` bindings too, not just drugs.
- `dsl.py`'s `decoding_schema`: `between` is no longer unconditionally
  offered against the full variable enum. It's now gated on numeric-variable
  existence the same way `delta`/`since` are already gated on event
  existence, and its `var` field points at a new numeric-only `numvar` enum
  instead of the general one -- the model can no longer select a boolean
  variable for `between` even if the companion heuristic above misses a
  future phrasing.

Verified live: PMC13090725:0513 now returns a clean ENCODE, exact match to
gold (`then: moderate_intensity_aerobic_exercise == True AND
Between(moderate_intensity_aerobic_exercise_dose, 150, 240)`). Re-ran the
full 5-statement PMC13090725 set to confirm no regressions elsewhere in the
same document -- none found. 155/155 offline tests still pass.

### R3. `NO_ACTION_ATOM`: "stay within a threshold" recommendations couldn't be encoded -- FIXED 2026-09-01

PMC13151662:0002 ("we recommend against intensively lowering systolic BP <
140 mmHg in the first 24h after mechanical thrombectomy") -- the exact
statement `dsl.py`'s own docstring cites as a historical bug ("the first 24
h... silently dropped") -- refused as `beyond_dsl`/`NO_ACTION_ATOM` even
after R1 fixed its terminology binding. `_action_atoms()` (`dsl.py`) only
counted a `Cmp`/`Between` node in `then` when its variable had
`role == "action"`. "Systolic BP" correctly binds as `role="observation"`
(it's a lab), so the threshold that IS this statement's entire
recommendation didn't count, and the rule was rejected as having no action.

**First attempt was wrong, caught before implementing further.** The
initial design relaxed `_action_atoms` to accept a numeric threshold for
`avoid`/`hold`/`monitor` whenever the registry had NO action-role variable
bound at all (`not reg.actions()`). Re-run live, this still failed: `:0002`
binds `mechanical_thrombectomy` as `role="action"` (procedures get that role
by convention), so `reg.actions()` was non-empty and the guardrail never
engaged -- even though `mechanical_thrombectomy` is only ever used as the
`Since` baseline event, not a dropped action. `not reg.actions()` couldn't
tell "referenced elsewhere, appropriately" apart from "sitting unused,
suspiciously" (the actual PMC13270348:0143 shape the check exists to catch
-- see `test_the_recommended_action_going_missing_is_flagged`).

**Fixed properly**: replaced the registry-wide check with one computed from
the whole rule, not just `then`: `_no_action_dropped = all(n in
(_vars_in(rule.when) | _vars_in(rule.then) | set(events_in(rule))) for n in
reg.actions())` -- true only when every action-role variable is referenced
*somewhere* in the rule, whether as a decision atom or a Since/Delta anchor.
Verified: all 3 raw `formalize()` attempts for `:0002` now validate with
zero issues, and the full `gate.triage()` pipeline returns a clean ENCODE
matching gold exactly (`when: Since(mechanical_thrombectomy, 1 day)`,
`then: systolic_bp_mmhg < 140`, `action: avoid`). 155/155 offline tests
still pass, including both the action-atom test and the epidural-drop test
this fix was specifically checked against.

### R4. `modal_only` didn't recognize discretion phrased as a noun/process -- FIXED 2026-09-01

PMC13054209:0005 ("Deprescribing should be a preference-sensitive decision,
requiring a shared decision-making approach") should REFUSE as `defeasible`
-- as explicitly discretionary as guideline language gets -- but returned
PARTIAL: `then: has_preference_sensitive_decision AND
shared_decision_making_approach`. `dsl.modal_only()` already existed to
catch exactly this class of mistake ("may be considered" -> `has_may`,
excluded from becoming a hole because nothing will ever populate it), but
only recognized modal VERBS ("may," "should") and hedging ADJECTIVES
("appropriate," "indicated") via token-by-token matching -- not the same
discretion expressed as a noun phrase, which no individual token in it is
hedging on its own.

Fixed by adding `MODAL_PHRASES` (`dsl.py`), a short list matched by
substring against the stemmed name -- the same style `VAGUE_HINTS` already
uses elsewhere, rather than extending the stricter all-tokens-must-match
approach that couldn't catch this shape at all. Verified directly:
`dsl.modal_only()` now returns `True` for `has_preference_sensitive_decision`,
`shared_decision_making_approach`, and other phrasings of the same two
concepts -- confirmed at the unit level, deterministically, independent of
any live call. 155/155 offline tests still pass.

**Caveat, for honesty:** a live re-run of `:0005` after this fix still
returned REFUSE, but via `unbound_concept` rather than `defeasible` --
"Deprescribing" and "shared decision-making" got refused at the BINDER
stage this time (a `procedure`-kind lookup failing to bind), before the
pipeline ever reached the formalize step this fix guards. Same underlying
item 2 (single-shot binder variance) intervening on a different call this
run, not a failure of this fix -- the unit-level proof is the reliable
evidence here, not the one live sample.

### R5. Generic reason codes masking a more informative one -- three distinct mechanisms, all fixed 2026-09-02

Was three bundled mechanisms under one symptom, not one bug -- re-diagnosed
before fixing anything:

- **A.** The `if not bindings:` fast path never calls `formalize()`, so no
  model reasoning is ever available for statements where nothing binds at
  all -- it just picks `unbound_concept`/`not_patient_level` blind.
- **B.** A real mechanical bug: the final refusal's reason came from
  `best = decls[0] if decls else None` (`gate.py`) -- literally the FIRST
  of K parallel, independently-sampled self-declared refusals, in
  submission order. Not the best one, not a vote -- arbitrary, whichever
  call happened to be listed first. `_best()` already exists to pick among
  K candidate RULES; there was no equivalent for K candidate REFUSALS.
- **C.** The real root cause behind the R1-era PMC12948374:0008 example
  (reasoning text said "epistemic caution," reason_code said
  `unbound_concept`, from the SAME declined object): `llm.py`'s
  `FORMALIZE_INSTRUCTIONS` never told the model what the six `reason_code`
  values actually MEAN -- it only ever saw the bare enum names. The correct
  definitions already existed, just as developer documentation
  (`gate.py`'s own header docstring), never ported into the prompt.

Fixed all three:
- A: `gate.py`'s not-bindings branch now reuses `is_vague()` (already
  trusted elsewhere for hole-eligibility) against the refused phrases --
  `vague_predicate` instead of `unbound_concept` when they match
  `VAGUE_HINTS`.
- B: added `_best_refusal()` (`gate.py`, next to `_best()`) -- majority vote
  on `reason_code` among ALL self-declared refusals from BOTH the strict
  and partial-retry attempts (the partial attempt's declines were
  previously discarded entirely -- only its Rules were inspected), tied
  toward a specific code over `unbound_concept`/`not_patient_level`.
- C: added a six-line rubric to `FORMALIZE_INSTRUCTIONS`, adapted directly
  from `gate.py`'s own docstring table, defining each reason code's actual
  scope and the boundary between the easily-confused ones (`unbound_concept`
  vocabulary gap vs `beyond_dsl` structural gap; `vague_predicate` undefined
  threshold vs `defeasible` optional-by-design force).

Verified live against all three concrete cases tracked in this backlog:
- PMC12948374:0008 (the original R1-era example) -- now REFUSE/`beyond_dsl`,
  **exact match to gold**, with excellent reasoning ("diagnostic-
  interpretation guidance... rather than an applicability condition mapping
  to a required care-plan action").
- PMC13270348:0069 -- now REFUSE/`vague_predicate`, **exact match**. The
  `TYPE_ERROR` from before (string value against a boolean var) is gone
  entirely -- the model now correctly names "multidimensional" as undefined
  instead of trying to force it into a comparison.
- PMC13270348:0023 -- still `unbound_concept` vs gold's `vague_predicate`.
  **Known, expected residual**: the actual vague phrase here is "actively
  identified and managed" (the ACTION verb), which never becomes a
  binder-refused CONCEPT at all -- so none of A/B/C ever gets a chance to
  see it. A genuinely different sub-pattern (verb-level vagueness vs.
  concept-level vagueness), not a failure of what was built. Left open, not
  reopened as its own item -- narrow, single observed instance, doesn't
  change verdict correctness, only reason-code precision.

155/155 offline tests pass throughout.

### R6. Age thresholds had no path to a numeric variable -- FIXED 2026-09-02

PMC12948374:0000 ("ABPM from age 5 is strongly recommended") -- should
ENCODE, gold's own clean example -- returned PARTIAL with `has_age_5` as a
boolean hole instead of a real `age >= 5` comparison. Age is a structural
patient attribute, not a terminology-coded concept: there's no generic
"patient age" SNOMED/LOINC code to search for, so it fell through to a
boolean hole the same way a dose or duration did before `dose_companions()`
existed for those. A real `age` VarDef (`type="real", unit="years",
plausible=(0,120)`) already existed, by hand, in `CKD_REGISTRY` -- just
never reachable from the live binding pipeline for an arbitrary document.

Fixed by adding `age_companion()` (`gate.py`), mirroring `dose_companions()`
exactly: a small regex (`_AGE_THRESHOLD`) detects a stated age threshold
("age 5", "over 65 years", "under 12 years", "aged 18") and declares the
standing `age` variable whenever one is found, independent of any
terminology lookup. Verified directly and deterministically: fires
correctly on the target statement and five other realistic age-threshold
phrasings, stays silent on unrelated numbers (dose percentages, monitoring
schedules) -- no false positives. 155/155 offline tests still pass.

**Caveat, for honesty:** two live re-runs of `:0000` after this fix both
still refused -- not because of anything age-related, but because "clinic-
based readings" (from the statement's bundled RATIONALE clause: "ABPM is
more strongly correlated with LVH than clinic-based readings") consistently
failed to bind, both times, blocking the whole statement before the age fix
could show its effect end-to-end. Not random noise -- it reproduced
identically -- and it maps onto item 3 (multi-clause bundling): gold's own
note on this exact statement says it folds "recommendation + rationale +
comparison" into one table row, and the rationale clause is pulling in a
concept unrelated to the actual recommendation. Same shape as R4's `:0005`
caveat: the fix is correct and verified at the unit level; live
confirmation on this specific statement is currently blocked by a separate,
already-tracked issue, not a failure of this fix.

### R7. `classify_unbound` re-checked a phrase with NO sentence context -- FIXED 2026-09-02

PMC13090725:0503 ("avoid holding their breath during the exertion phase of
weightlifting...") should REFUSE -- gold's own note is explicit: this isn't
recorded in any real health record, it's a technique cue, not clinical
data. Instead returned PARTIAL, promoting "Exhaling" (an exact term match,
`overlap=1.00`) and "Breath holding attack" (a pediatric behavioral
condition, 50% overlap) into holes. Two prior fix attempts failed on closer
inspection: a stricter lexical-overlap requirement changed nothing (these
candidates were never lexically weak), and restricting the rescue to
`kind in {"drug","drug_class"}` was caught before implementing -- it would
have regressed `PMC13074134:0010`'s "at start LRD" (`procedure`-kind, this
session's first fix), which has the identical shape of weak-but-real
candidates.

**Measured before implementing anything, per the plan.** `bind.select()`
already asks the model for a free-text `reason` WITH the sentence in view
on every call -- but `classify_unbound` never used it, re-deriving a guess
from the bare phrase alone instead. Tested two designs against a known
6-phrase set (3 that must stay rescuable, 3 that must not) before writing
production code for either:
- Explicit confidence field on `select()`'s existing schema: **6/6 clean
  separation**, cheaply ($0.036, 6 calls). Every should-rescue phrase
  answered `data_capturable=True`, every should-NOT-rescue phrase answered
  `False` -- including correctly noting that "Exhaling" exists as an exact
  match but doesn't apply here, given the sentence.
- (K-sample consistency voting was the other candidate; not tested, since
  the first design already gave an unambiguous, cheap result.)

Implemented: `SELECT_SCHEMA` (`bind.py`) gained a required `data_capturable`
boolean, asked directly in `SELECT_INSTRUCTIONS` ("even though none of the
candidates matched, is this the kind of fact a structured health record
would ever capture?"). `bind()` now carries the answer straight onto the
`Refusal`'s `evidence` field instead of leaving it implicit.
`classify_unbound` (`gate.py`) reads that field first, falling back to the
old blind re-search only for refusals that never went through `select()`
(e.g. a code rejected post-selection by `index.validate`). The offline
heuristic fallback (`_heuristic_select`) defaults to the OLD behavior
exactly (`False` only when there were zero candidates at all), so
offline/test-mode behavior is unchanged -- only the live path, where a real
model can actually answer the question, changes.

Verified live, both directions (mandatory given the near-miss regression
above): `PMC13090725:0503` now REFUSEs correctly. `PMC13074134:0010`
("at start LRD") and `:0018` (all 8 drug brands) both still PARTIAL exactly
as before, full `Delta` rules intact -- zero regression on the session's
first and most important fix. 155/155 offline tests pass throughout.

## Open

### 1. Grammar-too-large ceiling: event cap helps, doesn't fully close it

`gate.py`'s `MAX_DECODE_VARS = 10` was empirically calibrated (R8) against
variable-only registries with no events. This session added an equivalent
cap on the event enum (previously fully uncapped) -- a real, verified fix
for the "events pass through with zero limit" gap. But live testing on
PMC13074134:0018 (8 drug-brand mentions) still showed ~13% of live calls
hitting "grammar too large" afterward, because the cap never even engaged
(the statement's event count was already under 10) yet failures persisted
anyway.

Working theory, not yet verified: a var-enum near its own cap of 10 AND an
event-enum near its own cap of 10 *simultaneously* can still exceed the real
ceiling, because R8's original number was measured with no events present at
all. A shared/combined budget across vars+holes+events (rather than two
independent caps of 10 each) is the likely next step, but picking a new
number needs its own live calibration -- don't just guess a smaller number.

### 2. Single-shot LLM calls with no retry/vote, at two different stages

The root cause of two separate bugs found this session (the PMC13074134
ENCODE/REFUSE flip, and PMC13151662:0002's spurious "lowering systolic BP"
mention) is the same underlying architectural gap: `bind.select()` (concept
selection) and `bind.propose_mentions()` (mention extraction) are each a
single, un-retried, un-voted model call. `gate.triage`'s `samples=` only
covers the *formalize* step; nothing upstream of it gets retried or voted.

Two of the specific symptoms this caused are now fixed (the
`unbound_unselected` hole-eligibility fix, and the mention-instruction
clarification for threshold statements). The general fragility is not fixed
and wasn't attempted -- it would mean adding K-sample voting to binder-stage
calls, which has real cost (multiplies calls per concept mention) and no
existing calibration showing it actually helps at this stage (the one
calibration study in this codebase, cited in `gate.py`'s docstring, was
specifically about the *formalize* stage, not binding). Explicitly deferred,
not silently skipped.

A specific, confirmed consequence (was open item 8, closed after checking
the full evidence trace): on PMC13151662:0008, "correction of reversible
contributors to hypotension" doesn't appear anywhere in the output -- not in
the registry, not as a binder refusal, not flagged by `DROPPED_CONCEPT_RISK`.
It was never proposed as a mention at all by that run's single
`propose_mentions()` call. That's a real blind spot worth naming precisely:
`DROPPED_CONCEPT_RISK` (`gate.py:368-388`) only guards against a concept
that WAS proposed and then refused -- it structurally cannot catch a concept
that mention-extraction never noticed in the first place. Not a new root
cause, just a sharper description of what single-shot mention proposal
actually costs.

### 3. Multi-sentence "statements" bundling narrative + one directive clause

PMC13151662:0014 refused with the right verdict and the right reason code,
but naming the wrong concept -- it named an unbound concept from the
background-narrative sentences rather than the one actually blocking the
directive clause at the end. Gold's own annotator flagged this exact
statement as "a segmentation problem as much as a formalization one." Known,
not new, not scoped for a quick fix -- would mean the segmenter splitting a
paragraph like this into narrative + directive as separate statements,
which is a bigger change than anything touched this session.

### 4. `README.md` is gone from the repo root

Removed by a teammate's restructuring commit at some point before this
session. Run instructions now live only in `Part B/CHECKER.md`. Nobody has
decided whether to recreate a root README or leave documentation split this
way -- open question, not a bug.

### 5. A phrase can fail to bind because the index has only MORE SPECIFIC candidates than what the sentence generically said (the mirror image of R7)

PMC13270348:0024 ("we recommend epidural analgesia... provided local
expertise is available") should PARTIAL (epidural analgesia is a real,
bindable action; "local expertise available" is an institutional-capability
hole gold itself flags as a boundary case). Instead: REFUSE/`unbound_concept`.
The refusal reasoning is precise about why: "the statement refers to
epidural analgesia generically (any route/level, continuous or PCEA). No
candidate is a generic 'epidural analgesia' concept" -- the index only has
SPECIFIC variants (by route/level), and none of them is a fair match for the
sentence's deliberately generic phrasing, so binding correctly fails.

R7 was a context-blind check letting a WRONG candidate rescue a phrase it
shouldn't -- fixed by having `select()` report `data_capturable` directly,
with the sentence in view. This is the same underlying gap producing the
OPPOSITE symptom: a RIGHT concept failing to bind because reality's
terminology is more granular than the guideline's own wording.

R7's fix may partially help this case (untested) -- `data_capturable` for
"epidural analgesia" would very plausibly be True (it's a real bindable
concept, not a technique cue), which would at least promote it to a hole
instead of a hard refusal, turning this into a PARTIAL. But that's a lesser
outcome than gold's ideal (epidural analgesia as a properly bound ACTION
variable, not a boolean hole) -- full resolution would need something more:
recognizing that a generic phrase over several specific same-family
candidates (route/level variants of the same procedure) should bind to the
FAMILY, not abstain entirely. Not investigated further this session.

## Known, not pursued further

### 6. Pre-existing PDF/pymupdf test failures

`test_segment.py::test_pdf_box_recovery_finds_the_callout` and
`test_pdf_statements_carry_page_and_bbox` fail with
`AttributeError: 'Rect' object has no attribute 'get_area'`. Confirmed
(earlier session, via a clean `origin/main` worktree) to be a pre-existing
pymupdf version mismatch, independent of anything touched in these sessions.
Means PDF ingestion is currently untested in this environment, but out of
scope here.
