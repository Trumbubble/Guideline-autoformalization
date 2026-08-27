"""Cheap differential check: a second, independently-worded Z3 encoding for
three fixtures, evaluated against the same boundary bundles. See ../CHECKER.md."""

import csv
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from z3 import And, Implies, Solver, sat

from real_rules import DOMAIN_AXIOMS, REAL_RULES  # noqa: E402
from synthesize_boundary_tests import synthesize_cases  # noqa: E402

OUTPUT_CSV = Path(__file__).parent / "differential_check_results.csv"

RULES_BY_ID = {rule.rule_id: rule for rule in REAL_RULES}


@dataclass(frozen=True, eq=False)
class AltEncoding:
    rule_id: str
    alt_source_sentence: str
    predicate: object  # Z3 BoolRef, built from the primary rule's own var_refs


def _build_alt_encodings() -> list:
    ace = RULES_BY_ID["ace_inhibitor_hyperkalemia_contraindication"].var_refs
    rivaroxaban = RULES_BY_ID["rivaroxaban_dose_reduction"].var_refs
    digoxin = RULES_BY_ID["digoxin_renal_dose_reduction"].var_refs

    return [
        # Primary reads "above 5.0" as a strict cutoff (compliant at
        # exactly 5.0). An equally natural independent reading treats
        # 5.0 itself as already too high. Note the primary now frames this
        # as a contraindication (Implies(K>5.0, not on)) per the DSL's
        # action-atom requirement; the alt keeps the same shape with the
        # inclusivity flipped.
        AltEncoding(
            rule_id="ace_inhibitor_hyperkalemia_contraindication",
            alt_source_sentence=(
                "An ACE inhibitor should not be started in a patient with "
                "serum potassium at or above 5.0 mmol/L."
            ),
            predicate=Implies(
                ace["potassium"] >= 5.0, ace["ace_inhibitor_on"] == False
            ),
        ),
        # Primary now encodes the dose-reduction tier with an explicit
        # Between(15, 50, lo_inclusive=True, hi_inclusive=False) -- the DSL
        # forces this decision to be made rather than left implicit. This
        # alt picks the other equally natural inclusivity for the same
        # prose ("between 15 and 50") to show what a second reading would
        # have produced before that decision was forced.
        AltEncoding(
            rule_id="rivaroxaban_dose_reduction",
            alt_source_sentence=(
                "A reduced dose of rivaroxaban should be used if "
                "creatinine clearance is 15 mL/min or above but up to and "
                "including 50 mL/min."
            ),
            predicate=Implies(
                And(
                    rivaroxaban["rivaroxaban_on"],
                    rivaroxaban["creatinine_clearance_ml_min"] >= 15,
                    rivaroxaban["creatinine_clearance_ml_min"] <= 50,
                ),
                rivaroxaban["rivaroxaban_dose_reduced"],
            ),
        ),
        # Primary reads "below 60 mL/min" as a strict cutoff (60 itself
        # needs no dose reduction). An equally natural independent reading
        # of "60 mL/min or less" includes the endpoint.
        AltEncoding(
            rule_id="digoxin_renal_dose_reduction",
            alt_source_sentence=(
                "Digoxin dose should be reduced in patients with a "
                "creatinine clearance of 60 mL/min or less."
            ),
            predicate=Implies(
                And(
                    digoxin["digoxin_on"],
                    digoxin["creatinine_clearance_ml_min"] <= 60,
                ),
                digoxin["digoxin_dose_reduced"],
            ),
        ),
    ]


ALT_ENCODINGS = _build_alt_encodings()
ALT_BY_RULE_ID = {alt.rule_id: alt for alt in ALT_ENCODINGS}


@dataclass(frozen=True, eq=False)
class DifferentialResult:
    rule_id: str
    boundary_description: str
    side: str
    source: str  # "bundle" | "counter_bundle"
    bundle: dict
    primary_holds: bool
    alt_holds: bool
    agree: bool


def pin_domain(rule, bundle: dict) -> list:
    return list(DOMAIN_AXIOMS) + [
        var_ref == bundle[name] for name, var_ref in rule.var_refs.items()
    ]


def holds(predicate, domain: list) -> bool:
    solver = Solver()
    solver.add(*domain, predicate)
    return solver.check() == sat


def run_differential_check() -> list:
    results = []
    for case in synthesize_cases():
        alt = ALT_BY_RULE_ID.get(case.rule_id)
        if alt is None:
            continue
        rule = RULES_BY_ID[case.rule_id]

        instances = [("bundle", case.bundle)]
        if case.counter_bundle is not None:
            instances.append(("counter_bundle", case.counter_bundle))

        for source, bundle in instances:
            domain = pin_domain(rule, bundle)
            primary_holds = holds(rule.predicate, domain)
            alt_holds = holds(alt.predicate, domain)
            results.append(
                DifferentialResult(
                    rule_id=case.rule_id,
                    boundary_description=case.boundary_description,
                    side=case.side,
                    source=source,
                    bundle=bundle,
                    primary_holds=primary_holds,
                    alt_holds=alt_holds,
                    agree=primary_holds == alt_holds,
                )
            )
    return results


def _format_bundle(bundle: dict) -> str:
    return ";".join(f"{name}={value}" for name, value in sorted(bundle.items()))


def report(results: list) -> None:
    fieldnames = [
        "rule_id",
        "boundary_description",
        "side",
        "source",
        "bundle",
        "primary_holds",
        "alt_holds",
        "agree",
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
                    "bundle": _format_bundle(result.bundle),
                    "primary_holds": int(result.primary_holds),
                    "alt_holds": int(result.alt_holds),
                    "agree": int(result.agree),
                }
            )

    disagreements = [r for r in results if not r.agree]

    print("metric,value")
    print(f"rules_with_alt_encoding,{len(ALT_ENCODINGS)}")
    print(f"bundles_checked,{len(results)}")
    print(f"disagreements,{len(disagreements)}")
    print(f"csv,{OUTPUT_CSV}")

    if disagreements:
        print()
        print("-- disagreements found: inspect these --")
        for result in disagreements:
            rule = RULES_BY_ID[result.rule_id]
            alt = ALT_BY_RULE_ID[result.rule_id]
            print(f"\n{result.rule_id} [{result.boundary_description} / {result.side} / {result.source}]")
            print(f"  primary : {rule.source_sentence}")
            print(f"            -> holds={result.primary_holds}")
            print(f"  alt     : {alt.alt_source_sentence}")
            print(f"            -> holds={result.alt_holds}")
            print(f"  bundle  : {_format_bundle(result.bundle)}")
    else:
        print("no disagreements on the checked bundles")


if __name__ == "__main__":
    report(run_differential_check())
