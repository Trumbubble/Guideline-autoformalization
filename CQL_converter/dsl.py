"""
GuidelineForge intermediate DSL - the Track A / Track B interface.

A `Rule` is a checkable formula, not a label: `when`/`then` denote
`phi = when -> then`, which Track B verifies, while `action`/`target` are a
human-readable gloss with no semantics. Time is flattened to derived state
(`Since(event, n)` becomes an occurrence flag plus a day count) rather than
indexed, which is contained and stays visible in the fragment spectrum but
cannot express genuinely multi-visit predicates, which must refuse as
`unsupported_temporal`.
"""
from __future__ import annotations
from dataclasses import dataclass, field, replace
from datetime import date as _date
from typing import Any, Iterable, Literal, Union
import functools
import json
import re
import threading

# z3's Python bindings share one global context, and it is NOT thread-safe:
# two threads building terms or running a solver at the same time segfault the
# interpreter -- no exception, no traceback, the process is simply gone.
# Demonstrated with plain `z3.Real(...)` + `Solver().check()` across 12
# threads, which dies with SIGSEGV every time.
#
# That is easy to hit without meaning to. Serving the checker with Flask's
# `threaded=True` is enough: two browser requests that each load a bundle
# lower it through `to_z3` concurrently and take the process down with them.
#
# So every entry point here that touches z3 is serialised. RLock, not Lock,
# because these call each other (validate_rule -> to_z3, check_nondegenerate
# -> make_env). The critical sections are microseconds of term building and
# small solver queries, so the contention cost is not the thing that makes a
# run slow -- the model calls are, and those are outside this lock.
Z3_LOCK = threading.RLock()


def _z3_guarded(fn):
    @functools.wraps(fn)
    def guarded(*args, **kwargs):
        with Z3_LOCK:
            return fn(*args, **kwargs)
    return guarded

# ---------------------------------------------------------------- variables

VarType = Literal["real", "int", "bool", "code", "date"]
NUMERIC = {"real", "int"}
ORDERED = {"real", "int", "date"}      # types on which <, <=, >, >= are defined

# An `action` variable is something a care plan can decide (a drug is on, a
# referral is placed); an `observation` is patient state. Track B needs the
# split to write frame axioms that close the world over unmentioned actions.
Role = Literal["observation", "action"]

@dataclass(frozen=True)
class VarDef:
    name: str
    type: VarType
    unit: str | None = None
    description: str = ""
    # FHIR/terminology binding is resolved by RETRIEVAL, never generated.
    binding: str | None = None
    role: Role = "observation"
    # Physiological range. Serves twice: the validator flags a threshold
    # outside it (the mg/g-vs-mg/mmol unit-slip class), and Track B asserts it
    # as a domain axiom so the solver stops returning eGFR = 2.0.
    plausible: tuple[float, float] | None = None
    # For `code` vars: the retrieved value set. A code outside it is a
    # hallucinated code, which is a checkable property rather than a claim.
    codes: tuple[str, ...] = ()
    # A HOLE: a predicate the source states but the pipeline cannot bind or
    # quantify ("high risk", "adequate trial"). It is a real free variable --
    # no binding, no data source, no domain axiom -- so the three-valued
    # verdict reports MAY-violate and names it, instead of the rule silently
    # never firing. PathWISE stubs such nodes as `false`, which narrows the
    # rule invisibly; `true` over-fires. Leaving it free is the honest option
    # and is what makes a PARTIAL encoding meaningful.
    unbound: bool = False

@dataclass(frozen=True)
class EventDef:
    """A datable clinical event, referred to by Since() or a Delta baseline.

    `unbound` marks an anchor the SENTENCE names but the terminology does not
    hold -- "at start LRD", "pre-operatively". Without it the only events on
    offer were the drug bindings themselves, so "reduce basal insulin by 50% at
    start LRD" could only anchor the dose change to *being on basal insulin*:
    circular, satisfiable by any dose, and it typechecked. The anchor became a
    plain boolean hole instead, which the DSL cannot measure from.

    This is the same discipline as an unbound variable, and the CQL lowering
    already ends in the same place -- a named null with a comment telling the
    site which query to bind. It is not a new node in the language: the DSL is
    frozen, and this is a property of a declaration."""
    name: str
    description: str = ""
    binding: str | None = None
    unbound: bool = False

class Registry:
    """The closed vocabulary. Generation is constrained to these names."""
    def __init__(self, vars_: list[VarDef], events: list[EventDef] | None = None):
        self._v = {v.name: v for v in vars_}
        self._e = {e.name: e for e in (events or [])}
    def __contains__(self, k): return k in self._v
    def __getitem__(self, k): return self._v[k]
    def names(self): return sorted(self._v)
    def type_of(self, k): return self._v[k].type
    def role_of(self, k): return self._v[k].role
    def actions(self): return sorted(n for n, v in self._v.items() if v.role == "action")
    def unbound(self): return sorted(n for n, v in self._v.items() if v.unbound)
    def has_event(self, k): return k in self._e
    def event_names(self): return sorted(self._e)
    def event(self, k): return self._e[k]

# ---------------------------------------------------------------- expressions

CMP = {"<", "<=", ">", ">=", "==", "!="}
EQ_ONLY = {"==", "!="}

@dataclass
class Cmp:
    var: str
    op: str
    value: Any
    kind: Literal["cmp"] = "cmp"

@dataclass
class Between:
    """Explicit interval with EXPLICIT inclusivity - the boundary bug class."""
    var: str
    lo: float
    hi: float
    lo_inclusive: bool = True
    hi_inclusive: bool = True
    kind: Literal["between"] = "between"

@dataclass
class Since:
    """`event` occurred within the last `days` days. See the module docstring
    for what this flattening does and does not buy."""
    event: str
    days: int
    inclusive: bool = True
    kind: Literal["since"] = "since"

@dataclass
class Delta:
    """A RELATIVE change in `var` from its value at `baseline`.

    `Cmp` compares a patient to a fixed number. Whole classes of medication
    guidance compare a patient to their OWN EARLIER STATE:

        "Reduce basal insulin by 50% at the start of the liver reduction diet"
        "Reduce basal insulin dose by 35%-50% of the pre-LRD dose"

    There is no number to compare against -- 50% of whatever this patient was
    already on. Without this node every such statement is `beyond_dsl`, and
    measured on PMC13074134 (perioperative diabetes) that was three of five
    statements and the reason the document scored zero. Dose titration is a
    large fraction of perioperative, oncology, anticoagulation and insulin
    guidance, so the gap is a whole category rather than a few sentences.

    `lo_pct` is the SMALLEST change the recommendation requires and `hi_pct` the
    largest, both as fractions of the baseline. "reduce by 35%-50%" is
    lo_pct=0.35, hi_pct=0.50; "reduce by 50%" is lo_pct=hi_pct=0.50.

    The baseline is an EventDef -- the point in time the comparison is against
    -- which lowers to a variable `<var>_at_<baseline>`. That variable is a free
    one unless the caller supplies it, exactly like a hole: the statement is now
    EXPRESSED, and what is missing is named rather than the whole sentence being
    refused."""
    var: str
    baseline: str                                    # an EventDef name
    direction: Literal["reduce", "increase"]
    lo_pct: float
    hi_pct: float | None = None
    kind: Literal["delta"] = "delta"

@dataclass
class Not:
    operand: "Expr"
    kind: Literal["not"] = "not"

@dataclass
class And:
    operands: list["Expr"]
    kind: Literal["and"] = "and"

@dataclass
class Or:
    operands: list["Expr"]
    kind: Literal["or"] = "or"

Expr = Union[Cmp, Between, Since, Delta, Not, And, Or]

def baseline_var(var: str, event: str) -> str:
    """The symbol holding `var`'s value at `event`. Named, not anonymous, so a
    caller can pin it and a report can say which prior value it needs."""
    return f"{var}_at_{event}"

Action = Literal["recommend", "avoid", "hold", "monitor", "refer", "screen"]

Deontic = Literal["obligation", "permission"]

@dataclass
class Rule:
    """Denotes phi = (when -> then) under a DEONTIC FORCE. `action`/`target`
    are the gloss only.

    `deontic` is the operator, not a label:

      obligation  O(then | when)  -- the guideline requires the action. A
                                     patient meeting `when` without `then` is
                                     a real deviation.
      permission  P(then | when)  -- the guideline OFFERS the action. The same
                                     patient violates nothing; the option was
                                     simply not taken.

    Prohibition is not a third case: F(p) = O(NOT p), which this DSL already
    writes as `action="avoid"` with a negated consequent.

    The distinction is load-bearing rather than cosmetic. Without it a
    permissive statement lowers to the same formula as an obligation, and the
    checker accuses a clinician of a violation for declining an option --
    which is the false-alarm class that gets decision support switched off.
    See RESULTS.md R17b."""
    rule_id: str
    when: Expr
    then: Expr
    action: Action
    target: str
    strength: str | None = None
    deontic: Deontic = "obligation"
    # provenance is mandatory: page + sentence span in the source document
    source_page: int | None = None
    source_span: str | None = None

@dataclass
class Refusal:
    """A statement the compiler declined to encode, and why."""
    rule_id: str
    reason_code: Literal[
        "vague_predicate", "defeasible", "not_patient_level",
        "unsupported_temporal", "unbound_concept", "beyond_dsl"]
    reason_text: str
    source_page: int | None = None
    source_span: str | None = None
    # E3 needs a risk-coverage curve, which needs a score on the gate decision.
    confidence: float | None = None
    # The exact predicates that made this unencodable, VERBATIM from the source
    # ("high risk", "an adequate trial"). Naming them is what lets a partial
    # encoding turn each one into a declared free variable instead of dropping
    # the whole statement -- and it is what a governance reader wants to see.
    undefined_predicates: tuple[str, ...] = ()
    # WHY this statement was refused, one line per signal, naming the concept
    # or check that failed. Without it a residue report says "unbound_concept"
    # and a reader cannot tell whether the GUIDELINE was vague or OUR INDEX
    # missed a code -- which is the distinction the whole measurement turns on.
    evidence: tuple[str, ...] = ()

# ---------------------------------------------------------------- fragment

FRAGMENTS = ("PROP", "LIA", "MTL")

def fragment_of(*exprs: Expr) -> str:
    """The weakest fragment that contains these expressions. This is what M1's
    spectrum is computed from, so it must not be inferred from variable names."""
    rank = 0
    def walk(e):
        nonlocal rank
        if isinstance(e, Since):
            rank = max(rank, 2)
        elif isinstance(e, Delta):
            # A ratio against a free baseline is linear arithmetic, same tier
            # as a numeric threshold -- it is not temporal logic, even though
            # the baseline is fixed by an event.
            rank = max(rank, 1)
        elif isinstance(e, Between):
            rank = max(rank, 1)
        elif isinstance(e, Cmp):
            if isinstance(e.value, bool):
                pass                       # boolean atom -> propositional
            elif isinstance(e.value, str) and e.op in EQ_ONLY:
                pass                       # code equality -> finite domain, propositional
            else:
                rank = max(rank, 1)        # numeric / ordered comparison -> LIA
        elif isinstance(e, Not):
            walk(e.operand)
        elif isinstance(e, (And, Or)):
            for o in e.operands: walk(o)
    for e in exprs:
        walk(e)
    return FRAGMENTS[rank]

# ---------------------------------------------------------------- validation

Severity = Literal["error", "warn"]

@dataclass
class Issue:
    path: str
    code: str
    message: str
    hint: str = ""                 # fed verbatim back into the repair loop
    severity: Severity = "error"   # warns are advisory; errors block compilation

def errors(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.severity == "error"]

def _is_number(v) -> bool:
    # bool is a subclass of int in Python, so `isinstance(True, int)` is True.
    # Without this guard `egfr > true` typechecks.
    return isinstance(v, (int, float)) and not isinstance(v, bool)

def _iso_date(v) -> _date | None:
    if not isinstance(v, str): return None
    try: return _date.fromisoformat(v)
    except ValueError: return None

def _validate_cmp(e: Cmp, reg: Registry, path: str) -> list[Issue]:
    out: list[Issue] = []
    if e.var not in reg:
        return [Issue(path, "UNKNOWN_VAR", f"{e.var!r} is not in the registry",
                      f"allowed: {', '.join(reg.names()[:12])}...")]
    if e.op not in CMP:
        return [Issue(path, "BAD_OP", f"{e.op!r} is not a comparison",
                      f"allowed: {sorted(CMP)}")]
    vd, t = reg[e.var], reg.type_of(e.var)

    if t not in ORDERED and e.op not in EQ_ONLY:
        out.append(Issue(path, "TYPE_ERROR",
                         f"{e.var} is {t}; {e.op!r} is not defined on {t}",
                         "use == or !="))
    if t == "bool":
        if not isinstance(e.value, bool):
            out.append(Issue(path, "TYPE_ERROR",
                             f"{e.var} is bool but value is {e.value!r}",
                             "supply true or false"))
    elif t in NUMERIC:
        if not _is_number(e.value):
            out.append(Issue(path, "TYPE_ERROR",
                             f"{e.var} is {t} but value is {type(e.value).__name__}",
                             "supply a number"))
        else:
            if t == "int" and float(e.value) != int(e.value):
                out.append(Issue(path, "TYPE_ERROR",
                                 f"{e.var} is int but threshold {e.value} is fractional",
                                 "round the threshold or declare the variable real"))
            if e.op in EQ_ONLY and t == "real":
                out.append(Issue(path, "EXACT_REAL_COMPARISON",
                                 f"{e.op!r} against a real threshold on {e.var}",
                                 "guideline thresholds are almost always inequalities; "
                                 "did the prose say 'at or above' / 'below'?",
                                 severity="warn"))
            if vd.plausible and not (vd.plausible[0] <= float(e.value) <= vd.plausible[1]):
                out.append(Issue(path, "IMPLAUSIBLE_THRESHOLD",
                                 f"{e.value} is outside the plausible range "
                                 f"{vd.plausible} for {e.var} ({vd.unit})",
                                 f"check the unit: {e.var} is recorded in {vd.unit}",
                                 severity="warn"))
    elif t == "code":
        if not isinstance(e.value, str):
            out.append(Issue(path, "TYPE_ERROR",
                             f"{e.var} is code but value is {type(e.value).__name__}",
                             "supply a code string from the bound value set"))
        elif vd.codes and e.value not in vd.codes:
            out.append(Issue(path, "UNBOUND_CODE",
                             f"{e.value!r} is not in the value set bound to {e.var}",
                             f"allowed: {', '.join(vd.codes[:8])}; codes are RETRIEVED, "
                             "never generated - refuse with unbound_concept instead"))
    elif t == "date":
        if _iso_date(e.value) is None:
            out.append(Issue(path, "TYPE_ERROR",
                             f"{e.var} is date but value is {e.value!r}",
                             "supply an ISO-8601 date, e.g. 2024-01-31"))
    return out

def validate(expr: Expr, reg: Registry, path: str = "when") -> list[Issue]:
    out: list[Issue] = []
    if isinstance(expr, Cmp):
        out += _validate_cmp(expr, reg, path)
    elif isinstance(expr, Between):
        if expr.var not in reg:
            out.append(Issue(path, "UNKNOWN_VAR", f"{expr.var!r} is not in the registry"))
        else:
            vd, t = reg[expr.var], reg.type_of(expr.var)
            if t not in NUMERIC:
                out.append(Issue(path, "TYPE_ERROR", "between requires a numeric var"))
            if not (_is_number(expr.lo) and _is_number(expr.hi)):
                out.append(Issue(path, "TYPE_ERROR", "between bounds must be numbers"))
            elif vd.plausible:
                for b, v in (("lo", expr.lo), ("hi", expr.hi)):
                    if not (vd.plausible[0] <= float(v) <= vd.plausible[1]):
                        out.append(Issue(path, "IMPLAUSIBLE_THRESHOLD",
                                         f"{b}={v} is outside the plausible range "
                                         f"{vd.plausible} for {expr.var} ({vd.unit})",
                                         f"check the unit: {expr.var} is in {vd.unit}",
                                         severity="warn"))
        if _is_number(expr.lo) and _is_number(expr.hi):
            # lo == hi with either bound open is empty too - and an open/closed
            # slip is exactly the bug class this node exists to make explicit.
            empty = expr.lo > expr.hi or (
                expr.lo == expr.hi and not (expr.lo_inclusive and expr.hi_inclusive))
            if empty:
                out.append(Issue(path, "EMPTY_INTERVAL",
                                 f"[{expr.lo}, {expr.hi}] with inclusivity "
                                 f"({expr.lo_inclusive}, {expr.hi_inclusive}) is empty",
                                 "an unsatisfiable interval is almost always a "
                                 "transcription error"))
    elif isinstance(expr, Delta):
        if expr.var not in reg:
            out.append(Issue(path, "UNKNOWN_VAR", f"{expr.var!r} is not in the registry"))
        elif reg.type_of(expr.var) not in NUMERIC:
            out.append(Issue(path, "TYPE_ERROR",
                             "a relative change requires a numeric variable"))
        if not reg.has_event(expr.baseline):
            out.append(Issue(path, "UNKNOWN_EVENT",
                             f"{expr.baseline!r} is not a declared event",
                             "the baseline is the point in time the change is "
                             "measured from; declare it as an event"))
        elif expr.var == expr.baseline or expr.var.startswith(expr.baseline + "_"):
            # The baseline is the SAME CONCEPT as the variable being changed:
            # "the basal insulin dose as it stood at the time the patient was
            # on basal insulin". That is circular -- it names no point in time,
            # so the rule is satisfied by any dose at all.
            #
            # It happens because the only events on offer are the drug
            # bindings themselves (`datable_events`), while the anchor the
            # sentence actually names -- "at start LRD" -- has no event to be,
            # and is declared as a plain boolean hole instead. The encoding
            # typechecks, compiles, and is clinically wrong, which is precisely
            # the class this project exists to make visible.
            out.append(Issue(path, "CIRCULAR_BASELINE",
                             f"the change in {expr.var!r} is measured from "
                             f"{expr.baseline!r}, which is the same concept",
                             "the baseline must be a distinct clinical event "
                             "(the anchor the sentence names), not the drug "
                             "whose dose is changing"))
        if expr.direction not in {"reduce", "increase"}:
            out.append(Issue(path, "TYPE_ERROR",
                             f"direction must be reduce|increase, got "
                             f"{expr.direction!r}"))
        pcts = [expr.lo_pct] + ([expr.hi_pct] if expr.hi_pct is not None else [])
        if not all(_is_number(x) and 0 < float(x) < 1 for x in pcts):
            # A percentage written as 50 rather than 0.5 asks for a 5000%
            # reduction and still typechecks, so the range is enforced.
            out.append(Issue(path, "TYPE_ERROR",
                             f"percentages must be fractions in (0, 1); got {pcts}",
                             "\"reduce by 50%\" is lo_pct=0.5, not 50"))
        elif expr.hi_pct is not None and float(expr.hi_pct) < float(expr.lo_pct):
            out.append(Issue(path, "EMPTY_INTERVAL",
                             f"hi_pct {expr.hi_pct} is below lo_pct {expr.lo_pct}"))
    elif isinstance(expr, Since):
        if reg.has_event(expr.event) and reg.event(expr.event).unbound:
            # A Delta baseline lowers to a named null, so an unbound anchor is
            # honest there. `Since` lowers to a FHIR retrieve, which needs a
            # code; against an unbound event it would silently match nothing
            # and the window would read as "never happened".
            out.append(Issue(path, "UNBOUND_EVENT",
                             f"{expr.event!r} has no terminology binding, so a "
                             f"time window cannot be measured against it",
                             "a Since window needs a coded, datable event"))
        if not reg.has_event(expr.event):
            out.append(Issue(path, "UNKNOWN_EVENT",
                             f"{expr.event!r} is not a registered event",
                             f"allowed: {', '.join(reg.event_names()) or '(none)'}"))
        if not _is_number(expr.days) or float(expr.days) != int(expr.days):
            out.append(Issue(path, "TYPE_ERROR", "since.days must be a whole number of days"))
        elif expr.days < 0:
            out.append(Issue(path, "BAD_WINDOW", f"negative window {expr.days}",
                             "a monitoring window looks backwards from now; use a positive count"))
    elif isinstance(expr, Not):
        out += validate(expr.operand, reg, path + ".operand")
    elif isinstance(expr, (And, Or)):
        if len(expr.operands) < 2:
            out.append(Issue(path, "DEGENERATE_JUNCTION",
                             f"{expr.kind} with {len(expr.operands)} operand(s)",
                             "a junction needs >= 2 operands; drop it or add the missing one"))
        for i, o in enumerate(expr.operands):
            out += validate(o, reg, f"{path}.{expr.kind}[{i}]")
    else:
        out.append(Issue(path, "BAD_NODE", f"not an expression node: {expr!r}"))
    return out

def _action_atoms(expr: Expr, reg: Registry, action: str | None = None,
                  no_action_dropped: bool = False) -> set[str]:
    """Atoms in an obligation that a care plan can actually decide. A Since
    node counts: in a `then`, "potassium checked within 14 days" is a demand
    that the plan order the test, not an observation of patient state.

    `no_action_dropped` is precomputed by the caller, which alone has both
    `when` and `then`: True only when EVERY action-role variable in the
    registry is referenced somewhere in the rule (as a decision atom OR as a
    Since/Delta anchor). An action-role var bound but referenced NOWHERE is
    the PMC13270348:0143 shape this check exists to catch (the real action
    dropped, a precondition promoted in its place -- see
    test_the_recommended_action_going_missing_is_flagged), and must keep
    failing. One bound ONLY as a Since baseline (like `mechanical_thrombectomy`
    in "avoid lowering BP < 140 in the 24h after thrombectomy") is accounted
    for, not dropped -- it was never the action this rule is about."""
    if isinstance(expr, (Cmp, Between)):
        if expr.var in reg and reg.role_of(expr.var) == "action":
            return {expr.var}
        # A numeric threshold IS the action for avoid/hold/monitor -- "avoid
        # systolic BP < 140" has no drug or procedure to point to, the target
        # value itself is what the plan manages toward. Restricted to those
        # three verbs (never "recommend", which needs a real action).
        if (action in {"avoid", "hold", "monitor"} and expr.var in reg
                and reg.type_of(expr.var) in NUMERIC and no_action_dropped):
            return {expr.var}
        return set()
    if isinstance(expr, Since):
        return {expr.event} if reg.has_event(expr.event) else set()
    if isinstance(expr, Delta):
        # A relative change IS an action: "reduce the dose by 50%" is an
        # instruction to the care plan, whatever role the underlying variable
        # carries. Requiring role == "action" here would reject every titration
        # rule, since the dose is also an observation of the patient.
        return {expr.var} if expr.var in reg else set()
    if isinstance(expr, Not):
        return _action_atoms(expr.operand, reg, action, no_action_dropped)
    if isinstance(expr, (And, Or)):
        return (set().union(*(_action_atoms(o, reg, action, no_action_dropped)
                              for o in expr.operands))
                if expr.operands else set())
    return set()

# Words in a `target` gloss that carry no identity -- matching on them would
# make the check pass on any rule mentioning a patient.
_TARGET_STOP = {"patient", "patients", "adult", "adults", "with", "without",
                "therapy", "treatment", "management", "care", "risk", "acute",
                "chronic", "severe", "this", "that", "when", "from", "into"}

# ------------------------------------------------------- dropped quantities
# Every other check inspects the rule in ISOLATION and asks whether what is
# there is correct. None can see what is MISSING. Measured on PMC13151662:
#
#   "against intensively lowering systolic BP < 140 mmHg in the first 24 h
#    after successful mechanical thrombectomy"
#      ->  mechanical_thrombectomy -> NOT lowering_systolic_bp
#
# The threshold and the window are both gone, so the rule forbids BP lowering
# after thrombectomy outright -- not a weaker version of the recommendation but
# a different and clinically wrong one. It typechecks, is non-vacuous, names
# the right action, and compiles. Nothing fires.
#
# The only thing that can catch a deletion is a comparison against the source,
# which the rule already carries in `source_span`. This is that comparison, and
# it closes the CLASS rather than the instance: any dropped threshold, in any
# guideline, whatever the number is.

# Units that make a number a CLINICAL quantity rather than a statistic. Bare
# integers and percentages are excluded: "25 studies", "95% CI", "30% of
# patients" are evidence, and flagging them would bury the signal.
_QUANTITY = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*"
    r"(mmhg|mm hg|mg/dl|mg/g|mg/mmol|mmol/l|mmol/mol|mg|mcg|ug|g/dl|g/l|g|kg|"
    r"ml/min|ml|l/min|iu|units?|"
    r"hours?|hrs?|h|days?|weeks?|wks?|months?|years?|yrs?|min(?:utes?)?)"
    r"(?![a-z])", re.I)

# Evidence contexts in which a number is never a clinical threshold.
_STATISTICAL = re.compile(
    r"(95%\s*ci|confidence interval|odds ratio|hazard ratio|risk ratio|"
    r"\b(?:or|hr|rr|aor)\s*[,=]|\bn\s*=|\bp\s*[<=]|\d+\s+(?:studies|trials|"
    r"patients|participants))", re.I)

# Units in which a number is a DOSE -- part of the action. Deliberately excludes
# kg (body weight), mmHg and mmol/L (thresholds), mg/dL and mg/g (lab values),
# and every duration.
_DOSE_UNITS = {"mg", "mcg", "ug", "g", "iu", "unit", "ml"}

_TO_DAYS = {"hour": 1 / 24, "hr": 1 / 24, "h": 1 / 24, "day": 1.0, "week": 7.0,
            "wk": 7.0, "month": 30.0, "year": 365.0, "yr": 365.0,
            "min": 1 / 1440, "minute": 1 / 1440}

# Guidelines state ranges ("150-240 min of exercise per week") and paired blood
# pressures ("185/110 mmHg"). Only the LAST number carries the unit, so a
# single-number pattern sees 240 and misses 150.
#
# The two separators mean different things and are treated differently. A DASH
# is one quantity with two endpoints: a rule encoding `>= 150` has kept it, and
# flagging the missing 240 would be a false alarm. A SLASH is two distinct
# measurements -- systolic AND diastolic -- and dropping either is a real loss.
_RANGE = re.compile(r"(\d+(?:[.,]\d+)?)\s*[-\u2010-\u2015]\s*$")
_PAIR = re.compile(r"(\d+(?:[.,]\d+)?)\s*/\s*$")

def source_quantities(span: str | None) -> list[tuple[tuple[float, ...], str]]:
    """Clinical quantities stated in the source, as (alternatives, unit).

    `alternatives` holds the endpoints of a range: the quantity counts as kept
    if the rule constrains on ANY of them."""
    if not span:
        return []
    out: list[tuple[tuple[float, ...], str]] = []
    for m in _QUANTITY.finditer(span):
        window = span[max(0, m.start() - 40):m.end() + 20]
        if _STATISTICAL.search(window):
            continue
        try:
            value = float(m.group(1).replace(",", "."))
        except ValueError:
            continue
        unit = m.group(2).lower()
        before = span[max(0, m.start() - 12):m.start()]
        pair = _PAIR.search(before)
        rng = _RANGE.search(before)
        if pair:                       # 185/110 -- two measurements
            try:
                out.append(((float(pair.group(1).replace(",", ".")),), unit))
            except ValueError:
                pass
            out.append(((value,), unit))
        elif rng:                      # 150-240 -- one quantity, two endpoints
            try:
                out.append(((float(rng.group(1).replace(",", ".")), value), unit))
            except ValueError:
                out.append(((value,), unit))
        else:
            out.append(((value,), unit))
    return out

def rule_quantities(rule: "Rule") -> set[float]:
    """Every number the rule actually constrains on, durations included."""
    vals: set[float] = set()

    def walk(e):
        if isinstance(e, Cmp) and _is_number(e.value):
            vals.add(float(e.value))
        elif isinstance(e, Between):
            vals.update({float(e.lo), float(e.hi)})
        elif isinstance(e, Since):
            vals.add(float(e.days))
        elif isinstance(e, Delta):
            # As percentages, which is how the source writes them: "reduce by
            # 50%" is lo_pct=0.5 in the node and "50" in the sentence.
            vals.add(float(e.lo_pct) * 100.0)
            if e.hi_pct is not None:
                vals.add(float(e.hi_pct) * 100.0)
        elif isinstance(e, Not):
            walk(e.operand)
        elif isinstance(e, (And, Or)):
            for o in e.operands:
                walk(o)
    walk(rule.when); walk(rule.then)
    return vals

def dropped_quantities(rule: "Rule") -> list[tuple[float, str]]:
    """Quantities the source states that the rule does not constrain on.

    A duration counts as kept if the rule carries it in ANY unit -- "the first
    24 h" and `Since(event, days=1)` are the same window, and flagging that
    would be a false alarm on a correct encoding."""
    have = rule_quantities(rule)
    # A DOSE belongs to the ACTION, not to the applicability condition, and it
    # lives in `target`. Measured on a hand-written fixture rule: "Reduce the
    # apixaban dose to 2.5 mg twice daily if at least two of ..." correctly
    # encodes the three criteria as numeric comparisons and the action as a
    # boolean, with the dose recorded in
    # `target="reduced apixaban dose (2.5 mg twice daily)"`. The 2.5 is not
    # lost, so flagging it is a false alarm on a correct rule.
    #
    # Restricted to DOSE units, because the first version accepted any quantity
    # in the target and a real rule immediately exploited it: the model wrote
    # `target="intensive lowering of systolic BP < 140 mmHg in the first 24 h
    # after successful mechanical thrombectomy"` -- the whole sentence restated
    # -- which made the check pass on the very rule it was written to catch.
    # A threshold (mmHg, mmol/L) and a window (h, days) are CONDITIONS and must
    # be in the logic; only a dose is discharged by naming it.
    for alts, unit in source_quantities(rule.target):
        if unit.rstrip("s.") in _DOSE_UNITS:
            have |= set(alts)
    missing = []
    for alts, unit in source_quantities(rule.source_span):
        stem = unit.rstrip("s.")
        forms = set(alts)
        for value in alts:
            if stem in _TO_DAYS:                   # try every equivalent unit
                days = value * _TO_DAYS[stem]
                forms |= {days, days * 24, days * 1440, days / 7, days / 30}
        if not any(any(abs(f - h) < 1e-6 for h in have) for f in forms):
            missing.append((alts[0], unit))
    return missing

def _vars_in(e: Expr) -> set[str]:
    """Every variable an expression references, regardless of the registry."""
    if isinstance(e, (Cmp, Between, Delta)):
        return {e.var}
    if isinstance(e, Not):
        return _vars_in(e.operand)
    if isinstance(e, (And, Or)):
        return set().union(*(_vars_in(o) for o in e.operands)) if e.operands else set()
    return set()

def open_holes(rule: Rule, reg: Registry) -> tuple[str, ...]:
    """Unbound variables this rule actually mentions -- what a PARTIAL
    encoding is partial ABOUT, and what the verdict must name."""
    used = set()
    def walk(e):
        if isinstance(e, (Cmp, Between)):
            if e.var in reg and reg[e.var].unbound:
                used.add(e.var)
        elif isinstance(e, Delta):
            # The prior value is free unless the caller supplies it. Naming it
            # is the whole point: the statement is expressed and what is
            # missing is one datum, rather than the sentence being refused.
            used.add(baseline_var(e.var, e.baseline))
            if e.var in reg and reg[e.var].unbound:
                used.add(e.var)
        elif isinstance(e, Not):
            walk(e.operand)
        elif isinstance(e, (And, Or)):
            for o in e.operands:
                walk(o)
    walk(rule.when); walk(rule.then)
    return tuple(sorted(used))

def delta_baselines(rule: Rule) -> set[tuple[str, str]]:
    """(variable, event) pairs the rule's relative-change nodes measure from."""
    out: set[tuple[str, str]] = set()

    def walk(e):
        if isinstance(e, Delta):
            out.add((e.var, e.baseline))
        elif isinstance(e, Not):
            walk(e.operand)
        elif isinstance(e, (And, Or)):
            for o in e.operands:
                walk(o)
    walk(rule.when); walk(rule.then)
    return out

def events_in(rule: Rule) -> tuple[str, ...]:
    """Every event the rule anchors to -- `Since` AND `Delta`.

    Whoever assembles a bundle registry has to declare these or Track B
    rejects the rule as UNKNOWN_EVENT. The walker in demo.py handled `Since`
    only, so the first `Delta` we ever produced shipped with `events: []` and
    was rejected at load. Defined here so there is one walker, not one per
    caller that happens to need it."""
    out: set[str] = set()

    def walk(e):
        if isinstance(e, Since):
            out.add(e.event)
        elif isinstance(e, Delta):
            out.add(e.baseline)
        elif isinstance(e, Not):
            walk(e.operand)
        elif isinstance(e, (And, Or)):
            for o in e.operands:
                walk(o)
    walk(rule.when); walk(rule.then)
    return tuple(sorted(out))

def hole_defs(reg: Registry, holes: Iterable[str],
              rule: Rule | None = None) -> list[VarDef]:
    """VarDefs for the holes a registry does not already declare.

    Callers used to write `VarDef(h, "bool", ...)` inline, which broke a
    `Delta` two ways at once. A baseline (`<var>_at_<event>`) is a DERIVED
    name that `rule_to_cql` already emits as a named null, so declaring it
    again produced `Identifier "..." is already in use` and the library would
    not translate at all. And it holds the prior value of a REAL variable, so
    typing it bool made `dose <= baseline * 0.5` compare a number to a
    boolean. Skipped here: a derived name is the lowering's to declare, not
    the registry's."""
    derived = {baseline_var(v, e) for v, e in delta_baselines(rule)} if rule else set()
    return [VarDef(h, "bool", None, h, unbound=True)
            for h in holes if h not in reg and h not in derived]

# ---------------------------------------------------------------- English
# A rule is delivered to a governance committee, not to a compiler. Nobody
# adjudicates `{"kind":"cmp","var":"egfr_ml_min_1_73m2","op":"<","value":30}`,
# and nobody reads CQL either -- so the artifact that gets reviewed has to say
# what the rule MEANS, next to the sentence it came from. That comparison is
# the actual unit of clinical review, and it is the only way a reader can tell
# a faithful encoding from one that typechecks.

_OP_WORDS = {"<": "is below", "<=": "is at most", ">": "is above",
             ">=": "is at least", "==": "equals", "!=": "is not"}

def _term(var: str, reg: Registry) -> str:
    """The description if the registry has one -- that is the clinician-facing
    label the binding carried -- otherwise the variable name unmangled."""
    if var in reg and reg[var].description:
        return reg[var].description
    return var.replace("_", " ")

def _event_term(event: str, reg: Registry) -> str:
    if reg.has_event(event) and reg.event(event).description:
        return reg.event(event).description
    return event.replace("_", " ")

def to_english(e: Expr, reg: Registry) -> str:
    """One expression as a readable clause."""
    if isinstance(e, Cmp):
        term = _term(e.var, reg)
        unit = f" {reg[e.var].unit}" if e.var in reg and reg[e.var].unit else ""
        if isinstance(e.value, bool):
            # A boolean is a presence claim, not a comparison. "on_warfarin
            # equals true" is not English; "the patient is on warfarin" is.
            yes = (e.op == "==") == bool(e.value)
            return f"{term}" if yes else f"NOT {term}"
        return f"{term} {_OP_WORDS.get(e.op, e.op)} {e.value}{unit}"
    if isinstance(e, Between):
        unit = f" {reg[e.var].unit}" if e.var in reg and reg[e.var].unit else ""
        lo = "" if e.lo_inclusive else " (exclusive)"
        hi = "" if e.hi_inclusive else " (exclusive)"
        return (f"{_term(e.var, reg)} is between {e.lo}{unit}{lo} "
                f"and {e.hi}{unit}{hi}")
    if isinstance(e, Since):
        return (f"{_event_term(e.event, reg)} happened within the last "
                f"{e.days} day{'s' if e.days != 1 else ''}")
    if isinstance(e, Delta):
        pct = f"{e.lo_pct:.0%}"
        span = f"{e.lo_pct:.0%}-{e.hi_pct:.0%}" if e.hi_pct is not None else pct
        word = "reduced by" if e.direction == "reduce" else "increased by"
        return (f"{_term(e.var, reg)} is {word} at least {span} "
                f"from its value at {_event_term(e.baseline, reg)}")
    if isinstance(e, Not):
        return f"NOT ({to_english(e.operand, reg)})"
    if isinstance(e, (And, Or)):
        joiner = " AND " if isinstance(e, And) else " OR "
        parts = [to_english(o, reg) for o in e.operands]
        return joiner.join(f"({p})" if " AND " in p or " OR " in p else p
                           for p in parts)
    return str(e)

# GRADE-style wording, as guidelines actually write it. `strength` is free
# text from the source, so this matches rather than enumerates.
_WEAK = re.compile(r"\b(weak|conditional|suggest\w*|option\w*|discretionar\w+)\b", re.I)
_STRONG = re.compile(r"\bstrong\b", re.I)

def deontic_force(rule: Rule) -> str:
    """How firmly the rule binds, in words.

    A "weak recommendation against finerenone" rendered as "THEN must: NOT
    finerenone" overstates the guideline, and overstating a recommendation to a
    governance committee is the specific harm this project exists to avoid. The
    DSL's `deontic` field separates obligation from permission; GRADE strength
    is a second axis and the source states it in prose, so both are read."""
    if rule.deontic == "permission":
        return "may"
    text = rule.strength or ""
    if _WEAK.search(text):
        return "should (weak recommendation)"
    if _STRONG.search(text):
        return "must (strong recommendation)"
    return "must"

_LOGIC_OP = {"<": "<", "<=": "\u2264", ">": ">", ">=": "\u2265",
             "==": "=", "!=": "\u2260"}

def to_logic(e: Expr) -> str:
    """The expression in symbolic form, over VARIABLE NAMES.

    The English gloss reads the registry's descriptions, which is right for a
    clinician but hides which variable -- and so which terminology code -- a
    clause actually stands on. Two variables can share a description. This is
    the layer a reviewer checks the bindings against, and it sits between the
    English and the CQL rather than replacing either."""
    if isinstance(e, Cmp):
        val = ("true" if e.value is True else "false" if e.value is False
               else e.value)
        return f"{e.var} {_LOGIC_OP.get(e.op, e.op)} {val}"
    if isinstance(e, Between):
        lo = "\u2264" if e.lo_inclusive else "<"
        hi = "\u2264" if e.hi_inclusive else "<"
        return f"{e.lo} {lo} {e.var} {hi} {e.hi}"
    if isinstance(e, Since):
        op = "\u2264" if e.inclusive else "<"
        return f"since({e.event}) {op} {e.days}d"
    if isinstance(e, Delta):
        sign = "\u2212" if e.direction == "reduce" else "+"
        span = (f"{e.lo_pct:.0%}\u2013{e.hi_pct:.0%}" if e.hi_pct is not None
                else f"{e.lo_pct:.0%}")
        return f"\u0394{e.var} {sign}{span} vs {e.var}@{e.baseline}"
    if isinstance(e, Not):
        inner = to_logic(e.operand)
        return f"\u00ac({inner})" if not isinstance(e.operand, (Cmp, Between, Since, Delta)) \
            else f"\u00ac{inner}"
    if isinstance(e, (And, Or)):
        joiner = " \u2227 " if isinstance(e, And) else " \u2228 "
        parts = []
        for o in e.operands:
            t = to_logic(o)
            parts.append(f"({t})" if isinstance(o, (And, Or)) else t)
        return joiner.join(parts)
    return str(e)

def rule_to_logic(rule: Rule) -> str:
    """phi = when -> then, which is exactly what the solver is given."""
    return f"{to_logic(rule.when)}\n    \u2192  {to_logic(rule.then)}"

def rule_to_english(rule: Rule, reg: Registry) -> str:
    """The whole rule as one sentence a clinician can adjudicate."""
    return (f"IF {to_english(rule.when, reg)}\n"
            f"THEN {deontic_force(rule)}: {to_english(rule.then, reg)}")

@_z3_guarded
def validate_rule(rule: Rule, reg: Registry) -> list[Issue]:
    issues = validate(rule.when, reg, "when") + validate(rule.then, reg, "then")
    # Provenance means a POINTER BACK TO THE SOURCE, and what that pointer is
    # depends on the source. For XML/HTML it is the locator (an XPath) plus
    # offsets; a page number does not exist. Requiring `source_page` made every
    # rule extracted from a JATS document fail a hard error, so `encode` was
    # unreachable on any XML source -- an entire guideline came back 14/14
    # refused before this was spotted.
    if not rule.source_span:
        issues.append(Issue("provenance", "MISSING_PROVENANCE",
                            "every rule must cite the span it came from",
                            "provenance is non-optional: it is what a governance "
                            "committee reviews. Use the XPath locator for XML/HTML "
                            "and the page plus span for PDFs."))
    # The obligation has to be about something the care plan can decide,
    # otherwise the rule constrains nobody and Track B has no atom to frame.
    for h in open_holes(rule, reg):
        issues.append(Issue(h, "OPEN_HOLE",
                            f"{h!r} is an unbound predicate the guideline states but "
                            f"does not define",
                            "the rule is a PARTIAL encoding: verdicts that depend on "
                            "this variable are MAY, not MUST", severity="warn"))
    # A variable whose name says only that the guideline is hedging is not a
    # patient predicate, and no data source will ever populate it: the rule
    # returns UNKNOWN for every patient, forever. Rule.strength is where this
    # belongs, and on the statement that produced `has_may` it was ALREADY
    # filled in correctly with "suggest (conditional)" -- the modal was copied
    # into the logic as well, where it does damage.
    for n in sorted(_vars_in(rule.when) | _vars_in(rule.then)):
        if modal_only(n):
            issues.append(Issue(n, "MODAL_PREDICATE",
                                f"{n!r} encodes the recommendation's STRENGTH, not a "
                                f"fact about the patient",
                                "deontic force belongs in Rule.strength; a statement "
                                "whose condition reduces to a modal is discretionary "
                                "and should be refused as `defeasible`"))
    # The action the sentence is ABOUT has to survive into `then`. Measured on
    # PMC13270348: "epidural analgesia may be considered when expertise is
    # available, and strict aseptic techniques and close monitoring are
    # essential" encoded as
    #     has_expertise_available -> strict_aseptic_techniques AND close_monitoring
    # -- the epidural is absent entirely, and what survives is the PRECONDITION
    # for doing it, promoted to be the recommendation. NO_ACTION_ATOM passed on
    # a technicality, because aseptic technique is itself an action variable.
    #
    # A WARNING, not an error. The match is lexical, and `target` may name the
    # action in a different vocabulary than the variable does
    # ("thromboprophylaxis" vs `on_lmwh`), so a hard rejection here would
    # discard good rules. It belongs in the report, where a human decides.
    _referenced = _vars_in(rule.when) | _vars_in(rule.then) | set(events_in(rule))
    _no_action_dropped = all(n in _referenced for n in reg.actions())
    acts = _action_atoms(rule.then, reg, rule.action, _no_action_dropped)
    if acts and rule.target:
        want = {w for w in re.findall(r"[a-z]+", rule.target.lower())
                if len(w) > 3 and w not in _TARGET_STOP}
        have = set()
        for n in acts:
            have |= set(re.findall(r"[a-z]+", n.lower()))
            if n in reg:
                have |= set(re.findall(r"[a-z]+", (reg[n].description or "").lower()))
        # Prefix matching, not equality: a `target` gloss abbreviates
        # ("ACEi" for `on_ace_inhibitor`), and requiring exact tokens made this
        # fire on a correct rule -- caught by test_bind, which is what that
        # test is for. Loose on purpose; this is a warning, and a missed
        # mismatch costs less than a false one.
        def _related(w: str, h: str) -> bool:
            return len(w) >= 3 and len(h) >= 3 and (w.startswith(h) or h.startswith(w))
        if want and not any(_related(w, h) for w in want for h in have):
            issues.append(Issue("then", "ACTION_TARGET_MISMATCH",
                                f"`then` acts on {sorted(acts)}, but the rule is about "
                                f"{rule.target!r}",
                                "the action the recommendation is about may have been "
                                "dropped, leaving only its preconditions",
                                severity="warn"))
    dropped = dropped_quantities(rule)
    if dropped:
        issues.append(Issue("rule", "DROPPED_THRESHOLD",
                            "the source states "
                            + ", ".join(f"{v:g} {u}" for v, u in dropped)
                            + " and the rule constrains on none of it",
                            "a rule missing its threshold is not a weaker version of "
                            "the recommendation, it is a different one -- check "
                            "whether the quantity belongs in `when` or `then`",
                            severity="warn"))
    if not _action_atoms(rule.then, reg, rule.action, _no_action_dropped):
        issues.append(Issue("then", "NO_ACTION_ATOM",
                            "the obligation mentions no action variable or event",
                            f"actions: {', '.join(reg.actions()) or '(none)'}; "
                            f"events: {', '.join(reg.event_names()) or '(none)'}; "
                            "if the recommendation is not about an action, it is probably "
                            "not_patient_level - refuse it"))
    return issues

# ---------------------------------------------------------------- lowering

HORIZON_DAYS = 3650   # bound on days_since_*, so the solver cannot invent 10^9

def days_since_var(event: str) -> str: return f"days_since_{event}"
def occurred_var(event: str) -> str:   return f"{event}_occurred"

@_z3_guarded
def make_env(reg: Registry) -> dict:
    import z3
    env: dict = {}
    for n in reg.names():
        t = reg.type_of(n)
        env[n] = (z3.Bool(n) if t == "bool" else
                  z3.String(n) if t == "code" else
                  z3.Int(n) if t in {"int", "date"} else
                  z3.Real(n))
    for e in reg.event_names():
        env[occurred_var(e)] = z3.Bool(occurred_var(e))
        env[days_since_var(e)] = z3.Int(days_since_var(e))
        # A baseline symbol per (numeric variable, event). Registries are the
        # STATEMENT's vocabulary, not the document's (R8), so this is a handful
        # of extra symbols, and creating them here keeps `domain_axioms` and
        # `to_z3` working from the same env rather than one of them inventing a
        # symbol the other has never seen.
        for n in reg.names():
            if reg.type_of(n) in {"real", "int"}:
                b = baseline_var(n, e)
                env[b] = z3.Real(b) if reg.type_of(n) == "real" else z3.Int(b)
    return env

@_z3_guarded
def domain_axioms(reg: Registry, env: dict) -> list:
    """Physiological + well-formedness constraints. Without these the solver
    returns counterexamples at eGFR = 2.0 that no clinician will accept."""
    import z3
    ax = []
    for n in reg.names():
        vd = reg[n]
        if vd.unbound:
            continue          # a hole ranges free; constraining it would be a claim
        if vd.type in NUMERIC and vd.plausible:
            ax.append(z3.And(env[n] >= vd.plausible[0], env[n] <= vd.plausible[1]))
        if vd.type == "code" and vd.codes:
            ax.append(z3.Or([env[n] == z3.StringVal(c) for c in vd.codes]))
    for e in reg.event_names():
        d = env[days_since_var(e)]
        ax.append(z3.And(d >= 0, d <= HORIZON_DAYS))
        # A baseline dose is a real prior measurement: strictly positive, and
        # inside the same plausible range as the variable itself. Without the
        # positivity the solver satisfies "reduce by 50%" with a baseline of 0,
        # where every value is simultaneously half of it and the rule becomes
        # unfalsifiable -- the vacuity class, arriving through the back door.
        for n in reg.names():
            vd = reg[n]
            if vd.unbound or vd.type not in NUMERIC:
                continue
            b = env.get(baseline_var(n, e))
            if b is None:
                continue
            ax.append(b > 0)
            if vd.plausible:
                ax.append(z3.And(b >= vd.plausible[0], b <= vd.plausible[1]))
    return ax

@_z3_guarded
def to_z3(expr: Expr, env: dict):
    """Lower to z3. This is the Track B interface for the solver work."""
    import z3
    if isinstance(expr, Cmp):
        x, v = env[expr.var], expr.value
        if isinstance(v, bool):     y = z3.BoolVal(v)
        elif isinstance(v, str):
            d = _iso_date(v)
            y = z3.IntVal(d.toordinal()) if d is not None else z3.StringVal(v)
        elif z3.is_real(x):         y = z3.RealVal(float(v))
        elif z3.is_int(x):
            if float(v) != int(v):
                raise ValueError(f"{expr.var} is integer-sorted; {v} is fractional")
            y = z3.IntVal(int(v))
        else:
            raise TypeError(f"cannot compare {expr.var} ({x.sort()}) with {v!r}")
        # NB: build lazily - eagerly constructing every comparison raises on bools.
        if expr.op == "==": return x == y
        if expr.op == "!=": return x != y
        if expr.op == "<":  return x < y
        if expr.op == "<=": return x <= y
        if expr.op == ">":  return x > y
        if expr.op == ">=": return x >= y
        raise ValueError(f"bad op {expr.op!r}")
    if isinstance(expr, Between):
        x = env[expr.var]
        lo = (x >= expr.lo) if expr.lo_inclusive else (x > expr.lo)
        hi = (x <= expr.hi) if expr.hi_inclusive else (x < expr.hi)
        return z3.And(lo, hi)
    if isinstance(expr, Delta):
        x = env[expr.var]
        base = env[baseline_var(expr.var, expr.baseline)]
        sign = -1.0 if expr.direction == "reduce" else 1.0
        # "reduce BY 50%" leaves 50%: new <= 0.5 * baseline. A range gives two
        # bounds -- reduce by at least lo and at most hi.
        lo_mult = z3.RealVal(1.0 + sign * float(expr.lo_pct))
        parts = [x <= base * lo_mult] if sign < 0 else [x >= base * lo_mult]
        if expr.hi_pct is not None:
            hi_mult = z3.RealVal(1.0 + sign * float(expr.hi_pct))
            parts.append(x >= base * hi_mult if sign < 0 else x <= base * hi_mult)
        return z3.And(*parts) if len(parts) > 1 else parts[0]
    if isinstance(expr, Since):
        # "not checked at all" is the commonest real violation, so the event
        # must carry an occurrence flag; a bare days_since_* lets the solver
        # satisfy the window for a patient who was never checked.
        d = env[days_since_var(expr.event)]
        return z3.And(env[occurred_var(expr.event)],
                      d <= expr.days if expr.inclusive else d < expr.days)
    if isinstance(expr, Not): return z3.Not(to_z3(expr.operand, env))
    if isinstance(expr, And): return z3.And([to_z3(o, env) for o in expr.operands])
    if isinstance(expr, Or):  return z3.Or([to_z3(o, env) for o in expr.operands])
    raise TypeError(expr)

@_z3_guarded
def rule_to_z3(rule: Rule, env: dict):
    """phi = when -> then. THIS is the object Track B verifies."""
    import z3
    return z3.Implies(to_z3(rule.when, env), to_z3(rule.then, env))

# ------------------------------------------------------------- canonical form
# Two names for one concept is what a GENERATED registry produces and a
# hand-authored one does not. Measured on PMC13270348: the formalizer wrote
# both `pca` and `patient_controlled_intravenous_analgesia` into a single rule,
# each bound to SNOMED 1351574007, yielding
#
#     (has_ap AND pca) -> patient_controlled_intravenous_analgesia
#
# which denotes (A AND B) -> B. Valid under every interpretation, so the rule
# CERTIFIES every patient and can never fail. check_nondegenerate had a
# VACUOUS_RULE check and did not fire, because it compares variable SYMBOLS and
# these are two symbols. The identity lives in the terminology layer.
#
# Canonicalising first is what turns that into the vacuity the existing check
# already knows how to report. Only two signals are trusted to merge concepts,
# both ground truth: a shared terminology binding, and a bound variable that an
# unbound one is a `has_`-prefixed restatement of. Never a judgement call.

_HOLE_PREFIXES = ("has_", "is_", "on_", "receives_", "receiving_")

def _stem(name: str) -> str:
    for p in _HOLE_PREFIXES:
        if name.startswith(p):
            return name[len(p):]
    return name

def canonical_aliases(reg: Registry) -> dict[str, str]:
    """{alias -> canonical name} for variables that denote one concept.

    Case 1 -- same binding. Two bound variables carrying the identical
    system|code|version ARE the same concept; the terminology server said so.

    Case 2 -- a hole restating a bound variable. `has_close_monitoring`
    (unbound) alongside `close_monitoring` (bound to SNOMED 182777000) is the
    formalizer inventing a free variable for a concept that already resolved.
    The bound one wins: it has the code, and a hole with a real binding
    available is not residue, it is a duplicate.

    Merging across roles is deliberate and safe: if an observation and an
    action really were distinct here, the merged rule says "if monitored, then
    monitor", and the vacuity check below rejects it. Either the merge is
    right or its consequence is caught."""
    alias: dict[str, str] = {}

    by_binding: dict[str, list[str]] = {}
    for n in reg.names():
        v = reg[n]
        if v.binding and not v.unbound:
            by_binding.setdefault(v.binding, []).append(n)
    for names in by_binding.values():
        if len(names) < 2:
            continue
        # The same code does NOT make two variables the same variable when
        # their TYPES differ. `on_basal_insulin` (bool -- is it prescribed) and
        # `on_basal_insulin_dose` (real -- how much) both carry ATC D049528 and
        # denote different things. Merging them retyped the boolean, and the
        # model's `on_basal_insulin == true` surfaced three stages later as
        # "on_basal_insulin_dose is real but value is bool" -- with the boolean
        # gone from the shipped registry entirely. A rewrite that changes a
        # variable's type is never safe, because every comparison already
        # written against it silently changes meaning.
        by_type: dict[str, list[str]] = {}
        for n in names:
            by_type.setdefault(reg[n].type, []).append(n)
        for group in by_type.values():
            if len(group) < 2:
                continue
            # Deterministic, and prefers the descriptive name: a governance
            # report reading `patient_controlled_intravenous_analgesia` beats
            # one reading `pca`.
            keep = max(group, key=lambda n: (len(n), n))
            for n in group:
                if n != keep:
                    alias[n] = keep

    bound_stems = {_stem(n): n for n in reg.names()
                   if not reg[n].unbound and reg[n].binding}
    for n in reg.names():
        if reg[n].unbound and _stem(n) in bound_stems:
            target = bound_stems[_stem(n)]
            # Same type guard as above. A hole is declared `bool`, so a stem
            # match onto a numeric bound variable would retype it.
            if target != n and reg[target].type == reg[n].type:
                alias[n] = alias.get(target, target)
    return alias

def rename_vars(e: Expr, alias: dict[str, str]) -> Expr:
    """Rewrite variable references. Events are untouched -- Since() names an
    EventDef, which lives in a separate namespace."""
    if not alias:
        return e
    if isinstance(e, Cmp):
        return replace(e, var=alias.get(e.var, e.var))
    if isinstance(e, Between):
        return replace(e, var=alias.get(e.var, e.var))
    if isinstance(e, Delta):
        return replace(e, var=alias.get(e.var, e.var))
    if isinstance(e, Not):
        return replace(e, operand=rename_vars(e.operand, alias))
    if isinstance(e, (And, Or)):
        return replace(e, operands=[rename_vars(o, alias) for o in e.operands])
    return e

def canonicalize(reg: Registry, rules: Iterable[Rule]
                 ) -> tuple[Registry, list[Rule], dict[str, str]]:
    """Collapse duplicate concepts across a whole bundle. Returns the reduced
    registry, the rewritten rules, and the alias map -- the map is REPORTED,
    not silently applied, because a merge is a claim about the guideline."""
    alias = canonical_aliases(reg)
    kept = [reg[n] for n in reg.names() if n not in alias]
    out = [replace(r, when=rename_vars(r.when, alias),
                   then=rename_vars(r.then, alias)) for r in rules]
    return (Registry(kept, [reg.event(e) for e in reg.event_names()]), out, alias)

# ---------------------------------------------------------------- deontic force
# A guideline sentence is either an OBLIGATION ("we recommend prophylaxis" --
# not doing it is a real deviation) or a PERMISSION ("epidural may be
# considered" -- not doing it violates nothing). Rule denotes phi = when ->
# then, which IS an obligation; there is no permission operator. So a
# permissive statement cannot be faithfully encoded here at all, and encoding
# one anyway lets the checker report MUST-violate against a clinician who
# merely declined an option. False alarms are the main reason clinical decision
# support gets switched off, so this is the expensive kind of wrong.
#
# Until the DSL can express permission, the honest mitigation is to make the
# VERDICT respect what `strength` already records: a permissive rule may inform,
# but it may never accuse. See RESULTS.md R17b.
#
# GRADE "conditional"/"weak" is deliberately NOT treated as permission. "We
# suggest anticoagulant prophylaxis" still recommends the action -- the
# weakness is in the evidence, not in the obligation. Only explicit permission
# language counts, which keeps this conservative: it downgrades a verdict only
# where the source says the action is optional.
PERMISSIVE_MARKERS = (
    "may be considered", "may be offered", "may be used", "may be given",
    "can be considered", "can be offered", "might be considered",
    "could be considered", "is an option", "are an option", "as an option",
    "at the discretion", "optional", "if desired", "may be reasonable",
    "is reasonable to consider",
)

def deontic_of(rule: "Rule") -> str:
    """The rule's deontic force. The explicit operator wins; the wording is the
    backstop for a rule written before the field existed, or by a model that
    left it at the default while the source plainly says "may be considered".
    The fallback only ever moves obligation -> permission, never the reverse:
    a wrong guess in that direction silences an alert, which is recoverable; a
    wrong guess the other way accuses a clinician, which is not."""
    if getattr(rule, "deontic", "obligation") == "permission":
        return "permission"
    if is_permissive(rule.strength, rule.source_span):
        return "permission"
    return "obligation"

@_z3_guarded
def violation_condition(rule: "Rule", env: dict):
    """What it takes to VIOLATE this rule -- the formula Track B negates.

    For an obligation that is `when AND NOT then`. For a permission it is
    FALSE, unconditionally: a permitted action that was not taken is not a
    violation, and no patient data can make it one. This is where the operator
    earns its place. The guarantee is structural rather than a guard on the
    verdict string -- there is no patient, and no completion of the unknowns,
    for which a permission yields MUST-violate."""
    import z3
    if deontic_of(rule) == "permission":
        return z3.BoolVal(False)
    return z3.And(to_z3(rule.when, env), z3.Not(to_z3(rule.then, env)))

@_z3_guarded
def conflicts(rules: "Iterable[Rule]", reg: Registry) -> list[dict]:
    """Statements that PERMIT and FORBID the same action for the same patient.

    This is the check the operator buys, and it cannot be written without it:
    with permission collapsed into obligation, "X may be considered" and "avoid
    X" are just two obligations about X, and their disagreement is invisible.
    With the distinction, the question is decidable -- is there a patient
    satisfying both antecedents, for whom one rule permits the action and the
    other requires its absence?

    Reported, never auto-resolved. A guideline that contradicts itself is a
    finding for the committee, not something a compiler should quietly pick a
    side on."""
    import z3
    env = make_env(reg)
    for src, dst in canonical_aliases(reg).items():
        env[src] = env[dst]
    D = domain_axioms(reg, env)
    rules = list(rules)
    out: list[dict] = []
    for i, a in enumerate(rules):
        if deontic_of(a) != "permission":
            continue
        for b in rules:
            if b is a or deontic_of(b) != "obligation":
                continue
            shared = _vars_in(a.then) & _vars_in(b.then)
            if not shared:
                continue
            try:
                s = z3.Solver()
                s.add(*D)
                s.add(to_z3(a.when, env), to_z3(b.when, env),
                      to_z3(a.then, env), z3.Not(to_z3(b.then, env)))
                # Satisfiable = a patient for whom the permitted action is
                # taken and the obligation is thereby broken.
                if s.check() != z3.sat:
                    continue
            except (TypeError, ValueError, KeyError):
                continue
            out.append({"permits": a.rule_id, "requires": b.rule_id,
                        "shared_variables": sorted(shared),
                        "detail": f"{a.rule_id} permits an action that "
                                  f"{b.rule_id} requires be absent, and a patient "
                                  f"can satisfy both antecedents"})
    return out

def is_permissive(strength: str | None, source_span: str | None = None) -> bool:
    """True when the source frames the action as permitted rather than owed.

    Both fields are consulted: `strength` is the model's own summary, and
    `source_span` is verbatim from the document, so a run where the model
    recorded the strength loosely is still caught by the sentence itself."""
    for text in (strength, source_span):
        low = (text or "").lower()
        if any(m in low for m in PERMISSIVE_MARKERS):
            return True
    return False

# ------------------------------------------------------------- modal predicates
# A recommendation carries two separable things: facts about the PATIENT, and
# how hard the guideline is pushing. The second is a property of the document,
# identical for every patient, and this DSL already has a slot for it --
# Rule.strength. On PMC13270348 the formalizer filled that slot correctly with
# "suggest (conditional)" AND ALSO put the modal into the antecedent, as a
# variable named `has_may` distilled from "may be considered".
#
# That rule cannot run: no FHIR resource populates "has may", so it is UNKNOWN
# for every patient forever. GEM and BRIDGE-Wiz avoid this structurally by
# giving a human author a fixed deontic slot; machine extraction reintroduces
# it, because nothing in the decoding grammar forbids the name.
#
# A closed word list is the right tool here and is not the brittle choice it
# looks like: English modals are a CLOSED class -- about a dozen, no new ones
# being coined. Clinical concepts are open-ended, which is why abbreviations
# needed a document-derived pass instead.
MODAL_PREDICATES = frozenset({
    "may", "might", "could", "would", "should", "shall", "must", "can",
    "consider", "considered", "considering", "consideration",
    "optional", "optionally", "discretion", "discretionary", "elective",
    "appropriate", "appropriately", "indicated", "warranted", "necessary",
    "needed", "required", "recommended", "suggested", "advised", "advisable",
    "preferred", "possible", "permitted", "allowed", "reasonable",
})

# The core modal VERBS, as opposed to the discretionary adjectives above. These
# are function words: a predicate about a patient never contains one. Measured:
# "may be sufficient" produced the hole `has_may_sufficient`, which survived the
# all-tokens-modal test because "sufficient" is not itself modal. One modal verb
# anywhere in the name is enough to condemn it.
MODAL_VERBS = frozenset({"may", "might", "could", "would", "should", "shall",
                         "must", "can", "ought"})

# Discretion expressed as a NOUN PHRASE describing the decision process
# itself ("shared decision-making"), not a modal verb or a hedging adjective
# describing a value. MODAL_PREDICATES' token-by-token match can't catch
# this shape no matter how many individual words get added to it, because
# none of the words ("preference", "shared", "decision", "making") are
# themselves hedging -- it's the phrase as a whole that signals discretion.
# Matched by substring, like VAGUE_HINTS, rather than requiring every token
# to match. Measured on PMC13054209:0005: "a preference-sensitive decision,
# requiring a shared decision-making approach" produced
# `has_preference_sensitive_decision` and `shared_decision_making_approach`
# as PARTIAL holes instead of the `defeasible` refusal this is.
MODAL_PHRASES = ("preference_sensitive", "shared_decision_making")

def modal_only(name: str) -> bool:
    """True if a variable name says nothing but 'the guideline is hedging'."""
    stem = _stem(name)
    if any(p in stem for p in MODAL_PHRASES):
        return True
    toks = [t for t in stem.split("_") if t]
    if not toks:
        return False
    if any(t in MODAL_VERBS for t in toks):
        return True
    return all(t in MODAL_PREDICATES for t in toks)

# ---------------------------------------------------------------- degeneracy

@_z3_guarded
def check_nondegenerate(rule: Rule, reg: Registry) -> list[Issue]:
    """The vacuity sweep. A rule can typecheck, compile, and still forbid
    nothing (arXiv:2606.29493) or fire for nobody. Separate from validate() so
    that validate() stays dependency-free."""
    import z3
    env = make_env(reg)
    # Alias duplicate concepts onto ONE solver symbol before asking the solver
    # anything. Two names for one code are two symbols to Z3, and a rule that is
    # a tautology through that identity looks contingent -- which is exactly how
    # (has_ap AND pca) -> patient_controlled_intravenous_analgesia passed as a
    # sound encoding on PMC13270348. Rebinding the env is enough; the rule need
    # not be rewritten, and every existing caller gets the check for free.
    for src, dst in canonical_aliases(reg).items():
        env[src] = env[dst]
    D = domain_axioms(reg, env)
    try:
        when, then = to_z3(rule.when, env), to_z3(rule.then, env)
    except (TypeError, ValueError, KeyError) as exc:
        # An ill-typed rule cannot be lowered, and CRASHING here is the wrong
        # answer: this runs on model output, where `has_x == 60` (a boolean
        # compared to a number) is a normal thing to receive. validate() already
        # reports it as a TYPE_ERROR; report it here too rather than taking the
        # process down.
        return [Issue("rule", "NOT_LOWERABLE",
                      f"rule cannot be lowered to the solver: {exc}",
                      "fix the type errors validate() reports first")]
    phi = z3.Implies(when, then)
    out: list[Issue] = []

    def sat(*claims) -> bool:
        s = z3.Solver(); s.add(D); s.add(*claims)
        return s.check() == z3.sat

    if not sat(when):
        out.append(Issue("when", "UNSATISFIABLE_ANTECEDENT",
                         "no patient in the domain can satisfy `when`",
                         "the rule can never fire; a contradictory conjunction is the "
                         "usual cause - did an OR become an AND?"))
    if not sat(z3.Not(when)):
        out.append(Issue("when", "TAUTOLOGICAL_ANTECEDENT",
                         "`when` holds for every patient in the domain",
                         "the rule applies to everyone; a disjunction that should have "
                         "been a conjunction is the usual cause", severity="warn"))
    if not sat(z3.Not(phi)):
        merged = canonical_aliases(reg)
        why = ("a spec no patient can violate is not a spec")
        if merged:
            why += (f"; note {len(merged)} variable(s) denote one concept and were "
                    f"merged before this check: "
                    + ", ".join(f"{k}={v}" for k, v in sorted(merged.items())))
        out.append(Issue("rule", "VACUOUS_RULE",
                         "NOT(when -> then) is unsatisfiable: the rule forbids nothing",
                         why))
    if not sat(when, then):
        out.append(Issue("then", "UNSATISFIABLE_OBLIGATION",
                         "no patient satisfying `when` can also satisfy `then`",
                         "every applicable patient is in violation by construction"))
    return out

# ---------------------------------------------------------------- equivalence

@_z3_guarded
def distinguishing_patient(a: Rule, b: Rule, reg: Registry) -> dict | None:
    """A patient the two rules disagree about, or None if they are equivalent.

    This is the semantic-difference oracle. The compiler cannot see that two
    rules mean different things -- both typecheck -- so equivalence has to be
    decided by the solver over the domain axioms. Used by the sampling-variance
    probe (do k samples of one statement agree?) and by differential validation
    (do two independent encoders agree?)."""
    import z3
    env = make_env(reg)
    s = z3.Solver()
    s.add(domain_axioms(reg, env))
    s.add(rule_to_z3(a, env) != rule_to_z3(b, env))
    if s.check() != z3.sat:
        return None
    m = s.model()
    out = {}
    for n in reg.names():
        v = m.eval(env[n], model_completion=True)
        out[n] = (z3.is_true(v) if reg.type_of(n) == "bool"
                  else float(v.as_fraction()) if hasattr(v, "as_fraction")
                  else str(v))
    for e in reg.event_names():
        out[occurred_var(e)] = z3.is_true(m.eval(env[occurred_var(e)], True))
        out[days_since_var(e)] = str(m.eval(env[days_since_var(e)], True))
    return out

def equivalent(a: Rule, b: Rule, reg: Registry) -> bool:
    """True iff no patient in the domain is judged differently by the two."""
    return distinguishing_patient(a, b, reg) is None

def equivalence_classes(rules: list[Rule], reg: Registry) -> list[list[int]]:
    """Partition rules into semantic equivalence classes. One class means the
    encodings agree; more than one means the statement admits more than one
    reading, which is the ambiguity signal."""
    classes: list[list[int]] = []
    for i, r in enumerate(rules):
        for cls in classes:
            if equivalent(r, rules[cls[0]], reg):
                cls.append(i)
                break
        else:
            classes.append([i])
    return classes

# ---------------------------------------------------------------- json

def expr_to_dict(e: Expr) -> dict:
    if isinstance(e, Cmp):     return {"kind": "cmp", "var": e.var, "op": e.op, "value": e.value}
    if isinstance(e, Between): return {"kind": "between", "var": e.var, "lo": e.lo, "hi": e.hi,
                                       "lo_inclusive": e.lo_inclusive, "hi_inclusive": e.hi_inclusive}
    if isinstance(e, Since):   return {"kind": "since", "event": e.event, "days": e.days,
                                       "inclusive": e.inclusive}
    if isinstance(e, Delta):   return {"kind": "delta", "var": e.var,
                                       "baseline": e.baseline, "direction": e.direction,
                                       "lo_pct": e.lo_pct, "hi_pct": e.hi_pct}
    if isinstance(e, Not):     return {"kind": "not", "operand": expr_to_dict(e.operand)}
    if isinstance(e, (And, Or)): return {"kind": e.kind,
                                         "operands": [expr_to_dict(o) for o in e.operands]}
    raise TypeError(e)

def expr_from_dict(d: dict) -> Expr:
    k = d.get("kind")
    if k == "cmp":     return Cmp(d["var"], d["op"], d["value"])
    if k == "between": return Between(d["var"], d["lo"], d["hi"],
                                      d.get("lo_inclusive", True), d.get("hi_inclusive", True))
    if k == "since":   return Since(d["event"], d["days"], d.get("inclusive", True))
    if k == "delta":   return Delta(d["var"], d["baseline"], d["direction"],
                                    d["lo_pct"], d.get("hi_pct"))
    if k == "not":     return Not(expr_from_dict(d["operand"]))
    if k == "and":     return And([expr_from_dict(x) for x in d["operands"]])
    if k == "or":      return Or([expr_from_dict(x) for x in d["operands"]])
    raise ValueError(f"unknown expression kind {k!r}")

_expr_from_dict = expr_from_dict   # back-compat alias

def to_dict(obj: Rule | Refusal) -> dict:
    if isinstance(obj, Rule):
        return {"rule_id": obj.rule_id, "when": expr_to_dict(obj.when),
                "then": expr_to_dict(obj.then), "action": obj.action, "target": obj.target,
                "strength": obj.strength, "deontic": deontic_of(obj),
                "source_page": obj.source_page, "source_span": obj.source_span}
    if isinstance(obj, Refusal):
        return {"rule_id": obj.rule_id, "reason_code": obj.reason_code,
                "reason_text": obj.reason_text, "source_page": obj.source_page,
                "source_span": obj.source_span, "confidence": obj.confidence,
                "undefined_predicates": list(obj.undefined_predicates),
                "evidence": list(obj.evidence)}
    raise TypeError(obj)

def from_dict(d: dict) -> Rule | Refusal:
    if "reason_code" in d:
        return Refusal(d["rule_id"], d["reason_code"], d["reason_text"],
                       d.get("source_page"), d.get("source_span"), d.get("confidence"),
                       tuple(d.get("undefined_predicates") or ()),
                       tuple(d.get("evidence") or ()))
    return Rule(d["rule_id"], expr_from_dict(d["when"]), expr_from_dict(d["then"]),
                d["action"], d["target"], d.get("strength"),
                d.get("deontic") or "obligation",
                d.get("source_page"), d.get("source_span"))

def dumps(objs: list[Rule | Refusal], **kw) -> str:
    return json.dumps([to_dict(o) for o in objs], **kw)

def loads(s: str) -> list[Rule | Refusal]:
    return [from_dict(d) for d in json.loads(s)]

# ---------------------------------------------------------- interface bundle

# The DEV_PLAN's Track A / Track B contract is "the typed intermediate DSL and
# its JSON serialization". Rules alone do not satisfy it: a rule is a set of
# names, and without the REGISTRY those names have no type, no unit, no
# terminology binding and no plausible range -- so the consumer cannot build a
# solver environment, cannot assert domain axioms, and cannot tell a hole from
# a bound variable. The bundle is the unit that actually crosses the boundary.

def vardef_to_dict(v: VarDef) -> dict:
    return {"name": v.name, "type": v.type, "unit": v.unit,
            "description": v.description, "binding": v.binding, "role": v.role,
            "plausible": list(v.plausible) if v.plausible else None,
            "codes": list(v.codes), "unbound": v.unbound}

def vardef_from_dict(d: dict) -> VarDef:
    p = d.get("plausible")
    return VarDef(d["name"], d["type"], d.get("unit"), d.get("description", ""),
                  d.get("binding"), d.get("role", "observation"),
                  tuple(p) if p else None, tuple(d.get("codes") or ()),
                  bool(d.get("unbound", False)))

def eventdef_to_dict(e: EventDef) -> dict:
    return {"name": e.name, "description": e.description, "binding": e.binding,
            "unbound": e.unbound}

def eventdef_from_dict(d: dict) -> EventDef:
    return EventDef(d["name"], d.get("description", ""), d.get("binding"),
                    bool(d.get("unbound", False)))

def registry_to_dict(reg: Registry) -> dict:
    return {"variables": [vardef_to_dict(reg[n]) for n in reg.names()],
            "events": [eventdef_to_dict(reg.event(e)) for e in reg.event_names()]}

def registry_from_dict(d: dict) -> Registry:
    return Registry([vardef_from_dict(v) for v in d.get("variables", [])],
                    [eventdef_from_dict(e) for e in d.get("events", [])])

BUNDLE_VERSION = "1"

def bundle_to_dict(reg: Registry, rules: Iterable[Rule],
                   refusals: Iterable[Refusal] = (),
                   findings: Iterable[dict] = ()) -> dict:
    """Everything a consumer needs: the vocabulary, the rules over it, and the
    statements that were refused. The refusals travel WITH the rules on
    purpose -- a consumer that sees only what compiled cannot report the
    residue, and the residue is a deliverable."""
    # Canonicalise at the serialisation boundary, so Track B is never handed a
    # registry in which one concept appears twice. `merged_concepts` records
    # what was collapsed: a merge is a claim about the guideline, and a claim a
    # governance committee cannot see is a claim it cannot check.
    reg, rules, alias = canonicalize(reg, rules)
    out = {"bundle_version": BUNDLE_VERSION,
           "registry": registry_to_dict(reg),
           "rules": [to_dict(r) for r in rules],
           "refusals": [to_dict(r) for r in refusals]}
    if alias:
        out["merged_concepts"] = alias
    # Per-statement findings: the decision, the holes, and the EVIDENCE.
    # Additive and optional, so an older consumer ignores it. It exists because
    # a rule's cautions -- a dropped threshold, a circular baseline, a bound
    # concept the rule never used -- were printed to a terminal and then lost,
    # while the bundle carried evidence for REFUSALS only. A committee reading
    # the artifact therefore saw a clean rule and no trace of the checks that
    # had flagged it. The findings are the reviewable half of the deliverable.
    fnd = list(findings)
    if fnd:
        out["findings"] = fnd
    return out

def bundle_from_dict(d: dict) -> tuple[Registry, list[Rule], list[Refusal]]:
    if str(d.get("bundle_version", BUNDLE_VERSION)) != BUNDLE_VERSION:
        raise ValueError(f"unsupported bundle version {d.get('bundle_version')!r}")
    reg = registry_from_dict(d.get("registry", {}))
    rules = [from_dict(r) for r in d.get("rules", [])]
    refusals = [from_dict(r) for r in d.get("refusals", [])]
    return reg, rules, refusals

def write_bundle(path: str, reg: Registry, rules: Iterable[Rule],
                 refusals: Iterable[Refusal] = (),
                 findings: Iterable[dict] = (), **kw) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(bundle_to_dict(reg, rules, refusals, findings), fh,
                  indent=1, **kw)

def read_bundle(path: str) -> tuple[Registry, list[Rule], list[Refusal]]:
    with open(path, encoding="utf-8") as fh:
        return bundle_from_dict(json.load(fh))

def json_schema(reg: Registry) -> dict:
    """The schema handed to structured decoding. Closed variable set by construction."""
    expr_ref = {"$ref": "#/$defs/expr"}
    return {
      "$schema": "https://json-schema.org/draft/2020-12/schema",
      "title": "GuidelineForge rule or refusal",
      "oneOf": [{"$ref": "#/$defs/rule"}, {"$ref": "#/$defs/refusal"}],
      "$defs": {
        "var": {"enum": reg.names()},
        "event": {"enum": reg.event_names()},
        "expr": {"oneOf": [
          {"type":"object","required":["kind","var","op","value"],"additionalProperties":False,
           "properties":{"kind":{"const":"cmp"},"var":{"$ref":"#/$defs/var"},
                         "op":{"enum":sorted(CMP)},
                         "value":{"type":["number","boolean","string"]}}},
          {"type":"object","required":["kind","var","lo","hi","lo_inclusive","hi_inclusive"],
           "additionalProperties":False,
           "properties":{"kind":{"const":"between"},"var":{"$ref":"#/$defs/var"},
                         "lo":{"type":"number"},"hi":{"type":"number"},
                         "lo_inclusive":{"type":"boolean"},"hi_inclusive":{"type":"boolean"}}},
          {"type":"object","required":["kind","event","days"],"additionalProperties":False,
           "properties":{"kind":{"const":"since"},"event":{"$ref":"#/$defs/event"},
                         "days":{"type":"integer","minimum":0},
                         "inclusive":{"type":"boolean"}}},
          {"type":"object","required":["kind","operand"],"additionalProperties":False,
           "properties":{"kind":{"const":"not"},"operand":expr_ref}},
          {"type":"object","required":["kind","operands"],"additionalProperties":False,
           "properties":{"kind":{"const":"and"},
                         "operands":{"type":"array","minItems":2,"items":expr_ref}}},
          {"type":"object","required":["kind","operands"],"additionalProperties":False,
           "properties":{"kind":{"const":"or"},
                         "operands":{"type":"array","minItems":2,"items":expr_ref}}}]},
        "rule": {"type":"object",
          "required":["rule_id","when","then","action","target","deontic",
                      "source_page","source_span"],
          "additionalProperties":False,
          "properties":{"rule_id":{"type":"string"},"when":expr_ref,"then":expr_ref,
            "action":{"enum":["recommend","avoid","hold","monitor","refer","screen"]},
            "target":{"type":"string"},"strength":{"type":["string","null"]},
            "deontic":{"enum":["obligation","permission"]},
            "source_page":{"type":"integer"},"source_span":{"type":"string"}}},
        "refusal": {"type":"object","required":["rule_id","reason_code","reason_text"],
          "additionalProperties":False,
          "properties":{"rule_id":{"type":"string"},
            "reason_code":{"enum":["vague_predicate","defeasible","not_patient_level",
                                   "unsupported_temporal","unbound_concept","beyond_dsl"]},
            "reason_text":{"type":"string"},
            "source_page":{"type":["integer","null"]},"source_span":{"type":["string","null"]},
            "confidence":{"type":["number","null"],"minimum":0,"maximum":1}}}}}

# ---------------------------------------------------------------- fixture

# The API's structured-output validator accepts a strict subset of JSON Schema.
# Measured against claude-opus-5:
#   REJECTED  circular $ref (expr -> expr), minimum/maximum on number|integer,
#             minItems other than 0 or 1, `oneOf`, `$defs` alongside a top-level
#             `anyOf` (the root must be an object for $defs to be allowed)
#   ACCEPTED  acyclic $ref chains, enum, const, nullable unions, `anyOf`, minItems: 1
# A recursive expression grammar is therefore not directly expressible: this is
# the practical form of the point that the target grammar is context-free and
# FSA-based decoders are unsound for it. The workaround is to UNROLL the
# recursion to a fixed depth, which is sound here because guideline logic is
# shallow -- and anything deeper is a `beyond_dsl` refusal rather than a silent
# truncation. The constraints dropped for decoding (minItems: 2 on junctions,
# the confidence bounds) are all re-checked by validate(), so nothing is lost.
# Depth 2 covers `and/or` over `not/cmp` -- every rule shape seen in the
# corpora so far. Deeper nesting trips the API's compiled-grammar size limit,
# and a statement that genuinely needs it is a `beyond_dsl` refusal rather
# than a silent truncation.
MAX_DECODING_DEPTH = 2

def check_decoding_schema(schema: dict, path: str = "$") -> list[str]:
    """Constraint-check a schema against what the structured-output validator
    accepts, WITHOUT a network call.

    Every rule here was learned from a 400. They are cheap to violate and
    expensive to notice, because a rejected schema surfaces as a fallback to
    the offline heuristic -- which looks exactly like the model finding
    nothing. Measured cost of not having this: `MENTION_SCHEMA` was missing one
    `additionalProperties` and the model path silently never ran."""
    out: list[str] = []
    if not isinstance(schema, dict):
        return out
    # `type` may be a nullable union like ["string", "null"], so normalise
    # before comparing -- a raw list is unhashable and blows up a set test.
    t = schema.get("type")
    types = set(t) if isinstance(t, list) else ({t} if t else set())
    if "object" in types:
        if schema.get("additionalProperties") is not False:
            out.append(f"{path}: object needs additionalProperties=false")
        if "required" not in schema and schema.get("properties"):
            out.append(f"{path}: object needs an explicit `required`")
    if types & {"number", "integer"}:
        for k in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
            if k in schema:
                out.append(f"{path}: {k} is not supported on {schema['type']}")
    if "array" in types and schema.get("minItems", 0) not in (0, 1):
        out.append(f"{path}: minItems must be 0 or 1, got {schema['minItems']}")
    if "oneOf" in schema:
        out.append(f"{path}: `oneOf` is not supported (use anyOf)")
    if "anyOf" in schema and "$defs" in schema:
        out.append(f"{path}: `$defs` cannot sit alongside a top-level anyOf")
    for key in ("properties", "$defs"):
        for name, sub in (schema.get(key) or {}).items():
            out += check_decoding_schema(sub, f"{path}.{key}.{name}")
    for key in ("items", "operand"):
        if isinstance(schema.get(key), dict):
            out += check_decoding_schema(schema[key], f"{path}.{key}")
    for i, sub in enumerate(schema.get("anyOf") or []):
        out += check_decoding_schema(sub, f"{path}.anyOf[{i}]")
    return out

def offered_leaves(reg: Registry, max_depth: int = MAX_DECODING_DEPTH) -> list[str]:
    """Which leaf nodes a constrained decode against `reg` can actually emit.

    A `beyond_dsl` refusal is ambiguous on its own: the DSL may genuinely lack
    the construct, or the construct may EXIST and have been withheld from the
    grammar because the registry had no event to anchor to or no numeric
    variable to change. Three separate times a titration sentence refused as
    "this DSL has no delta" when the DSL had one -- the node was gated off
    upstream and the model was answering honestly about the menu it was given.

    Read off `decoding_schema` rather than a hand-kept list, so it cannot drift
    from what the decoder will actually accept."""
    out: set[str] = set()
    def walk(node):
        if isinstance(node, dict):
            k = node.get("properties", {}).get("kind")
            if isinstance(k, dict) and "const" in k:
                out.add(k["const"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(decoding_schema(reg, max_depth))
    return sorted(out)

def decoding_schema(reg: Registry, max_depth: int = MAX_DECODING_DEPTH) -> dict:
    """`json_schema` flattened into what a constrained decoder will accept.

    Use this for generation and `json_schema` for documentation: the recursive
    version says what the DSL IS, this one says what the decoder can emit."""
    def obj(required, props):
        return {"type": "object", "required": required,
                "additionalProperties": False, "properties": props}

    # `between` means a NUMERIC range -- offering it against every variable,
    # booleans included, let the model pick a boolean action var (drugs and
    # procedures always bind as booleans) with nowhere to attach `lo`/`hi`,
    # caught only afterward as a TYPE_ERROR. Gated the same way `delta` is
    # already gated on `reg.event_names()`: no numeric variable, no leaf.
    numeric_names = [n for n in reg.names() if reg.type_of(n) in NUMERIC]
    leaves = [
        obj(["kind", "var", "op", "value"],
            {"kind": {"const": "cmp"}, "var": {"$ref": "#/$defs/var"},
             "op": {"enum": sorted(CMP)},
             "value": {"type": ["number", "boolean", "string"]}}),
    ]
    if numeric_names:
        leaves.append(obj(
            ["kind", "var", "lo", "hi", "lo_inclusive", "hi_inclusive"],
            {"kind": {"const": "between"}, "var": {"$ref": "#/$defs/numvar"},
             "lo": {"type": "number"}, "hi": {"type": "number"},
             "lo_inclusive": {"type": "boolean"}, "hi_inclusive": {"type": "boolean"}}))
    if reg.event_names():
        # Only offered when the registry has both an event to measure from and
        # a numeric variable to change -- a titration node with nowhere to
        # anchor is a decode the model cannot get right.
        if any(reg.type_of(n) in NUMERIC for n in reg.names()):
            leaves.append(obj(
                ["kind", "var", "baseline", "direction", "lo_pct", "hi_pct"],
                {"kind": {"const": "delta"}, "var": {"$ref": "#/$defs/var"},
                 "baseline": {"$ref": "#/$defs/event"},
                 "direction": {"enum": ["reduce", "increase"]},
                 "lo_pct": {"type": "number"},
                 "hi_pct": {"type": ["number", "null"]}}))
        leaves.append(obj(["kind", "event", "days"],
                          {"kind": {"const": "since"},
                           "event": {"$ref": "#/$defs/event"},
                           "days": {"type": "integer"},
                           "inclusive": {"type": "boolean"}}))

    defs: dict = {"var": {"enum": reg.names()}}
    if numeric_names:
        defs["numvar"] = {"enum": numeric_names}
    if reg.event_names():
        defs["event"] = {"enum": reg.event_names()}
    # Leaves are defined ONCE and referenced. Inlining them at every level
    # multiplies the compiled grammar by the depth and trips the API's
    # grammar-size limit.
    defs["leaf"] = {"anyOf": list(leaves)}
    leaf_ref = {"$ref": "#/$defs/leaf"}
    defs["expr0"] = leaf_ref
    for d in range(1, max_depth + 1):
        child = {"$ref": f"#/$defs/expr{d - 1}"}
        defs[f"expr{d}"] = {"anyOf": [leaf_ref] + [
            obj(["kind", "operand"], {"kind": {"const": "not"}, "operand": child}),
            obj(["kind", "operands"], {"kind": {"const": "and"},
                                       "operands": {"type": "array", "minItems": 1,
                                                    "items": child}}),
            obj(["kind", "operands"], {"kind": {"const": "or"},
                                       "operands": {"type": "array", "minItems": 1,
                                                    "items": child}}),
        ]}
    top = {"$ref": f"#/$defs/expr{max_depth}"}

    defs["rule"] = obj(
        ["rule_id", "when", "then", "action", "target", "deontic",
         "source_page", "source_span"],
        {"rule_id": {"type": "string"}, "when": top, "then": top,
         "action": {"enum": ["recommend", "avoid", "hold", "monitor", "refer", "screen"]},
         "target": {"type": "string"}, "strength": {"type": ["string", "null"]},
         # Required, so the model must COMMIT to a reading rather than letting
         # the default silently promote a permission into an obligation.
         "deontic": {"enum": ["obligation", "permission"]},
         "source_page": {"type": ["integer", "null"]},
         "source_span": {"type": ["string", "null"]}})
    defs["refusal"] = obj(
        ["rule_id", "reason_code", "reason_text"],
        {"rule_id": {"type": "string"},
         "reason_code": {"enum": ["vague_predicate", "defeasible", "not_patient_level",
                                  "unsupported_temporal", "unbound_concept", "beyond_dsl"]},
         "reason_text": {"type": "string"},
         "source_page": {"type": ["integer", "null"]},
         "source_span": {"type": ["string", "null"]},
         "confidence": {"type": ["number", "null"]},
         "undefined_predicates": {"type": "array", "items": {"type": "string"}},
         "evidence": {"type": "array", "items": {"type": "string"}}})
    # The root has to be an object for `$defs` to be legal, so the rule/refusal
    # choice becomes a discriminator rather than a top-level anyOf. The model
    # still has to commit to one or the other, which is the point: refusal is a
    # first-class output of the same decode, not a separate call.
    return obj(["outcome"],
               {"outcome": {"enum": ["rule", "refusal"]},
                "rule": {"anyOf": [{"$ref": "#/$defs/rule"}, {"type": "null"}]},
                "refusal": {"anyOf": [{"$ref": "#/$defs/refusal"}, {"type": "null"}]}}
               ) | {"$defs": defs}

def from_decoded(d: dict) -> Rule | Refusal:
    """Unwrap what `decoding_schema` produces. Raises if the model set the
    discriminator without filling in the matching branch."""
    outcome = d.get("outcome")
    payload = d.get(outcome) if outcome in {"rule", "refusal"} else None
    if payload is None:
        raise ValueError(f"decoder returned outcome={outcome!r} with no {outcome} body")
    return from_dict(payload)

CKD_REGISTRY = Registry([
    VarDef("egfr", "real", "mL/min/1.73m2", "estimated glomerular filtration rate",
           "LOINC:33914-3", plausible=(0, 200)),
    # mg/g vs mg/mmol is a factor of ~8.84 and is the classic silent unit slip.
    VarDef("acr_mg_g", "real", "mg/g", "urine albumin-to-creatinine ratio",
           "LOINC:9318-7", plausible=(0, 30000)),
    VarDef("age", "real", "years", "patient age", plausible=(0, 120)),
    VarDef("potassium", "real", "mmol/L", "serum potassium", "LOINC:2823-3",
           plausible=(1.5, 9.0)),
    VarDef("hba1c", "real", "%", "hemoglobin A1c", "LOINC:4548-4", plausible=(3.0, 20.0)),
    VarDef("on_dialysis", "bool", None, "receiving maintenance dialysis"),
    VarDef("contrast_within_48h", "bool", None, "iodinated contrast within 48 hours"),
    VarDef("has_diabetes", "bool", None, "diagnosis of diabetes mellitus"),
    VarDef("has_hypertension", "bool", None, "diagnosis of hypertension"),
    VarDef("metabolic_acidosis", "bool", None, "acute or chronic metabolic acidosis"),
    # --- actions: things a care plan decides -----------------------------
    VarDef("on_acei_arb", "bool", None, "currently prescribed an ACEi or ARB", role="action"),
    VarDef("nephrology_referral", "bool", None, "referral to nephrology placed", role="action"),
    VarDef("sglt2_on", "bool", None, "currently prescribed an SGLT2 inhibitor", role="action"),
], events=[
    EventDef("potassium_check", "serum potassium measured", "LOINC:2823-3"),
    EventDef("egfr_check", "eGFR measured", "LOINC:33914-3"),
    EventDef("acr_check", "urine ACR measured", "LOINC:9318-7"),
])
