"""
Segmenter: guideline document -> recommendation Statements with provenance.

Stage 1 of Track A. Feeds the refusal gate and the synthesizer, which read
`Statement.text` and must be able to point a clinician at the exact sentence it
came from.

Three loaders, one output type:
  load_jats  -- PMC Open Access XML. THE MAIN PATH: the publisher already
                tagged <boxed-text> and <table-wrap> at typesetting time, so
                the "boxed callout" problem is solved structurally.
  load_html  -- USPSTF and similar web-published guidelines.
  load_pdf   -- fallback. Layout model (optional) + a deterministic geometric
                pass that recovers boxes from PDF drawing primitives.

Two rules the whole file is built around:

  RECALL OVER PRECISION. A false positive becomes a `not_patient_level`
  refusal downstream: visible, cheap, and reported in the residue. A false
  negative is invisible and silently corrupts the formalizability denominator.
  Tune the prefilter LOW.

  NEVER SYNTHESIZE. Every Statement's text must be a verbatim substring of its
  source block, and every Statement carries the offsets to prove it. A
  paraphrase has no source span and cannot be adjudicated by a clinician.

  The ONE deliberate exception is `kind == "table_row"`, whose text is a
  rendering of the row's cells with the header names folded in ("Finding: eGFR
  below 30; Action: refer to nephrology"). A recommendation table splits the
  condition and the action across columns, so no contiguous span of the
  document is the rule. Provenance still points at the <tr>, every cell value
  is verbatim, and the kind field says the text was assembled -- but a
  clinician adjudicating one of these is shown the row, not a character span.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Callable, Iterable

# ---------------------------------------------------------------- data model

BlockKind = str   # boxed_text | table_row | section_text | list_item | heading | figure

@dataclass(frozen=True)
class Provenance:
    """Where a span came from. `locator` is the stable re-find key: an XPath
    for XML/HTML, "p<page>:b<n>" for PDF. char offsets index the RAW block
    text, not the normalized text, so a UI can highlight the original."""
    doc_id: str
    locator: str
    char_start: int
    char_end: int
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None

    def key(self) -> str:
        return f"{self.doc_id}|{self.locator}|{self.char_start}:{self.char_end}"

@dataclass(frozen=True)
class Block:
    """One layout element, before any recommendation judgement.

    `text` is verbatim from the document except when kind == "table_row", where
    it is a header-folded rendering of the row's cells (see the module
    docstring)."""
    text: str                       # raw text, as extracted
    kind: BlockKind
    prov: Provenance
    section_path: tuple[str, ...] = ()
    box_id: str | None = None       # groups blocks recovered from one callout
    # The full text of the element this unit was split out of, and where that
    # text starts in the source element. The classifier quotes against THIS,
    # not against `text`, so a sentence the splitter cut in the wrong place is
    # still recoverable -- see classify().
    context: str = ""
    context_start: int = 0

    @property
    def source_text(self) -> str:
        return self.context or self.text

    @property
    def source_start(self) -> int:
        return self.context_start if self.context else self.prov.char_start

@dataclass(frozen=True)
class Statement:
    """A candidate recommendation. `text` is verbatim from the source block."""
    statement_id: str
    text: str
    kind: BlockKind
    prov: Provenance
    section_path: tuple[str, ...] = ()
    strength_label: str | None = None
    evidence_label: str | None = None
    prefilter_score: float = 0.0
    prefilter_features: tuple[str, ...] = ()
    is_recommendation: bool = True
    is_patient_level: bool | None = None
    classifier_reason: str = ""
    classifier: str = "none"                    # which judge decided
    context_locators: tuple[str, ...] = ()      # resolved within-doc cross-refs
    duplicate_of: str | None = None             # statement_id kept in its place
    also_at: tuple[str, ...] = ()               # provenance keys of duplicates

# ------------------------------------------------------- offset-safe normalize

_SUBST = {
    " ": " ", " ": " ", " ": " ", " ": " ",
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "≤": "<=", "≥": ">=", "±": "+/-", "×": "x",
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl",
}
_DEHYPHEN = re.compile(r"-[ \t]*\r?\n[ \t]*(?=[a-z])")

class Text:
    """Normalized text that remembers where every character came from.

    Normalization is where provenance usually dies: de-hyphenation and
    whitespace collapse shift every downstream offset. Keeping an index map
    means a sentence found in `.s` can always be reported against the raw
    string, which is what the UI highlights and what the clinician reads."""

    __slots__ = ("raw", "s", "_map")

    def __init__(self, raw: str):
        self.raw = raw
        out: list[str] = []
        idx: list[int] = []
        i, n = 0, len(raw)
        while i < n:
            c = raw[i]
            m = _DEHYPHEN.match(raw, i)
            if m:                                   # "kid-\ney" -> "kidney"
                i = m.end()
                continue
            if c in _SUBST:
                for ch in _SUBST[c]:
                    out.append(ch); idx.append(i)
                i += 1
                continue
            if c.isspace():
                j = i
                while j < n and raw[j].isspace():
                    j += 1
                if out and out[-1] != " ":
                    out.append(" "); idx.append(i)
                i = j
                continue
            out.append(c); idx.append(i)
            i += 1
        while out and out[-1] == " ":
            out.pop(); idx.pop()
        self.s = "".join(out)
        self._map = idx

    def __len__(self): return len(self.s)

    def raw_span(self, start: int, end: int) -> tuple[int, int]:
        """Map a [start, end) span in `.s` back to a span in `.raw`."""
        if not self._map or start >= end:
            return (0, 0)
        start = max(0, min(start, len(self._map) - 1))
        end = max(start + 1, min(end, len(self._map)))
        return (self._map[start], self._map[end - 1] + 1)

# ---------------------------------------------------------------- sentences

# Abbreviations that end in a period and do NOT end a sentence. Clinical prose
# is dense with these; a generic splitter shatters "2.5 mg b.i.d. for 4 weeks".
_ABBREV = {
    "e.g", "i.e", "vs", "etc", "cf", "approx", "ca", "al", "fig", "figs", "no",
    "dr", "prof", "mr", "mrs", "ms", "st", "inc", "ltd", "dept", "univ",
    "b.i.d", "t.i.d", "q.i.d", "q.d", "p.o", "i.v", "i.m", "s.c", "p.r.n",
    "mg", "ml", "kg", "mcg", "mmol", "u.s", "u.k", "sec", "min", "hr", "wk", "yr",
}
_BOUNDARY = re.compile(r"[.!?]['\")\]]*\s+(?=[\"'(\[]?[A-Z0-9])")
_TOKEN_BEFORE = re.compile(r"([A-Za-z0-9.]+)$")

def split_sentences(s: str) -> list[tuple[int, int]]:
    """Sentence spans over `s`. Returns [(start, end), ...] with no gaps lost."""
    spans, start = [], 0
    for m in _BOUNDARY.finditer(s):
        cut = m.end()
        head = s[start:m.start() + 1]
        tok = _TOKEN_BEFORE.search(s[start:m.start()])
        word = (tok.group(1) if tok else "").lower().rstrip(".")
        if word in _ABBREV:
            continue
        if len(word) == 1 and word.isalpha():           # initials: "J. Smith"
            continue
        if word.isdigit() and len(head.strip()) <= 4:   # list marker: "1. Offer"
            continue
        spans.append((start, cut))
        start = cut
    if start < len(s):
        spans.append((start, len(s)))
    return [(a, b) for a, b in spans if s[a:b].strip()]

# One sentence can carry two recommendations, joined by a semicolon. Measured on
# PMC12355350: "...weak recommendation in favour of SGLT-2 inhibitors or GLP-1
# receptor agonists; AND a weak recommendation against finerenone in adults with
# CKD" was formalized as ONE rule -- the first clause encoded, the second
# silently discarded, with `on_finerenone` bound and sitting unused in the
# registry.
#
# Splitting is deliberately narrow. Only seams where the guideline restarts its
# own recommendation grammar count; a semicolon between two conditions
# ("...without established CVD or CKD; or established CVD...") is part of ONE
# rule and splitting it would destroy the population.
_CLAUSE_SEAM = re.compile(
    r";\s+(?=and\s+(a|an)\s+(weak|strong|conditional|ungraded|good\s+practice)\b)"
    r"|;\s+(?=(and\s+)?we\s+(also\s+)?(recommend|suggest|advise)\b)"
    r"|;\s+(?=(and\s+)?(a|an)\s+(weak|strong|conditional)\s+recommendation\b)",
    re.I)

MIN_CLAUSE_CHARS = 25

def split_clauses(s: str, start: int, end: int) -> list[tuple[int, int]]:
    """Subdivide one sentence at recommendation seams. Offsets stay absolute so
    provenance survives -- the character span still points at the source."""
    frag = s[start:end]
    cuts = [start] + [start + m.end() for m in _CLAUSE_SEAM.finditer(frag)] + [end]
    spans = [(a, b) for a, b in zip(cuts, cuts[1:]) if b - a >= MIN_CLAUSE_CHARS]
    return spans or [(start, end)]

# ---------------------------------------------------------------- prefilter

def _rx(p): return re.compile(p, re.I)

# (name, weight, pattern). Positive weights are recommendation signals;
# negative weights are the sections a guideline puts its own methodology in.
PREFILTER_PATTERNS: list[tuple[str, float, re.Pattern]] = [
    # -- explicit grading schemes: the strongest signal a guideline emits ----
    ("grade_verbal",   4.0, _rx(r"\b(strong|weak|conditional)\s+recommendation\b")),
    ("grade_certainty",3.0, _rx(r"\b(high|moderate|low|very[\s-]low)\s+(certainty|quality)\b")),
    ("grade_kdigo",    4.0, _rx(r"\(\s*(?:grade\s+)?[12][ABCD]\s*\)|\bgrade[:\s]+[12][ABCD]\b")),
    ("grade_acc_aha",  4.0, _rx(r"\bclass\s+(?:I{1,3}|IV|2a|2b)\b|\blevel\s+of\s+evidence\b")),
    ("grade_uspstf",   3.0, _rx(r"\bgrade\s+[ABCD]\s+recommendation\b|\b\(grade\s+[ABCDI]\)")),
    ("grade_sign",     2.0, _rx(r"\bevidence\s+level\s+[1-4][+\-]?\b")),
    ("numbered_rec",   3.0, _rx(r"\b(recommendation|statement)\s*\d+(\.\d+)*\b|^\s*R\d+\b")),
    # -- deontic language ---------------------------------------------------
    ("deontic_first",  3.0, _rx(r"\bwe\s+(recommend|suggest|advise)\b")),
    ("deontic_passive",3.0, _rx(r"\b(is|are)\s+(recommended|indicated|contraindicated|advised)\b")),
    ("deontic_should", 2.5, _rx(r"\bshould\s+(not\s+)?(be\b|receive\b|have\b|undergo\b|start\b|>)")),
    ("deontic_must",   2.5, _rx(r"\bmust\s+(not\s+)?be\b")),
    ("deontic_nice",   2.5, _rx(r"^\s*(offer|do not offer|consider|do not use|start|stop)\b")),
    ("deontic_weak",   1.5, _rx(r"\b(consider|may be considered|is reasonable|avoid|first[- ]line)\b")),
    # A recommendation table names the action in a column, and the cell is a
    # bare imperative with no deontic auxiliary anywhere in the row.
    ("table_action",   2.0, _rx(r"\b(action|recommendation|management|advice)\s*:")),
    ("imperative",     2.0, _rx(r"(^|:\s*)(refer|monitor|offer|screen|measure|check|repeat|"
                               r"start|stop|avoid|discontinue|reduce|increase|prescribe|treat)\b")),
    ("dose_threshold", 1.0, _rx(r"\b\d+(\.\d+)?\s*(mg|ml|mmol/l|mg/g|ml/min|g/dl|%|days?|weeks?|months?)\b")),
    # -- negative: the guideline talking about itself -----------------------
    ("cites_study",   -2.0, _rx(r"\b[A-Z][a-z]+\s+et\s+al\b|\bin\s+a\s+(randomi[sz]ed|cohort)\b")),
    ("research_gap",  -2.5, _rx(r"\b(further|more)\s+(research|studies|trials)\s+(is|are)\s+needed\b")),
    ("methods_voice", -2.5, _rx(r"\bwe\s+(searched|included|excluded|assessed|screened|extracted)\b")),
]

# Section titles whose contents are never patient-level recommendations.
NON_RECOMMENDATION_SECTIONS = _rx(
    r"\b(methods?|search strategy|funding|conflicts? of interest|disclosur|"
    r"acknowledg|references|contributors|appendix|abbreviations|"
    r"peer review|copyright|licen[cs]e)\b")
RECOMMENDATION_SECTIONS = _rx(r"\brecommend|\bguidance\b|\bkey points?\b|\bstatements?\b")

STRUCTURAL_WEIGHTS = {"boxed_text": 2.5, "table_row": 1.5, "list_item": 0.5,
                      "section_text": 0.0, "heading": -1.0, "figure": -1.0}

# Kinds that carry a recommendation by their own structure, whatever the words
# are. A guideline that prints its full recommendation set in an appendix would
# otherwise lose every one of them: boxed_text (+2.5) plus
# section:non_recommendation (-4.0) lands at -1.5 and is dropped before anyone
# reads it. Structure outranks the section title.
STRUCTURAL_FLOOR = {"boxed_text", "table_row"}

PREFILTER_THRESHOLD = 1.5   # deliberately low; see the recall rule up top

def prefilter(block: Block) -> tuple[float, tuple[str, ...]]:
    """Deterministic, auditable candidate scoring. Returns (score, features)."""
    score, feats = 0.0, []
    text = block.text
    for name, weight, pat in PREFILTER_PATTERNS:
        if pat.search(text):
            score += weight
            feats.append(name)
    w = STRUCTURAL_WEIGHTS.get(block.kind, 0.0)
    if w:
        score += w
        feats.append(f"kind:{block.kind}")
    joined = " > ".join(block.section_path)
    if RECOMMENDATION_SECTIONS.search(joined):
        score += 2.0
        feats.append("section:recommendation")
    if NON_RECOMMENDATION_SECTIONS.search(joined):
        score -= 4.0
        feats.append("section:non_recommendation")
    if block.kind in STRUCTURAL_FLOOR and score < PREFILTER_THRESHOLD:
        score = PREFILTER_THRESHOLD
        feats.append("floor:structural")
    return score, tuple(feats)

# ---------------------------------------------------------------- labels

_LABEL_PATTERNS = [
    _rx(r"\b((?:strong|weak|conditional)\s+recommendation)\b"),
    _rx(r"\(\s*(?:grade\s+)?([12][ABCD])\s*\)"),
    _rx(r"\bgrade\s+([12][ABCD])\b"),
    _rx(r"\b(class\s+(?:I{1,3}|IV|2a|2b))\b"),
    _rx(r"\bgrade\s+([ABCDI])\s+recommendation\b"),
]
_EVIDENCE_PATTERNS = [
    _rx(r"\b((?:high|moderate|low|very[\s-]low)[\s-]+(?:certainty|quality)(?:\s+evidence)?)\b"),
    _rx(r"\blevel\s+of\s+evidence[:\s]+([A-C](?:-[A-Z]+)?)\b"),
    _rx(r"\bevidence\s+level\s+([1-4][+\-]?)\b"),
]

_LABEL_ONLY_MAX_WORDS = 12

# Words that survive stripping a grading label but carry no clinical content.
_LABEL_FILLER = {
    "and", "for", "the", "with", "without", "against", "favour", "favor", "this",
    "that", "these", "those", "all", "adults", "adult", "patients", "patient",
    "group", "risk", "evidence", "certainty", "quality", "grade", "level",
    "strength", "based", "use", "using", "recommendation", "recommendations",
    "statement", "consensus", "ungraded", "practice", "good", "very", "low",
    "moderate", "high", "strong", "weak", "conditional",
}

def is_label_only(text: str) -> bool:
    """True for a span that is ONLY a grading label. Guidelines print these on
    their own line inside the box ("Strong recommendation, moderate certainty
    evidence."); emitted as a statement it is a recommendation that recommends
    nothing, so it is absorbed into the recommendation it labels instead."""
    t = text.strip()
    if not t or len(t.split()) > _LABEL_ONLY_MAX_WORDS:
        return False
    if not (_first_match(_LABEL_PATTERNS, t) or _first_match(_EVIDENCE_PATTERNS, t)):
        return False
    if any(p.search(t) for n, _w, p in PREFILTER_PATTERNS if n.startswith("deontic")):
        return False
    # A label grades a recommendation; it does not CONTAIN one. Measured on
    # PMC12355350: "and a weak recommendation against finerenone in adults with
    # CKD" was absorbed as a label and vanished, taking a whole recommendation
    # with it -- the drug bound, reached the registry, and was never used.
    # Strip the grading vocabulary and see whether clinical content is left.
    residue = t
    for pats in (_LABEL_PATTERNS, _EVIDENCE_PATTERNS):
        for p in pats:
            residue = p.sub(" ", residue)
    words = [w for w in re.findall(r"[a-z][a-z-]{2,}", residue.lower())
             if w not in _LABEL_FILLER]
    return len(words) <= 1

def _first_match(pats, *texts) -> str | None:
    for t in texts:
        if not t:
            continue
        for p in pats:
            m = p.search(t)
            if m:
                return m.group(1).strip()
    return None

# ---------------------------------------------------------------- glossary
# Guidelines define their abbreviations once, on first use, in a fixed shape:
# "Long form (SHORT)". Collecting those in one sweep is what stops the
# formalizer minting two variables for one concept -- PMC13270348 wrote both
# `pca` and `patient_controlled_intravenous_analgesia` into a single rule,
# which made it the tautology (A and B) -> B. See NOTES.md.
#
# This is Schwartz & Hearst (2003), the standard biomedical abbreviation
# matcher: deterministic, ~60 lines, no model. It is deliberately NOT an LLM
# synonym pass -- merging "acute pancreatitis" with "severe acute
# pancreatitis" would be a clinical error, and a model asked "do these mean
# the same thing?" will make it. Only two signals are trusted to merge
# concepts: a definition stated in the document (here) and a shared
# terminology code (bind.py). Both are ground truth; judgement is not.

# Sections whose acronyms are bibliographic or organisational, not clinical.
_GLOSSARY_SKIP = re.compile(
    r"reference|acknowledg|competing|conflict|funding|contribut|appendix|"
    r"supplement|author information", re.I)

_PARENTHETICAL = re.compile(r"\(([^()]{2,40})\)")

# Statistics and study-design tokens. Some do get defined correctly (HR ->
# hazard ratio) but they are measurement vocabulary, never clinical concepts,
# and expanding them inside a retrieval query is noise at best -- "HR" in a
# cardiology guideline means heart rate, whatever this document defined it as.
# Excluded from the glossary rather than trusted and filtered downstream.
_ABBREV_STOPLIST = {
    "RR", "OR", "aOR", "CI", "95% CI", "95%CI", "HR", "SD", "SE", "IQR",
    "NNT", "ARR", "MD", "SMD", "ROC", "AUC", "ITT", "PP", "NO",
}

def _is_short_form(s: str) -> bool:
    """Schwartz-Hearst short-form test: 2-10 chars, <=2 words, starts
    alphanumeric, contains a letter, and carries at least one capital."""
    return (2 <= len(s) <= 10 and s[0].isalnum() and len(s.split()) <= 2
            and any(c.isalpha() for c in s)
            and any(c.isupper() for c in s))

def _best_long_form(short: str, window: str) -> str | None:
    """Walk both strings right-to-left. Every character of the short form must
    appear in the window in order, and the short form's FIRST character must
    land at the start of a word -- that word boundary is what stops "AP"
    matching the tail of "therapy"."""
    s, l = len(short) - 1, len(window) - 1
    while s >= 0:
        c = short[s].lower()
        if not c.isalnum():
            s -= 1
            continue
        while (l >= 0 and window[l].lower() != c) or \
              (s == 0 and l > 0 and window[l - 1].isalnum()):
            l -= 1
        if l < 0:
            return None
        l -= 1
        s -= 1
    return window[l + 1:].strip()

def _long_form_for(short: str, before: str) -> str | None:
    # S&H window: the definition is never far from the parenthesis.
    words = before.split()
    if not words:
        return None
    window = " ".join(words[-min(len(short) + 5, len(short) * 2):])
    long = _best_long_form(short, window)
    if not long:
        return None
    # An expansion that is shorter than what it expands, or that already
    # contains the short form spelled out, is a false match.
    if len(long.split()) > min(len(short) + 5, len(short) * 2):
        return None
    if len(long) <= len(short) or short.lower() in long.lower().split():
        return None
    # A "definition" that runs through a bracket or a sentence boundary is the
    # matcher having walked out of the phrase and into the surrounding prose:
    # PMC13276589 offered `95% CI -> "95% CI, 0.23-0.76). There were 246 fewer
    # recurrent thrombotic events"`. Cheap guard, kills the whole class.
    if "(" in long or ")" in long or re.search(r"\.\s+[A-Z]", long):
        return None
    return long

def extract_abbreviations(blocks: Iterable["Block"],
                          min_uses: int = 1) -> dict[str, str]:
    """One pass over the whole document -> {SHORT: long form}.

    `min_uses` is how many times the short form must appear again AFTER its
    definition to be kept. Defining an acronym and never reusing it is the
    signature of a society name in an author list (ESDO, EAGEN), not of a
    clinical concept; requiring one reuse drops those without a stoplist.

    This is the only stage that sees the whole document -- every later stage
    works one statement at a time -- so the sweep has to happen here."""
    body = [b for b in blocks
            if not any(_GLOSSARY_SKIP.search(s) for s in b.section_path)]
    text = "\n".join(b.text for b in body)
    found: dict[str, str] = {}
    for m in _PARENTHETICAL.finditer(text):
        # "(PCA)" but also "(patient-controlled analgesia, PCA)"
        for cand in [m.group(1)] + [p.strip() for p in m.group(1).split(",")]:
            if not _is_short_form(cand):
                continue
            long = _long_form_for(cand, text[max(0, m.start() - 200):m.start()])
            if cand in _ABBREV_STOPLIST:
                break
            if long and cand not in found:
                found[cand] = long
            break
    if min_uses:
        for short in list(found):
            uses = len(re.findall(rf"(?<![A-Za-z]){re.escape(short)}(?![A-Za-z])",
                                  text))
            if uses - 1 < min_uses:        # -1 for the definition itself
                del found[short]
    return found

def expand_abbreviations(text: str, abbrevs: dict[str, str]) -> str:
    """Rewrite short forms to their definitions, for RETRIEVAL QUERIES ONLY.

    Never call this on a Statement's stored text: `text` is verbatim from the
    source and its character offsets index the original document. Rewriting it
    breaks provenance and the classifier's verbatim-quote check. The binder
    searches on the expansion; the record keeps the original."""
    for short, long in sorted(abbrevs.items(), key=lambda kv: -len(kv[0])):
        text = re.sub(rf"(?<![A-Za-z]){re.escape(short)}(?![A-Za-z])",
                      long, text)
    return text

# ------------------------------------------------------- deterministic vetoes
# The prefilter scores directive VOCABULARY, and a sentence can carry that
# vocabulary while talking ABOUT a recommendation rather than making one:
# "this result should be interpreted with caution" is about a confidence
# interval. Measured on the gold set, 12 of 45 extracted spans were not
# recommendations at all (RESULTS.md R20).
#
# The model classifier catches 11 of those 12, but it costs a call per
# candidate. These three rules catch most of them for free and with certainty,
# so the model only sees what actually needs judgement. Every one is guarded by
# `_DIRECTIVE`: a sentence that also states a real obligation is never vetoed,
# because table rows fold the recommendation together with its evidence grading
# ("We recommend epidural analgesia ...; Certainty of evidence: Weak") and a
# rule keyed on the grading alone would delete the recommendation with it.

_DIRECTIVE = re.compile(
    r"\b(we recommend|we suggest|is recommended|are recommended|is not recommended|"
    r"recommends?|suggests?|advises?|should|must|shall|is indicated|are indicated|"
    r"may be considered|should be considered|avoid|do not use|contraindicated)\b", re.I)

# Statistics and evidence-grading vocabulary. An effect estimate is a finding,
# not an instruction.
_EVIDENCE_MARKERS = re.compile(
    r"\b(odds ratio|hazard ratio|risk ratio|confidence interval|"
    r"certainty (of |in )?(the )?evidence|"
    r"(OR|HR|RR|aOR)\s*[,=]?\s*\d|95%\s*CI|"
    r"n\s*=\s*\d|p\s*[<=]\s*0?\.\d|\d+\s+studies)\b")

# "recommendation" used to REFER to one -- "a strong recommendation", "this
# conditional recommendation", "recommendation 6" -- rather than to make one.
# A leading "Recommendation 12:" is a LABEL on a real recommendation and is
# exempt: numbered guidelines write their statements that way.
# Only the two constructions that cannot introduce a clinical action:
# "recommendations THAT <someone does something>" reports one, and
# "recommendation 6" names one. The looser "a/the/this <adjective>
# recommendation" was tried and vetoed a real statement -- "Although the panel
# made a conditional recommendation for no primary anticoagulant prophylaxis
# for persistently positive APLA, panel members emphasised ..." -- which
# belongs in the residue report as a `defeasible` refusal, not deleted. A
# vetoed span vanishes; a refused one is still counted and explained. Losing it
# understates the residue, so the rule gives up yield for safety.
_RECOMMENDATION_NOUN = re.compile(
    r"\brecommendations?\s+that\b|\brecommendation\s+\d", re.I)
# "Recommendation 12:" but also "Recommendation 12\nIn pediatric patients ..."
# -- a heading on its own line, which is how ASH/ISTH prints them and how the
# extractor delivers them. Requiring punctuation after the number vetoed the
# actual numbered recommendations of an entire guideline.
_RECOMMENDATION_LABEL = re.compile(
    r"^\s*recommendations?\s*\d*\s*([:.\u2013-]|$|\n|\s+[A-Z])", re.I | re.M)

# A sentence that OPENS by reporting research is describing evidence, not
# issuing a recommendation. Kept to sentence-initial position and to a closed
# set of research verbs, because that is what makes it decisive rather than a
# guess about the middle of a clause.
_REPORTING_FRAME = re.compile(
    r"^\s*(results?|findings?|data|evidence|studies|trials?|analys[ei]s|"
    r"observational stud\w+|in randomi[sz]ed|a (survey|systematic review|"
    r"meta-analysis)|the (evidence|data|trial|review))\b[^.]{0,40}?\b"
    r"(indicat\w+|show\w+|suggest\w+|report\w+|found|demonstrat\w+|"
    r"reveal\w+|were|was|is|are|includ\w+)\b", re.I)

# The guideline speaking in its own voice. A sentence that reports research AND
# then issues a recommendation keeps one of these, so the frame veto below can
# be decisive without swallowing "Studies show X; we therefore recommend Y".
_GUIDELINE_VOICE = re.compile(
    r"\b(we (recommend|suggest|advise|do not recommend)|is recommended|"
    r"are recommended|is not recommended|is indicated|are indicated|"
    r"should be (offered|given|used|started|considered))\b", re.I)

def not_a_statement(text: str) -> str | None:
    """A reason this span cannot be a recommendation, or None.

    Deliberately conservative: each rule is vetoed by the presence of directive
    language, so the cost of a miss is one model call and the cost of a false
    veto is a lost recommendation. Those are not symmetric."""
    t = text.strip()
    if not t:
        return "empty"
    # Checked BEFORE the directive guard, because a reported preference can
    # contain a directive word without being one: "patients ... preferred ... to
    # AVOID combination therapy" is a survey result, and `avoid` rescued it.
    if _REPORTING_FRAME.search(t) and not _GUIDELINE_VOICE.search(t):
        return "reports_research"
    if _DIRECTIVE.search(t) and not t.endswith(":"):
        return None
    if t.endswith(":"):
        # "Once the decision has been made to proceed with surgery, the
        # following should be considered:" -- a lead-in to a list. The
        # recommendations are the list items, which are separate blocks.
        return "list_stem"
    if _EVIDENCE_MARKERS.search(t):
        return "evidence_summary"
    if _RECOMMENDATION_NOUN.search(t) and not _RECOMMENDATION_LABEL.match(t):
        return "about_a_recommendation"
    return None

# ---------------------------------------------------------------- classifier

CLASSIFIER_SCHEMA = {
    "type": "object",
    "required": ["quote", "is_recommendation", "is_patient_level", "reason"],
    "additionalProperties": False,
    "properties": {
        # The model must QUOTE, never rewrite. The caller verifies this is a
        # substring of the source block and rejects the result if it is not.
        "quote": {"type": "string"},
        "is_recommendation": {"type": "boolean"},
        "is_patient_level": {"type": "boolean"},
        "strength_label": {"type": ["string", "null"]},
        "action_verb": {"type": ["string", "null"]},
        "reason": {"type": "string"},
    },
}

CLASSIFIER_INSTRUCTIONS = """\
You are triaging candidate spans from a clinical practice guideline.

Decide two things about the span:
  is_recommendation  - does it direct clinical action, as opposed to describing
                       evidence, methods, epidemiology, or the guideline itself?
  is_patient_level   - does it apply to the care of an individual patient, as
                       opposed to a service, commissioner, or research agenda?

Copy the recommendation into `quote` VERBATIM from the ELEMENT below. Do not
paraphrase, merge, expand abbreviations, or fix grammar.

The candidate span was cut by an automatic sentence splitter and may be
truncated mid-sentence. Quote the COMPLETE recommendation as it appears in the
element, even if that runs past the candidate's boundaries. If the
recommendation is only part of the candidate, quote exactly that part. If you
cannot quote it verbatim from the element, set is_recommendation to false and
say why."""

def build_prompt(block: Block) -> str:
    return (f"{CLASSIFIER_INSTRUCTIONS}\n\n"
            f"Section: {' > '.join(block.section_path) or '(none)'}\n"
            f"Element type: {block.kind}\n"
            f"Candidate span: {block.text.strip()!r}\n"
            f"Full element:\n\"\"\"\n{block.source_text}\n\"\"\"")

def _heuristic_verdict(block: Block, score: float, feats: tuple) -> dict:
    """Offline fallback so the pipeline and its tests run with no model."""
    deontic = any(f.startswith("deontic") or f in {"imperative", "table_action"}
                  for f in feats)
    graded = any(f.startswith("grade") or f == "numbered_rec" for f in feats)
    return {"quote": block.text,
            "is_recommendation": bool(deontic or graded),
            "is_patient_level": "section:non_recommendation" not in feats,
            "reason": f"heuristic: score={score:.1f} features={','.join(feats) or 'none'}"}

def classify(block: Block, score: float, feats: tuple,
             llm: Callable[[Block], dict] | None = None) -> tuple[dict, str]:
    """Judge one candidate. `llm` takes a Block and returns CLASSIFIER_SCHEMA.

    The quote is checked against the whole PARENT ELEMENT, not the candidate
    sentence, so the model can widen past a bad sentence split and still be
    verifiable. A quote that is not a verbatim substring of that element is
    REJECTED and the call falls back to the heuristic: an unfaithful quote has
    no provenance, which is worse than a coarse decision."""
    if llm is None:
        return _heuristic_verdict(block, score, feats), "heuristic"
    try:
        v = llm(block)
    except Exception as exc:                       # a dead model must not
        v = _heuristic_verdict(block, score, feats)  # silently drop statements
        v["reason"] = f"llm error, fell back to heuristic: {exc}"
        return v, "heuristic:llm_error"
    quote = (v.get("quote") or "").strip()
    if not quote or quote not in block.source_text:
        v = _heuristic_verdict(block, score, feats)
        v["reason"] = "llm quote was not verbatim; fell back to heuristic"
        return v, "heuristic:quote_rejected"
    return v, "llm"

# ---------------------------------------------------------------- assembly

_CROSSREF = _rx(r"\bsee\s+(?:also\s+)?(section|table|box|figure|appendix)\s+([\w.]+)")

def resolve_crossrefs(stmts: list[Statement], blocks: list[Block]) -> list[Statement]:
    """Within-document reference following. NOT retrieval: the target must be
    in the same document, and both spans are cited, so provenance survives."""
    index: dict[str, str] = {}
    for b in blocks:
        for part in b.section_path:
            m = re.match(r"\s*(?:section\s*)?([\d.]+)", part, re.I)
            if m and m.group(1).strip("."):
                index.setdefault(m.group(1).strip("."), b.prov.locator)
        m = re.match(r"\s*(box|table|figure|appendix)\s*([\w.]+)", b.text, re.I)
        if m:
            index.setdefault(f"{m.group(1).lower()}:{m.group(2).lower()}", b.prov.locator)
    per_block: dict[str, list[str]] = {}
    for st in stmts:
        for m in _CROSSREF.finditer(st.text):
            what, num = m.group(1).lower(), m.group(2).lower().strip(".")
            loc = index.get(num) if what == "section" else index.get(f"{what}:{num}")
            if loc and loc != st.prov.locator:
                per_block.setdefault(st.prov.locator, []).append(loc)
    return [replace(st, context_locators=tuple(dict.fromkeys(per_block[st.prov.locator])))
            if st.prov.locator in per_block else st for st in stmts]

_SUMMARY_SECTION = _rx(r"\b(summary|abstract|key points?|at a glance|synopsis)\b")

def _norm_key(text: str) -> str:
    t = unicodedata.normalize("NFKD", text).lower()
    return hashlib.sha1(re.sub(r"[^a-z0-9]+", "", t).encode()).hexdigest()

def dedupe(stmts: list[Statement]) -> list[Statement]:
    """Guidelines restate recommendations in an executive summary AND the body.
    Counting both inflates the formalizability denominator. Keep the body
    occurrence (more surrounding context) and record where else it appeared."""
    groups: dict[str, list[Statement]] = {}
    for st in stmts:
        groups.setdefault(_norm_key(st.text), []).append(st)
    out = []
    for group in groups.values():
        if len(group) == 1:
            out.append(group[0]); continue
        body = [s for s in group if not _SUMMARY_SECTION.search(" > ".join(s.section_path))]
        keep = (body or group)[0]
        others = [s for s in group if s is not keep]
        out.append(replace(keep, also_at=tuple(s.prov.key() for s in others)))
        out.extend(replace(s, duplicate_of=keep.statement_id) for s in others)
    return sorted(out, key=lambda s: s.statement_id)

# ---------------------------------------------------------------- pipeline

# Headings are navigation: they populate `section_path` and are never
# statements themselves. Without this a <title>Recommendation 1</title> scores
# +3.0 numbered_rec +2.0 section:recommendation -1.0 heading = 4.0 and is
# emitted as a recommendation whose entire content is "Recommendation 1".
NON_STATEMENT_KINDS = {"heading", "figure"}

def blocks_to_units(blocks: Iterable[Block]) -> list[Block]:
    """Split prose into sentences; leave table rows whole.

    A recommendation table puts the condition in one column and the action in
    another, so the ROW is the atom. Sentence-splitting it destroys the rule."""
    units: list[Block] = []
    for b in blocks:
        if b.kind in NON_STATEMENT_KINDS:
            continue
        if b.kind in {"table_row", "heading", "figure"}:
            units.append(replace(b, context=b.text, context_start=b.prov.char_start))
            continue
        t = Text(b.text)
        for s0, e0 in split_sentences(t.s):
          for start, end in split_clauses(t.s, s0, e0):
            rs, re_ = t.raw_span(start, end)
            units.append(replace(
                b, text=b.text[rs:re_],
                context=b.text, context_start=b.prov.char_start,
                prov=replace(b.prov,
                             char_start=b.prov.char_start + rs,
                             char_end=b.prov.char_start + re_)))
    return units

def segment(blocks: list[Block], doc_id: str,
            llm: Callable[[Block], dict] | None = None,
            threshold: float = PREFILTER_THRESHOLD,
            measure_prefilter: bool = False,
            limit: int | None = None) -> tuple[list[Statement], dict]:
    """blocks -> (statements, manifest). The manifest is the extraction-error
    report: it is published alongside the formalizability number so the
    extractor's error is never conflated with the formalizer's.

    `measure_prefilter=True` also runs the classifier over the units the
    prefilter REJECTED and reports where the two disagree. These are the spans
    the threshold costs you: adjudicate them by hand and the survivors are the
    prefilter's true false-negative rate, measured rather than argued. Reported
    as DISAGREEMENTS, not as false negatives, because the classifier is not
    ground truth -- on the fixture it flags "This work should be cited as an
    independent review" purely on the word `should`.

    Only informative with a real `llm`: the offline heuristic reads the same
    features the prefilter scores, so it can only disagree where a negative
    weight outvoted a positive one."""
    units = blocks_to_units(blocks)
    abbrevs = extract_abbreviations(blocks)
    scored = [(u, *prefilter(u)) for u in units]
    candidates, labels, rejected, vetoed = [], {}, [], []
    for u, sc, f in scored:
        if sc < threshold:
            rejected.append((u, sc, f))
            continue
        # Label absorption comes FIRST. "Strong recommendation, moderate
        # certainty evidence" is a GRADE label belonging to a sibling
        # recommendation, and it carries the strength the rule needs -- but it
        # also matches the evidence-summary veto, so checking the veto first
        # threw the label away instead of attaching it.
        if is_label_only(u.text):
            slot = labels.setdefault(u.box_id or u.prov.locator, [None, None])
            slot[0] = slot[0] or _first_match(_LABEL_PATTERNS, u.text)
            slot[1] = slot[1] or _first_match(_EVIDENCE_PATTERNS, u.text)
            continue
        veto = not_a_statement(u.text)
        if veto:
            vetoed.append((u, veto))
            continue
        candidates.append((u, sc, f))

    # A model classifier costs one call per candidate, and a guideline has
    # hundreds: PMC13270348 has 286. A caller that wants five recommendations
    # should not pay to judge 281 it will discard. With `limit` set, candidates
    # are judged in descending prefilter order and judging stops once enough
    # have been found -- which yields the SAME top-N, because that is the order
    # the caller selects in anyway.
    #
    # A margin over `limit` covers what dedupe and the classifier's own
    # rejections remove. Statement ids stay keyed to the candidate's position
    # in the DOCUMENT, so they are stable whether or not a limit was used.
    stmts: list[Statement] = []
    order = list(range(len(candidates)))
    budget = None
    if limit and llm:
        order.sort(key=lambda i: -candidates[i][1])
        budget = limit * 2 + 5
    found = unclassified = 0

    for n in order:
        block, score, feats = candidates[n]
        if budget is not None and found >= budget:
            unclassified += 1
            continue
        sibling_labels = labels.get(block.box_id or block.prov.locator, (None, None))
        verdict, judge = classify(block, score, feats, llm)
        if verdict.get("is_recommendation", True):
            found += 1
        quote = (verdict.get("quote") or block.text).strip()
        off = block.source_text.find(quote)
        prov = block.prov if off < 0 else replace(
            block.prov, char_start=block.source_start + off,
            char_end=block.source_start + off + len(quote))
        stmts.append(Statement(
            statement_id=f"{doc_id}:{n:04d}",
            text=quote, kind=block.kind, prov=prov, section_path=block.section_path,
            strength_label=(verdict.get("strength_label")
                            or _first_match(_LABEL_PATTERNS, block.text)
                            or sibling_labels[0]),
            evidence_label=_first_match(_EVIDENCE_PATTERNS, block.text) or sibling_labels[1],
            prefilter_score=round(score, 2), prefilter_features=feats,
            is_recommendation=bool(verdict.get("is_recommendation", True)),
            is_patient_level=verdict.get("is_patient_level"),
            classifier_reason=verdict.get("reason", ""), classifier=judge))

    # Back into document order: cross-reference resolution and the ids both
    # read as positional, and a score-ordered list would silently reorder them.
    stmts.sort(key=lambda x: x.statement_id)

    disagreements = None
    if measure_prefilter:
        disagreements = [
            u.text.strip() for u, sc, f in rejected
            if not is_label_only(u.text)
            and classify(u, sc, f, llm)[0].get("is_recommendation")]

    stmts = dedupe(resolve_crossrefs(stmts, blocks))
    kept = [s for s in stmts if s.is_recommendation and not s.duplicate_of]
    by_kind: dict[str, int] = {}
    for b in blocks:
        by_kind[b.kind] = by_kind.get(b.kind, 0) + 1
    # A document with no directive language at all is usually the wrong file --
    # a supplement, an evidence table, a methods appendix. Saying so beats
    # reporting a bare zero, which reads as a tool failure.
    deontic = sum(1 for u in units
                  if any(f.startswith("deontic") or f in {"imperative", "table_action"}
                         for f in prefilter(u)[1]))
    manifest = {
        "doc_id": doc_id, "blocks": len(blocks), "blocks_by_kind": by_kind,
        # Document-level abbreviation glossary. Handed to the binder (as query
        # expansion) and to the formalizer (as canonical names) so one concept
        # does not enter the registry under two labels.
        "abbreviations": abbrevs,
        "units_with_directive_language": deontic,
        "units": len(units), "candidates": len(candidates),
        "statements": len(stmts), "recommendations": len(kept),
        "duplicates": sum(1 for s in stmts if s.duplicate_of),
        "prefilter_threshold": threshold,
        "prefilter_rejected": len(rejected),
        # A partial pass is not a whole-document measurement. Said plainly so
        # `recommendations` is never read as a document property when it is not.
        "limit": limit,
        "unclassified": unclassified,
        # Spans the deterministic vetoes removed before any judge saw them.
        # Reported by reason and never silently: a veto that fires wrongly
        # deletes a recommendation, so it has to be auditable.
        "vetoed": len(vetoed),
        "vetoed_by_reason": {r: sum(1 for _, x in vetoed if x == r)
                             for r in sorted({x for _, x in vetoed})},
        "vetoed_texts": [u.text.strip()[:120] for u, _ in vetoed[:10]],
        "labels_absorbed": len(labels),
        "classifier": "llm" if llm else "heuristic",
        # Flowcharts are PathWISE's substrate, not ours. Count them, do not
        # chase them: this is the declared uncovered surface of the document.
        # Declared uncovered surface: flowcharts, and tables shipped as images.
        "figures_uncovered": sum(1 for b in blocks if b.kind == "figure"),
        "headings": sum(1 for b in blocks if b.kind == "heading"),
    }
    if disagreements is not None:
        manifest["prefilter_disagreements"] = len(disagreements)
        manifest["prefilter_disagreement_texts"] = disagreements[:20]
    return stmts, manifest

# ---------------------------------------------------------------- io

def statement_to_dict(s: Statement) -> dict:
    d = {k: getattr(s, k) for k in
         ("statement_id", "text", "kind", "section_path", "strength_label",
          "evidence_label", "prefilter_score", "prefilter_features",
          "is_recommendation", "is_patient_level", "classifier_reason",
          "classifier", "context_locators", "duplicate_of", "also_at")}
    d["section_path"] = list(s.section_path)
    d["prefilter_features"] = list(s.prefilter_features)
    d["context_locators"] = list(s.context_locators)
    d["also_at"] = list(s.also_at)
    p = s.prov
    d["prov"] = {"doc_id": p.doc_id, "locator": p.locator, "char_start": p.char_start,
                 "char_end": p.char_end, "page": p.page,
                 "bbox": list(p.bbox) if p.bbox else None}
    return d

def statement_from_dict(d: dict) -> Statement:
    p = d["prov"]
    return Statement(
        statement_id=d["statement_id"], text=d["text"], kind=d["kind"],
        prov=Provenance(p["doc_id"], p["locator"], p["char_start"], p["char_end"],
                        p.get("page"), tuple(p["bbox"]) if p.get("bbox") else None),
        section_path=tuple(d.get("section_path", ())),
        strength_label=d.get("strength_label"), evidence_label=d.get("evidence_label"),
        prefilter_score=d.get("prefilter_score", 0.0),
        prefilter_features=tuple(d.get("prefilter_features", ())),
        is_recommendation=d.get("is_recommendation", True),
        is_patient_level=d.get("is_patient_level"),
        classifier_reason=d.get("classifier_reason", ""),
        classifier=d.get("classifier", "none"),
        context_locators=tuple(d.get("context_locators", ())),
        duplicate_of=d.get("duplicate_of"), also_at=tuple(d.get("also_at", ())))

def write_jsonl(stmts: Iterable[Statement], path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for s in stmts:
            fh.write(json.dumps(statement_to_dict(s)) + "\n")

def read_jsonl(path: str) -> list[Statement]:
    with open(path, encoding="utf-8") as fh:
        return [statement_from_dict(json.loads(line)) for line in fh if line.strip()]

# ---------------------------------------------------------------- JATS loader

def _lname(el) -> str:
    t = getattr(el, "tag", None)
    return t.rsplit("}", 1)[-1] if isinstance(t, str) else ""

def _itext(el) -> str:
    return "".join(el.itertext())

def _body_after_head(el, head) -> tuple[str, int]:
    """Element text with a leading <label>/<caption> sliced off, plus the offset
    that slice starts at. Slicing the full itertext (rather than joining the
    children) keeps the whitespace BETWEEN children -- without it two adjacent
    <p> sentences are concatenated into one unsplittable run -- and keeps the
    offsets honest against the element a clinician will be shown."""
    full = _itext(el)
    if not head:
        return full, 0
    last = _itext(head[-1])
    pos = full.find(last) if last else -1
    start = pos + len(last) if pos >= 0 else 0
    return full[start:], start

def _section_path(el, tree) -> tuple[str, ...]:
    """Ancestor section titles, outermost first."""
    parts = []
    for anc in reversed(list(el.iterancestors())):
        n = _lname(anc)
        if n == "sec":
            title = next((c for c in anc if _lname(c) == "title"), None)
            label = next((c for c in anc if _lname(c) == "label"), None)
            txt = " ".join(x for x in (_itext(label).strip() if label is not None else "",
                                       _itext(title).strip() if title is not None else "") if x)
            parts.append(txt or "(untitled section)")
        elif n in {"abstract", "front", "back", "app", "glossary"}:
            parts.append(n.capitalize())
    return tuple(parts)

def _table_rows(tw, tree, doc_id, section_path) -> list[Block]:
    """One Block per table row. Header cells are folded into each row so the
    row stands alone as a rule: 'eGFR: <30; Action: refer to nephrology'.

    Returns a single `figure` block when the table has no extractable text.
    Measured on real PMC: some journals ship EVERY table as a scanned image
    (<td><graphic src=".jpg"/></td>) -- 26 of 26 in one 2025 ESO guideline. The
    recommendations in those tables are unreachable from the XML and need the
    PDF path, so they must be counted as declared uncovered surface rather than
    silently producing nothing."""
    out: list[Block] = []
    table = next((e for e in tw.iter() if _lname(e) == "table"), None)
    if table is None:
        return out
    if not "".join(table.itertext()).strip():
        cap = re.sub(r"\s+", " ", _itext(tw)).strip()
        return [Block(cap, "figure", Provenance(doc_id, tree.getpath(tw), 0, len(cap)),
                      section_path)]
    headers: list[str] = []
    for tr in (e for e in table.iter() if _lname(e) == "tr"):
        cells = [c for c in tr if _lname(c) in {"th", "td"}]
        if not cells:
            continue
        vals = [re.sub(r"\s+", " ", _itext(c)).strip() for c in cells]
        if all(_lname(c) == "th" for c in cells) and not headers:
            headers = vals
            continue
        text = ("; ".join(f"{h}: {v}" for h, v in zip(headers, vals) if v)
                if headers else " | ".join(v for v in vals if v))
        if not text.strip():
            continue
        out.append(Block(text=text, kind="table_row", section_path=section_path,
                         prov=Provenance(doc_id, tree.getpath(tr), 0, len(text))))
    return out

def load_jats(source: str | bytes, doc_id: str = "doc") -> list[Block]:
    """PMC Open Access JATS XML -> Blocks. The main path.

    <boxed-text> and <table-wrap> are tagged by the publisher at typesetting
    time, so the boxed-callout problem is solved structurally here and does not
    need a layout model at all."""
    from lxml import etree
    parser = etree.XMLParser(recover=True, resolve_entities=False, load_dtd=False,
                             no_network=True)
    root = (etree.fromstring(source, parser) if isinstance(source, bytes)
            else etree.parse(source, parser).getroot())
    tree = root.getroottree()

    # Elements whose whole subtree is consumed by a structural handler, so a
    # <p> inside a <boxed-text> is not also emitted as loose prose.
    consumed: set = set()
    blocks: list[Block] = []

    for el in root.iter():
        n = _lname(el)
        if n not in {"boxed-text", "table-wrap", "fig"} or el in consumed:
            continue
        consumed.update(el.iter())
        sp = _section_path(el, tree)
        loc = tree.getpath(el)
        if n == "table-wrap":
            label = next((c for c in el if _lname(c) in {"label", "caption"}), None)
            cap = re.sub(r"\s+", " ", _itext(label)).strip() if label is not None else ""
            blocks.extend(_table_rows(el, tree, doc_id, sp + ((cap,) if cap else ())))
        elif n == "fig":
            cap = re.sub(r"\s+", " ", _itext(el)).strip()
            blocks.append(Block(cap, "figure", Provenance(doc_id, loc, 0, len(cap)), sp))
        else:
            head = [c for c in el if _lname(c) in {"label", "caption", "title"}]
            crumb = " ".join(re.sub(r"\s+", " ", _itext(c)).strip() for c in head).strip()
            txt, start = _body_after_head(el, head)
            blocks.append(Block(txt, "boxed_text",
                                Provenance(doc_id, loc, start, start + len(txt)),
                                sp + ((crumb,) if crumb else ()), box_id=loc))

    for el in root.iter():
        n = _lname(el)
        if el in consumed or n not in {"p", "list-item", "title"}:
            continue
        if n == "list-item":
            consumed.update(c for c in el.iter() if c is not el)
        if n == "title" and _lname(el.getparent() if el.getparent() is not None else el) != "sec":
            continue
        txt = _itext(el)
        if not txt.strip():
            continue
        kind = {"p": "section_text", "list-item": "list_item", "title": "heading"}[n]
        blocks.append(Block(txt, kind, Provenance(doc_id, tree.getpath(el), 0, len(txt)),
                            _section_path(el, tree)))
    return blocks

# ---------------------------------------------------------------- HTML loader

_HTML_BOX = re.compile(r"\b(recommend|callout|box|panel|highlight|key-?point)", re.I)

def load_html(source: str | bytes, doc_id: str = "doc") -> list[Block]:
    """USPSTF and similar web-published guidelines. Same structural trick as
    JATS: a callout is a div with a telltale class, not a paragraph."""
    from lxml import html as lhtml
    root = lhtml.fromstring(source)
    tree = root.getroottree()
    consumed: set = set()
    blocks: list[Block] = []

    def heading_path(el) -> tuple[str, ...]:
        prev, path = el, []
        for node in el.itersiblings(preceding=True):
            if node.tag in {"h1", "h2", "h3", "h4"}:
                path.append(node.text_content().strip())
                break
        for anc in el.iterancestors():
            h = anc.find("h2") if anc.find("h2") is not None else anc.find("h3")
            if h is not None and h.text_content().strip() not in path:
                path.append(h.text_content().strip())
        return tuple(reversed([p for p in path if p]))

    for el in root.iter():
        if not isinstance(el.tag, str) or el in consumed:
            continue
        cls = " ".join(filter(None, [el.get("class") or "", el.get("id") or ""]))
        if el.tag in {"div", "aside", "section"} and _HTML_BOX.search(cls):
            consumed.update(el.iter())
            txt = el.text_content()
            loc = tree.getpath(el)
            blocks.append(Block(txt, "boxed_text", Provenance(doc_id, loc, 0, len(txt)),
                                heading_path(el), box_id=loc))
        elif el.tag == "table":
            consumed.update(el.iter())
            headers = [th.text_content().strip() for th in el.iter("th")]
            for tr in el.iter("tr"):
                vals = [td.text_content().strip() for td in tr if td.tag == "td"]
                if not any(vals):
                    continue
                txt = ("; ".join(f"{h}: {v}" for h, v in zip(headers, vals) if v)
                       if headers else " | ".join(v for v in vals if v))
                blocks.append(Block(txt, "table_row",
                                    Provenance(doc_id, tree.getpath(tr), 0, len(txt)),
                                    heading_path(el)))

    for el in root.iter():
        if not isinstance(el.tag, str) or el in consumed:
            continue
        if el.tag not in {"p", "li", "h1", "h2", "h3", "h4"}:
            continue
        txt = el.text_content()
        if not txt.strip():
            continue
        kind = ("heading" if el.tag.startswith("h")
                else "list_item" if el.tag == "li" else "section_text")
        blocks.append(Block(txt, kind, Provenance(doc_id, tree.getpath(el), 0, len(txt)),
                            heading_path(el)))
    return blocks

# ---------------------------------------------------------------- PDF loader

MIN_BOX_W, MIN_BOX_H = 60.0, 18.0   # smaller than this is a rule/underline
MAX_BOX_PAGE_FRAC = 0.85            # bigger than this is the page frame
CONTAINMENT = 0.90                  # block area that must sit inside the rect

def _docling_available() -> bool:
    import importlib.util
    return importlib.util.find_spec("docling") is not None

def _docling_blocks(path: str, doc_id: str) -> list[Block]:
    """Layout-model path. Imported lazily: docling pulls in torch, and the
    JATS path -- which is the main path -- must not pay for that."""
    from docling.document_converter import DocumentConverter
    doc = DocumentConverter().convert(path).document
    label_map = {"section_header": "heading", "title": "heading", "list_item": "list_item",
                 "picture": "figure", "table": "table_row", "caption": "section_text"}
    blocks: list[Block] = []
    for i, (item, _level) in enumerate(doc.iterate_items()):
        text = (getattr(item, "text", "") or "").strip()
        if not text:
            continue
        label = str(getattr(item, "label", "text")).split(".")[-1].lower()
        prov = (getattr(item, "prov", None) or [None])[0]
        page = getattr(prov, "page_no", None) if prov else None
        bb = getattr(prov, "bbox", None) if prov else None
        bbox = ((bb.l, bb.t, bb.r, bb.b) if bb is not None and hasattr(bb, "l") else None)
        blocks.append(Block(text, label_map.get(label, "section_text"),
                            Provenance(doc_id, f"p{page}:b{i}", 0, len(text), page, bbox)))
    return blocks

def _pymupdf_blocks(doc, doc_id: str) -> list[Block]:
    """No-layout-model path: native text blocks plus PyMuPDF's own table finder.
    Also the deterministic path used by the tests."""
    blocks: list[Block] = []
    for pno, page in enumerate(doc, start=1):
        table_rects = []
        try:
            for t in page.find_tables().tables:
                rows = t.extract()
                # `find_tables` over-detects: on a text-heavy PDF it reads
                # aligned prose as a table and chops one sentence across seven
                # "rows". Measured on a real supplement: 64 fake table_rows,
                # each a fragment of "CPET refers to a testing method that
                # measures:". A real table has ROWS WITH SEVERAL CELLS -- if
                # most rows hold a single cell, it is a column of prose.
                multi = sum(1 for r in rows
                            if sum(1 for c in r if (c or "").strip()) >= 2)
                if not rows or multi < max(2, len(rows) * 0.5):
                    continue
                table_rects.append(tuple(t.bbox))
                headers = [h or "" for h in (getattr(t.header, "names", None) or [])]
                for r, row in enumerate(t.extract()):
                    vals = [(c or "").strip() for c in row]
                    if not any(vals) or vals == headers:
                        continue
                    txt = ("; ".join(f"{h}: {v}" for h, v in zip(headers, vals) if v)
                           if headers else " | ".join(v for v in vals if v))
                    blocks.append(Block(txt, "table_row",
                                        Provenance(doc_id, f"p{pno}:t{r}", 0, len(txt),
                                                   pno, tuple(t.bbox))))
        except Exception:
            pass                                   # find_tables is best-effort
        for i, b in enumerate(page.get_text("blocks")):
            x0, y0, x1, y1, txt = b[0], b[1], b[2], b[3], b[4]
            if not txt.strip():
                continue
            if any(_contained((x0, y0, x1, y1), tr) >= CONTAINMENT for tr in table_rects):
                continue                           # already emitted as table rows
            blocks.append(Block(txt, "section_text",
                                Provenance(doc_id, f"p{pno}:b{i}", 0, len(txt),
                                           pno, (x0, y0, x1, y1))))
    return blocks

def _contained(inner, outer) -> float:
    ax0, ay0, ax1, ay1 = inner
    bx0, by0, bx1, by1 = outer
    iw = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    ih = max(0.0, min(ay1, by1) - max(ay0, by0))
    area = (ax1 - ax0) * (ay1 - ay0)
    return (iw * ih) / area if area > 0 else 0.0

def find_callout_rects(page) -> list[tuple[float, float, float, float]]:
    """A boxed callout in a born-digital PDF is a drawing primitive, not a
    semantic label: a stroked or filled rectangle with text inside it. Layout
    models trained on DocLayNet cannot report this -- there is no 'callout'
    class -- so recover it geometrically, where the answer is exact."""
    page_area = abs(page.rect.get_area()) or 1.0
    rects = []
    for d in page.get_drawings():
        if not (d.get("fill") or d.get("color")):
            continue
        r = d["rect"]
        w, h = r.x1 - r.x0, r.y1 - r.y0
        if w < MIN_BOX_W or h < MIN_BOX_H:
            continue                               # a rule, underline or gridline
        if (w * h) / page_area > MAX_BOX_PAGE_FRAC:
            continue                               # the page frame itself
        rects.append((r.x0, r.y0, r.x1, r.y1))
    # Smallest first, so a block inside nested boxes is attributed to the inner.
    return sorted(set(rects), key=lambda r: (r[2] - r[0]) * (r[3] - r[1]))

def recover_boxes(doc, blocks: list[Block]) -> list[Block]:
    """Relabel blocks that sit inside a callout rect as boxed_text."""
    by_page: dict[int, list] = {}
    for pno, page in enumerate(doc, start=1):
        by_page[pno] = find_callout_rects(page)
    out = []
    for b in blocks:
        rects = by_page.get(b.prov.page or -1, [])
        hit = next((i for i, r in enumerate(rects)
                    if b.prov.bbox and _contained(b.prov.bbox, r) >= CONTAINMENT), None)
        out.append(b if hit is None or b.kind == "table_row"
                   else replace(b, kind="boxed_text", box_id=f"p{b.prov.page}:box{hit}"))
    return out

def load_pdf(path: str, doc_id: str = "doc", use_docling: bool = True) -> list[Block]:
    """Fallback path, for guidelines with no XML or HTML edition."""
    import pymupdf                    # `import fitz` is the deprecated spelling
    doc = pymupdf.open(path)
    if use_docling and _docling_available():
        blocks = _docling_blocks(path, doc_id)
    else:
        blocks = _pymupdf_blocks(doc, doc_id)
    return recover_boxes(doc, blocks)

# ---------------------------------------------------------------- cli

EUROPE_PMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/{}/fullTextXML"

def fetch_pmc(pmcid: str, cache_dir: str = "corpus") -> str:
    """Download a PMC Open Access article's full-text JATS, cached on disk.

    Europe PMC rather than E-utilities: no API key, no rate-limit dance, and it
    serves the same JATS. Only works for the OA subset -- a non-OA PMCID
    returns an error document, not XML."""
    import urllib.request
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"{pmcid}.xml")
    if not os.path.exists(path):
        req = urllib.request.Request(
            EUROPE_PMC.format(pmcid),
            headers={"User-Agent": "GuidelineForge/0.1 (research; segmenter)"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        # Parse rather than sniff: real PMC JATS may open with <?xml, with a
        # <!DOCTYPE article ...> declaration, or with <article> directly.
        from lxml import etree
        try:
            root = etree.fromstring(
                data, etree.XMLParser(recover=True, resolve_entities=False,
                                      load_dtd=False, no_network=True))
        except Exception as exc:
            raise ValueError(f"{pmcid}: response is not parseable XML ({exc})") from None
        if root is None or _lname(root) != "article":
            raise ValueError(f"{pmcid}: no full text returned -- not in the Europe PMC "
                             f"Open Access subset? (got <{_lname(root)}>)")
        with open(path, "wb") as fh:
            fh.write(data)
    return path

def search_pmc(query: str, limit: int = 25) -> list[tuple[str, str]]:
    """Europe PMC search -> [(pmcid, title)]. Only full-text OA records."""
    import urllib.parse, urllib.request
    # HAS_FT:y is the filter that actually predicts fullTextXML availability;
    # IN_EPMC:y alone returns records whose full text is not served.
    q = f"({query}) AND (OPEN_ACCESS:y) AND (IN_EPMC:y) AND (HAS_FT:y)"
    url = ("https://www.ebi.ac.uk/europepmc/webservices/rest/search?"
           + urllib.parse.urlencode({"query": q, "format": "json",
                                     "pageSize": limit, "resultType": "lite"}))
    with urllib.request.urlopen(url, timeout=60) as r:
        data = json.loads(r.read())
    return [(x.get("pmcid", ""), x.get("title", ""))
            for x in data["resultList"]["result"] if x.get("pmcid")]

def _review(stmts: list[Statement], show_all: bool = False, top: int = 0) -> None:
    """Human-readable view of a saved run. The JSONL keeps everything -- the
    rejects and duplicates are the audit trail for the extraction-error report
    -- so reading it raw is not useful without this filter."""
    kept = [s for s in stmts if s.is_recommendation and not s.duplicate_of]
    dupes = [s for s in stmts if s.duplicate_of]
    rejected = [s for s in stmts if not s.is_recommendation]
    print(f"{len(stmts)} records: {len(kept)} recommendations, "
          f"{len(dupes)} duplicates, {len(rejected)} rejected by the classifier\n")
    shown = sorted(kept, key=lambda s: -s.prefilter_score)
    for s in (shown[:top] if top else shown):
        label = " | ".join(filter(None, [s.strength_label, s.evidence_label]))
        print(f"[{s.prefilter_score:5.1f}] {s.kind}"
              + (f"  ({label})" if label else ""))
        print(f"  {' '.join(s.text.split())}")
        print(f"  section : {' > '.join(s.section_path) or '(none)'}")
        print(f"  source  : {s.prov.locator}  chars {s.prov.char_start}:{s.prov.char_end}"
              + (f"  page {s.prov.page}" if s.prov.page else ""))
        if s.also_at:
            print(f"  restated at: {len(s.also_at)} other place(s)")
        if s.context_locators:
            print(f"  cross-ref  : {', '.join(s.context_locators)}")
        print()
    if show_all:
        print("=" * 70)
        print(f"REJECTED BY THE CLASSIFIER ({len(rejected)}) -- the prefilter let these\n"
              "through and the classifier declined them. Scan for false negatives.\n")
        for s in sorted(rejected, key=lambda s: -s.prefilter_score)[:top or None]:
            print(f"[{s.prefilter_score:5.1f}] {' '.join(s.text.split())[:150]}")

def _main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Segment a guideline into statements.")
    ap.add_argument("target", nargs="?",
                    help="a PMCID (PMC13276589), or a path to .xml / .html / .pdf")
    ap.add_argument("--search", metavar="Q",
                    help='find OA guidelines, e.g. --search \'PUB_TYPE:"Practice Guideline"\'')
    ap.add_argument("--out", help="write statements as JSONL to this path")
    ap.add_argument("--threshold", type=float, default=PREFILTER_THRESHOLD)
    ap.add_argument("--measure-prefilter", action="store_true",
                    help="also classify the REJECTED units and report disagreements")
    ap.add_argument("--top", type=int, default=15,
                    help="statements to print; 0 for all")
    ap.add_argument("--review", metavar="JSONL",
                    help="pretty-print a saved .jsonl instead of segmenting")
    ap.add_argument("--all", action="store_true",
                    help="with --review, also show what the classifier rejected")
    a = ap.parse_args(argv)

    if a.review:
        _review(read_jsonl(a.review), show_all=a.all, top=a.top)
        return 0
    if a.search:
        for pmcid, title in search_pmc(a.search):
            print(f"{pmcid:13s} {title[:96]}")
        return 0
    if not a.target:
        ap.error("give a PMCID or a file path, or use --search")

    path = fetch_pmc(a.target) if a.target.upper().startswith("PMC") else a.target
    doc_id = os.path.splitext(os.path.basename(path))[0]
    ext = os.path.splitext(path)[1].lower()
    blocks = (load_pdf(path, doc_id) if ext == ".pdf" else
              load_html(open(path, "rb").read(), doc_id) if ext in {".html", ".htm"} else
              load_jats(path, doc_id))

    stmts, manifest = segment(blocks, doc_id, threshold=a.threshold,
                              measure_prefilter=a.measure_prefilter)
    print(json.dumps(manifest, indent=2))
    kept = [s for s in stmts if s.is_recommendation and not s.duplicate_of]
    print(f"\n--- {len(kept)} recommendations, highest-scoring first ---")
    for s in sorted(kept, key=lambda x: -x.prefilter_score)[:a.top]:
        label = f"[{s.strength_label}] " if s.strength_label else ""
        print(f"{s.prefilter_score:6.1f} {s.kind:12s} {label}{' '.join(s.text.split())[:110]}")
        print(f"       {s.prov.locator}  chars {s.prov.char_start}:{s.prov.char_end}")
    if a.out:
        write_jsonl(stmts, a.out)
        print(f"\nwrote {len(stmts)} statements to {a.out}")
    return 0

if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
