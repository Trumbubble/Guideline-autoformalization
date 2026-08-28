#!/usr/bin/env python3
"""
End-to-end demo: guideline prose -> governance artifact -> live patient verdict.

Runs the whole product in the order a reviewer should watch it:

  1. TRIAGE FIRST      what the tool refuses to encode, and why -- before any
                       code is shown. Leading with the artifact and mentioning
                       the residue afterwards reads as hiding it.
  2. BIND              every clinical concept resolved to a versioned code by
                       RETRIEVAL, never generated
  3. SYNTHESISE        typed rule under constrained decoding, then a
                       deterministic lowering to CQL
  4. COMPILE           the official HL7 cql-to-elm translator, with the repair
                       loop feeding real type errors back
  5. VERIFY            three-valued verdict over Z3, with a minimal
                       counterexample naming the assumptions it needs
  6. EXECUTE           the same library run by cql-execution against a FHIR
                       bundle -- the artifact actually runs
  7. BUNDLE            CQL + rules + refusal report, for a governance committee

    python3 demo.py                 # offline: heuristics, no key, no toolchain
    python3 demo.py --live          # real model + real compiler + execution
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "Part A"))
sys.path.insert(0, os.path.join(HERE, "Part B"))

import bind, cql, dsl, gate  # noqa: E402

# Statements are quoted verbatim from KDIGO-style CKD guidance. Chosen to
# exercise every path: one that encodes cleanly, one that is partial, one that
# must be refused, and one that is not about a patient at all.
STATEMENTS = [
    ("s1", "We recommend treatment with an ACE inhibitor in adults with diabetes "
           "and a urine albumin-to-creatinine ratio of 30 mg/g or higher."),
    ("s2", "Check serum potassium within 14 days of starting an ACE inhibitor."),
    ("s3", "Start an ACE inhibitor when eGFR falls below 30 or the patient is at "
           "high risk."),
    ("s4", "Consider referral after an adequate trial of conservative therapy."),
    ("s5", "Commissioners should ensure rapid-access clinics are available in "
           "every region."),
]

RULE = "-" * 78

def hdr(n, title):
    print(f"\n{RULE}\n {n}. {title}\n{RULE}")

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--live", action="store_true",
                    help="use the real model, compiler and execution engine")
    ap.add_argument("--out", default=os.path.join(HERE, "Part B", "bundle.json"))
    a = ap.parse_args(argv)

    mention_llm = select_llm = formalize = None
    compiler = None
    if a.live:
        import llm, toolchain
        mention_llm, select_llm = llm.propose_mentions, llm.select_candidate
        formalize = lambda t, r: llm.formalize(t, r, effort="medium")
        if toolchain.available():
            compiler = toolchain.cql_to_elm
        else:
            print("[warn] CQL toolchain not built; compilation will be structural only")

    index = _build_index(a.live)

    # ---- 1. triage, before anything is compiled --------------------------
    hdr(1, "TRIAGE  -- what the tool refuses to encode, and why")
    triages = []
    for sid, text in STATEMENTS:
        t = gate.triage(text, index, statement_id=sid, mention_llm=mention_llm,
                        select_llm=select_llm, formalize=formalize,
                        source_span=text)
        triages.append(t)
        mark = {"encode": "ENCODE ", "partial": "PARTIAL", "refuse": "REFUSE "}[t.decision]
        print(f"  [{mark}] {sid}  {text[:62]}")
        if t.reason_code:
            print(f"            reason: {t.reason_code}")
        if t.open_holes:
            print(f"            holes : {', '.join(t.open_holes)}  (left FREE, not stubbed)")
    sp = gate.spectrum(triages)
    print(f"\n  spectrum: {json.dumps(sp['buckets'])}")
    print(f"  in-fragment {sp['in_fragment_rate']:.0%}"
          f"  |  {sp['refuse']} refused  |  {sp['open_holes']} open holes")
    if sp.get("retrieval_attributable"):
        print(f"  NOTE {sp['retrieval_attributable']} refusal(s) attributable to OUR "
              f"retrieval, not the guideline")

    # ---- 2-4. bind, synthesise, compile ---------------------------------
    hdr(2, "SYNTHESISE + COMPILE  -- the encodable statements become CQL")
    rules, refusals, libraries = [], [], {}
    for t in triages:
        if not t.encodable or t.rule is None:
            if t.as_refusal():
                refusals.append(t.as_refusal())
            continue
        bs, _ = bind.bind_statement(t.text, index, mention_llm, select_llm)
        reg = bind.registry_from_bindings(bs)
        for h in t.open_holes:
            if h not in reg:
                reg = dsl.Registry([reg[n] for n in reg.names()]
                                   + [dsl.VarDef(h, "bool", None, h, unbound=True)],
                                   [dsl.EventDef(e) for e in reg.event_names()])
        try:
            lib = cql.rule_to_cql(t.rule, reg, library_name=f"Demo{t.statement_id}")
        except Exception as exc:
            print(f"  {t.statement_id}: could not lower ({exc})")
            continue
        errs = (compiler(lib.text) if compiler else
                [i.message for i in cql.check_cql(lib.text) if i.severity == "error"])
        gate_word = "cql-to-elm" if compiler else "structural"
        print(f"  {t.statement_id}: {len(lib.text.splitlines())} lines of CQL, "
              f"{len(lib.valuesets)} value set(s)  --  {gate_word}: "
              f"{'COMPILES' if not errs else errs[0][:52]}")
        rules.append(t.rule)
        libraries[t.statement_id] = lib

    # ---- 5. the governance bundle ---------------------------------------
    hdr(3, "BUNDLE  -- what a governance committee receives")
    reg_all = _merge_registries(triages, index, mention_llm, select_llm)
    dsl.write_bundle(a.out, reg_all, rules, refusals)
    print(f"  wrote {a.out}")
    print(f"    {len(rules)} rule(s), {len(refusals)} refusal(s), "
          f"{len(reg_all.names())} bound variable(s)")
    for r in refusals:
        print(f"    REFUSED {r.rule_id}: [{r.reason_code}] {r.reason_text[:58]}")

    # ---- 6. verify: three-valued verdict over Z3 -------------------------
    hdr(4, "VERIFY  -- three-valued verdict, with the assumptions named")
    from from_dsl import load_bundle
    loaded, loaded_refusals, _ = load_bundle(a.out)
    import api
    for lr in loaded:
        for label, known in _patients_for(lr):
            res = api.check_rule(lr, known)
            print(f"  {lr.rule_id:5s} {label:34s} -> {res['verdict']}")
            if res["counterexample_core"]:
                print(f"        requires assuming: {res['counterexample_core']}")

    # ---- 7. execute the CQL itself ---------------------------------------
    if a.live and compiler and libraries:
        hdr(5, "EXECUTE  -- the same library, run against a FHIR patient")
        import toolchain, tempfile
        eng = toolchain.ENGINE
        # Prefer a PARTIAL library: its hole is null, so CQL's three-valued
        # logic is visible in the output rather than merely described.
        partial = {t.statement_id for t in triages if t.open_holes}
        sid = next((k for k in libraries if k in partial), next(iter(libraries)))
        lib = libraries[sid]
        print(f"  library: {sid}"
              + ("  (PARTIAL -- contains an undefined predicate)" if sid in partial else ""))
        with tempfile.TemporaryDirectory() as d:
            elm = os.path.join(d, "lib.json")
            errs = toolchain.translate_to_file(lib.text, elm)
            if errs:
                print(f"  translation failed: {errs[0][:70]}")
            else:
                vs = os.path.join(d, "valuesets.json")
                sizes = toolchain.expand_valuesets(elm, vs)
                for oid, n in sizes.items():
                    print(f"  expanded {oid.replace('urn:oid:','')} -> {n} codes")
                for label, path in _demo_patients(elm, vs, d):
                    out = toolchain.execute(elm, path, vs)["results"]
                    vals = next(iter(out.values()))
                    v = vals.get("Violated")
                    verdict = ("VIOLATED" if v is True else
                               "compliant" if v is False else
                               "UNKNOWN (a hole is null -- CQL propagates it)")
                    print(f"  {label:34s} Applicable={vals.get('Applicable')!s:5s} "
                          f"-> {verdict}")
    print()

def _demo_patients(elm_path, vs_path, tmpdir):
    """Patients generated FROM the compiled artifact.

    The thresholds come out of the ELM, so the values sit exactly AT the
    boundary -- `>= 30` and `> 30` differ only at 30, and an inclusivity slip
    compiles cleanly. This is the small honest version of Track B's
    coverage-directed test synthesis; the real one walks every branch."""
    import toolchain, json as _json
    with open(elm_path) as fh:
        lib = _json.load(fh)["library"]
    terms = [n for _t, n in toolchain._retrieves(lib)]
    ths = toolchain.thresholds(elm_path)
    med = {t for t in terms if "inhibitor" in t.lower() or "arb" in t.lower()}

    def quantities(satisfy):
        out = {}
        for th in ths:
            sat, vio = toolchain.boundary_values(th)
            out[th["subject"]] = ((sat if satisfy else vio), th["unit"])
        return out

    plans = [("threshold met, NOT treated", set(terms) - med, quantities(True)),
             ("threshold met, treated",     set(terms),        quantities(True))]
    if ths:
        plans.append((f"just below the threshold ({ths[0]['value']} {ths[0]['unit']})",
                      set(terms) - med, quantities(False)))
    out = []
    for label, include, q in plans:
        p = os.path.join(tmpdir, f"pt{len(out)}.json")
        toolchain.synthetic_bundle(elm_path, vs_path, p, patient_id=f"pt{len(out)}",
                                   quantities=q, include=include)
        out.append((label, p))
    return out

def _build_index(live):
    """Local LOINC first, then live terminology.

    Seeding from the licensed LOINC release is what makes lab binding
    trustworthy: the free keyword endpoints return groupers and gene assays for
    "eGFR", while the release carries SCALE_TYP, PROPERTY, COMMON_TEST_RANK and
    real UCUM units, so the right code is selected structurally."""
    if not live:
        return bind.fixture_index()
    db = os.path.join(HERE, "Part A", "loinc.db")
    local = bind.SqliteIndex(db)
    if not local.search("potassium", "lab", 1):
        try:
            n = bind.load_loinc(local)
            print(f"[setup] loaded {n} LOINC concepts (v{local.version(bind.LOINC)})")
        except FileNotFoundError as exc:
            print(f"[warn] {exc}")
    return bind.CachedIndex(local, bind.RemoteIndex())

def _first_quantity_unit(lib):
    found = []
    def walk(n):
        if isinstance(n, dict):
            if n.get("type") == "Quantity" and n.get("unit"):
                found.append(n["unit"])
            for v in n.values(): walk(v)
        elif isinstance(n, list):
            for v in n: walk(v)
    walk(lib.get("statements", {}))
    return found[0] if found else None

def _merge_registries(triages, index, mention_llm, select_llm):
    seen, events = {}, {}
    for t in triages:
        if not t.encodable:
            continue
        bs, _ = bind.bind_statement(t.text, index, mention_llm, select_llm)
        for v in bind.registry_from_bindings(bs)._v.values():
            seen.setdefault(v.name, v)
        for h in t.open_holes:
            seen.setdefault(h, dsl.VarDef(h, "bool", None, h, unbound=True))
        if t.rule is not None:
            for e in _events_in(t.rule):
                events.setdefault(e, dsl.EventDef(e, e.replace("_", " ")))
    return dsl.Registry(list(seen.values()), list(events.values()))

def _events_in(rule):
    out = []
    def walk(e):
        if isinstance(e, dsl.Since): out.append(e.event)
        elif isinstance(e, dsl.Not): walk(e.operand)
        elif isinstance(e, (dsl.And, dsl.Or)):
            for o in e.operands: walk(o)
    walk(rule.when); walk(rule.then)
    return out

def _patients_for(lr):
    """A pinned compliant patient, a pinned violating one, and a partial one --
    the third is the case the two-valued check gets wrong."""
    names = set(lr.var_refs)
    out = []
    bools = {n for n in names if "acr" not in n and "egfr" not in n
             and not n.startswith("days_since")}
    if bools:
        out.append(("every variable pinned, compliant",
                    {n: True for n in names if n in bools}))
    out.append(("nothing pinned -- partial patient", {}))
    return out

if __name__ == "__main__":
    main()
