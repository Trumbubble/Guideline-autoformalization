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
item 1 (single-shot binder variance) intervening on a different call this
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
identically -- and it maps onto item 2 (multi-clause bundling): gold's own
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

### R8. `README.md` was gone from the repo root -- FIXED 2026-09-02

Removed by a teammate's restructuring commit at some point before this
session; run instructions lived only in `Part B/CHECKER.md`, findable only
if you already knew to look there. Added a short root `README.md`:
project description, setup (`venv` + `.env`), the run command, and pointers
to `CHECKER.md`/`DSL.md`/`BACKLOG.md` rather than duplicating them.
Deliberately kept minimal, not a rewrite of `CHECKER.md`. Verified the
documented commands actually work -- started the server exactly as written
and confirmed the dashboard, `/demo`, and `/check` pages all return 200.

### R9. `is_patient_level` was silently dropped once any concept binds -- FIXED 2026-09-02

Found live retesting PMC13131131:0060 ("Patient education should be
evidence-based, verifiable and accessible..." -- gold: REFUSE/`not_patient_level`).
`gate.triage()` takes an `is_patient_level` keyword, set upstream by the
segmenter, but only ever consulted it in ONE place: the early shortcut for
when nothing binds at all. The moment even one concept binds -- `:0060`
binds `patient_education` -- the statement proceeds into `formalize()`,
which is never told `is_patient_level` (not in its signature, not in its
prompt), and the final refuse-code-selection block only overrode `code` for
two other signals (`MODAL_PREDICATE`, `VACUOUS_RULE`), never this one. A
known, correct, upstream fact was silently dropped the moment any binding
succeeded -- confirmed live: `:0060` still returned `vague_predicate` even
with `is_patient_level=False` passed correctly, before this fix.

Checked against every other open item first (no code changed until this was
clear): distinct from item 2 (misattributes which CONCEPT blocks a
statement -- a vocabulary-layer problem) and item 3 (a real concept fails to
bind -- also vocabulary-layer). This bug is orthogonal to vocabulary
entirely; it's a pre-computed scope fact getting dropped. Also distinct from
R5, despite touching the same code block -- R5 fixed the model not knowing
what the six reason codes MEANT; this fixes a known fact never reaching that
decision at all.

Fixed: `is_patient_level is False` now checked FIRST in the final
refuse-code-selection block (`gate.py`), ahead of `MODAL_PREDICATE`/
`VACUOUS_RULE` -- if a statement isn't about an individual patient at all,
that's the more fundamental reason it can't become a Track-B rule,
regardless of what else went wrong encoding it. Same priority order already
used in the early nothing-bound branch. New test
`test_a_refusal_after_binding_still_reports_not_patient_level`
(`test_gate.py`) covers the binding-succeeds path the bug was in --
mirrors the existing `test_an_empty_binding_is_never_labelled_not_patient_level_by_guess`,
which only covered the nothing-bound path. 222/222 offline tests pass (was
221/221; the two pre-existing pymupdf failures are item 5, unrelated).
Verified live: `:0060` now returns REFUSE/`not_patient_level`, exact match
to gold.

**Flagged, not acted on:** whether `is_patient_level is False` should also
block a successful ENCODE/PARTIAL outcome earlier (before `formalize()` is
even attempted), rather than only correcting the reason code after a refusal
already happened. No case has been observed where a not-patient-level
statement produces an ENCODE -- extending the fix that far would be
unevidenced speculation. Worth a look if such a case ever turns up.

### R10. A correct "not a recommendation" verdict was discarded for returning an empty quote -- FIXED 2026-09-02

Found live retesting PMC13131131:0044 ("Tebentafusp has been approved by
the FDA and the EMA as first-line systemic treatment...") -- gold:
NOT_A_RECOMMENDATION. `segment.classify()`'s verbatim-quote check reads
`if not quote or quote not in block.source_text:` and rejects the whole LLM
verdict if it trips, falling back to the offline heuristic. Captured the
model's actual raw response: `{'quote': '', 'is_recommendation': False,
'is_patient_level': False, 'reason': 'Describes regulatory approval status
and ongoing trials; no directive clinical action.'}` -- the model was
CORRECT, exact match to gold. But a model that correctly says "nothing to
quote" returns `quote=""`, and `not ""` is `True` in Python -- so a
genuinely right answer tripped the same guard meant to catch an invented,
non-verbatim quote, and got thrown away in favor of the heuristic's wrong
guess (`is_recommendation=True`, matching R1-era "approved... as
first-line" reading like treatment language).

Fixed: the verbatim check now only applies when the model actually claims a
recommendation --
`if v.get("is_recommendation") and (not quote or quote not in
block.source_text):` -- an empty quote paired with `is_recommendation=False`
is accepted as-is; a claimed recommendation still needs a real, verified
verbatim quote exactly as before. New test
`test_empty_quote_is_accepted_when_not_a_recommendation` (`test_segment.py`).
223/223 offline tests pass (was 222/222; same 2 pre-existing pymupdf
failures, item 5, unrelated). Verified by replaying the exact captured model
output through the fixed `classify()` -- no new live call needed --
confirming `judge="llm"`, `is_recommendation=False`, matching gold.

### R11. `dose_companions()` mistook a timing window for a dose -- FIXED 2026-09-02

Found live exploring PMC13169229 (a balloon-gastrostomy-tube replacement
guideline, chosen at random). "we recommend waiting 6-12 weeks after BGT
placement for the initial replacement" declared nonsensical `bgt_dose` and
`bgt_placement_dose` companion variables on a statement with no dose
anywhere in it. `_QUANTITY_RANGE` (`gate.py`) matches any stated numeric
range in an accepted unit, including bare durations ("weeks", "days",
"hours") -- and nothing distinguished "150-240 min OF aerobic exercise" (a
quantity of the bound action itself, R2's original case, `Delta`/`Between`'s
domain) from "6-12 weeks AFTER BGT placement" (a window relative to a prior
event, `Since`'s domain, structurally unrelated).

Fixed: `_QUANTITY_RANGE` now has a negative lookahead excluding a match
immediately followed by `after`/`before`/`following`/`post`/`prior to`.
Checked against every statement in `gold.csv` first -- none currently
matches this pattern in either direction, so this is a pure precision gain,
not a measured tradeoff against the existing corpus. New test
`test_a_range_after_an_event_is_not_a_dose` (`test_gate.py`), which also
re-confirms R2's original "150-240 min of aerobic exercise" phrasing still
fires correctly. 224/224 offline tests pass (was 223/223; same 2
pre-existing pymupdf failures, item 5, unrelated). Verified by replaying the
exact captured statement text through the fixed regex -- no new live call
needed -- confirming `dose_companions()` no longer fires on it.

Harmless to any final DECISION either way -- the wrongly-declared companion
always went unused, since `formalize()` correctly never had a dose to put
in it -- but polluted the evidence trail with confusing, wrongly-named
variables a reviewer would have had to puzzle over.

### R12. Grammar-too-large ceiling -- FIXED 2026-09-03

Item 1's original framing (~13% of calls on one case, PMC13074134:0018)
understated it badly. Grepping every saved log from this session for the
actual `BadRequestError` text found **46 failed calls across 16 different
verification runs** -- this was a routine, recurring failure across nearly
every live round this session, not one narrow edge case. Every occurrence
was `formalize()`'s compiled-grammar-size limit, the only call whose
schema scales with a `Registry`.

Two fixes, in the order they were actually found:

**Reactive (built first):** `gate._attempt()` now catches `formalize()`'s
`GrammarTooLarge` (recognized by message text, not exception class --
`gate.py` stays decoupled from `llm.py` so it can be tested offline with a
fake `formalize`) and retries once via `_shrink_registry()`, which drops
half the unbound holes/events and keeps every BOUND var untouched (a bound
var is the statement's actual substance; dropping one is the exact silent
omission `DROPPED_CONCEPT_RISK` exists to catch). Turns a lost statement
into a smaller-but-sound encoding instead of a misleading `beyond_dsl`.
Two new offline tests (`test_grammar_too_large_shrinks_the_registry_and_retries`,
`test_grammar_too_large_gives_up_cleanly_when_nothing_left_to_shrink`,
`test_gate.py`).

**Proactive (the one that actually moved the failure rate):** a first
design -- a hand-derived formula weighting variable/event name-length by
which DSL leaf type references it, times `(max_depth + 1)` -- was drafted,
then independently reviewed and **rejected before being implemented**: it
could not fit this project's own existing calibration data (`RESULTS.md`'s
R8 table has a depth-3 row no formula of that shape can match -- no
multiplicative model can produce "3 variables fails at depth 3" while "13
variables succeeds at depth 1," which R8 already measured), and it
targeted the wrong code path (`gate.py`'s registry caps only apply in the
PARTIAL-retry phase; the STRICT attempt, where the real 39-variable
failure case almost certainly happened, was never capped at all).

What actually fits every known anchor: `len(json.dumps(decoding_schema(reg,
depth)))` -- literal compiled-schema byte length, additive in depth
(~629 bytes per level, independent of registry size) plus a small
per-character cost per enum member, names counted once. This matches
`decoding_schema()`'s own design comment (leaves are `$ref`'d, not
inlined, specifically so depth doesn't multiply the grammar) and means
**one extra decode depth level costs about as much as 39 short variables**
-- registry trimming was never going to be the lever that mattered.
Replaying `PMC13074134`'s own recorded rules found none ever nested past
depth 1, even though `formalize()` always started at
`dsl.MAX_DECODING_DEPTH` (2).

Fixed: `llm.formalize()`'s default starting depth changed from 2 to 1
(`depth = 1 if max_depth is None else max_depth`) -- a one-line,
near-zero-risk change; the existing depth-down retry loop still reaches
depth 2 if a caller explicitly asks via `max_depth`, and the function's
own design already argues a shallower encode is sound (anything that
genuinely needed the depth surfaces as a validation error, not silently).
226/226 offline tests pass throughout (unaffected -- `formalize()` isn't
exercised by the offline suite). Verified live: re-ran the same 5-statement
batch that had 2/30 grammar failures four times total -- 2/30 before the
fix, then **0/30, 0/29, 0/30 after** -- zero failures across 89 calls,
not one lucky run.

**Checked, not conflated:** individual statement decisions across those
same runs remained just as volatile as before (one statement landed on 5
different outcomes across 5 runs, including a bare ENCODE once) --
confirmed this is item 1's pre-existing single-shot variance, upstream of
and unrelated to this fix, not a regression it introduced.

The elaborate pre-flight size estimator (steps 2-4 of the plan that
produced this fix) was scoped but not built -- the depth-1 change alone
closed the gap in live testing, so the more complex machinery wasn't
needed. Worth revisiting only if a future document reproduces the failure
again despite this fix.

## Open

### 1. Single-shot LLM calls with no retry/vote, at two different stages

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

### 2. Multi-sentence "statements" bundling narrative + one directive clause -- narrower than it looked; live pipeline doesn't reach it

PMC13151662:0014 refused with the right verdict and the right reason code,
but naming the wrong concept -- it named an unbound concept from the
background-narrative sentences rather than the one actually blocking the
directive clause at the end. Gold's own annotator flagged this exact
statement as "a segmentation problem as much as a formalization one."

Investigated further 2026-09-02, live, before attempting any fix.
`segment.py`'s classifier (`classify()`) was tested directly against the real
sentence-splitter unit for this statement (and for PMC13131131:0040, a
second case with the same shape): 12 live calls, 100% consistent, it already
quotes ONLY the directive sentence, dropping the leading narrative/rationale
sentence -- with no code change. Feeding that narrow, real segmenter output
through `gate.triage()` end-to-end does NOT reproduce the wrong-concept
symptom for either statement -- both instead **ENCODE successfully**. (An
earlier pass through this same check found both refusing on
`MISSING_PROVENANCE`; that turned out to be a test-harness mistake -- an
omitted `source_span=` argument, which `gate.triage()` forcibly stamps onto
every produced rule, spuriously forcing `MISSING_PROVENANCE` regardless of
the statement. Not a pipeline bug; retracted once caught.) :0040 matches
gold's ENCODE label; :0014 actually
diverges from gold's REFUSE call by successfully binding
`has_undifferentiated_stroke` -- plausibly because the `terms.db` rebuild
(R1) added coverage that did not exist when gold's annotator made that call.
Not investigated further. The wrong-concept symptom only reproduces when
gold.csv's own wide, hand-authored text (built by design without running the
segmenter, per `GOLD.md`'s methodology, as an independent formalization-only
ground truth) is fed to `triage()` directly -- which is how the original
finding was made, not how the live pipeline behaves.

So: not a live bug today for these two cases -- segmentation already avoids
ever constructing the wide multi-sentence input that triggers it. But the
underlying weakness this exposed is real and still unfixed: when a
statement's text DOES legitimately span multiple sentences with multiple
concept mentions (a genuinely bundled recommendation, not a segmentation
failure -- e.g. a table row, or a guideline that states its rationale and
directive in one sentence), `bind()`/`gate()`'s refusal-reason selection has
no logic to prefer the mention that's actually inside the directive clause
over one that's merely earlier or more prominent in the text. Just unreached
by anything in the current corpus's live segmenter output. A prompt-only
`CLASSIFIER_INSTRUCTIONS` fix was drafted and live-tested against both cases,
then reverted -- it changed nothing measurable, since the baseline
instruction ("if the recommendation is only part of the candidate, quote
exactly that part") was already being followed reliably.

### 3. A phrase can fail to bind because the index has only MORE SPECIFIC candidates than what the sentence generically said (the mirror image of R7)

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

### 4. Mechanism-based drug CLASSES ("ICI", "TKI") are unreachable -- `drug_class`'s real vocabulary (ATC) was never loaded, and there is no loader for it

Found live exploring PMC13034276 (SITC immunotherapy guideline, chosen at
random to look for new issues). "ICI therapy" and "TKI-based regimen" both
refused/held-out as unbound `drug_class` concepts. Traced all the way to
root cause before proposing anything -- three compounding layers, each
independently sufficient to block it:

1. **Abbreviation matching is exact-whole-phrase-only.** `segment.py`
   already extracts `"ICIs": "Immune checkpoint inhibitors"` and
   `"TKIs": "tyrosine kinase inhibitors"` straight from this document's own
   text, and `bind_statement()` does thread that glossary through to
   `search_all()`. It still never fires: the lookup requires the ENTIRE
   mention text to equal a dictionary key verbatim. `"ICI therapy"` (a
   multi-word mention containing the abbreviation) doesn't equal `"ICIs"`,
   and even the bare word `"ICI"` doesn't either -- the extracted key is the
   plural `"ICIs"`, and the lookup does no singular/plural normalization at
   all, confirmed directly: `expand_query("ICI", abbrevs)` returns `['ICI',
   'ici']`, the glossary hit never fires.
2. **`KIND_SYSTEMS["drug"] = (RxNorm, VSAC)` excludes SNOMED, by
   documented design** (`BIND.md`'s own sources table: `drug, drug_class ->
   RxNorm / ATC`, present since the first commit, confirmed via `git
   blame`). Not a bug -- but it means the row for `Immune checkpoint
   inhibitor` and `Protein-tyrosine kinase inhibitor` (both real, both
   sitting in `terms.db` right now, both SNOMED-sourced) can never be found
   by any `drug`-kind search, however it's phrased. Checked how much of the
   loaded table this actually costs: **110,260 of 171,231 `drug`-kind rows
   (64%) are SNOMED-sourced and structurally unreachable** by this
   restriction alone.
3. **`drug_class`'s real vocabulary was never loaded.** Unlike SNOMED,
   LOINC, and RxNorm (`load_snomed()`, `load_rxnorm()`, `load_loinc()` --
   all real bulk loaders, all exercised by R1's rebuild), there is **no
   `load_atc()` anywhere in this codebase** -- confirmed by a full-codebase
   search, zero matches. `drug_class` has exactly 99 rows total in the real
   `terms.db` (19 ATC + 80 VSAC); R1's own writeup recorded this number
   without flagging it, since the near-zero-everywhere problem it was fixing
   dwarfed it at the time. This is the SAME shape of gap R1 already fixed
   for the other three systems -- just missed for ATC, likely because
   (unlike SNOMED/LOINC/RxNorm) WHO's ATC/DDD data isn't freely
   redistributable the same way, so there was nothing free to point a
   loader at.

**Why ICI and TKI behaved differently, given the identical root cause:** ICI
has a second entry filed under `kind='procedure'` (SNOMED, which
`procedure` *does* allow) -- `idx.search('ICI therapy', 'procedure', k=8)`
finds it cleanly as the #1 hit, no abbreviation fix even needed. TKI has no
such escape hatch: its only entry is `kind='drug'`/SNOMED, and there is no
ATC or RxNorm class-level entry for it in this database at all.

**Candidate fixes, tested offline against real `terms.db` data (no API
calls, no code changed yet):**

- Fixing the abbreviation-matching gap (item 1 above) alone: necessary but
  NOT sufficient -- generates the right expanded query text, but the search
  still can't reach a SNOMED-only row.
- Adding `procedure` to `KIND_FALLBACK["drug_class"]`: cleanly rescues ICI
  (verified). Does nothing for TKI (no procedure-kind entry exists for it).
- Widening `KIND_SYSTEMS["drug"]` to include SNOMED, combined with the
  abbreviation fix: rescues BOTH ICI and TKI cleanly, verified directly
  (`Immune checkpoint inhibitor` and `Protein-tyrosine kinase inhibitor`
  both land as unambiguous #1 hits). **But it has a real, demonstrated
  regression cost**, checked against a known-good case from earlier this
  session: `"basal insulin"` currently correctly surfaces the specific
  long-acting analogs (`insulin glargine`, `insulin detemir`, `insulin
  lispro`); widened, a bare generic SNOMED `Insulin` concept (via the same
  head-noun-backoff mechanism that surfaced `Events` for the "irAEs" case
  earlier in this same investigation) outscores them and crowds them out of
  the candidate list entirely, losing the clinically meaningful "basal"
  qualifier. Widening
  `drug` to compete on equal footing with SNOMED helps the empty cases and
  actively hurts cases that already work.
- **Proposed, not yet built or tested:** treat SNOMED-under-`drug` as a
  RESERVED, lower-priority fallback -- only consulted when the RxNorm/ATC
  search comes up empty -- mirroring the pattern `search_all()` already
  uses for its semantic layer ("APPEND -- never evict... a fixed share of
  the list is honest about there being two rankings"). Should rescue TKI
  (RxNorm/ATC find nothing there) without ever touching `"basal insulin"`
  (RxNorm already finds good candidates, so the reserve never triggers).
  This is the most promising direction but is unverified.

**Real fix, mirrors R1 exactly:** get real ATC data into `terms.db` and
write `load_atc()`. Everything above is retrieval-layer mitigation around a
vocabulary that was never actually loaded -- the same class of problem R1
solved for the other three systems. Needs a data source, not a code change;
not something to do unilaterally.

## Known, not pursued further

### 5. Pre-existing PDF/pymupdf test failures

`test_segment.py::test_pdf_box_recovery_finds_the_callout` and
`test_pdf_statements_carry_page_and_bbox` fail with
`AttributeError: 'Rect' object has no attribute 'get_area'`. Confirmed
(earlier session, via a clean `origin/main` worktree) to be a pre-existing
pymupdf version mismatch, independent of anything touched in these sessions.
Means PDF ingestion is currently untested in this environment, but out of
scope here.
