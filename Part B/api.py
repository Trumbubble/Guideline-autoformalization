"""Flask API for the Track B checker: dashboard + per-guideline check.
See CHECKER.md for the endpoints, the verdict semantics, and how the
counterexample is minimized."""

from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from z3 import Not, Solver, is_bool, is_int, sat, unsat

import guidelines
from real_rules import DOMAIN_AXIOMS as FIXTURE_DOMAIN_AXIOMS

# The single-bundle GF_BUNDLE/BUNDLE_PATH loading this replaced is now just
# one entry in guidelines.py's library (whichever bundle a browser upload
# produced) -- every route below is scoped to a guideline_id instead of one
# process-global bundle. `_pin` reading `rule.domain` before falling back to
# the fixtures' shared axioms is the same idea, generalized: a Track A rule
# carries its own bundle's domain axioms; a fixture SpecClause has none of
# its own, so it falls back to FIXTURE_DOMAIN_AXIOMS.

app = Flask(__name__)
APP_DIR = Path(__file__).parent


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
    # Track A rules bring their own per-rule axioms (their own registry's
    # plausible ranges); fixtures fall back to the fixture module's shared
    # DOMAIN_AXIOMS, since a SpecClause carries no `.domain` of its own.
    domain = list(getattr(rule, "domain", None) or FIXTURE_DOMAIN_AXIOMS)
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

    Same assumption-dropping minimization as `tests/benchmark.py`'s function
    of the same name, generalized from generic drug/monitor indices to named
    fixture (or Track A) variables.
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


# ---------------------------------------------------------------- pages

@app.get("/")
def dashboard():
    return send_from_directory(APP_DIR, "dashboard.html")


@app.get("/check")
def checker_page():
    return send_from_directory(APP_DIR, "ui.html")


@app.get("/demo")
def demo_page():
    return send_from_directory(APP_DIR, "demo.html")


# ---------------------------------------------------------------- guidelines

@app.get("/settings")
def settings():
    # Never echoes the key itself -- only whether one is already configured,
    # so the UI can tell a visitor whether pasting their own is necessary.
    return jsonify({"server_key_configured": guidelines.server_has_api_key()})


@app.get("/guidelines")
def list_guidelines():
    return jsonify(guidelines.list_guidelines())


@app.post("/guidelines")
def upload_guideline():
    is_json = request.is_json
    json_body = request.get_json(silent=True) if is_json else None
    json_body = json_body or {}

    title = request.form.get("title") or json_body.get("title")
    live_raw = request.form.get("live") if not is_json else json_body.get("live")
    live = str(live_raw if live_raw is not None else "").lower() in ("1", "true", "on")
    # A key typed into the browser overrides the server's own .env for this
    # upload only -- see guidelines._temporary_api_key. Blank means "use
    # whatever the server already has configured".
    api_key = (request.form.get("api_key") or json_body.get("api_key") or "").strip() or None

    file_bytes = filename = text = None
    if "file" in request.files and request.files["file"].filename:
        upload = request.files["file"]
        filename = upload.filename
        file_bytes = upload.read()
        title = title or Path(filename).stem
    elif is_json:
        text = json_body.get("text")
    else:
        text = request.form.get("text")

    if not title:
        return jsonify({"error": "'title' is required"}), 400
    if not (file_bytes or text):
        return jsonify({"error": "provide a 'file' upload or 'text'"}), 400

    try:
        entry = guidelines.ingest(
            title, file_bytes=file_bytes, filename=filename, text=text,
            live=live, api_key=api_key,
        )
    except guidelines.IngestError as error:
        return jsonify({"error": str(error)}), 400
    except Exception as error:  # a bad upload must not take the app down
        return jsonify({"error": f"ingestion failed: {error}"}), 502

    return jsonify(entry), 201


@app.get("/guidelines/<guideline_id>")
def guideline_detail(guideline_id):
    found = guidelines.get_guideline(guideline_id)
    if found is None:
        return jsonify({"error": f"unknown guideline {guideline_id!r}"}), 404
    rules, refusals, reg, meta = found
    return jsonify(
        dict(
            meta,
            rules=[
                {
                    "rule_id": r.rule_id,
                    "title": r.title,
                    "category": r.category,
                    "drug": r.drug,
                    "source": getattr(r, "source", ""),
                    "source_sentence": r.source_sentence,
                    "variables": sorted(r.var_refs.keys()),
                    "partial": bool(getattr(r, "holes", ())),
                }
                for r in rules
            ],
        )
    )


@app.get("/guidelines/<guideline_id>/rules")
def guideline_rules(guideline_id):
    found = guidelines.get_guideline(guideline_id)
    if found is None:
        return jsonify({"error": f"unknown guideline {guideline_id!r}"}), 404
    rules, _refusals, _reg, _meta = found
    return jsonify(
        [
            {
                "rule_id": r.rule_id,
                "title": r.title,
                "category": r.category,
                "drug": r.drug,
                "source": getattr(r, "source", ""),
                "source_sentence": r.source_sentence,
                "variables": sorted(r.var_refs.keys()),
            }
            for r in rules
        ]
    )


@app.get("/guidelines/<guideline_id>/residue")
def guideline_residue(guideline_id):
    """What Track A REFUSED to encode for this guideline, and why.

    A checker that serves only the rules that compiled tells a governance
    reader the tool covered everything. This route is the other half of the
    deliverable."""
    found = guidelines.get_guideline(guideline_id)
    if found is None:
        return jsonify({"error": f"unknown guideline {guideline_id!r}"}), 404
    _rules, refusals, _reg, _meta = found
    from from_dsl import residue_report

    return jsonify(residue_report(refusals))


@app.post("/guidelines/<guideline_id>/check")
def guideline_check(guideline_id):
    found = guidelines.get_guideline(guideline_id)
    if found is None:
        return jsonify({"error": f"unknown guideline {guideline_id!r}"}), 404
    rules, _refusals, _reg, _meta = found
    rules_by_id = {r.rule_id: r for r in rules}

    body = request.get_json(force=True, silent=True) or {}
    patient = body.get("patient", {})
    plan = body.get("plan", {})
    if not isinstance(patient, dict) or not isinstance(plan, dict):
        return jsonify({"error": "'patient' and 'plan' must be JSON objects"}), 400

    known = {**patient, **plan}
    rule_id = body.get("rule_id")

    try:
        if rule_id is not None:
            rule = rules_by_id.get(rule_id)
            if rule is None:
                return jsonify({"error": f"unknown rule_id {rule_id!r}"}), 404
            return jsonify(check_rule(rule, known))

        touched_rules = [r for r in rules if set(r.var_refs) & set(known)]
    except (TypeError, ValueError) as error:
        return jsonify({"error": f"bad patient/plan value: {error}"}), 400

    if not touched_rules:
        return (
            jsonify(
                {
                    "error": (
                        "no rule variables matched 'patient'/'plan' keys; "
                        f"GET /guidelines/{guideline_id}/rules to see available "
                        "variable names"
                    )
                }
            ),
            400,
        )

    try:
        return jsonify([check_rule(r, known) for r in touched_rules])
    except (TypeError, ValueError) as error:
        return jsonify({"error": f"bad patient/plan value: {error}"}), 400


# --------------------------------------------------- back-compat: fixtures

@app.get("/rules")
def rules_alias():
    return guideline_rules(guidelines.FIXTURES_ID)


@app.post("/check")
def check_alias():
    return guideline_check(guidelines.FIXTURES_ID)


@app.get("/health")
def health():
    all_guidelines = guidelines.list_guidelines()
    return jsonify(
        {
            "status": "ok",
            "guidelines": len(all_guidelines),
            "total_rules": sum(g["rule_count"] for g in all_guidelines),
        }
    )


if __name__ == "__main__":
    app.run(port=8000, debug=False)
