"""Regression tests for the DSL. Each test names the failure it prevents.

Run: python3 test_dsl.py    (no pytest dependency; asserts + a summary line)
"""
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
