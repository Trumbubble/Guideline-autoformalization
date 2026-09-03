#!/usr/bin/env python3
"""Two checks on undetermined verdicts.

RESOLUTION (experiment 3).  Supply the values the core named and re-run. Every
verdict should become MUST-violate. This is true by construction --- the core is
extracted precisely as a set whose values make compliance unsatisfiable --- so it
is a test of the implementation, not a finding, and it is reported as one. A
failure here would mean the minimiser returns cores that do not do what they
claim.

DETERMINATION (experiment 2, in a form that is actually sound).  ``The core is a
single variable'' does NOT mean ``measure that variable and you will know''. The
core is a sufficient condition for ONE outcome: those values force a violation,
but a different value may leave the verdict undetermined. The claim a clinical
reader would take from ``measure X'' is stronger:

    for EVERY value of X, the verdict is determined.

That is checkable exactly, and not by enumerating values. The verdict is
undetermined at x when a completion complies AND a completion violates. Take two
copies of the free variables, sharing only X, and ask whether both can hold at
once:

    SAT[ (D and P and phi)[R -> R1]  and  (D and P and not phi)[R -> R2] ]

with X shared between the copies and R the other free variables. Satisfiable
means some value of X still leaves the verdict undetermined, so measuring X is
not enough. Unsatisfiable means every value of X determines the verdict, and
measuring it resolves the case whatever comes back.

    python3 may_actionability.py --plans 50 --seed 0
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import z3                                                         # noqa: E402
import api                                                        # noqa: E402
import ablation as A                                              # noqa: E402


def resolves_to_must(rule, domain, core) -> str:
    """Experiment 3: pin what the core named, and re-read the verdict."""
    pinned = list(domain) + [rule.var_refs[n] == api._coerce(rule.var_refs[n], v)
                             for n, v in core.items()]
    return A.verdict_full(rule, pinned)


def measuring_determines(rule, domain, var, free) -> bool | None:
    """Experiment 2: does measuring `var` determine the verdict for EVERY value?

    Returns True if it does, False if some value leaves it undetermined, and
    None if the query could not be decided.
    """
    others = [n for n in free if n != var]
    if not others:
        return True                       # nothing else is free; var decides it
    s1, s2 = [], []
    for n in others:
        ref = rule.var_refs[n]
        s1.append((ref, z3.Const(f"{n}__c1", ref.sort())))
        s2.append((ref, z3.Const(f"{n}__c2", ref.sort())))
    comply = z3.And(*domain, rule.predicate)
    violate = z3.And(*domain, z3.Not(rule.predicate))
    solver = z3.Solver()
    solver.add(z3.substitute(comply, *s1))     # copy 1: some completion complies
    solver.add(z3.substitute(violate, *s2))    # copy 2: some completion violates
    r = solver.check()                         # ...both at the same value of var
    if r == z3.unsat:
        return True
    if r == z3.sat:
        return False
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plans", type=int, default=50)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default="may_actionability.csv")
    a = ap.parse_args(argv)

    from generated_rules import build
    rules, _ = build()
    holes = {r.rule_id: set(r.holes) for r in rules}
    rng = random.Random(a.seed)

    rows = []
    resolved = Counter()
    determ = Counter()

    for rule in rules:
        domain = A.domain_of(rule)
        for plan in A.sample_plans(rule, domain, a.plans, rng):
            pinned = A.pin(domain, rule, plan)
            if A.verdict_full(rule, pinned) != "MAY-violate":
                continue
            free = [n for n in sorted(rule.var_refs) if n not in plan]
            core = api.minimal_counterexample_core(rule, pinned, free)
            if not core:
                continue

            after = resolves_to_must(rule, pinned, core)
            resolved[after] += 1

            single = len(core) == 1
            var = next(iter(core)) if single else None
            det = measuring_determines(rule, pinned, var, free) if single else None
            if single:
                determ[det] += 1

            rows.append({
                "rule_id": rule.rule_id, "free": len(free),
                "core_size": len(core), "core": ";".join(sorted(core)),
                "verdict_after_supplying_core": after,
                "single_variable_core": int(single),
                "is_undefined_predicate":
                    int(bool(single and var in holes.get(rule.rule_id, set()))),
                "measuring_it_determines": "" if det is None else int(det),
            })

    with open(a.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    n = len(rows)
    print("metric,value")
    print(f"may_violate_verdicts,{n}")
    print()
    print("# EXPERIMENT 3 -- supply the core's values, re-read the verdict")
    for v, c in resolved.most_common():
        print(f"after_supplying_core_{v},{c},{100*c/n:.1f}%")
    print(f"resolved_to_a_definite_verdict_pct,"
          f"{100*(n - resolved['MAY-violate'])/n:.1f}")
    print()
    print("# EXPERIMENT 2 -- would measuring the named variable determine it?")
    singles = sum(determ.values())
    print(f"single_variable_cores,{singles},{100*singles/n:.1f}%")
    yes = determ[True]
    print(f"  measuring_it_determines_the_verdict,{yes},"
          f"{100*yes/singles:.1f}% of single-variable cores,"
          f"{100*yes/n:.1f}% of all MAY")
    print(f"  some_value_leaves_it_undetermined,{determ[False]},"
          f"{100*determ[False]/singles:.1f}%")
    if determ[None]:
        print(f"  undecided,{determ[None]}")
    und = sum(1 for r in rows if r["is_undefined_predicate"])
    print(f"  of which name an undefined predicate,{und},"
          f"{100*und/singles:.1f}% of single-variable cores")
    print(f"csv,{a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
