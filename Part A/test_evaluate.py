"""Regression tests for the M2 clause scorer. Offline."""
import evaluate as ev
from evaluate import Clause, extract_clauses, score_clauses, score_cql, subject_similarity

EXPERT = """
// The threshold below is discussed in section 4 and was set at 200 after review.
define "Numerator":
  "Has Systolic Blood Pressure Less Than 140" and "Has Diastolic Blood Pressure Less Than 90"
define "Has Systolic Blood Pressure Less Than 140":
  "Lowest Systolic Reading on Most Recent Blood Pressure Day" < 140 'mm[Hg]'
define "Has Diastolic Blood Pressure Less Than 90":
  "Lowest Diastolic Reading on Most Recent Blood Pressure Day" < 90 'mm[Hg]'
define "Initial Population":
  AgeInYearsAt(end of "Measurement Period") >= 18
  and exists ([Condition: "Essential Hypertension"])
"""

def test_thresholds_comparators_and_units_are_extracted():
    cs = extract_clauses(EXPERT)
    comps = {(c.value, c.op, c.unit) for c in cs if c.kind == "comparison"}
    assert ("140", "<", "mmhg") in comps
    assert ("90", "<", "mmhg") in comps
    assert any(c.kind == "age" and c.op == ">=" and c.value == "18" for c in cs)
    assert any(c.kind == "retrieve" and c.valueset == "essential hypertension" for c in cs)

def test_comment_numbers_are_not_counted_as_clauses():
    """An expert artifact carries long rationale comments full of numbers.
    Counting those would inflate the reference side and depress recall."""
    assert not any(c.value == "200" for c in extract_clauses(EXPERT))

def test_units_are_canonicalised_across_spellings():
    a = extract_clauses("define \"X\": \"BP\" < 140 'mm[Hg]'")[0]
    b = extract_clauses("define \"X\": \"BP\" < 140 'mmHg'")[0]
    assert a.unit == b.unit == "mmhg"

def test_a_perfect_reproduction_scores_one():
    sc = score_cql(EXPERT, EXPERT)
    assert sc.f1 == 1.0 and sc.precision == 1.0 and sc.recall == 1.0
    for f in ev.FIELDS:
        assert sc.field_accuracy(f) == 1.0

def test_a_wrong_threshold_is_caught_even_though_it_would_compile():
    """The whole point of M2: an artifact that compiles perfectly and gets the
    number wrong must not score as correct."""
    wrong = EXPERT.replace("< 140 'mm[Hg]'", "< 130 'mm[Hg]'")
    sc = score_cql(EXPERT, wrong)
    assert sc.field_accuracy("value") < 1.0
    got = [g for r, g, _ in sc.pairs if r.value == "140"]
    assert got and got[0].value == "130"

def test_a_flipped_comparator_is_caught():
    flipped = EXPERT.replace("< 140 'mm[Hg]'", "> 140 'mm[Hg]'")
    sc = score_cql(EXPERT, flipped)
    assert sc.field_accuracy("op") < 1.0

def test_a_dropped_unit_is_caught():
    """mg/g vs mg/mmol is 8.84x; a bare number loses the distinction."""
    bare = EXPERT.replace("< 140 'mm[Hg]'", "< 140")
    sc = score_cql(EXPERT, bare)
    assert sc.field_accuracy("unit") < 1.0

def test_a_missing_clause_costs_recall_not_precision():
    partial = EXPERT.replace('define "Has Diastolic Blood Pressure Less Than 90":\n'
                             '  "Lowest Diastolic Reading on Most Recent Blood Pressure Day"'
                             " < 90 'mm[Hg]'", "")
    sc = score_cql(EXPERT, partial)
    assert sc.recall < 1.0 and sc.precision == 1.0

def test_an_invented_clause_costs_precision_not_recall():
    extra = EXPERT + "\ndefine \"Extra\":\n  \"Serum Potassium\" > 5 'mmol/L'\n"
    sc = score_cql(EXPERT, extra)
    assert sc.precision < 1.0 and sc.recall == 1.0

def test_subject_wording_may_differ():
    """The reference says 'Lowest Systolic Reading on Most Recent Blood Pressure
    Day'; a generated artifact says 'systolic blood pressure'. Same clause."""
    assert subject_similarity("Lowest Systolic Reading on Most Recent Blood Pressure Day",
                              "systolic blood pressure") > 0
    ours = 'define "X":\n  "systolic blood pressure" < 140 \'mm[Hg]\'\n'
    sc = score_cql('define "Y":\n  "Lowest Systolic Reading on Most Recent Blood '
                   'Pressure Day" < 140 \'mm[Hg]\'\n', ours)
    assert sc.matched == 1 and sc.field_accuracy("value") == 1.0

def test_pairs_are_inspectable_so_a_disagreement_can_be_adjudicated():
    sc = score_cql(EXPERT, EXPERT.replace("140", "130"))
    assert sc.pairs and all(len(p) == 3 for p in sc.pairs)
    assert any(r.value != g.value for r, g, _ in sc.pairs)

def test_clauses_of_different_kinds_never_match():
    r = Clause("comparison", "bp", "<", "140", "mmhg")
    g = Clause("retrieve", "bp", valueset="bp")
    assert ev.match(r, g) == 0.0
    assert score_clauses([r], [g]).matched == 0

def test_the_real_expert_artifact_parses():
    import os
    p = ("/private/tmp/claude-501/-Users-kaavya-Downloads-Guideline-autoformalization/"
         "148ab5e8-72fb-4631-8ccb-84a29e88a3a2/scratchpad/ecqm/"
         "CMS165FHIRControllingHighBloodPressure-0.5.000.cql")
    if not os.path.exists(p):
        return                                  # corpus not fetched; skip
    cs = extract_clauses(open(p).read())
    assert any(c.value == "140" and c.unit == "mmhg" for c in cs)
    assert any(c.value == "90" and c.unit == "mmhg" for c in cs)


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
