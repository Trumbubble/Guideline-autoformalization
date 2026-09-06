"""Regression tests for the DSL. Each test names the failure it prevents.

Run: python3 test_dsl.py    (no pytest dependency; asserts + a summary line)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import json

import z3

import dsl

from dsl import (
    And, Between, Cmp, EventDef, Not, Or, Refusal, Registry, Rule, Since, VarDef,
    CKD_REGISTRY, check_nondegenerate, domain_axioms, dumps, errors, fragment_of,
    distinguishing_patient, equivalence_classes, equivalent, from_dict, json_schema,
    loads, make_env, rule_to_z3, to_dict, to_z3, validate, validate_rule,
)

R = CKD_REGISTRY

def codes(issues):
    return sorted(i.code for i in issues)

# --------------------------------------------------------------- (1) phi

def test_rule_lowers_to_a_checkable_formula():
    """The old Rule had no consequent, so there was nothing to negate."""
    rule = Rule(
        rule_id="kdigo-acei",
        when=And([Cmp("has_diabetes", "==", True), Cmp("acr_mg_g", ">=", 30)]),
        then=Cmp("on_acei_arb", "==", True),
        action="recommend", target="ACEi or ARB",
        source_page=12, source_span="Treat with an ACEi or ARB ...")
    assert validate_rule(rule, R) == []
    env = make_env(R)
    phi = rule_to_z3(rule, env)

    # A patient the rule applies to, who is not on the drug, VIOLATES it.
    s = z3.Solver(); s.add(domain_axioms(R, env)); s.add(z3.Not(phi))
    assert s.check() == z3.sat
    m = s.model()
    assert m.eval(env["has_diabetes"], True) == z3.BoolVal(True)
    assert m.eval(env["on_acei_arb"], True) == z3.BoolVal(False)

    # ... and the same patient on the drug is compliant.
    s2 = z3.Solver(); s2.add(domain_axioms(R, env))
    s2.add(env["has_diabetes"], env["acr_mg_g"] >= 30, env["on_acei_arb"], z3.Not(phi))
    assert s2.check() == z3.unsat

def test_obligation_must_mention_an_action():
    """An obligation over pure patient state constrains nobody."""
    rule = Rule("r", Cmp("has_diabetes", "==", True), Cmp("egfr", ">", 60),
                "recommend", "x", source_page=1, source_span="s")
    assert "NO_ACTION_ATOM" in codes(validate_rule(rule, R))

# --------------------------------------------------------------- (2) time

def test_since_needs_an_occurrence_flag():
    """A bare days_since_* lets the solver satisfy a monitoring window for a
    patient who was never checked at all - the commonest real violation."""
    env = make_env(R)
    within14 = to_z3(Since("potassium_check", 14), env)
    s = z3.Solver(); s.add(domain_axioms(R, env))
    s.add(within14, z3.Not(env["potassium_check_occurred"]))
    assert s.check() == z3.unsat            # cannot be "within 14 days" if never done

    # The never-checked patient is reachable as a violation of the window.
    s2 = z3.Solver(); s2.add(domain_axioms(R, env))
    s2.add(z3.Not(within14), z3.Not(env["potassium_check_occurred"]))
    assert s2.check() == z3.sat

def test_since_window_boundary_is_exact():
    env = make_env(R)
    d = env["days_since_potassium_check"]
    for days, inclusive, expect_sat in [(14, True, True), (14, False, False)]:
        s = z3.Solver(); s.add(domain_axioms(R, env))
        s.add(to_z3(Since("potassium_check", days, inclusive), env), d == 14)
        assert (s.check() == z3.sat) is expect_sat

def test_monitoring_rule_reports_as_MTL_not_LIA():
    """M1's spectrum must not silently reclassify a flattened monitoring rule."""
    rule = Rule("mon", Cmp("on_acei_arb", "==", True),
                Since("potassium_check", 14), "monitor", "serum potassium",
                source_page=3, source_span="check within 14 days")
    assert validate_rule(rule, R) == []
    assert fragment_of(rule.when, rule.then) == "MTL"
    assert fragment_of(Cmp("egfr", "<", 30)) == "LIA"
    assert fragment_of(And([Cmp("has_diabetes", "==", True),
                            Cmp("on_dialysis", "==", False)])) == "PROP"

def test_unknown_event_is_caught():
    assert "UNKNOWN_EVENT" in codes(validate(Since("bp_check", 7), R))

# --------------------------------------------------------------- (3) types

def test_bool_is_not_a_number():
    """isinstance(True, int) is True, so `egfr > true` used to typecheck."""
    assert "TYPE_ERROR" in codes(validate(Cmp("egfr", ">", True), R))

def test_int_vars_lower_to_Int_not_Real():
    """A Real day-count lets the solver hand Track B days = 14.5."""
    reg = Registry([VarDef("days", "int", "d", "days since check")])
    assert z3.is_int(make_env(reg)["days"])
    assert "TYPE_ERROR" in codes(validate(Cmp("days", "<=", 14.5), reg))

def test_code_vars_are_equality_only_and_value_set_bound():
    reg = Registry([VarDef("dx", "code", None, "diagnosis", "SNOMED",
                           codes=("SNOMED:44054006", "SNOMED:38341003"))])
    assert "TYPE_ERROR" in codes(validate(Cmp("dx", "<", "SNOMED:44054006"), reg))
    assert "UNBOUND_CODE" in codes(validate(Cmp("dx", "==", "SNOMED:99999999"), reg))
    assert validate(Cmp("dx", "==", "SNOMED:44054006"), reg) == []
    # ... and it lowers without crashing, constrained to the value set.
    env = make_env(reg)
    s = z3.Solver(); s.add(domain_axioms(reg, env))
    s.add(to_z3(Cmp("dx", "!=", "SNOMED:44054006"), env))
    assert s.check() == z3.sat
    assert str(s.model()[env["dx"]]) == '"SNOMED:38341003"'

def test_date_vars_round_trip_through_ordinals():
    reg = Registry([VarDef("last_visit", "date", None, "date of last visit")])
    assert "TYPE_ERROR" in codes(validate(Cmp("last_visit", ">=", "31/01/2024"), reg))
    assert validate(Cmp("last_visit", ">=", "2024-01-31"), reg) == []
    env = make_env(reg)
    s = z3.Solver()
    s.add(to_z3(Cmp("last_visit", ">=", "2024-01-31"), env),
          to_z3(Cmp("last_visit", "<", "2024-02-01"), env))
    assert s.check() == z3.sat

# --------------------------------------------------------------- (4) checks

def test_empty_interval_with_open_bound():
    assert "EMPTY_INTERVAL" in codes(validate(Between("egfr", 30, 30, False, False), R))
    assert "EMPTY_INTERVAL" in codes(validate(Between("egfr", 60, 30), R))
    assert validate(Between("egfr", 30, 60, True, False), R) == []

def test_unit_slip_is_flagged_as_a_warning():
    """ACR in mg/mmol vs mg/g is a factor of ~8.84; potassium of 500 is a slip."""
    iss = validate(Cmp("potassium", ">", 500), R)
    assert codes(iss) == ["IMPLAUSIBLE_THRESHOLD"]
    assert iss[0].severity == "warn" and errors(iss) == []

def test_exact_equality_on_a_real_is_a_warning():
    iss = validate(Cmp("egfr", "==", 30), R)
    assert codes(iss) == ["EXACT_REAL_COMPARISON"] and errors(iss) == []

def test_vacuity_sweep_catches_the_and_for_or_bug():
    """P3: `refer if eGFR < 30 OR ACR >= 300` rendered as AND is satisfiable,
    but the same slip on a single variable makes the rule unfireable."""
    bad = Rule("s7", And([Cmp("egfr", "<", 30), Cmp("egfr", ">", 60)]),
               Cmp("nephrology_referral", "==", True),
               "refer", "nephrology", source_page=9, source_span="...")
    assert validate_rule(bad, R) == []                     # typechecks fine
    assert "UNSATISFIABLE_ANTECEDENT" in codes(check_nondegenerate(bad, R))

def test_vacuity_sweep_catches_a_rule_that_forbids_nothing():
    vacuous = Rule("v", Cmp("egfr", "<", 30), Or([Cmp("on_dialysis", "==", True),
                                                  Cmp("on_dialysis", "==", False),
                                                  Cmp("on_acei_arb", "==", True)]),
                   "recommend", "anything", source_page=1, source_span="s")
    assert "VACUOUS_RULE" in codes(check_nondegenerate(vacuous, R))

def test_tautological_antecedent_is_a_warning():
    taut = Rule("t", Or([Cmp("egfr", "<", 30), Cmp("egfr", ">=", 30)]),
                Cmp("nephrology_referral", "==", True), "refer", "nephrology",
                source_page=1, source_span="s")
    iss = check_nondegenerate(taut, R)
    assert "TAUTOLOGICAL_ANTECEDENT" in codes(iss)
    assert all(i.severity == "warn" for i in iss if i.code == "TAUTOLOGICAL_ANTECEDENT")

def test_check_nondegenerate_reports_an_illtyped_rule_instead_of_crashing():
    """It runs on MODEL OUTPUT, where a boolean compared to a number is a
    normal thing to receive. Crashing there took down a whole guideline run."""
    bad = Rule("b", And([Cmp("has_diabetes", "==", 60)]),
               Cmp("on_acei_arb", "==", True), "recommend", "x",
               source_page=1, source_span="s")
    assert "TYPE_ERROR" in codes(validate_rule(bad, R))
    got = codes(dsl.check_nondegenerate(bad, R))
    assert got == ["NOT_LOWERABLE"], got

def test_a_good_rule_is_nondegenerate():
    good = Rule("g", And([Cmp("has_diabetes", "==", True), Cmp("acr_mg_g", ">=", 30)]),
                Cmp("on_acei_arb", "==", True), "recommend", "ACEi or ARB",
                source_page=12, source_span="...")
    assert check_nondegenerate(good, R) == []

def test_domain_axioms_stop_the_egfr_equals_2_counterexample():
    env = make_env(R)
    unconstrained = z3.Solver(); unconstrained.add(env["egfr"] < 30)
    assert unconstrained.check() == z3.sat
    bounded = z3.Solver(); bounded.add(domain_axioms(R, env)); bounded.add(env["egfr"] < 0)
    assert bounded.check() == z3.unsat

def test_missing_provenance_is_an_error():
    r = Rule("p", Cmp("egfr", "<", 30), Cmp("nephrology_referral", "==", True),
             "refer", "nephrology")
    assert "MISSING_PROVENANCE" in codes(validate_rule(r, R))

def test_an_xml_locator_is_provenance_even_without_a_page():
    """JATS has no page numbers -- the XPath IS the pointer back to the source.
    Requiring `source_page` made every rule from an XML document fail a hard
    error, so `encode` was unreachable and a whole guideline came back 14/14
    refused."""
    r = Rule("p", Cmp("egfr", "<", 30), Cmp("nephrology_referral", "==", True),
             "refer", "nephrology", source_page=None,
             source_span="/article/body/sec[5]/boxed-text")
    assert "MISSING_PROVENANCE" not in codes(validate_rule(r, R))
    assert validate_rule(r, R) == []

def test_semantic_equivalence_sees_what_the_compiler_cannot():
    """Both rules typecheck; only the solver can see they mean different
    things. This is the oracle the whole differential-validation idea rests on."""
    mk = lambda when: Rule("r", when, Cmp("nephrology_referral", "==", True),
                           "refer", "nephrology", source_page=1, source_span="s")
    # P3's bug: a disjunction rendered as a conjunction. Well-typed, wrong.
    or_rule = mk(Or([Cmp("egfr", "<", 30), Cmp("acr_mg_g", ">=", 300)]))
    and_rule = mk(And([Cmp("egfr", "<", 30), Cmp("acr_mg_g", ">=", 300)]))
    assert validate_rule(or_rule, R) == [] and validate_rule(and_rule, R) == []
    assert not equivalent(or_rule, and_rule, R)
    p = distinguishing_patient(or_rule, and_rule, R)
    assert p is not None and (p["egfr"] < 30) != (p["acr_mg_g"] >= 300)

def test_equivalent_rules_written_differently_are_recognised():
    a = Rule("a", Between("egfr", 30, 60, True, True), Cmp("nephrology_referral", "==", True),
             "refer", "n", source_page=1, source_span="s")
    b = Rule("b", And([Cmp("egfr", ">=", 30), Cmp("egfr", "<=", 60)]),
             Cmp("nephrology_referral", "==", True), "refer", "n",
             source_page=1, source_span="s")
    assert equivalent(a, b, R) and distinguishing_patient(a, b, R) is None

def test_an_inclusivity_slip_is_a_real_disagreement():
    """eGFR exactly 30: the boundary bug that compiles cleanly."""
    a = Rule("a", Cmp("egfr", "<", 30), Cmp("nephrology_referral", "==", True),
             "refer", "n", source_page=1, source_span="s")
    b = Rule("b", Cmp("egfr", "<=", 30), Cmp("nephrology_referral", "==", True),
             "refer", "n", source_page=1, source_span="s")
    p = distinguishing_patient(a, b, R)
    assert p is not None and p["egfr"] == 30.0

def test_equivalence_classes_partition_samples():
    mk = lambda t: Rule("r", Cmp("egfr", "<", t), Cmp("nephrology_referral", "==", True),
                        "refer", "n", source_page=1, source_span="s")
    classes = equivalence_classes([mk(30), mk(30), mk(45), mk(30)], R)
    assert sorted(len(c) for c in classes) == [1, 3]

def test_decoding_schema_avoids_every_unsupported_construct():
    """Measured against claude-opus-5's structured-output validator. Each of
    these is a 400, and a recursive expression grammar hits the first one by
    construction -- so the recursion is unrolled to a fixed depth instead."""
    import json as _json
    sch = dsl.decoding_schema(R, max_depth=2)
    blob = _json.dumps(sch)
    assert "oneOf" not in blob                     # `oneOf` is rejected outright
    assert '"minimum"' not in blob and '"maximum"' not in blob
    assert '"minItems": 2' not in blob             # only 0 or 1 are accepted
    assert sch["type"] == "object"                 # $defs needs an object root
    # no cycles: every $ref target is defined earlier than its user
    defs = sch["$defs"]
    assert "expr2" in defs and "expr0" in defs
    assert _json.dumps(defs["expr0"]).count("expr") == 0    # leaves bottom out

def test_every_schema_we_send_is_api_compatible():
    """Constraint-check without a network call. Each rule was learned from a
    400, and a rejected schema does NOT surface as an error -- it falls back to
    the offline heuristic, which looks exactly like the model finding nothing.
    `bind.MENTION_SCHEMA` was missing one `additionalProperties` and the model
    path silently never ran on a whole guideline."""
    import bind, segment
    for name, sch in [("decoding_schema", dsl.decoding_schema(R)),
                      ("decoding_schema d1", dsl.decoding_schema(R, 1)),
                      ("MENTION_SCHEMA", bind.MENTION_SCHEMA),
                      ("SELECT_SCHEMA", bind.SELECT_SCHEMA),
                      ("CLASSIFIER_SCHEMA", segment.CLASSIFIER_SCHEMA)]:
        assert dsl.check_decoding_schema(sch) == [], (name, dsl.check_decoding_schema(sch))

def test_the_schema_checker_catches_each_known_rejection():
    bad = [
        ({"type": "object", "required": [], "properties": {"a": {"type": "string"}}},
         "additionalProperties"),
        ({"type": "object", "required": [], "additionalProperties": False,
          "properties": {"n": {"type": "integer", "minimum": 0}}}, "minimum"),
        ({"type": "object", "required": [], "additionalProperties": False,
          "properties": {"xs": {"type": "array", "minItems": 2,
                                "items": {"type": "string"}}}}, "minItems"),
        ({"oneOf": [{"type": "string"}]}, "oneOf"),
        ({"anyOf": [{"type": "string"}], "$defs": {"x": {"type": "string"}}}, "$defs"),
    ]
    for schema, needle in bad:
        found = dsl.check_decoding_schema(schema)
        assert any(needle in f for f in found), (needle, found)

def test_decoding_schema_still_closes_the_vocabulary():
    sch = dsl.decoding_schema(R)
    assert sch["$defs"]["var"]["enum"] == R.names()
    assert sch["$defs"]["event"]["enum"] == R.event_names()
    assert set(sch["properties"]["outcome"]["enum"]) == {"rule", "refusal"}

def test_from_decoded_unwraps_the_discriminator():
    rule = Rule("r", Cmp("egfr", "<", 30), Cmp("nephrology_referral", "==", True),
                "refer", "nephrology", source_page=1, source_span="s")
    assert dsl.from_decoded({"outcome": "rule", "rule": to_dict(rule),
                             "refusal": None}) == rule
    ref = Refusal("r", "vague_predicate", "no threshold stated")
    assert dsl.from_decoded({"outcome": "refusal", "rule": None,
                             "refusal": to_dict(ref)}) == ref
    try:
        dsl.from_decoded({"outcome": "rule", "rule": None, "refusal": None})
        assert False, "should have raised"
    except ValueError:
        pass

def test_a_smaller_registry_permits_deeper_nesting():
    """The grammar-size ceiling scales with the variable enum, so the registry
    handed to the decoder should be the STATEMENT's bound vocabulary."""
    import json as _json
    small = Registry([R["egfr"], R["nephrology_referral"]])
    assert len(_json.dumps(dsl.decoding_schema(small, 2))) < \
           len(_json.dumps(dsl.decoding_schema(R, 2)))

# --------------------------------------------------------------- (5) json

def test_rule_json_round_trip():
    rule = Rule("rt", Or([Between("egfr", 30, 60, True, False),
                          Not(Cmp("has_diabetes", "==", True)),
                          Since("acr_check", 365)]),
                Cmp("nephrology_referral", "==", True), "refer", "nephrology",
                strength="1B", source_page=7, source_span="Refer when ...")
    assert from_dict(to_dict(rule)) == rule
    assert loads(dumps([rule])) == [rule]
    json.dumps(to_dict(rule))                       # must be JSON-serialisable

def test_a_refusal_carries_the_evidence_beneath_it():
    """"unbound_concept" alone cannot be triaged. A reader needs to know WHICH
    concept failed, to tell a vague guideline from a gap in our index."""
    r = Refusal("s4", "unbound_concept", "nothing resolved",
                undefined_predicates=("an adequate trial",),
                evidence=("[binder/unbound_index_gap] no code for 'deprescribing'",))
    assert from_dict(to_dict(r)) == r
    assert "evidence" in to_dict(r)
    sch = dsl.decoding_schema(R)
    assert "evidence" in sch["$defs"]["refusal"]["properties"]

def test_refusal_json_round_trip():
    ref = Refusal("r5", "vague_predicate",
                  "'after an adequate trial of conservative therapy' has no threshold",
                  source_page=4, source_span="...", confidence=0.91)
    assert from_dict(to_dict(ref)) == ref
    assert loads(dumps([ref, ref])) == [ref, ref]

def test_schema_and_validator_agree_on_value_types():
    sch = json_schema(R)
    assert sch["$defs"]["event"]["enum"] == R.event_names()
    assert "then" in sch["$defs"]["rule"]["required"]
    kinds = {b["properties"]["kind"]["const"] for b in sch["$defs"]["expr"]["oneOf"]}
    assert kinds == {"cmp", "between", "since", "not", "and", "or"}
    # `between` must force the inclusivity decision to be made explicitly.
    btw = next(b for b in sch["$defs"]["expr"]["oneOf"]
               if b["properties"]["kind"]["const"] == "between")
    assert {"lo_inclusive", "hi_inclusive"} <= set(btw["required"])

def test_degenerate_junction():
    assert "DEGENERATE_JUNCTION" in codes(validate(And([Cmp("egfr", "<", 30)]), R))


# ------------------------------------------- (N) canonical form and modals
# The PMC13270348 failure: two names for one code made a tautology look
# contingent, and a deontic modal became a patient predicate. See RESULTS.md R14.

_PCA = "http://snomed.info/sct|1351574007|20260801"

def _pca_registry():
    return Registry([
        VarDef("has_ap", "bool", binding="http://snomed.info/sct|197456007|x",
               role="observation"),
        VarDef("pca", "bool", binding=_PCA, role="action"),
        VarDef("patient_controlled_intravenous_analgesia", "bool",
               binding=_PCA, role="action"),
    ])

def test_two_variables_on_one_code_are_aliased():
    a = dsl.canonical_aliases(_pca_registry())
    assert a == {"pca": "patient_controlled_intravenous_analgesia"}

def test_a_hole_restating_a_bound_variable_is_aliased_onto_it():
    """`has_close_monitoring` (unbound) alongside `close_monitoring` (bound) is
    a duplicate, not residue -- and the rule used the hole, so it was reported
    PARTIAL despite the concept having bound."""
    reg = Registry([
        VarDef("close_monitoring", "bool", binding="http://snomed.info/sct|182777000|x",
               role="action"),
        VarDef("has_close_monitoring", "bool", role="observation", unbound=True)])
    assert dsl.canonical_aliases(reg) == {"has_close_monitoring": "close_monitoring"}

def test_an_unbound_variable_with_no_bound_twin_is_left_alone():
    reg = Registry([VarDef("has_high_risk", "bool", role="observation", unbound=True)])
    assert dsl.canonical_aliases(reg) == {}

def test_the_binding_collision_tautology_is_caught():
    """(has_ap AND pca) -> patient_controlled_intravenous_analgesia denotes
    (A AND B) -> B. It shipped as a sound encoding because the two names are
    two SYMBOLS; the identity is in the terminology layer."""
    reg = _pca_registry()
    rule = Rule("t", And([Cmp("has_ap", "==", True), Cmp("pca", "==", True)]),
                Cmp("patient_controlled_intravenous_analgesia", "==", True),
                "monitor", "PCA", source_span="x")
    assert "VACUOUS_RULE" in codes(check_nondegenerate(rule, reg))

def test_the_same_rule_over_distinct_concepts_is_not_vacuous():
    """The check must not fire just because a rule mentions its consequent's
    neighbours -- only when the two really are one concept."""
    reg = Registry([
        VarDef("has_ap", "bool", binding="http://snomed.info/sct|197456007|x"),
        VarDef("pca", "bool", binding=_PCA, role="action"),
        VarDef("epidural", "bool", binding="http://snomed.info/sct|18946005|x",
               role="action")])
    rule = Rule("t", And([Cmp("has_ap", "==", True), Cmp("pca", "==", True)]),
                Cmp("epidural", "==", True), "recommend", "epidural", source_span="x")
    assert "VACUOUS_RULE" not in codes(check_nondegenerate(rule, reg))

def test_canonicalize_rewrites_rules_and_shrinks_the_registry():
    reg = _pca_registry()
    rule = Rule("t", Cmp("pca", "==", True), Cmp("has_ap", "==", True),
                "monitor", "x", source_span="x")
    reg2, rules2, alias = dsl.canonicalize(reg, [rule])
    assert len(reg2.names()) == 2 and "pca" not in reg2
    assert rules2[0].when.var == "patient_controlled_intravenous_analgesia"
    assert alias == {"pca": "patient_controlled_intravenous_analgesia"}

def test_canonicalize_leaves_events_alone():
    """Since() names an EventDef, a separate namespace from variables."""
    reg = Registry([VarDef("a", "bool", binding="s|1|x"),
                    VarDef("aa", "bool", binding="s|1|x")], [EventDef("dialysis")])
    rule = Rule("t", Since("dialysis", 30), Cmp("a", "==", True),
                "monitor", "x", source_span="x")
    _, rules2, _ = dsl.canonicalize(reg, [rule])
    assert rules2[0].when.event == "dialysis"

def test_the_bundle_is_canonical_and_says_what_it_merged():
    reg = _pca_registry()
    rule = Rule("t", Cmp("pca", "==", True), Cmp("has_ap", "==", True),
                "monitor", "x", source_span="x")
    d = dsl.bundle_to_dict(reg, [rule])
    assert len(d["registry"]["variables"]) == 2
    assert d["merged_concepts"] == {"pca": "patient_controlled_intravenous_analgesia"}

def test_a_clean_bundle_carries_no_merge_record():
    d = dsl.bundle_to_dict(Registry([VarDef("a", "bool", binding="s|1|x")]), [])
    assert "merged_concepts" not in d

def test_modal_only_separates_deontic_force_from_clinical_predicates():
    assert dsl.modal_only("has_may")
    assert dsl.modal_only("is_recommended")
    assert dsl.modal_only("has_considered")
    assert not dsl.modal_only("has_close_monitoring")
    assert not dsl.modal_only("has_appropriate_acupoints")   # vague, but clinical
    assert not dsl.modal_only("egfr")

def test_a_modal_predicate_is_a_validation_error():
    """`has_may`, distilled from "may be considered", is the guideline's own
    strength wearing a patient predicate's clothes. Nothing populates it, so
    the rule is UNKNOWN for every patient forever."""
    reg = Registry([VarDef("has_may", "bool", role="observation", unbound=True),
                    VarDef("epidural", "bool", binding="s|1|x", role="action")])
    rule = Rule("t", Cmp("has_may", "==", True), Cmp("epidural", "==", True),
                "recommend", "epidural", source_span="x")
    iss = [i for i in validate_rule(rule, reg) if i.severity == "error"]
    assert "MODAL_PREDICATE" in [i.code for i in iss]

def test_strength_in_its_own_slot_is_not_an_error():
    """The DSL already separates deontic force from the condition; the check
    must object only when the modal LEAKS into the logic."""
    reg = Registry([VarDef("has_ap", "bool", binding="s|1|x"),
                    VarDef("epidural", "bool", binding="s|2|x", role="action")])
    rule = Rule("t", Cmp("has_ap", "==", True), Cmp("epidural", "==", True),
                "recommend", "epidural", strength="suggest (conditional)",
                source_span="x")
    assert "MODAL_PREDICATE" not in codes(validate_rule(rule, reg))


# ------------------------------------------- (N+1) deontic force (R17b)

def test_explicit_permission_is_detected():
    assert dsl.is_permissive("weak (suggest); may be considered")
    assert dsl.is_permissive(None, "epidural analgesia may be considered when "
                                   "expertise is available")
    assert dsl.is_permissive("optional")

def test_grade_conditional_is_not_permission():
    """"We suggest anticoagulant prophylaxis" still recommends the ACTION --
    the weakness is in the evidence, not in the obligation. Treating every weak
    recommendation as optional would silence most of a guideline."""
    assert not dsl.is_permissive("weak (suggest)", "We suggest anticoagulant "
                                                  "prophylaxis in this population.")
    assert not dsl.is_permissive("conditional recommendation")
    assert not dsl.is_permissive("strong", "We recommend prophylaxis.")

def test_permission_is_read_from_the_source_when_strength_is_loose():
    """The model's strength summary is not always faithful; the span is
    verbatim from the document, so both are consulted."""
    assert dsl.is_permissive("suggest (conditional)",
                             "We suggest that acupuncture may be considered.")


# ------------------------------------- (N+2) deontic operator (R18)

def _deontic_reg():
    return Registry([VarDef("a", "bool", binding="s|1|x"),
                     VarDef("b", "bool", binding="s|2|x"),
                     VarDef("x", "bool", binding="s|3|x", role="action")])

def _rule_d(deontic, span="We recommend x.", when="a", then_val=True, rid="r"):
    return Rule(rid, Cmp(when, "==", True), Cmp("x", "==", then_val),
                "recommend", "x", deontic=deontic, source_span=span)

def test_the_operator_round_trips():
    r = _rule_d("permission")
    assert to_dict(r)["deontic"] == "permission"
    assert from_dict(to_dict(r)).deontic == "permission"

def test_a_rule_with_no_operator_defaults_to_obligation():
    """Bundles written before the field existed must not silently become
    permissions -- the default has to be the stricter reading."""
    d = to_dict(_rule_d("obligation"))
    del d["deontic"]
    assert from_dict(d).deontic == "obligation"

def test_the_wording_can_only_strengthen_permission_never_weaken_it():
    """The fallback moves obligation -> permission, never the reverse. Guessing
    permission wrongly silences an alert, which is recoverable; guessing
    obligation wrongly accuses a clinician, which is not."""
    assert dsl.deontic_of(_rule_d("obligation", "x may be considered")) == "permission"
    assert dsl.deontic_of(_rule_d("permission", "We recommend x.")) == "permission"
    assert dsl.deontic_of(_rule_d("obligation", "We recommend x.")) == "obligation"

def test_a_permission_can_never_be_violated():
    """The soundness property the operator buys, checked directly on the
    formula rather than on a verdict string: there is no patient, and no
    completion of the unknowns, for which a permission is violated."""
    reg = _deontic_reg()
    env = make_env(reg)
    s = z3.Solver()
    s.add(*domain_axioms(reg, env))
    s.add(dsl.violation_condition(_rule_d("permission"), env))
    assert s.check() == z3.unsat

def test_an_obligation_can_be_violated():
    reg = _deontic_reg()
    env = make_env(reg)
    s = z3.Solver()
    s.add(*domain_axioms(reg, env))
    s.add(dsl.violation_condition(_rule_d("obligation"), env))
    assert s.check() == z3.sat

def test_permitting_what_another_rule_forbids_is_a_conflict():
    """The check the operator makes POSSIBLE. Collapse permission into
    obligation and "x may be considered" and "avoid x" are just two
    obligations about x -- their disagreement is invisible."""
    reg = _deontic_reg()
    permit = _rule_d("permission", "x may be considered", rid="perm")
    forbid = _rule_d("obligation", "Avoid x.", when="b", then_val=False, rid="avoid")
    got = dsl.conflicts([permit, forbid], reg)
    assert len(got) == 1
    assert got[0]["permits"] == "perm" and got[0]["requires"] == "avoid"
    assert got[0]["shared_variables"] == ["x"]

def test_two_obligations_are_not_a_deontic_conflict():
    """They may still contradict each other, but that is not what this check
    is for -- reporting it here would bury the signal."""
    reg = _deontic_reg()
    a = _rule_d("obligation", "We recommend x.", rid="a")
    b = _rule_d("obligation", "Avoid x.", when="b", then_val=False, rid="b")
    assert dsl.conflicts([a, b], reg) == []

def test_rules_over_different_actions_do_not_conflict():
    reg = Registry([VarDef("a", "bool", binding="s|1|x"),
                    VarDef("x", "bool", binding="s|2|x", role="action"),
                    VarDef("y", "bool", binding="s|3|x", role="action")])
    permit = Rule("p", Cmp("a", "==", True), Cmp("x", "==", True),
                  "recommend", "x", deontic="permission", source_span="s")
    other = Rule("o", Cmp("a", "==", True), Cmp("y", "==", True),
                 "recommend", "y", deontic="obligation", source_span="s")
    assert dsl.conflicts([permit, other], reg) == []

def test_the_decoding_schema_forces_a_choice_of_operator():
    """`deontic` is REQUIRED, so the model must commit rather than letting the
    default quietly promote a permission into an obligation."""
    sch = dsl.decoding_schema(CKD_REGISTRY, 1)
    rule = sch["$defs"]["rule"]
    assert "deontic" in rule["required"]
    assert rule["properties"]["deontic"]["enum"] == ["obligation", "permission"]
    assert dsl.check_decoding_schema(sch) == []


# ------------------------------------- (N+3) the action must survive (R19)

def _action_reg():
    return Registry([
        VarDef("has_expertise_available", "bool", role="observation", unbound=True),
        VarDef("strict_aseptic_techniques", "bool", binding="s|1|x", role="action",
               description="Implements aseptic technique"),
        VarDef("epidural", "bool", binding="s|2|x", role="action",
               description="Epidural analgesia"),
        VarDef("on_ace_inhibitor", "bool", binding="s|3|x", role="action",
               description="ACE INHIBITORS, PLAIN")])

def _act_codes(then, target, reg=None):
    r = Rule("r", Cmp("has_expertise_available", "==", True), Cmp(then, "==", True),
             "recommend", target, source_span="x")
    return [i.code for i in validate_rule(r, reg or _action_reg())]

def test_the_recommended_action_going_missing_is_flagged():
    """The real PMC13270348:0143 shape: "epidural analgesia may be considered
    ... aseptic technique and close monitoring are essential" encoded as
    expertise -> aseptic technique. The epidural is gone and its PRECONDITION
    was promoted to be the recommendation. NO_ACTION_ATOM passed, because
    aseptic technique is itself an action variable."""
    assert "ACTION_TARGET_MISMATCH" in _act_codes(
        "strict_aseptic_techniques", "epidural analgesia")

def test_the_right_action_is_not_flagged():
    assert "ACTION_TARGET_MISMATCH" not in _act_codes("epidural", "epidural analgesia")

def test_an_abbreviated_target_is_not_a_mismatch():
    """`target` is a human gloss and abbreviates. Requiring exact token
    equality fired on a correct rule -- "ACEi" vs `on_ace_inhibitor`."""
    assert "ACTION_TARGET_MISMATCH" not in _act_codes("on_ace_inhibitor", "ACEi")
    assert "ACTION_TARGET_MISMATCH" not in _act_codes("on_ace_inhibitor", "ACE inhibitor")

def test_the_mismatch_is_a_warning_not_an_error():
    """The match is lexical and `target` may name the action in a different
    vocabulary than the variable does ("thromboprophylaxis" vs `on_lmwh`), so
    rejecting on it would discard good rules. It belongs in the report."""
    reg = _action_reg()
    r = Rule("r", Cmp("has_expertise_available", "==", True),
             Cmp("strict_aseptic_techniques", "==", True),
             "recommend", "epidural analgesia", source_span="x")
    bad = [i for i in validate_rule(r, reg) if i.code == "ACTION_TARGET_MISMATCH"]
    assert bad and all(i.severity == "warn" for i in bad)


# ------------------------------------- (N+4) dropped quantities (R25)

def _q_reg():
    return Registry([VarDef("mt", "bool", binding="s|1|x"),
                     VarDef("sbp", "real", unit="mm[Hg]", binding="s|2|x",
                            plausible=(40.0, 260.0)),
                     VarDef("lowering", "bool", binding="s|3|x", role="action")])

def _q_rule(when, span):
    return Rule("r", when, Cmp("lowering", "==", False), "avoid", "lowering",
                source_span=span)

SPAN = ("We recommend against intensively lowering systolic BP < 140 mmHg in the "
        "first 24 h after successful mechanical thrombectomy.")

def test_a_dropped_threshold_is_flagged():
    """The real PMC13151662:0002. The rule typechecks, is non-vacuous, names the
    right action and compiles -- and forbids BP lowering after thrombectomy
    outright, because 140 mmHg and the 24 h window are both gone. Nothing else
    in the validator can see a deletion."""
    r = _q_rule(Cmp("mt", "==", True), SPAN)
    iss = [i for i in validate_rule(r, _q_reg()) if i.code == "DROPPED_THRESHOLD"]
    assert iss and "140" in iss[0].message and "24" in iss[0].message

def test_keeping_the_threshold_is_not_flagged():
    r = Rule("r", And([Cmp("mt", "==", True), Cmp("sbp", "<", 140)]),
             Cmp("lowering", "==", False), "avoid", "lowering", source_span=SPAN)
    got = [q for q in dsl.dropped_quantities(r)]
    assert [u for _, u in got] == ["h"], got      # only the window is still gone

def test_a_duration_counts_as_kept_in_any_unit():
    """"the first 24 h" and Since(event, days=1) are the same window."""
    reg = Registry([VarDef("lowering", "bool", binding="s|1|x", role="action")],
                   [EventDef("mt")])
    r = Rule("r", Since("mt", 1), Cmp("lowering", "==", False), "avoid", "x",
             source_span="avoid lowering BP in the first 24 h after thrombectomy")
    assert dsl.dropped_quantities(r) == []

def test_a_range_is_kept_by_either_endpoint():
    """"a minimum of 150-240 min" encoded as `>= 150` has kept the quantity.
    Flagging the missing 240 would be a false alarm on a correct rule."""
    reg = Registry([VarDef("mins", "real", binding="s|1|x"),
                    VarDef("act", "bool", binding="s|2|x", role="action")])
    r = Rule("r", Cmp("mins", ">=", 150), Cmp("act", "==", True), "recommend", "x",
             source_span="engaging in a minimum of 150-240 min per week")
    assert dsl.dropped_quantities(r) == []

def test_a_blood_pressure_pair_needs_both_numbers():
    """185/110 is systolic AND diastolic -- two measurements, not a range."""
    reg = Registry([VarDef("sbp", "real", binding="s|1|x"),
                    VarDef("act", "bool", binding="s|2|x", role="action")])
    r = Rule("r", Cmp("sbp", "<", 185), Cmp("act", "==", True), "recommend", "x",
             source_span="maintaining BP <185/110 mmHg before thrombolysis")
    assert dsl.dropped_quantities(r) == [(110.0, "mmhg")]

def test_statistics_are_not_thresholds():
    """"25 studies; n = 15,374; 95% CI 0.87-1.08" is evidence. Flagging it would
    bury the signal under every graded recommendation in the corpus."""
    r = _q_rule(Cmp("mt", "==", True),
                "deprescribing had no effect on mortality (odds ratio [OR], 0.97; "
                "95% CI, 0.87-1.08; 25 studies; n = 15,374; low certainty).")
    assert dsl.dropped_quantities(r) == []

def test_the_check_is_a_warning():
    """Guideline sentences carry numbers that should NOT be in the rule. This
    reports for review; it never rejects."""
    r = _q_rule(Cmp("mt", "==", True), SPAN)
    iss = [i for i in validate_rule(r, _q_reg()) if i.code == "DROPPED_THRESHOLD"]
    assert iss and all(i.severity == "warn" for i in iss)

def test_a_source_with_no_quantities_is_never_flagged():
    r = _q_rule(Cmp("mt", "==", True),
                "We advise against routine pre-hospital BP lowering in suspected stroke.")
    assert dsl.dropped_quantities(r) == []


def test_a_dose_recorded_in_the_target_is_not_dropped():
    """"Reduce the apixaban dose to 2.5 mg twice daily if at least two of ..."
    correctly encodes the three criteria as numeric comparisons and the ACTION
    as a boolean, recording the dose in `target`. The 2.5 is not lost, and
    flagging it broke the import of a hand-written fixture rule."""
    reg = Registry([VarDef("age", "int", binding="s|1|x"),
                    VarDef("reduced", "bool", binding="s|2|x", role="action")])
    r = Rule("r", Cmp("age", ">=", 80), Cmp("reduced", "==", True),
             "recommend", "reduced apixaban dose (2.5 mg twice daily)",
             source_span="Reduce the apixaban dose to 2.5 mg twice daily if age "
                         "80 years or older.")
    assert dsl.dropped_quantities(r) == []

def test_a_dose_in_neither_the_rule_nor_the_target_is_still_flagged():
    """The target exemption must not become a way to launder a dropped
    quantity: it counts only when the number is actually recorded there."""
    reg = Registry([VarDef("age", "int", binding="s|1|x"),
                    VarDef("reduced", "bool", binding="s|2|x", role="action")])
    r = Rule("r", Cmp("age", ">=", 80), Cmp("reduced", "==", True),
             "recommend", "reduced apixaban dose",
             source_span="Reduce the apixaban dose to 2.5 mg twice daily if age "
                         "80 years or older.")
    assert dsl.dropped_quantities(r) == [(2.5, "mg")]


def test_a_target_restating_the_sentence_cannot_launder_a_threshold():
    """The first version of the target exemption accepted ANY quantity found
    there, and a real rule defeated it immediately: the model wrote
    `target="intensive lowering of systolic BP < 140 mmHg in the first 24 h
    after successful mechanical thrombectomy"` -- the sentence restated -- so
    the check passed on the very rule it exists to catch. Only DOSE units are
    discharged by the target."""
    reg = Registry([VarDef("mt", "bool", binding="s|1|x"),
                    VarDef("lowering", "bool", binding="s|2|x", role="action")])
    r = Rule("r", Cmp("mt", "==", True), Cmp("lowering", "==", False),
             "avoid",
             "intensive lowering of systolic BP < 140 mmHg in the first 24 h "
             "after successful mechanical thrombectomy",
             source_span=SPAN)
    assert dsl.dropped_quantities(r) == [(140.0, "mmhg"), (24.0, "h")]

def test_body_weight_in_a_target_is_not_a_dose():
    """kg is a body-weight CONDITION, not a dose, so naming it in the target
    does not discharge it."""
    reg = Registry([VarDef("on", "bool", binding="s|1|x"),
                    VarDef("act", "bool", binding="s|2|x", role="action")])
    r = Rule("r", Cmp("on", "==", True), Cmp("act", "==", True), "recommend",
             "dose reduction in patients 60 kg or less",
             source_span="Reduce the dose if body weight is 60 kg or less.")
    assert dsl.dropped_quantities(r) == [(60.0, "kg")]


def test_a_modal_verb_anywhere_in_a_name_condemns_it():
    """"may be sufficient" produced the hole `has_may_sufficient`, which passed
    the all-tokens-modal test because "sufficient" is not itself a modal. A
    modal VERB is a function word: a patient predicate never contains one."""
    assert dsl.modal_only("has_may_sufficient")
    assert dsl.modal_only("has_can_be_given")
    assert not dsl.modal_only("has_mild_moderate_pain")
    assert not dsl.modal_only("has_appropriate_acupoints")


# ------------------------------------- (N+5) relative change / titration (R30)

def _titration():
    reg = Registry([VarDef("basal_insulin_dose", "real", unit="[iU]",
                           binding="s|1|x", plausible=(0.0, 200.0)),
                    VarDef("on_lrd", "bool", binding="s|2|x", role="action")],
                   [EventDef("lrd_start")])
    rule = Rule("t", Cmp("on_lrd", "==", True),
                dsl.Delta("basal_insulin_dose", "lrd_start", "reduce", 0.5),
                "recommend", "reduced basal insulin dose",
                source_span="Reduce basal insulin by 50% at the start of the diet.")
    return reg, rule

def test_a_relative_change_encodes_what_cmp_cannot():
    """"Reduce basal insulin by 50%" has no number to compare against -- 50% of
    whatever this patient was already on. Three of five statements in
    PMC13074134 were this, and the document scored zero without it."""
    reg, rule = _titration()
    assert [i for i in validate_rule(rule, reg) if i.severity == "error"] == []
    assert check_nondegenerate(rule, reg) == []

def test_a_titration_rule_is_violable():
    """It must be breakable, or it is the vacuity class arriving sideways."""
    reg, rule = _titration()
    env = make_env(reg)
    s = z3.Solver(); s.add(*domain_axioms(reg, env))
    s.add(dsl.violation_condition(rule, env))
    assert s.check() == z3.sat

def test_a_zero_baseline_cannot_make_it_unfalsifiable():
    """With baseline 0 every value is half of it and the rule can never fail.
    The domain axiom that a prior dose is positive is what stops that."""
    reg, rule = _titration()
    env = make_env(reg)
    base = env[dsl.baseline_var("basal_insulin_dose", "lrd_start")]
    s = z3.Solver(); s.add(*domain_axioms(reg, env)); s.add(base == 0)
    assert s.check() == z3.unsat

def test_the_baseline_is_reported_as_a_hole():
    """The prior dose is free unless supplied. Naming it is the point: the
    statement is EXPRESSED and one datum is missing, rather than the whole
    sentence being refused."""
    reg, rule = _titration()
    assert dsl.open_holes(rule, reg) == ("basal_insulin_dose_at_lrd_start",)

def test_a_range_reduction_bounds_both_ends():
    """"reduce by 35%-50%" leaves between 50% and 65% of the baseline."""
    reg, _ = _titration()
    env = make_env(reg)
    d = dsl.Delta("basal_insulin_dose", "lrd_start", "reduce", 0.35, 0.50)
    base = env[dsl.baseline_var("basal_insulin_dose", "lrd_start")]
    cur = env["basal_insulin_dose"]
    for value, ok in ((0.60, True), (0.70, False), (0.45, False)):
        s = z3.Solver(); s.add(base == 100, cur == 100 * value, to_z3(d, env))
        assert (s.check() == z3.sat) is ok, value

def test_a_percentage_written_as_50_is_rejected():
    """lo_pct=50 asks for a 5000% reduction and would otherwise typecheck."""
    reg, _ = _titration()
    bad = Rule("t", Cmp("on_lrd", "==", True),
               dsl.Delta("basal_insulin_dose", "lrd_start", "reduce", 50),
               "recommend", "x", source_span="x")
    assert "TYPE_ERROR" in codes(validate_rule(bad, reg))

def test_an_undeclared_baseline_event_is_rejected():
    reg, _ = _titration()
    bad = Rule("t", Cmp("on_lrd", "==", True),
               dsl.Delta("basal_insulin_dose", "surgery", "reduce", 0.5),
               "recommend", "x", source_span="x")
    assert "UNKNOWN_EVENT" in codes(validate_rule(bad, reg))

def test_a_relative_change_counts_as_the_action():
    """"Reduce the dose" IS the instruction. Requiring role == "action" on the
    dose variable would reject every titration rule, since a dose is also an
    observation of the patient."""
    reg, rule = _titration()
    assert "NO_ACTION_ATOM" not in codes(validate_rule(rule, reg))

def test_a_relative_change_round_trips():
    reg, rule = _titration()
    back = from_dict(to_dict(rule))
    assert isinstance(back.then, dsl.Delta)
    assert (back.then.var, back.then.baseline, back.then.lo_pct) == \
           ("basal_insulin_dose", "lrd_start", 0.5)

def test_the_decoder_can_emit_a_relative_change():
    reg, _ = _titration()
    sch = dsl.decoding_schema(reg, 1)
    assert '"delta"' in json.dumps(sch)
    assert dsl.check_decoding_schema(sch) == []


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:
            failed.append((name, exc))
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)

# ------------------------------------------- the dose companion / Delta chain

def _insulin_registry():
    """What a titration statement builds: a boolean 'is it prescribed' and a
    real 'how much', both carrying the SAME drug code, plus the event."""
    return Registry(
        [VarDef("on_basal_insulin", "bool", None, "Insulin, Long-Acting",
                binding="http://www.whocc.no/atc|D049528|2026", role="action"),
         VarDef("on_basal_insulin_dose", "real", None, "Insulin, Long-Acting dose",
                binding="http://www.whocc.no/atc|D049528|2026", role="action")],
        [EventDef("on_basal_insulin", "basal insulin"),
         EventDef("start_lrd", "at start LRD", None, unbound=True)])

def _wide_insulin_registry():
    """...as the gate widens it for the partial attempt: bound vocabulary plus
    the declared holes. This is what the model actually decodes against."""
    reg = _insulin_registry()
    return Registry([reg[n] for n in reg.names()]
                    + [VarDef("has_start_lrd", "bool", None, "at start LRD",
                              unbound=True)],
                    [reg.event(e) for e in reg.event_names()])

def _insulin_rule():
    return Rule(rule_id="r", when=And([Cmp("on_basal_insulin", "==", True),
                                       Cmp("has_start_lrd", "==", True)]),
                then=dsl.Delta("on_basal_insulin_dose", "start_lrd",
                               "reduce", 0.5),
                action="recommend", target="dose reduction", source_span="x")

def test_one_code_two_types_is_two_variables():
    """`on_basal_insulin` (is it prescribed) and `on_basal_insulin_dose` (how
    much) carry the identical ATC code. Merging them retyped the boolean, so
    the model's `on_basal_insulin == true` became "real but value is bool" and
    the boolean vanished from the shipped registry. A canonicalisation that
    changes a variable's TYPE silently rewrites every comparison against it."""
    reg = _insulin_registry()
    assert dsl.canonical_aliases(reg) == {}
    # and the guard is specific: same code AND same type still merges
    same = Registry([reg["on_basal_insulin"],
                     VarDef("prescribed_basal_insulin", "bool", None, "same drug",
                            binding="http://www.whocc.no/atc|D049528|2026",
                            role="action")], [])
    assert dsl.canonical_aliases(same) != {}

def test_a_hole_is_never_merged_into_a_numeric_variable():
    """The stem rule has the same trap: a hole is declared bool, so matching it
    onto a real-valued bound variable would retype it."""
    reg = Registry([VarDef("insulin_dose", "real", None, "dose",
                           binding="http://www.whocc.no/atc|D049528|2026"),
                    VarDef("has_insulin_dose", "bool", None, "hole", unbound=True)], [])
    assert "has_insulin_dose" not in dsl.canonical_aliases(reg)

def test_the_bundle_registry_declares_a_delta_baseline_event():
    """The event walker handled `Since` only, so the first `Delta` we produced
    shipped with `events: []` and Track B rejected it as UNKNOWN_EVENT."""
    assert dsl.events_in(_insulin_rule()) == ("start_lrd",)
    since = Rule(rule_id="r", when=Since("on_basal_insulin", 30),
                 then=Cmp("on_basal_insulin", "==", True), action="recommend",
                 target="t", source_span="x")
    assert dsl.events_in(since) == ("on_basal_insulin",)

def test_a_delta_baseline_is_the_lowerings_to_declare_not_the_registrys():
    """`rule_to_cql` already emits `<var>_at_<event>` as a named null. Adding it
    to the registry as a bool hole produced BOTH a duplicate CQL identifier
    (the library would not translate) and a number-vs-boolean comparison."""
    reg, rule = _wide_insulin_registry(), _insulin_rule()
    holes = dsl.open_holes(rule, reg)
    assert "on_basal_insulin_dose_at_start_lrd" in holes, "still reported"
    names = [v.name for v in dsl.hole_defs(reg, holes, rule)]
    assert names == [], "the derived baseline is skipped, the rest already declared"
    # and a hole the registry does NOT yet declare is still stubbed
    assert [v.name for v in dsl.hole_defs(reg, ["has_ketoacidosis"], rule)] \
        == ["has_ketoacidosis"]

def test_the_titration_rule_survives_validation_and_lowering():
    """End to end: the shape that produced TYPE_ERROR, UNKNOWN_EVENT and a
    duplicate identifier in one run."""
    import cql
    import re
    reg, rule = _wide_insulin_registry(), _insulin_rule()
    reg = Registry([reg[n] for n in reg.names()]
                   + dsl.hole_defs(reg, dsl.open_holes(rule, reg), rule),
                   [reg.event(e) for e in reg.event_names()])
    assert not [i for i in validate_rule(rule, reg) if i.severity == "error"]
    lib = cql.rule_to_cql(rule, reg, library_name="T")
    assert not [i for i in cql.check_cql(lib.text) if i.severity == "error"]
    defs = re.findall(r'^define "([^"]+)"', lib.text, re.M)
    assert len(defs) == len(set(defs)), f"duplicate define: {defs}"


def test_a_circular_baseline_is_rejected_not_merely_flagged():
    """"Reduce the basal insulin dose to 50% of the dose at the time the
    patient was on basal insulin" names no point in time, so any dose at all
    satisfies it. It typechecks, is non-vacuous, compiles, and verifies. It was
    produced twelve times across a single real document, because the only
    events on offer were the drug bindings themselves. A warning is not enough
    for a rule that is wrong."""
    reg = _wide_insulin_registry()
    circular = Rule(rule_id="r", when=Cmp("has_start_lrd", "==", True),
                    then=dsl.Delta("on_basal_insulin_dose", "on_basal_insulin",
                                   "reduce", 0.5),
                    action="recommend", target="t", source_span="x")
    errs = [i for i in validate_rule(circular, reg) if i.severity == "error"]
    assert [i.code for i in errs] == ["CIRCULAR_BASELINE"]

def test_a_delta_may_anchor_to_an_unbound_event_but_a_window_may_not():
    """A Delta baseline lowers to a named null, so an anchor with no code is
    honest there. `Since` lowers to a FHIR retrieve, which against an uncoded
    event matches nothing and reads as "never happened"."""
    reg = _wide_insulin_registry()
    assert reg.event("start_lrd").unbound
    delta = dsl.Delta("on_basal_insulin_dose", "start_lrd", "reduce", 0.5)
    assert not [i for i in validate(delta, reg, "then") if i.severity == "error"]
    window = Since("start_lrd", 14)
    assert "UNBOUND_EVENT" in [i.code for i in validate(window, reg, "when")]

def test_an_unbound_event_survives_the_bundle():
    """The flag has to reach Track B, or the loader re-reads the anchor as a
    coded event and the distinction is lost at the interface."""
    reg = _wide_insulin_registry()
    back = dsl.registry_from_dict(dsl.registry_to_dict(reg))
    assert back.event("start_lrd").unbound is True
    assert back.event("on_basal_insulin").unbound is False

def test_a_weak_recommendation_is_not_rendered_as_must():
    """"a weak recommendation against finerenone" read as `THEN must: NOT
    finerenone`. Overstating a guideline to a governance committee is the
    specific harm this project exists to avoid, and the strength was sitting in
    the rule unused."""
    base = dict(when=Cmp("has_diabetes", "==", True),
                then=Cmp("on_ace_inhibitor", "==", True),
                action="recommend", target="ACE inhibitor", source_span="x")
    weak = Rule(rule_id="r", strength="weak recommendation against", **base)
    strong = Rule(rule_id="r", strength="strong recommendation in favour", **base)
    plain = Rule(rule_id="r", **base)
    permissive = Rule(rule_id="r", deontic="permission",
                      strength="conditional (permissive wording)", **base)
    assert dsl.deontic_force(weak) == "should (weak recommendation)"
    assert dsl.deontic_force(strong) == "must (strong recommendation)"
    assert dsl.deontic_force(plain) == "must"
    assert dsl.deontic_force(permissive) == "may", "permission outranks strength"

def test_english_reads_a_boolean_as_a_presence_claim():
    """"on_warfarin equals true" is not English and nobody adjudicates it."""
    reg = Registry([VarDef("on_warfarin", "bool", None, "Warfarin", role="action"),
                    VarDef("egfr", "real", "mL/min", "eGFR")], [])
    assert dsl.to_english(Cmp("on_warfarin", "==", True), reg) == "Warfarin"
    assert dsl.to_english(Cmp("on_warfarin", "==", False), reg) == "NOT Warfarin"
    assert dsl.to_english(Cmp("egfr", "<", 30), reg) == "eGFR is below 30 mL/min"
