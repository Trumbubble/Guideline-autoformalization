"""
The real CQL toolchain, as injectable callables (DEV_PLAN P6).

`cql.synthesize` and patient_checker's checker take their external tools as
parameters so the pipeline runs offline by default; this module supplies the
real ones -- HL7's `cql-to-elm` translator and `cql-execution` -- and
importing it starts no process. `cql_to_elm` raises rather than returning an
empty error list when the toolchain isn't built, since a missing compiler
must never be indistinguishable from a clean compile.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile

ENGINE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "patient_checker", "engine")
TIMEOUT = 120

class ToolchainMissing(RuntimeError):
    """Raised rather than returning an empty error list. Silence must not read
    as success -- that mistake already cost a whole guideline run once."""

def available() -> bool:
    return (os.path.exists(os.path.join(ENGINE, "cp.txt"))
            and os.path.exists(os.path.join(ENGINE, "classes", "Translate.class")))

def _require() -> str:
    if not available():
        raise ToolchainMissing(
            f"CQL toolchain not built. Run: bash {os.path.join(ENGINE, 'setup.sh')}")
    with open(os.path.join(ENGINE, "cp.txt")) as fh:
        return fh.read().strip()

_ERR = re.compile(r"^ERROR(?: \[line (\d+):(\d+)\])?: (.*)$")

def cql_to_elm(text: str, *, out_path: str | None = None) -> list[str]:
    """Translate CQL and return the translator's errors, one string each.

    This is the `compiler` argument `cql.synthesize` expects. An empty list
    means it compiled -- which now means the FHIR types resolved, not merely
    that the brackets balanced.

    The library is written into the engine directory so `DefaultLibrarySource
    Provider` can find FHIRHelpers next to it; every generated library includes
    it, and without a sibling copy every translation fails on the include."""
    cp = _require()
    with tempfile.NamedTemporaryFile("w", suffix=".cql", dir=ENGINE,
                                     delete=False) as fh:
        fh.write(text)
        src = fh.name
    dst = out_path or src.replace(".cql", ".json")
    try:
        p = subprocess.run(["java", "-cp", f"{os.path.join(ENGINE,'classes')}:{cp}",
                            "Translate", src, dst],
                           capture_output=True, text=True, timeout=TIMEOUT, cwd=ENGINE)
        errors = []
        for line in (p.stderr or "").splitlines():
            m = _ERR.match(line.strip())
            if m:
                where = f"line {m.group(1)}:{m.group(2)} " if m.group(1) else ""
                errors.append(f"{where}{m.group(3)}")
            elif line.startswith("DRIVER-FAILURE:"):
                # The driver itself broke. That is NOT a clean compile, and it
                # is not a CQL error the model can repair either.
                raise ToolchainMissing(line)
        # An empty error list means "compiles", so it must be reachable ONLY by
        # a translator that actually succeeded. A JVM that dies with
        # ClassNotFoundException, an OOM, or any stderr the CQL error grammar
        # does not match produces no parsed errors -- and the pipeline would
        # print "cql-to-elm: COMPILES" over a build that never ran. The
        # compile-pass claim rests on this line.
        if p.returncode != 0 and not errors:
            raise ToolchainMissing(
                f"translator exited {p.returncode} with no parseable CQL error; "
                f"stderr: {(p.stderr or '').strip()[:300] or '(empty)'}")
        if not errors and not (os.path.exists(dst) and os.path.getsize(dst) > 0):
            # Exit 0 and no ELM written is not a compile either.
            raise ToolchainMissing(
                f"translator reported success but wrote no ELM to {dst}")
        return errors
    except subprocess.TimeoutExpired:
        raise ToolchainMissing(f"translator timed out after {TIMEOUT}s")
    finally:
        for p_ in (src, dst if out_path is None else None):
            if p_ and os.path.exists(p_):
                try: os.unlink(p_)
                except OSError: pass

def translate_to_file(text: str, elm_path: str) -> list[str]:
    """Translate and KEEP the ELM, for execution."""
    return cql_to_elm(text, out_path=elm_path)

def expand_valuesets(elm_path: str, out_path: str) -> dict:
    """Expand every value set an ELM library references, into the code-service
    shape cql-execution wants.

    Done from the ELM rather than a fixed file because the binder picks the
    OIDs: a stale valuesets.json fails at run time with "Unable to resolve
    expected valueset", which is indistinguishable from a patient not matching.
    Expanded ONCE and written to disk, so execution is deterministic and makes
    no terminology call."""
    import bind
    with open(elm_path, encoding="utf-8") as fh:
        elm = json.load(fh)
    defs = elm.get("library", {}).get("valueSets", {}).get("def", [])
    vsac = bind.VsacIndex()
    if not vsac.available and defs:
        raise ToolchainMissing(
            "UMLS_API_KEY is not set; cannot expand the value sets this library "
            "references, and executing without them silently reports no matches")
    out: dict = {}
    empty: list[str] = []
    for v in defs:
        oid = v["id"].replace("urn:oid:", "")
        ver = v.get("version") or "1"
        members = vsac.expand_coded(oid)
        if not members:
            # VSAC answered, and answered with nothing -- a retired OID, a
            # version that does not exist, or an entitlement the key lacks. The
            # library then executes against a value set no code can match, every
            # patient looks non-compliant, and the disagreement is reported
            # against the LOWERING. Measured: the crosscheck called a VSAC
            # miss a "LOWERING BUG" for exactly this reason. An empty expansion
            # is a fetch failure until proven otherwise.
            empty.append(v["id"])
        out.setdefault(v["id"], {})[ver] = [
            {"code": c, "system": s, "version": ver} for s, c in members]
    if empty:
        print(f"[toolchain] WARNING: {len(empty)} value set(s) expanded to ZERO "
              f"codes: {', '.join(empty[:3])}. Nothing can match them, so every "
              f"patient will look non-compliant.", file=sys.stderr)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(out, fh)
    return {v["id"]: len(out[v["id"]][v.get("version") or "1"]) for v in defs}

RESOURCE_FOR = {"Condition": "Condition", "MedicationRequest": "MedicationRequest",
                "Observation": "Observation", "Procedure": "Procedure"}

def synthetic_bundle(elm_path: str, valuesets_path: str, out_path: str, *,
                     patient_id: str = "demo", quantities: dict | None = None,
                     include: set | None = None) -> dict:
    """Build a FHIR bundle FROM the compiled artifact.

    Test data derived from the rule rather than hand-written: the retrieve in
    the ELM names the resource type and the terminology, so the patient is
    guaranteed to carry codes the library actually looks for. Hand-written
    patients silently stop matching the moment the binder picks a different
    code -- which it does, because binding is a model decision.

    This is the small, honest version of patient_checker's coverage-directed test
    synthesis; the real one walks branch conditions and solves for boundary
    values."""
    with open(elm_path, encoding="utf-8") as fh:
        elm = json.load(fh)
    lib = elm["library"]
    vs = json.load(open(valuesets_path, encoding="utf-8")) if os.path.exists(valuesets_path) else {}

    # name -> (oid or code, system) for both value sets and single codes
    terms: dict = {}
    for v in lib.get("valueSets", {}).get("def", []):
        members = next(iter(vs.get(v["id"], {}).values()), [])
        if members:
            terms[v["name"]] = (members[0]["system"], members[0]["code"])
    for c in lib.get("codes", {}).get("def", []):
        csname = (c.get("codeSystem") or {}).get("name")
        sysurl = next((s["id"] for s in lib.get("codeSystems", {}).get("def", [])
                       if s["name"] == csname), "")
        terms[c["name"]] = (sysurl, c.get("id"))

    entries = [{"resource": {"resourceType": "Patient", "id": patient_id,
                             "birthDate": "1958-03-11"}}]
    used = {}
    for expr in _retrieves(lib):
        rtype, term = expr
        if term not in terms:
            continue
        if include is not None and term not in include:
            continue
        system, code = terms[term]
        rid = f"{patient_id}-{len(entries)}"
        res = {"resourceType": rtype, "id": rid,
               "subject": {"reference": f"Patient/{patient_id}"}}
        if rtype == "Observation":
            res.update({"status": "final", "effectiveDateTime": "2026-07-01",
                        "code": {"coding": [{"system": system, "code": code}]}})
            # `thresholds()` keys by the DEFINE name ("urine ACR") while a
            # retrieve names the TERMINOLOGY ("urine ACR codes"). Accept either
            # so a caller can pass whichever it has.
            qs = quantities or {}
            q = qs.get(term) or qs.get(term[:-6].strip() if term.endswith(" codes")
                                       else term)
            if q is not None:
                res["valueQuantity"] = {"value": q[0], "unit": q[1],
                                        "system": "http://unitsofmeasure.org",
                                        "code": q[1]}
        elif rtype == "Condition":
            res.update({"clinicalStatus": {"coding": [{"system":
                "http://terminology.hl7.org/CodeSystem/condition-clinical",
                "code": "active"}]},
                "code": {"coding": [{"system": system, "code": code}]}})
        elif rtype == "MedicationRequest":
            res.update({"status": "active", "intent": "order",
                        "medicationCodeableConcept": {"coding": [
                            {"system": system, "code": code}]}})
        else:
            res["code"] = {"coding": [{"system": system, "code": code}]}
        entries.append({"resource": res})
        used[term] = code

    bundle = {"resourceType": "Bundle", "type": "collection", "entry": entries}
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(bundle, fh)
    return used

def _retrieves(lib: dict):
    """(resourceType, terminology name) for every Retrieve in the ELM."""
    out = []
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "Retrieve":
                dt = (node.get("dataType") or "").rsplit("}", 1)[-1]
                # A value-set retrieve carries `codes: {ValueSetRef}` directly,
                # but a CODE retrieve wraps it: `codes: {ToList: {CodeRef}}`.
                # Reading `codes.name` finds only the first kind, which silently
                # drops every Condition and Observation from a library bound to
                # single codes rather than value sets.
                ref = _first_terminology_ref(node.get("codes"))
                if dt and ref:
                    out.append((dt, ref))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(lib.get("statements", {}))
    return out

def thresholds(elm_path: str) -> list[dict]:
    """Every quantity comparison in the library: which define, which operator,
    which value, which unit.

    Read off the compiled ELM rather than the DSL, because the ELM is what
    actually executes -- if a lowering bug changed the comparison, this sees the
    changed one. It is the small honest version of patient_checker's coverage-directed
    test synthesis: the branch conditions come from the artifact, so a test
    cannot drift from the rule it is testing."""
    with open(elm_path, encoding="utf-8") as fh:
        lib = json.load(fh)["library"]
    ops = {"Less": "<", "LessOrEqual": "<=", "Greater": ">",
           "GreaterOrEqual": ">=", "Equal": "==", "NotEqual": "!="}
    found = []

    def walk(node, define=None):
        if isinstance(node, dict):
            if node.get("name") and node.get("expression") is not None:
                define = node["name"]
            t = node.get("type")
            if t in ops:
                operands = node.get("operand") or []
                # The subject is usually wrapped in a FHIRHelpers conversion
                # (FunctionRef -> ExpressionRef), so look INSIDE the operand
                # rather than only at its top level.
                ref = next((r for o in operands for r in [_first_expr_ref(o)] if r), None)
                # A comparison against a unitless number lowers to a Literal,
                # not a Quantity. Missing that skips the threshold entirely, so
                # the generated patient carries no value and every verdict comes
                # back null -- which looks like the rule not firing.
                q = next((o for o in operands if o.get("type") == "Quantity"), None)
                lit = next((o for o in operands if o.get("type") == "Literal"
                            and _numeric(o.get("value"))), None)
                if ref and (q is not None or lit is not None):
                    value = float((q or lit).get("value"))
                    found.append({"define": define, "subject": ref, "op": ops[t],
                                  "value": value,
                                  "unit": q.get("unit") if q is not None else None})
            for v in node.values():
                walk(v, define)
        elif isinstance(node, list):
            for v in node:
                walk(v, define)

    walk(lib.get("statements", {}))
    return found

def _numeric(v) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False

def _first_terminology_ref(node) -> str | None:
    """The ValueSetRef / CodeRef / CodeSystemRef name inside a retrieve's
    `codes`, however it is wrapped."""
    if isinstance(node, dict):
        if node.get("type") in {"ValueSetRef", "CodeRef", "CodeSystemRef"} \
                and node.get("name"):
            return node["name"]
        for v in node.values():
            r = _first_terminology_ref(v)
            if r:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _first_terminology_ref(v)
            if r:
                return r
    return None

def _first_expr_ref(node) -> str | None:
    if isinstance(node, dict):
        if node.get("type") == "ExpressionRef" and node.get("name"):
            return node["name"]
        for v in node.values():
            r = _first_expr_ref(v)
            if r:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _first_expr_ref(v)
            if r:
                return r
    return None

def boundary_values(th: dict, step: float | None = None) -> tuple[float, float]:
    """(satisfying, violating) values for one threshold, placed AT the boundary.

    Boundary rather than middle-of-range: `< 30` and `<= 30` differ only at
    exactly 30, and an inclusivity slip compiles cleanly. Random sampling hits
    that point only by luck."""
    v = th["value"]
    step = step if step is not None else max(abs(v) * 0.01, 0.1)
    if th["op"] in ("<", "<="):
        return (v - step, v + step) if th["op"] == "<" else (v, v + step)
    if th["op"] in (">", ">="):
        return (v + step, v - step) if th["op"] == ">" else (v, v - step)
    return (v, v + step)

def execute(elm_path: str, bundle_path: str, valuesets_path: str | None = None,
            deps: list[str] | None = None) -> dict:
    """Run compiled ELM against a FHIR bundle. Returns {patient_id: {define: value}}.

    A define that evaluates to null comes back as None, not False. That
    distinction IS the three-valued verdict -- an unbound predicate is encoded
    as `null` precisely so CQL's own logic propagates it, and collapsing it
    here would undo that."""
    _require()
    deps = deps or [os.path.join(ENGINE, "FHIRHelpers.json")]
    args = ["node", os.path.join(ENGINE, "execute.js"), elm_path, bundle_path]
    args.append(valuesets_path or os.path.join(ENGINE, "valuesets.json"))
    args += [d for d in deps if os.path.exists(d)]
    p = subprocess.run(args, capture_output=True, text=True, timeout=TIMEOUT, cwd=ENGINE)
    if p.returncode != 0:
        raise RuntimeError(f"execution failed: {(p.stderr or '').strip()[:300]}")
    return json.loads(p.stdout)
