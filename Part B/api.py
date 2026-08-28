"""Flask API for the Track B checker: GET /rules, POST /check, GET / (ui.html).
See CHECKER.md for the endpoints, the verdict semantics, and how the
counterexample is minimized."""

from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from z3 import Not, Solver, is_bool, is_int, sat, unsat

from real_rules import DOMAIN_AXIOMS, REAL_RULES

app = Flask(__name__)

APP_DIR = Path(__file__).parent
RULES_BY_ID = {rule.rule_id: rule for rule in REAL_RULES}


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
    domain = list(DOMAIN_AXIOMS)
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
    verdict = classify(domain, rule.predicate)
    counterexample = (
        minimal_counterexample_core(rule, domain, free_names)
        if verdict == "MAY-violate"
        else {}
    )
    return {
        "rule_id": rule.rule_id,
        "title": rule.title,
        "category": rule.category,
        "drug": rule.drug,
        "verdict": verdict,
        "counterexample_core": counterexample,
        "source_sentence": rule.source_sentence,
        "known_variables": provided,
        "free_variables": sorted(free_names),
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
            for rule in REAL_RULES
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
            rule for rule in REAL_RULES if set(rule.var_refs) & set(known)
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


@app.get("/health")
def health():
    return jsonify({"status": "ok", "rules_loaded": len(REAL_RULES)})


if __name__ == "__main__":
    app.run(port=8000, debug=False)
