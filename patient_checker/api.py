"""Flask API for the patient checker: dashboard + per-guideline check."""

from pathlib import Path
from urllib.parse import quote

from flask import Flask, Response, jsonify, request, send_from_directory
from z3 import Not, Solver, is_bool, is_int, sat, unsat

import guidelines
from real_rules import DOMAIN_AXIOMS as FIXTURE_DOMAIN_AXIOMS

# The single-bundle GF_BUNDLE/BUNDLE_PATH loading this replaced is now just
# one entry in guidelines.py's library (whichever bundle a browser upload
# produced) -- every route below is scoped to a guideline_id instead of one
# process-global bundle. `_pin` reading `rule.domain` before falling back to
# the fixtures' shared axioms is the same idea, generalized: a CQL_converter rule
# carries its own bundle's domain axioms; a fixture SpecClause has none of
# its own, so it falls back to FIXTURE_DOMAIN_AXIOMS.

app = Flask(__name__)
APP_DIR = Path(__file__).parent

# Registers demo.html's two fixed-id guidelines from their committed bundles
# on this machine's local store, if they aren't there yet -- see
# guidelines.seed_demo_guidelines. Runs on import, not just __main__, so it
# also happens under a WSGI server that imports this module directly.
guidelines.seed_demo_guidelines()


def _python_value(ref, model):
    value = model.eval(ref, model_completion=True)
    if is_bool(ref):
        return bool(value)
    if is_int(ref):
        return value.as_long()
    return float(value.as_fraction())


_FALSEY = {"false", "no", "0", "", "none", "null", "off", "absent"}

def _coerce(ref, value):
    """A supplied value, in the sort its z3 symbol expects.

    Strings are handled explicitly. `bool("false")` is True in Python, so a
    form field or a JSON string of "false" pinned a variable to TRUE and could
    flip a clinical verdict -- the same bool-shaped trap as `isinstance(True,
    int)`. A value that cannot be read is refused rather than guessed: pinning
    a patient to a number nobody supplied is worse than declining."""
    if is_bool(ref):
        if isinstance(value, str):
            return value.strip().lower() not in _FALSEY
        return bool(value)
    if isinstance(value, bool):
        # True + a numeric variable is a category error, not the number 1.
        raise ValueError(f"expected a number, got the boolean {value!r}")
    try:
        return int(value) if is_int(ref) else float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"cannot read {value!r} as a number") from exc


def _pin(rule, known: dict) -> tuple:
    # CQL_converter rules bring their own per-rule axioms (their own registry's
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
    fixture (or CQL_converter) variables.
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


def _is_permissive(rule) -> bool:
    """The one place that reads deontic force off a rule.

    It was read directly in `check_rule` and nowhere else, so the rule LISTING
    could not report it -- and the listing's `category` column is derived from
    `action`, a UI gloss, which labels a permission whose action is `recommend`
    a "recommendation". A viewer comparing the table against an OPTION-not-taken
    verdict sees the table contradict the badge. The force is the rule's, not
    the verdict's, so both must read it from here."""
    return bool(getattr(rule, "is_permissive", False))


def _deontic_of(rule) -> str:
    return "permission" if _is_permissive(rule) else "obligation"


def check_rule(rule, known: dict) -> dict:
    domain, provided, free_names = _pin(rule, known)
    # Deontic force selects the QUESTION, not just the wording of the answer.
    # An obligation is judged by compliance; a permission by applicability. The
    # DSL carries the operator (dsl.Rule.deontic), so this is a dispatch on the
    # rule's declared meaning rather than a guard bolted onto a verdict string.
    permissive = _is_permissive(rule)
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


@app.get("/demo/toy")
def demo_toy_page():
    """The single hand-authored rule, three patients, three verdicts. Kept
    because it isolates the three-valued verdict better than a real document
    can -- a real guideline's rules carry their own vocabulary and their own
    reasons for refusing -- but it is a toy, so /demo leads with the real
    documents and links here rather than the other way round."""
    return send_from_directory(APP_DIR, "demo-toy.html")


# ---------------------------------------------------------------- guidelines

@app.get("/settings")
def settings():
    # Never echoes the key itself -- only whether one is already configured,
    # so the UI can tell a visitor whether pasting their own is necessary.
    return jsonify({"server_key_configured": guidelines.server_has_api_key()})


def _client_id() -> str | None:
    """The browser's self-minted id, used to keep one visitor's uploads out
    of another's dashboard. Not authentication -- it is client-supplied and
    forgeable; see the note above guidelines._visible_to."""
    value = (request.headers.get("X-GF-Client")
             or request.form.get("client_id")
             or ((request.get_json(silent=True) or {}).get("client_id")
                 if request.is_json else None)
             or "")
    value = value.strip()
    # Bound it: this lands in the index on disk.
    return value[:64] or None


@app.get("/guidelines")
def list_guidelines():
    return jsonify(guidelines.list_guidelines(owner=_client_id()))


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
            live=live, api_key=api_key, owner=_client_id(),
        )
    except guidelines.IngestError as error:
        return jsonify({"error": str(error)}), 400
    except Exception as error:  # a bad upload must not take the app down
        return jsonify({"error": f"ingestion failed: {error}"}), 502

    # live=True: guidelines.ingest() has handed off to a background thread
    # and entry is {"id", "status": "processing", "total"} -- 202 says so.
    # live=False: entry is the finished record, same as before -- 201.
    return jsonify(entry), (202 if entry.get("status") == "processing" else 201)


@app.post("/guidelines/<guideline_id>/process")
def process_guideline(guideline_id):
    """Reprocess an existing guideline live, against its original upload --
    the other half of upload_guideline() for a guideline that was first
    uploaded without 'process live' checked and came back with zero rules.
    Always live, so always backgrounded -- see upload_guideline()."""
    is_json = request.is_json
    json_body = (request.get_json(silent=True) if is_json else None) or {}
    api_key = (request.form.get("api_key") or json_body.get("api_key") or "").strip() or None

    try:
        entry = guidelines.reprocess(guideline_id, api_key=api_key)
    except guidelines.IngestError as error:
        return jsonify({"error": str(error)}), 400
    except Exception as error:  # a bad reprocess must not take the app down
        return jsonify({"error": f"reprocessing failed: {error}"}), 502

    return jsonify(entry), 202


@app.get("/guidelines/<guideline_id>/progress")
def guideline_progress(guideline_id):
    """Poll target for a backgrounded live ingest/reprocess: {done, total,
    status}, plus 'result' (the finished entry) once status is 'done', or
    'error' once it's 'error'. 404 once that terminal status has been read
    once -- see guidelines.get_progress -- or if this id was never run
    through the tracked (live) path at all."""
    progress = guidelines.get_progress(guideline_id)
    if progress is None:
        return jsonify({"error": f"no live progress tracked for {guideline_id!r}"}), 404
    return jsonify(progress)


@app.delete("/guidelines/<guideline_id>")
def delete_guideline(guideline_id):
    """Remove an uploaded guideline and everything stored for it."""
    try:
        result = guidelines.delete_guideline(guideline_id, owner=_client_id())
    except guidelines.IngestError as error:
        return jsonify({"error": str(error)}), 400
    return jsonify(result)


@app.patch("/guidelines/<guideline_id>")
def rename_guideline(guideline_id):
    """Change a guideline's title. The id, and so every URL already handed
    out for it, is unchanged."""
    body = request.get_json(silent=True) or {}
    title = body.get("title") or request.form.get("title")
    try:
        entry = guidelines.rename_guideline(guideline_id, title,
                                            owner=_client_id())
    except guidelines.IngestError as error:
        return jsonify({"error": str(error)}), 400
    return jsonify(entry)


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
                    "deontic": _deontic_of(r),
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
                "deontic": _deontic_of(r),
            }
            for r in rules
        ]
    )


@app.get("/guidelines/<guideline_id>/residue")
def guideline_residue(guideline_id):
    """What CQL_converter REFUSED to encode for this guideline, and why.

    A checker that serves only the rules that compiled tells a governance
    reader the tool covered everything. This route is the other half of the
    deliverable."""
    found = guidelines.get_guideline(guideline_id)
    if found is None:
        return jsonify({"error": f"unknown guideline {guideline_id!r}"}), 404
    _rules, refusals, _reg, _meta = found
    from from_dsl import residue_report

    return jsonify(residue_report(refusals))


def _fixture_finding(r) -> dict:
    """A hand-written fixture rule in the report's shape.

    These carry no dsl.Rule and no registry, so there is no English gloss and
    no terminology column. They DO carry the z3 predicate, which is the logic
    itself -- printing it is honest and is what a reader clicking "show the
    logic" is asking for. The variables are listed with their sorts so the
    formula can be read; the absence of codes is shown rather than hidden."""
    holes = tuple(getattr(r, "holes", ()) or ())
    rows = []
    for name, ref in sorted(getattr(r, "var_refs", {}).items()):
        rows.append({"name": name,
                     "type": str(ref.sort()).lower().replace("real", "real"),
                     "description": "",
                     "binding": None,
                     "unbound": name in holes})
    return {"statement_id": r.rule_id,
            "statement": getattr(r, "source_sentence", "") or r.title,
            "decision": "partial" if holes else "encode",
            "action": r.title,
            "strength": getattr(r, "source", "") or None,
            "open_holes": list(holes),
            "logic": str(getattr(r, "predicate", "")) or None,
            "variables": rows,
            "evidence": []}


@app.get("/guidelines/<guideline_id>/report")
def guideline_report(guideline_id):
    """The human-readable formalization report for a stored guideline.

    The other routes serve JSON for the checker UI. This one serves the page a
    clinician or a governance committee actually reads: every source sentence
    beside the rule it became, the refusals with reasons in plain English, and
    the checks that fired on rules that compiled anyway. Rendered on demand
    from the stored bundle, so it can never drift from what is served."""
    import json as _json

    import report as _report

    found = guidelines.get_guideline(guideline_id)
    if found is None:
        return jsonify({"error": f"unknown guideline {guideline_id!r}"}), 404
    rules, refusals, _reg, entry = found

    bundle_path = guidelines.STORE_DIR / f"{guideline_id}.json"
    if bundle_path.exists():
        with bundle_path.open(encoding="utf-8") as fh:
            bundle = _json.load(fh)
    else:
        # The built-in fixture library has no stored bundle -- its rules are
        # hand-written SpecClauses, not a CQL_converter artifact. Render what these rules do
        # carry: the source sentence, what the rule is for, and its holes.
        # There is no registry, so no English clause -- the page says less
        # rather than claiming more.
        bundle = {"registry": {"variables": [{"name": n} for n in sorted(
                      {v for r in rules for v in getattr(r, "var_refs", {})})]},
                  "rules": [], "refusals": [],
                  "findings": [_fixture_finding(r) for r in rules]}
    # Back goes to the checker for THIS guideline, not to the library: someone
    # reading the report is looking at one document, and the useful next move
    # is to run a patient against it. ?from=demo is carried through so the
    # checker's own back link still returns to the demo.
    from_demo = request.args.get("from") == "demo"
    check_url = f"/check?guideline={quote(guideline_id)}"
    html = _report.render(bundle,
                          title=f"{entry.get('title', guideline_id)} "
                                f"\u2014 formalization report",
                          home_url=check_url + ("&from=demo" if from_demo else ""),
                          home_label="Check a patient")
    return Response(html, mimetype="text/html")


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
    # threaded=True: a live upload's background thread runs for as long as
    # the document takes, and the polling GETs against /progress need to be
    # served concurrently with it rather than queued behind it.
    # A container gets its port from the platform and must bind every
    # interface; 127.0.0.1 would be unreachable from outside the container.
    import os
    app.run(host=os.environ.get("GF_HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", "8000")),
            debug=False, threaded=True)
