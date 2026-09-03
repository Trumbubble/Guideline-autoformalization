"""Synthetic 200-patient / 25-clause solve-performance benchmark. See ../CHECKER.md."""

import csv
import random
import time
from dataclasses import dataclass
from pathlib import Path

from z3 import And, Bool, BoolVal, Implies, Int, Not, Or, Real, Solver, sat, unsat


PATIENT_COUNT = 200
CLAUSE_COUNT = 25
DRUG_COUNT = 15
MONITOR_COUNT = 15
ACTION_COUNT = DRUG_COUNT + MONITOR_COUNT
VISIT_HORIZON = 52
OUTPUT_CSV = Path(__file__).parent / "benchmark_results.csv"


@dataclass(frozen=True)
class Patient:
    patient_id: int
    labs: dict[str, float]
    comorbidities: frozenset[str]


@dataclass(frozen=True)
class Clause:
    clause_id: str
    kind: str
    drug: int
    monitor: int
    comorbidity: str
    lab: str
    threshold: float
    window: int


def action_name(kind: str, index: int) -> str:
    return f"{kind}_{index}"


def action_variables(kind: str, index: int) -> list:
    return [
        Bool(f"{action_name(kind, index)}_v{visit}")
        for visit in range(VISIT_HORIZON)
    ]


def generate_patients(random_generator: random.Random) -> list[Patient]:
    lab_names = [f"lab_{index}" for index in range(1, 11)]
    comorbidity_names = [f"comorbidity_{index}" for index in range(1, 6)]
    patients = []
    for patient_id in range(1, PATIENT_COUNT + 1):
        selected_labs = random_generator.sample(
            lab_names, random_generator.randint(5, 10)
        )
        labs = {
            lab: round(random_generator.uniform(0, 120), 1)
            for lab in selected_labs
        }
        comorbidities = frozenset(
            random_generator.sample(
                comorbidity_names, random_generator.randint(2, 5)
            )
        )
        patients.append(Patient(patient_id, labs, comorbidities))
    return patients


def generate_clauses(random_generator: random.Random) -> list[Clause]:
    clauses = []
    for index in range(1, CLAUSE_COUNT + 1):
        if index <= 12:
            clauses.append(
                Clause(
                    f"contraindication_{index}",
                    "contraindication",
                    random_generator.randint(1, DRUG_COUNT),
                    0,
                    f"comorbidity_{random_generator.randint(1, 5)}",
                    f"lab_{random_generator.randint(1, 10)}",
                    random_generator.choice([30.0, 45.0, 60.0]),
                    0,
                )
            )
        else:
            clauses.append(
                Clause(
                    f"monitoring_{index - 12}",
                    "monitoring",
                    random_generator.randint(1, DRUG_COUNT),
                    random_generator.randint(1, MONITOR_COUNT),
                    "",
                    "",
                    0.0,
                    random_generator.randint(1, 7),
                )
            )
    return clauses


def frame_axioms() -> list:
    axioms = []
    for kind, count in (("drug", DRUG_COUNT), ("monitor", MONITOR_COUNT)):
        for index in range(1, count + 1):
            variables = action_variables(kind, index)
            axioms.extend(
                variables[visit] == variables[0]
                for visit in range(1, VISIT_HORIZON)
            )
    return axioms


def patient_domain(patient: Patient) -> list:
    constraints = [Int("visit") >= 0, Int("visit") < VISIT_HORIZON]
    for lab, value in patient.labs.items():
        lab_value = Real(lab)
        constraints.extend((lab_value == value, lab_value >= 0, lab_value <= 120))
    for index in range(1, 6):
        comorbidity = Bool(f"comorbidity_{index}")
        constraints.append(
            comorbidity == (f"comorbidity_{index}" in patient.comorbidities)
        )
    return constraints


def clause_formula(clause: Clause) -> object:
    drug = action_variables("drug", clause.drug)
    if clause.kind == "contraindication":
        trigger = And(
            Bool(clause.comorbidity),
            Real(clause.lab) < clause.threshold,
        )
        return Implies(trigger, And(*[Not(value) for value in drug]))

    monitor = action_variables("monitor", clause.monitor)
    obligations = []
    for visit in range(VISIT_HORIZON):
        end = min(VISIT_HORIZON, visit + clause.window + 1)
        obligations.append(Implies(drug[visit], Or(*monitor[visit:end])))
    return And(*obligations)


def make_plan(random_generator: random.Random) -> tuple[list, set[str]]:
    action_names = [
        action_name("drug", index) for index in range(1, DRUG_COUNT + 1)
    ] + [
        action_name("monitor", index) for index in range(1, MONITOR_COUNT + 1)
    ]
    mentioned = set(random_generator.sample(action_names, 14))
    assignments = []
    for name in sorted(mentioned):
        kind, index_text = name.split("_")
        assignments.append(
            action_variables(kind, int(index_text))[0] == BoolVal(False)
        )
    return assignments, set(action_names) - mentioned


def minimal_counterexample_core(
    base_constraints: list, formula: object, violation: object, free_names: set[str]
) -> list[str]:
    witness_solver = Solver()
    witness_solver.add(*base_constraints, violation)
    if witness_solver.check() != sat:
        return []

    model = witness_solver.model()
    latent_literals = []
    for name in sorted(free_names):
        kind, index_text = name.split("_")
        variable = action_variables(kind, int(index_text))[0]
        value = model.eval(variable, model_completion=True)
        latent_literals.append((name, variable == value))

    compliance_solver = Solver()
    compliance_solver.add(*base_constraints, formula)
    core = latent_literals[:]
    for candidate in latent_literals:
        reduced = [item for item in core if item != candidate]
        if compliance_solver.check(*[literal for _, literal in reduced]) == unsat:
            core = reduced
    return [name for name, _ in core]


def run_benchmark() -> list[dict[str, object]]:
    random_generator = random.Random(20260827)
    patients = generate_patients(random_generator)
    clauses = generate_clauses(random_generator)
    frames = frame_axioms()
    rows = []

    for patient in patients:
        plan, free_names = make_plan(random_generator)
        base = patient_domain(patient) + frames + plan
        for clause in clauses:
            formula = clause_formula(clause)
            violation = Not(formula)

            start = time.perf_counter()
            naive_solver = Solver()
            naive_solver.add(*base, violation)
            naive_result = naive_solver.check()
            naive_ms = (time.perf_counter() - start) * 1000

            start = time.perf_counter()
            compliant_solver = Solver()
            compliant_solver.add(*base, formula)
            compliant_result = compliant_solver.check()
            violating_solver = Solver()
            violating_solver.add(*base, violation)
            violating_result = violating_solver.check()
            three_valued_ms = (time.perf_counter() - start) * 1000

            if compliant_result == unsat:
                classification = "MUST-violate"
                counterexample = []
            elif violating_result == unsat:
                classification = "CERTIFIED-compliant"
                counterexample = []
            else:
                classification = "MAY-violate"
                counterexample = minimal_counterexample_core(
                    base, formula, violation, free_names
                )

            rows.append(
                {
                    "patient_id": patient.patient_id,
                    "clause_id": clause.clause_id,
                    "naive_violation": int(naive_result == sat),
                    "classification": classification,
                    "counterexample_core": ";".join(counterexample),
                    "naive_ms": f"{naive_ms:.4f}",
                    "three_valued_ms": f"{three_valued_ms:.4f}",
                }
            )
    return rows


def report(rows: list[dict[str, object]]) -> None:
    with OUTPUT_CSV.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    total = len(rows)
    naive_count = sum(row["naive_violation"] for row in rows)
    names = ("MUST-violate", "MAY-violate", "CERTIFIED-compliant")
    classifications = {
        name: sum(row["classification"] == name for row in rows)
        for name in names
    }
    may_rows = [row for row in rows if row["classification"] == "MAY-violate"]
    core_sizes = [
        len(row["counterexample_core"].split(";"))
        for row in may_rows
        if row["counterexample_core"]
    ]
    average_core_size = sum(core_sizes) / len(core_sizes) if core_sizes else 0
    average_naive_ms = sum(float(row["naive_ms"]) for row in rows) / total
    average_three_valued_ms = (
        sum(float(row["three_valued_ms"]) for row in rows) / total
    )

    print("metric,value")
    print(f"checks,{total}")
    print(f"naive_false_violation_rate,{naive_count / total:.4f}")
    print(f"three_valued_MUST_violate,{classifications['MUST-violate']}")
    print(f"three_valued_MAY_violate,{classifications['MAY-violate']}")
    print(
        f"three_valued_CERTIFIED_compliant,{classifications['CERTIFIED-compliant']}"
    )
    print(f"average_counterexample_core_size,{average_core_size:.4f}")
    print(f"average_naive_solve_ms,{average_naive_ms:.4f}")
    print(f"average_three_valued_solve_ms,{average_three_valued_ms:.4f}")
    print(f"csv,{OUTPUT_CSV}")


if __name__ == "__main__":
    report(run_benchmark())