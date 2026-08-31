"""
The model hooks, wired to Claude.

Every stage of Track A takes its model as an injected callable so the pipeline
runs offline on heuristics and the tests need no key. This module supplies the
real implementations of those callables and nothing else -- no stage imports
it, and importing it never makes a network call.

    import llm, segment, bind
    stmts, man = segment.segment(blocks, doc, llm=llm.classify_block)
    bs, rs = bind.bind_statement(text, ix, llm.propose_mentions, llm.select_candidate)

Design notes:
  * Structured outputs, not prose parsing. `output_config.format` constrains
    generation to the JSON schema each stage already declares, so the model
    cannot emit a variable outside the registry or a field the validator does
    not know. That is the "constrained decoding" step the plan assumes.
  * Instructions go in a CACHED system block. The prefix is identical across
    every call in a run, so a document's 600+ classifier calls pay for it once.
  * TEMPERATURE IS NOT AVAILABLE. Claude Opus 5 / Sonnet 5 / Opus 4.8 / 4.7
    reject `temperature`, `top_p` and `top_k` with a 400; only Sonnet 4.6 and
    Haiku 4.5 still accept them. `sample()` therefore varies by re-calling, not
    by dialling temperature -- which is also what a real deployment would see.
"""
from __future__ import annotations

import json
import os
import re
import threading
from typing import Any, Callable

DEFAULT_MODEL = "claude-opus-5"
# Only these still accept `temperature`; needed by the sampling-variance probe
# if variance at default settings turns out to be zero.
TEMPERATURE_CAPABLE = {"claude-sonnet-4-6", "claude-haiku-4-5"}

import cassette

def load_env(path: str | None = None) -> None:
    """Read a .env into os.environ without adding a dependency. Existing
    environment variables win, so an exported key overrides the file."""
    if path is None:
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(os.path.dirname(here), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))

_client = None
_lock = threading.Lock()

def client():
    global _client
    with _lock:
        if _client is None:
            load_env()
            import anthropic
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise RuntimeError(
                    "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and "
                    "add your key (console.anthropic.com -> API Keys).")
            _client = anthropic.Anthropic()
        return _client

def model() -> str:
    load_env()
    return os.environ.get("GF_MODEL", DEFAULT_MODEL)

# ---------------------------------------------------------------- core call

class Usage:
    """Running token/cost tally, so an experiment can report what it cost."""
    PRICES = {  # USD per 1M tokens, input/output
        "claude-opus-5": (5.0, 25.0), "claude-sonnet-5": (2.0, 10.0),
        "claude-sonnet-4-6": (3.0, 15.0), "claude-haiku-4-5": (1.0, 5.0),
    }
    def __init__(self):
        self.calls = self.input = self.output = self.cached = 0
        self.errors: list[str] = []
        # `self.calls += 1` is load-add-store, and the binder and the K-sample
        # formalizer both run in thread pools -- so concurrent calls lose
        # updates and the tally UNDER-reports. That tally is the cost figure a
        # paper quotes, which makes a silently-low number worse than a missing
        # one.
        self._lock = threading.RLock()
    def add(self, u) -> None:
        with self._lock:
            self.calls += 1
            self.input += u.input_tokens
            self.output += u.output_tokens
            self.cached += getattr(u, "cache_read_input_tokens", 0) or 0
    def cost(self, m: str) -> float:
        pin, pout = self.PRICES.get(m, self.PRICES[DEFAULT_MODEL])
        return (self.input * pin + self.output * pout) / 1e6
    def fail(self, exc: Exception) -> None:
        with self._lock:
            self.errors.append(f"{type(exc).__name__}: {str(exc)[:200]}")

    def __str__(self):
        m = model()
        base = (f"{self.calls} calls, {self.input} in ({self.cached} cached), "
                f"{self.output} out, ~${self.cost(m):.3f}")
        if self.errors:
            uniq = sorted(set(self.errors))
            base += (f"\n  !! {len(self.errors)} FAILED CALL(S) -- these fell back to "
                     f"the offline heuristic, so any result above is NOT the model's:")
            for e in uniq[:3]:
                base += f"\n     {e}"
        return base

USAGE = Usage()

def ask_json(system: str, user: str, schema: dict, *,
             effort: str = "medium", max_tokens: int = 4000,
             model_id: str | None = None, temperature: float | None = None) -> dict:
    """One structured-output call. Returns the parsed object.

    `system` is cached: it is the fixed instruction prefix, identical across a
    run, so only `user` is billed at full rate after the first call."""
    m = model_id or model()
    # Record/replay, before anything is billed. One real run becomes a fixture
    # that every later code change is tested against offline -- see cassette.py
    # for why the unit tests could not catch what a real document catches.
    _mode = cassette.mode()
    _key = (cassette.key_for(m, system, user, schema) if _mode != "off" else "")
    if _mode == "replay":
        return cassette.replay(_key, note=user)
    kwargs: dict[str, Any] = dict(
        model=m, max_tokens=max_tokens,
        system=[{"type": "text", "text": system,
                 "cache_control": {"type": "ephemeral"}}],
        output_config={"format": {"type": "json_schema", "schema": schema},
                       "effort": effort},
        messages=[{"role": "user", "content": user}],
    )
    if temperature is not None:
        if m not in TEMPERATURE_CAPABLE:
            raise ValueError(
                f"{m} rejects `temperature` with a 400. Use one of "
                f"{sorted(TEMPERATURE_CAPABLE)} or leave it unset.")
        kwargs["temperature"] = temperature
    try:
        r = client().messages.create(**kwargs)
    except Exception as exc:
        USAGE.fail(exc)
        raise
    USAGE.add(r.usage)
    if r.stop_reason == "refusal":
        raise RuntimeError(f"model refused: {getattr(r.stop_details, 'category', '?')}")
    text = next((b.text for b in r.content if b.type == "text"), "")
    parsed = json.loads(text)
    if _mode == "record":
        cassette.record(_key, parsed, note=user)
    return parsed

# ---------------------------------------------------------------- hooks

def classify_block(block) -> dict:
    """segment.segment(..., llm=llm.classify_block)"""
    import segment as sg
    prompt = sg.build_prompt(block)
    head, _, tail = prompt.partition("\n\nSection:")
    return ask_json(head, "Section:" + tail, sg.CLASSIFIER_SCHEMA, effort="low",
                    max_tokens=1500)

def propose_mentions(text: str) -> dict:
    """bind.propose_mentions(text, llm=llm.propose_mentions)"""
    import bind as b
    return ask_json(b.MENTION_INSTRUCTIONS,
                    f'Sentence:\n"""\n{text}\n"""', b.MENTION_SCHEMA,
                    effort="low", max_tokens=1500)

def select_candidate(prompt: str) -> dict:
    """bind.select(..., llm=llm.select_candidate) -- receives a built prompt."""
    import bind as b
    head, _, tail = prompt.partition("\n\nSentence:")
    return ask_json(head, "Sentence:" + tail, b.SELECT_SCHEMA,
                    effort="medium", max_tokens=1500)

# ---------------------------------------------------------------- formalize

FORMALIZE_INSTRUCTIONS = """\
Encode this clinical recommendation as a rule in the given schema.

  `when` is the applicability condition -- when does the recommendation apply?
  `then` is the obligation -- what must be true of the care plan?

The rule means `when -> then`. Use ONLY the variables and events in the schema
enums; there are no others. Give `then` at least one action variable.

`deontic` is the FORCE of the recommendation, and it is not the same as the
strength of the evidence:

  "obligation" -- the guideline REQUIRES the action. "We recommend X." "X is
                  indicated." "We suggest X." A patient who meets `when` and
                  did not get `then` is a real deviation.
  "permission" -- the guideline OFFERS the action. "X may be considered."
                  "X is an option." "at the discretion of the clinician."
                  The same patient has violated NOTHING; the option was not
                  taken.

"We suggest X" is an OBLIGATION -- GRADE-conditional means the evidence is
weaker, not that the action is optional. "We suggest that X may be considered"
is a PERMISSION: the permissive wording governs. Getting this wrong in the
permissive direction accuses a clinician of a violation for declining an
option, so when the wording genuinely offers rather than requires, say so.

A recommendation to CHANGE a dose relative to what the patient was already on
is a `delta`, not a `cmp`. "Reduce basal insulin by 50% at the start of the
liver reduction diet" has no number to compare against -- 50% of whatever this
patient was on -- so it is
    {"kind": "delta", "var": "basal_insulin_dose", "baseline": "lrd_start",
     "direction": "reduce", "lo_pct": 0.5, "hi_pct": null}
A stated range, "reduce by 35%-50%", is lo_pct 0.35 and hi_pct 0.5. Percentages
are FRACTIONS: 50% is 0.5, never 50. `baseline` must be one of the declared
events -- the point in time the change is measured from.

Never encode the modality as a variable. "may", "should", "consider" describe
the recommendation, not the patient, and no data source can populate them.
`deontic` and `strength` are where that information belongs.

If the recommendation cannot be faithfully encoded -- an unstated threshold
("adequate", "high risk", "significant"), an exception or discretion the logic
cannot carry, a statement about services rather than a patient -- return a
refusal with the reason code that fits. DO NOT invent a threshold the source
text does not state. A refusal is a correct and expected answer.

When you refuse, list in `undefined_predicates` the exact phrases that made it
unencodable, copied VERBATIM from the recommendation -- "at high risk", "an
adequate trial of conservative therapy". Quote only the undefined predicate,
not the whole sentence. These become named free variables in a partial
encoding, so a wrong or paraphrased phrase there is worse than none."""

class GrammarTooLarge(RuntimeError):
    """The decoding schema did not compile. Measured on claude-opus-5: a
    13-variable registry compiles only at depth 1, a 3-variable one at depth 2.
    So the registry handed to the decoder should be the STATEMENT's bound
    vocabulary, not the whole document's -- smaller, and it also stops the
    model reaching for a variable this statement never mentioned."""

def _glossary_block(abbrevs: dict[str, str] | None) -> str:
    """The document's own abbreviation definitions, handed to the formalizer so
    it does not mint two variables for one concept. PMC13270348 wrote both
    `pca` and `patient_controlled_intravenous_analgesia` into a single rule and
    produced the tautology (has_ap and pca) -> pca, which certifies every
    patient. Telling the model the two strings are one concept is the cheap half
    of the fix; the sound half is the post-binding check in dsl.py."""
    if not abbrevs:
        return ""
    lines = "\n".join(f"  {k} = {v}" for k, v in sorted(abbrevs.items()))
    return ("\n\nThis document defines the following abbreviations. A short form "
            "and its expansion are THE SAME CONCEPT: use one variable for both, "
            "never two.\n" + lines)

def formalize(statement: str, registry, *, effort: str = "high",
              model_id: str | None = None, max_depth: int | None = None,
              temperature: float | None = None,
              abbrevs: dict[str, str] | None = None) -> dict:
    """One encoding attempt. Returns what `dsl.decoding_schema` produces --
    pass it to `dsl.from_decoded()` for a Rule or a Refusal. The model must
    commit to one or the other in the same decode, which is what makes refusal
    a first-class output rather than an afterthought."""
    import anthropic, dsl
    depth = dsl.MAX_DECODING_DEPTH if max_depth is None else max_depth
    while depth >= 0:
        try:
            return ask_json(FORMALIZE_INSTRUCTIONS,
                            f'Recommendation:\n"""\n{statement}\n"""'
                            + _glossary_block(abbrevs),
                            dsl.decoding_schema(registry, depth), effort=effort,
                            max_tokens=4000, model_id=model_id,
                            temperature=temperature)
        except anthropic.BadRequestError as e:
            # The grammar-size ceiling depends on registry size, so it cannot be
            # predicted from the depth alone. Step down rather than fail: a
            # shallower encoding is still sound, and anything that genuinely
            # needed the depth surfaces as a validation error, not silently.
            if "grammar" not in str(e).lower() and "too complex" not in str(e).lower():
                raise
            depth -= 1
    raise GrammarTooLarge(
        f"registry of {len(registry.names())} variables will not compile at any "
        f"depth; bind per-statement and pass that smaller registry")

def sample(statement: str, registry, k: int = 8, **kw) -> list[dict]:
    """K independent encodings of one statement.

    Variance comes from re-calling, not from a temperature setting -- current
    models do not expose one. That makes this a measurement of the variance a
    real deployment sees, which is the number worth reporting."""
    return [formalize(statement, registry, **kw) for _ in range(k)]

# ---------------------------------------------------------------- M2 baselines

DIRECT_CQL_INSTRUCTIONS = """\
Write an HL7 CQL library that encodes this clinical statement.

Emit ONLY CQL. Start with a `library` declaration, declare `using FHIR version
'4.0.1'`, declare `context Patient`, declare any value sets you reference, and
define the logic. Use explicit units on every quantity comparison.

Encode exactly what the statement says. Do not add thresholds, populations, or
exclusions it does not state."""

def direct_cql(statement: str, *, effort: str = "medium",
               feedback: list[str] | None = None) -> str:
    """BASELINE (a) and (b): the model writes CQL directly.

    No DSL, no closed vocabulary, no terminology gate -- the model types value
    set names and codes itself. This is the comparison M2 exists to make: if
    the constrained pipeline does not beat this on clause-level fidelity, the
    determinism machinery buys only compilation, and the method contribution is
    empty."""
    user = f'Statement:\n"""\n{statement}\n"""'
    if feedback:
        user += ("\n\nThe previous attempt was rejected:\n"
                 + "\n".join(f"  - {f}" for f in feedback)
                 + "\nFix exactly these problems.")
    m = model()
    r = client().messages.create(
        model=m, max_tokens=4000,
        system=[{"type": "text", "text": DIRECT_CQL_INSTRUCTIONS,
                 "cache_control": {"type": "ephemeral"}}],
        output_config={"effort": effort},
        messages=[{"role": "user", "content": user}])
    USAGE.add(r.usage)
    text = "".join(b.text for b in r.content if b.type == "text")
    # strip any markdown fence the model adds despite the instruction
    m2 = re.search(r"```(?:cql)?\n(.*?)```", text, re.S)
    return (m2.group(1) if m2 else text).strip()
