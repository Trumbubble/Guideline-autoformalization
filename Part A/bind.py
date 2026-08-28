"""
Terminology binder: clinical phrase -> registry variable with a bound code.

Stage 2 of Track A. It does NOT look codes up on demand for the compiler; it
BUILDS the closed vocabulary (`dsl.Registry`) that the compiler is then
constrained to. Anything it cannot bind becomes a `Refusal(unbound_concept)`
and is part of the measured residue, not an error log.

    statement text
      -> propose_mentions   verbatim spans + a kind guess
      -> index.search       candidates from a real terminology
      -> select             the model returns an INDEX or ABSTAIN, never a code
      -> verify             the chosen code must exist in the index
      -> Binding | Refusal
      -> registry_from_bindings -> dsl.Registry

THE MODEL NEVER TYPES A CODE. Asked for "the SNOMED code for albuminuria" a
model returns a well-formed, plausible, sometimes-wrong number, and nothing
downstream can catch it: the CQL compiles, the solver runs, the verdict is
confidently wrong. Here the selector's output space is `0..k` plus ABSTAIN, so
a hallucinated code cannot be expressed. (Measured while writing this file:
SNOMED 29738008 is *Proteinuria*, not *Albuminuria* -- a mapping I had wrong
from memory and the lookup corrected.)

RETRIEVAL IS LEXICAL AND THAT IS NOT ENOUGH. Clinical terminology is full of
minimal pairs where one morpheme flips the meaning. Measured with SQLite FTS5:
the query "kalemia" ranks *Hypokalemia* ABOVE *Hyperkalemia*. So retrieval
proposes and the selector -- which sees the whole guideline sentence -- decides.
"""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Callable, Iterable, Protocol

import dsl

# ---------------------------------------------------------------- systems

SNOMED = "http://snomed.info/sct"
LOINC = "http://loinc.org"
RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
ATC = "http://www.whocc.no/atc"

SYSTEM_NAMES = {SNOMED: "SNOMED CT", LOINC: "LOINC", RXNORM: "RxNorm", ATC: "ATC"}

# What kind of thing a phrase denotes decides which vocabulary can answer.
Kind = str      # lab | condition | drug | drug_class | procedure

KIND_SYSTEMS: dict[Kind, tuple[str, ...]] = {
    "lab":        (LOINC,),
    "condition":  (SNOMED,),
    "procedure":  (SNOMED,),
    "drug":       (RXNORM,),
    "drug_class": (ATC, RXNORM),
}

# A lab is a measured quantity; everything else is a yes/no about the patient
# or the plan. `role` is what Track B needs to write frame axioms.
KIND_VARTYPE: dict[Kind, str] = {"lab": "real", "condition": "bool", "procedure": "bool",
                                 "drug": "bool", "drug_class": "bool"}
KIND_ROLE: dict[Kind, str] = {"lab": "observation", "condition": "observation",
                              "procedure": "action", "drug": "action",
                              "drug_class": "action"}
KIND_PREFIX: dict[Kind, str] = {"condition": "has_", "drug": "on_", "drug_class": "on_",
                                "procedure": "", "lab": ""}

# ---------------------------------------------------------------- model

@dataclass(frozen=True)
class Concept:
    system: str
    code: str
    term: str
    kind: Kind
    version: str | None = None
    unit: str | None = None

    @property
    def curie(self) -> str:
        return f"{SYSTEM_NAMES.get(self.system, self.system)}:{self.code}"

@dataclass(frozen=True)
class Candidate:
    concept: Concept
    score: float
    why: tuple[str, ...] = ()       # which signals fired; the audit trail

@dataclass(frozen=True)
class Mention:
    """A concept phrase, verbatim from the statement it was found in."""
    text: str
    kind: Kind
    char_start: int = 0
    char_end: int = 0
    unit: str | None = None         # the unit the statement used, if any

@dataclass(frozen=True)
class Binding:
    """A registry variable with a terminology binding that has been verified."""
    variable: str
    var_type: str
    role: str
    mention: str
    kind: Kind
    system: str
    code: str
    display: str
    version: str | None = None
    unit: str | None = None
    valueset_url: str | None = None
    expansion: tuple[str, ...] = ()      # member codes, when the concept is a set
    candidates: tuple[str, ...] = ()     # every option shown to the selector
    selected_index: int = -1
    selector: str = "none"
    reason: str = ""

    @property
    def binding_uri(self) -> str:
        """What goes in VarDef.binding. Version-pinned: value sets drift, and a
        verdict that silently changes when the terminology updates is a
        documented CDS malfunction mode (Wright 2018)."""
        base = self.valueset_url or f"{self.system}|{self.code}"
        return f"{base}|{self.version}" if self.version else base

# ---------------------------------------------------------------- index

class TerminologyIndex(Protocol):
    def search(self, phrase: str, kind: Kind, k: int = 8) -> list[Candidate]: ...
    def validate(self, system: str, code: str) -> bool: ...
    def expand(self, system: str, code: str, limit: int = 500) -> tuple[str, ...]: ...
    def version(self, system: str) -> str | None: ...

SCHEMA = """
CREATE TABLE IF NOT EXISTS concept(
  system TEXT NOT NULL, code TEXT NOT NULL, term TEXT NOT NULL,
  kind TEXT NOT NULL, version TEXT, unit TEXT, is_preferred INTEGER DEFAULT 1,
  PRIMARY KEY (system, code, term));
CREATE INDEX IF NOT EXISTS idx_concept_kind ON concept(kind);
CREATE VIRTUAL TABLE IF NOT EXISTS concept_fts USING fts5(
  term, system UNINDEXED, code UNINDEXED, kind UNINDEXED, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS expansion(
  system TEXT NOT NULL, code TEXT NOT NULL, member_system TEXT NOT NULL,
  member_code TEXT NOT NULL, PRIMARY KEY (system, code, member_code));
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""

_TOKEN = re.compile(r"[a-z0-9]+")

def _tokens(s: str) -> list[str]:
    s = unicodedata.normalize("NFKD", s).lower()
    return [t for t in _TOKEN.findall(s) if t not in {"of", "in", "the", "a", "an", "and"}]

def _stem(t: str) -> str:
    """Crudest possible plural stripping, for COMPARISON only -- never for the
    FTS query, where a prefix match already covers it. Guideline prose says
    "an ACE inhibitor"; ATC says "ACE INHIBITORS, PLAIN", and without this the
    two share no evidence and a correct binding is refused."""
    return t[:-1] if len(t) > 3 and t.endswith("s") and not t.endswith("ss") else t

def _stems(s: str) -> list[str]:
    return [_stem(t) for t in _tokens(s)]

class SqliteIndex:
    """Local terminology cache. Three jobs, and the third is the point:

    1. search -- FTS5 with BM25, no dependencies, works offline
    2. validate -- the deterministic gate a binding is checked against
    3. VERSION PIN -- the file is the terminology version. A reviewer re-running
       your pipeline gets the same codes, which a live terminology server
       cannot promise."""

    def __init__(self, path: str = ":memory:"):
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # -- writing -------------------------------------------------------
    def add(self, concepts: Iterable[Concept]) -> int:
        n = 0
        for c in concepts:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO concept(system,code,term,kind,version,unit) "
                "VALUES (?,?,?,?,?,?)",
                (c.system, c.code, c.term, c.kind, c.version, c.unit))
            if cur.rowcount:
                self.db.execute(
                    "INSERT INTO concept_fts(term,system,code,kind) VALUES (?,?,?,?)",
                    (c.term, c.system, c.code, c.kind))
                n += 1
        self.db.commit()
        return n

    def add_expansion(self, system: str, code: str,
                      members: Iterable[tuple[str, str]]) -> None:
        self.db.executemany(
            "INSERT OR IGNORE INTO expansion VALUES (?,?,?,?)",
            [(system, code, ms, mc) for ms, mc in members])
        self.db.commit()

    def set_version(self, system: str, version: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                        (f"version:{system}", version))
        self.db.commit()

    # -- reading -------------------------------------------------------
    def version(self, system: str) -> str | None:
        r = self.db.execute("SELECT value FROM meta WHERE key=?",
                            (f"version:{system}",)).fetchone()
        return r["value"] if r else None

    def validate(self, system: str, code: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM concept WHERE system=? AND code=? LIMIT 1",
            (system, code)).fetchone() is not None

    def expand(self, system: str, code: str, limit: int = 500) -> tuple[str, ...]:
        rows = self.db.execute(
            "SELECT member_code FROM expansion WHERE system=? AND code=? LIMIT ?",
            (system, code, limit)).fetchall()
        return tuple(r["member_code"] for r in rows)

    def search(self, phrase: str, kind: Kind, k: int = 8) -> list[Candidate]:
        toks = _tokens(phrase)
        if not toks:
            return []
        systems = KIND_SYSTEMS.get(kind, ())
        # Prefix-OR: "albumin creatinine" -> albumin* OR creatinine*. A guideline
        # phrase rarely matches a terminology term token-for-token, so requiring
        # every token (AND) loses far more than the noise it removes.
        q = " OR ".join(f'"{t}"*' for t in toks)
        rows = self.db.execute(
            "SELECT term, system, code, kind, bm25(concept_fts) AS bm "
            "FROM concept_fts WHERE concept_fts MATCH ? ORDER BY bm LIMIT ?",
            (q, max(k * 8, 40))).fetchall()
        out = []
        for r in rows:
            if systems and r["system"] not in systems:
                continue
            if r["kind"] != kind:
                continue
            meta = self.db.execute(
                "SELECT version, unit FROM concept WHERE system=? AND code=? AND term=?",
                (r["system"], r["code"], r["term"])).fetchone()
            c = Concept(r["system"], r["code"], r["term"], r["kind"],
                        meta["version"] if meta else None, meta["unit"] if meta else None)
            out.append(_score(phrase, c, -float(r["bm"])))
        out.sort(key=lambda c: -c.score)
        return _dedupe_candidates(out)[:k]

def _dedupe_candidates(cands: list[Candidate]) -> list[Candidate]:
    seen, out = set(), []
    for c in cands:
        key = (c.concept.system, c.concept.code)
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out

# LOINC contains grouper and administrative codes alongside observable
# quantities. "HEDIS MY 2020 Value Set - Estimated Glomerular Filtration Rate"
# out-ranks the actual eGFR result code on token overlap, but you cannot
# threshold a value set.
NOT_AN_OBSERVATION = re.compile(
    r"\b(value set|deprecated|panel|narrative|document|report|order set|"
    r"questionnaire|survey)\b", re.I)

def _score(phrase: str, c: Concept, lexical: float) -> Candidate:
    """Lexical rank plus explainable boosts. Deliberately simple: the score only
    has to get the right answer INTO the candidate list, not to the top -- the
    selector, which can see the guideline sentence, decides which one it is."""
    p, t = set(_stems(phrase)), set(_stems(c.term))
    score, why = lexical, []
    if _stems(phrase) == _stems(c.term):
        score += 10.0; why.append("exact_term")
    if p and p <= t:
        score += 3.0; why.append("all_query_tokens")
    if p and t and (p & t):
        cov = len(p & t) / len(p | t)
        score += 2.0 * cov; why.append(f"overlap={cov:.2f}")
    if len(t) > 3 * max(1, len(p)):
        score -= 1.0; why.append("term_much_longer")
    if NOT_AN_OBSERVATION.search(c.term):
        score -= 4.0; why.append("not_an_observation")
    return Candidate(c, round(score, 3), tuple(why))

# Guideline prose uses abbreviations; terminology indexes them badly or not at
# all. Searching the expansion as well as the abbreviation is deterministic and
# auditable, and it is what stops "eGFR" resolving to a genomics assay.
ABBREVIATIONS = {
    "egfr": "estimated glomerular filtration rate",
    "gfr": "glomerular filtration rate",
    "acr": "albumin creatinine ratio urine",
    "uacr": "albumin creatinine ratio urine",
    "acei": "angiotensin converting enzyme inhibitor",
    "ace inhibitor": "angiotensin converting enzyme inhibitor",
    "arb": "angiotensin receptor blocker",
    "hba1c": "hemoglobin A1c",
    "crcl": "creatinine clearance",
    "inr": "international normalized ratio",
    "ldl": "LDL cholesterol",
    "bp": "blood pressure",
    "ckd": "chronic kidney disease",
    "nsaid": "nonsteroidal anti-inflammatory",
    "sglt2 inhibitor": "sodium glucose cotransporter 2 inhibitor",
    "tpn": "total parenteral nutrition",
    "vte": "venous thromboembolism",
    "cvad": "central venous access device",
    "af": "atrial fibrillation",
}

def expand_query(phrase: str) -> list[str]:
    """The phrase, plus its expansion if it is a known abbreviation."""
    key = " ".join(_tokens(phrase))
    out = [phrase]
    if key in ABBREVIATIONS:
        out.append(ABBREVIATIONS[key])
    return out

def search_all(index: TerminologyIndex, mention: Mention, k: int) -> list[Candidate]:
    """Union over the phrase and its expansion. Union, not intersection: each
    query sees terms the other misses, and the selector decides between them."""
    found: list[Candidate] = []
    for q in expand_query(mention.text):
        found.extend(index.search(q, mention.kind, k=k))
    found.sort(key=lambda c: -c.score)
    return _dedupe_candidates(found)[:k]

def rerank_by_unit(cands: list[Candidate], mention: Mention) -> list[Candidate]:
    """Deterministic disambiguation before the model is asked anything.

    "ACR of 30 mg/g" and "ACR of 3.4 mg/mmol" are the same clinical threshold
    under different LOINC concepts (9318-7 mass ratio, 14585-4 MOLAR ratio,
    a factor of ~8.84 apart). The sentence states the unit, so the unit settles
    it -- no judgement required, and the reason is recorded either way."""
    if not mention.unit:
        return cands
    want, out = _unit_slug(mention.unit), []
    for c in cands:
        cu = c.concept.unit
        if cu and _unit_slug(cu) == want:
            out.append(replace(c, score=c.score + 4.0, why=c.why + ("unit_match",)))
        elif cu:
            out.append(replace(c, score=c.score - 2.0, why=c.why + ("unit_mismatch",)))
        else:
            out.append(replace(c, score=c.score - 0.5, why=c.why + ("no_unit_recorded",)))
    out.sort(key=lambda c: -c.score)
    return out

# ---------------------------------------------------------------- mentions

# Units seen next to a threshold in guideline prose. The unit is bound with the
# concept because it is the difference between two correct-looking rules:
# ACR >= 30 mg/g and ACR >= 3.4 mg/mmol are the same clinical threshold.
UNIT_RX = re.compile(
    r"(?<![\w/])(mg/g|mg/mmol|mg/dL|mg/dl|g/dL|mmol/L|mmol/l|mEq/L|umol/L|µmol/L|"
    r"mL/min/1\.73\s*m2|mL/min|ml/min|kg|mmHg|%|IU/L|U/L|ng/mL|pg/mL)(?![\w])")

def unit_near(text: str, at: int, window: int = 60) -> str | None:
    m = UNIT_RX.search(text, at, at + window)
    return m.group(1) if m else None

MENTION_SCHEMA = {
    "type": "object",
    "required": ["mentions"],
    "properties": {"mentions": {"type": "array", "items": {
        "type": "object",
        "required": ["text", "kind"],
        "additionalProperties": False,
        "properties": {
            "text": {"type": "string"},
            "kind": {"enum": ["lab", "condition", "drug", "drug_class", "procedure"]}}}}},
}

MENTION_INSTRUCTIONS = """\
List the clinical concepts this recommendation depends on: the labs it
thresholds, the conditions that make it apply, and the drugs, drug classes or
procedures it directs.

Copy each concept VERBATIM from the sentence. Do not expand abbreviations,
normalise wording, or invent a canonical name. Do not list severity words,
populations ("adults"), or vague qualifiers ("high-risk") -- those are not
bindable concepts and are handled elsewhere.

kind is one of: lab, condition, drug, drug_class, procedure."""

def build_mention_prompt(text: str) -> str:
    return f'{MENTION_INSTRUCTIONS}\n\nSentence:\n"""\n{text}\n"""'

# Offline fallback so the pipeline and its tests run with no model.
_HEURISTIC_MENTIONS: list[tuple[str, Kind]] = [
    (r"albumin[- ]to[- ]creatinine ratio|urine albumin[/-]creatinine|\bACR\b", "lab"),
    (r"\beGFR\b|estimated glomerular filtration rate", "lab"),
    (r"serum potassium|\bpotassium\b", "lab"),
    (r"\bHbA1c\b|haemoglobin A1c|hemoglobin A1c", "lab"),
    (r"serum creatinine|creatinine clearance", "lab"),
    (r"\bdiabetes\b(?: mellitus)?", "condition"),
    (r"\bhypertension\b", "condition"),
    (r"chronic kidney disease", "condition"),
    (r"\basthma\b", "condition"),
    (r"ACE inhibitor(?:s)?|angiotensin[- ]converting[- ]enzyme inhibitor(?:s)?", "drug_class"),
    (r"\bARB\b|angiotensin receptor blocker(?:s)?", "drug_class"),
    (r"SGLT2 inhibitor(?:s)?", "drug_class"),
    (r"\bNSAID(?:s)?\b", "drug_class"),
    (r"\bwarfarin\b|\bapixaban\b|\brivaroxaban\b|\bdigoxin\b|\bmetformin\b", "drug"),
    (r"refer(?:ral)? to nephrology|nephrology referral|dialysis", "procedure"),
]

def propose_mentions(text: str,
                     llm: Callable[[str], dict] | None = None) -> list[Mention]:
    """Concept spans, verbatim. Proposing SPANS is safe in a way proposing codes
    is not: a span is checkable against the source string, so a paraphrase is
    caught the same way the segmenter catches one."""
    if llm is None:
        found: list[Mention] = []
        for pat, kind in _HEURISTIC_MENTIONS:
            for m in re.finditer(pat, text, re.I):
                found.append(Mention(m.group(0), kind, m.start(), m.end(),
                                     unit_near(text, m.end()) if kind == "lab" else None))
        return _drop_overlaps(found)
    try:
        raw = llm(text)
    except Exception:
        return propose_mentions(text, None)
    out = []
    for m in (raw or {}).get("mentions", []):
        span, kind = (m.get("text") or "").strip(), m.get("kind")
        at = text.find(span)
        if not span or at < 0 or kind not in KIND_SYSTEMS:
            continue                     # not verbatim, or not a bindable kind
        out.append(Mention(span, kind, at, at + len(span),
                           unit_near(text, at + len(span)) if kind == "lab" else None))
    return _drop_overlaps(out)

def _drop_overlaps(ms: list[Mention]) -> list[Mention]:
    """Keep the longest span when two mentions overlap: 'urine
    albumin-to-creatinine ratio' beats the 'creatinine' inside it."""
    out: list[Mention] = []
    for m in sorted(ms, key=lambda m: (-(m.char_end - m.char_start), m.char_start)):
        if not any(m.char_start < o.char_end and o.char_start < m.char_end for o in out):
            out.append(m)
    return sorted(out, key=lambda m: m.char_start)

# ---------------------------------------------------------------- selection

SELECT_SCHEMA = {
    "type": "object",
    "required": ["choice", "reason"],
    "additionalProperties": False,
    "properties": {
        # An INDEX into the candidate list, or -1 to abstain. Never a code.
        "choice": {"type": "integer", "minimum": -1},
        "reason": {"type": "string"},
    },
}

SELECT_INSTRUCTIONS = """\
Choose which terminology concept the phrase refers to, IN THE CONTEXT of the
sentence it came from. Answer with the INDEX of one candidate, or -1 to abstain.

Abstain when no candidate is the concept, when the phrase is too vague to
have one ("high-risk", "adequate trial"), or when two candidates differ in a
way the sentence does not settle. Abstaining sends the statement to the
refusal report, which is a correct and expected outcome -- a wrong binding is
far worse, because nothing downstream can detect it.

Watch the near-misses: mass ratio vs molar ratio, hyper- vs hypo-, type 1 vs
type 2, with vs without complication, measurement vs finding."""

def build_select_prompt(mention: Mention, sentence: str,
                        cands: list[Candidate]) -> str:
    lines = [f"  [{i}] {c.concept.term}   ({c.concept.curie})"
             for i, c in enumerate(cands)]
    return (f"{SELECT_INSTRUCTIONS}\n\n"
            f'Sentence:\n"""\n{sentence}\n"""\n\n'
            f"Phrase: {mention.text!r}   (kind: {mention.kind}"
            + (f", unit in the sentence: {mention.unit}" if mention.unit else "") + ")\n\n"
            "Candidates:\n" + "\n".join(lines) + "\n  [-1] ABSTAIN")

# A candidate must show one of these to be bindable without a model. Being the
# only candidate is NOT evidence: measured against live LOINC, the query "eGFR"
# returns "Epidermal growth factor receptor ... by Molecular genetics method"
# and nothing else, and a selector that reads "no competition" as confidence
# binds the wrong gene to a kidney-function threshold.
POSITIVE_EVIDENCE = {"exact_term", "unit_match", "all_query_tokens"}

def _heuristic_select(mention: Mention, cands: list[Candidate]) -> dict:
    """Offline fallback. Abstains unless there is positive evidence AND no
    near-tie -- a near-tie is exactly the minimal-pair case."""
    if not cands:
        return {"choice": -1, "reason": "no candidates"}
    best = cands[0]
    evidence = set(best.why) & POSITIVE_EVIDENCE
    if not evidence:
        return {"choice": -1,
                "reason": f"no positive evidence for {best.concept.term!r}: the phrase "
                          f"matched only loosely ({', '.join(best.why) or 'nothing'})"}
    if "unit_mismatch" in best.why:
        return {"choice": -1,
                "reason": f"unit mismatch: sentence says {mention.unit}, "
                          f"{best.concept.term!r} is in {best.concept.unit}"}
    runner = cands[1].score if len(cands) > 1 else float("-inf")
    if best.score - runner < 1.0:
        return {"choice": -1,
                "reason": f"ambiguous: {best.concept.term!r} vs {cands[1].concept.term!r} "
                          f"({best.score:.2f} vs {runner:.2f})"}
    return {"choice": 0,
            "reason": f"{'/'.join(sorted(evidence))} ({best.score:.2f})"}

def select(mention: Mention, sentence: str, cands: list[Candidate],
           llm: Callable[[str], dict] | None = None) -> tuple[dict, str]:
    if llm is None:
        return _heuristic_select(mention, cands), "heuristic"
    try:
        v = llm(build_select_prompt(mention, sentence, cands))
    except Exception as exc:
        v = _heuristic_select(mention, cands)
        v["reason"] = f"llm error, fell back to heuristic: {exc}"
        return v, "heuristic:llm_error"
    choice = v.get("choice")
    if not isinstance(choice, int) or not (-1 <= choice < len(cands)):
        v = _heuristic_select(mention, cands)
        v["reason"] = f"selector returned an out-of-range choice; fell back to heuristic"
        return v, "heuristic:bad_choice"
    return v, "llm"

# ---------------------------------------------------------------- binding

def _slug(text: str, max_tokens: int = 4) -> str:
    toks = _tokens(text)[:max_tokens]
    return "_".join(toks) or "concept"

def _unit_slug(unit: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", unit.lower()).strip("_")

def variable_name(mention: Mention) -> str:
    """Deterministic, and named after the GUIDELINE's words rather than the
    terminology's, so a reader can trace the variable back to the sentence.
    Labs carry their unit in the name because the unit is part of the meaning."""
    base = KIND_PREFIX.get(mention.kind, "") + _slug(mention.text)
    if mention.kind == "lab" and mention.unit:
        base = f"{base}_{_unit_slug(mention.unit)}"
    return base

def bind(mention: Mention, sentence: str, index: TerminologyIndex,
         llm: Callable[[str], dict] | None = None, k: int = 8,
         ref_id: str = "") -> Binding | dsl.Refusal:
    """One phrase -> a verified Binding, or a refusal. Never a guessed code."""
    cands = rerank_by_unit(search_all(index, mention, k), mention)
    shown = tuple(f"{c.concept.term} ({c.concept.curie})" for c in cands)
    verdict, selector = select(mention, sentence, cands, llm)
    choice, reason = verdict.get("choice", -1), verdict.get("reason", "")

    if choice == -1 or not cands:
        return dsl.Refusal(
            rule_id=ref_id or mention.text, reason_code="unbound_concept",
            reason_text=(f"no terminology binding for {mention.text!r} "
                         f"({mention.kind}): {reason}"),
            source_span=mention.text, confidence=None)

    c = cands[choice].concept
    # The gate. Structurally the selector cannot invent a code, but a stale
    # cache or a mis-wired index still can, so the code is re-checked.
    if not index.validate(c.system, c.code):
        return dsl.Refusal(
            rule_id=ref_id or mention.text, reason_code="unbound_concept",
            reason_text=(f"selected {c.curie} for {mention.text!r} but it is not in "
                         f"the index -- binding rejected"),
            source_span=mention.text)

    expansion = ()
    if mention.kind in {"condition", "drug_class", "procedure"}:
        expansion = index.expand(c.system, c.code)
    return Binding(
        variable=variable_name(mention), var_type=KIND_VARTYPE[mention.kind],
        role=KIND_ROLE[mention.kind], mention=mention.text, kind=mention.kind,
        system=c.system, code=c.code, display=c.term,
        version=c.version or index.version(c.system),
        unit=mention.unit or c.unit, expansion=expansion,
        candidates=shown, selected_index=choice, selector=selector, reason=reason)

def bind_statement(text: str, index: TerminologyIndex,
                   mention_llm: Callable[[str], dict] | None = None,
                   select_llm: Callable[[str], dict] | None = None,
                   ref_id: str = "") -> tuple[list[Binding], list[dsl.Refusal]]:
    bindings, refusals = [], []
    for m in propose_mentions(text, mention_llm):
        r = bind(m, text, index, select_llm, ref_id=ref_id)
        (bindings if isinstance(r, Binding) else refusals).append(r)
    return bindings, refusals

# ---------------------------------------------------------------- verify

def verify(b: Binding, index: TerminologyIndex) -> list[dsl.Issue]:
    """Deterministic re-check, separable from the model entirely. This is what
    turns 'we told it not to hallucinate' into a checkable property."""
    out: list[dsl.Issue] = []
    if not index.validate(b.system, b.code):
        out.append(dsl.Issue(b.variable, "CODE_NOT_IN_INDEX",
                             f"{SYSTEM_NAMES.get(b.system, b.system)}:{b.code} is not in the index",
                             "retrieve again, or refuse with unbound_concept"))
    if b.system not in KIND_SYSTEMS.get(b.kind, ()):
        out.append(dsl.Issue(b.variable, "WRONG_SYSTEM_FOR_KIND",
                             f"{b.kind} bound to {SYSTEM_NAMES.get(b.system, b.system)}",
                             f"expected {[SYSTEM_NAMES.get(s, s) for s in KIND_SYSTEMS.get(b.kind, ())]}"))
    if not b.version:
        out.append(dsl.Issue(b.variable, "UNVERSIONED_BINDING",
                             "no terminology version recorded",
                             "value sets drift; an unversioned binding changes meaning "
                             "silently between runs", severity="warn"))
    if b.kind == "lab" and not b.unit:
        out.append(dsl.Issue(b.variable, "MISSING_UNIT",
                             f"no unit for lab {b.mention!r}",
                             "mg/g vs mg/mmol is a factor of ~8.84 and both typecheck",
                             severity="warn"))
    if b.kind in {"condition", "drug_class"} and not b.expansion:
        out.append(dsl.Issue(b.variable, "SINGLE_CODE_FOR_A_SET",
                             f"{b.mention!r} bound to one code with no expansion",
                             "a patient recorded under a sibling term will silently fail "
                             "the rule; bind to a value set", severity="warn"))
    return out

# ---------------------------------------------------------------- registry

def registry_from_bindings(bindings: Iterable[Binding],
                           plausible: dict[str, tuple[float, float]] | None = None,
                           events: list | None = None) -> dsl.Registry:
    """The payoff: the binder's output IS the DSL's closed vocabulary. Whatever
    is not in here cannot be written by the synthesizer.

    `plausible` is supplied by the caller, not the terminology -- LOINC does not
    publish physiological ranges. It is load-bearing twice over (unit-slip
    warnings, and the solver's domain axioms), so it is curated per registry."""
    plausible = plausible or {}
    seen: dict[str, dsl.VarDef] = {}
    for b in bindings:
        if b.variable in seen:
            continue
        seen[b.variable] = dsl.VarDef(
            name=b.variable, type=b.var_type, unit=b.unit, description=b.display,
            binding=b.binding_uri, role=b.role,
            plausible=plausible.get(b.variable),
            codes=b.expansion or ((b.code,) if b.var_type == "code" else ()))
    return dsl.Registry(list(seen.values()), events or [])

def binding_to_dict(b: Binding) -> dict:
    d = {k: getattr(b, k) for k in
         ("variable", "var_type", "role", "mention", "kind", "system", "code",
          "display", "version", "unit", "valueset_url", "selected_index",
          "selector", "reason")}
    d["expansion"] = list(b.expansion)
    d["candidates"] = list(b.candidates)
    d["binding_uri"] = b.binding_uri
    return d

# ---------------------------------------------------------------- remote

# Every source below is reachable with NO ACCOUNT. A UMLS/UTS licence buys
# VSAC's curated, steward-versioned value sets -- better than a computed
# is-a expansion, and what the AHRQ CDS Connect reference artifacts bind to --
# but nothing here is blocked without it.
TX_FHIR = "https://tx.fhir.org/r4"
CLINICAL_TABLES = "https://clinicaltables.nlm.nih.gov/api"
RXNAV = "https://rxnav.nlm.nih.gov/REST"

def _get_json(url: str, params: dict, timeout: int = 30, fhir: bool = False):
    import urllib.parse, urllib.request
    full = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
    headers = {"User-Agent": "GuidelineForge/0.1 (research; terminology binder)"}
    if fhir:
        headers["Accept"] = "application/fhir+json"
    with urllib.request.urlopen(urllib.request.Request(full, headers=headers),
                                timeout=timeout) as r:
        return json.loads(r.read())

class RemoteIndex:
    """Live terminology over public endpoints, routed by kind.

    Use it to BUILD a SqliteIndex, not to serve queries in a loop: tx.fhir.org
    is a shared public service, and a run that re-queries it is neither polite
    nor reproducible."""

    def __init__(self, tx: str = TX_FHIR, tables: str = CLINICAL_TABLES,
                 rxnav: str = RXNAV):
        self.tx, self.tables, self.rxnav = tx, tables, rxnav
        self._versions: dict[str, str | None] = {}

    # -- SNOMED, via a FHIR terminology server --------------------------
    def _snomed_search(self, phrase: str, kind: Kind, k: int) -> list[Candidate]:
        d = _get_json(f"{self.tx}/ValueSet/$expand",
                      {"url": f"{SNOMED}?fhir_vs", "filter": phrase, "count": k},
                      fhir=True)
        ver = self.version(SNOMED)
        out = []
        for c in d.get("expansion", {}).get("contains", []):
            out.append(_score(phrase, Concept(SNOMED, c["code"], c.get("display", ""),
                                              kind, ver), 0.0))
        return out

    def _loinc_search(self, phrase: str, k: int) -> list[Candidate]:
        d = _get_json(f"{self.tables}/loinc_items/v3/search",
                      {"terms": phrase, "maxList": k,
                       "df": "LOINC_NUM,LONG_COMMON_NAME,EXAMPLE_UCUM_UNITS"})
        out = []
        for row in d[3]:
            code, name = row[0], row[1]
            unit = row[2] if len(row) > 2 and row[2] else None
            out.append(_score(phrase, Concept(LOINC, code, name, "lab", None, unit), 0.0))
        return out

    def _rx_search(self, phrase: str, kind: Kind, k: int) -> list[Candidate]:
        out = []
        if kind == "drug_class":
            d = _get_json(f"{self.rxnav}/rxclass/class/byName.json",
                          {"className": phrase})
            for ci in (d.get("rxclassMinConceptList", {})
                         .get("rxclassMinConcept", []) or [])[:k]:
                out.append(_score(phrase, Concept(ATC, ci["classId"], ci["className"],
                                                  kind), 0.0))
        else:
            d = _get_json(f"{self.rxnav}/drugs.json", {"name": phrase})
            for g in (d.get("drugGroup", {}).get("conceptGroup", []) or []):
                for c in (g.get("conceptProperties", []) or [])[:k]:
                    out.append(_score(phrase, Concept(RXNORM, c["rxcui"], c["name"],
                                                      kind), 0.0))
        return out

    def search(self, phrase: str, kind: Kind, k: int = 8) -> list[Candidate]:
        try:
            if kind == "lab":
                cands = self._loinc_search(phrase, k)
            elif kind in {"drug", "drug_class"}:
                cands = self._rx_search(phrase, kind, k)
            else:
                cands = self._snomed_search(phrase, kind, k)
        except Exception:
            return []
        cands.sort(key=lambda c: -c.score)
        return _dedupe_candidates(cands)[:k]

    def validate(self, system: str, code: str) -> bool:
        try:
            if system == SNOMED:
                d = _get_json(f"{self.tx}/CodeSystem/$validate-code",
                              {"url": SNOMED, "code": code}, fhir=True)
                p = {x["name"]: x for x in d.get("parameter", [])}
                return bool(p.get("result", {}).get("valueBoolean"))
            if system == LOINC:
                d = _get_json(f"{self.tables}/loinc_items/v3/search",
                              {"terms": code, "maxList": 20, "df": "LOINC_NUM"})
                return any(r[0] == code for r in d[3])
            if system == RXNORM:
                d = _get_json(f"{self.rxnav}/rxcui/{code}/properties.json", {})
                return bool(d.get("properties"))
            if system == ATC:
                return bool(self.expand(ATC, code, limit=1))
        except Exception:
            return False
        return False

    def expand(self, system: str, code: str, limit: int = 500) -> tuple[str, ...]:
        try:
            if system == SNOMED:
                d = _get_json(f"{self.tx}/ValueSet/$expand",
                              {"url": f"{SNOMED}?fhir_vs=isa/{code}", "count": limit},
                              fhir=True)
                return tuple(c["code"] for c in
                             d.get("expansion", {}).get("contains", []))
            if system == ATC:
                d = _get_json(f"{self.rxnav}/rxclass/classMembers.json",
                              {"classId": code, "relaSource": "ATC"})
                return tuple(m["minConcept"]["rxcui"] for m in
                             (d.get("drugMemberGroup", {}).get("drugMember", []) or [])[:limit])
        except Exception:
            return ()
        return ()

    def version(self, system: str) -> str | None:
        if system in self._versions:
            return self._versions[system]
        v = None
        try:
            if system == SNOMED:
                d = _get_json(f"{self.tx}/CodeSystem/$lookup",
                              {"system": SNOMED, "code": "138875005"}, fhir=True)
                for p in d.get("parameter", []):
                    if p.get("name") == "version":
                        v = p.get("valueString")
        except Exception:
            v = None
        self._versions[system] = v
        return v

class CachedIndex:
    """SqliteIndex first; on a miss, ask the remote and write through.

    The cache is not an optimisation -- it is the version pin. A reviewer
    re-running your pipeline against the .db gets the codes you got, which a
    live terminology server cannot promise."""

    def __init__(self, local: SqliteIndex, remote: RemoteIndex | None = None):
        self.local, self.remote = local, remote

    def search(self, phrase: str, kind: Kind, k: int = 8) -> list[Candidate]:
        hits = self.local.search(phrase, kind, k)
        if hits or self.remote is None:
            return hits
        fresh = self.remote.search(phrase, kind, k)
        if fresh:
            self.local.add(c.concept for c in fresh)
            ver = self.remote.version(fresh[0].concept.system)
            if ver:
                self.local.set_version(fresh[0].concept.system, ver)
        return self.local.search(phrase, kind, k) or fresh

    def validate(self, system: str, code: str) -> bool:
        if self.local.validate(system, code):
            return True
        return bool(self.remote and self.remote.validate(system, code))

    def expand(self, system: str, code: str, limit: int = 500) -> tuple[str, ...]:
        local = self.local.expand(system, code, limit)
        if local or self.remote is None:
            return local
        members = self.remote.expand(system, code, limit)
        if members:
            self.local.add_expansion(system, code, [(system, m) for m in members])
        return members

    def version(self, system: str) -> str | None:
        return self.local.version(system) or (self.remote.version(system)
                                              if self.remote else None)

# ---------------------------------------------------------------- fixture

# Real codes, verified against tx.fhir.org / NLM Clinical Tables / RxNav while
# writing this file. Deliberately contains the near-misses that make binding
# hard, so the tests exercise the failure mode rather than a happy path:
#   9318-7 vs 14585-4   mass ratio (mg/g) vs MOLAR ratio (mg/mmol)
#   14140009 vs 43339004  HYPERkalemia vs HYPOkalemia
#   44054006 vs 46635009  type 2 vs type 1 diabetes
FIXTURE_CONCEPTS = [
    Concept(LOINC, "9318-7", "Albumin/Creatinine [Mass Ratio] in Urine", "lab",
            "2.78", "mg/g"),
    Concept(LOINC, "14585-4", "Albumin/Creatinine [Molar ratio] in Urine", "lab",
            "2.78", "mg/mmol"),
    Concept(LOINC, "33914-3", "Glomerular filtration rate/1.73 sq M.predicted", "lab",
            "2.78", "mL/min/1.73m2"),
    Concept(LOINC, "2823-3", "Potassium [Moles/volume] in Serum or Plasma", "lab",
            "2.78", "mmol/L"),
    Concept(LOINC, "4548-4", "Hemoglobin A1c/Hemoglobin.total in Blood", "lab",
            "2.78", "%"),
    Concept(SNOMED, "73211009", "Diabetes mellitus", "condition", "20250201"),
    Concept(SNOMED, "44054006", "Type 2 diabetes mellitus", "condition", "20250201"),
    Concept(SNOMED, "46635009", "Type 1 diabetes mellitus", "condition", "20250201"),
    Concept(SNOMED, "38341003", "Hypertensive disorder", "condition", "20250201"),
    Concept(SNOMED, "709044004", "Chronic kidney disease", "condition", "20250201"),
    Concept(SNOMED, "195967001", "Asthma", "condition", "20250201"),
    Concept(SNOMED, "14140009", "Hyperkalemia", "condition", "20250201"),
    Concept(SNOMED, "43339004", "Hypokalemia", "condition", "20250201"),
    Concept(SNOMED, "274769005", "Albuminuria", "condition", "20250201"),
    Concept(SNOMED, "396487001", "Sensitivity to nephrology referral", "procedure",
            "20250201"),
    Concept(ATC, "C09A", "ACE INHIBITORS, PLAIN", "drug_class"),
    Concept(ATC, "C09C", "ANGIOTENSIN II RECEPTOR BLOCKERS (ARBs), PLAIN", "drug_class"),
    Concept(ATC, "A10BK", "Sodium-glucose co-transporter 2 (SGLT2) inhibitors",
            "drug_class"),
    Concept(ATC, "M01A", "ANTIINFLAMMATORY AND ANTIRHEUMATIC PRODUCTS, NON-STEROIDS",
            "drug_class"),
    Concept(RXNORM, "6809", "metformin", "drug"),
    Concept(RXNORM, "11289", "warfarin", "drug"),
]

FIXTURE_EXPANSIONS = {
    (SNOMED, "73211009"): ["73211009", "44054006", "46635009", "5368009", "2751001"],
    (ATC, "C09A"): ["18867", "1998", "21102", "29046", "30131", "35208", "29046"],
    (ATC, "C09C"): ["83818", "52175", "69749", "73494"],
}

# Physiological ranges. NOT from any terminology -- LOINC does not publish them.
# Load-bearing twice: unit-slip warnings at compile time, and the solver's
# domain axioms, without which counterexamples come back at eGFR = 2.0.
FIXTURE_PLAUSIBLE = {
    "urine_albumin_creatinine_ratio_mg_g": (0.0, 30000.0),
    "egfr_ml_min_1_73_m2": (0.0, 200.0),
    "egfr": (0.0, 200.0),
    "serum_potassium_mmol_l": (1.5, 9.0),
    "hba1c": (3.0, 20.0),
}

def fixture_index(path: str = ":memory:") -> SqliteIndex:
    ix = SqliteIndex(path)
    ix.add(FIXTURE_CONCEPTS)
    for (system, code), members in FIXTURE_EXPANSIONS.items():
        ix.add_expansion(system, code, [(system, m) for m in members])
    ix.set_version(SNOMED, "20250201")
    ix.set_version(LOINC, "2.78")
    ix.set_version(ATC, "2025")
    ix.set_version(RXNORM, "20250203")
    return ix

# ---------------------------------------------------------------- cli

def _print_binding(b: Binding, issues: list[dsl.Issue]) -> None:
    print(f"  {b.mention!r}  [{b.kind}]")
    print(f"    -> {b.variable} : {b.var_type}"
          + (f" ({b.unit})" if b.unit else "") + f"  role={b.role}")
    print(f"    -> {SYSTEM_NAMES.get(b.system, b.system)} {b.code}  {b.display}")
    print(f"    -> binding: {b.binding_uri}")
    if b.expansion:
        print(f"    -> value set: {len(b.expansion)} codes "
              f"({', '.join(b.expansion[:4])}{'...' if len(b.expansion) > 4 else ''})")
    print(f"    -> chose [{b.selected_index}] of {len(b.candidates)} by {b.selector}: {b.reason}")
    for i in issues:
        print(f"    !! {i.severity.upper()} {i.code}: {i.message}")

DEMO_SENTENCE = ("We recommend treatment with an ACE inhibitor or ARB in adults with "
                 "diabetes and a urine albumin-to-creatinine ratio of 30 mg/g or higher.")

def _main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Bind clinical phrases to terminology codes.")
    ap.add_argument("statement", nargs="?", default=DEMO_SENTENCE,
                    help="a recommendation sentence (default: a KDIGO-style demo)")
    ap.add_argument("--db", help="SQLite terminology cache (default: in-memory fixture)")
    ap.add_argument("--remote", action="store_true",
                    help="allow live lookups against tx.fhir.org / NLM / RxNav "
                         "(no account needed) and cache them into --db")
    ap.add_argument("--registry", action="store_true",
                    help="print the dsl.Registry the bindings produce")
    a = ap.parse_args(argv)

    local = SqliteIndex(a.db) if a.db else fixture_index()
    index = CachedIndex(local, RemoteIndex()) if a.remote else local
    if a.db and a.remote:
        print(f"cache: {a.db} (live lookups enabled)\n")

    print(f"statement: {a.statement}\n")
    bindings, refusals = bind_statement(a.statement, index)
    print(f"--- {len(bindings)} bound ---")
    for b in bindings:
        _print_binding(b, verify(b, index))
    if refusals:
        print(f"\n--- {len(refusals)} refused (this is the residue, not an error) ---")
        for r in refusals:
            print(f"  {r.reason_code}: {r.reason_text}")
    if a.registry:
        reg = registry_from_bindings(bindings, FIXTURE_PLAUSIBLE)
        print(f"\n--- dsl.Registry: {len(reg.names())} variables, "
              f"{len(reg.actions())} actions ---")
        for n in reg.names():
            v = reg[n]
            print(f"  {n:42s} {v.type:5s} {str(v.unit or ''):14s} {v.role:11s} {v.binding}")
    return 0

if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))
