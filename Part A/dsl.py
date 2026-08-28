"""
GuidelineForge intermediate DSL - the Track A / Track B interface.

Design constraints, each traceable to a preliminary result:
  P3: an expression language without disjunction FORCED a wrong encoding
      (AND-for-OR, wrong referral verdict for 69.5% of the patient space).
      => boolean structure (and/or/not) is mandatory, not optional.
  P1: a typechecker cannot see a semantically wrong rule, so the validator's
      job is to be STRICT about what it CAN see and to emit machine-readable
      errors the repair loop can act on.
  P0: statements that cannot be faithfully encoded must ABSTAIN with a reason
      rather than be approximated. Refusal is a first-class value, not a failure.

A Rule is a CHECKABLE FORMULA, not a label. `when` is the applicability
condition and `then` is the obligation, both expressions over the same closed
registry; the rule denotes phi = (when -> then). Track B verifies phi. The
`action`/`target` fields are the human-readable gloss for the UI and for
provenance review, and carry no semantics.

TIME. The registry declares EVENTS as well as variables, and `Since(event, n)`
means "the event occurred within the last n days". It is compiled here to
`<event>_occurred AND days_since_<event> <= n` over a single visit - i.e. the
metric-temporal layer is flattened to derived state, which is bounded
unrolling at T=1. Two consequences the paper must state:
  * flattening is CONTAINED: `days_since_*` is produced by this lowering, not
    hand-written by the model, so there is exactly one place it can be wrong;
  * flattening is VISIBLE: `fragment_of` still reports MTL for any rule
    containing a Since node, so the M1 spectrum does not silently reclassify
    monitoring rules as plain linear arithmetic.
Genuinely multi-visit operators - "on two occasions >= 90 days apart",
"sustained for 3 months" - are NOT expressible and must route to the
`unsupported_temporal` refusal. Moving to a real time-indexed semantics is a
strictly additive change: Since nodes get a new lowering, written rules do not
change.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date as _date
from typing import Any, Literal, Union
import json

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
    """A datable clinical event, referred to by Since()."""
    name: str
    description: str = ""
    binding: str | None = None

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

Expr = Union[Cmp, Between, Since, Not, And, Or]

Action = Literal["recommend", "avoid", "hold", "monitor", "refer", "screen"]

@dataclass
class Rule:
    """Denotes phi = (when -> then). `action`/`target` are the gloss only."""
    rule_id: str
    when: Expr
    then: Expr
    action: Action
    target: str
    strength: str | None = None
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
    elif isinstance(expr, Since):
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

def _action_atoms(expr: Expr, reg: Registry) -> set[str]:
    """Atoms in an obligation that a care plan can actually decide. A Since
    node counts: in a `then`, "potassium checked within 14 days" is a demand
    that the plan order the test, not an observation of patient state."""
    if isinstance(expr, (Cmp, Between)):
        return {expr.var} if expr.var in reg and reg.role_of(expr.var) == "action" else set()
    if isinstance(expr, Since):
        return {expr.event} if reg.has_event(expr.event) else set()
    if isinstance(expr, Not):
        return _action_atoms(expr.operand, reg)
    if isinstance(expr, (And, Or)):
        return set().union(*(_action_atoms(o, reg) for o in expr.operands)) if expr.operands else set()
    return set()

def open_holes(rule: Rule, reg: Registry) -> tuple[str, ...]:
    """Unbound variables this rule actually mentions -- what a PARTIAL
    encoding is partial ABOUT, and what the verdict must name."""
    used = set()
    def walk(e):
        if isinstance(e, (Cmp, Between)):
            if e.var in reg and reg[e.var].unbound:
                used.add(e.var)
        elif isinstance(e, Not):
            walk(e.operand)
        elif isinstance(e, (And, Or)):
            for o in e.operands:
                walk(o)
    walk(rule.when); walk(rule.then)
    return tuple(sorted(used))

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
    if not _action_atoms(rule.then, reg):
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
    return env

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
    return ax

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

def rule_to_z3(rule: Rule, env: dict):
    """phi = when -> then. THIS is the object Track B verifies."""
    import z3
    return z3.Implies(to_z3(rule.when, env), to_z3(rule.then, env))

# ---------------------------------------------------------------- degeneracy

def check_nondegenerate(rule: Rule, reg: Registry) -> list[Issue]:
    """The vacuity sweep. A rule can typecheck, compile, and still forbid
    nothing (arXiv:2606.29493) or fire for nobody. Separate from validate() so
    that validate() stays dependency-free."""
    import z3
    env = make_env(reg)
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
        out.append(Issue("rule", "VACUOUS_RULE",
                         "NOT(when -> then) is unsatisfiable: the rule forbids nothing",
                         "a spec no patient can violate is not a spec"))
    if not sat(when, then):
        out.append(Issue("then", "UNSATISFIABLE_OBLIGATION",
                         "no patient satisfying `when` can also satisfy `then`",
                         "every applicable patient is in violation by construction"))
    return out

# ---------------------------------------------------------------- equivalence

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
    if k == "not":     return Not(expr_from_dict(d["operand"]))
    if k == "and":     return And([expr_from_dict(x) for x in d["operands"]])
    if k == "or":      return Or([expr_from_dict(x) for x in d["operands"]])
    raise ValueError(f"unknown expression kind {k!r}")

_expr_from_dict = expr_from_dict   # back-compat alias

def to_dict(obj: Rule | Refusal) -> dict:
    if isinstance(obj, Rule):
        return {"rule_id": obj.rule_id, "when": expr_to_dict(obj.when),
                "then": expr_to_dict(obj.then), "action": obj.action, "target": obj.target,
                "strength": obj.strength, "source_page": obj.source_page,
                "source_span": obj.source_span}
    if isinstance(obj, Refusal):
        return {"rule_id": obj.rule_id, "reason_code": obj.reason_code,
                "reason_text": obj.reason_text, "source_page": obj.source_page,
                "source_span": obj.source_span, "confidence": obj.confidence,
                "undefined_predicates": list(obj.undefined_predicates)}
    raise TypeError(obj)

def from_dict(d: dict) -> Rule | Refusal:
    if "reason_code" in d:
        return Refusal(d["rule_id"], d["reason_code"], d["reason_text"],
                       d.get("source_page"), d.get("source_span"), d.get("confidence"),
                       tuple(d.get("undefined_predicates") or ()))
    return Rule(d["rule_id"], expr_from_dict(d["when"]), expr_from_dict(d["then"]),
                d["action"], d["target"], d.get("strength"),
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
    return {"name": e.name, "description": e.description, "binding": e.binding}

def eventdef_from_dict(d: dict) -> EventDef:
    return EventDef(d["name"], d.get("description", ""), d.get("binding"))

def registry_to_dict(reg: Registry) -> dict:
    return {"variables": [vardef_to_dict(reg[n]) for n in reg.names()],
            "events": [eventdef_to_dict(reg.event(e)) for e in reg.event_names()]}

def registry_from_dict(d: dict) -> Registry:
    return Registry([vardef_from_dict(v) for v in d.get("variables", [])],
                    [eventdef_from_dict(e) for e in d.get("events", [])])

BUNDLE_VERSION = "1"

def bundle_to_dict(reg: Registry, rules: Iterable[Rule],
                   refusals: Iterable[Refusal] = ()) -> dict:
    """Everything a consumer needs: the vocabulary, the rules over it, and the
    statements that were refused. The refusals travel WITH the rules on
    purpose -- a consumer that sees only what compiled cannot report the
    residue, and the residue is a deliverable."""
    return {"bundle_version": BUNDLE_VERSION,
            "registry": registry_to_dict(reg),
            "rules": [to_dict(r) for r in rules],
            "refusals": [to_dict(r) for r in refusals]}

def bundle_from_dict(d: dict) -> tuple[Registry, list[Rule], list[Refusal]]:
    if str(d.get("bundle_version", BUNDLE_VERSION)) != BUNDLE_VERSION:
        raise ValueError(f"unsupported bundle version {d.get('bundle_version')!r}")
    reg = registry_from_dict(d.get("registry", {}))
    rules = [from_dict(r) for r in d.get("rules", [])]
    refusals = [from_dict(r) for r in d.get("refusals", [])]
    return reg, rules, refusals

def write_bundle(path: str, reg: Registry, rules: Iterable[Rule],
                 refusals: Iterable[Refusal] = (), **kw) -> None:
    with open(path, "w") as fh:
        json.dump(bundle_to_dict(reg, rules, refusals), fh, indent=1, **kw)

def read_bundle(path: str) -> tuple[Registry, list[Rule], list[Refusal]]:
    with open(path) as fh:
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
          "required":["rule_id","when","then","action","target","source_page","source_span"],
          "additionalProperties":False,
          "properties":{"rule_id":{"type":"string"},"when":expr_ref,"then":expr_ref,
            "action":{"enum":["recommend","avoid","hold","monitor","refer","screen"]},
            "target":{"type":"string"},"strength":{"type":["string","null"]},
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

def decoding_schema(reg: Registry, max_depth: int = MAX_DECODING_DEPTH) -> dict:
    """`json_schema` flattened into what a constrained decoder will accept.

    Use this for generation and `json_schema` for documentation: the recursive
    version says what the DSL IS, this one says what the decoder can emit."""
    def obj(required, props):
        return {"type": "object", "required": required,
                "additionalProperties": False, "properties": props}

    leaves = [
        obj(["kind", "var", "op", "value"],
            {"kind": {"const": "cmp"}, "var": {"$ref": "#/$defs/var"},
             "op": {"enum": sorted(CMP)},
             "value": {"type": ["number", "boolean", "string"]}}),
        obj(["kind", "var", "lo", "hi", "lo_inclusive", "hi_inclusive"],
            {"kind": {"const": "between"}, "var": {"$ref": "#/$defs/var"},
             "lo": {"type": "number"}, "hi": {"type": "number"},
             "lo_inclusive": {"type": "boolean"}, "hi_inclusive": {"type": "boolean"}}),
    ]
    if reg.event_names():
        leaves.append(obj(["kind", "event", "days"],
                          {"kind": {"const": "since"},
                           "event": {"$ref": "#/$defs/event"},
                           "days": {"type": "integer"},
                           "inclusive": {"type": "boolean"}}))

    defs: dict = {"var": {"enum": reg.names()}}
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
        ["rule_id", "when", "then", "action", "target", "source_page", "source_span"],
        {"rule_id": {"type": "string"}, "when": top, "then": top,
         "action": {"enum": ["recommend", "avoid", "hold", "monitor", "refer", "screen"]},
         "target": {"type": "string"}, "strength": {"type": ["string", "null"]},
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
         "undefined_predicates": {"type": "array", "items": {"type": "string"}}})
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
