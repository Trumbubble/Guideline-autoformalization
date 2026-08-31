"""Load a Track A bundle and expose it in the shape the checker already uses.

THE INTERFACE. Track A writes a JSON bundle -- registry + rules + refusals --
and Track B reads it. Neither side imports the other's internals; the contract
is the file. That is what the DEV_PLAN meant by "the tracks share one
interface, the typed intermediate DSL and its JSON serialization".

WHY A REGISTRY AND NOT JUST RULES. A rule is a set of names. Without the
registry those names have no type, no unit, no terminology binding and no
plausible range, so a consumer cannot build a solver environment and cannot
assert domain axioms. `real_rules.py` hard-codes both together because it is a
fixture; a real bundle has to carry them.

WHY REFUSALS TRAVEL WITH THE RULES. A consumer that sees only what compiled
cannot report the residue, and the residue is a deliverable, not a log.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field

_PART_A = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "Part A")
if _PART_A not in sys.path:
    sys.path.insert(0, _PART_A)

import dsl  # noqa: E402


@dataclass
class LoadedRule:
    """A Track A rule, wearing the same attributes `api.check_rule` reads off a
    fixture `SpecClause`. Duck-typed on purpose: the checker does not need to
    know where a rule came from."""
    rule_id: str
    title: str
    category: str
    drug: str
    predicate: object                    # z3 BoolRef -- phi = when -> then
    var_refs: dict                       # name -> z3 ref
    source_sentence: str
    domain: list = field(default_factory=list)   # per-rule, not global
    holes: tuple = ()                    # unbound predicates, left free
    # `source` is the citation the fixtures carry ("BNF / NICE guidance"); for a
    # Track A rule the citation IS the provenance, so /rules renders both kinds
    # without special-casing.
    source: str = ""
    source_page: int | None = None
    source_span: str | None = None
    strength: str | None = None
    deontic: str = "obligation"
    violation: object = None             # z3 BoolRef -- when the rule is broken
    applies: object = None               # z3 BoolRef -- `when`
    taken: object = None                 # z3 BoolRef -- `then`
    threshold: tuple = ()
    variables: tuple = ()
    boundaries: tuple = ()

    @property
    def is_partial(self) -> bool:
        return bool(self.holes)

    @property
    def is_permissive(self) -> bool:
        """The source permits the action rather than requiring it. Such a rule
        may inform, but it must never accuse."""
        return self.deontic == "permission"


# `action` is a UI gloss in the DSL, but the checker groups by category, so map
# it to the vocabulary `real_rules.py` already uses.
ACTION_CATEGORY = {
    "recommend": "recommendation", "avoid": "contraindication",
    "hold": "contraindication", "monitor": "monitoring",
    "refer": "referral", "screen": "screening",
}


def load_bundle(path: str) -> tuple[list[LoadedRule], list, "dsl.Registry", list]:
    """bundle.json -> (rules the checker can run, refusals, registry, rejected).

    Track B re-verifies before it loads. A bundle is a file: it can be written
    by an older Track A, hand-edited, or produced by a formalizer whose checks
    have since been strengthened, so trusting it because it parsed is the same
    mistake as trusting a rule because it typechecked.

    The rejection that matters is VACUOUS_RULE. A rule that no patient can
    violate does not fail loudly -- it reports CERTIFIED-compliant for
    everyone, which is indistinguishable from a rule that is working. Measured
    on PMC13270348, one of two shipped rules was of this kind (RESULTS.md R14).
    Rejected rules are returned SEPARATELY from the guideline's refusals: they
    are our defect, not the document's residue, and folding them together
    would corrupt the one number the residue report exists to make readable."""
    reg, rules, refusals = dsl.read_bundle(path)
    env = dsl.make_env(reg)
    axioms = dsl.domain_axioms(reg, env)
    for src, dst in dsl.canonical_aliases(reg).items():
        env[src] = env[dst]

    loaded, rejected = [], []
    for r in rules:
        defects = [i for i in dsl.validate_rule(r, reg) + dsl.check_nondegenerate(r, reg)
                   if i.severity == "error"]
        if defects:
            rejected.append({
                "rule_id": r.rule_id,
                "codes": sorted({i.code for i in defects}),
                "why": defects[0].message,
                "source_span": r.source_span or "",
            })
            continue
        # Only the variables THIS rule mentions are exposed as var_refs: the
        # checker pins what the caller supplies and leaves the rest free, and
        # offering unrelated variables would inflate the free-variable space
        # the counterexample minimiser has to search.
        names = _mentioned(r, reg)
        loaded.append(LoadedRule(
            rule_id=r.rule_id,
            title=f"{r.action} {r.target}".strip(),
            category=ACTION_CATEGORY.get(r.action, r.action),
            drug=r.target,
            predicate=dsl.rule_to_z3(r, env),
            # What it takes to VIOLATE the rule. For a permission this is
            # FALSE unconditionally, so no patient and no completion of the
            # unknowns can yield MUST-violate -- the guarantee is structural,
            # not a guard on the verdict string.
            violation=dsl.violation_condition(r, env),
            deontic=dsl.deontic_of(r),
            # Kept separately because a PERMISSION is not judged by compliance
            # but by applicability: does this patient meet the conditions under
            # which the option exists, and was it taken?
            applies=dsl.to_z3(r.when, env),
            taken=dsl.to_z3(r.then, env),
            var_refs={n: env[n] for n in names if n in env},
            source_sentence=r.source_span or "",
            source=f"Track A · {r.rule_id}"
                   + (f" · page {r.source_page}" if r.source_page else ""),
            domain=list(axioms),
            holes=dsl.open_holes(r, reg),
            source_page=r.source_page, source_span=r.source_span,
            strength=r.strength,
            variables=tuple(sorted(names)),
        ))
    # A guideline that permits an action one rule requires be absent is
    # contradicting itself. Reported, never resolved: picking a side is a
    # committee's call, not a compiler's.
    for c in dsl.conflicts(rules, reg):
        rejected.append({"rule_id": f"{c['permits']} vs {c['requires']}",
                         "codes": ["DEONTIC_CONFLICT"], "why": c["detail"],
                         "source_span": ", ".join(c["shared_variables"])})
    return loaded, refusals, reg, rejected


def _mentioned(rule: "dsl.Rule", reg: "dsl.Registry") -> set:
    """Variable and event-derived names the rule actually refers to."""
    out: set = set()

    def walk(e):
        if isinstance(e, (dsl.Cmp, dsl.Between)):
            out.add(e.var)
        elif isinstance(e, dsl.Since):
            out.add(dsl.occurred_var(e.event))
            out.add(dsl.days_since_var(e.event))
        elif isinstance(e, dsl.Delta):
            # Both the current value and the baseline, so a caller can pin the
            # prior dose and get a real verdict instead of MAY-violate.
            out.add(e.var)
            out.add(dsl.baseline_var(e.var, e.baseline))
        elif isinstance(e, dsl.Not):
            walk(e.operand)
        elif isinstance(e, (dsl.And, dsl.Or)):
            for o in e.operands:
                walk(o)

    walk(rule.when)
    walk(rule.then)
    return out


# Evidence lines are tagged by where the signal came from. Only the binder ones
# are OUR failure; the rest are properties of the statement.
# Evidence tags that make a refusal OURS rather than the guideline's.
# TERMINOLOGY_UNAVAILABLE is the strongest of them: the lookup never reached the
# server, so the refusal carries no information about the document at all.
_OUR_FAULT = ("unbound_index_gap", "unbound_unselected", "NO_BOUND_VOCABULARY",
              "TERMINOLOGY_UNAVAILABLE")


def residue_report(refusals) -> dict:
    """The governance half of the deliverable: what was NOT encoded, and why.

    Each refusal carries the concept-level evidence beneath it, and the report
    separates refusals ATTRIBUTABLE TO RETRIEVAL from those that are properties
    of the guideline. A reader who cannot make that split cannot tell whether to
    fix the index or accept the residue -- and a formalizability fraction quoted
    without it is not interpretable.
    """
    by_reason: dict = {}
    retrieval = 0
    for r in refusals:
        ev = list(getattr(r, "evidence", ()) or ())
        ours = any(tag in line for line in ev for tag in _OUR_FAULT)
        retrieval += bool(ours)
        by_reason.setdefault(r.reason_code, []).append({
            "rule_id": r.rule_id,
            "reason": r.reason_text,
            "source_span": r.source_span,
            "undefined_predicates": list(getattr(r, "undefined_predicates", ()) or ()),
            "evidence": ev,
            "attributable_to_retrieval": ours,
        })
    return {"total": len(refusals),
            "by_reason": {k: len(v) for k, v in sorted(by_reason.items())},
            "attributable_to_retrieval": retrieval,
            "attributable_to_guideline": len(refusals) - retrieval,
            "detail": by_reason}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(
        description="Inspect a Track A bundle: the rules, and the residue.")
    ap.add_argument("bundle", nargs="?", default="bundle.json")
    ap.add_argument("--residue", action="store_true",
                    help="show only what was refused, and why")
    a = ap.parse_args()

    rules, refusals, reg, rejected = load_bundle(a.bundle)
    rep = residue_report(refusals)
    total = len(rules) + len(refusals) + len(rejected)

    if not a.residue:
        print(f"{len(rules)} encoded, {len(refusals)} refused, "
              f"{len(reg.names())} bound variables\n")
        for r in rules:
            flag = f"   PARTIAL holes={list(r.holes)}" if r.is_partial else ""
            print(f"  [{r.category:14s}] {r.rule_id:20s} {r.title[:40]}{flag}")
            print(f"        {(r.source_sentence or '')[:88]}")
        print()

    if rejected:
        print("=" * 74)
        print(f" REJECTED BY VERIFICATION -- {len(rejected)} rule(s) in this bundle "
              f"are unsound")
        print("=" * 74)
        print("  These are OUR defect, not the guideline's residue, and they are not")
        print("  loaded into the checker. A VACUOUS_RULE is the dangerous one: it")
        print("  reports every patient COMPLIANT instead of failing visibly.\n")
        for x in rejected:
            print(f"  [{', '.join(x['codes'])}]  {x['rule_id']}")
            print(f"     {x['why'][:86]}")
            print(f"     {x['source_span'][:86]}")
        print()

    print("=" * 74)
    print(f" RESIDUE -- {len(refusals)} of {total} statements were NOT encoded")
    print("=" * 74)
    if total:
        print(f"  coverage: {len(rules)}/{total} = {len(rules)/total:.0%}")
    print(f"  attributable to the GUIDELINE : {rep['attributable_to_guideline']}"
          "   (vague, defeasible, not patient-level -- real residue)")
    print(f"  attributable to OUR RETRIEVAL : {rep['attributable_to_retrieval']}"
          "   (a code exists and we did not find it -- fixable)")
    print(f"  by reason: {json.dumps(rep['by_reason'])}\n")

    for reason, items in sorted(rep["detail"].items()):
        for it in items:
            who = "OURS" if it["attributable_to_retrieval"] else "guideline"
            print(f"  [{reason}]  {it['rule_id']}   ({who})")
            print(f"     {(it['source_span'] or '')[:84]}")
            for p in it["undefined_predicates"]:
                print(f"     undefined predicate: {p!r}")
            for e in it["evidence"][:3]:
                print(f"     · {e[:84]}")
            print()
