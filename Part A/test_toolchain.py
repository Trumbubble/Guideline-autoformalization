"""Tests for the real CQL toolchain (P6).

SKIPPED, LOUDLY, when the toolchain is not built -- these shell out to a JVM
and Node, so they cannot be part of the offline suite. Build with
`bash "Part B/engine/setup.sh"`.
"""
import json
import os
import tempfile

import cql
import dsl
import toolchain

ENGINE = toolchain.ENGINE
SKIP = not toolchain.available()

REG = dsl.Registry([
    dsl.VarDef("on_ace_inhibitor", "bool", None, "ACE inhibitors", role="action",
               binding="http://cts.nlm.nih.gov/fhir/ValueSet|2.16.840.1.113762.1.4.1078.9|20241228"),
    dsl.VarDef("has_diabetes", "bool", None, "Diabetes",
               binding="http://cts.nlm.nih.gov/fhir/ValueSet|2.16.840.1.113883.3.464.1003.103.12.1001|20250115"),
    dsl.VarDef("acr_mg_g", "real", "mg/g", "urine ACR",
               binding="http://loinc.org|9318-7|2.78"),
])
RULE = dsl.Rule("kdigo-3.1",
                dsl.And([dsl.Cmp("has_diabetes", "==", True),
                         dsl.Cmp("acr_mg_g", ">=", 30)]),
                dsl.Cmp("on_ace_inhibitor", "==", True), "recommend", "ACEi",
                strength="1B", source_span="/article/body/sec[3]/boxed-text")

def _lib(): return cql.rule_to_cql(RULE, REG).text

def test_a_generated_library_compiles_against_the_official_translator():
    """The claim P6 exists to test. `check_cql` only counts brackets."""
    if SKIP: return
    assert toolchain.cql_to_elm(_lib()) == []

def test_the_translator_catches_a_type_error_check_cql_cannot_see():
    """Comparing a Boolean to a number. Structurally perfect, semantically
    nonsense -- and exactly the class of error the paper argues compilation
    alone does not catch, which is only true of a WEAK compiler."""
    if SKIP: return
    sneaky = _lib().replace('"urine ACR" >= 30', '"Diabetes" >= 30')
    assert [i for i in cql.check_cql(sneaky) if i.severity == "error"] == []
    errs = toolchain.cql_to_elm(sneaky)
    assert errs and any("GreaterOrEqual" in e or "operator" in e for e in errs)

def test_an_unresolvable_type_is_reported_with_a_line_number():
    if SKIP: return
    bad = _lib().replace("[Condition:", "[NoSuchResource:")
    errs = toolchain.cql_to_elm(bad)
    assert errs and any("NoSuchResource" in e for e in errs)
    assert any(e.startswith("line ") for e in errs)      # repairable feedback

def test_a_missing_toolchain_raises_rather_than_reporting_success():
    """Silence must never read as a clean compile -- that mistake already cost
    a whole guideline run once."""
    real = toolchain.ENGINE
    try:
        toolchain.ENGINE = "/nonexistent"
        try:
            toolchain.cql_to_elm("library X version '1'")
            assert False, "should have raised"
        except toolchain.ToolchainMissing:
            pass
    finally:
        toolchain.ENGINE = real

def test_synthesize_runs_with_the_real_compiler_in_the_loop():
    if SKIP: return
    calls = {"n": 0}
    def formalize(text, reg):
        calls["n"] += 1
        return {"outcome": "rule", "refusal": None, "rule": dsl.to_dict(RULE)}
    out = cql.synthesize("...", REG, formalize, statement_id="kdigo-3.1",
                         compiler=toolchain.cql_to_elm, source_span="s")
    assert out.ok and calls["n"] == 1
    assert toolchain.cql_to_elm(out.library.text) == []

def test_compiled_elm_executes_against_a_fhir_bundle():
    """The whole chain: prose-derived rule -> CQL -> ELM -> verdict."""
    if SKIP or not os.path.exists(os.path.join(ENGINE, "valuesets.json")):
        return
    with tempfile.TemporaryDirectory() as d:
        elm = os.path.join(d, "lib.json")
        assert toolchain.translate_to_file(_lib(), elm) == []
        for bundle, expect in [("patient-violating.json", True),
                               ("patient-compliant.json", False)]:
            p = os.path.join(ENGINE, bundle)
            if not os.path.exists(p):
                continue
            res = toolchain.execute(elm, p)["results"]
            vals = next(iter(res.values()))
            assert vals["Applicable"] is True
            assert vals["Violated"] is expect, (bundle, vals)
            assert vals["Compliant"] is (not expect)

def test_a_quantity_is_rendered_with_its_unit():
    """It printed '[object Object] [object Object]' -- the unit is the thing
    that distinguishes mg/g from mg/mmol, so it has to survive."""
    if SKIP or not os.path.exists(os.path.join(ENGINE, "patient-violating.json")):
        return
    with tempfile.TemporaryDirectory() as d:
        elm = os.path.join(d, "lib.json")
        toolchain.translate_to_file(_lib(), elm)
        res = toolchain.execute(elm, os.path.join(ENGINE, "patient-violating.json"))
        vals = next(iter(res["results"].values()))
        assert vals["urine ACR"] == "45 mg/g", vals["urine ACR"]


if __name__ == "__main__":
    if SKIP:
        print("toolchain NOT built -- compile/execute tests skipped.")
        print(f"  build with: bash {os.path.join(ENGINE, 'setup.sh')}")
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:
            failed.append(name)
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed"
          + (" (some skipped)" if SKIP else ""))
    raise SystemExit(1 if failed else 0)
