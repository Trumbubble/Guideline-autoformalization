"""Regression tests for the terminology binder. Each names the failure it prevents.

Run: python3 test_bind.py     (no pytest, no network, no UMLS account)

Everything runs against `bind.fixture_index()` -- real codes, verified against
tx.fhir.org / NLM Clinical Tables / RxNav, chosen to include the near-misses
that make binding dangerous.
"""
import z3

import bind as b
import dsl
from bind import (ATC, LOINC, RXNORM, SNOMED, Binding, Candidate, Concept, Mention,
                  bind_statement, expand_query, fixture_index, propose_mentions,
                  registry_from_bindings, rerank_by_unit, select, variable_name, verify)

IX = fixture_index()
SENT = ("We recommend an ACE inhibitor in adults with diabetes and a urine "
        "albumin-to-creatinine ratio of 30 mg/g or higher.")

def codes(issues): return sorted(i.code for i in issues)

# ------------------------------------------------------- the core guarantee

def test_the_selector_returns_an_index_never_a_code():
    """A model asked for a code returns a plausible wrong number and nothing
    downstream can catch it. Here its output space is 0..k plus abstain."""
    m = Mention("diabetes", "condition")
    cands = IX.search("diabetes", "condition")
    assert cands
    typed_a_code = lambda p: {"choice": "73211009", "reason": "I know this one"}
    verdict, judge = select(m, SENT, cands, typed_a_code)
    assert judge == "heuristic:bad_choice"      # rejected, not parsed
    out_of_range = lambda p: {"choice": 99, "reason": "off the end"}
    assert select(m, SENT, cands, out_of_range)[1] == "heuristic:bad_choice"

def test_abstain_becomes_a_refusal_not_a_guess():
    m = Mention("frailty", "condition")
    abstainer = lambda p: {"choice": -1, "reason": "no candidate is frailty"}
    r = b.bind(m, "Assess frailty before starting treatment.", IX, abstainer)
    assert isinstance(r, dsl.Refusal) and r.reason_code == "unbound_concept"
    assert "frailty" in r.reason_text

def test_a_code_outside_the_index_is_rejected_even_if_selected():
    """Structurally the selector cannot invent a code, but a stale cache can.
    The gate is re-run against the index regardless."""
    ghost = Concept(SNOMED, "99999999009", "Invented disorder", "condition")
    binding = Binding(variable="has_ghost", var_type="bool", role="observation",
                      mention="ghost", kind="condition", system=SNOMED,
                      code="99999999009", display="Invented disorder")
    assert "CODE_NOT_IN_INDEX" in codes(verify(binding, IX))
    assert not IX.validate(SNOMED, "99999999009")
    assert IX.validate(SNOMED, "73211009")

# ------------------------------------------------------------ minimal pairs

def test_unit_disambiguates_mass_ratio_from_molar_ratio():
    """LOINC 9318-7 is mg/g and 14585-4 is mg/mmol -- the same clinical
    threshold a factor of ~8.84 apart. The sentence states the unit, so the
    unit settles it with no model involved."""
    for unit, want in [("mg/g", "9318-7"), ("mg/mmol", "14585-4")]:
        m = Mention("albumin-to-creatinine ratio", "lab", unit=unit)
        r = b.bind(m, SENT, IX)
        assert isinstance(r, Binding), r.reason_text
        assert r.code == want and r.unit == unit
        assert r.system == LOINC

def test_no_unit_leaves_the_pair_unresolvable_and_it_abstains():
    """Without the unit, mass ratio and molar ratio are indistinguishable --
    they score identically. Refusing is the correct outcome: a coin-flip here
    is an 8.84x error that nothing downstream can detect."""
    m = Mention("albumin-to-creatinine ratio", "lab", unit=None)
    r = b.bind(m, "Measure the albumin-to-creatinine ratio.", IX)
    assert isinstance(r, dsl.Refusal), getattr(r, "code", r)
    assert r.reason_code == "unbound_concept"
    mass, molar = b.search_all(IX, m, 8)[:2]
    assert mass.score == molar.score                 # genuinely indistinguishable

def test_hyper_and_hypokalemia_are_not_confused():
    """Lexically almost identical, clinically opposite. Measured with SQLite
    FTS5: the query 'kalemia' ranks Hypokalemia ABOVE Hyperkalemia."""
    hyper = b.bind(Mention("hyperkalemia", "condition"), "avoid in hyperkalemia", IX)
    assert isinstance(hyper, Binding) and hyper.code == "14140009"
    hypo = b.bind(Mention("hypokalemia", "condition"), "correct hypokalemia", IX)
    assert isinstance(hypo, Binding) and hypo.code == "43339004"

def test_a_lone_candidate_is_not_evidence():
    """Measured against live LOINC: the query 'eGFR' returns 'Epidermal growth
    factor receptor' and nothing else. A selector that reads 'no competition'
    as confidence binds the wrong gene to a kidney-function threshold."""
    gene = Candidate(Concept(LOINC, "13659-8",
                             "Epidermal growth factor receptor [Mass/mass] in Tissue",
                             "lab"), score=4.0, why=("overlap=0.10",))
    verdict, _ = select(Mention("eGFR", "lab", unit="mL/min"), SENT, [gene])
    assert verdict["choice"] == -1 and "no positive evidence" in verdict["reason"]

def test_unit_mismatch_blocks_a_binding():
    molar = Candidate(Concept(LOINC, "14585-4", "Albumin/Creatinine [Molar ratio] in Urine",
                              "lab", unit="mg/mmol"), score=9.0, why=("exact_term",))
    m = Mention("albumin-to-creatinine ratio", "lab", unit="mg/g")
    verdict, _ = select(m, SENT, rerank_by_unit([molar], m))
    assert verdict["choice"] == -1 and "unit mismatch" in verdict["reason"]

def test_value_set_groupers_do_not_outrank_real_lab_codes():
    real = b._score("estimated glomerular filtration rate",
                    Concept(LOINC, "33914-3", "Glomerular filtration rate/1.73 sq M.predicted",
                            "lab"), 0.0)
    grouper = b._score("estimated glomerular filtration rate",
                       Concept(LOINC, "96083-1",
                               "HEDIS MY 2020 Value Set - Estimated Glomerular Filtration Rate",
                               "lab"), 0.0)
    assert "not_an_observation" in grouper.why and real.score > grouper.score

# ------------------------------------------------------------------- units

def test_ucum_comparison_is_dimensional_not_textual():
    """String equality is wrong here and the failure is silent. LOINC records
    9318-7 (albumin/creatinine MASS ratio) as `ug/mg{creat}` -- dimensionally
    identical to the `mg/g` a guideline writes. A textual test rejects the
    correct code, and has no way to distinguish it from `mg/mmol`, which is a
    molar ratio and a different quantity (~8.84x)."""
    assert b.units_match("ug/mg{creat}", "mg/g")          # same dimension AND scale
    assert b.units_match("mmol/L", "mmol/L")
    assert not b.units_match("mg/mmol{creat}", "mg/g")    # mass/amount vs mass/mass
    assert not b.units_match("mmol/mol{creat}", "mg/g")

def test_a_convertible_unit_is_preferred_but_never_rejected():
    """mmol/L vs mol/L is a conversion, not a different quantity: same
    dimension, different scale. (g/kg and mg/g are not an example -- they are
    the SAME ratio, 1e-3, and units_match rightly says so.)"""
    assert b.units_comparable("mmol/L", "mol/L")
    assert not b.units_match("mmol/L", "mol/L")
    assert b.units_match("g/kg", "mg/g")               # identical ratios
    assert not b.units_comparable("mg/mmol", "mg/g")   # mass/amount vs mass/mass

def test_annotations_are_stripped():
    assert b.ucum_key("mg/g{creat}") == b.ucum_key("mg/g")

def test_the_unit_in_the_sentence_picks_between_the_acr_codes():
    """The R5b minimal pair, against real LOINC spellings."""
    mass = Candidate(Concept(LOINC, "9318-7", "Albumin/Creatinine [Mass Ratio] in Urine",
                             "lab", unit="ug/mg{creat}"), 10.0, ("exact_term",))
    molar = Candidate(Concept(LOINC, "32294-1", "Albumin/Creatinine [Ratio] in Urine",
                              "lab", unit="mg/mmol{creat}"), 10.0, ("exact_term",))
    for unit, want in [("mg/g", "9318-7"), ("mg/mmol", "32294-1")]:
        m = Mention("urine albumin-to-creatinine ratio", "lab", unit=unit)
        top = b.rerank_by_unit([mass, molar], m)[0]
        assert top.concept.code == want, (unit, top.concept.code)

def test_loinc_loader_is_optional_and_says_where_to_get_it():
    import os
    if b.find_loinc_table():
        return                                  # a release is present; nothing to assert
    try:
        b.load_loinc(b.SqliteIndex(":memory:"), path="/nonexistent")
        assert False, "should have raised"
    except FileNotFoundError as exc:
        assert "loinc.org/downloads" in str(exc)

# --------------------------------------------------------------- retrieval

def test_abbreviations_are_expanded():
    assert "estimated glomerular filtration rate" in expand_query("eGFR")
    assert "angiotensin converting enzyme inhibitor" in expand_query("ACE inhibitor")
    assert expand_query("diabetes") == ["diabetes"]

def test_a_slash_compound_is_also_searched_as_its_halves():
    """Guideline prose joins concepts with a slash --
    "leukemia/lymphoblastic lymphoma" is two diseases, and no terminology has a
    concept for the pair. Splitting is ADDITIVE, so a meaningful slash keeps
    working."""
    qs = b.expand_query("leukemia/lymphoblastic lymphoma")
    assert "leukemia" in qs and "lymphoblastic lymphoma" in qs
    assert "leukemia/lymphoblastic lymphoma" in qs      # the compound survives

def test_a_unit_slash_is_not_split():
    """mg/g is division, not a compound -- splitting it would search for "mg"."""
    assert b.split_compound("mg/g") == []
    assert b.split_compound("mL/min") == []
    assert "Albumin/Creatinine" in b.expand_query("Albumin/Creatinine")

def test_plurals_do_not_block_a_match():
    """Guideline prose: 'an ACE inhibitor'. ATC: 'ACE INHIBITORS, PLAIN'."""
    r = b.bind(Mention("ACE inhibitor", "drug_class"), SENT, IX)
    assert isinstance(r, Binding) and r.code == "C09A" and r.system == ATC

def test_zero_overlap_candidates_are_discarded():
    """Measured against live tx.fhir.org: `$expand?filter=` does PREFIX
    matching, and on a miss returns arbitrary concepts rather than an empty
    set -- "central venous access device" retrieves *Spine board*, *Arrow*,
    *Atomizer*. Those look like results, so the selector would be choosing from
    noise. A candidate sharing no word with any query is not a match."""
    class Noisy:
        def search(self, phrase, kind, k=8):
            return [b._score(phrase, Concept(SNOMED, "156009", "Spine board", kind), 0.0),
                    b._score(phrase, Concept(SNOMED, "210005", "Arrow", kind), 0.0),
                    b._score(phrase, Concept(SNOMED, "52124006",
                                             "Central venous catheter", kind), 0.0)]
        def validate(self, s, c): return True
        def expand(self, s, c, limit=500): return ()
        def version(self, s): return None
    m = Mention("CVAD", "procedure", canonical="central venous catheter")
    got = [c.concept.term for c in b.search_all(Noisy(), m, 8)]
    assert got == ["Central venous catheter"], got

def test_a_canonical_form_is_searched_but_never_becomes_the_answer():
    """The model proposes a QUERY; the index still supplies the code. `text`
    stays verbatim so provenance is unaffected."""
    seen = []
    class Spy:
        def search(self, phrase, kind, k=8):
            seen.append(phrase)
            return IX.search(phrase, kind, k)
        def validate(self, s, c): return IX.validate(s, c)
        def expand(self, s, c, limit=500): return IX.expand(s, c, limit)
        def version(self, s): return IX.version(s)
    m = Mention("ACEi", "drug_class", canonical="ACE inhibitors")
    r = b.bind(m, "Start an ACEi.", Spy())
    assert "ACE inhibitors" in seen                 # the canonical was searched
    assert isinstance(r, Binding) and r.code == "C09A"
    assert r.mention == "ACEi"                      # provenance keeps the source words

def test_search_is_scoped_to_the_right_vocabulary():
    assert all(c.concept.system == LOINC for c in IX.search("potassium", "lab"))
    assert all(c.concept.system == SNOMED for c in IX.search("diabetes", "condition"))
    assert not IX.search("diabetes", "lab")            # wrong kind -> no hits

# ---------------------------------------------------------------- mentions

def test_mentions_are_verbatim_and_a_paraphrase_is_dropped():
    liar = lambda t: {"mentions": [{"text": "kidney impairment", "kind": "condition"},
                                   {"text": "diabetes", "kind": "condition"}]}
    ms = propose_mentions(SENT, liar)
    assert [m.text for m in ms] == ["diabetes"]        # the invented span is dropped

def test_overlapping_mentions_keep_the_longest():
    ms = propose_mentions(SENT)
    texts = [m.text for m in ms]
    assert any("albumin" in t for t in texts)
    assert "creatinine" not in texts                   # swallowed by the longer span

def test_units_are_read_off_the_sentence():
    m = next(m for m in propose_mentions(SENT) if m.kind == "lab")
    assert m.unit == "mg/g"
    assert variable_name(m).endswith("_mg_g")

def test_llm_failure_falls_back_rather_than_losing_the_concept():
    boom = lambda t: (_ for _ in ()).throw(RuntimeError("429"))
    assert propose_mentions(SENT, boom)                # mention proposal survives
    m = Mention("diabetes", "condition")
    verdict, judge = select(m, SENT, IX.search("diabetes", "condition"), boom)
    assert judge == "heuristic:llm_error" and verdict["choice"] == 0

# ---------------------------------------------------------------- verify

def test_verify_flags_the_things_that_rot_silently():
    r = b.bind(Mention("diabetes", "condition"), SENT, IX)
    assert isinstance(r, Binding) and verify(r, IX) == []
    unversioned = Binding(variable="on_x", var_type="bool", role="action", mention="x",
                          kind="drug_class", system=ATC, code="C09A", display="X")
    iss = {i.code: i.severity for i in verify(unversioned, IX)}
    assert iss.get("UNVERSIONED_BINDING") == "warn"
    assert iss.get("SINGLE_CODE_FOR_A_SET") == "warn"
    wrong = Binding(variable="k", var_type="real", role="observation", mention="k",
                    kind="lab", system=SNOMED, code="73211009", display="Diabetes mellitus")
    assert "WRONG_SYSTEM_FOR_KIND" in codes(verify(wrong, IX))

def test_binding_uri_is_version_pinned():
    r = b.bind(Mention("diabetes", "condition"), SENT, IX)
    assert r.binding_uri.endswith("|20250201")

def test_a_condition_binds_to_a_set_not_a_single_code():
    """'Diabetes' is not one SNOMED code. A patient recorded under a sibling
    term silently fails a rule bound to the parent alone."""
    r = b.bind(Mention("diabetes", "condition"), SENT, IX)
    assert len(r.expansion) > 1 and "44054006" in r.expansion   # type 2 included

# ---------------------------------------------------------------- registry

def test_the_binder_output_is_a_usable_dsl_registry():
    bindings, refusals = bind_statement(SENT, IX)
    reg = registry_from_bindings(bindings, b.FIXTURE_PLAUSIBLE)
    assert {"has_diabetes", "on_ace_inhibitor"} <= set(reg.names())
    assert reg.role_of("on_ace_inhibitor") == "action"
    assert reg.role_of("has_diabetes") == "observation"
    assert reg["has_diabetes"].binding.endswith("|20250201")
    assert reg["has_diabetes"].codes                       # the value set came through

def test_a_rule_written_over_the_bound_registry_typechecks_and_solves():
    """The whole point: what comes out of stage 2 is the closed vocabulary
    stage 4 is constrained to."""
    bindings, _ = bind_statement(SENT, IX)
    reg = registry_from_bindings(bindings, b.FIXTURE_PLAUSIBLE)
    acr = next(n for n in reg.names() if n.startswith("albumin"))
    rule = dsl.Rule("kdigo-3.1",
                    dsl.And([dsl.Cmp("has_diabetes", "==", True),
                             dsl.Cmp(acr, ">=", 30)]),
                    dsl.Cmp("on_ace_inhibitor", "==", True),
                    "recommend", "ACEi", source_page=1, source_span=SENT)
    assert dsl.validate_rule(rule, reg) == []
    assert dsl.check_nondegenerate(rule, reg) == []
    env = dsl.make_env(reg)
    s = z3.Solver(); s.add(dsl.domain_axioms(reg, env))
    s.add(z3.Not(dsl.rule_to_z3(rule, env)))
    assert s.check() == z3.sat                             # a violating patient exists

def test_unbindable_concepts_land_in_the_residue():
    text = "Refer high-risk patients with frailty for specialist review."
    bindings, refusals = bind_statement(text, IX)
    assert all(r.reason_code == "unbound_concept" for r in refusals)
    assert not any(bd.mention in {"high-risk", "frailty"} for bd in bindings)


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:
            failed.append(name)
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)


# ------------------------------------- semantic fallback (R32)

class _StubSemantic:
    """Stands in for the vector index. The real one is 974 MB and loading it
    made the suite four times slower for no signal about the LOGIC, which is
    what these tests are about: when it is consulted, and how its results are
    combined."""
    model_name = "stub"
    def __init__(self, hits=None):
        self.hits = hits or {}
        self.queries = []
    def available(self, kind):
        return True
    def search(self, phrase, kind, k=8, min_similarity=0.0):
        self.queries.append((phrase, kind))
        return self.hits.get((phrase.lower(), kind), [])

def _with_stub(stub, fn):
    saved = dict(b._SEMANTIC)
    b._SEMANTIC.update({"index": [stub], "tried": True, "why": ""})
    try:
        return fn()
    finally:
        b._SEMANTIC.clear(); b._SEMANTIC.update(saved)

def test_an_exact_lexical_match_skips_the_semantic_index():
    """It is a FALLBACK. A semantic near-miss must never displace a lexical
    exact match, and the cost belongs off the common path."""
    stub = _StubSemantic()
    def run():
        m = b.Mention("diabetes", "condition", 0, 8)
        return b.search_all(IX, m, 8)
    _with_stub(stub, run)
    assert stub.queries == []          # never consulted

def test_a_lexical_miss_consults_the_semantic_index():
    """"kidney function" retrieves *Decreased kidney function* -- something,
    but not the concept -- so "returned nothing" is the wrong trigger. The
    absence of an EXACT term match is the right one."""
    stub = _StubSemantic({("kidney function", "lab"): [
        ("11953005", b.SNOMED, "Renal function", 0.954)]})
    ix = b.fixture_index(); ix.semantic = True
    def run():
        m = b.Mention("kidney function", "lab", 0, 15)
        return b.search_all(ix, m, 8)
    out = _with_stub(stub, run)
    assert stub.queries, "semantic index was never consulted"
    assert any(c.concept.code == "11953005" for c in out)

def test_semantic_candidates_are_marked_as_such():
    """A reviewer must be able to tell a meaning-match from a term-match."""
    stub = _StubSemantic({("kidney function", "lab"): [
        ("11953005", b.SNOMED, "Renal function", 0.954)]})
    ix = b.fixture_index(); ix.semantic = True
    out = _with_stub(stub, lambda: b.search_all(
        ix, b.Mention("kidney function", "lab", 0, 15), 8))
    hit = next(c for c in out if c.concept.code == "11953005")
    assert any("semantic" in w for w in hit.why), hit.why

def test_a_broadening_query_matching_is_not_an_exact_match():
    """The head-noun backoff adds "analgesia" for "epidural analgesia", and the
    generic concept `Analgesia` matched THAT -- which read as "found it" and
    suppressed the semantic search. The trigger tests the mention, not the
    expansions."""
    stub = _StubSemantic({("epidural analgesia", "procedure"): [
        ("18946005", b.SNOMED, "Epidural anaesthesia", 0.857)]})
    ix = b.fixture_index()
    ix.add([b.Concept(b.SNOMED, "38433004", "Analgesia", "procedure")])
    ix.semantic = True
    out = _with_stub(stub, lambda: b.search_all(
        ix, b.Mention("epidural analgesia", "procedure", 0, 18), 8))
    assert any(c.concept.code == "18946005" for c in out)

def test_the_fixture_index_never_reaches_for_meaning():
    """A fixture is a closed world: "not in it" is the answer, not a failure."""
    assert b.fixture_index().semantic is False

def test_an_unavailable_index_is_not_an_absent_concept():
    """`SemanticUnavailable` is raised rather than returning [] so a caller
    cannot mistake "not installed" for "no such concept" -- the failure class
    this project keeps rediscovering (R27)."""
    import semantic
    ix = semantic.SemanticIndex("/nonexistent.db", "stub-model")
    try:
        ix.search("anything", "lab")
    except semantic.SemanticUnavailable as exc:
        assert "semantic index" in str(exc) or "no " in str(exc)
    else:
        raise AssertionError("returned instead of raising")

# ------------------------------------------- retrieval: category-word noise

def test_a_category_word_is_not_evidence_of_a_match():
    """BM25 rewards a RARE token, and a clinically empty word can be very rare:
    "regimens" occurs in 6 concepts of the real index, "insulin" in 1,638. So
    "Basal insulin regimen" retrieved *Transfeminine Regimens, All*,
    *Transmasculine Regimens, All* and nothing else -- the entire candidate
    list decided by the one word carrying no clinical meaning, and the drug the
    sentence is about never proposed to the selector at all. Three statements
    in one document refused on this, with the model abstaining correctly each
    time because the right concept was never shown to it."""
    ix = b.SqliteIndex(":memory:")
    ix.semantic = False
    ix.add([b.Concept(system=b.VSAC, code="1.1", kind="drug_class",
                         term="Transfeminine Regimens, All"),
            b.Concept(system=b.ATC, code="D049528", kind="drug_class",
                         term="Insulin, Long-Acting")])
    cands = b.search_all(ix, b.Mention("Basal insulin regimen", "drug_class"), 8)
    terms = [c.concept.term for c in cands]
    assert "Insulin, Long-Acting" in terms
    assert "Transfeminine Regimens, All" not in terms, (
        "sharing only 'regimen' is not sharing a concept")

def test_stripping_leaves_a_phrase_alone_when_it_would_gut_it():
    assert b.strip_generic_modifiers("Basal insulin regimen") == "basal insulin"
    assert b.strip_generic_modifiers("regimen") is None, "nothing would be left"
    assert b.strip_generic_modifiers("insulin") is None, "nothing to strip"
    assert b.strip_generic_modifiers("mechanical thrombectomy") is None

def test_a_neighbouring_kind_never_outranks_the_kind_asked_for():
    """The lexical path penalised fallback-kind candidates and the semantic path
    did not, so SNOMED `Insulin` (a drug, cosine 0.78) outranked ATC
    `Insulin, Long-Acting` (a drug CLASS, 0.61) and filled every slot before the
    only candidate that denotes a class could appear."""
    assert b.FALLBACK_KIND_PENALTY > 0
    import inspect
    src = inspect.getsource(b.semantic_candidates)
    assert "FALLBACK_KIND_PENALTY" in src, (
        "the semantic path must apply the same penalty as the lexical one")
