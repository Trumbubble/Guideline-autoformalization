import random

from z3 import Bool, Real, Solver, And, Implies, Not, sat, unsat


PLAN_COUNT = 20
DRUG_COUNT = 30
ASSIGNMENT_COUNT = 14


def main() -> None:
    metformin_on = Bool("metformin_on")
    drugs = [Bool(f"drug_{index}") for index in range(1, DRUG_COUNT + 1)]
    egfr = Real("eGFR")
    domain = And(egfr >= 0, egfr <= 120)
    spec = Implies(metformin_on, egfr >= 30)

    random_generator = random.Random(0)
    plans = []
    for _ in range(PLAN_COUNT):
        assigned = random_generator.sample(range(DRUG_COUNT), ASSIGNMENT_COUNT)
        plans.append(
            And(*[
                drugs[index] == random_generator.choice([True, False])
                for index in assigned
            ])
        )

    naive_flagged = 0
    three_valued_counts = {"MUST": 0, "MAY": 0, "CERTIFIED": 0}

    for plan in plans:
        naive_solver = Solver()
        naive_solver.add(domain, plan, Not(spec))
        if naive_solver.check() == sat:
            naive_flagged += 1

        compliant_solver = Solver()
        compliant_solver.add(domain, plan, spec)
        violating_solver = Solver()
        violating_solver.add(domain, plan, Not(spec))

        if compliant_solver.check() == unsat:
            three_valued_counts["MUST"] += 1
        elif violating_solver.check() == unsat:
            three_valued_counts["CERTIFIED"] += 1
        else:
            three_valued_counts["MAY"] += 1

    print(
        naive_flagged,
        three_valued_counts["MUST"],
        three_valued_counts["MAY"],
        three_valued_counts["CERTIFIED"],
    )


if __name__ == "__main__":
    main()