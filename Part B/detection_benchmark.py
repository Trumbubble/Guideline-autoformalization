"""Detection benchmark: does the three-valued checker actually catch violations?

`synthesize_boundary_tests.py` builds concrete, Z3-solved patient bundles at
each rule's threshold boundaries, each one built specifically to be either a
compliant or a violating instantiation. This script re-derives the verdict
for every one of those bundles independently -- fully pinning every
variable the rule references and re-running the same MUST-violate /
MAY-violate / CERTIFIED-compliant check `benchmark.py` uses -- and confirms
MUST-violate fires on every bundle that was built to violate, and only
those.

`benchmark.py` already shows the checker doesn't false-positive on fully
random synthetic plans (`naive_false_violation_rate`). This is the other
half: true-positive detection on bundles that are ground-truth violations
by construction, not hand-picked test numbers.

    python3 detection_benchmark.py
"""

import csv
from dataclasses import dataclass
from pathlib import Path

from real_rules import DOMAIN_AXIOMS, REAL_RULES
from synthesize_boundary_tests import classify, synthesize_cases

OUTPUT_CSV = Path(__file__).parent / "detection_benchmark_results.csv"

RULES_BY_ID = {rule.rule_id: rule for rule in REAL_RULES}


@dataclass(frozen=True, eq=False)
class DetectionResult:
    rule_id: str
    boundary_description: str
    side: str
    source: str  # "bundle" | "counter_bundle"
    expected_label: str  # "compliant" | "violating"
    actual_verdict: str
    matched: bool


def pin_domain(rule, bundle: dict) -> list:
    """Fully pin every variable the rule references to one concrete bundle."""
    return list(DOMAIN_AXIOMS) + [
        var_ref == bundle[name] for name, var_ref in rule.var_refs.items()
    ]


def run_detection_benchmark() -> list:
    results = []
    for case in synthesize_cases():
        rule = RULES_BY_ID[case.rule_id]
        instances = []
        if case.verdict == "CERTIFIED-compliant":
            instances.append(("bundle", case.bundle, "compliant"))
        elif case.verdict == "MUST-violate":
            instances.append(("bundle", case.bundle, "violating"))
        else:  # MAY-violate: the solver produced one bundle on each side
            instances.append(("bundle", case.bundle, "compliant"))
            instances.append(("counter_bundle", case.counter_bundle, "violating"))

        for source, bundle, expected_label in instances:
            domain = pin_domain(rule, bundle)
            actual_verdict = classify(domain, rule.predicate)
            expected_violation = expected_label == "violating"
            detected_violation = actual_verdict == "MUST-violate"
            results.append(
                DetectionResult(
                    rule_id=case.rule_id,
                    boundary_description=case.boundary_description,
                    side=case.side,
                    source=source,
                    expected_label=expected_label,
                    actual_verdict=actual_verdict,
                    matched=detected_violation == expected_violation,
                )
            )
    return results


def report(results: list) -> None:
    fieldnames = [
        "rule_id",
        "boundary_description",
        "side",
        "source",
        "expected_label",
        "actual_verdict",
        "matched",
    ]
    with OUTPUT_CSV.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for result in results:
            writer.writerow(
                {
                    "rule_id": result.rule_id,
                    "boundary_description": result.boundary_description,
                    "side": result.side,
                    "source": result.source,
                    "expected_label": result.expected_label,
                    "actual_verdict": result.actual_verdict,
                    "matched": int(result.matched),
                }
            )

    violating = [r for r in results if r.expected_label == "violating"]
    compliant = [r for r in results if r.expected_label == "compliant"]
    true_positives = sum(r.actual_verdict == "MUST-violate" for r in violating)
    false_negatives = len(violating) - true_positives
    true_negatives = sum(
        r.actual_verdict == "CERTIFIED-compliant" for r in compliant
    )
    false_positives = len(compliant) - true_negatives
    unexpected_may = sum(r.actual_verdict == "MAY-violate" for r in results)
    mismatches = [r for r in results if not r.matched]

    print("metric,value")
    print(f"cases_checked,{len(results)}")
    print(f"ground_truth_violating,{len(violating)}")
    print(f"ground_truth_compliant,{len(compliant)}")
    print(f"true_positive_violations_detected,{true_positives}")
    print(f"false_negative_violations_missed,{false_negatives}")
    print(f"true_negative_compliant_cleared,{true_negatives}")
    print(f"false_positive_compliant_flagged,{false_positives}")
    print(f"unexpected_MAY_on_fully_pinned_bundle,{unexpected_may}")
    print(f"detection_recall,{true_positives / len(violating):.4f}")
    print(f"false_positive_rate,{false_positives / len(compliant):.4f}")
    print(f"csv,{OUTPUT_CSV}")

    if mismatches:
        print("mismatches:")
        for result in mismatches:
            print(
                f"  {result.rule_id} [{result.boundary_description} / "
                f"{result.side} / {result.source}] "
                f"expected={result.expected_label} actual={result.actual_verdict}"
            )


if __name__ == "__main__":
    report(run_detection_benchmark())
