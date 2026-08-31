"""Flask API for the Track B checker: GET /rules, POST /check, GET / (ui.html).
See CHECKER.md for the endpoints, the verdict semantics, and how the
counterexample is minimized."""

import os
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from z3 import Not, Solver, is_bool, is_int, sat, unsat

from real_rules import DOMAIN_AXIOMS, REAL_RULES

# A Track A bundle, when one is present, is served alongside the fixtures.
# Its rules carry their OWN domain axioms (from the bundle's registry), which
# is why `_pin` reads `rule.domain` rather than the module-level constant --
# a global axiom set only works while every rule comes from one hand-written
# file.
BUNDLE_PATH = os.environ.get("GF_BUNDLE", str(Path(__file__).parent / "bundle.json"))
LOADED_RULES, LOADED_REFUSALS, LOADED_REGISTRY = [], [], None
if Path(BUNDLE_PATH).exists():
    try:
        from from_dsl import load_bundle
        (LOADED_RULES, LOADED_REFUSALS,
         LOADED_REGISTRY, LOADED_REJECTED) = load_bundle(BUNDLE_PATH)
    except Exception as exc:            # a bad bundle must not take the app down
        print(f"[warn] could not load bundle {BUNDLE_PATH}: {exc}")

ALL_RULES = list(REAL_RULES) + list(LOADED_RULES)

app = Flask(__name__)

APP_DIR = Path(__file__).parent
RULES_BY_ID = {rule.rule_id: rule for rule in ALL_RULES}


def _python_value(ref, model):
    value = model.eval(ref, model_completion=True)
    if is_bool(ref):
        return bool(value)
    if is_int(ref):
        return value.as_long()
    return float(value.as_fraction())


def _coerce(ref, value):
    if is_bool(ref):
        return bool(value)
    if is_int(ref):
        return int(value)
    return float(value)


def _pin(rule, known: dict) -> tuple:
    # Track A rules bring their own axioms; fixtures fall back to the global set.
    domain = list(getattr(rule, "domain", None) or DOMAIN_AXIOMS)
    provided = {}
    for name, ref in rule.var_refs.items():
        if name in known:
            coerced = _coerce(ref, known[name])
            domain.append(ref == coerced)
            provided[name] = coerced
    free_names = [name for name in rule.var_refs if name not in provided]
    return domain, provided, free_names


def classify(domain: list, predicate) -> str:
    compliant = Solver()
    compliant.add(*domain, predicate)
    violating = Solver()
    violating.add(*domain, Not(predicate))
    if compliant.check() != sat:
        return "MUST-violate"
    if violating.check() != sat:
        return "CERTIFIED-compliant"
    return "MAY-violate"


def classify_option(domain: list, applies, taken) -> str:
    """The verdict lattice for a PERMISSION.

    A permitted action cannot be violated, so compliance is the wrong question
    and the three-valued verdict does not apply. The useful question is
    applicability: does this patient meet the conditions under which the option
    exists, and was it taken? None of these four outcomes is an accusation --
    that is the point, and it is guaranteed by construction rather than by
    rewriting a verdict string after the fact."""
    def sat_(*claims) -> bool:
        s = Solver()
        s.add(*domain, *claims)
        return s.check() == sat

    if not sat_(applies):
        return "OPTION-not-applicable"
    if sat_(Not(applies)):
        return "OPTION-available"          # applicability itself undetermined
    if not sat_(Not(taken)):
        return "OPTION-taken"
    if not sat_(taken):
        return "OPTION-not-taken"
    return "OPTION-available"


def minimal_counterexample_core(rule, domain: list, free_names: list) -> dict:
    """The smallest set of free-variable values that forces a violation.

    Same assumption-dropping minimization as `tests/benchmark.py`'s function of
    the same name, generalized from generic drug/monitor indices to named
    fixture variables.
    """
    witness_solver = Solver()
    witness_solver.add(*domain, Not(rule.predicate))
    if witness_solver.check() != sat:
        return {}

    model = witness_solver.model()
    latent = []
    for name in sorted(free_names):
        ref = rule.var_refs[name]
        value = model.eval(ref, model_completion=True)
        latent.append((name, ref == value, _python_value(ref, model)))

    compliance_solver = Solver()
    compliance_solver.add(*domain, rule.predicate)
    core = latent[:]
    for candidate in latent:
        reduced = [item for item in core if item[0] != candidate[0]]
        assumptions = [literal for _, literal, _ in reduced]
        if compliance_solver.check(*assumptions) == unsat:
            core = reduced
    return {name: value for name, _, value in core}


def check_rule(rule, known: dict) -> dict:
    domain, provided, free_names = _pin(rule, known)
    # Deontic force selects the QUESTION, not just the wording of the answer.
    # An obligation is judged by compliance; a permission by applicability. The
    # DSL carries the operator (dsl.Rule.deontic), so this is a dispatch on the
    # rule's declared meaning rather than a guard bolted onto a verdict string.
    permissive = bool(getattr(rule, "is_permissive", False))
    if permissive and rule.applies is not None:
        verdict = classify_option(domain, rule.applies, rule.taken)
    else:
        verdict = classify(domain, rule.predicate)
    counterexample = (
        minimal_counterexample_core(rule, domain, free_names)
        if verdict == "MAY-violate"
        else {}
    )
    holes = list(getattr(rule, "holes", ()) or ())
    return {
        "rule_id": rule.rule_id,
        "title": rule.title,
        "category": rule.category,
        "drug": rule.drug,
        "verdict": verdict,
        # Named so a consumer can tell "the guideline required this and it did
        # not happen" from "the guideline offered this and it was not taken".
        "deontic": "permission" if permissive else "obligation",
        "counterexample_core": counterexample,
        "source_sentence": rule.source_sentence,
        "known_variables": provided,
        "free_variables": sorted(free_names),
        # A PARTIAL encoding's holes are unbound BY DESIGN -- predicates the
        # source states but never defines. Naming them separates "we do not
        # know this patient's value" from "the guideline never said".
        "open_holes": holes,
        "partial": bool(holes),
        "source_page": getattr(rule, "source_page", None),
        "provenance": getattr(rule, "source_span", None),
        "origin": "track-a" if hasattr(rule, "domain") else "fixture",
    }


@app.get("/")
def ui():
    return send_from_directory(APP_DIR, "ui.html")


@app.get("/rules")
def list_rules():
    return jsonify(
        [
            {
                "rule_id": rule.rule_id,
                "title": rule.title,
                "category": rule.category,
                "drug": rule.drug,
                "source": rule.source,
                "source_sentence": rule.source_sentence,
                "variables": sorted(rule.var_refs.keys()),
            }
            for rule in ALL_RULES
        ]
    )


@app.post("/check")
def check():
    body = request.get_json(force=True, silent=True) or {}
    patient = body.get("patient", {})
    plan = body.get("plan", {})
    if not isinstance(patient, dict) or not isinstance(plan, dict):
        return jsonify({"error": "'patient' and 'plan' must be JSON objects"}), 400

    known = {**patient, **plan}
    rule_id = body.get("rule_id")

    try:
        if rule_id is not None:
            rule = RULES_BY_ID.get(rule_id)
            if rule is None:
                return jsonify({"error": f"unknown rule_id {rule_id!r}"}), 404
            return jsonify(check_rule(rule, known))

        touched_rules = [
            rule for rule in ALL_RULES if set(rule.var_refs) & set(known)
        ]
    except (TypeError, ValueError) as error:
        return jsonify({"error": f"bad patient/plan value: {error}"}), 400

    if not touched_rules:
        return (
            jsonify(
                {
                    "error": (
                        "no rule variables matched 'patient'/'plan' keys; "
                        "GET /rules to see available variable names"
                    )
                }
            ),
            400,
        )

    try:
        return jsonify([check_rule(rule, known) for rule in touched_rules])
    except (TypeError, ValueError) as error:
        return jsonify({"error": f"bad patient/plan value: {error}"}), 400


@app.get("/residue")
def residue():
    """What Track A REFUSED to encode, and why.

    A checker that serves only the rules that compiled tells a governance
    reader the tool covered everything. This route is the other half of the
    deliverable, and it is why the bundle carries refusals alongside rules."""
    from from_dsl import residue_report
    return jsonify(residue_report(LOADED_REFUSALS))


@app.get("/health")
def health():
    return jsonify({"status": "ok",
                    "rules_loaded": len(ALL_RULES),
                    "fixture_rules": len(REAL_RULES),
                    "track_a_rules": len(LOADED_RULES),
                    "refused_statements": len(LOADED_REFUSALS),
                    "bundle": BUNDLE_PATH if LOADED_RULES else None})


if __name__ == "__main__":
    app.run(port=8000, debug=False)
