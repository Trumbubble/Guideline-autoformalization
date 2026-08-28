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
    threshold: tuple = ()
    variables: tuple = ()
    boundaries: tuple = ()

    @property
    def is_partial(self) -> bool:
        return bool(self.holes)


# `action` is a UI gloss in the DSL, but the checker groups by category, so map
# it to the vocabulary `real_rules.py` already uses.
ACTION_CATEGORY = {
    "recommend": "recommendation", "avoid": "contraindication",
    "hold": "contraindication", "monitor": "monitoring",
    "refer": "referral", "screen": "screening",
}


def load_bundle(path: str) -> tuple[list[LoadedRule], list, "dsl.Registry"]:
    """bundle.json -> (rules the checker can run, refusals, registry)."""
    reg, rules, refusals = dsl.read_bundle(path)
    env = dsl.make_env(reg)
    axioms = dsl.domain_axioms(reg, env)

    loaded = []
    for r in rules:
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
    return loaded, refusals, reg


def _mentioned(rule: "dsl.Rule", reg: "dsl.Registry") -> set:
    """Variable and event-derived names the rule actually refers to."""
    out: set = set()

    def walk(e):
        if isinstance(e, (dsl.Cmp, dsl.Between)):
            out.add(e.var)
        elif isinstance(e, dsl.Since):
            out.add(dsl.occurred_var(e.event))
            out.add(dsl.days_since_var(e.event))
        elif isinstance(e, dsl.Not):
            walk(e.operand)
        elif isinstance(e, (dsl.And, dsl.Or)):
            for o in e.operands:
                walk(o)

    walk(rule.when)
    walk(rule.then)
    return out


def residue_report(refusals) -> dict:
    """The governance half of the deliverable: what was NOT encoded, and why."""
    by_reason: dict = {}
    for r in refusals:
        by_reason.setdefault(r.reason_code, []).append({
            "rule_id": r.rule_id, "reason": r.reason_text,
            "source_span": r.source_span,
            "undefined_predicates": list(getattr(r, "undefined_predicates", ()) or ()),
        })
    return {"total": len(refusals),
            "by_reason": {k: len(v) for k, v in sorted(by_reason.items())},
            "detail": by_reason}


if __name__ == "__main__":
    import json
    path = sys.argv[1] if len(sys.argv) > 1 else "bundle.json"
    rules, refusals, reg = load_bundle(path)
    print(f"{len(rules)} rules, {len(refusals)} refusals, "
          f"{len(reg.names())} registry variables\n")
    for r in rules:
        flag = f"  PARTIAL holes={list(r.holes)}" if r.is_partial else ""
        print(f"  [{r.category:15s}] {r.rule_id:14s} {r.title[:46]}{flag}")
        print(f"      vars: {', '.join(r.variables)}")
    if refusals:
        print("\nresidue:", json.dumps(residue_report(refusals)["by_reason"]))
