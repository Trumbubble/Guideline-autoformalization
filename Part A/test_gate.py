"""Regression tests for the refusal gate. Offline: no key, no network.

The model is injected, so `formalize` here is a lookup table keyed on the
statement -- which makes every decision path deterministic and lets the tests
assert on the gate's LOGIC rather than on a model's mood.
"""
import z3

import bind
import dsl
import gate
from gate import Triage, holes_from, hole_for, is_vague, risk_coverage, spectrum, triage

IX = bind.fixture_index()

S_OK = ("We recommend an ACE inhibitor in adults with diabetes and a urine "
        "albumin-to-creatinine ratio of 30 mg/g or higher.")
S_HOLE = "Start an ACE inhibitor when eGFR falls below 30 or the patient is at high risk."
S_VAGUE = "Consider referral after an adequate trial of conservative therapy."

def _rule(when, then, rid="s"):
    """Build the decoder's wire shape directly. `when`/`then` are already
    dicts, so they must NOT go through dsl.to_dict, which expects dataclasses."""
    return {"outcome": "rule", "refusal": None,
            "rule": {"rule_id": rid, "when": when, "then": then,
                     "action": "recommend", "target": "x", "strength": None,
                     "source_page": 1, "source_span": "s"}}

def _refusal(code, text, preds=(), rid="s"):
    return {"outcome": "refusal", "rule": None,
            "refusal": {"rule_id": rid, "reason_code": code, "reason_text": text,
                        "source_page": None, "source_span": None,
                        "confidence": None,
                        "undefined_predicates": list(preds)}}

def codes(t): return sorted(e.code for e in t.evidence)

# ------------------------------------------------------------------ holes

def test_a_named_undefined_predicate_becomes_a_hole():
    declined = [dsl.Refusal("s", "vague_predicate", "no definition",
                            undefined_predicates=("at high risk",))]
    holes = holes_from(S_HOLE, [], declined)
    assert [h.name for h in holes] == ["has_high_risk"]
    assert holes[0].unbound and holes[0].binding is None

def test_a_paraphrased_predicate_is_rejected():
    """Same anti-synthesis rule as the segmenter and binder: a phrase that is
    not verbatim in the source cannot be pointed at, so it cannot be a hole."""
    declined = [dsl.Refusal("s", "vague_predicate", "x",
                            undefined_predicates=("elevated cardiovascular risk",))]
    assert holes_from(S_HOLE, [], declined) == ()

def test_a_retrieval_failure_is_not_a_hole():
    """'refer to nephrology' IS bindable -- the index just missed it. Turning
    that into a free variable would launder an index bug into a finding."""
    miss = dsl.Refusal("s", "unbound_concept", "no candidates",
                       source_span="refer to nephrology")
    assert holes_from(S_HOLE, [miss], []) == ()
    vague = dsl.Refusal("s", "unbound_concept", "no candidates",
                        source_span="at high risk")
    assert [h.name for h in holes_from(S_HOLE, [vague], [])] == ["has_high_risk"]

def test_is_vague_lexicon():
    assert is_vague("at high risk") and is_vague("an adequate trial")
    assert not is_vague("serum potassium") and not is_vague("refer to nephrology")

# --------------------------------------------------------------- decisions

def test_encode_when_everything_binds_and_validates():
    def f(text, reg):
        return _rule({"kind": "cmp", "var": "has_diabetes", "op": "==", "value": True},
                     {"kind": "cmp", "var": "on_ace_inhibitor", "op": "==", "value": True})
    t = triage(S_OK, IX, statement_id="s1", formalize=f, source_page=1, source_span="s")
    assert t.decision == "encode" and t.encodable
    assert t.fragment == "PROP" and t.rule is not None and not t.open_holes

def test_partial_when_the_model_names_a_hole_it_can_then_use():
    """Attempt 1 refuses naming the predicate; attempt 2 gets it as a declared
    free variable and succeeds. That two-step is what PARTIAL means."""
    calls = []
    def f(text, reg):
        calls.append(sorted(reg.names()))
        if "has_high_risk" not in reg.names():
            return _refusal("vague_predicate", "'at high risk' is undefined",
                            preds=("at high risk",))
        return _rule({"kind": "or", "operands": [
                        {"kind": "cmp", "var": "egfr", "op": "<", "value": 30},
                        {"kind": "cmp", "var": "has_high_risk", "op": "==", "value": True}]},
                     {"kind": "cmp", "var": "on_ace_inhibitor", "op": "==", "value": True})
    t = triage(S_HOLE, IX, statement_id="s3", formalize=f, source_page=1, source_span="s")
    assert t.decision == "partial" and t.encodable
    assert t.open_holes == ("has_high_risk",)
    assert len(calls) == 2 and "has_high_risk" in calls[1]

def test_an_unbound_concept_blocks_a_strict_encode():
    """MEASURED FAILURE, and the one the gate exists to prevent. On a real
    guideline: "In pediatric patients with persistently positive APLA without a
    history of thrombosis, suggest no primary prophylaxis" was encoded as
    `has_thrombosis == false -> no prophylaxis`. APLA would not bind, so the
    model silently dropped the whole population restriction and the rule became
    "anyone without a thrombosis history". It passed as ENCODE, because what
    remained still typechecked.

    A strict encode is therefore unsafe whenever the binder refused anything:
    the strict registry cannot express the missing concept, so the rule either
    dropped it or mis-encoded it, and dropping is invisible."""
    calls = []
    def f(text, reg):
        calls.append(sorted(reg.names()))
        # always returns a valid-looking rule that ignores the unbound concept
        return _rule({"kind": "cmp", "var": "has_diabetes", "op": "==", "value": True},
                     {"kind": "cmp", "var": "on_ace_inhibitor", "op": "==", "value": True})
    text = S_OK + " Reassess if the patient is at high risk."
    # inject the mention proposer so "at high risk" is actually offered to the
    # binder -- which then refuses it, since no value set defines it
    mentions = lambda _t: {"mentions": [
        {"text": "diabetes", "kind": "condition", "canonical": None},
        {"text": "ACE inhibitor", "kind": "drug_class", "canonical": None},
        {"text": "at high risk", "kind": "condition", "canonical": None}]}
    t = triage(text, IX, statement_id="s1", formalize=f, mention_llm=mentions,
               source_page=1, source_span="s")
    # the binder refuses "at high risk", so a strict encode must not stand
    assert t.decision != "encode", t.decision
    assert any(e.code == "DROPPED_CONCEPT_RISK" for e in t.evidence)
    assert len(calls) == 2                      # it retried with the hole declared

def test_a_clean_statement_still_encodes():
    """The guard must not block statements where everything bound."""
    def f(text, reg):
        return _rule({"kind": "cmp", "var": "has_diabetes", "op": "==", "value": True},
                     {"kind": "cmp", "var": "on_ace_inhibitor", "op": "==", "value": True})
    t = triage(S_OK, IX, statement_id="s1", formalize=f, source_page=1, source_span="s")
    assert t.decision == "encode"
    assert not any(e.code == "DROPPED_CONCEPT_RISK" for e in t.evidence)

def test_refuse_when_the_model_names_nothing_usable():
    """S_OK binds, so the model is actually consulted and its reason is kept."""
    f = lambda text, reg: _refusal("vague_predicate", "no threshold stated")
    t = triage(S_OK, IX, statement_id="s2", formalize=f, source_page=1, source_span="s")
    assert t.decision == "refuse" and not t.encodable
    assert t.reason_code == "vague_predicate"
    assert t.as_refusal() is not None and t.as_refusal().reason_code == "vague_predicate"

def test_an_empty_binding_is_never_labelled_not_patient_level_by_guess():
    """'Consider referral after an adequate trial' binds nothing, but it IS
    patient-level -- it is vague. Only the segmenter can make that call."""
    f = lambda text, reg: _refusal("vague_predicate", "x")
    t = triage(S_VAGUE, IX, statement_id="s2", formalize=f)
    assert t.decision == "refuse" and t.reason_code == "unbound_concept"
    assert "NO_BOUND_VOCABULARY" in codes(t)
    t2 = triage(S_VAGUE, IX, statement_id="s4", formalize=f, is_patient_level=False)
    assert t2.reason_code == "not_patient_level"

def test_nothing_binds_means_there_is_no_vocabulary_to_write_in():
    f = lambda text, reg: _rule({"kind": "cmp", "var": "x", "op": "==", "value": True},
                                {"kind": "cmp", "var": "y", "op": "==", "value": True})
    t = triage("Commissioners should ensure services are available.", IX,
               statement_id="s4", formalize=f)
    assert t.decision == "refuse" and t.rule is None

def test_a_rule_that_fails_validation_does_not_get_through():
    """The model producing a rule is not the same as the rule being usable."""
    def f(text, reg):
        # obligation over patient state, not an action -> NO_ACTION_ATOM
        return _rule({"kind": "cmp", "var": "has_diabetes", "op": "==", "value": True},
                     {"kind": "cmp", "var": "albumin_to_creatinine_ratio_mg_g",
                      "op": ">", "value": 30})
    t = triage(S_OK, IX, statement_id="s1", formalize=f, source_page=1, source_span="s")
    assert t.decision != "encode"
    assert "REJECTED_AFTER_ENCODING" in codes(t)

def test_offline_mode_reports_only_deterministic_evidence():
    t = triage(S_OK, IX, statement_id="s1", formalize=None)
    assert t.samples == 0 and t.rule is None and t.decision in {"encode", "refuse"}

# ------------------------------------------------------- the E3 comparison

def test_a_hole_ranges_free_and_produces_MAY_not_a_silent_verdict():
    """The whole point of PARTIAL. Same patient, three encodings:
       hole free   -> MAY-violate, and the witness names the assumption
       stub false  -> CERTIFIED-compliant  (PathWISE's baseline: unsafe)
       stub true   -> MUST-violate         (over-fires)"""
    reg = dsl.Registry([
        dsl.VarDef("egfr", "real", "mL/min/1.73m2", "eGFR", plausible=(0, 200)),
        dsl.VarDef("on_ace_inhibitor", "bool", None, "on ACEi", role="action"),
        dsl.VarDef("has_high_risk", "bool", None, "at high risk", unbound=True)])
    rule = dsl.Rule("s3", dsl.Or([dsl.Cmp("egfr", "<", 30),
                                  dsl.Cmp("has_high_risk", "==", True)]),
                    dsl.Cmp("on_ace_inhibitor", "==", True), "recommend", "ACEi",
                    source_page=1, source_span="s")
    assert dsl.open_holes(rule, reg) == ("has_high_risk",)
    env = dsl.make_env(reg); D = dsl.domain_axioms(reg, env)
    plan = [env["egfr"] == 45, z3.Not(env["on_ace_inhibitor"])]

    def verdict(spec):
        a = z3.Solver(); a.add(D); a.add(plan); a.add(spec)
        b = z3.Solver(); b.add(D); b.add(plan); b.add(z3.Not(spec))
        return ("MUST" if a.check() == z3.unsat else
                "CERTIFIED" if b.check() == z3.unsat else "MAY")

    assert verdict(dsl.rule_to_z3(rule, env)) == "MAY"
    stub_false = z3.Implies(z3.Or(env["egfr"] < 30, z3.BoolVal(False)),
                            env["on_ace_inhibitor"])
    stub_true = z3.Implies(z3.Or(env["egfr"] < 30, z3.BoolVal(True)),
                           env["on_ace_inhibitor"])
    assert verdict(stub_false) == "CERTIFIED"       # silently clears the patient
    assert verdict(stub_true) == "MUST"             # over-fires

def test_no_domain_axiom_is_asserted_over_a_hole():
    """Constraining a hole would be a claim the guideline never made."""
    reg = dsl.Registry([dsl.VarDef("h", "real", None, "hole", plausible=(0, 1),
                                   unbound=True),
                        dsl.VarDef("k", "real", None, "bound", plausible=(0, 1))])
    env = dsl.make_env(reg)
    s = z3.Solver(); s.add(dsl.domain_axioms(reg, env)); s.add(env["h"] == 999)
    assert s.check() == z3.sat                       # the hole is unconstrained
    s2 = z3.Solver(); s2.add(dsl.domain_axioms(reg, env)); s2.add(env["k"] == 999)
    assert s2.check() == z3.unsat                    # the bound variable is not

def test_open_holes_are_a_warning_not_an_error():
    reg = dsl.Registry([dsl.VarDef("h", "bool", None, "hole", unbound=True),
                        dsl.VarDef("a", "bool", None, "act", role="action")])
    rule = dsl.Rule("r", dsl.Cmp("h", "==", True), dsl.Cmp("a", "==", True),
                    "recommend", "x", source_page=1, source_span="s")
    iss = {i.code: i.severity for i in dsl.validate_rule(rule, reg)}
    assert iss.get("OPEN_HOLE") == "warn"
    assert not [c for c, sev in iss.items() if sev == "error"]

# ------------------------------------------------------------- reporting

def test_hole_names_drop_boilerplate_and_stay_readable():
    """Real output before this: has_pediatric_patients_who_have,
    has_require_medium_long_term. Unreadable in a governance report, and too
    coarse to be a variable."""
    assert hole_for("patients who are critically ill", "condition").name == \
        "has_critically_ill"
    assert hole_for("at high risk", "condition").name == "has_high_risk"
    assert hole_for("pediatric patients with solid tumors", "condition").name == \
        "has_solid_tumors"
    assert hole_for("children undergoing noncardiac surgery", "condition").name == \
        "has_noncardiac_surgery"
    assert hole_for("pediatric patients who have experienced trauma",
                    "condition").name == "has_experienced_trauma"

def test_a_whole_clause_is_rejected_as_a_predicate():
    """An undefined PREDICATE is a phrase. A model quoting half a sentence is
    not naming a variable."""
    clause = ("pediatric patients who have experienced trauma and require "
              "ongoing intensive care support")
    declined = [dsl.Refusal("s", "vague_predicate", "x",
                            undefined_predicates=(clause,))]
    assert holes_from(clause, [], declined) == ()
    ok = [dsl.Refusal("s", "vague_predicate", "x",
                      undefined_predicates=("at high risk",))]
    assert len(holes_from("... at high risk ...", [], ok)) == 1

def test_the_registry_handed_to_the_decoder_is_capped():
    """The compiled grammar scales with the variable enum; past ~10 it fails to
    compile at ANY depth and the statement is lost entirely (R8)."""
    seen = []
    def f(text, reg):
        seen.append(len(reg.names()))
        if not any(n.startswith("has_") and reg[n].unbound for n in reg.names()):
            return _refusal("vague_predicate", "x",
                            preds=[f"risk factor {i}" for i in range(12)])
        return _rule({"kind": "cmp", "var": "has_diabetes", "op": "==", "value": True},
                     {"kind": "cmp", "var": "on_ace_inhibitor", "op": "==", "value": True})
    text = S_OK + " " + " ".join(f"risk factor {i}" for i in range(12))
    t = triage(text, IX, statement_id="s1", formalize=f, source_page=1, source_span="s")
    assert max(seen) <= gate.MAX_DECODE_VARS, seen
    assert any(e.code == "REGISTRY_TRUNCATED" for e in t.evidence)

def test_spectrum_separates_index_gaps_from_real_residue():
    """The bucket that would otherwise blame the guideline for OUR retrieval
    failures. Measured on a real document: 8 of 14 refusals were index gaps for
    concepts that exist in SNOMED and VSAC."""
    gap = Triage("a", "x", "refuse", reason_code="unbound_concept",
                 evidence=(gate.Evidence("binder", "unbound_index_gap", "nothing found"),))
    miss = Triage("b", "x", "refuse", reason_code="unbound_concept",
                  evidence=(gate.Evidence("binder", "unbound_unselected", "none matched"),))
    vague = Triage("c", "x", "refuse", reason_code="vague_predicate")
    sp = spectrum([gap, miss, vague])
    assert sp["buckets"] == {"unbound_concept(index_gap)": 1,
                             "unbound_concept(unselected)": 1,
                             "vague_predicate": 1}
    assert sp["retrieval_attributable"] == 2

def test_as_refusal_carries_the_concept_level_evidence():
    """The bundle only ships statement-level refusals, so if the evidence does
    not travel with them the residue report cannot say which concept failed."""
    f = lambda text, reg: _refusal("vague_predicate", "no threshold stated")
    t = triage(S_OK, IX, statement_id="s2", formalize=f, source_page=1, source_span="s")
    r = t.as_refusal()
    assert r is not None and r.evidence
    assert any("model" in e or "binder" in e for e in r.evidence)
    assert r.source_span                       # always points back at the text

def test_spectrum_is_the_M1_six_bucket_table():
    ts = [Triage("a", "x", "encode", fragment="LIA"),
          Triage("b", "x", "encode", fragment="MTL"),
          Triage("c", "x", "partial", fragment="PROP", open_holes=("h",)),
          Triage("d", "x", "refuse", reason_code="vague_predicate"),
          Triage("e", "x", "refuse", reason_code="defeasible")]
    sp = spectrum(ts)
    assert sp["n"] == 5 and sp["encode"] == 2 and sp["partial"] == 1 and sp["refuse"] == 2
    assert sp["in_fragment_rate"] == 0.6 and sp["open_holes"] == 1
    assert sp["buckets"] == {"LIA": 1, "MTL": 1, "PROP": 1,
                             "defeasible": 1, "vague_predicate": 1}

def test_risk_coverage_curve_is_monotone_in_coverage():
    ts = [Triage(f"s{i}", "x", "encode", confidence=c)
          for i, c in enumerate([0.9, 0.8, 0.7, 0.6, 0.5])]
    truth = {"s0": True, "s1": True, "s2": True, "s3": False, "s4": False}
    curve = risk_coverage(ts, truth)
    assert [r["coverage"] for r in curve] == [0.2, 0.4, 0.6, 0.8, 1.0]
    assert curve[0]["precision_on_encodable"] == 1.0
    assert curve[-1]["precision_on_encodable"] == 0.6      # 3 of 5
    assert curve[2]["precision_on_encodable"] >= curve[-1]["precision_on_encodable"]

def test_confidence_comes_from_the_refusal_rate_over_k_samples():
    """The probe found refusals unanimous on clean cases; on borderline ones the
    rate is the natural score, and a risk-coverage curve needs a score."""
    n = {"i": 0}
    def flaky(text, reg):
        n["i"] += 1
        if n["i"] % 2:
            return _refusal("vague_predicate", "unclear")
        return _rule({"kind": "cmp", "var": "has_diabetes", "op": "==", "value": True},
                     {"kind": "cmp", "var": "on_ace_inhibitor", "op": "==", "value": True})
    t = triage(S_OK, IX, statement_id="s1", formalize=flaky, samples=4,
               source_page=1, source_span="s")
    assert t.samples == 4 and t.refusal_rate == 0.5 and t.confidence == 0.5

def test_partial_confidence_describes_the_attempt_that_decided_it():
    """A statement refused 8/8 under the strict vocabulary but encoded cleanly
    once its hole was declared is NOT low-confidence -- reporting the strict
    rate would sort a good PARTIAL to the bottom of the risk-coverage curve."""
    def f(text, reg):
        if "has_high_risk" not in reg.names():
            return _refusal("vague_predicate", "undefined", preds=("at high risk",))
        return _rule({"kind": "or", "operands": [
                        {"kind": "cmp", "var": "egfr", "op": "<", "value": 30},
                        {"kind": "cmp", "var": "has_high_risk", "op": "==", "value": True}]},
                     {"kind": "cmp", "var": "on_ace_inhibitor", "op": "==", "value": True})
    t = triage(S_HOLE, IX, statement_id="s3", formalize=f, samples=4,
               source_page=1, source_span="s")
    assert t.decision == "partial"
    assert t.refusal_rate == 0.0 and t.confidence == 1.0   # the partial attempt, not the strict one

def test_binary_is_the_headline_not_the_fragment():
    """Two labelling passes agreed 28/29 on the binary and only 71.8% on the
    exact fragment, so `encodable` must not depend on the fragment."""
    assert Triage("a", "x", "partial", fragment=None).encodable
    assert Triage("b", "x", "encode", fragment=None).encodable
    assert not Triage("c", "x", "refuse", fragment="LIA").encodable


# ----------------------------------------------- modal predicates (R14b)

S_MODAL = ("We suggest that epidural analgesia may be considered when expertise "
           "is available.")

def test_a_modal_never_becomes_a_hole():
    """"may be considered" reduces to `has_may`. Declaring it as a hole is
    worse than refusing: a hole promises a data source could one day fill it,
    and nothing will ever populate "has may"."""
    declined = [dsl.Refusal("s", "vague_predicate", "x",
                            undefined_predicates=("may be considered",))]
    assert holes_from(S_MODAL, [], declined) == ()

def test_a_clinical_predicate_alongside_a_modal_still_becomes_a_hole():
    """The guard must drop the modal WITHOUT swallowing the real predicate."""
    declined = [dsl.Refusal("s", "vague_predicate", "x",
                            undefined_predicates=("may be considered",
                                                  "at high risk"))]
    assert [h.name for h in holes_from(
        S_MODAL + " Also at high risk.", [], declined)] == ["has_high_risk"]

def test_a_rule_whose_condition_is_a_modal_is_refused_as_defeasible():
    """The whole R14b path: the model encodes `has_may` as a patient
    predicate, the validator rejects it, and the statement is reported as
    discretionary rather than falling through to the generic reason."""
    def formalize(text, reg):
        return _rule({"kind": "cmp", "var": "has_may", "op": "==", "value": True},
                     {"kind": "cmp", "var": "on_ace_inhibitor", "op": "==", "value": True})
    t = triage(S_OK, IX, formalize=formalize, statement_id="s", source_span=S_OK)
    assert t.decision == "refuse"
    assert t.reason_code == "defeasible", t.reason_code

def test_a_vacuous_encoding_is_refused_not_shipped():
    """(A AND B) -> B certifies every patient. It must never reach a bundle."""
    def formalize(text, reg):
        return _rule({"kind": "and", "operands": [
                        {"kind": "cmp", "var": "has_diabetes", "op": "==", "value": True},
                        {"kind": "cmp", "var": "on_ace_inhibitor", "op": "==", "value": True}]},
                     {"kind": "cmp", "var": "on_ace_inhibitor", "op": "==", "value": True})
    t = triage(S_OK, IX, formalize=formalize, statement_id="s", source_span=S_OK)
    assert t.decision == "refuse"
    assert "VACUOUS_RULE" in codes(t)


# ------------------------------------- the registry the rule was written against

def test_triage_carries_the_registry_the_rule_was_written_against():
    """Re-deriving the vocabulary for the bundle shipped a rule referencing a
    variable the shipped registry did not contain: binding goes through a model
    selector, so the second call disagreed with the first. The registry must
    travel WITH the rule."""
    def f(text, reg):
        return _rule({"kind": "cmp", "var": "has_diabetes", "op": "==", "value": True},
                     {"kind": "cmp", "var": "on_ace_inhibitor", "op": "==", "value": True})
    t = triage(S_OK, IX, statement_id="s1", formalize=f, source_span="s")
    assert t.decision == "encode" and t.registry is not None
    assert {"has_diabetes", "on_ace_inhibitor"} <= set(t.registry.names())

def test_every_variable_a_rule_mentions_is_in_the_registry_it_carries():
    """The invariant that was violated. Holds for PARTIAL too, where the
    registry is the WIDE one including the declared holes."""
    def f(text, reg):
        if "has_high_risk" not in reg.names():
            return _refusal("vague_predicate", "undefined", preds=("at high risk",))
        return _rule({"kind": "or", "operands": [
                        {"kind": "cmp", "var": "egfr", "op": "<", "value": 30},
                        {"kind": "cmp", "var": "has_high_risk", "op": "==", "value": True}]},
                     {"kind": "cmp", "var": "on_ace_inhibitor", "op": "==", "value": True})
    t = triage(S_HOLE, IX, statement_id="s3", formalize=f, source_span="s")
    assert t.decision == "partial" and t.registry is not None
    used = dsl._vars_in(t.rule.when) | dsl._vars_in(t.rule.then)
    assert used <= set(t.registry.names()), used - set(t.registry.names())


def test_a_bound_concept_the_rule_ignores_is_reported():
    """DROPPED_CONCEPT_RISK covers a BINDER failure. This covers the binder
    succeeding and the model not using the result -- which is how a population
    restriction goes missing from a rule that still typechecks."""
    def f(text, reg):
        return _rule({"kind": "cmp", "var": "has_diabetes", "op": "==", "value": True},
                     {"kind": "cmp", "var": "on_ace_inhibitor", "op": "==", "value": True})
    t = triage(S_OK, IX, statement_id="s", formalize=f, source_span="s")
    ev = [e for e in t.evidence if e.code == "UNUSED_BOUND_CONCEPT"]
    assert ev, [e.code for e in t.evidence]
    # the ACR bound from this sentence and the rule never mentions it
    assert "albumin" in ev[0].detail

def test_a_rule_using_everything_bound_reports_nothing_unused():
    def f(text, reg):
        acr = next(n for n in reg.names() if n.startswith("albumin"))
        return _rule({"kind": "and", "operands": [
                        {"kind": "cmp", "var": "has_diabetes", "op": "==", "value": True},
                        {"kind": "cmp", "var": acr, "op": ">=", "value": 30}]},
                     {"kind": "cmp", "var": "on_ace_inhibitor", "op": "==", "value": True})
    t = triage(S_OK, IX, statement_id="s", formalize=f, source_span="s")
    assert not [e for e in t.evidence if e.code == "UNUSED_BOUND_CONCEPT"]


# ------------------------------------- transport failures are ours (R27)

class _DeadIndex:
    """A terminology server that is down. Every lookup raises, exactly as a
    timeout or a 503 does."""
    def __init__(self):
        self.failures = bind.TransportFailures()
    def search(self, phrase, kind, k=8):
        exc = ConnectionError("connection refused")
        self.failures.note(phrase, kind, exc)
        return []
    def expand(self, system, code, limit=200):
        return ()
    def validate(self, system, code):
        return False
    def version(self, system):
        return None

def test_a_dead_terminology_server_is_not_the_guidelines_fault():
    """The most dangerous form of "advisory output used as a result": an
    endpoint returning nothing is indistinguishable from a vocabulary that
    lacks the concept, and the second is published as the guideline's residue.
    A network blip must never become a finding about clinical prose."""
    t = triage(S_OK, _DeadIndex(), statement_id="s", source_span=S_OK)
    assert t.decision == "refuse"
    assert "TERMINOLOGY_UNAVAILABLE" in codes(t)

def test_a_transport_failure_counts_as_retrieval_attributable():
    t = triage(S_OK, _DeadIndex(), statement_id="s", source_span=S_OK)
    assert spectrum([t])["retrieval_attributable"] == 1

def test_a_healthy_index_records_no_transport_failure():
    t = triage(S_OK, IX, statement_id="s", source_span=S_OK)
    assert "TERMINOLOGY_UNAVAILABLE" not in codes(t)
    assert len(IX.failures) == 0


def test_a_dropped_clause_is_reported_on_the_partial_path_too():
    """The real PMC12355350:0048: "...in favour of SGLT-2 inhibitors or GLP-1
    receptor agonists; AND a weak recommendation against finerenone in adults
    with CKD". `on_finerenone` bound and reached the registry and was never
    used. The check existed but only ran on the strict-encode branch, so a
    PARTIAL rule dropped a whole clause silently."""
    def f(text, reg):
        if "has_high_risk" not in reg.names():
            return _refusal("vague_predicate", "undefined", preds=("at high risk",))
        return _rule({"kind": "cmp", "var": "has_high_risk", "op": "==", "value": True},
                     {"kind": "cmp", "var": "on_ace_inhibitor", "op": "==", "value": True})
    t = triage(S_HOLE, IX, statement_id="s", formalize=f, source_span="s")
    assert t.decision == "partial"
    ev = [e for e in t.evidence if e.code == "UNUSED_BOUND_CONCEPT"]
    assert ev, [e.code for e in t.evidence]
    assert "egfr" in ev[0].detail        # bound from the sentence, never used


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:
            failed.append(name)
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
