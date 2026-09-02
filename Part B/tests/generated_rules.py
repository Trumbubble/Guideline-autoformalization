#!/usr/bin/env python3
"""Rules built on purpose, so each ablation row rests on a designed sample.

WHY.  Measured over the rules that happen to exist, the ablation table is
uneven: 6 of 19 rules carry an undefined predicate and exactly 2 are
permissive, so the deontic row is two rules wide.  That is not a sample, it is
an accident.  This module sweeps the space the language can express and reports
how many rules landed in each cell, so no row is quietly resting on two.

WHAT IS VARIED (every combination, a few instances each):

    hole position   none | in the antecedent | in the consequent
    deontic force   obligation | permission
    atom kind       numeric threshold | boolean | time window | relative change
    structure       one atom | two under AND | two under OR

WHAT KEEPS IT HONEST.  Every generated rule is put through the SAME
`dsl.validate_rule` and `dsl.check_nondegenerate` the pipeline uses, and
anything they reject is discarded.  Without that this would be measuring a toy
language rather than the one the system actually accepts.  The count of
rejected candidates is reported, because a generator that silently drops most
of its output is not sweeping the space it claims to.

WHAT IT CANNOT TELL YOU.  Nothing about whether real guidelines look like this.
The vocabulary is synthetic and the distribution is designed for coverage, not
realism.  Read it beside the real-rule numbers, never instead of them.

ONE DELIBERATE DIFFERENCE from the current live pipeline: every numeric
variable here carries a plausible range.  Track A supplies none, so results on
generated rules will look cleaner than results on extracted ones -- not because
generated rules are easier, but because the extracted ones are missing that
constraint.  Reported separately for exactly that reason.
"""

from __future__ import annotations

import sys
from itertools import product
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "Part A"))

import dsl                                                        # noqa: E402
from from_dsl import LoadedRule, _mentioned                       # noqa: E402

# ------------------------------------------------------------------ vocabulary
# Synthetic but correctly shaped: a lab with a real range, patient facts, an
# action a care plan controls, a datable event, and an undefined predicate.

NUMERIC = [("potassium", "mmol/L", (2.0, 7.5), 5.0),
           ("egfr", "mL/min/1.73m2", (0.0, 200.0), 30.0)]
FACTS = ["has_diabetes", "has_ckd"]
ACTIONS = ["on_acei", "dose_reduced"]
EVENTS = [("potassium_check", 14), ("renal_review", 30)]
HOLES = ["has_high_risk", "has_adequate_trial"]
DOSE = ("insulin_dose", (0.0, 200.0))
BASELINE = "therapy_start"

HOLE_POS = ("none", "when", "then")
DEONTIC = ("obligation", "permission")
KIND = ("threshold", "boolean", "window", "change")
SHAPE = ("single", "and", "or")


def _vardef(name, typ, unit=None, role="observation", plausible=None,
            unbound=False):
    return dsl.VarDef(name=name, type=typ, unit=unit, description=name,
                      binding=None if unbound else f"http://example.org|{name}|1",
                      role=role, plausible=plausible, unbound=unbound)


def _build_one(hole_pos, deontic, kind, shape, i):
    """One rule plus the minimal registry it needs.

    The registry is built PER RULE, holding only the names that rule mentions.
    That mirrors the pipeline (a registry comes from one statement's bindings)
    and it is also required: the validator rejects a rule that leaves an
    action-role variable in its registry unreferenced, which is the shape where
    the recommended action was dropped and a precondition promoted in its
    place."""
    num, unit, rng, thr = NUMERIC[i % len(NUMERIC)]
    fact = FACTS[i % len(FACTS)]
    action = ACTIONS[i % len(ACTIONS)]
    event, days = EVENTS[i % len(EVENTS)]
    hole = HOLES[i % len(HOLES)]

    vars_, events_ = [], []

    # ---- the antecedent's characteristic atom --------------------------
    if kind == "threshold":
        atom = dsl.Cmp(num, "<", thr)
        vars_.append(_vardef(num, "real", unit, plausible=rng))
    elif kind == "boolean":
        atom = dsl.Cmp(fact, "==", True)
        vars_.append(_vardef(fact, "bool"))
    elif kind == "window":
        # Monitoring shape: the obligation IS the timely check, so the Since
        # goes in the consequent and the antecedent is "the drug is on".
        atom = dsl.Cmp(action, "==", True)
        vars_.append(_vardef(action, "bool", role="action"))
    elif kind == "change":
        atom = dsl.Delta(DOSE[0], BASELINE, "reduce", 0.5, None)
        vars_.append(_vardef(DOSE[0], "real", "unit", plausible=DOSE[1]))
        events_.append(dsl.EventDef(BASELINE, "start of therapy", unbound=True))
        vars_.append(_vardef(action, "bool", role="action"))
    else:
        raise ValueError(kind)

    # ---- structure -----------------------------------------------------
    # The second conjunct must be a DIFFERENT fact from the one the boolean
    # atom already uses, or `boolean-single`, `boolean-and` and `boolean-or`
    # collapse to the same rule and three cells of the grid are duplicates.
    other = FACTS[(i + 1) % len(FACTS)]
    if shape != "single":
        vars_.append(_vardef(other, "bool"))
        second = dsl.Cmp(other, "==", True)
        atom = (dsl.And([atom, second]) if shape == "and"
                else dsl.Or([atom, second]))

    # ---- assemble when / then ------------------------------------------
    if kind == "window":
        when = atom
        then = dsl.Since(event, days)
        events_.append(dsl.EventDef(event, f"{event} performed",
                                    binding=f"http://example.org|{event}|1"))
    elif kind == "change":
        when = dsl.Cmp(fact, "==", True)
        vars_.append(_vardef(fact, "bool"))
        then = dsl.And([atom, dsl.Cmp(action, "==", True)])
    else:
        when = atom
        then = dsl.Cmp(action, "==", True)
        vars_.append(_vardef(action, "bool", role="action"))

    # ---- the undefined predicate ---------------------------------------
    # In the antecedent it goes under OR, which is the shape the motivating
    # example has ("eGFR < 30 OR the patient is at high risk") and the shape
    # where guessing its value changes who the rule applies to.
    if hole_pos == "when":
        when = dsl.Or([when, dsl.Cmp(hole, "==", True)])
        vars_.append(_vardef(hole, "bool", unbound=True))
    elif hole_pos == "then":
        then = dsl.And([then, dsl.Cmp(hole, "==", True)])
        vars_.append(_vardef(hole, "bool", unbound=True))

    seen, uniq = set(), []
    for v in vars_:
        if v.name not in seen:
            seen.add(v.name)
            uniq.append(v)
    seen_e, uniq_e = set(), []
    for e in events_:
        if e.name not in seen_e:
            seen_e.add(e.name)
            uniq_e.append(e)

    reg = dsl.Registry(uniq, uniq_e)
    rid = f"gen-{hole_pos}-{deontic[:4]}-{kind}-{shape}-{i}"
    rule = dsl.Rule(
        rule_id=rid, when=when, then=then,
        action="monitor" if kind == "window" else "recommend",
        target=action.replace("_", " "),
        deontic=deontic,
        strength="suggest (conditional)" if deontic == "permission" else "strong",
        source_page=1,
        source_span=("This option is offered." if deontic == "permission"
                     else "This action is required."))
    return rule, reg


def build(verbose: bool = False):
    """Sweep the grid.  Returns (rules, stats)."""
    out, rejected, cells = [], [], {}
    for hole_pos, deontic, kind, shape in product(HOLE_POS, DEONTIC, KIND, SHAPE):
        if kind == "boolean" and shape != "single":
            pass                      # still valid; boolean atom + second fact
        key = (hole_pos, deontic, kind, shape)
        cells[key] = 0
        for i in range(2):            # two instances per cell
            try:
                rule, reg = _build_one(hole_pos, deontic, kind, shape, i)
            except Exception as exc:
                rejected.append((key, i, f"build error: {exc}"))
                continue
            issues = dsl.validate_rule(rule, reg)
            if not [x for x in issues if x.severity == "error"]:
                issues = issues + dsl.check_nondegenerate(rule, reg)
            errors = [x for x in issues if x.severity == "error"]
            if errors:
                rejected.append((key, i, ",".join(sorted({x.code for x in errors}))))
                continue

            env = dsl.make_env(reg)
            names = sorted(_mentioned(rule, reg))
            out.append(LoadedRule(
                rule_id=rule.rule_id, title=rule.target,
                category="generated", drug=rule.target,
                predicate=dsl.rule_to_z3(rule, env),
                violation=dsl.violation_condition(rule, env),
                deontic=dsl.deontic_of(rule),
                applies=dsl.to_z3(rule.when, env),
                taken=dsl.to_z3(rule.then, env),
                var_refs={n: env[n] for n in names if n in env},
                source_sentence=rule.source_span or "",
                source=f"generated · {rule.rule_id}",
                domain=list(dsl.domain_axioms(reg, env)),
                holes=dsl.open_holes(rule, reg),
                strength=rule.strength,
                variables=tuple(names)))
            cells[key] += 1

    stats = {"built": len(out), "rejected": len(rejected),
             "cells": cells, "rejections": rejected}
    if verbose:
        print(f"generated {stats['built']} rules, "
              f"{stats['rejected']} candidates rejected by the real validator")
        for key, i, why in rejected[:12]:
            print(f"  rejected {key} #{i}: {why}")
    return out, stats


if __name__ == "__main__":
    rules, stats = build(verbose=True)
    print()
    print("cell,count")
    for key, n in sorted(stats["cells"].items()):
        print(f"{'-'.join(key)},{n}")
    print()
    hole = sum(1 for r in rules if r.holes)
    perm = sum(1 for r in rules if r.deontic == "permission")
    print(f"rules_with_a_hole,{hole}")
    print(f"rules_permissive,{perm}")
