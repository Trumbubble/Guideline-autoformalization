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
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "CQL_converter"))
sys.path.insert(0, os.path.join(HERE, "patient_checker"))

import z3  # noqa: E402

import bind, cassette, cql, dsl, gate  # noqa: E402

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

# Evidence that does not block an encoding but says the encoding may not mean
# what the sentence meant. Printed with the triage line, not buried in JSON.
CAUTION_CODES = {"CIRCULAR_BASELINE", "DROPPED_THRESHOLD", "ACTION_TARGET_MISMATCH",
                 "DROPPED_CONCEPT_RISK", "UNUSED_BOUND_CONCEPT", "REGISTRY_TRUNCATED",
                 "TERMINOLOGY_UNAVAILABLE"}

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--live", action="store_true",
                    help="use the real model, compiler and execution engine")
    ap.add_argument("--out", default=os.path.join(HERE, "patient_checker", "bundles", "bundle.json"))
    ap.add_argument("--doc", metavar="PATH_OR_PMCID",
                    help="your own guideline: a .pdf / .xml / .html file, or a "
                         "PubMed Central id like PMC13276589")
    ap.add_argument("--max", type=int, default=5,
                    help="how many recommendations to process from --doc")
    ap.add_argument("--no-record", action="store_true",
                    help="do not tape this run's model responses")
    a = ap.parse_args(argv)

    # Tape every live run by default. Each defect in this pipeline so far
    # surfaced only when a real document was run, because a real document is
    # the only thing that exercises the live path -- which made the user the
    # integration test. A tape turns one paid run into a fixture the whole
    # suite replays offline afterwards, so the next such defect is found at
    # commit time instead of on the next run. See CQL_converter/cassette.py.
    if a.live and not a.no_record and not os.environ.get(cassette.ENV_PATH):
        tapes = os.path.join(HERE, "CQL_converter", "cassettes")
        os.makedirs(tapes, exist_ok=True)
        stem = re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.basename(a.doc or "builtin"))
        os.environ[cassette.ENV_PATH] = os.path.join(tapes, f"{stem}.jsonl")
        os.environ[cassette.ENV_MODE] = "record"
        print(f"  recording model responses to "
              f"{os.path.relpath(os.environ[cassette.ENV_PATH], HERE)} "
              f"(replayable offline; --no-record to skip)")

    mention_llm = select_llm = formalize = classify = None
    compiler = None
    if a.live:
        import llm, toolchain
        mention_llm, select_llm = llm.propose_mentions, llm.select_candidate
        _formalize = llm.formalize
        classify = llm.classify_block
        if toolchain.available():
            compiler = toolchain.cql_to_elm
        else:
            print("[warn] CQL toolchain not built; compilation will be structural only")

    index = _build_index(a.live)
    statements, abbrevs = (_statements_from(a.doc, a.max, classify) if a.doc
                           else (STATEMENTS, {}))
    if a.live:
        # The formalizer hook keeps its (text, registry) contract; the document
        # glossary rides in on the closure, so gate.py never has to know about it.
        formalize = lambda t, r: _formalize(t, r, effort="medium", abbrevs=abbrevs)
    if a.doc:
        print(f"  source: {a.doc}  ->  {len(statements)} recommendation(s)\n")
    # The statements go on the tape too. Without them a replay would have to
    # re-segment the source document, which means shipping the PDF/XML and
    # re-running the classifier -- so the fixture would rot the moment the
    # corpus moved. With them, `test_replay.py` reruns the real pipeline from
    # a file and a tape alone.
    if os.environ.get(cassette.ENV_MODE) == "record":
        side = os.environ[cassette.ENV_PATH].replace(".jsonl", ".statements.json")
        with open(side, "w", encoding="utf-8") as fh:
            json.dump({"doc": a.doc, "abbrevs": abbrevs,
                       "statements": [list(x) for x in statements]}, fh, indent=1)

    # ---- 1. triage, before anything is compiled --------------------------
    hdr(1, "TRIAGE  -- what the tool refuses to encode, and why")
    triages = []
    for sid, text in statements:
        t = gate.triage(text, index, statement_id=sid, mention_llm=mention_llm,
                        select_llm=select_llm, formalize=formalize,
                        source_span=text, doc_abbrevs=abbrevs)
        triages.append(t)
        mark = {"encode": "ENCODE ", "partial": "PARTIAL", "refuse": "REFUSE "}[t.decision]
        print(f"  [{mark}] {sid}  {text[:62]}")
        if t.reason_code:
            print(f"            reason: {t.reason_code}")
        if t.open_holes:
            print(f"            holes : {', '.join(t.open_holes)}  (left FREE, not stubbed)")
        # Printed on every refusal so "the DSL cannot express this" can be told
        # apart from "the construct existed and was withheld from the grammar"
        # without opening the bundle or spending another live run.
        _menu = next((e for e in t.evidence if e.code == "GRAMMAR_OFFERED"), None)
        if _menu and t.decision == "refuse":
            print(f"            offered: {_menu.detail}")
        # The findings that do NOT block an encode but mean the rule may be
        # clinically wrong: a dropped threshold, a circular baseline, an action
        # that does not match its own gloss. These are the whole point -- a
        # rule that typechecks, compiles and is wrong is the failure mode a
        # governance reader cannot see -- so they must not sit unread in the
        # bundle's evidence list.
        for _c in [e for e in t.evidence if e.code in CAUTION_CODES]:
            print(f"            CAUTION [{_c.code}] {_c.detail[:96]}")
    if len(getattr(index, "failures", ())):
        print(f"\n  [warn] {len(index.failures)} terminology lookup(s) FAILED "
              f"(transport or server error, not an absent concept). Refusals "
              f"above may be ours, not the guideline's:")
        for line in index.failures.summary()[:5]:
            print(f"         {line[:100]}")
    sp = gate.spectrum(triages)
    print(f"\n  spectrum: {json.dumps(sp['buckets'])}")
    print(f"  in-fragment {sp['in_fragment_rate']:.0%}"
          f"  |  {sp['refuse']} refused  |  {sp['open_holes']} open holes")
    if sp.get("retrieval_attributable"):
        print(f"  NOTE {sp['retrieval_attributable']} refusal(s) attributable to OUR "
              f"retrieval, not the guideline")

    # ---- 2-4. bind, synthesise, compile ---------------------------------
    hdr(2, "SYNTHESISE + COMPILE  -- the encodable statements become CQL")
    rules, refusals, libraries, cql_files = [], [], {}, {}
    for t in triages:
        if not t.encodable or t.rule is None:
            if t.as_refusal():
                refusals.append(t.as_refusal())
            continue
        # The registry the rule was WRITTEN AGAINST -- never a fresh binding.
        # This was the second site of the R17a bug: re-binding here produced a
        # different vocabulary than triage had used, so the rule referenced a
        # variable the registry did not contain and cql-to-elm reported
        # "Could not resolve identifier on_bp_lowering". `bind_statement` is not
        # idempotent (it consults a model selector), so any re-derivation of a
        # registry is a bug waiting to surface three stages downstream.
        reg = t.registry
        if reg is None:
            bs, _ = bind.bind_statement(t.text, index, mention_llm, select_llm,
                                        doc_abbrevs=abbrevs)
            reg = bind.registry_from_bindings(bs)
        _stubs = dsl.hole_defs(reg, t.open_holes, t.rule)
        if _stubs:
            reg = dsl.Registry([reg[n] for n in reg.names()] + _stubs,
                               [reg.event(e) for e in reg.event_names()])
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
        # Write it out. The CQL is the artifact a site actually runs, and it
        # was being generated, printed as a one-line status, and thrown away --
        # so the pipeline's most concrete output existed only for as long as
        # the terminal scrollback did.
        cql_dir = os.path.join(os.path.dirname(a.out) or ".", "cql",
                               os.path.splitext(os.path.basename(a.out))[0])
        os.makedirs(cql_dir, exist_ok=True)
        cql_path = os.path.join(cql_dir, f"{_slugfile(t.statement_id)}.cql")
        with open(cql_path, "w", encoding="utf-8") as fh:
            fh.write(lib.text)
        cql_files[t.statement_id] = os.path.relpath(cql_path, HERE)

    # ---- 5. the governance bundle ---------------------------------------
    hdr(3, "BUNDLE  -- what a governance committee receives")
    reg_all = _merge_registries(triages, index, mention_llm, select_llm)
    import report as _rep
    findings = [_rep.finding(t, reg_all, cql_files) for t in triages]
    dsl.write_bundle(a.out, reg_all, rules, refusals, findings)
    print(f"  wrote {a.out}")
    # The readable half. The bundle is an interface artifact for Track B; this
    # is the one a clinician or a governance committee can actually read, with
    # each source sentence beside the rule it became.
    try:
        import report as _report
        _stem = os.path.splitext(os.path.basename(a.out))[0]
        _html = os.path.join(os.path.dirname(a.out) or ".", f"{_stem}.html")
        with open(_html, "w", encoding="utf-8") as fh:
            fh.write(_report.render(
                json.load(open(a.out, encoding="utf-8")),
                title=f"{a.doc or 'demo'} — formalization report",
                cql_root=os.path.join(os.path.dirname(a.out) or ".", "cql", _stem)))
        print(f"    report: {os.path.relpath(_html, HERE)}  (open in a browser)")
        # Put it in the library the web UI serves. demo.py and Track B's upload
        # both produce bundles, but only the upload's landed in the store -- so
        # a document formalized from the CLI never appeared on the dashboard,
        # and the report route the UI has could not reach it. Re-running a
        # document replaces its entry rather than stacking duplicates.
        try:
            import guidelines as _gl
            _entry = _gl.register_bundle(
                a.out, title=(a.doc or "demo"),
                guideline_id=_slugfile(a.doc or "demo"))
            print(f"    library: http://localhost:8000/guidelines/"
                  f"{_entry['id']}/report  (run `python3 api.py` in patient_checker)")
        except Exception as exc:
            print(f"    (not added to the web library: {type(exc).__name__}: {exc})")
        if cql_files:
            print(f"    CQL   : {len(cql_files)} library/libraries under "
                  f"{os.path.relpath(os.path.dirname(next(iter(cql_files.values()))), HERE) if cql_files else ''}")
    except Exception as exc:
        # A report is a convenience. It must never be the reason a run fails
        # after the bundle -- the deliverable -- has already been written.
        print(f"    (report not generated: {type(exc).__name__}: {exc})")
    print(f"    {len(rules)} rule(s), {len(refusals)} refusal(s), "
          f"{len(reg_all.names())} bound variable(s)")
    for r in refusals:
        print(f"    REFUSED {r.rule_id}: [{r.reason_code}] {r.reason_text[:58]}")

    # ---- 6. verify: three-valued verdict over Z3 -------------------------
    hdr(4, "VERIFY  -- three-valued verdict, with the assumptions named")
    if not rules:
        # Say so. An empty section under a printed header reads as a crash,
        # and on a 0-encoded run stage 5 vanished entirely with no explanation.
        print("  nothing to verify: 0 statements encoded. Every one was refused,"
              "\n  with the reasons listed above -- that is an outcome, not a"
              " failure.\n")
    from from_dsl import load_bundle
    loaded, loaded_refusals, _, loaded_rejected = load_bundle(a.out)
    if loaded_rejected:
        # Track B re-verifies what Track A wrote. A rule that reaches here and
        # fails is OUR defect, not the guideline's residue -- report it as such
        # and do not run it against a patient.
        print(f"  {len(loaded_rejected)} rule(s) REJECTED BY VERIFICATION and not "
              f"loaded:")
        for x in loaded_rejected:
            print(f"    [{', '.join(x['codes'])}] {x['rule_id']}: {x['why'][:60]}")
    import api
    for lr in loaded:
        for label, known in _patients_for(lr):
            res = api.check_rule(lr, known)
            print(f"  {lr.rule_id:5s} {label:34s} -> {res['verdict']}")
            if res["counterexample_core"]:
                print(f"        requires assuming: {res['counterexample_core']}")

    # ---- 7. execute the CQL itself ---------------------------------------
    if a.live and compiler and not libraries:
        hdr(5, "EXECUTE  -- the same library, run against a FHIR patient")
        print("  nothing to execute: no library was produced.\n")
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
                # The library reports its own deontic force. Reading only
                # `Violated` would print "compliant" for a permitted action
                # that was simply not taken -- the CQL side would then contradict
                # the Z3 side, which says OPTION-not-taken. CQL cannot express
                # the distinction, so the executor has to honour the marker the
                # library carries instead (RESULTS.md R18).
                for label, path in _demo_patients(elm, vs, d):
                    out = toolchain.execute(elm, path, vs)["results"]
                    if not out:
                        # An empty result set is the engine finding no patient,
                        # not a compliant one. Bare `next(iter(...))` turned
                        # that into an opaque StopIteration traceback.
                        print(f"  {label:34s} no patient evaluated -- "
                              f"check the FHIR bundle")
                        continue
                    vals = next(iter(out.values()))
                    v, applicable = vals.get("Violated"), vals.get("Applicable")
                    if vals.get("Deontic") == "permission":
                        verdict = ("OPTION-not-applicable" if applicable is False else
                                   "OPTION-taken" if vals.get("OptionTaken") is True else
                                   "OPTION-not-taken" if vals.get("OptionNotTaken") is True
                                   else "OPTION-available (a hole is null)")
                    else:
                        verdict = ("VIOLATED" if v is True else
                                   "compliant" if v is False else
                                   "UNKNOWN (a hole is null -- CQL propagates it)")
                    print(f"  {label:34s} Applicable={applicable!s:5s} "
                          f"-> {verdict}")
    print()

def _demo_patients(elm_path, vs_path, tmpdir):
    """Patients generated FROM the compiled artifact.

    The thresholds come out of the ELM, so the values sit exactly AT the
    boundary -- `>= 30` and `> 30` differ only at 30, and an inclusivity slip
    compiles cleanly. This is the small honest version of Track B's
    coverage-directed test synthesis; the real one walks every branch."""
    import toolchain, json as _json
    with open(elm_path, encoding="utf-8") as fh:
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

def _statements_from(doc, limit, classify=None):
    """Segment YOUR guideline into recommendation statements.

    Accepts a local .xml / .html / .pdf, or a PubMed Central id (fetched from
    Europe PMC). Only statements the segmenter judges to be recommendations are
    passed on -- background prose, methods and funding sections are dropped
    before anything is bound or encoded.

    `classify` is the judge. Passing None is a WORKING mode -- the offline
    heuristic -- which is exactly why this went unnoticed: every --live run
    used the word-counting fallback while the model judged mentions, candidates
    and encodings. Measured on the gold set, that fallback let 27% non-
    recommendations through (RESULTS.md R20). The manifest recorded
    `"classifier": "heuristic"` on every one of those runs and nothing printed
    it, so the evidence was present and invisible; the segmenter line below now
    says which judge ran."""
    import segment as sg
    if doc.upper().startswith("PMC") and not os.path.exists(doc):
        path = sg.fetch_pmc(doc, cache_dir=os.path.join(HERE, "CQL_converter", "corpus"))
    else:
        path = doc
        if not os.path.exists(path):
            raise SystemExit(f"no such file: {path}")
    ext = os.path.splitext(path)[1].lower()
    doc_id = os.path.splitext(os.path.basename(path))[0]
    if ext == ".pdf":
        blocks = sg.load_pdf(path, doc_id)
    elif ext in {".html", ".htm"}:
        blocks = sg.load_html(open(path, "rb").read(), doc_id)
    else:
        blocks = sg.load_jats(path, doc_id)
    # Only judge as many candidates as `--max` needs. Without this a --max 5 run
    # on a 517-block guideline spent 286 model calls before the first statement
    # was formalized, most of them on statements the caller had asked to skip.
    stmts, manifest = sg.segment(blocks, doc_id, llm=classify, limit=limit)
    keep = [x for x in stmts if x.is_recommendation and not x.duplicate_of]
    keep.sort(key=lambda x: -x.prefilter_score)
    abbrevs = manifest.get("abbreviations", {})
    print(f"  segmenter: {manifest['blocks']} blocks -> {len(keep)} recommendations"
          f"  ({manifest['figures_uncovered']} figures/image-tables uncovered)"
          f"  [judge: {manifest['classifier']}]")
    if manifest.get("unclassified"):
        print(f"  partial pass: {manifest['candidates'] - manifest['unclassified']} of "
              f"{manifest['candidates']} candidates judged (--max {limit}); "
              f"drop --max for the whole document")
    if abbrevs:
        shown = ", ".join(f"{k}={v}" for k, v in sorted(abbrevs.items())[:4])
        print(f"  glossary : {len(abbrevs)} abbreviations defined by this document"
              f"  ({shown}{', ...' if len(abbrevs) > 4 else ''})")
    if not keep:
        directive = manifest.get("units_with_directive_language", 0)
        print(f"\n  NO RECOMMENDATIONS FOUND. {directive} of {manifest['units']} "
              f"sentences contain any directive language\n"
              f"  (\"we recommend\", \"should\", \"offer\", \"is indicated\").")
        if directive == 0:
            print("  A guideline has many. Zero usually means this is the wrong "
                  "file -- a\n  supplement, evidence tables, a methods appendix -- "
                  "rather than the\n  recommendations document itself.")
        else:
            print("  The prefilter found directive language but the classifier "
                  "rejected it.\n  Try --max with a lower prefilter threshold, or "
                  "check the extraction.")
        if manifest["figures_uncovered"]:
            print(f"  Also: {manifest['figures_uncovered']} figures/image-tables are "
                  f"unreadable from this file.")
        raise SystemExit(1)
    return ([(x.statement_id, " ".join(x.text.split())) for x in keep[:limit]],
            abbrevs)

def _build_index(live):
    """Local LOINC first, then live terminology.

    Seeding from the licensed LOINC release is what makes lab binding
    trustworthy: the free keyword endpoints return groupers and gene assays for
    "eGFR", while the release carries SCALE_TYP, PROPERTY, COMMON_TEST_RANK and
    real UCUM units, so the right code is selected structurally."""
    if not live:
        return bind.fixture_index()
    local = bind.SqliteIndex(os.path.join(HERE, "CQL_converter", "terms.db"))
    # Load whichever licensed releases are present. Each is optional; the
    # binder degrades to the free endpoints for anything missing, and says so.
    for name, system, loader in [("LOINC", bind.LOINC, bind.load_loinc),
                                 ("SNOMED", bind.SNOMED, bind.load_snomed),
                                 ("RxNorm", bind.RXNORM, bind.load_rxnorm)]:
        if local.version(system):
            continue
        try:
            n = loader(local)
            print(f"[setup] loaded {n:,} {name} concepts "
                  f"(v{local.version(system)})")
        except FileNotFoundError:
            print(f"[warn] no local {name} release -- falling back to public "
                  f"endpoints, which rank badly and carry no synonyms")
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
    """Union of the registries the rules were WRITTEN AGAINST.

    This used to re-bind each statement from scratch. That is wrong, and it
    shipped a broken bundle: binding runs through a model selector, so the
    second call can disagree with the first. On PMC13270348 "close monitoring"
    bound during triage and did not bind on the rebuild, so the delivered rule
    referenced a variable the delivered registry did not contain -- the
    artifact shipped was not the artifact verified. Track B rejected it as
    UNKNOWN_VAR, which is the safety net working, not the pipeline working.

    Re-binding is kept only as the fallback for a Triage from an older run that
    has no registry attached."""
    seen, events = {}, {}
    for t in triages:
        if not t.encodable:
            continue
        if t.registry is not None:
            for n in t.registry.names():
                seen.setdefault(n, t.registry[n])
        else:
            bs, _ = bind.bind_statement(t.text, index, mention_llm, select_llm)
            for v in bind.registry_from_bindings(bs)._v.values():
                seen.setdefault(v.name, v)
        # Events BEFORE holes: a `Delta` baseline is derived from an event, and
        # `hole_defs` needs the event declared to recognise it as the lowering's
        # to emit rather than the registry's.
        if t.rule is not None:
            for e in dsl.events_in(t.rule):
                events.setdefault(e, (t.registry.event(e)
                                      if t.registry is not None and e in t.registry.event_names()
                                      else dsl.EventDef(e, e.replace("_", " "))))
        _reg_so_far = dsl.Registry(list(seen.values()), list(events.values()))
        for v in dsl.hole_defs(_reg_so_far, t.open_holes, t.rule):
            seen.setdefault(v.name, v)
    return dsl.Registry(list(seen.values()), list(events.values()))

def _z3_py(value):
    """A z3 model value as a Python bool/int/float, or None if it is not a
    concrete number (an unconstrained or algebraic value)."""
    if z3.is_true(value):
        return True
    if z3.is_false(value):
        return False
    if z3.is_int_value(value):
        return value.as_long()
    if z3.is_rational_value(value):
        return value.numerator_as_long() / value.denominator_as_long()
    if z3.is_algebraic_value(value):
        return _z3_py(value.approx(12))
    return None

def _slugfile(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)

def _patients_for(lr):
    """A pinned compliant patient, a pinned violating one, and a partial one --
    the third is the case the two-valued check gets wrong.

    The patients are SOLVED FOR, not guessed. This used to decide a variable's
    type by substring-matching its name (`"acr" not in n and "egfr" not in n`)
    and pin everything else to True, which worked only for the hand-picked
    demo statements. The first real registry containing a dose handed the
    boolean True to a real-valued variable and Track B refused it -- correctly,
    since True is a category error, not the number 1 -- and stage 4 crashed.

    z3 already knows every sort and every constraint, so ask it: a model of
    `domain and phi` is a genuinely compliant patient and a model of
    `domain and not phi` a genuinely violating one, for any rule shape, with no
    name heuristics anywhere."""
    out = []
    for label, goal in [("solved compliant patient", lr.predicate),
                        ("solved violating patient", z3.Not(lr.predicate))]:
        solver = z3.Solver()
        solver.add(*list(getattr(lr, "domain", None) or []), goal)
        if solver.check() != z3.sat:
            continue                      # no such patient exists; say nothing
        model = solver.model()
        known = {}
        for name, ref in lr.var_refs.items():
            # Holes included, deliberately. Supplying the missing datum is what
            # turns MAY-violate into a definite verdict, and the contrast with
            # the unpinned patient below IS the three-valued demonstration: the
            # same rule and the same z3, answering "cannot tell" only because
            # one value was never supplied.
            value = _z3_py(model.eval(ref, model_completion=True))
            if value is not None:
                known[name] = value
        out.append((label, known))
    out.append(("nothing pinned -- the holes stay free", {}))
    return out

if __name__ == "__main__":
    main()
