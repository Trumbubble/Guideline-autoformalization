"""
Clause-level scoring of generated CQL against a hand-authored reference (M2).

The experiment M2 asks: does the determinism machinery buy MEANING, or only
compilation? Scoring is therefore per CLAUSE and per FIELD -- predicate,
comparator, threshold, unit, value set -- not per library and not by string
similarity. A library that compiles and gets the threshold wrong scores zero on
the threshold field, which is the distinction the whole paper rests on.

ONE EXTRACTOR, BOTH SIDES. The reference is CQL and our output is CQL, so the
same parser reads both. That removes a whole class of scoring artifact: a
difference in the score cannot come from having parsed the two sides
differently.

WHAT IT IS NOT. This is a structural comparison, not a semantic one. Two
clauses that mean the same thing written differently (`>= 140` vs `not < 140`)
score as a mismatch. `dsl.equivalent` is the semantic oracle and needs both
sides in the DSL; this works on CQL text, which is what a reference artifact
actually gives you.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

# ---------------------------------------------------------------- clauses

@dataclass(frozen=True)
class Clause:
    """One atomic, checkable assertion pulled out of a CQL library."""
    kind: str                      # comparison | age | retrieve | temporal
    subject: str                   # normalised predicate
    op: str | None = None
    value: str | None = None
    unit: str | None = None
    valueset: str | None = None

    def key(self) -> tuple:
        return (self.kind, self.op, self.value, self.unit, self.valueset)

_WS = re.compile(r"\s+")
_STOP = {"the", "a", "an", "of", "in", "on", "most", "recent", "lowest", "highest",
         "first", "last", "has", "is", "reading", "readings", "value", "result",
         "results", "day", "during", "measurement", "period", "qualifying"}

def norm(s: str) -> str:
    s = _WS.sub(" ", (s or "").strip().strip('"').lower())
    return re.sub(r"[^a-z0-9 .\-/\[\]]", "", s)

def tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", norm(s)) if t not in _STOP}

# `X < 140 'mm[Hg]'` / `AgeInYears() >= 18` / `X <= 9 '%'`
_CMP = re.compile(
    r'("(?:[^"]+)"|[A-Za-z_][\w.()]*)\s*(<=|>=|!=|<>|=|<|>)\s*'
    r"(-?\d+(?:\.\d+)?)\s*(?:'([^']*)')?")
_AGE = re.compile(r"AgeInYears(?:At)?\s*\([^)]*\)\s*(<=|>=|=|<|>)\s*(\d+)")
_RETRIEVE = re.compile(r'\[\s*(\w+)\s*:\s*"([^"]+)"\s*\]')
_INTERVAL = re.compile(r"Interval\[[^\]]*?(\d+)\s*(day|days|month|months|year|years)")
_DURING = re.compile(r'\bduring\s+("(?:[^"]+)"|\w+)')

def extract_clauses(cql_text: str) -> list[Clause]:
    """Every atomic assertion in a CQL library, as comparable tuples.

    Comments are stripped first: an expert artifact carries long rationale
    comments that contain numbers, and counting those as clauses would inflate
    the reference side."""
    text = re.sub(r"//[^\n]*", " ", cql_text)
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    out: list[Clause] = []

    for m in _AGE.finditer(text):
        out.append(Clause("age", "age", _canon_op(m.group(1)), m.group(2), "years"))
    for m in _CMP.finditer(text):
        subj = m.group(1)
        if "AgeInYears" in subj:
            continue                       # already captured as an age clause
        out.append(Clause("comparison", norm(subj), _canon_op(m.group(2)),
                          m.group(3), _canon_unit(m.group(4))))
    for m in _RETRIEVE.finditer(text):
        out.append(Clause("retrieve", norm(m.group(2)), valueset=norm(m.group(2))))
    for m in _INTERVAL.finditer(text):
        out.append(Clause("temporal", "window", "within", m.group(1),
                          m.group(2).rstrip("s")))
    for m in _DURING.finditer(text):
        out.append(Clause("temporal", norm(m.group(1)), "during"))
    return _dedupe(out)

def _canon_op(op: str) -> str:
    return {"=": "==", "<>": "!="}.get(op, op)

def _canon_unit(u: str | None) -> str | None:
    if not u:
        return None
    u = u.strip()
    return {"mm[Hg]": "mmhg", "mmHg": "mmhg", "%": "%", "mg/dL": "mg/dl",
            "d": "day", "a": "year", "mo": "month"}.get(u, u.lower())

def _dedupe(cs: list[Clause]) -> list[Clause]:
    seen, out = set(), []
    for c in cs:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out

# ---------------------------------------------------------------- matching

FIELDS = ("kind", "op", "value", "unit", "valueset")

def subject_similarity(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)

def match(ref: Clause, got: Clause) -> float:
    """How well one generated clause matches one reference clause. Requires the
    same kind; then subject similarity dominates, with the checkable fields as
    tie-breakers."""
    if ref.kind != got.kind:
        return 0.0
    s = subject_similarity(ref.subject, got.subject)
    if ref.kind in {"comparison", "age"}:
        if ref.op == got.op:
            s += 0.25
        if ref.value == got.value:
            s += 0.5
        if ref.unit == got.unit:
            s += 0.25
    return s

@dataclass
class Score:
    matched: int = 0
    n_ref: int = 0
    n_got: int = 0
    field_hits: dict = None
    pairs: list = None

    def __post_init__(self):
        self.field_hits = self.field_hits or {f: 0 for f in FIELDS}
        self.pairs = self.pairs or []

    @property
    def precision(self) -> float:
        return self.matched / self.n_got if self.n_got else 0.0

    @property
    def recall(self) -> float:
        return self.matched / self.n_ref if self.n_ref else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    def field_accuracy(self, field: str) -> float:
        return self.field_hits[field] / self.matched if self.matched else 0.0

    def as_dict(self) -> dict:
        return {"n_ref": self.n_ref, "n_got": self.n_got, "matched": self.matched,
                "precision": round(self.precision, 3), "recall": round(self.recall, 3),
                "f1": round(self.f1, 3),
                "fields": {f: round(self.field_accuracy(f), 3) for f in FIELDS}}

MATCH_THRESHOLD = 0.4

def score_clauses(reference: Iterable[Clause], generated: Iterable[Clause],
                  threshold: float = MATCH_THRESHOLD) -> Score:
    """Greedy best-first matching, then per-field accuracy over matched pairs.

    Greedy rather than optimal assignment: with tens of clauses the difference
    is negligible, and greedy is inspectable -- `Score.pairs` shows exactly
    which reference clause each generated clause was scored against, which is
    what makes a disagreement adjudicable rather than a number."""
    ref, got = list(reference), list(generated)
    sc = Score(n_ref=len(ref), n_got=len(got))
    cand = sorted(((match(r, g), i, j) for i, r in enumerate(ref)
                   for j, g in enumerate(got) if match(r, g) >= threshold),
                  key=lambda t: -t[0])
    used_r, used_g = set(), set()
    for s, i, j in cand:
        if i in used_r or j in used_g:
            continue
        used_r.add(i); used_g.add(j)
        sc.matched += 1
        sc.pairs.append((ref[i], got[j], round(s, 3)))
        for f in FIELDS:
            if getattr(ref[i], f) == getattr(got[j], f):
                sc.field_hits[f] += 1
    return sc

def score_cql(reference_cql: str, generated_cql: str, **kw) -> Score:
    return score_clauses(extract_clauses(reference_cql),
                         extract_clauses(generated_cql), **kw)
