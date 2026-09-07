"""
Differential check between the DSL's two lowerings.

One `dsl.Rule` is lowered independently to a Z3 formula and a CQL library and
both are asked about the same patient; a disagreement means one lowering is
wrong, which needs no ground truth, clinician, or reference artifact to
detect. The interesting case is Z3's MAY-violate against CQL's null
Violated -- two different mechanisms (a free variable vs. a propagated null)
that should still land in the same place.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass

import z3

import cql
import dsl
import toolchain


@dataclass
class Comparison:
    label: str
    assignment: dict
    z3_verdict: str
    cql_violated: object          # True | False | None
    agree: bool
    note: str = ""


Z3_TO_CQL = {"MUST-violate": True, "CERTIFIED-compliant": False,
             "MAY-violate": None}


def z3_verdict(rule: dsl.Rule, reg: dsl.Registry, assignment: dict) -> str:
    """Three-valued verdict for a partially-known patient."""
    env = dsl.make_env(reg)
    phi = dsl.rule_to_z3(rule, env)
    pinned = []
    for name, value in assignment.items():
        if name not in env:
            continue
        ref = env[name]
        pinned.append(ref == (z3.BoolVal(bool(value)) if z3.is_bool(ref)
                              else z3.RealVal(float(value)) if z3.is_real(ref)
                              else z3.IntVal(int(value))))
    base = dsl.domain_axioms(reg, env) + pinned
    sat_ = z3.Solver(); sat_.add(base); sat_.add(phi)
    vio = z3.Solver(); vio.add(base); vio.add(z3.Not(phi))
    if sat_.check() != z3.sat:
        return "MUST-violate"
    if vio.check() != z3.sat:
        return "CERTIFIED-compliant"
    return "MAY-violate"


def crosscheck(rule: dsl.Rule, reg: dsl.Registry, *,
               library_name: str = "Crosscheck") -> list[Comparison]:
    """Build patients from the compiled artifact, then ask both engines."""
    lib = cql.rule_to_cql(rule, reg, library_name=library_name)
    out: list[Comparison] = []
    with tempfile.TemporaryDirectory() as d:
        elm = os.path.join(d, "lib.json")
        errs = toolchain.translate_to_file(lib.text, elm)
        if errs:
            raise RuntimeError(f"library did not compile: {errs[0]}")
        vs = os.path.join(d, "vs.json")
        toolchain.expand_valuesets(elm, vs)

        with open(elm, encoding="utf-8") as fh:
            elm_lib = json.load(fh)["library"]
        terms = [n for _t, n in toolchain._retrieves(elm_lib)]
        ths = toolchain.thresholds(elm)
        actions = {n for n in reg.names() if reg[n].role == "action"}

        for label, satisfy_threshold, include_actions in [
            ("threshold met, action absent", True, False),
            ("threshold met, action present", True, True),
            ("threshold not met, action absent", False, False),
        ]:
            quantities, assignment = {}, {}
            for th in ths:
                sat_v, vio_v = toolchain.boundary_values(th)
                v = sat_v if satisfy_threshold else vio_v
                quantities[th["subject"]] = (v, th["unit"])
                var = _var_for_define(th["subject"], reg)
                if var:
                    assignment[var] = v

            include = set(terms)
            if not include_actions:
                include = {t for t in terms
                           if not _is_action_term(t, reg, actions)}
            for n in reg.names():
                vd = reg[n]
                if vd.unbound:
                    continue                       # a hole stays UNKNOWN on both sides
                if vd.type == "bool":
                    present = _term_for_var(n, reg) in include if terms else False
                    assignment[n] = present

            pt = os.path.join(d, f"{len(out)}.json")
            toolchain.synthetic_bundle(elm, vs, pt, patient_id=f"p{len(out)}",
                                       quantities=quantities, include=include)
            # A value set that expanded to nothing makes every action look
            # absent, which reads as a lowering disagreement and is not one.
            empty_vs = [k for k, versions in json.load(
                open(vs, encoding="utf-8")).items()
                if not any(versions.values())]
            res = toolchain.execute(elm, pt, vs)["results"]
            vals = next(iter(res.values()), {})
            cql_v = vals.get("Violated")

            zv = z3_verdict(rule, reg, assignment)
            expected = Z3_TO_CQL.get(zv, "?")
            agree = (cql_v is expected) if expected is not None else (cql_v is None)
            note = ""
            if not agree and empty_vs:
                note = (f"INCONCLUSIVE: {len(empty_vs)} value set(s) expanded to "
                        f"zero codes, so no patient can carry the action")
            out.append(Comparison(label, assignment, zv, cql_v, agree, note))
    return out


def _var_for_define(define_name: str, reg: dsl.Registry) -> str | None:
    for n in reg.names():
        if (reg[n].description or n).strip().lower() == define_name.strip().lower():
            return n
    return None


def _term_for_var(var: str, reg: dsl.Registry) -> str:
    return (reg[var].description or var) + " codes"


def _is_action_term(term: str, reg: dsl.Registry, actions: set) -> bool:
    base = term[:-6].strip() if term.endswith(" codes") else term
    for a in actions:
        if (reg[a].description or a).strip().lower() == base.lower():
            return True
    return False


def report(comparisons: list[Comparison]) -> str:
    lines = [f"{'patient':34s} {'Z3':22s} {'CQL Violated':14s} agree"]
    for c in comparisons:
        # A disagreement caused by an empty value set is not evidence about the
        # lowerings, and calling it a LOWERING BUG sends a reader to debug the
        # wrong component -- the misattribution this whole project is about.
        verdict = ("YES" if c.agree else
                   "NO  <-- INCONCLUSIVE" if c.note else
                   "NO  <-- LOWERING BUG")
        lines.append(f"{c.label:34s} {c.z3_verdict:22s} {str(c.cql_violated):14s} "
                     f"{verdict}")
        if c.note:
            lines.append(f"{'':34s} {c.note}")
    n_ok = sum(1 for c in comparisons if c.agree)
    n_incon = sum(1 for c in comparisons if not c.agree and c.note)
    n_bug = len(comparisons) - n_ok - n_incon
    lines.append(f"\n{n_ok}/{len(comparisons)} agree"
                 + (f", {n_incon} inconclusive" if n_incon else "")
                 + (f", {n_bug} LOWERING BUG(S)" if n_bug else ""))
    return "\n".join(lines)


if __name__ == "__main__":
    reg = dsl.Registry([
        dsl.VarDef("egfr", "real", "mL/min", "eGFR", plausible=(0, 200),
                   binding="http://loinc.org|69405-9|2.83"),
        dsl.VarDef("on_acei", "bool", None, "ACE inhibitors", role="action",
                   binding="http://cts.nlm.nih.gov/fhir/ValueSet|"
                           "2.16.840.1.113762.1.4.1078.9|20241228"),
    ])
    plain = dsl.Rule("plain", dsl.Cmp("egfr", "<", 30),
                     dsl.Cmp("on_acei", "==", True), "recommend", "ACEi",
                     source_span="s")
    with_hole = dsl.Rule("hole",
                         dsl.Or([dsl.Cmp("egfr", "<", 30),
                                 dsl.Cmp("has_high_risk", "==", True)]),
                         dsl.Cmp("on_acei", "==", True), "recommend", "ACEi",
                         source_span="s")
    reg_hole = dsl.Registry([reg[n] for n in reg.names()]
                            + [dsl.VarDef("has_high_risk", "bool", None,
                                          "at high risk", unbound=True)])
    for name, r, g in [("FULLY BOUND RULE", plain, reg), ("PARTIAL RULE (a hole)", with_hole, reg_hole)]:
        print(f"\n=== {name} ===")
        print(report(crosscheck(r, g, library_name=f"X{r.rule_id}")))
