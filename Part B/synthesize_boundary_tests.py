"""Coverage-directed test synthesis over the Track B fixtures.

For every numeric threshold declared on a `real_rules.SpecClause`, this
solves -- with Z3, not by hand -- for a concrete patient bundle sitting on
each side of the boundary (e.g. eGFR=29 vs 30, a 14-day vs 15-day gap), then
runs that bundle through the same three-valued MUST/MAY/CERTIFIED check
`benchmark.py` uses on a full plan. The boundary arithmetic (threshold +-
step) is the only hand-picked part; every other free variable in the bundle
-- and the resulting verdict -- comes out of the solver.

    python3 synthesize_boundary_tests.py
"""

import csv
from dataclasses import dataclass, field
from pathlib import Path

from z3 import BoolRef, Not, Solver, is_bool, is_int, sat

from real_rules import DOMAIN_AXIOMS, REAL_RULES

OUTPUT_CSV = Path(__file__).parent / "synthesized_cases.csv"


@dataclass(frozen=True, eq=False)
class GeneratedCase:
    rule_id: str
    boundary_description: str
    side: str  # "compliant_side" | "violating_side"
    test_value: float
    verdict: str  # MUST-violate | MAY-violate | CERTIFIED-compliant
    bundle: dict  # a concrete, Z3-solved patient bundle at this point
    counter_bundle: dict = None  # MAY-violate only: a bundle on the other verdict


def boundary_points(operator: str, threshold: float, step: float) -> tuple:
    """Return (compliant_value, violating_value) for one side of a threshold.

    Both values are solved-for test points, one on each side of the exact
    spot where the predicate's truth value flips -- not arbitrary numbers.
    """
    if operator == "<=":
        return threshold, threshold + step
    if operator == "<":
        return threshold - step, threshold
    if operator == ">=":
        return threshold, threshold - step
    if operator == ">":
        return threshold + step, threshold
    raise ValueError(f"unsupported operator {operator!r}")


def _value_of(ref, model) -> object:
    value = model.eval(ref, model_completion=True)
    if is_bool(ref):
        return bool(value)
    if is_int(ref):
        return value.as_long()
    return float(value.as_fraction())


def solve_bundle(domain: list, formula: BoolRef, var_refs: dict) -> dict:
    """Ask Z3 for a model of domain+formula and read off every fixture variable."""
    solver = Solver()
    solver.add(*domain, formula)
    if solver.check() != sat:
        return None
    model = solver.model()
    return {name: _value_of(ref, model) for name, ref in var_refs.items()}


def classify(domain: list, predicate: BoolRef) -> str:
    """The same MUST-violate / MAY-violate / CERTIFIED-compliant check as benchmark.py."""
    compliant = Solver()
    compliant.add(*domain, predicate)
    violating = Solver()
    violating.add(*domain, Not(predicate))
    if compliant.check() != sat:
        return "MUST-violate"
    if violating.check() != sat:
        return "CERTIFIED-compliant"
    return "MAY-violate"


def synthesize_cases() -> list:
    cases = []
    for rule in REAL_RULES:
        for boundary in rule.boundaries:
            compliant_value, violating_value = boundary_points(
                boundary.operator, boundary.threshold, boundary.step
            )
            for side, value in (
                ("compliant_side", compliant_value),
                ("violating_side", violating_value),
            ):
                domain = (
                    list(DOMAIN_AXIOMS)
                    + list(boundary.context)
                    + [boundary.variable == value]
                )
                verdict = classify(domain, rule.predicate)

                if verdict == "CERTIFIED-compliant":
                    bundle = solve_bundle(domain, rule.predicate, rule.var_refs)
                    counter_bundle = None
                elif verdict == "MUST-violate":
                    bundle = solve_bundle(
                        domain, Not(rule.predicate), rule.var_refs
                    )
                    counter_bundle = None
                else:  # MAY-violate: both completions are solver-satisfiable
                    bundle = solve_bundle(domain, rule.predicate, rule.var_refs)
                    counter_bundle = solve_bundle(
                        domain, Not(rule.predicate), rule.var_refs
                    )

                cases.append(
                    GeneratedCase(
                        rule_id=rule.rule_id,
                        boundary_description=boundary.description,
                        side=side,
                        test_value=value,
                        verdict=verdict,
                        bundle=bundle,
                        counter_bundle=counter_bundle,
                    )
                )
    return cases


def _format_bundle(bundle: dict) -> str:
    if not bundle:
        return ""
    return ";".join(f"{name}={value}" for name, value in sorted(bundle.items()))


def report(cases: list) -> None:
    fieldnames = [
        "rule_id",
        "boundary_description",
        "side",
        "test_value",
        "verdict",
        "bundle",
        "counter_bundle",
    ]
    with OUTPUT_CSV.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for case in cases:
            writer.writerow(
                {
                    "rule_id": case.rule_id,
                    "boundary_description": case.boundary_description,
                    "side": case.side,
                    "test_value": case.test_value,
                    "verdict": case.verdict,
                    "bundle": _format_bundle(case.bundle),
                    "counter_bundle": _format_bundle(case.counter_bundle),
                }
            )

    skipped = [rule.rule_id for rule in REAL_RULES if not rule.boundaries]
    verdict_counts = {"MUST-violate": 0, "MAY-violate": 0, "CERTIFIED-compliant": 0}
    for case in cases:
        verdict_counts[case.verdict] += 1

    print("metric,value")
    print(f"rules_with_boundaries,{len({c.rule_id for c in cases})}")
    print(f"rules_skipped_no_numeric_threshold,{len(skipped)}")
    for rule_id in skipped:
        print(f"  skipped,{rule_id}")
    print(f"cases_generated,{len(cases)}")
    for verdict, count in verdict_counts.items():
        print(f"cases_{verdict.replace('-', '_')},{count}")
    print(f"csv,{OUTPUT_CSV}")


if __name__ == "__main__":
    report(synthesize_cases())
