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
import os
import re
import sqlite3
import threading
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Callable, Iterable, Protocol

import dsl

# ---------------------------------------------------------------- systems

def load_env(path: str | None = None) -> None:
    """Read a .env into os.environ. No dependency; existing env vars win."""
    if path is None:
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(os.path.dirname(here), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))

SNOMED = "http://snomed.info/sct"
LOINC = "http://loinc.org"
RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
ATC = "http://www.whocc.no/atc"
# A VSAC candidate is a VALUE SET, not a single concept: its "code" is the OID
# and its expansion is the member code list. That is the right shape -- a
# clinical concept is a set, and binding to a single code silently fails any
# patient recorded under a sibling term.
VSAC = "http://cts.nlm.nih.gov/fhir/ValueSet"

SYSTEM_NAMES = {SNOMED: "SNOMED CT", LOINC: "LOINC", RXNORM: "RxNorm",
                ATC: "ATC", VSAC: "VSAC"}

# What kind of thing a phrase denotes decides which vocabulary can answer.
Kind = str      # lab | condition | drug | drug_class | procedure

KIND_SYSTEMS: dict[Kind, tuple[str, ...]] = {
    "lab":        (LOINC, VSAC),
    "condition":  (SNOMED, VSAC),
    "procedure":  (SNOMED, VSAC),
    "drug":       (RXNORM, VSAC),
    "drug_class": (ATC, RXNORM, VSAC),
}

# Kinds denoting a SET of codes rather than one, and so better served by a
# curated value set than by a single concept plus a computed is-a expansion.
SET_VALUED_KINDS = {"condition", "drug_class", "procedure"}

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
    # LOINC's COMMON_TEST_RANK: 1 is the most commonly ordered test, 0 is
    # unranked. A guideline threshold almost always names a common test, so
    # this separates "Glomerular filtration rate" from "Tubular reabsorption of
    # phosphorus/Glomerular filtration rate" -- which share every token and are
    # indistinguishable on lexical overlap alone.
    rank: int = 0

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
    # An expanded or canonical form of the phrase, used ONLY as an extra search
    # query. Guideline prose is abbreviation-dense and terminology indexes are
    # not: measured against live SNOMED, "CVAD" retrieves *Structure of central
    # axillary lymph node*, "APS" retrieves *Dapsone and pyrimethamine*, and
    # "home TPN" retrieves *Homeria poisoning*. A hand-written abbreviation
    # table cannot cover a specialty it was not written for.
    #
    # This is safe in a way that generating a code is not: the model proposes
    # a QUERY, the index still supplies the answer, and the selector still picks
    # from what the index returned. `text` stays verbatim for provenance.
    canonical: str | None = None

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
  kind TEXT NOT NULL, version TEXT, unit TEXT, rank INTEGER DEFAULT 0,
  is_preferred INTEGER DEFAULT 1,
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

class TransportFailures:
    """Terminology lookups that failed for a TRANSPORT reason rather than
    because the concept is absent.

    Silently swallowing them was the most dangerous instance of the
    "advisory output used as a result" class: `except Exception: pass` around a
    remote search makes a timed-out server indistinguishable from an empty
    vocabulary. The binder then refuses, the refusal is labelled
    `unbound_concept`, and the residue report attributes it to the GUIDELINE --
    so a network blip is published as a finding about clinical prose.

    Recorded on the index rather than raised, because one failed lookup should
    not abort a run: the failure is real but recoverable. What must not happen
    is it being invisible."""

    def __init__(self):
        self._items: list[tuple[str, str, str]] = []
        self._lock = threading.RLock()

    def note(self, phrase: str, kind: str, exc: BaseException) -> None:
        with self._lock:
            self._items.append((phrase, kind, f"{type(exc).__name__}: {exc}"[:160]))

    def __len__(self) -> int:
        return len(self._items)

    def summary(self) -> list[str]:
        with self._lock:
            return [f"{p!r} ({k}): {e}" for p, k, e in self._items]

class SqliteIndex:
    """Local terminology cache. Three jobs, and the third is the point:

    1. search -- FTS5 with BM25, no dependencies, works offline
    2. validate -- the deterministic gate a binding is checked against
    3. VERSION PIN -- the file is the terminology version. A reviewer re-running
       your pipeline gets the same codes, which a live terminology server
       cannot promise."""

    # Consult the semantic index when a lexical lookup finds no exact term.
    # An attribute rather than a global so a caller -- a fixture, an offline
    # run, a benchmark isolating lexical recall -- can turn it off explicitly.
    semantic = True

    def __init__(self, path: str = ":memory:"):
        self.path = path
        # No transport, so never any transport failures -- present so every
        # index answers `len(index.failures)` without the caller type-testing.
        self.failures = TransportFailures()
        # A sqlite3 connection is bound to its creating thread unless told
        # otherwise, and the binder IS used from a thread pool (K samples, or
        # one worker per statement). So the connection is shared explicitly and
        # `_lock` serialises every access. Without this a concurrent run dies
        # with "SQLite objects created in a thread can only be used in that
        # same thread" -- and only under concurrency, which is the worst way to
        # discover it.
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.db.executescript(SCHEMA)

    # -- writing -------------------------------------------------------
    def add(self, concepts: Iterable[Concept]) -> int:
        with self._lock:
            n = 0
            for c in concepts:
                cur = self.db.execute(
                    "INSERT OR IGNORE INTO concept(system,code,term,kind,version,unit,rank) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (c.system, c.code, c.term, c.kind, c.version, c.unit, c.rank))
                if cur.rowcount:
                    self.db.execute(
                        "INSERT INTO concept_fts(term,system,code,kind) VALUES (?,?,?,?)",
                        (c.term, c.system, c.code, c.kind))
                    n += 1
            self.db.commit()
            return n

    def add_expansion(self, system: str, code: str,
                      members: Iterable[tuple[str, str]]) -> None:
        with self._lock:
            self.db.executemany(
                "INSERT OR IGNORE INTO expansion VALUES (?,?,?,?)",
                [(system, code, ms, mc) for ms, mc in members])
            self.db.commit()

    def set_version(self, system: str, version: str) -> None:
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                            (f"version:{system}", version))
            self.db.commit()

    # -- reading -------------------------------------------------------
    def version(self, system: str) -> str | None:
        with self._lock:
            r = self.db.execute("SELECT value FROM meta WHERE key=?",
                                (f"version:{system}",)).fetchone()
        return r["value"] if r else None

    def validate(self, system: str, code: str) -> bool:
        with self._lock:
            return self.db.execute(
                "SELECT 1 FROM concept WHERE system=? AND code=? LIMIT 1",
                (system, code)).fetchone() is not None

    def expand(self, system: str, code: str, limit: int = 500) -> tuple[str, ...]:
        with self._lock:
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
        # The kind and system filters MUST be inside the query. They used to be
        # applied in Python after `LIMIT 40`, so the limit was spent on rows of
        # the wrong kind: "insulin" filled all forty slots with LOINC assays and
        # SNOMED findings, and a drug search returned ONE candidate out of
        # 171,231 drug rows -- with `Insulin` (SNOMED 67866001) sitting in the
        # table under exactly that term. The same starvation explains
        # `albuminuria` finding nothing as a lab and `blind` returning only
        # "Blind staggers": in each case the matching rows existed and never
        # reached the ranker.
        clauses = ["concept_fts MATCH ?", "kind = ?"]
        params: list = [q, kind]
        if systems:
            clauses.append("system IN (%s)" % ",".join("?" * len(systems)))
            params += list(systems)
        params.append(max(k * 8, 40))
        with self._lock:
            rows = self.db.execute(
                "SELECT term, system, code, kind, bm25(concept_fts) AS bm "
                "FROM concept_fts WHERE " + " AND ".join(clauses)
                + " ORDER BY bm LIMIT ?", params).fetchall()
        out = []
        for r in rows:
            with self._lock:
                meta = self.db.execute(
                    "SELECT version, unit, rank FROM concept "
                    "WHERE system=? AND code=? AND term=?",
                    (r["system"], r["code"], r["term"])).fetchone()
            c = Concept(r["system"], r["code"], r["term"], r["kind"],
                        meta["version"] if meta else None,
                        meta["unit"] if meta else None,
                        (meta["rank"] if meta else 0) or 0)
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
    if c.rank:
        # Common tests first. Rank 1 is the single most-ordered LOINC test;
        # anything past a few thousand is vanishingly rare in practice.
        score += min(3.0, 3.0 / (1.0 + c.rank / 400.0)); why.append(f"rank={c.rank}")
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

# Words that carry no retrieval signal but break keyword endpoints when left
# in. "albumin-to-creatinine ratio" returns zero LOINC hits; "albumin
# creatinine ratio" returns the right ones.
QUERY_STOPWORDS = {"to", "for", "with", "per", "by", "on", "at"}

def normalise_query(phrase: str) -> str:
    """Bare keyword form: hyphens to spaces, infix connectives dropped."""
    return " ".join(t for t in _tokens(phrase.replace("-", " "))
                    if t not in QUERY_STOPWORDS)

# A slash in guideline prose usually joins two concepts --
# "leukemia/lymphoblastic lymphoma", "medium-/long-term" -- but it is also
# division in a unit ("mg/g") and part of many terminology terms
# ("Albumin/Creatinine"). Splitting is therefore ADDITIVE: the compound is still
# searched, and the halves are searched as well, so a meaningful slash loses
# nothing.
_UNITISH = re.compile(r"^\s*[a-zµ]{1,6}\s*$", re.I)

def split_compound(phrase: str) -> list[str]:
    """The halves of a slash-joined phrase, when both look like real terms."""
    if "/" not in phrase:
        return []
    parts = [p.strip(" -\u2010\u2011\u2013") for p in phrase.split("/")]
    if len(parts) < 2 or any(not p for p in parts):
        return []
    # "mg/g", "mL/min" -- both sides are unit-shaped, so the slash is division.
    if all(_UNITISH.match(p) for p in parts):
        return []
    return [p for p in parts if len(p) > 3]

def expand_query(phrase: str, doc_abbrevs: dict[str, str] | None = None) -> list[str]:
    """The phrase, its bare keyword form, the halves of any slash-compound, and
    its expansion if it is a known abbreviation. Union, so a query that works on
    one endpoint is not lost because another prefers a different shape.

    `doc_abbrevs` is the glossary segment.py read out of THIS document, and it
    is consulted before the hand-written table below. A guideline defines its
    own abbreviations on first use, so the document is a better authority on
    what "PCA" means here than any list we could write in advance -- and it
    covers specialties the hand-written table was never written for."""
    key = " ".join(_tokens(phrase))
    out = [phrase]
    if doc_abbrevs:
        for form in (phrase, phrase.strip()):
            if form in doc_abbrevs and doc_abbrevs[form] not in out:
                out.append(doc_abbrevs[form])
    bare = normalise_query(phrase)
    if bare and bare != phrase:
        out.append(bare)
    for half in split_compound(phrase):
        if half not in out:
            out.append(half)
    for k in (key, bare):
        if k in ABBREVIATIONS and ABBREVIATIONS[k] not in out:
            out.append(ABBREVIATIONS[k])
    head = head_noun(phrase)
    if head and head not in out:
        out.append(head)
    return out

# A multi-word phrase can retrieve ONLY over-specific descendants, leaving the
# generic concept -- the one the guideline actually means -- unreachable.
# Measured: "mechanical thrombectomy" returns eight fluoroscopy-guided variants
# of the wrong vessel (dialysis fistula, pulmonary artery, hepatic vein), every
# one of which the selector correctly rejects, while `Thrombectomy` (SNOMED
# 43810009) and `Intracranial thrombectomy` (21710002) sit in the same index
# and are never offered. The selector was right and the retrieval was wrong,
# which reads downstream as `unbound_unselected` residue attributable to the
# guideline. It is not; it is ours.
#
# Searching the head noun alone returns both at ranks 1 and 2. This is a union
# like every other expansion, so it can only ADD candidates -- the reranker and
# the selector still decide.

# Heads too generic to discriminate. Hand-written, and defensibly so: unlike
# clinical vocabulary these are a small closed set of English nouns that name a
# category rather than a thing. Searching them returns an arbitrary eight of
# hundreds, which is noise in the selector's prompt rather than a candidate.
_GENERIC_HEADS = {
    "device", "agent", "agents", "therapy", "therapies", "treatment",
    "management", "care", "procedure", "test", "tests", "level", "levels",
    "value", "values", "status", "disease", "disorder", "syndrome", "system",
    "factor", "factors", "measure", "measures", "score", "scores", "index",
    "rate", "function", "assessment", "monitoring", "intervention", "drug",
    "drugs", "medication", "medications", "condition", "state", "study",
    # Measured: "premixed insulin regimen" backed off to "regimen" and
    # retrieved *Transfeminine Regimens* -- the backoff actively injecting
    # noise. A head that names a FORM of treatment rather than the treatment
    # discriminates nothing.
    "regimen", "regimens", "dose", "doses", "dosing", "schedule", "protocol",
    "programme", "program", "plan", "approach", "strategy",
}

def head_noun(phrase: str) -> str | None:
    """The last content word of a multi-word phrase -- English medical noun
    phrases put the head last ("mechanical THROMBECTOMY", "intravenous
    THROMBOLYSIS", "low-molecular-weight HEPARIN"). Returns None when the
    phrase is already one word, or when the head names a category rather than
    a thing."""
    toks = _tokens(phrase)
    if len(toks) < 2:
        return None
    head = toks[-1]
    if len(head) < 5 or head in _GENERIC_HEADS:
        return None
    return head

# The kind is the MODEL's guess, and terminology hierarchies do not line up
# with it. "CVAD" is a physical object to SNOMED, a procedure to a model, and a
# patient state to a guideline. Searching the neighbouring kind costs one query
# and recovers exactly the cases where the two disagree.
# A candidate from the NEIGHBOURING hierarchy is worth proposing but must not
# outrank one from the hierarchy actually asked for. Applied on both retrieval
# paths -- the lexical one had it inline and the semantic one did not, which is
# how a drug outranked the drug CLASS the sentence was talking about.
FALLBACK_KIND_PENALTY = 0.5

KIND_FALLBACK: dict[Kind, tuple[Kind, ...]] = {
    "procedure": ("condition",),
    "condition": ("procedure",),
    "drug": ("drug_class",),
    "drug_class": ("drug",),
    # `lab` had no fallback at all, which made an entire class of concept
    # unreachable: a guideline says "monitoring for albuminuria" and the model
    # calls it a lab, but SNOMED files `Albuminuria` (274769005) as a FINDING
    # and `Antiphospholipid antibody measurement` (82886000) as a PROCEDURE.
    # Measured: the expected code for `albuminuria` sits in the index and is
    # reachable only under `condition`. Two extra local queries.
    "lab": ("condition", "procedure"),
}

# Semantic retrieval is a FALLBACK, never a replacement. Measured on the
# benchmark's fixable failures, lexical finds 3 of 10 and semantic finds 4 --
# but they are different threes and fours. Lexical alone catches `APLA` (an
# abbreviation the glossary expands); semantic alone catches `kidney function`
# -> `Renal function` (0.95) and `epidural analgesia` -> `Epidural anaesthesia`
# (0.86), which share no useful tokens with their targets. The union is 5.
#
# Running it only when lexical has produced nothing worth selecting keeps the
# cost off the common path and means a semantic near-miss can never displace a
# lexical exact match.
_SEMANTIC: dict = {"index": None, "tried": False, "why": ""}
_SEMANTIC_LOCK = threading.RLock()

def semantic_index(db_path: str | None = None):
    """The shared semantic index, or None with the reason recorded.

    Built once per process. `None` here means "unavailable", and the reason is
    kept rather than discarded -- an absent index and an absent concept must
    never look the same (R27)."""
    # Under the thread pool the binder runs in, an unlocked `tried` flag is a
    # race with a SILENT consequence: thread A sets it and starts loading,
    # thread B sees tried=True with index still None, concludes "unavailable"
    # and skips semantic retrieval for the rest of the run. No error, no log,
    # just quietly worse retrieval -- the exact failure shape this project
    # keeps finding. The lock makes the first caller do the work and the rest
    # wait for its answer.
    with _SEMANTIC_LOCK:
        if _SEMANTIC["tried"]:
            return _SEMANTIC["index"]
        _SEMANTIC["tried"] = True
        return _semantic_index_locked(db_path)

def _semantic_index_locked(db_path: str | None):
    try:
        import semantic
        path = db_path or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "terms.db")
        if not os.path.exists(path):
            _SEMANTIC["why"] = f"no local terminology db at {path}"
            return None
        # BOTH models, chosen per kind at search time. Picking one globally
        # meant a half-finished biomedical build shadowed a complete general
        # one and every lookup returned nothing -- an absent index reading as
        # an absent concept, the same failure class again. Biomedical first
        # where it exists, because it scores materially higher on domain
        # synonymy (BP-lowering/antihypertensive 0.53 vs 0.36).
        cands = [semantic.SemanticIndex(path),
                 semantic.SemanticIndex(path, semantic.FAST_MODEL)]
        usable = [c for c in cands if any(c.available(kd) for kd in KIND_SYSTEMS)]
        if not usable:
            _SEMANTIC["why"] = ("no semantic index built; run "
                                "`python3 semantic.py --build --fast`")
            return None
        _SEMANTIC["index"] = usable
    except Exception as exc:
        _SEMANTIC["why"] = f"{type(exc).__name__}: {exc}"
    return _SEMANTIC["index"]

def semantic_candidates(mention: Mention, k: int) -> list[Candidate]:
    """Nearest-meaning terms for a mention the lexical index could not match."""
    indices = semantic_index()
    if not indices:
        return []
    import semantic as _sem
    out: list[Candidate] = []
    kinds = [mention.kind] + list(KIND_FALLBACK.get(mention.kind, ()))
    queries = [mention.text] + ([mention.canonical] if mention.canonical else [])
    for kind in kinds:
        ix = next((c for c in indices if c.available(kind)), None)
        if ix is None:
            continue
        # The same penalty the lexical path applies. Without it a neighbouring
        # kind outranks the RIGHT kind on raw cosine and evicts it: "basal
        # insulin" put SNOMED `Insulin` (drug, 0.78) above ATC `Insulin,
        # Long-Acting` (drug_class, 0.61) and filled all eight slots before the
        # drug-CLASS concept -- the only candidate that actually denotes a
        # class -- could appear. The selector then abstained, correctly, on
        # three statements across one document, and bound on the two where the
        # model happened to guess the synonym "long-acting insulin" itself.
        penalty = 0.0 if kind == mention.kind else FALLBACK_KIND_PENALTY
        for q in queries:
            try:
                hits = ix.search(q, kind, k)
            except _sem.SemanticUnavailable:
                continue
            for code, system, term, sim in hits:
                why = (f"semantic({sim:.2f})",)
                if penalty:
                    why += (f"kind_fallback:{kind}",)
                out.append(Candidate(
                    Concept(system=system, code=code, term=term, kind=kind),
                    round(float(sim) - penalty, 3), why))
    return _dedupe_candidates(sorted(out, key=lambda c: -c.score))[:k]

_GENERIC_STEMS = frozenset(_stems(" ".join(sorted(_GENERIC_HEADS))))

def strip_generic_modifiers(phrase: str) -> str | None:
    """The phrase with category words like "regimen" or "protocol" removed.

    BM25 rewards a RARE token, and a clinically empty one can be very rare:
    "regimens" occurs in 6 concepts in this index, "insulin" in 1,638. So
    "Basal insulin regimen" retrieved *Transfeminine Regimens, All*,
    *Transmasculine Regimens, All* and nothing else -- the whole candidate list
    decided by the one word that carries no clinical meaning, while the drug it
    is about was never even proposed.

    `_GENERIC_HEADS` already names these words; it was only consulted for the
    head-noun BACKOFF, so it could not help when the noise word sat in the
    primary query. Returns None when stripping would leave nothing, or change
    nothing."""
    toks = _tokens(phrase)
    kept = [t for t in toks if t not in _GENERIC_HEADS]
    if not kept or len(kept) == len(toks):
        return None
    return " ".join(kept)

def search_all(index: TerminologyIndex, mention: Mention, k: int,
               doc_abbrevs: dict[str, str] | None = None) -> list[Candidate]:
    """Union over the phrase and its expansion. Union, not intersection: each
    query sees terms the other misses, and the selector decides between them."""
    found: list[Candidate] = []
    queries = expand_query(mention.text, doc_abbrevs)
    if mention.canonical and mention.canonical.lower() not in {q.lower() for q in queries}:
        queries.append(mention.canonical)
        bare = normalise_query(mention.canonical)
        if bare and bare not in queries:
            queries.append(bare)
    # Union, so the unstripped phrase still gets its chance: "insulin pump"
    # would lose nothing here, but a phrase whose head really is the concept
    # must not be silently rewritten away.
    for q in list(queries):
        bare = strip_generic_modifiers(q)
        if bare and bare.lower() not in {x.lower() for x in queries}:
            queries.append(bare)
    for q in queries:
        found.extend(index.search(q, mention.kind, k=k))
    # Always search the neighbouring kind, not only when the chosen one comes up
    # empty. Measured: "CVAD" typed as a procedure returns *Repair of central
    # venous access device* -- high token overlap, convincing-looking, and the
    # wrong concept. Retrieval proposes from both hierarchies; the selector,
    # which can see the sentence, decides.
    for alt in KIND_FALLBACK.get(mention.kind, ()):
        for q in queries:
            found.extend(replace(c, score=c.score - FALLBACK_KIND_PENALTY,
                                 why=c.why + (f"kind_fallback:{alt}",))
                         for c in index.search(q, alt, k=k))

    # Drop candidates that share NO word with any query. Measured: tx.fhir.org's
    # $expand?filter= does prefix matching, and on a MISS it returns arbitrary
    # concepts rather than an empty set -- "central venous access device"
    # retrieves *Spine board*, *Arrow*, *Atomizer*. Those look like results, so
    # a selector sees a full candidate list and may pick from noise. A candidate
    # with zero token overlap is not a match; discarding it turns a silent
    # wrong answer into an honest "no candidates", which routes to a refusal.
    qtokens = set()
    for q in queries:
        qtokens |= set(_stems(q))
    # CONTENT tokens only. Overlap on a category word is not overlap on a
    # concept: "Basal insulin regimen" and "Transfeminine Regimens, All" share
    # "regimen" and nothing else, and because BM25 rewards a rare token --
    # "regimens" occurs in 6 concepts here, "insulin" in 1,638 -- the gender
    # affirming value sets scored 14.5 against the insulins' 12.0 and took every
    # slot. Three statements in one document refused on that, with the selector
    # abstaining correctly each time because the drug was never proposed to it.
    #
    # Union across queries cannot fix it: BM25 scores from different queries
    # are not comparable, so the noisier query simply wins. The fix has to be a
    # FILTER, and this is the filter that already exists -- it was just counting
    # a meaningless word as evidence of a match.
    content = qtokens - _GENERIC_STEMS
    keep = content or qtokens
    if keep:
        found = [c for c in found if set(_stems(c.concept.term)) & keep]

    # The token-overlap filter above is exactly what makes clinical synonymy
    # unreachable: "kidney function" and "Renal function" share no stem, so the
    # right concept is discarded even on the rare occasion it was retrieved.
    # When that leaves nothing, ask for the nearest MEANING instead. Only then,
    # so a semantic near-miss can never outrank a lexical exact match.
    # "Returned nothing" is too strict a trigger. Lexical search rarely returns
    # nothing -- it returns the WRONG thing: "kidney function" retrieves
    # *Decreased kidney function* and *Kidney function monitoring*, neither of
    # which is the observable the guideline means, while *Renal function* sits
    # unreachable. So the trigger is the absence of an EXACT term match, which
    # is cheap to test and precisely separates "we found it" from "we found
    # something". `albuminuria` and `acute pancreatitis` match exactly and skip
    # the model entirely; `kidney function` and `epidural analgesia` do not.
    found.sort(key=lambda c: -c.score)
    found = _dedupe_candidates(found)

    # Against the MENTION, not against every expanded query. The head-noun
    # backoff adds "analgesia" for "epidural analgesia", and the generic
    # concept `Analgesia` then matched that query exactly -- which read as
    # "we found it" and suppressed the semantic search that would have offered
    # `Epidural anaesthesia`. A broadening query matching is not a hit.
    qnorm = {" ".join(_tokens(mention.text))}
    if mention.canonical:
        qnorm.add(" ".join(_tokens(mention.canonical)))
    if getattr(index, "semantic", True) and not any(
            " ".join(_tokens(c.concept.term)) in qnorm for c in found):
        # RESERVE slots rather than merge by score. A cosine similarity (0..1)
        # and a lexical score (0..12) are not comparable, and sorting them
        # together buried every semantic hit below the lexical noise it was
        # meant to rescue -- `Renal function` came back at 0.954 and fell off
        # the end of a list of eight wrong `kidney function` matches. Faking a
        # score to interleave them would be inventing a comparison that does
        # not exist; taking a fixed share of the list is honest about there
        # being two rankings.
        share = max(2, k // 3)
        # Ask for more than the share and spend it on candidates the lexical
        # list does NOT already contain. Truncating to the share first wasted
        # both reserved slots on duplicates -- for "Basal insulin regimen" the
        # top two semantic hits were `Insulin Detemir` and `Insulin Glargine`,
        # already retrieved lexically, so `Insulin, Long-Acting` at rank 3 --
        # the only candidate in the whole index that denotes a drug CLASS, and
        # the one the selector binds to when it sees it -- was cut. The slots
        # exist to add information, so they are filled with information.
        sem = semantic_candidates(mention, max(4 * share, k))
        if sem:
            # APPEND -- never evict. Reserving slots by truncating the lexical
            # list to `k - len(sem)` dropped a correct candidate sitting at
            # position 7: `anticoagulant prophylaxis` regressed from hit to
            # miss, caught by the ablation that was measuring the feature. The
            # list is allowed to exceed k by the semantic share; a slightly
            # longer selector prompt is a much smaller cost than evicting the
            # right answer to make room for a guess.
            seen = {(c.concept.system, c.concept.code) for c in found[:k]}
            extra = [c for c in sem
                     if (c.concept.system, c.concept.code) not in seen][:share]
            return found[:k] + extra
    return found[:k]

# Minimal UCUM comparison. String equality is wrong here and the failure is
# silent: LOINC records 9318-7 (albumin/creatinine MASS ratio) as
# `ug/mg{creat}`, which is dimensionally identical to the `mg/g` a guideline
# writes -- so a textual comparison rejects the correct code and, worse, has no
# way to tell it apart from `mg/mmol`, which is a MOLAR ratio and a genuinely
# different quantity. Dimension first, then scale.
_SI = {"y": 1e-24, "z": 1e-21, "a": 1e-18, "f": 1e-15, "p": 1e-12, "n": 1e-9,
       "u": 1e-6, "m": 1e-3, "c": 1e-2, "d": 1e-1, "": 1.0,
       "k": 1e3, "M": 1e6, "G": 1e9}
_BASE = {"g": "mass", "mol": "amount", "L": "volume", "l": "volume",
         "m": "length", "s": "time", "min": "time", "h": "time", "d": "time",
         "U": "activity", "IU": "activity", "%": "fraction", "1": "fraction"}

def _parse_ucum_atom(atom: str) -> tuple[str, float] | None:
    atom = re.sub(r"\{[^}]*\}", "", atom).strip()
    if not atom:
        return None
    if atom in _BASE:
        return (_BASE[atom], 1.0)
    for plen in (1,):
        p, rest = atom[:plen], atom[plen:]
        if p in _SI and rest in _BASE:
            return (_BASE[rest], _SI[p])
    return (atom.lower(), 1.0)          # unknown: compare literally

def ucum_key(unit: str | None) -> tuple | None:
    """(dimension, scale) for a unit or ratio of units, or None if unparseable."""
    if not unit:
        return None
    u = re.sub(r"\{[^}]*\}", "", unit).strip()
    if not u:
        return None
    parts = u.split("/")
    atoms = [_parse_ucum_atom(p) for p in parts]
    if any(a is None for a in atoms):
        return None
    dims = "/".join(a[0] for a in atoms)
    scale = atoms[0][1]
    for a in atoms[1:]:
        scale /= a[1]
    return (dims, round(scale, 12))

def units_match(a: str | None, b: str | None) -> bool:
    ka, kb = ucum_key(a), ucum_key(b)
    if ka is None or kb is None:
        return False
    return ka == kb

def units_comparable(a: str | None, b: str | None) -> bool:
    """Same dimension, possibly different scale -- mg/g vs g/kg."""
    ka, kb = ucum_key(a), ucum_key(b)
    return ka is not None and kb is not None and ka[0] == kb[0]

def rerank_stewards(cands: list[Candidate]) -> list[Candidate]:
    """Break ties between value sets that different stewards publish under the
    same name.

    Measured (R6b): VSAC carries 40 value sets titled exactly "Diabetes", from
    at least two steward roots, spanning 2017-2026. Name alone does not choose
    between them, and the selector correctly refused -- but refusing every
    common concept is not a usable pipeline either.

    The policy, which is deterministic and reportable rather than a guess:
    prefer an exact title match, then the most recently versioned set. The
    alternatives stay in `Binding.candidates`, so the choice is auditable and a
    clinician can see what was passed over. This is a POLICY, not a clinical
    judgement, and the paper should say so."""
    def rank(c: Candidate):
        exact = "exact_term" in c.why
        version = c.concept.version or ""
        return (-c.score, not exact, -_version_key(version))
    return sorted(cands, key=rank)

def _version_key(v: str) -> int:
    digits = re.sub(r"[^0-9]", "", v or "")[:8]
    return int(digits) if digits else 0

def rerank_by_unit(cands: list[Candidate], mention: Mention) -> list[Candidate]:
    """Deterministic disambiguation before the model is asked anything.

    "ACR of 30 mg/g" and "ACR of 3.4 mg/mmol" are the same clinical threshold
    under different LOINC concepts (9318-7 mass ratio, 14585-4 MOLAR ratio,
    a factor of ~8.84 apart). The sentence states the unit, so the unit settles
    it -- no judgement required, and the reason is recorded either way."""
    if not mention.unit:
        return cands
    want, out = mention.unit, []
    for c in cands:
        cu = c.concept.unit
        if cu and units_match(cu, want):
            out.append(replace(c, score=c.score + 4.0, why=c.why + ("unit_match",)))
        elif cu and units_comparable(cu, want):
            # Same dimension, different scale (mg/g vs g/kg): a conversion, not
            # a different quantity. Mildly preferred, never rejected.
            out.append(replace(c, score=c.score + 1.0,
                               why=c.why + ("unit_convertible",)))
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
    "additionalProperties": False,
    "properties": {"mentions": {"type": "array", "items": {
        "type": "object",
        "required": ["text", "kind", "canonical"],
        "additionalProperties": False,
        "properties": {
            "text": {"type": "string"},
            "kind": {"enum": ["lab", "condition", "drug", "drug_class", "procedure"]},
            "canonical": {"type": ["string", "null"]}}}}},
}

MENTION_INSTRUCTIONS = """\
List the clinical concepts this recommendation depends on: the labs it
thresholds, the conditions that make it apply, and the drugs, drug classes or
procedures it directs.

Copy each concept VERBATIM from the sentence. Do not expand abbreviations,
normalise wording, or invent a canonical name. Do not list severity words,
populations ("adults"), or vague qualifiers ("high-risk") -- those are not
bindable concepts and are handled elsewhere.

kind is one of: lab, condition, drug, drug_class, procedure.

Also give `canonical`: the name a clinical TERMINOLOGY would use for the same
concept. Two jobs, and the second matters more:

  Expand abbreviations -- "CVAD" -> "central venous access device",
  "APS" -> "antiphospholipid syndrome", "TPN" -> "total parenteral nutrition".

  Give the terminology's WORD for it when the guideline uses a different one.
  Guidelines and SNOMED name the same thing differently, and a keyword index
  cannot bridge that on its own:
      "kidney function"      -> "renal function"
      "BP lowering"          -> "antihypertensive therapy"
      "volume optimisation"  -> "fluid management"
      "CGM readings"         -> "blood glucose measurement"
      "epidural analgesia"   -> "epidural anaesthesia"
  Prefer the noun form of a procedure ("mobilisation", not "mobilising").

This is used only to SEARCH a terminology index; the code itself still comes
from that index, never from you -- so a wrong guess here costs a wasted query,
never a wrong code. Use null when the phrase is already the standard name."""

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
                                     unit_near(text, m.end()) if kind == "lab" else None,
                                     ABBREVIATIONS.get(" ".join(_tokens(m.group(0)))))) 
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
        canon = (m.get("canonical") or "").strip() or None
        out.append(Mention(span, kind, at, at + len(span),
                           unit_near(text, at + len(span)) if kind == "lab" else None,
                           canon))
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
        # No `minimum`: the structured-output validator rejects numeric bounds.
        # select() range-checks the value instead.
        "choice": {"type": "integer"},
        "reason": {"type": "string"},
    },
}

SELECT_INSTRUCTIONS = """\
Choose which terminology concept the phrase refers to, IN THE CONTEXT of the
sentence it came from. Answer with the INDEX of one candidate, or -1 to abstain.

Abstain when no candidate is the concept, when the phrase is too vague to
have one ("high-risk", "adequate trial"), or when two candidates differ in a
way the sentence does not settle.

Do NOT abstain merely because two candidates are REDUNDANT -- the same concept
published by different stewards, or a value set and the single concept it
expands. That is not under-determination. When several curated value sets carry
the same name from different stewards, take the FIRST -- the list is already
ordered by exact-title match and then by most recent version -- and say in
`reason` which one you took and that others existed. Prefer a curated value set over a
single code when the phrase denotes a category of patients or drugs, prefer the
more specific steward, and say which you took. Abstaining sends the statement to the
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
    # `isinstance(True, int)` is True and `-1 <= True < len(cands)` is True, so
    # a model returning `true` for `choice` silently selected candidate 1 --
    # a wrong CODE, chosen by a type confusion, with a plausible reason string
    # attached. Third appearance of this trap in the codebase (VarDef
    # typechecking, the probe mock, here), hence the shared `dsl._is_number`
    # discipline: bool is never a number.
    if isinstance(choice, bool) or not isinstance(choice, int) \
            or not (-1 <= choice < len(cands)):
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
         ref_id: str = "", _exclude: tuple = (),
         doc_abbrevs: dict[str, str] | None = None) -> Binding | dsl.Refusal:
    """One phrase -> a verified Binding, or a refusal. Never a guessed code."""
    cands = rerank_stewards(rerank_by_unit(
        search_all(index, mention, k, doc_abbrevs), mention))
    if _exclude:
        cands = [c for c in cands if c.concept.code not in _exclude]
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
    # A value set that expands to NOTHING is not a usable binding: every
    # retrieve against it matches no resource, so the rule silently never
    # fires. Measured on a live run -- a steward's "Diabetes" set expanded to 0
    # codes and the whole library evaluated to null, which is indistinguishable
    # from a patient who simply does not qualify.
    if mention.kind in SET_VALUED_KINDS and c.system == VSAC:
        if not index.expand(c.system, c.code):
            if len(cands) > 1:
                return bind(mention, sentence, index, llm, k, ref_id,
                            _exclude=tuple(_exclude) + (c.code,))
            return dsl.Refusal(
                ref_id or mention.text, "unbound_concept",
                f"the only candidate value set for {mention.text!r} "
                f"({c.curie}) expands to no codes", source_span=mention.text)

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
                   ref_id: str = "",
                   doc_abbrevs: dict[str, str] | None = None
                   ) -> tuple[list[Binding], list[dsl.Refusal]]:
    bindings, refusals = [], []
    for m in propose_mentions(text, mention_llm):
        r = bind(m, text, index, select_llm, ref_id=ref_id, doc_abbrevs=doc_abbrevs)
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
                 rxnav: str = RXNAV, vsac: "VsacIndex | None" = None):
        self.tx, self.tables, self.rxnav = tx, tables, rxnav
        self.vsac = vsac if vsac is not None else VsacIndex()
        self._versions: dict[str, str | None] = {}
        self.failures = TransportFailures()

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
        """LOINC PROPERTY is the structural test for "is this a measurable
        quantity": VRat, ArVRat, MCnc and friends are quantities, while panels,
        groupers and value-set entries have NO property at all.

        Measured: "estimated glomerular filtration rate" returns
        `HEDIS MY 2020 Value Set - Estimated Glomerular Filtration Rate`, and an
        earlier binder threshold-compared it. You cannot threshold a grouper --
        the CQL compiles and every patient evaluates to null."""
        d = _get_json(f"{self.tables}/loinc_items/v3/search",
                      {"terms": phrase, "maxList": max(k * 3, 20),
                       "df": "LOINC_NUM,LONG_COMMON_NAME,PROPERTY"})
        out = []
        for row in d[3]:
            code, name = row[0], row[1]
            prop = (row[2] if len(row) > 2 else "") or ""
            c = _score(phrase, Concept(LOINC, code, name, "lab", None, None), 0.0)
            if not prop:
                c = replace(c, score=c.score - 6.0,
                            why=c.why + ("no_loinc_property",))
            out.append(c)
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
        cands: list[Candidate] = []
        # Curated value sets first for the set-valued kinds, then the
        # single-concept sources. Union, not either/or: the selector should see
        # both a curated set and the concept it would otherwise expand itself.
        if kind in SET_VALUED_KINDS and self.vsac.available:
            cands += self.vsac.search(phrase, kind, k)
        try:
            if kind == "lab":
                cands += self._loinc_search(phrase, k)
            elif kind in {"drug", "drug_class"}:
                cands += self._rx_search(phrase, kind, k)
            else:
                cands += self._snomed_search(phrase, kind, k)
        except Exception as exc:
            # NOT `pass`. A dead endpoint returning nothing is indistinguishable
            # from a vocabulary that does not contain the concept, and the
            # second is reported as the guideline's residue.
            self.failures.note(phrase, kind, exc)
        cands.sort(key=lambda c: -c.score)
        return _dedupe_candidates(cands)[:k]

    def validate(self, system: str, code: str) -> bool:
        if system == VSAC:
            return self.vsac.validate(system, code)
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
        if system == VSAC:
            return self.vsac.expand(system, code, limit)
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

VSAC_FHIR = "https://cts.nlm.nih.gov/fhir"

class VsacIndex:
    """Curated value sets from the NLM Value Set Authority Center.

    The one source here that needs a UMLS/UTS licence, and the one worth it:
    VSAC value sets are expert-authored, steward-attributed and VERSIONED, and
    they are what the AHRQ CDS Connect reference artifacts bind to -- which is
    what makes a clause-level comparison against that expert CQL like-for-like
    rather than apples-to-oranges. Measured: the curated `Diabetes` value set
    holds 773 codes where a computed SNOMED is-a expansion holds 124.

    Auth is HTTP Basic with the literal username `apikey` and the UMLS API key
    as the password."""

    def __init__(self, api_key: str | None = None, base: str = VSAC_FHIR):
        if api_key is None:
            load_env()
            api_key = os.environ.get("UMLS_API_KEY")
        self.api_key, self.base = api_key, base
        self.failures = TransportFailures()

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _get(self, path: str, params: dict, timeout: int = 40):
        import base64, urllib.parse, urllib.request
        if not self.api_key:
            raise RuntimeError("UMLS_API_KEY is not set; VSAC needs a UMLS licence")
        url = f"{self.base}/{path}?{urllib.parse.urlencode(params, doseq=True)}"
        token = base64.b64encode(f"apikey:{self.api_key}".encode()).decode()
        req = urllib.request.Request(url, headers={
            "Accept": "application/fhir+json", "Authorization": f"Basic {token}",
            "User-Agent": "GuidelineForge/0.1 (research; terminology binder)"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())

    def search(self, phrase: str, kind: Kind, k: int = 8) -> list[Candidate]:
        """Value sets whose title contains the phrase. `title:contains` rather
        than `name=`: guideline prose says "ACE inhibitor", a steward titles the
        set "ACE Inhibitor or ARB Ingredient", and an exact-name match finds
        neither."""
        try:
            d = self._get("ValueSet", {"title:contains": phrase,
                                       "_count": max(k * 6, 50)})
        except Exception as exc:
            self.failures.note(phrase, kind, exc)
            return []
        out = []
        for e in d.get("entry", []) or []:
            r = e.get("resource", {})
            oid = (r.get("id") or "").rsplit("-", 1)[0]
            name = r.get("title") or r.get("name") or ""
            if not oid or not name:
                continue
            out.append(_score(phrase, Concept(VSAC, oid, name, kind,
                                              r.get("version")), 0.0))
        out.sort(key=lambda c: -c.score)
        return _dedupe_candidates(out)[:k]

    def expand(self, system: str, code: str, limit: int = 500) -> tuple[str, ...]:
        return tuple(c for _s, c in self.expand_coded(code, limit))

    def expand_coded(self, oid: str, limit: int = 2000) -> tuple[tuple[str, str], ...]:
        """(system, code) pairs. An execution engine matches on BOTH -- a bare
        code list silently fails to match a resource coded in the right system,
        which looks like a patient not meeting the criterion."""
        try:
            d = self._get(f"ValueSet/{oid}/$expand", {"count": limit})
        except Exception:
            return ()
        return tuple((c.get("system", ""), c["code"])
                     for c in d.get("expansion", {}).get("contains", [])
                     if "code" in c)

    def validate(self, system: str, code: str) -> bool:
        if system != VSAC:
            return False
        try:
            d = self._get("ValueSet", {"_id": code, "_count": 1})
            return bool(d.get("entry"))
        except Exception:
            return False

    def version(self, system: str) -> str | None:
        return None      # VSAC versions are per value set, carried on the Concept

class CachedIndex:
    """SqliteIndex first; on a miss, ask the remote and write through.

    The cache is not an optimisation -- it is the version pin. A reviewer
    re-running your pipeline against the .db gets the codes you got, which a
    live terminology server cannot promise."""

    def __init__(self, local: SqliteIndex, remote: RemoteIndex | None = None):
        self.local, self.remote = local, remote
        # Its own record, plus whatever the remote and VSAC accumulate --
        # `len(index.failures)` must mean the same thing whichever index the
        # caller was handed.
        self._failures = TransportFailures()

    @property
    def failures(self) -> TransportFailures:
        out = TransportFailures()
        out._items = list(self._failures._items)
        if self.remote is not None:
            out._items += self.remote.failures._items
            if self.remote.vsac is not None:
                out._items += self.remote.vsac.failures._items
        return out

    # Systems for which a licensed local release is strictly better than the
    # free keyword endpoints -- which do prefix matching, carry no synonyms and
    # rank badly. Once loaded, the remote is not consulted for these.
    LOCAL_AUTHORITATIVE = (LOINC, SNOMED, RXNORM, ATC)

    def authoritative_for(self, kind: Kind) -> bool:
        """True when a licensed release is loaded for every single-concept
        system this kind uses. VSAC is deliberately excluded: curated value
        sets are not something a local release replaces, so the remote is still
        worth asking for set-valued kinds."""
        systems = [s for s in KIND_SYSTEMS.get(kind, ())
                   if s in self.LOCAL_AUTHORITATIVE]
        return bool(systems) and all(self.local.version(s) for s in systems)

    def search(self, phrase: str, kind: Kind, k: int = 8) -> list[Candidate]:
        hits = self.local.search(phrase, kind, k)
        if self.authoritative_for(kind):
            # Local release covers the single-concept systems. For set-valued
            # kinds still ask VSAC -- a curated value set beats a lone code --
            # but never the weak keyword endpoints that poisoned the index.
            if kind in SET_VALUED_KINDS and self.remote is not None:
                try:
                    vsac = self.remote.vsac
                    if vsac.available:
                        hits = _dedupe_candidates(
                            sorted(hits + vsac.search(phrase, kind, k),
                                   key=lambda c: -c.score))[:k]
                except Exception as exc:
                    self._failures.note(phrase, kind, exc)
            return hits
        # A cache that holds only WEAK candidates must not shadow the remote.
        # Measured: demo.db held three eGFR entries -- two deprecated panels and
        # a HEDIS grouper -- and because the cache was non-empty the binder
        # never asked the terminology server for the real result code. "Any hit
        # at all" is the wrong test; "a hit with positive evidence" is right.
        good = [c for c in hits if set(c.why) & POSITIVE_EVIDENCE]
        if good or self.remote is None:
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

# ---------------------------------------------------------------- LOINC load

LOINC_DIR_ENV = "LOINC_DIR"

def find_loinc_table(path: str | None = None) -> str | None:
    """Locate `Loinc.csv` from an unpacked LOINC release."""
    roots = [path] if path else []
    roots.append(os.environ.get(LOINC_DIR_ENV))
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    roots.append(here)
    for root in [r for r in roots if r]:
        if os.path.isfile(root):
            return root
        for base, _dirs, files in os.walk(root):
            if "Loinc.csv" in files and os.path.basename(base) == "LoincTable":
                return os.path.join(base, "Loinc.csv")
        if os.path.basename(root).lower().startswith("loinc"):
            cand = os.path.join(root, "LoincTable", "Loinc.csv")
            if os.path.exists(cand):
                return cand
    return None

# A guideline thresholds a MEASURED QUANTITY. `Qn` is LOINC's own scale type for
# that, and it excludes ordinal presence tests, nominal panels and grouper
# entries structurally -- rather than by regex on the display name, which is
# what the free keyword endpoints forced.
QUANTITATIVE_SCALES = {"Qn"}
BOOLEANISH_SCALES = {"Ord", "Nom"}

def load_loinc(index: SqliteIndex, path: str | None = None, *,
               active_only: bool = True, max_rank: int | None = None) -> int:
    """Load a licensed LOINC release into the local index.

    Why this beats the free endpoints, measured: querying NLM Clinical Tables
    for "eGFR" returns gene assays and a HEDIS grouper; tx.fhir.org returns
    LOINC *Part* codes alphabetically. Neither surfaces 48642-3. The licensed
    table carries SCALE_TYP, PROPERTY, STATUS and COMMON_TEST_RANK, so the
    right code can be selected structurally instead of hoped for.

    LOINC is licensed: the table must not be redistributed. Load it locally,
    keep the release out of version control, and ship the loader."""
    import csv
    src = find_loinc_table(path)
    if not src:
        raise FileNotFoundError(
            "Loinc.csv not found. Unpack a LOINC release into the repo, or set "
            f"{LOINC_DIR_ENV}. Download: https://loinc.org/downloads/ (free account)")
    concepts, n = [], 0
    version = os.path.basename(os.path.dirname(os.path.dirname(src))).replace("Loinc_", "")
    with open(src, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            if active_only and row.get("STATUS") != "ACTIVE":
                continue
            scale = row.get("SCALE_TYP") or ""
            if scale not in QUANTITATIVE_SCALES:
                # Ordinal and nominal codes are ANSWERS ("Estimated or measured
                # glomerular filtration rate less than 60"), not measurements.
                # They share every token with the real code and win on overlap,
                # and you cannot threshold them.
                continue
            rank = row.get("COMMON_TEST_RANK") or "0"
            if max_rank and rank.isdigit() and int(rank) > max_rank:
                continue
            code = row["LOINC_NUM"]
            unit = (row.get("EXAMPLE_UCUM_UNITS") or "").strip() or None
            name = (row.get("LONG_COMMON_NAME") or "").strip()
            if not name:
                continue
            rank_i = int(rank) if rank.isdigit() else 0
            concepts.append(Concept(LOINC, code, name, "lab", version, unit, rank_i))
            short = (row.get("SHORTNAME") or "").strip()
            if short and short.lower() != name.lower():
                concepts.append(Concept(LOINC, code, short, "lab", version, unit, rank_i))
            n += 1
            if len(concepts) >= 5000:
                index.add(concepts); concepts = []
    if concepts:
        index.add(concepts)
    index.set_version(LOINC, version)
    return n

# ------------------------------------------------------------- SNOMED load

# SNOMED's Fully Specified Name ends in a semantic tag -- "Diabetes mellitus
# (disorder)", "Central venous catheter (physical object)". That tag says which
# hierarchy a concept lives in WITHOUT reading the 382 MB relationship file, so
# it is how concepts are routed to kinds here.
SNOMED_TAG_KIND = {
    "disorder": "condition", "finding": "condition", "situation": "condition",
    "event": "condition",
    # A device IN PLACE is a patient state, not a procedure. "In pediatric
    # patients with a short-term CVAD" is about the catheter being present; the
    # procedure hierarchy only offers *repair of* and *maintenance of* one.
    "physical object": "condition", "observable entity": "condition",
    "procedure": "procedure", "regime/therapy": "procedure",
    "substance": "drug", "product": "drug",
    "medicinal product": "drug", "medicinal product form": "drug",
    "clinical drug": "drug",
}
_FSN_TAG = re.compile(r"\(([^()]+)\)\s*$")

def find_snomed_dir(path: str | None = None) -> str | None:
    """Locate an unpacked RF2 release's Snapshot/Terminology directory."""
    roots = [p for p in (path, os.environ.get("SNOMED_DIR"),
                         os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
             if p]
    for root in roots:
        if os.path.isdir(root) and os.path.basename(root) == "Terminology":
            return root
        for base, dirs, _files in os.walk(root):
            if os.path.basename(base) == "Terminology" and "Snapshot" in base:
                return base
    return None

def load_snomed(index: SqliteIndex, path: str | None = None, *,
                kinds: set | None = None) -> int:
    """Load SNOMED CT descriptions into the local index.

    SYNONYMS ARE THE POINT. A guideline writes "CVAD"; the preferred term is
    "Central venous catheter". Without the synonym rows a keyword search cannot
    connect them, which is why the free endpoints returned *Structure of central
    axillary lymph node* for that query. RF2 ships every synonym.

    Reads Snapshot (current state), not Full (every historical revision), and
    keeps only ACTIVE rows in clinically relevant hierarchies -- about 250 MB of
    a 3.6 GB release."""
    term_dir = find_snomed_dir(path)
    if not term_dir:
        raise FileNotFoundError(
            "SNOMED RF2 Snapshot not found. Unpack a release into the repo or set "
            "SNOMED_DIR. Download (UMLS licence): "
            "https://www.nlm.nih.gov/healthit/snomedct/international.html")
    files = os.listdir(term_dir)
    desc = next((f for f in files if f.startswith("sct2_Description_Snapshot")), None)
    conc = next((f for f in files if f.startswith("sct2_Concept_Snapshot")), None)
    if not desc or not conc:
        raise FileNotFoundError(f"Description/Concept snapshot missing in {term_dir}")
    version = re.search(r"(\d{8})", desc)
    version = version.group(1) if version else "unknown"
    kinds = kinds or {"condition", "procedure", "drug"}

    active = set()
    with open(os.path.join(term_dir, conc), encoding="utf-8") as fh:
        next(fh, None)
        for line in fh:
            p = line.split("\t")
            if len(p) > 2 and p[2] == "1":
                active.add(p[0])

    # First pass: the FSN gives each concept its semantic tag, hence its kind.
    kind_of: dict[str, str] = {}
    with open(os.path.join(term_dir, desc), encoding="utf-8") as fh:
        next(fh, None)
        for line in fh:
            p = line.rstrip("\n").split("\t")
            if len(p) < 8 or p[2] != "1" or p[6] != "900000000000003001":
                continue                       # inactive, or not the FSN
            if p[4] not in active:
                continue
            m = _FSN_TAG.search(p[7])
            k = SNOMED_TAG_KIND.get((m.group(1).strip().lower() if m else ""), None)
            if k in kinds:
                kind_of[p[4]] = k

    # Second pass: every active description of a kept concept, synonyms included.
    n, batch = 0, []
    with open(os.path.join(term_dir, desc), encoding="utf-8") as fh:
        next(fh, None)
        for line in fh:
            p = line.rstrip("\n").split("\t")
            if len(p) < 8 or p[2] != "1":
                continue
            k = kind_of.get(p[4])
            if not k:
                continue
            term = _FSN_TAG.sub("", p[7]).strip() if p[6] == "900000000000003001" else p[7]
            if not term:
                continue
            batch.append(Concept(SNOMED, p[4], term, k, version))
            n += 1
            if len(batch) >= 20000:
                index.add(batch); batch = []
    if batch:
        index.add(batch)
    index.set_version(SNOMED, version)
    return n

# ------------------------------------------------------------- RxNorm load

# RxNorm term types worth indexing. IN/PIN are ingredients (what a guideline
# names), BN a brand, SCD/SBD a dispensable product. Everything else is
# packaging and dose-form noise.
RXNORM_TTY = {"IN", "PIN", "MIN", "BN", "SCD", "SBD", "SCDC", "PSN"}

def find_rxnorm_file(path: str | None = None) -> str | None:
    roots = [p for p in (path, os.environ.get("RXNORM_DIR"),
                         os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
             if p]
    for root in roots:
        if os.path.isfile(root):
            return root
        for base, _dirs, files in os.walk(root):
            if "RXNCONSO.RRF" in files and "prescribe" in base:
                return os.path.join(base, "RXNCONSO.RRF")
    for root in roots:                          # fall back to the full release
        for base, _dirs, files in os.walk(root):
            if "RXNCONSO.RRF" in files:
                return os.path.join(base, "RXNCONSO.RRF")
    return None

def load_rxnorm(index: SqliteIndex, path: str | None = None) -> int:
    """Load RxNorm drug names into the local index.

    Prefers the `prescribe/` subset -- currently prescribable content, and the
    portion NLM states is freely redistributable. Fixes the gap where RxClass
    `byName` needed an exact class name: "ACE inhibitor" got 0 hits while
    "ACE INHIBITORS, PLAIN" got 2."""
    src = find_rxnorm_file(path)
    if not src:
        raise FileNotFoundError(
            "RXNCONSO.RRF not found. Unpack an RxNorm release into the repo or set "
            "RXNORM_DIR. Download (UMLS licence): "
            "https://www.nlm.nih.gov/research/umls/rxnorm/docs/rxnormfiles.html")
    version = re.search(r"(\d{8})", os.path.basename(os.path.dirname(
        os.path.dirname(src))) or "")
    version = version.group(1) if version else "unknown"
    n, batch = 0, []
    with open(src, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            p = line.split("|")
            # RXCUI | LAT | ... | SAB(11) | TTY(12) | ... | STR(14) | ... | SUPPRESS(16)
            if len(p) < 17 or p[1] != "ENG" or p[12] not in RXNORM_TTY:
                continue
            if p[16] not in ("N", ""):
                continue                        # suppressed
            term = p[14].strip()
            if not term:
                continue
            batch.append(Concept(RXNORM, p[0], term, "drug", version))
            n += 1
            if len(batch) >= 20000:
                index.add(batch); batch = []
    if batch:
        index.add(batch)
    index.set_version(RXNORM, version)
    return n

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
    # A synonym row for the same code. Real terminologies carry many terms per
    # concept, and without the short form "eGFR" -- the form guidelines
    # actually use -- the phrase reaches only the oncology EGFR gene assays.
    Concept(LOINC, "33914-3", "eGFR", "lab", "2.78", "mL/min/1.73m2"),
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
    """The offline test vocabulary. Semantic fallback is OFF: a fixture is a
    closed world, so "not in it" is the answer rather than a retrieval failure
    -- and loading a 974 MB vector index made the test suite four times slower
    for no signal."""
    ix = SqliteIndex(path)
    ix.semantic = False
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
