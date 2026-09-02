#!/usr/bin/env python3
"""How small is the explanation behind a MAY-violate verdict?

THE QUESTION.  When the checker says "this may be a violation, I cannot tell",
it owes the reader a reason.  The unhelpful reason is the whole model: every
unstated variable the solver had to fix to produce a witness, which for a real
rule is a wall of values.  The useful reason is the SMALLEST set of those values
that on its own forces the violation -- "it becomes a certain violation if the
potassium check did not occur", one fact rather than eleven.

`api.minimal_counterexample_core` already computes the small set, and
`tests/benchmark.py` already reports its average size.  What neither records is
the DENOMINATOR: how many unstated variables there were to begin with.  A mean
core of 1.4 is meaningless without it -- it is impressive against 11 and trivial
against 2.  This script records both, per verdict.

    python3 explanation_size.py                    # generated rules
    python3 explanation_size.py --existing         # the system's own rules
    python3 explanation_size.py --plans 50 --seed 0 --out explanation.csv

NOT A MINIMUM CORE.  The minimiser drops one assumption at a time and keeps the
drop if the remainder still cannot be made compliant.  That yields a MINIMAL
core -- one nothing further can be removed from -- not a MINIMUM one, which
would be the smallest such set over all orders.  Reported sizes are therefore an
upper bound on the smallest possible explanation.  The traversal order is
`sorted(free_names)`, so the result is at least deterministic; an unsorted order
would make the reported average depend on Python's hash seed.
"""

from __future__ import annotations

import argparse
import csv
import random
import statistics as st
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api                                                        # noqa: E402
import ablation as A                                              # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plans", type=int, default=50)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--existing", action="store_true",
                    help="the system's own rules instead of the generated set")
    ap.add_argument("--out", default="explanation.csv")
    a = ap.parse_args(argv)

    rng = random.Random(a.seed)
    if a.existing:
        rules, _sources, _bundles = A.collect_rules(
            Path(__file__).resolve().parent.parent, True)
    else:
        from generated_rules import build
        rules, _ = build()

    rows, free_n, core_n = [], [], []
    verdicts = Counter()

    for rule in rules:
        domain = A.domain_of(rule)
        for plan in A.sample_plans(rule, domain, a.plans, rng):
            pinned = A.pin(domain, rule, plan)
            verdict = A.verdict_full(rule, pinned)
            verdicts[verdict] += 1
            if verdict != "MAY-violate":
                continue                       # only an undetermined verdict
                                               # needs an explanation
            free = [n for n in sorted(rule.var_refs) if n not in plan]
            core = api.minimal_counterexample_core(rule, pinned, free)
            free_n.append(len(free))
            core_n.append(len(core))
            rows.append({"rule_id": rule.rule_id,
                         "free_variables": len(free),
                         "core_size": len(core),
                         "core": ";".join(sorted(core)),
                         "pinned": ";".join(sorted(plan))})

    with open(a.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["rule_id", "free_variables",
                                           "core_size", "core", "pinned"])
        w.writeheader()
        w.writerows(rows)

    print("metric,value")
    print(f"rules,{len(rules)}")
    print(f"records,{sum(verdicts.values())}")
    for v, n in verdicts.most_common():
        print(f"verdict_{v},{n}")
    print(f"may_violate_explained,{len(rows)}")
    if not rows:
        print("# no MAY-violate verdicts: nothing to explain")
        return 0
    print(f"free_variables_mean,{st.mean(free_n):.2f}")
    print(f"free_variables_median,{st.median(free_n)}")
    print(f"free_variables_max,{max(free_n)}")
    print(f"core_size_mean,{st.mean(core_n):.2f}")
    print(f"core_size_median,{st.median(core_n)}")
    print(f"core_size_max,{max(core_n)}")
    print(f"reduction_mean_ratio,{st.mean(free_n)/st.mean(core_n):.2f}")
    print(f"single_fact_explanations_pct,"
          f"{100*sum(1 for c in core_n if c == 1)/len(core_n):.1f}")
    print()
    print("core_size,count,pct")
    dist = Counter(core_n)
    for k in sorted(dist):
        print(f"{k},{dist[k]},{100*dist[k]/len(core_n):.1f}")
    print(f"csv,{a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
