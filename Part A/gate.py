"""
Refusal gate: can this statement be faithfully encoded, partly, or not at all?

Stage 3 of Track A, and the one with an asymmetric cost. A false REFUSE costs
coverage and shows up honestly in the residue report. A false ENCODE produces a
silently wrong checker -- so the gate is thresholded for precision on `encode`,
and whatever recall that buys is what it buys.

WHAT THIS IS NOT. It is not an LLM asked "is this vague?". That is circular
(arXiv:2607.04655 puts purpose-built diagnostic models at ~0.71 F1 on error
categorisation). The gate AGGREGATES deterministic evidence first -- what the
binder could not resolve, what the validator rejects, what the solver shows to
be degenerate -- and asks a model to encode only after that, then checks what
comes back.

But be precise about how much that buys, because the earlier claim here ("most
of the decision is already made by machinery stages 1-2 produce") was not true.
Of the six refusal codes, ONE is deterministic evidence from stages 1-2
(`unbound_concept`) and one more is deterministic from this stage's validator
(`defeasible`). The rest need the model, either to decline or -- for
`not_patient_level` -- to have classified the statement upstream. The
deterministic machinery is a GATE ON the model's answer, not a replacement for
it.

  reason_code            who decides           what actually sets it
  ---------------------- --------------------- -----------------------------
  unbound_concept        DETERMINISTIC         `bind.bind()` when the selector
                         (binder)              returns -1, no candidate
                                               survives, or a value set
                                               expands to no codes; and the
                                               fallback here when a statement
                                               refuses with binder refusals
                                               outstanding.
  defeasible             DETERMINISTIC         MODAL_PREDICATE among the
                         (validator)           blocking codes -- the guideline
                                               hedged, so the "predicate" is
                                               its deontic force wearing a
                                               patient fact's clothes.
                                               The model may also name it.
  beyond_dsl             MIXED                 VACUOUS_RULE deterministically;
                                               otherwise the model's own
                                               refusal code, or the fallback
                                               when nothing bound-specific
                                               applies.
  not_patient_level      MODEL                 `is_patient_level is False`,
                         (segmenter's          which is the SEGMENTER'S
                          classifier)          CLASSIFIER -- an LLM call on a
                                               live run, heuristic only when
                                               it errors or its quote is not
                                               verbatim. Never derived here.
  vague_predicate        MODEL                 the formalizer declining.
  unsupported_temporal   MODEL                 the formalizer declining. NO
                                               deterministic check produces
                                               it: there is no fragment test
                                               for "multi-visit", only the
                                               model recognising one. Seen
                                               twice across the real bundles,
                                               both model-authored.

THIS TABLE WAS WRONG FOR A LONG TIME, and being wrong here is expensive: it is
what an architecture diagram gets drawn from, so the error propagates into
anything describing the system. It claimed `not_patient_level` came from
"obligation names no action" (it does not -- NO_ACTION_ATOM is a validator
error that causes rejection, and never sets this code) and that
`unsupported_temporal` was deterministic (nothing assigns it). Both read as
guarantees the pipeline does not make. `test_gate.py` now pins which codes are
reachable without a model, so the claim cannot drift again.

MEASURED, AND IT SHAPED THIS FILE. On 20 statements x K=8 real samples, all 80
vague samples were REFUSED and 0 of the encoded statements produced more than
one semantic reading. So separation comes from refusal, not from disagreement:
one constrained decode does what a K-sample ambiguity estimator was supposed
to, at an eighth of the cost. K>1 is kept only as the CONFIDENCE estimator
(`refusal_rate`), because a risk-coverage curve needs a score.

PARTIAL is the interesting verdict. "Refer if eGFR < 30 or the patient is high
risk" has one clause that binds and one that does not. Stubbing the second as
`false` (what PathWISE does) silently stops the rule firing for high-risk
patients; `true` over-fires. Instead the hole becomes a genuinely free variable
(`VarDef.unbound`), so the three-valued verdict returns MAY-violate and NAMES
the assumption. That is the comparison E3 is built on.
"""
from __future__ import annotations

import concurrent.futures as cf
import re
import statistics
from dataclasses import dataclass, field, replace
from typing import Callable, Iterable

import bind
import cassette
import dsl

Decision = str          # encode | partial | refuse

# Measured (R8): a 13-variable registry compiles only at nesting depth 1 and a
# 3-variable one reaches depth 2. Past roughly this many the grammar fails to
# compile at ANY depth and the statement is lost.
MAX_DECODE_VARS = 10

@dataclass(frozen=True)
class Evidence:
    """One signal, and where it came from. The gate must be auditable: a
    clinician asking 'why was this refused' gets these, not a score."""
    source: str         # binder | validator | solver | model
    code: str
    detail: str

@dataclass(frozen=True)
class Triage:
    statement_id: str
    text: str
    decision: Decision
    reason_code: str | None = None      # one of dsl.Refusal's six
    fragment: str | None = None         # PROP | LIA | MTL, from fragment_of
    confidence: float = 0.0             # P(encodable); drives the risk-coverage curve
    evidence: tuple[Evidence, ...] = ()
    open_holes: tuple[str, ...] = ()    # free variables a PARTIAL encoding leaves
    rule: dsl.Rule | None = None
    refusal: dsl.Refusal | None = None
    samples: int = 1
    refusal_rate: float = 0.0
    # The registry the rule was ACTUALLY WRITTEN AGAINST. Carried rather than
    # re-derived: binding goes through a model selector, so re-binding the same
    # statement can return a different answer, and a caller that rebuilds the
    # vocabulary for the bundle can drop a variable the rule still references.
    # Measured on PMC13270348: `close_monitoring` bound during triage, did not
    # bind on the rebuild, and the shipped rule referenced a variable the
    # shipped registry did not contain -- so the artifact delivered was not the
    # artifact verified. Track B caught it as UNKNOWN_VAR, which is the net
    # working, not the pipeline working.
    registry: dsl.Registry | None = None

    @property
    def encodable(self) -> bool:
        """The BINARY the gate is calibrated on. Two independent labelling
        passes agreed 28/29 on this call but only 71.8% on the exact fragment,
        so the binary is the headline and the fragment is secondary."""
        return self.decision in {"encode", "partial"}

    def as_refusal(self) -> dsl.Refusal | None:
        """The statement-level refusal, carrying the concept-level detail.

        The evidence is what makes a residue report actionable: "unbound_concept"
        alone cannot be triaged, while "no code for 'deprescribing'" tells a
        reader whether to fix the index or accept the residue."""
        if self.decision != "refuse":
            return None
        ev = tuple(f"[{e.source}/{e.code}] {e.detail}" for e in self.evidence)
        if self.refusal is not None:
            return replace(self.refusal,
                               evidence=self.refusal.evidence or ev,
                               source_span=self.refusal.source_span or self.text)
        return dsl.Refusal(
            self.statement_id, self.reason_code or "beyond_dsl",
            "; ".join(e.detail for e in self.evidence) or "no encoding produced",
            source_span=self.text, confidence=1.0 - self.confidence, evidence=ev)

# ---------------------------------------------------------------- holes

VAGUE_HINTS = ("high risk", "low risk", "adequate", "appropriate", "sufficient",
               "significant", "prolonged", "elevated", "severe", "as needed",
               "if indicated", "clinically", "individualized", "tailored",
               "selected patients", "where feasible", "frail")

def is_vague(phrase: str) -> bool:
    p = phrase.lower()
    return any(h in p for h in VAGUE_HINTS)

def holes_from(statement: str, refusals, declined,
               unbound_kinds: list[str] | None = None) -> tuple[dsl.VarDef, ...]:
    """Which unbindable things deserve to become free variables.

    Three sources, and the distinction matters. A predicate the model NAMES as
    undefined ("at high risk") is a genuine hole. A concept the binder failed
    to retrieve with NO candidates at all is a COVERAGE GAP -- a fixable
    index problem -- and turning it into a hole would launder a bug into a
    finding, so it stays excluded. A concept where candidates DID exist and
    the one-shot selector just didn't commit to one
    (`classify_unbound(...) == "unbound_unselected"`) is closer to the
    model-named case than to a coverage gap: the concept is real, only the
    single call was unlucky, so it qualifies too -- refusing the whole
    statement over one unlucky pick (measured on PMC13074134: "at start LRD",
    and drug brand names like "Abasaglar") was the actual bug.

    Named predicates are checked verbatim against the statement, the same
    anti-synthesis rule the segmenter and binder use."""
    out, seen = [], set()
    for d in declined:
        for phrase in getattr(d, "undefined_predicates", ()) or ():
            p = (phrase or "").strip()
            if (p and p.lower() in statement.lower() and p.lower() not in seen
                    and len(p.split()) <= MAX_HOLE_WORDS):
                seen.add(p.lower())
                h = hole_for(p, "condition")
                # "may be considered" reduces to `has_may`, which is the
                # guideline's deontic force wearing a patient predicate's
                # clothes. Declaring it as a hole makes it worse, not better:
                # a hole is a promise that a data source could one day fill it,
                # and nothing will ever populate "has may". The statement is
                # discretionary; that is a refusal, not a free variable.
                if dsl.modal_only(h.name):
                    continue
                out.append(h)
    for i, r in enumerate(refusals):
        p = (r.source_span or "").strip()
        kind = unbound_kinds[i] if unbound_kinds is not None else None
        if (p and (is_vague(p) or kind == "unbound_unselected")
                and p.lower() not in seen):
            seen.add(p.lower())
            out.append(hole_for(p, "condition"))
    return tuple(out)

def classify_unbound(refusal, index) -> str:
    """`index_gap` when the concept isn't the kind of thing a data source
    would capture (or nothing was even found); `unselected` when it is, and
    a single-shot select() call just didn't commit to a candidate.

    Prefers `refusal.evidence`'s `data_capturable` flag when present --
    carried straight from `select()`, which judged this WITH the full
    sentence in view (see `bind.py`'s SELECT_SCHEMA and BACKLOG.md item 5:
    "did any candidate exist at all" was measured to NOT distinguish a
    genuine coverage gap from a lexically-similar-but-wrong candidate the
    model correctly rejected -- "at start LRD" and "Exhaling" both had
    candidates; only the sentence-aware judgment tells them apart). Falls
    back to the old blind re-search only for refusals that never went
    through that path (e.g. a code rejected by `index.validate` after
    selection)."""
    for e in refusal.evidence or ():
        if e == "data_capturable=True":
            return "unbound_unselected"
        if e == "data_capturable=False":
            return "unbound_index_gap"
    phrase = (refusal.source_span or "").strip()
    if not phrase:
        return "unbound_unknown"
    for kind in ("condition", "drug_class", "drug", "procedure", "lab"):
        try:
            if bind.search_all(index, bind.Mention(phrase, kind), 5):
                return "unbound_unselected"
        except Exception:
            continue
    return "unbound_index_gap"

# An undefined PREDICATE is a phrase, not a clause. Longer spans are the model
# quoting half a sentence, which produces holes like
# `has_pediatric_patients_who_have` -- unreadable in a governance report and
# too coarse to be a variable.
MAX_HOLE_WORDS = 6

# Filler that names the population or the sentence frame rather than the
# predicate. Dropping it turns `has_pediatric_patients_who_have` into
# `has_experienced_trauma`, which is what a governance reader needs to see.
HOLE_FILLER = {
    "the", "a", "an", "of", "in", "on", "at", "with", "from", "for", "to",
    "and", "or", "if", "when", "that", "which", "who", "whose",
    "are", "is", "be", "been", "was", "were", "have", "has", "had",
    "require", "requires", "required", "considered", "receiving", "undergoing",
    "patients", "patient", "pediatric", "paediatric", "children", "child",
    "adults", "adult", "adolescents", "infants", "their", "his", "her",
}

def hole_for(mention: str, kind: str) -> dsl.VarDef:
    """Turn an unbindable predicate into a declared free variable rather than a
    silent omission. It has no binding and no plausible range on purpose."""
    # Strip leading filler so the variable names the PREDICATE, not the
    # sentence around it: "patients who are critically ill" -> has_critically_ill.
    words = [w for w in mention.lower().split() if w not in HOLE_FILLER]
    name = bind.KIND_PREFIX.get(kind, "") + bind._slug(" ".join(words) or mention, 5)
    return dsl.VarDef(name=name, type="bool", description=mention,
                      role="observation", unbound=True)

# Concept kinds that name something DATABLE -- something with a moment it
# happened, which a temporal node can anchor to.
_DATABLE_KINDS = {"procedure", "drug", "drug_class"}

def datable_events(bindings) -> list:
    """EventDefs for the bound concepts a temporal node can measure from.

    Nothing ever created one. `registry_from_bindings` takes `events` and every
    caller left it None, so `reg.event_names()` was empty for every real
    statement -- and BOTH temporal nodes are gated on it in
    `dsl.decoding_schema`. `Since` and `Delta` were therefore never offered to
    the model on a real guideline, in this entire project. It shows up as
    `unsupported_temporal` refusals, as "the first 24 h" silently dropped from
    PMC13151662:0002, and as PMC13074134 refusing dose titration for
    `beyond_dsl` the day after `Delta` was built for exactly that sentence.

    A procedure or a medication is datable: it has a moment it was done or
    started. A finding is not -- "has diabetes" has no single timestamp a
    guideline means. Restricting to those kinds also keeps the decoding grammar
    small, which R8 showed is a hard constraint: a 13-variable registry
    compiles only at depth 1."""
    out, seen = [], set()
    for b in bindings:
        kind = getattr(b, "kind", None)
        if getattr(b, "role", "") != "action" and kind not in _DATABLE_KINDS:
            continue
        if b.variable in seen:
            continue
        seen.add(b.variable)
        out.append(dsl.EventDef(b.variable, b.display or b.variable,
                                b.binding_uri))
    return out

# Verbs that ask for a RELATIVE change to a quantity. A closed-ish set, and
# used only to decide whether a dose variable is worth declaring -- WHAT gets
# changed is still the model's choice from the bound vocabulary.
_DOSE_CHANGE = re.compile(
    r"\b(reduc\w+|decreas\w+|lower\w*|halv\w+|increas\w+|rais\w+|up-?titrat\w+|"
    r"down-?titrat\w+|titrat\w+|adjust\w*|escalat\w+|de-?escalat\w+)\b"
    r"[^.]{0,60}?(\b\d+\s*%|\bdose\b|\bdosage\b)", re.I)

# A hole whose phrase names a POINT IN TIME rather than a patient property.
# "at start LRD", "pre-operatively", "on admission" -- the thing a relative
# change is measured from.
_ANCHOR = re.compile(
    r"\b(?:at|after|before|from|on|during|post|pre|prior\s+to|by)\b[^.]{0,30}?"
    r"\b(start\w*|initiat\w+|commenc\w+|onset|admission|discharge|operation|"
    r"surgery|induction|baseline|presentation|diagnosis|day\s*\d+)\b"
    r"|\bpre-?(?:op\w*|operative\w*|treatment|dose|[A-Z]{2,})\b"
    r"|\bbaseline\b", re.I)

_HOLE_PREFIX = ("has_", "is_", "on_")

def anchor_events(holes) -> list:
    """Unbound EventDefs for the holes that name a point in time.

    Without this the only events on offer were the bound concepts themselves,
    so "reduce basal insulin by 50% at start LRD" had nothing to anchor to
    except *being on basal insulin*. The model dutifully wrote that, twelve
    times across one document, and every one was circular -- a dose measured
    from itself, satisfiable by any value, and it typechecked and compiled.

    The anchor the sentence actually names is already in `holes`, as a plain
    boolean the DSL cannot measure from. Declared here as an event as well, so
    a `Delta` can use it. It stays UNBOUND: no terminology holds "start of the
    liver reduction diet", and claiming otherwise would be the guess this whole
    pipeline exists to avoid. The lowering emits it as a named null with the
    site query to bind -- the same treatment an unbound predicate gets."""
    out, seen = [], set()
    for h in holes:
        phrase = getattr(h, "description", "") or h.name
        if not _ANCHOR.search(phrase):
            continue
        name = h.name
        for p in _HOLE_PREFIX:
            if name.startswith(p):
                name = name[len(p):]
                break
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(dsl.EventDef(name, phrase.strip(), None, unbound=True))
    return out
# A stated RANGE ("150-240 min", "450-720 MET-min"), not a bare number. A
# single number ("the dose was 5 mg") is reporting a fact, not asking for a
# quantity target -- requiring two numbers joined by a dash/"to" is what
# keeps this from firing on that case (see
# test_the_dose_cue_needs_a_verb_and_a_quantity). Catches statements like
# "engaging in 150-240 min of aerobic exercise per week", which have no
# change verb for `_DOSE_CHANGE` to match at all.
#
# But "wait 6-12 weeks AFTER BGT placement" is the same regex shape with a
# different meaning: a window relative to a prior EVENT (Since's domain),
# not a quantity of the bound action itself (Delta/Between's domain, which
# is what this companion variable exists to feed). Measured on
# PMC13169229's replacement-timing statement -- it declared `bgt_dose`,
# `bgt_placement_dose` on a statement with no dose anywhere in it, purely
# because "weeks" is an accepted unit and nothing distinguished "6-12 weeks
# of X" from "6-12 weeks after X". The lookahead excludes the latter shape;
# checked against every gold.csv statement, none currently rely on a range
# immediately followed by one of these words, so this is a pure precision
# gain, not a measured tradeoff.
_QUANTITY_RANGE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:-|–|\bto\b)\s*\d+(?:\.\d+)?\s*"
    r"(min(?:ute)?s?|hours?|days?|weeks?|mg|kg|mmHg|mmol\S*|%|units?|MET-?min\w*)\b"
    r"(?!\s*(?:after|before|following|post|prior\s+to)\b)",
    re.I)

def dose_companions(bindings, statement: str) -> list:
    """A numeric companion variable for each bound drug/procedure, when the
    sentence asks for a dose change OR states a numeric target range.

    `Delta`/`between` need something numeric to change or bound. A medication
    or procedure binds as a BOOLEAN -- `on_basal_insulin`, is it prescribed --
    so a registry built from bindings alone has no quantity, and
    `dsl.decoding_schema` will not offer either node however many events are
    declared. That is why "Reduce basal insulin by 50%" still refused as
    `beyond_dsl` after both were added, and why "engaging in 150-240 min of
    aerobic exercise per week" still failed `between`'s TYPE_ERROR after
    `terms.db` was fixed: the min/max range had nothing numeric to attach to,
    because "aerobic exercise" is a `procedure`, which this used to exclude
    outright, and the sentence has no dose-change verb for `_DOSE_CHANGE` to
    match.

    Declared only when the statement contains a relative-change cue OR a
    stated numeric range, because every extra variable enlarges the decoding
    grammar and R8 showed that is a hard ceiling: a 13-variable registry
    compiles only at depth 1. The cues are closed patterns; which variable
    actually gets the quantity is the model's choice.

    Parallel to how `Since` derives `<event>_occurred` and
    `days_since_<event>`: a companion the DSL needs, named rather than
    implicit, so a report can say which quantity it wants."""
    if not (_DOSE_CHANGE.search(statement or "") or _QUANTITY_RANGE.search(statement or "")):
        return []
    out = []
    for b in bindings:
        if b.kind not in {"drug", "drug_class", "procedure"}:
            continue
        name = f"{b.variable}_dose"
        out.append(dsl.VarDef(
            name=name, type="real", unit=None,
            description=f"{b.display} dose".strip(),
            binding=b.binding_uri, role="action",
            # No plausible range: doses span micrograms to units and a wrong
            # one would be asserted to the solver as fact.
            plausible=None))
    return out

# A stated age threshold: "from age 5", "under 12 years", "65 years old".
# Deliberately narrower than a general number-finder -- age phrasing is a
# closed enough set that a few patterns cover the common cases without
# firing on unrelated numbers in the sentence.
_AGE_THRESHOLD = re.compile(
    r"\bage[ds]?\s+(?:of\s+)?\d+|\d+\s*years?\s*(?:of\s*age\b|old\b)|"
    r"\b(?:under|over|above|below|younger than|older than)\s+\d+\s*years?\b",
    re.I)

def age_companion(statement: str) -> list:
    """A numeric `age` variable, declared whenever the statement states an
    age threshold.

    Age is a structural patient attribute, not a terminology-coded concept:
    no SNOMED/LOINC search was ever going to bind it -- there is no generic
    "patient age" code to look up -- so it fell through to a boolean hole
    the same way a dose or duration did before its own companion existed.
    Measured on PMC12948374:0000 ("ABPM from age 5 is strongly
    recommended"): encoded with `has_age_5` (a boolean) instead of a real
    `age >= 5` comparison. Reuses the exact `age` VarDef already declared
    once, by hand, in `CKD_REGISTRY` -- one real variable, not a
    document-specific invention.

    Declared only when the statement states a threshold, for the same
    reason `dose_companions` is conditional: every extra variable enlarges
    the decoding grammar (R8)."""
    if not _AGE_THRESHOLD.search(statement or ""):
        return []
    return [dsl.VarDef("age", "real", "years", "patient age", plausible=(0, 120))]

# ---------------------------------------------------------------- gate

def triage(statement: str, index, *, statement_id: str = "s",
           mention_llm: Callable | None = None,
           select_llm: Callable | None = None,
           formalize: Callable | None = None,
           samples: int = 1, allow_partial: bool = True,
           is_patient_level: bool | None = None,
           source_page: int | None = None,
           source_span: str | None = None,
           doc_abbrevs: dict[str, str] | None = None) -> Triage:
    """Decide one statement. `formalize(text, registry) -> dict` is injected the
    same way every other model hook is, so this runs offline in tests."""
    ev: list[Evidence] = []

    # -- stage 2 evidence: what would not bind ---------------------------
    _failed_before = len(getattr(index, "failures", ()))
    bindings, refusals = bind.bind_statement(
        statement, index, mention_llm, select_llm, ref_id=statement_id,
        doc_abbrevs=doc_abbrevs)
    # A terminology lookup that failed for a TRANSPORT reason is not evidence
    # about the guideline. Without this the two are indistinguishable: the
    # endpoint returns nothing, the binder refuses, and the residue report
    # attributes it to the document. Tagged so `spectrum` and `residue_report`
    # both count it as OURS.
    _failed = len(getattr(index, "failures", ())) - _failed_before
    unbound_kinds = []
    for r in refusals:
        # Attribute the failure. An `unbound_concept` that is really an INDEX
        # GAP is a bug in our retrieval; one where no candidate exists at all
        # may be genuine residue. Reporting them in one bucket makes the
        # formalizability number uninterpretable -- measured on a real
        # guideline, 8 of 14 refusals were index gaps for concepts (CVAD,
        # solid tumors, noncardiac surgery) that DO exist in SNOMED and VSAC.
        #
        # Reused below by holes_from(): an "unselected" refusal (candidates
        # existed, the one-shot pick just missed) is hole-eligible the same
        # way a model-named vague predicate is, instead of discarding the
        # whole statement over one unlucky selector call.
        kind = classify_unbound(r, index)
        unbound_kinds.append(kind)
        ev.append(Evidence("binder", kind, r.reason_text))

    # Appended AFTER the per-concept evidence on purpose. `as_refusal` builds
    # the refusal's reason_text by joining evidence in order, so putting this
    # first made a transport warning displace the actual reason -- a real
    # regression: "no terminology binding for 'oral feeding'" became
    # "4 terminology lookup(s) failed...". A meta-warning must trail the finding
    # it qualifies.
    if _failed > 0:
        ev.append(Evidence("binder", "TERMINOLOGY_UNAVAILABLE",
                           f"{_failed} terminology lookup(s) failed (transport or "
                           f"server error, not an absent concept), so a refusal "
                           f"here may not be a property of the guideline: "
                           + "; ".join(index.failures.summary()[-_failed:])[:200]))

    reg = bind.registry_from_bindings(bindings, events=datable_events(bindings))
    _companions = dose_companions(bindings, statement) + age_companion(statement)
    if _companions:
        reg = dsl.Registry([reg[n] for n in reg.names()] + _companions,
                           [reg.event(e) for e in reg.event_names()])
    holes = holes_from(statement, refusals, [], unbound_kinds)

    if not bindings:
        # Nothing bound at all: there is no vocabulary to write a rule in, and
        # no registry to constrain a decode against, so the model cannot be
        # asked either. `unbound_concept` is the honest label -- the gate must
        # NOT infer `not_patient_level` from an empty binding, which would
        # confuse "about a service" with "too vague to have concepts". That
        # distinction is the segmenter's `is_patient_level`, passed in here.
        #
        # `unbound_concept` alone doesn't distinguish "this concept is real
        # but uncoded" from "this phrase is inherently hedging" -- the same
        # is_vague() check holes_from() already trusts for hole-eligibility,
        # reused here so a wave of refused VAGUE_HINTS phrases (with nothing
        # else bound) reports the informative reason instead of the generic
        # coverage-gap one every time.
        if is_patient_level is False:
            code = "not_patient_level"
        elif any(is_vague((r.source_span or "")) for r in refusals):
            code = "vague_predicate"
        else:
            code = "unbound_concept"
        ev.append(Evidence("binder", "NO_BOUND_VOCABULARY",
                           "no clinical concept in the statement resolved to a code"))
        return Triage(statement_id, statement, "refuse", code, None, 0.0,
                      tuple(ev), tuple(h.name for h in holes), samples=samples,
                      refusal_rate=1.0)

    if formalize is None:
        # No model: report what the deterministic evidence alone supports.
        return Triage(statement_id, statement,
                      "refuse" if refusals else "encode",
                      "unbound_concept" if refusals else None,
                      None, 0.0 if refusals else 0.5, tuple(ev),
                      tuple(h.name for h in holes), samples=0, registry=reg)

    # -- attempt 1: strict. Only bound vocabulary is available. ----------
    outcomes = _attempt_k(statement, reg, formalize, statement_id,
                          source_page, source_span, max(1, samples))
    rules = [o for o in outcomes if isinstance(o, dsl.Rule)]
    decls = [o for o in outcomes if isinstance(o, dsl.Refusal)]
    rate = len(decls) / len(outcomes)

    blocked_codes: set[str] = set()
    if rules:
        rule, iss = _best(rules, reg)
        ev += [Evidence("validator", i.code, i.message) for i in iss]
        blocking = [i for i in iss if i.severity == "error"]
        blocked_codes |= {i.code for i in blocking}

        # A strict encode is UNSAFE when the binder refused something. The
        # strict registry does not contain that concept, so a rule it produces
        # has either dropped the concept or mis-encoded it -- and dropping is
        # invisible, because what remains still typechecks.
        #
        # Measured, and this is the failure the whole gate exists to prevent:
        # "In pediatric patients with persistently positive APLA without a
        # history of thrombosis, suggest no primary prophylaxis" encoded as
        # `has_thrombosis == false -> no prophylaxis`. APLA would not bind, so
        # the entire population restriction was silently dropped and the rule
        # became "anyone without a thrombosis history". It passed as ENCODE.
        #
        # So: any binder refusal forces the partial path, where the dropped
        # concept is available as a declared hole. If the model still does not
        # use it, that is a refusal, not an encoding.
        if refusals and not blocking:
            ev.append(Evidence("gate", "DROPPED_CONCEPT_RISK",
                               f"{len(refusals)} concept(s) did not bind, so a strict "
                               f"encoding may have silently omitted them; retrying "
                               f"with them declared as holes"))
            blocking = list(iss) or [Evidence]      # force the partial path below
        # A concept that BOUND and then went unused. Not an error: an outcome
        # is legitimately absent from `when` -- "acupuncture may be effective in
        # relieving abdominal pain in patients with AP" has AP as the
        # population and pain as the EFFECT, so encoding pain as a condition
        # would be wrong. But a dropped population restriction looks identical
        # from here, and that one changes who the rule applies to. So: surface
        # it, name it, and let a reviewer decide. DROPPED_CONCEPT_RISK covers
        # only the case where the BINDER failed; this covers the case where the
        # binder succeeded and the model simply did not use the result.
        used = dsl._vars_in(rule.when) | dsl._vars_in(rule.then)
        unused = sorted(n for n in reg.names() if n not in used)
        if unused:
            ev.append(Evidence("gate", "UNUSED_BOUND_CONCEPT",
                               f"bound but not used in the rule: {', '.join(unused)}"
                               f" -- an outcome may legitimately be unused, a dropped "
                               f"population restriction may not"))
        if not blocking:
            return Triage(statement_id, statement, "encode", None,
                          dsl.fragment_of(rule.when, rule.then), 1.0 - rate,
                          tuple(ev), dsl.open_holes(rule, reg), rule=rule,
                          samples=len(outcomes), refusal_rate=rate,
                          registry=reg)
        if [i for i in iss if getattr(i, "severity", "") == "error"]:
            ev.append(Evidence("validator", "REJECTED_AFTER_ENCODING",
                               f"encoding failed validation: "
                               f"{[i.code for i in iss if i.severity == 'error']}"))

    # -- attempt 2: partial. Declare the unbindable predicates as HOLES. --
    # The model names the predicates that defeated it; those are the holes.
    holes = holes_from(statement, refusals, decls, unbound_kinds) or holes
    pdecls: list = []
    if allow_partial and holes:
        # The compiled grammar scales with the variable enum (R8), and a wide
        # registry -- bound vars PLUS every hole -- can exceed the API's limit
        # even at depth 0, which loses the statement entirely. Cap it, keeping
        # bound variables first, and say so rather than truncating silently.
        kept_holes = list(holes)[:max(0, MAX_DECODE_VARS - len(reg.names()))]
        if len(kept_holes) < len(holes):
            ev.append(Evidence("gate", "REGISTRY_TRUNCATED",
                               f"{len(holes) - len(kept_holes)} of {len(holes)} holes "
                               f"dropped to keep the decoding grammar compilable"))
        # Cap the event enum for the same reason the holes are capped: it is
        # its own `$defs` entry in decoding_schema, so it inflates the compiled
        # grammar independently of the variable enum, and a statement with many
        # datable concepts can blow the ceiling on the event enum ALONE.
        #
        # Bound events keep their slots first and the anchors fill what is
        # left, matching how bound variables outrank holes above.
        #
        # `reg.event(e)`, not `EventDef(e)`: rebuilding them bare dropped every
        # event's description and its `unbound` flag on the way into the very
        # attempt that uses them.
        anchors = anchor_events(kept_holes)
        event_names = reg.event_names()
        kept_events = event_names[:MAX_DECODE_VARS]
        kept_anchors = anchors[:max(0, MAX_DECODE_VARS - len(kept_events))]
        _dropped = ((len(event_names) - len(kept_events))
                    + (len(anchors) - len(kept_anchors)))
        if _dropped:
            ev.append(Evidence("gate", "REGISTRY_TRUNCATED",
                               f"{_dropped} of {len(event_names) + len(anchors)} "
                               f"events dropped to keep the decoding grammar "
                               f"compilable"))
        wide = dsl.Registry(
            [reg[n] for n in reg.names()] + kept_holes,
            [reg.event(e) for e in kept_events] + kept_anchors)
        outs = _attempt_k(statement, wide, formalize, statement_id,
                          source_page, source_span, max(1, samples))
        prules = [o for o in outs if isinstance(o, dsl.Rule)]
        pdecls = [o for o in outs if isinstance(o, dsl.Refusal)]
        # Confidence must describe the attempt the DECISION came from. Measured:
        # a statement refused 8/8 strictly but encoded cleanly once its hole was
        # declared was being reported at confidence 0.00, which would sort a
        # perfectly good PARTIAL to the bottom of the risk-coverage curve.
        prate = sum(1 for o in outs if isinstance(o, dsl.Refusal)) / len(outs)
        if prules:
            rule, iss = _best(prules, wide)
            # Record them. Only `blocked_codes` was kept here, so every
            # validator WARNING raised on the partial path was discarded --
            # a rule with a CIRCULAR_BASELINE reached the bundle reporting
            # nothing wrong, while the identical finding on the strict path
            # printed. Third time this branch has silently dropped evidence the
            # other one keeps.
            ev += [Evidence("validator", i.code, i.message) for i in iss]
            blocked_codes |= {i.code for i in iss if i.severity == "error"}
            if not [i for i in iss if i.severity == "error"]:
                open_ = dsl.open_holes(rule, wide)
                # Also on this path. It only ran on the strict-encode branch,
                # so the dropped clause in "...in favour of SGLT-2i or GLP-1 RA;
                # AND a weak recommendation against finerenone in adults with
                # CKD" went unreported -- `on_finerenone` bound, reached the
                # registry, and was simply never used.
                _used = dsl._vars_in(rule.when) | dsl._vars_in(rule.then)
                _unused = sorted(n for n in wide.names()
                                 if n not in _used and not wide[n].unbound)
                if _unused:
                    ev.append(Evidence("gate", "UNUSED_BOUND_CONCEPT",
                                       f"bound but not used in the rule: "
                                       f"{', '.join(_unused)} -- an outcome may "
                                       f"legitimately be unused, a dropped clause "
                                       f"or population restriction may not"))
                if open_:
                    ev.append(Evidence("model", "PARTIAL_ENCODING",
                                       f"encoded with {len(open_)} free variable(s): "
                                       f"{', '.join(open_)}"))
                    return Triage(statement_id, statement, "partial", None,
                                  dsl.fragment_of(rule.when, rule.then),
                                  1.0 - prate, tuple(ev), open_, rule=rule,
                                  samples=len(outcomes) + len(outs),
                                  refusal_rate=prate, registry=wide)

    # -- refuse, carrying the model's own reason where it gave one -------
    best = _best_refusal(decls + pdecls)
    if best:
        ev.append(Evidence("model", best.reason_code, best.reason_text))
    # The validator's diagnosis outranks the fallback, and outranks the model's
    # own reason when the model did not decline at all. A statement rejected
    # for MODAL_PREDICATE is discretionary -- the guideline hedged -- which is
    # `defeasible`, not the "unbound_concept" the fallback would have reported.
    # A VACUOUS_RULE means no non-vacuous obligation could be extracted; that
    # is reported as `beyond_dsl` with the vacuity named in the evidence, so a
    # governance reader is never told a rule certifies patients when it cannot.
    #
    # `is_patient_level` outranks all of that. It was only ever consulted in
    # the nothing-bound branch above -- the moment ANY concept binds, this
    # known fact got dropped and whatever formalize() self-declared (e.g.
    # `vague_predicate`) won instead, even though "not about an individual
    # patient at all" is the more fundamental reason the statement can't
    # become a Track-B rule, no matter what else also went wrong encoding it.
    if is_patient_level is False:
        code = "not_patient_level"
    elif "MODAL_PREDICATE" in blocked_codes:
        code = "defeasible"
    elif "VACUOUS_RULE" in blocked_codes:
        code = "beyond_dsl"
    else:
        code = (best.reason_code if best else
                "unbound_concept" if refusals else "beyond_dsl")
    # What the decoder was ACTUALLY offered. A model that says "this DSL has no
    # delta construct" is reporting its menu, not the DSL: three times running,
    # a titration sentence refused because the node was gated off upstream (no
    # events declared; then the leaf required an event; then it also required a
    # numeric variable, and medications bind as booleans). Recording the menu
    # next to the refusal makes that distinguishable from the outside instead
    # of costing a live run each time.
    ev.append(Evidence("gate", "GRAMMAR_OFFERED",
                       f"decoder could emit [{', '.join(dsl.offered_leaves(reg))}] "
                       f"over vars [{', '.join(reg.names())}] and events "
                       f"[{', '.join(reg.event_names()) or 'none'}]"))
    return Triage(statement_id, statement, "refuse", code, None, 1.0 - rate,
                  tuple(ev), tuple(h.name for h in holes), refusal=best,
                  samples=len(outcomes), refusal_rate=rate, registry=reg)

def _attempt_k(statement, reg, formalize, sid, page, span, k):
    """K samples in parallel. They are independent calls, and sequentially K=8
    takes minutes per statement -- long enough that nobody runs the calibration
    sweep the confidence score exists for."""
    if k <= 1:
        return [_attempt(statement, reg, formalize, sid, page, span)]
    with cf.ThreadPoolExecutor(max_workers=min(8, k)) as pool:
        futures = [pool.submit(_attempt, statement, reg, formalize, sid, page, span)
                   for _ in range(k)]
        return [f.result() for f in futures]

def _shrink_registry(reg: dsl.Registry) -> dsl.Registry:
    """Halve the unbound holes and the events, keep every BOUND var untouched.

    A bound var is the statement's actual substance -- dropping one silently
    omits a real concept, the exact failure `DROPPED_CONCEPT_RISK` exists to
    catch. Holes and events are supplementary structure the grammar can
    afford to lose first, and a smaller-but-sound encoding beats losing the
    statement outright."""
    holes = reg.unbound()
    bound = [n for n in reg.names() if n not in holes]
    kept_holes = holes[:len(holes) // 2]
    events = reg.event_names()
    kept_events = events[:len(events) // 2]
    return dsl.Registry([reg[n] for n in bound + kept_holes],
                        [reg.event(e) for e in kept_events])

def _attempt(statement, reg, formalize, sid, page, span):
    try:
        obj = dsl.from_decoded(formalize(statement, reg))
    except cassette.CassetteMiss:
        # NEVER swallowed. A stale tape must stop the replay harness, not turn
        # into a refusal that makes the harness report success -- which is what
        # it did: the first run after the registry changed replayed "green"
        # while every single call was missing.
        raise
    except Exception as exc:
        # `formalize()` already steps the decode DEPTH down to 0 before
        # raising `GrammarTooLarge` -- if it still fails, the REGISTRY itself
        # is too big, not the depth. Recognized by message, not by class:
        # gate.py stays decoupled from llm.py so it can be tested offline
        # with a fake `formalize` and no model. One shrink-and-retry.
        #
        # Both give-ups below are LABELLED, not the plain "beyond_dsl:
        # encoding attempt failed" this used to read. That message is
        # indistinguishable from the DSL genuinely lacking a construct, so an
        # auth failure, a rate limit or a malformed JSON body all inflated the
        # formalizability residue with our own outages. That number is a
        # headline result; it cannot quietly include our downtime.
        if "will not compile at any depth" in str(exc):
            shrunk = _shrink_registry(reg)
            if shrunk.names() != reg.names() or shrunk.event_names() != reg.event_names():
                try:
                    obj = dsl.from_decoded(formalize(statement, shrunk))
                except Exception as exc2:
                    return dsl.Refusal(sid, "beyond_dsl",
                                       f"ENCODER FAILURE (ours, not the guideline's): "
                                       f"encoding attempt failed after registry shrink: "
                                       f"{type(exc2).__name__}: {exc2}")
                if isinstance(obj, dsl.Rule):
                    return replace(obj, rule_id=sid, source_page=page, source_span=span)
                return obj
        return dsl.Refusal(sid, "beyond_dsl",
                           f"ENCODER FAILURE (ours, not the guideline's): "
                           f"{type(exc).__name__}: {exc}")
    if isinstance(obj, dsl.Rule):
        return replace(obj, rule_id=sid, source_page=page, source_span=span)
    return obj

def _best(rules: list[dsl.Rule], reg: dsl.Registry):
    """Prefer a sample that validates. With K>1 the samples agree semantically
    (measured: 0 of 10 statements produced >1 reading), so this is a tie-break,
    not a vote."""
    def issues(r):
        # Only ask the solver about a rule that typechecks. check_nondegenerate
        # has to build a Z3 term, which an ill-typed rule cannot do.
        v = dsl.validate_rule(r, reg)
        if [i for i in v if i.severity == "error"]:
            return v
        return v + dsl.check_nondegenerate(r, reg)
    scored = [(r, issues(r)) for r in rules]
    scored.sort(key=lambda t: sum(1 for i in t[1] if i.severity == "error"))
    return scored[0]

# Codes a model reaches only by engaging with WHY a statement fails, preferred
# on a tie over the two that mean little more than "nothing else fit".
_GENERIC_REASON_CODES = {"unbound_concept", "not_patient_level"}

def _best_refusal(decls: list) -> "dsl.Refusal | None":
    """Pick among K (possibly disagreeing) self-declared refusals by simple
    majority on `reason_code` -- NOT the first one in the list, which was
    never a real choice, just whichever of K identical, independently
    sampled calls happened to be listed first. Ties (including every code
    getting exactly one vote) favour a SPECIFIC code over
    unbound_concept/not_patient_level: a model that committed to
    vague_predicate/defeasible/beyond_dsl/unsupported_temporal is reporting
    it engaged with the content, not just that nothing bound."""
    if not decls:
        return None
    counts: dict[str, int] = {}
    first_by_code: dict = {}
    for d in decls:
        counts[d.reason_code] = counts.get(d.reason_code, 0) + 1
        first_by_code.setdefault(d.reason_code, d)
    best_code = max(counts, key=lambda c: (counts[c], c not in _GENERIC_REASON_CODES))
    return first_by_code[best_code]

# ---------------------------------------------------------------- report

def spectrum(triages: Iterable[Triage]) -> dict:
    """The M1 six-bucket formalizability spectrum, computed from the outputs
    rather than annotated by hand: PROP / LIA / MTL for what encoded, and the
    refusal reason codes for what did not."""
    t = list(triages)
    buckets: dict[str, int] = {}
    for x in t:
        if x.encodable and x.fragment:
            key = x.fragment
        elif x.reason_code == "unbound_concept":
            # Split the bucket that would otherwise attribute OUR retrieval
            # failures to the guideline. Only `unbound_index_gap` is arguably
            # residue; `unbound_unselected` means candidates existed.
            gaps = {e.code for e in x.evidence if e.code.startswith("unbound_")}
            key = ("unbound_concept(index_gap)" if "unbound_index_gap" in gaps
                   else "unbound_concept(unselected)" if "unbound_unselected" in gaps
                   else "unbound_concept")
        else:
            key = x.reason_code or "unknown"
        buckets[key] = buckets.get(key, 0) + 1
    enc = [x for x in t if x.encodable]
    return {
        "n": len(t),
        "encode": sum(1 for x in t if x.decision == "encode"),
        "partial": sum(1 for x in t if x.decision == "partial"),
        "refuse": sum(1 for x in t if x.decision == "refuse"),
        "in_fragment_rate": round(len(enc) / len(t), 3) if t else 0.0,
        "buckets": dict(sorted(buckets.items())),
        "open_holes": sum(len(x.open_holes) for x in t),
        # Statements lost to OUR retrieval rather than to the guideline. A
        # formalizability fraction reported without this number is not
        # interpretable.
        # Only an `unbound_concept` refusal can be attributable to retrieval.
        # This counted ANY statement carrying a binder miss, so a statement
        # refused for `beyond_dsl` -- dose titration, which no retrieval fix
        # could ever encode -- was reported as our bug because some incidental
        # concept in it also failed to bind. Measured on PMC13074134: 5 of 5
        # refusals attributed to retrieval when only 2 were.
        "retrieval_attributable": sum(
            1 for x in t if not x.encodable
            and x.reason_code == "unbound_concept"
            and any(e.code in {"unbound_index_gap", "unbound_unselected",
                               "TERMINOLOGY_UNAVAILABLE"}
                    for e in x.evidence)),
        "mean_confidence": round(statistics.mean([x.confidence for x in t]), 3) if t else 0.0,
    }

def risk_coverage(triages: Iterable[Triage], truth: dict[str, bool]) -> list[dict]:
    """E3's curve. Sort by confidence, sweep the threshold, and report precision
    on `encode` against clinician labels at each coverage level. The operating
    point is chosen for precision, because a wrongly-accepted statement is the
    failure the whole tool argues against.

    `truth[statement_id]` is the human judgement: is it faithfully encodable?"""
    scored = sorted([t for t in triages if t.statement_id in truth],
                    key=lambda t: -t.confidence)
    out, tp, fp = [], 0, 0
    for i, t in enumerate(scored, 1):
        if truth[t.statement_id]:
            tp += 1
        else:
            fp += 1
        out.append({"threshold": round(t.confidence, 3),
                    "coverage": round(i / len(scored), 3),
                    "precision_on_encodable": round(tp / (tp + fp), 3)})
    return out
