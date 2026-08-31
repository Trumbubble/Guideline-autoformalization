"""
Refusal gate: can this statement be faithfully encoded, partly, or not at all?

Stage 3 of Track A, and the one with an asymmetric cost. A false REFUSE costs
coverage and shows up honestly in the residue report. A false ENCODE produces a
silently wrong checker -- so the gate is thresholded for precision on `encode`,
and whatever recall that buys is what it buys.

WHAT THIS IS NOT. It is not an LLM asked "is this vague?". That is circular
(arXiv:2607.04655 puts purpose-built diagnostic models at ~0.71 F1 on error
categorisation), and it is unnecessary: most of the decision is already made by
machinery stages 1-2 produce. The gate AGGREGATES evidence and only then asks a
model to try.

                          evidence                          maps to
  binder could not bind a concept   (deterministic)   unbound_concept
  obligation names no action        (deterministic)   not_patient_level
  multi-visit predicate             (deterministic)   unsupported_temporal
  rule never fires / forbids nothing(deterministic)   beyond_dsl
  the model declines to encode      (model)           vague_predicate / defeasible

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

def holes_from(statement: str, refusals, declined) -> tuple[dsl.VarDef, ...]:
    """Which unbindable things deserve to become free variables.

    Two sources, and the distinction matters. A predicate the model NAMES as
    undefined ("at high risk") is a genuine hole. A concept the binder simply
    failed to retrieve ("refer to nephrology") is a COVERAGE GAP -- a fixable
    index problem -- and turning it into a hole would launder a bug into a
    finding. Only vague-looking binder refusals qualify.

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
    for r in refusals:
        p = (r.source_span or "").strip()
        if p and is_vague(p) and p.lower() not in seen:
            seen.add(p.lower())
            out.append(hole_for(p, "condition"))
    return tuple(out)

def classify_unbound(refusal, index) -> str:
    """`index_gap` when retrieval returned nothing to choose from; `unselected`
    when it returned candidates and none was right. Free to compute -- the
    terminology endpoints cost nothing and the query is already cached."""
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

def dose_companions(bindings, statement: str) -> list:
    """A numeric dose variable for each bound medication, when the sentence
    asks for a dose change.

    `Delta` needs something numeric to change. A medication binds as a BOOLEAN
    -- `on_basal_insulin`, is it prescribed -- so a registry built from
    bindings alone has no quantity, and `dsl.decoding_schema` will not offer
    the node however many events are declared. That is why "Reduce basal
    insulin by 50%" still refused as `beyond_dsl` after both were added.

    Declared only when the statement contains a relative-change cue, because
    every extra variable enlarges the decoding grammar and R8 showed that is a
    hard ceiling: a 13-variable registry compiles only at depth 1. The cue is a
    closed set of verbs; which variable actually changes is the model's choice.

    Parallel to how `Since` derives `<event>_occurred` and
    `days_since_<event>`: a companion the DSL needs, named rather than
    implicit, so a report can say which quantity it wants."""
    if not _DOSE_CHANGE.search(statement or ""):
        return []
    out = []
    for b in bindings:
        if b.kind not in {"drug", "drug_class"}:
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
    for r in refusals:
        # Attribute the failure. An `unbound_concept` that is really an INDEX
        # GAP is a bug in our retrieval; one where no candidate exists at all
        # may be genuine residue. Reporting them in one bucket makes the
        # formalizability number uninterpretable -- measured on a real
        # guideline, 8 of 14 refusals were index gaps for concepts (CVAD,
        # solid tumors, noncardiac surgery) that DO exist in SNOMED and VSAC.
        ev.append(Evidence("binder", classify_unbound(r, index), r.reason_text))

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
    _doses = dose_companions(bindings, statement)
    if _doses:
        reg = dsl.Registry([reg[n] for n in reg.names()] + _doses,
                           [reg.event(e) for e in reg.event_names()])
    holes = holes_from(statement, refusals, [])

    if not bindings:
        # Nothing bound at all: there is no vocabulary to write a rule in, and
        # no registry to constrain a decode against, so the model cannot be
        # asked either. `unbound_concept` is the honest label -- the gate must
        # NOT infer `not_patient_level` from an empty binding, which would
        # confuse "about a service" with "too vague to have concepts". That
        # distinction is the segmenter's `is_patient_level`, passed in here.
        code = ("not_patient_level" if is_patient_level is False
                else "unbound_concept")
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
    holes = holes_from(statement, refusals, decls) or holes
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
        wide = dsl.Registry([reg[n] for n in reg.names()] + kept_holes,
                            [dsl.EventDef(e) for e in reg.event_names()])
        outs = _attempt_k(statement, wide, formalize, statement_id,
                          source_page, source_span, max(1, samples))
        prules = [o for o in outs if isinstance(o, dsl.Rule)]
        # Confidence must describe the attempt the DECISION came from. Measured:
        # a statement refused 8/8 strictly but encoded cleanly once its hole was
        # declared was being reported at confidence 0.00, which would sort a
        # perfectly good PARTIAL to the bottom of the risk-coverage curve.
        prate = sum(1 for o in outs if isinstance(o, dsl.Refusal)) / len(outs)
        if prules:
            rule, iss = _best(prules, wide)
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
    best = decls[0] if decls else None
    if best:
        ev.append(Evidence("model", best.reason_code, best.reason_text))
    # The validator's diagnosis outranks the fallback, and outranks the model's
    # own reason when the model did not decline at all. A statement rejected
    # for MODAL_PREDICATE is discretionary -- the guideline hedged -- which is
    # `defeasible`, not the "unbound_concept" the fallback would have reported.
    # A VACUOUS_RULE means no non-vacuous obligation could be extracted; that
    # is reported as `beyond_dsl` with the vacuity named in the evidence, so a
    # governance reader is never told a rule certifies patients when it cannot.
    if "MODAL_PREDICATE" in blocked_codes:
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

def _attempt(statement, reg, formalize, sid, page, span):
    try:
        obj = dsl.from_decoded(formalize(statement, reg))
    except Exception as exc:
        return dsl.Refusal(sid, "beyond_dsl", f"encoding attempt failed: {exc}")
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
