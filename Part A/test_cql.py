"""Regression tests for the CQL lowering and the verify-repair loop.

Offline: the lowering is deterministic and the compiler is injected, so none of
this needs a JVM, a key, or a network.
"""
import dsl
import cql
from cql import (check_cql, compile_cql, event_term, expr_to_cql, parse_binding,
                 rule_to_cql, synthesize, term_name)

REG = dsl.Registry([
    dsl.VarDef("on_ace_inhibitor", "bool", None, "ACE inhibitors", role="action",
               binding="http://cts.nlm.nih.gov/fhir/ValueSet|2.16.840.1.113762.1.4.1078.9|20241228"),
    dsl.VarDef("has_diabetes", "bool", None, "Diabetes",
               binding="http://cts.nlm.nih.gov/fhir/ValueSet|2.16.840.1.113883.3.464.1003.103.12.1001|20250115"),
    dsl.VarDef("acr_mg_g", "real", "mg/g", "urine ACR",
               binding="http://loinc.org|9318-7|2.78", plausible=(0, 30000)),
    dsl.VarDef("has_high_risk", "bool", None, "at high risk", unbound=True),
], events=[dsl.EventDef("potassium_check", "serum potassium measured", "LOINC:2823-3")])

RULE = dsl.Rule("kdigo-3.1",
                dsl.And([dsl.Cmp("has_diabetes", "==", True),
                         dsl.Cmp("acr_mg_g", ">=", 30)]),
                dsl.Cmp("on_ace_inhibitor", "==", True), "recommend", "ACEi",
                strength="1B", source_span="/article/body/sec[3]/boxed-text")

def codes(iss): return sorted(i.code for i in iss)

# ------------------------------------------------------------ the library

def test_a_rule_lowers_to_a_structurally_clean_library():
    lib = rule_to_cql(RULE, REG)
    assert check_cql(lib.text) == [], check_cql(lib.text)
    assert lib.text.startswith("library ")
    assert "context Patient" in lib.text
    for d in ('"Applicable"', '"Satisfied"', '"Compliant"', '"Violated"'):
        assert f"define {d}:" in lib.text

def test_a_library_name_is_always_a_valid_identifier():
    """Statement ids carry their document -- `PMC12355350:0129` -- and a colon
    is illegal in a CQL identifier. An unsanitised name produced
    `library DemoPMC12355350:0129`, which the official translator rejected with
    "Syntax error at :", losing the statement."""
    import re as _re
    for raw in ["PMC12355350:0129", "Demo PMC1235:0129", "0129-abc", "!!!", "ash:0042"]:
        lib = rule_to_cql(RULE, REG, library_name=raw)
        first = lib.text.splitlines()[0]
        name = first.split()[1]
        assert _re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", name), (raw, first)
        assert check_cql(lib.text) == [], (raw, check_cql(lib.text))

def test_terminology_names_differ_from_define_names():
    """`[Condition: X]` needs X to be a valueset or code. Reusing the define's
    name produces a self-referential library that looks plausible and is not
    valid CQL."""
    assert term_name("has_diabetes", REG) != cql.cql_name("has_diabetes", REG)
    lib = rule_to_cql(RULE, REG)
    assert 'valueset "Diabetes codes":' in lib.text
    assert 'define "Diabetes":\n  exists ([Condition: "Diabetes codes"])' in lib.text

def test_bindings_are_declared_with_their_versions():
    """The artifact's terminology contract: a reviewer resolves these against
    VSAC themselves, so an unversioned OID is not enough."""
    lib = rule_to_cql(RULE, REG)
    assert "'urn:oid:2.16.840.1.113762.1.4.1078.9' version '20241228'" in lib.text
    assert "'urn:oid:2.16.840.1.113883.3.464.1003.103.12.1001' version '20250115'" in lib.text
    assert len(lib.valuesets) == 2

def test_parse_binding_handles_both_shapes():
    assert parse_binding("http://cts.nlm.nih.gov/fhir/ValueSet|2.16.840.1.113762.1.4.1078.9|20241228") \
        == ("valueset", "2.16.840.1.113762.1.4.1078.9", "20241228")
    assert parse_binding("http://loinc.org|9318-7|2.78") == ("code", "9318-7", "2.78")
    assert parse_binding("LOINC:9318-7") == ("code", "9318-7", None)
    assert parse_binding(None) == ("none", "", None)

def test_units_survive_into_the_comparison():
    """mg/g vs mg/mmol is a factor of ~8.84; a bare number in CQL loses it."""
    lib = rule_to_cql(RULE, REG)
    assert '"urine ACR" >= 30 \'mg/g\'' in lib.text

# ------------------------------------------------------------ holes

def test_a_hole_becomes_null_not_false():
    """CQL is natively three-valued: `true and null` is null. So an unbound
    predicate propagates the uncertainty. PathWISE stubs `false`, which
    silently narrows the rule; `true` would over-fire."""
    rule = dsl.Rule("r", dsl.Or([dsl.Cmp("has_diabetes", "==", True),
                                 dsl.Cmp("has_high_risk", "==", True)]),
                    dsl.Cmp("on_ace_inhibitor", "==", True), "recommend", "x",
                    source_span="s")
    lib = rule_to_cql(rule, REG)
    assert 'define "at high risk":\n  null as Boolean' in lib.text
    assert "false" not in lib.text.split('define "at high risk"')[1][:120]
    assert lib.holes == ("has_high_risk",)
    assert "PARTIAL: 1 unbound predicate" in lib.text

def test_a_hole_gets_no_terminology_declaration():
    lib = rule_to_cql(RULE, REG)
    assert "at high risk codes" not in lib.text

# ------------------------------------------------------------ time

def test_since_lowers_to_a_real_temporal_retrieve_not_a_flattened_counter():
    """The two lowerings differ on purpose: to_z3 flattens time because Z3 has
    no timeline; CQL keeps it because the engine evaluates over the record.
    The retrieve plus a temporal `where` IS the bounded existential, so the DSL
    needs no time-indexed variables for the CQL to be faithful."""
    e = dsl.Since("potassium_check", 14)
    out = expr_to_cql(e, REG)
    assert "during Interval[Today() - 14 days, Today()]" in out
    assert "days_since" not in out                     # not the Z3 encoding
    # and Z3 still gets the flattened form
    env = dsl.make_env(REG)
    assert "days_since_potassium_check" in str(dsl.to_z3(e, env))

def test_an_event_used_in_a_retrieve_is_declared():
    """A Since node lowers to `[Observation: X]`, and X must be declared --
    events were being referenced without any declaration."""
    rule = dsl.Rule("r", dsl.Cmp("has_diabetes", "==", True),
                    dsl.And([dsl.Cmp("on_ace_inhibitor", "==", True),
                             dsl.Since("potassium_check", 14)]),
                    "monitor", "potassium", source_span="s")
    lib = rule_to_cql(rule, REG)
    assert check_cql(lib.text) == [], check_cql(lib.text)
    assert event_term("potassium_check", REG).strip('"') in lib.text

def test_exclusive_since_shortens_the_interval():
    assert "Today() - 1 day]" in expr_to_cql(dsl.Since("potassium_check", 14, False), REG)

# ------------------------------------------------------------ checking

def test_check_catches_the_obvious_breakage():
    assert "MISSING_LIBRARY_HEADER" in codes(check_cql("define \"X\": true"))
    assert "MISSING_CONTEXT" in codes(check_cql("library L version '1'\ndefine \"X\": true"))
    good = rule_to_cql(RULE, REG).text
    assert "UNBALANCED_DELIMITER" in codes(check_cql(good.replace("])", "]", 1)))
    assert "UNDECLARED_IDENTIFIER" in codes(
        check_cql(good + '\ndefine "Extra":\n  "Never Declared"\n'))

def test_a_define_whose_body_is_on_the_next_line_is_not_empty():
    """Normal CQL formatting; an over-eager check flagged every library."""
    assert "EMPTY_DEFINE" not in codes(check_cql(rule_to_cql(RULE, REG).text))

def test_an_unavailable_compiler_is_a_warning_not_a_pass():
    """Silence must not read as success -- a compiler that failed to run has
    not validated anything."""
    def broken(text): raise RuntimeError("no JVM")
    iss = compile_cql(rule_to_cql(RULE, REG).text, broken)
    assert "COMPILER_UNAVAILABLE" in codes(iss)
    assert [i for i in iss if i.code == "COMPILER_UNAVAILABLE"][0].severity == "warn"

def test_an_injected_compiler_reports_errors():
    lib = rule_to_cql(RULE, REG)
    iss = compile_cql(lib.text, lambda t: ["Could not resolve identifier Foo"])
    assert "CQL_COMPILE_ERROR" in codes(iss)

# ------------------------------------------------------------ repair loop

def _decoded(rule=None, refusal=None):
    return {"outcome": "rule" if rule else "refusal",
            "rule": dsl.to_dict(rule) if rule else None,
            "refusal": dsl.to_dict(refusal) if refusal else None}

def test_a_valid_first_attempt_needs_no_repair():
    f = lambda text, reg: _decoded(rule=RULE)
    out = synthesize("...", REG, f, statement_id="s1", source_span="s")
    assert out.ok and out.attempts == 1 and out.repairs == ()
    assert out.library is not None and check_cql(out.library.text) == []

def test_a_compiler_error_is_fed_back_and_the_retry_succeeds():
    seen = []
    calls = {"n": 0}
    def f(text, reg):
        seen.append(text)
        calls["n"] += 1
        return _decoded(rule=RULE)
    fails_once = {"n": 0}
    def compiler(t):
        fails_once["n"] += 1
        return ["Could not resolve identifier Foo"] if fails_once["n"] == 1 else []
    out = synthesize("Encode this.", REG, f, statement_id="s1",
                     compiler=compiler, source_span="s")
    assert out.ok and out.attempts == 2
    assert "Could not resolve identifier Foo" in seen[1]      # the error was fed back
    assert "previous attempt was rejected" in seen[1]

def test_the_loop_is_bounded_and_falls_through_to_a_refusal():
    """Unbounded repair converges on whatever happens to compile -- exactly the
    unreliable self-correction arXiv:2502.12065 documents."""
    f = lambda text, reg: _decoded(rule=RULE)
    out = synthesize("...", REG, f, statement_id="s1",
                     compiler=lambda t: ["always broken"], max_repairs=2,
                     source_span="s")
    assert not out.ok and out.attempts == 3
    assert out.refusal.reason_code == "beyond_dsl"
    assert "CQL_COMPILE_ERROR" in out.refusal.reason_text

def test_a_model_refusal_short_circuits_the_loop():
    ref = dsl.Refusal("s1", "vague_predicate", "no threshold stated")
    f = lambda text, reg: _decoded(refusal=ref)
    out = synthesize("...", REG, f, statement_id="s1")
    assert not out.ok and out.attempts == 1
    assert out.refusal.reason_code == "vague_predicate"

def test_a_rule_failing_dsl_validation_is_repaired_before_any_cql():
    """Validation errors are fed back too, not just compiler ones."""
    bad = dsl.Rule("r", dsl.Cmp("has_diabetes", "==", True),
                   dsl.Cmp("acr_mg_g", ">", 30),        # obligation names no action
                   "recommend", "x", source_span="s")
    n = {"i": 0}
    def f(text, reg):
        n["i"] += 1
        return _decoded(rule=bad if n["i"] == 1 else RULE)
    out = synthesize("...", REG, f, statement_id="s1", source_span="s")
    assert out.ok and out.attempts == 2

def test_a_decoder_exception_becomes_a_refusal_not_a_crash():
    def boom(text, reg): raise RuntimeError("429")
    out = synthesize("...", REG, boom, statement_id="s1")
    assert not out.ok and out.refusal.reason_code == "beyond_dsl"
    assert "429" in out.refusal.reason_text


def test_a_permission_lowers_to_a_library_that_cannot_report_a_violation():
    """CQL has no deontic content -- "Violated" means the same thing whether
    the source required the action or offered it. The lowering therefore cannot
    carry the distinction in its LOGIC, so it carries it in the definitions:
    `Deontic` names the operator and `Violated` is false outright."""
    reg = dsl.Registry([dsl.VarDef("has_ap", "bool", binding="http://snomed.info/sct|1|x"),
                        dsl.VarDef("epidural", "bool", binding="http://snomed.info/sct|2|x",
                                   role="action")])
    r = dsl.Rule("r", dsl.Cmp("has_ap", "==", True), dsl.Cmp("epidural", "==", True),
                 "recommend", "epidural", deontic="permission", source_span="x")
    text = cql.rule_to_cql(r, reg).text
    assert "'permission'" in text
    assert 'define "Violated":\n  false' in text
    assert 'define "OptionNotTaken"' in text

def test_an_obligation_still_lowers_to_a_real_violation_define():
    reg = dsl.Registry([dsl.VarDef("has_ap", "bool", binding="http://snomed.info/sct|1|x"),
                        dsl.VarDef("epidural", "bool", binding="http://snomed.info/sct|2|x",
                                   role="action")])
    r = dsl.Rule("r", dsl.Cmp("has_ap", "==", True), dsl.Cmp("epidural", "==", True),
                 "recommend", "epidural", deontic="obligation", source_span="x")
    text = cql.rule_to_cql(r, reg).text
    assert "'obligation'" in text
    assert 'define "Violated":\n  "Applicable" and not "Satisfied"' in text


def test_a_relative_change_lowers_to_cql_with_a_named_baseline():
    """No portable FHIR retrieve expresses "the dose before the diet started",
    so the baseline is emitted as a NAMED null for a site to bind -- the same
    call Arden Syntax made with its curly braces, and the same discipline as an
    unbound predicate."""
    reg = dsl.Registry([dsl.VarDef("basal_insulin_dose", "real", unit="[iU]",
                                   description="Basal insulin dose",
                                   binding="http://snomed.info/sct|1|x",
                                   plausible=(0.0, 200.0)),
                        dsl.VarDef("on_lrd", "bool", description="On liver reduction diet",
                                   binding="http://snomed.info/sct|2|x", role="action")],
                       [dsl.EventDef("lrd_start", "start of liver reduction diet")])
    r = dsl.Rule("t", dsl.Cmp("on_lrd", "==", True),
                 dsl.Delta("basal_insulin_dose", "lrd_start", "reduce", 0.35, 0.50),
                 "recommend", "reduced basal insulin dose",
                 source_span="Reduce basal insulin dose by 35%-50% of pre-LRD dose.")
    text = cql.rule_to_cql(r, reg).text
    assert 'define "basal_insulin_dose_at_lrd_start":\n  null as Decimal' in text
    assert "BASELINE:" in text
    assert '* 0.65' in text and '* 0.5' in text     # 35% and 50% reductions
    assert check_cql(text) == []


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
