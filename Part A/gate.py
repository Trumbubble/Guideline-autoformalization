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
import statistics
from dataclasses import dataclass, field, replace
from typing import Callable, Iterable

import bind
import dsl

Decision = str          # encode | partial | refuse

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

    @property
    def encodable(self) -> bool:
        """The BINARY the gate is calibrated on. Two independent labelling
        passes agreed 28/29 on this call but only 71.8% on the exact fragment,
        so the binary is the headline and the fragment is secondary."""
        return self.decision in {"encode", "partial"}

    def as_refusal(self) -> dsl.Refusal | None:
        if self.decision != "refuse":
            return None
        return self.refusal or dsl.Refusal(
            self.statement_id, self.reason_code or "beyond_dsl",
            "; ".join(e.detail for e in self.evidence) or "no encoding produced",
            confidence=1.0 - self.confidence)

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
            if p and p.lower() in statement.lower() and p.lower() not in seen:
                seen.add(p.lower())
                out.append(hole_for(p, "condition"))
    for r in refusals:
        p = (r.source_span or "").strip()
        if p and is_vague(p) and p.lower() not in seen:
            seen.add(p.lower())
            out.append(hole_for(p, "condition"))
    return tuple(out)

def hole_for(mention: str, kind: str) -> dsl.VarDef:
    """Turn an unbindable predicate into a declared free variable rather than a
    silent omission. It has no binding and no plausible range on purpose."""
    name = bind.KIND_PREFIX.get(kind, "") + bind._slug(mention)
    return dsl.VarDef(name=name, type="bool", description=mention,
                      role="observation", unbound=True)

# ---------------------------------------------------------------- gate

def triage(statement: str, index, *, statement_id: str = "s",
           mention_llm: Callable | None = None,
           select_llm: Callable | None = None,
           formalize: Callable | None = None,
           samples: int = 1, allow_partial: bool = True,
           is_patient_level: bool | None = None,
           source_page: int | None = None,
           source_span: str | None = None) -> Triage:
    """Decide one statement. `formalize(text, registry) -> dict` is injected the
    same way every other model hook is, so this runs offline in tests."""
    ev: list[Evidence] = []

    # -- stage 2 evidence: what would not bind ---------------------------
    bindings, refusals = bind.bind_statement(
        statement, index, mention_llm, select_llm, ref_id=statement_id)
    for r in refusals:
        ev.append(Evidence("binder", "unbound_concept", r.reason_text))

    reg = bind.registry_from_bindings(bindings)
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
                      tuple(h.name for h in holes), samples=0)

    # -- attempt 1: strict. Only bound vocabulary is available. ----------
    outcomes = _attempt_k(statement, reg, formalize, statement_id,
                          source_page, source_span, max(1, samples))
    rules = [o for o in outcomes if isinstance(o, dsl.Rule)]
    decls = [o for o in outcomes if isinstance(o, dsl.Refusal)]
    rate = len(decls) / len(outcomes)

    if rules:
        rule, iss = _best(rules, reg)
        ev += [Evidence("validator", i.code, i.message) for i in iss]
        blocking = [i for i in iss if i.severity == "error"]
        if not blocking:
            return Triage(statement_id, statement, "encode", None,
                          dsl.fragment_of(rule.when, rule.then), 1.0 - rate,
                          tuple(ev), dsl.open_holes(rule, reg), rule=rule,
                          samples=len(outcomes), refusal_rate=rate)
        ev.append(Evidence("validator", "REJECTED_AFTER_ENCODING",
                           f"encoding failed validation: {[i.code for i in blocking]}"))

    # -- attempt 2: partial. Declare the unbindable predicates as HOLES. --
    # The model names the predicates that defeated it; those are the holes.
    holes = holes_from(statement, refusals, decls) or holes
    if allow_partial and holes:
        wide = dsl.Registry([reg[n] for n in reg.names()] + list(holes),
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
            if not [i for i in iss if i.severity == "error"]:
                open_ = dsl.open_holes(rule, wide)
                if open_:
                    ev.append(Evidence("model", "PARTIAL_ENCODING",
                                       f"encoded with {len(open_)} free variable(s): "
                                       f"{', '.join(open_)}"))
                    return Triage(statement_id, statement, "partial", None,
                                  dsl.fragment_of(rule.when, rule.then),
                                  1.0 - prate, tuple(ev), open_, rule=rule,
                                  samples=len(outcomes) + len(outs),
                                  refusal_rate=prate)

    # -- refuse, carrying the model's own reason where it gave one -------
    best = decls[0] if decls else None
    if best:
        ev.append(Evidence("model", best.reason_code, best.reason_text))
    code = (best.reason_code if best else
            "unbound_concept" if refusals else "beyond_dsl")
    return Triage(statement_id, statement, "refuse", code, None, 1.0 - rate,
                  tuple(ev), tuple(h.name for h in holes), refusal=best,
                  samples=len(outcomes), refusal_rate=rate)

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
    scored = [(r, dsl.validate_rule(r, reg) + dsl.check_nondegenerate(r, reg))
              for r in rules]
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
        key = x.fragment if x.encodable and x.fragment else (x.reason_code or "unknown")
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
