#!/usr/bin/env python3
"""Ablation: what each guard is worth, measured as verdict divergence.

THE IDEA, IN ONE PARAGRAPH.  Every guard in this system, if you remove it,
produces the same observable: the checker returns a DIFFERENT VERDICT about a
patient than the full system does.  Only the direction differs.  So this is one
harness with one metric, not five experiments.

WHY IT NEEDS NO GROUND TRUTH.  This is a differential measurement -- full
system versus ablated system, same rules, same patients.  The claim is not "we
are right"; it is "these designs disagree, here is how often and in which
direction".  Nobody has to label anything.

THE TWO DIRECTIONS, which are not equally bad:

  wrongly certified  the ablated checker says CERTIFIED-compliant where the
                     full one does not.  This is the SAFETY column: it reports
                     a patient as fine where the guideline was merely silent,
                     and nothing downstream can see that it happened.

  wrongly accused    the ablated checker reports a definite violation where the
                     full one does not.  This is the USABILITY column -- alert
                     fatigue -- which routes back to the safety column the
                     moment a clinician switches the alert off.

THE DENOMINATOR is (rule x partial plan) pairs, not rules.  Patients come out
of the solver, so that axis is free and unbiased; rules are the scarce axis.
Both counts are printed, and the paper must quote both.

    python3 ablation.py                      # fixtures + every bundle found
    python3 ablation.py --plans 200 --seed 7
    python3 ablation.py --out ablation.csv
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # Part B

import z3                                                          # noqa: E402
import real_rules                                                  # noqa: E402
import api                                                         # noqa: E402
from from_dsl import load_bundle                                   # noqa: E402

# Ablations that can be measured per (rule, plan) with no model call.
ABLATIONS = ("stub_false", "stub_true", "no_deontic",
             "no_occurrence_flag", "naive_single_call")

# The fifth guard -- canonicalise-before-the-vacuity-sweep -- is not a
# per-plan ablation: it changes which rules EXIST, not what verdict they give.
# It is measured separately, in `canonicalisation_report`.
#
# The sixth -- force the partial path after a binder refusal -- cannot be run
# here at all.  It changes what the FORMALIZER produces, so measuring it needs a
# live re-run of the gate with the guard disabled.  Reported as not-measured
# rather than silently dropped.

CERTIFIED = {"CERTIFIED-compliant"}
DEFINITE_VIOLATION = {"MUST-violate"}


# ----------------------------------------------------------------- accessors
# Fixtures are SpecClause, Track A rules are LoadedRule.  Duck-typed, so the
# harness does not need to know which it has.

def domain_of(rule) -> list:
    return list(getattr(rule, "domain", None) or real_rules.DOMAIN_AXIOMS)


def holes_of(rule) -> tuple:
    return tuple(getattr(rule, "holes", ()) or ())


def deontic_of(rule) -> str:
    return getattr(rule, "deontic", "obligation")


# ------------------------------------------------------------------ verdicts

def verdict_full(rule, domain) -> str:
    """What the system as shipped says."""
    if deontic_of(rule) == "permission":
        return api.classify_option(domain, rule.applies, rule.taken)
    return api.classify(domain, rule.predicate)


def verdict_ablated(rule, domain, mode: str) -> str:
    if mode == "naive_single_call":
        # The single-call check: is a violation satisfiable at all?  It cannot
        # tell "every completion violates" from "some completion violates", so
        # anything satisfiable reads as a violation.
        #
        # It uses the rule's OWN violation condition, which keeps the deontic
        # operator intact.  Using not-phi instead would make this row also drop
        # that operator -- every permission would read as violable -- and the
        # two rows would stop being independent.  Same confound as the stub
        # rows had; each ablation must change exactly one thing.
        viol = getattr(rule, "violation", None)
        if viol is None:
            viol = z3.Not(rule.predicate)          # fixtures carry no operator
        s = z3.Solver()
        s.add(*domain, viol)
        return "MUST-violate" if s.check() == z3.sat else "CERTIFIED-compliant"

    if mode == "no_deontic":
        # Every rule treated as an obligation, which is what a pipeline that
        # lowers to CQL and reads `Violated` does.
        return api.classify(domain, rule.predicate)

    if mode == "no_occurrence_flag":
        # A time window lowers to "the event happened AND it was within n
        # days".  Drop the first conjunct -- i.e. assume it always happened --
        # and a patient who was NEVER TESTED satisfies the window, because the
        # solver simply picks a small number of days. "The test was never done"
        # is the commonest real failure of a monitoring recommendation, so this
        # is the guard that makes that case reachable at all.
        occ = [v for n, v in rule.var_refs.items() if n.endswith("_occurred")]
        if not occ:
            return verdict_full(rule, domain)      # no time window in this rule
        return _sub_verdict(rule, domain,
                            [(o, z3.BoolVal(True)) for o in occ])

    if mode in ("stub_false", "stub_true"):
        # BOOLEAN holes only.  An unbound EVENT also leaves free symbols
        # behind -- the days-since counter and any baseline value -- but those
        # are Int and Real, and "assume the undefined predicate is false" has
        # no meaning for a quantity.  Substituting a boolean into them is a
        # sort error, and pretending otherwise would silently measure
        # something other than the guard under test.
        refs = [rule.var_refs[h] for h in holes_of(rule)
                if h in rule.var_refs and z3.is_bool(rule.var_refs[h])]
        if not refs:
            return verdict_full(rule, domain)      # no hole: nothing to stub
        val = z3.BoolVal(mode == "stub_true")
        return _sub_verdict(rule, domain, [(r, val) for r in refs])

    raise ValueError(f"unknown ablation {mode!r}")


def _sub_verdict(rule, domain, pairs) -> str:
    """Verdict after substituting into the rule, KEEPING its deontic operator.

    Getting this wrong contaminated two rows at once: calling the obligation
    verdict on a permission meant `stub_false` was also silently dropping the
    deontic operator, so its numbers included that effect and the two rows were
    no longer independent. Each ablation must change exactly one thing."""
    if deontic_of(rule) == "permission":
        return api.classify_option(domain,
                                   z3.substitute(rule.applies, *pairs),
                                   z3.substitute(rule.taken, *pairs))
    return api.classify(domain, z3.substitute(rule.predicate, *pairs))


def applies(rule, mode: str) -> bool:
    """Can this guard act on this rule at all?

    A rate over pairs the guard CANNOT touch is diluted by construction: a rule
    with no undefined predicate can never move under a substitution, however
    many records you throw at it, and counting those pairs made the two rule
    sets look further apart than they are. So every rate is reported over the
    applicable pairs, and this predicate -- not a note in a paper -- is what
    defines them.

      stub_false / stub_true   at least one undefined predicate of BOOLEAN
                               sort. An unbound event also leaves Int and Real
                               symbols free, but substituting true or false
                               into a quantity is meaningless, so those rules
                               are not applicable to a substitution.
      no_deontic               the rule is a permission. An obligation treated
                               as an obligation is unchanged.
      no_occurrence_flag       the rule mentions an occurrence flag, i.e. it
                               contains a time window.
      naive_single_call        every rule.
    """
    if mode in ("stub_false", "stub_true"):
        return any(h in rule.var_refs and z3.is_bool(rule.var_refs[h])
                   for h in holes_of(rule))
    if mode == "no_deontic":
        return deontic_of(rule) == "permission"
    if mode == "no_occurrence_flag":
        return any(n.endswith("_occurred") for n in rule.var_refs)
    if mode == "naive_single_call":
        return True
    raise ValueError(mode)


def direction(full: str, ablated: str) -> str | None:
    """Which way the verdict moved, or None if it did not move meaningfully.

    A PERMISSION's full verdict lives on its own lattice (OPTION-taken,
    -not-taken, -available, -not-applicable), none of which is an accusation
    and none of which is a compliance claim.  So "CERTIFIED-compliant" against
    an OPTION-* baseline is NOT a safety regression -- both are non-accusatory,
    they just answer different questions.  Only an accusation counts as a move
    there.  Scoring it naively reported the deontic ablation as *certifying*
    patients, which is the opposite of what dropping the operator does."""
    if full.startswith("OPTION"):
        # ANY violation verdict is an accusation here, MAY-violate included.
        # The permission lattice has no accusing state at all, so a checker
        # that reports "may violate -- witness: the option was not taken" is
        # already telling a clinician they may have deviated, on a statement
        # the guideline made optional. Counting only MUST-violate scored this
        # ablation at 0.0% while it was in fact firing on every permissive rule.
        return "wrongly_accused" if "violate" in ablated else None
    if ablated in CERTIFIED and full not in CERTIFIED:
        return "wrongly_certified"
    if ablated in DEFINITE_VIOLATION and full not in DEFINITE_VIOLATION:
        return "wrongly_accused"
    return None


# --------------------------------------------------------------------- plans

def sample_plans(rule, domain, n: int, rng: random.Random) -> list[dict]:
    """Partial plans drawn from the solver, so every value is one the domain
    axioms actually permit.  A plan pins a random subset of the rule's
    variables and leaves the rest free -- which is the whole point: a full
    assignment has no unknowns, and a three-valued verdict only differs from a
    two-valued one when something IS unknown.

    HALF THE PLANS ARE DRAWN FROM THE VIOLATING REGION, and this is stated
    rather than buried, because it changes what the numbers mean.  A uniformly
    random plan almost never lands where two encodings disagree: measured, the
    deontic ablation scored 0.0% under uniform sampling not because dropping
    the operator is harmless but because no sampled plan happened to pin a
    permission's antecedent true and its consequent false -- which is exactly
    the patient a clinician would be alerted about.  So half of each rule's
    plans are subsets of a model of (D and not phi): a completion in which the
    rule really is broken.  Rates below are therefore rates over a sample
    ENRICHED for disagreement, not over an arbitrary patient population, and
    the paper must say so."""
    # SORTED, not list(): from_dsl builds var_refs from a set, so its key
    # order varies between processes with Python's hash randomisation. Left
    # unsorted, the same --seed gave different counts run to run (103 vs 104
    # pairs), which would make the paper's numbers unreproducible.
    names = sorted(rule.var_refs)
    if not names:
        return []

    def model_of(*extra):
        s_ = z3.Solver()
        s_.set("random_seed", rng.randrange(1 << 30))
        s_.add(*domain, *extra)
        return s_.model() if s_.check() == z3.sat else None

    plans, seen = [], set()
    for i in range(n * 6):                      # oversample; dedupe below
        if len(plans) >= n:
            break
        targeted = (i % 2 == 1)
        model = model_of(z3.Not(rule.predicate)) if targeted else model_of()
        if model is None:
            model = model_of()                  # unviolable rule: fall back
            if model is None:
                return []
        k = rng.randint(0, max(0, len(names) - 1))     # leave >=1 free
        plan = {n_: model.eval(rule.var_refs[n_], model_completion=True)
                for n_ in rng.sample(names, k)}
        key = tuple(sorted((k_, str(v)) for k_, v in plan.items()))
        if key in seen:
            continue
        seen.add(key)
        plans.append(plan)
    return plans


def pin(domain: list, rule, plan: dict) -> list:
    return list(domain) + [rule.var_refs[n] == v for n, v in plan.items()]


# ------------------------------------------------------- canonicalisation row

def canonicalisation_report(bundles) -> dict:
    """Not a per-plan ablation: this guard changes which rules EXIST.

    Two variable names bound to the same code are two distinct SYMBOLS to the
    solver, so a rule written over both -- (A and B) -> B -- is satisfiable,
    violable, and loads clean.  Merge them first and the same rule is a
    tautology: no patient can violate it, so it returns CERTIFIED-compliant for
    every patient it is ever tested against.  Its wrongly-certified rate is
    therefore 100% of whatever plans you throw at it, which is why it is
    reported as a count of RULES rather than folded into the per-pair table.

    Measured by lowering each rule twice -- once with the alias merge the
    loader applies, once without -- and asking the solver whether anyone can
    violate it, each way."""
    import dsl
    out = {"caught": 0, "checked": 0, "rejected_other": [], "detail": []}
    for path in bundles:
        reg, rules, _refusals = dsl.read_bundle(str(path))
        aliases = dsl.canonical_aliases(reg)

        plain = dsl.make_env(reg)                  # every name its own symbol
        merged = dsl.make_env(reg)                 # aliases collapsed
        for src, dst in aliases.items():
            merged[src] = merged[dst]

        for r in rules:
            out["checked"] += 1
            try:
                def violable(env):
                    s_ = z3.Solver()
                    s_.add(*dsl.domain_axioms(reg, env),
                           z3.Not(dsl.rule_to_z3(r, env)))
                    return s_.check() == z3.sat
                if violable(plain) and not violable(merged):
                    out["caught"] += 1
                    out["detail"].append(
                        f"{path.name}:{r.rule_id} -- violable only because two "
                        f"names for one concept stayed distinct")
            except Exception as exc:                # a malformed rule is not this
                out["detail"].append(f"{path.name}:{r.rule_id} skipped ({exc})")

        _l, _rf, _rg, rejected = load_bundle(str(path))
        for rj in rejected:
            out["rejected_other"].append(
                f"{path.name}:{rj['rule_id']} {rj.get('codes')}")
    return out


# ---------------------------------------------------------------------- main

def collect_rules(bundle_dir: Path, use_fixtures: bool):
    rules, sources = [], []
    if use_fixtures:
        for r in real_rules.REAL_RULES:
            rules.append(r)
            sources.append("fixtures")
    bundles = sorted(bundle_dir.glob("bundle.*.json"))
    for path in bundles:
        loaded, _refusals, _reg, _rejected = load_bundle(str(path))
        for r in loaded:
            rules.append(r)
            sources.append(path.stem)
    return rules, sources, bundles


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plans", type=int, default=50,
                    help="partial plans sampled per rule (default 50)")
    ap.add_argument("--seed", type=int, default=0, help="RNG seed (default 0)")
    ap.add_argument("--out", default="ablation.csv", help="per-pair CSV")
    ap.add_argument("--no-fixtures", action="store_true",
                    help="Track A bundles only")
    ap.add_argument("--no-ranges", action="store_true",
                    help="generated rules without plausible ranges, as the live "
                         "pipeline supplies them")
    ap.add_argument("--generated", action="store_true",
                    help="run over the designed sweep instead of the real rules")
    a = ap.parse_args(argv)

    rng = random.Random(a.seed)
    here = Path(__file__).resolve().parent
    if a.generated:
        from generated_rules import build as build_generated
        rules, gen_stats = build_generated(ranges=not a.no_ranges)
        sources = ["generated"] * len(rules)
        bundles = []
    else:
        gen_stats = None
        rules, sources, bundles = collect_rules(here.parent, not a.no_fixtures)

    tally = {m: defaultdict(int) for m in ABLATIONS}
    rows, pairs, n_holes, n_perms = [], 0, 0, 0

    for rule, source in zip(rules, sources):
        domain = domain_of(rule)
        if holes_of(rule):
            n_holes += 1
        if deontic_of(rule) == "permission":
            n_perms += 1
        for plan in sample_plans(rule, domain, a.plans, rng):
            d = pin(domain, rule, plan)
            full = verdict_full(rule, d)
            pairs += 1
            for mode in ABLATIONS:
                abl = verdict_ablated(rule, d, mode)
                tally[mode]["pairs"] += 1
                if applies(rule, mode):
                    tally[mode]["applies"] += 1
                move = direction(full, abl)
                if move:
                    tally[mode][move] += 1
                rows.append({
                    "source": source, "rule_id": rule.rule_id,
                    "deontic": deontic_of(rule),
                    "holes": ";".join(holes_of(rule)),
                    "pinned": ";".join(sorted(plan)),
                    "ablation": mode, "applies": int(applies(rule, mode)),
                    "full": full, "ablated": abl, "direction": move or "",
                })

    with open(a.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else
                           ["source", "rule_id", "ablation"])
        w.writeheader()
        w.writerows(rows)

    canon = canonicalisation_report(bundles)

    # ---- report -----------------------------------------------------------
    print("metric,value")
    print(f"rules,{len(rules)}")
    print(f"rules_with_a_hole,{n_holes}")
    print(f"rules_permissive,{n_perms}")
    print(f"plans_per_rule,{a.plans}")
    print(f"rule_plan_pairs,{pairs}")
    print(f"seed,{a.seed}")
    if gen_stats is not None:
        print(f"generated_rules_rejected_by_the_validator,{gen_stats['rejected']}")
        print(f"grid_cells,{len(gen_stats['cells'])}")
        print(f"rules_with_a_time_window,"
              f"{sum(1 for r in rules if any(n.endswith('_occurred') for n in r.var_refs))}")
    print()
    # `applies_to` is the denominator every rate uses; `pairs` is printed
    # beside it so the dilution is visible rather than implied.
    print("ablation,pairs,applies_to,to_certified,to_must_violate,rate_pct")
    for mode in ABLATIONS:
        t = tally[mode]
        app = t["applies"] or 1
        moved = t["wrongly_certified"] + t["wrongly_accused"]
        print(f"{mode},{t['pairs']},{t['applies']},{t['wrongly_certified']},"
              f"{t['wrongly_accused']},{100*moved/app:.1f}")
    print()
    print("# canonicalise-before-the-vacuity-sweep is a rule-level guard, not a")
    print("# per-plan one: without it these rules load clean and then certify")
    print("# every patient they are ever tested against.")
    print(f"rules_checked,{canon['checked']}")
    print(f"tautologies_caught_only_by_canonicalising,{canon['caught']}")
    for d in canon["detail"]:
        print(f"  {d}")
    if canon["rejected_other"]:
        print("# also rejected by the loader (a different guard):")
        for d in canon["rejected_other"]:
            print(f"  {d}")
    print()
    print("# NOT MEASURED HERE: forcing the partial path after a binder refusal.")
    print("# That guard changes what the FORMALIZER emits, not what verdict a")
    print("# rule yields, so it needs a live gate re-run with the guard off.")
    print(f"csv,{a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
