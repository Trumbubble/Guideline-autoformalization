"""
Stage 4b: lower the typed DSL to HL7 CQL, and repair against a compiler.

The lowering is DETERMINISTIC. The model's only job was to produce a validated
`dsl.Rule`; turning that into CQL is a total function with no judgement in it,
which is the point -- it is the step a governance committee can re-run and get
the same artifact.

TWO LOWERINGS OF THE SAME DSL, and this is deliberate:

    dsl.to_z3    flattens time -- `Since(e, n)` becomes `e_occurred and
                 days_since_e <= n` at a single instant, because Z3 has no
                 notion of a patient timeline.
    cql.to_cql   keeps time -- the same node becomes a real retrieve with a
                 temporal `where`, because a CQL engine evaluates over the
                 record and does the existential itself.

So the DSL does NOT need time-indexed variables for CQL to be faithful: CQL's
`[Observation: X] O where O.effective during Interval[...]` IS the bounded
existential. That is what makes the earlier decision to defer time-indexing
safe rather than merely convenient.

HOLES BECOME `null`, NOT `false`. CQL is natively three-valued -- `true and
null` is null, `false and null` is false -- so an unbound predicate propagates
exactly the uncertainty it should, and the engine returns null rather than a
verdict. PathWISE stubs uncomputable nodes as `false`, which is a two-valued
choice that throws away information the language already carries.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Iterable

import dsl

CQL_VERSION = "1.0.000"
FHIR_VERSION = "4.0.1"

# FHIR resource each kind of concept is retrieved from.
KIND_RESOURCE = {
    "lab": "Observation", "condition": "Condition", "procedure": "Procedure",
    "drug": "MedicationRequest", "drug_class": "MedicationRequest",
}

@dataclass
class CqlLibrary:
    name: str
    text: str
    valuesets: tuple[str, ...] = ()
    codesystems: tuple[str, ...] = ()
    holes: tuple[str, ...] = ()

    def __str__(self) -> str:
        return self.text

# ---------------------------------------------------------------- naming

def _label(var: str, reg: dsl.Registry) -> str:
    vd = reg[var] if var in reg else None
    label = (vd.description if vd and vd.description else var).strip()
    return re.sub(r"\s+", " ", label).replace('"', "'")

def cql_name(var: str, reg: dsl.Registry) -> str:
    """The DEFINE's name -- the expression the rule refers to."""
    return '"' + _label(var, reg) + '"'

def event_term(event: str, reg: dsl.Registry) -> str:
    """Terminology name for an event. A `Since` node lowers to a retrieve, and
    a retrieve needs a declared value set or code -- events were being used in
    `[Observation: ...]` without ever being declared."""
    ed = reg.event(event) if reg.has_event(event) else None
    label = (ed.description if ed and ed.description else
             event.replace("_", " ")).strip()
    return '"' + re.sub(r"\s+", " ", label).title() + ' codes"'

def term_name(var: str, reg: dsl.Registry) -> str:
    """The TERMINOLOGY's name, and it must differ from the define's.

    `[Condition: X]` requires X to be a valueset or code declaration. Reusing
    the define's name there produces a self-referential library that looks
    plausible and is not valid CQL."""
    return '"' + _label(var, reg) + ' codes"'

def _slug(s: str) -> str:
    """A safe CQL library identifier.

    Statement ids carry the document they came from -- `PMC12355350:0129` --
    and a colon is not legal in a CQL identifier. An unsanitised name produced
    `library DemoPMC12355350:0129`, which the translator rejected with
    "Syntax error at :" and cost the whole statement. A leading digit is
    illegal too, so one is prefixed away."""
    out = re.sub(r"[^A-Za-z0-9]+", "", s.title())
    if not out:
        return "Rule"
    return out if out[0].isalpha() else f"L{out}"

def parse_binding(binding: str | None) -> tuple[str, str, str | None]:
    """`VarDef.binding` -> (kind, identifier, version).

    Two shapes come out of the binder: a VSAC value set
    (`http://cts.nlm.nih.gov/fhir/ValueSet|<oid>|<version>`) and a single
    concept (`http://snomed.info/sct|<code>|<version>`). CQL declares these
    differently -- `valueset` versus `codesystem` + `code` -- so they have to
    be told apart."""
    if not binding:
        return ("none", "", None)
    if "|" in binding:
        parts = binding.split("|")
        system, ident = parts[0], (parts[1] if len(parts) > 1 else "")
        version = parts[2] if len(parts) > 2 else None
    elif ":" in binding and not binding.startswith("http"):
        # The short `SNOMED:73211009` / `LOINC:9318-7` form used by
        # hand-written registries.
        system, ident, version = binding.split(":", 1)[0], binding.split(":", 1)[1], None
    else:
        return ("none", "", None)
    if "ValueSet" in system or ident.count(".") >= 4:
        return ("valueset", ident, version)
    return ("code", ident, version)

CODESYSTEM_URLS = {
    "SNOMED": "http://snomed.info/sct",
    "LOINC": "http://loinc.org",
    "RxNorm": "http://www.nlm.nih.gov/research/umls/rxnorm",
    "ATC": "http://www.whocc.no/atc",
}

def _codesystem_for(binding: str) -> str | None:
    for name, url in CODESYSTEM_URLS.items():
        if binding.startswith(url) or binding.startswith(name + ":"):
            return name
    return None

# ---------------------------------------------------------------- declarations

def declarations(reg: dsl.Registry) -> tuple[list[str], list[str], list[str]]:
    """`valueset` / `codesystem` / `code` declarations for the bound registry.

    These are the artifact's terminology contract: version-pinned identifiers a
    reviewer can resolve against VSAC themselves. They are the reason the
    binder pins versions."""
    valuesets, codesystems, codes = [], {}, []
    for n in reg.names():
        vd = reg[n]
        if vd.unbound:
            continue                       # a hole has no terminology, by design
        kind, ident, version = parse_binding(vd.binding)
        if kind == "valueset":
            v = f" version '{version}'" if version else ""
            valuesets.append(f"valueset {term_name(n, reg)}: 'urn:oid:{ident}'{v}")
        elif kind == "code":
            cs = _codesystem_for(vd.binding or "")
            if cs:
                codesystems[cs] = CODESYSTEM_URLS[cs]
                codes.append(f"code {term_name(n, reg)}: '{ident}' from \"{cs}\""
                             f" display '{_label(n, reg)}'")
    for e in reg.event_names():
        ed = reg.event(e)
        kind, ident, version = parse_binding(ed.binding)
        if kind == "valueset":
            v = f" version '{version}'" if version else ""
            valuesets.append(f"valueset {event_term(e, reg)}: 'urn:oid:{ident}'{v}")
        elif kind == "code":
            cs = _codesystem_for(ed.binding or "")
            if cs:
                codesystems[cs] = CODESYSTEM_URLS[cs]
                codes.append(f"code {event_term(e, reg)}: '{ident}' from \"{cs}\"")
        else:
            # An event with no binding cannot be retrieved. Declare it as an
            # explicit hole rather than emitting a dangling reference.
            codes.append(f"// NO TERMINOLOGY BINDING for event {e!r}")
    cs_lines = [f"codesystem \"{k}\": '{v}'" for k, v in sorted(codesystems.items())]
    return sorted(set(valuesets)), cs_lines, sorted(set(codes))

def variable_defines(reg: dsl.Registry) -> list[str]:
    """One `define` per registry variable, so every atom in the rule resolves
    to something a CQL engine can evaluate against a FHIR record."""
    out = []
    for n in reg.names():
        vd = reg[n]
        term = cql_name(n, reg)
        if vd.unbound:
            # Three-valued by construction. `null` is the honest encoding of a
            # predicate the source states but never defines; `false` would
            # silently narrow the rule and `true` would over-fire.
            out.append(f"// UNBOUND: the source states this predicate but does "
                       f"not define it.\n"
                       f"// Left null so CQL's three-valued logic propagates the "
                       f"uncertainty.\ndefine {term}:\n  null as Boolean")
            continue
        vs = term_name(n, reg)
        if parse_binding(vd.binding)[0] == "none":
            out.append(f"// NO TERMINOLOGY BINDING for {n!r}; cannot retrieve.\n"
                       f"define {term}:\n  null as Boolean")
            continue
        res = KIND_RESOURCE.get(_kind_of(vd), "Observation")
        if vd.type == "bool":
            out.append(f"define {term}:\n  exists ([{res}: {vs}])")
        else:
            unit = f" // recorded in {vd.unit}" if vd.unit else ""
            out.append(
                f"define {term}:{unit}\n"
                f"  Last([Observation: {vs}] O\n"
                f"    where O.status in {{'final', 'amended', 'corrected'}}\n"
                f"    sort by effective).value as Quantity")
    return out

def _kind_of(vd: dsl.VarDef) -> str:
    if vd.type in dsl.NUMERIC:
        return "lab"
    if vd.role == "action":
        return "drug_class"
    return "condition"

# ---------------------------------------------------------------- expressions

CQL_OPS = {"<": "<", "<=": "<=", ">": ">", ">=": ">=", "==": "=", "!=": "!="}

def expr_to_cql(e: dsl.Expr, reg: dsl.Registry, indent: int = 1) -> str:
    pad = "  " * indent
    if isinstance(e, dsl.Cmp):
        term = cql_name(e.var, reg)
        if isinstance(e.value, bool):
            return term if e.value else f"not {term}"
        if isinstance(e.value, str):
            return f"{term} {CQL_OPS[e.op]} '{e.value}'"
        vd = reg[e.var] if e.var in reg else None
        unit = f" '{vd.unit}'" if vd and vd.unit else ""
        return f"{term} {CQL_OPS[e.op]} {e.value}{unit}"
    if isinstance(e, dsl.Between):
        term = cql_name(e.var, reg)
        vd = reg[e.var] if e.var in reg else None
        unit = f" '{vd.unit}'" if vd and vd.unit else ""
        lo = ">=" if e.lo_inclusive else ">"
        hi = "<=" if e.hi_inclusive else "<"
        return f"{term} {lo} {e.lo}{unit} and {term} {hi} {e.hi}{unit}"
    if isinstance(e, dsl.Since):
        label = event_term(e.event, reg)
        op = "<=" if e.inclusive else "<"
        _ = op   # inclusivity is carried by the interval bound below
        end = "Today()" if e.inclusive else "Today() - 1 day"
        return (f"exists ([Observation: {label}] O\n"
                f"{pad}  where O.effective during "
                f"Interval[Today() - {e.days} days, {end}])")
    if isinstance(e, dsl.Delta):
        term = cql_name(e.var, reg)
        base = '"' + _label(dsl.baseline_var(e.var, e.baseline), reg) + '"'
        sign = -1.0 if e.direction == "reduce" else 1.0
        lo = round(1.0 + sign * float(e.lo_pct), 6)
        parts = [f"{term} {'<=' if sign < 0 else '>='} {base} * {lo}"]
        if e.hi_pct is not None:
            hi = round(1.0 + sign * float(e.hi_pct), 6)
            parts.append(f"{term} {'>=' if sign < 0 else '<='} {base} * {hi}")
        return "(" + ("\n" + pad + "and ").join(parts) + ")"
    if isinstance(e, dsl.Not):
        return f"not ({expr_to_cql(e.operand, reg, indent)})"
    if isinstance(e, (dsl.And, dsl.Or)):
        joiner = "\n" + pad + ("and " if isinstance(e, dsl.And) else "or ")
        return "(" + joiner.join(expr_to_cql(o, reg, indent + 1)
                                 for o in e.operands) + ")"
    raise TypeError(f"cannot lower {e!r} to CQL")

# ---------------------------------------------------------------- library

def _delta_baselines(rule: dsl.Rule) -> set[tuple[str, str]]:
    """(variable, event) pairs the rule's relative-change nodes measure from."""
    out: set[tuple[str, str]] = set()

    def walk(e):
        if isinstance(e, dsl.Delta):
            out.add((e.var, e.baseline))
        elif isinstance(e, dsl.Not):
            walk(e.operand)
        elif isinstance(e, (dsl.And, dsl.Or)):
            for o in e.operands:
                walk(o)
    walk(rule.when); walk(rule.then)
    return out

def rule_to_cql(rule: dsl.Rule, reg: dsl.Registry, *,
                library_name: str | None = None) -> CqlLibrary:
    """A complete, self-contained CQL library for one rule."""
    # Sanitise whatever the caller passed: this function is what guarantees the
    # output is valid CQL, so it cannot trust a name handed to it.
    name = _slug(library_name or rule.rule_id)
    valuesets, codesystems, codes = declarations(reg)
    holes = tuple(n for n in reg.names() if reg[n].unbound)

    lines = [f"library {name} version '{CQL_VERSION}'", "",
             f"using FHIR version '{FHIR_VERSION}'",
             f"include FHIRHelpers version '{FHIR_VERSION}' called FHIRHelpers", ""]
    if codesystems:
        lines += codesystems + [""]
    if valuesets:
        lines += valuesets + [""]
    if codes:
        lines += codes + [""]
    lines += ["context Patient", ""]

    # Provenance in the artifact itself: this is what a reviewer checks the
    # rule against, and it is the unit of clinician adjudication.
    lines += [f"/* {rule.rule_id}",
              f"   action: {rule.action} {rule.target}"]
    if rule.strength:
        lines.append(f"   strength: {rule.strength}")
    if rule.source_span:
        lines.append(f"   source: {rule.source_span}"
                     + (f" (page {rule.source_page})" if rule.source_page else ""))
    if holes:
        lines.append(f"   PARTIAL: {len(holes)} unbound predicate(s) left null: "
                     + ", ".join(holes))
    lines += ["*/", ""]

    lines += variable_defines(reg) + [""]

    # A relative change needs the variable's value at the baseline event, which
    # no standard FHIR retrieve expresses: "the basal insulin dose the patient
    # was on before the diet started" is an institution-specific query over
    # prior MedicationRequests. Emitted as a NAMED null, so the missing datum is
    # declared rather than guessed -- Arden Syntax made the same call with its
    # curly braces, and it is the same discipline as an unbound predicate here.
    for var, event in sorted(_delta_baselines(rule)):
        b = dsl.baseline_var(var, event)
        vd = reg[var] if var in reg else None
        unit = f" -- in {vd.unit}" if vd and vd.unit else ""
        lines += [f"// BASELINE: {var!r} as it stood at {event!r}{unit}.",
                  f"// No portable retrieve expresses a prior value; bind this to "
                  f"your site's query.",
                  f'define "{_label(b, reg)}":\n  null as Decimal', ""]
    lines += [f"define \"Applicable\":\n  {expr_to_cql(rule.when, reg)}", ""]
    lines += [f"define \"Satisfied\":\n  {expr_to_cql(rule.then, reg)}", ""]
    # `not Applicable or Satisfied` is the material implication the DSL means.
    # With a null hole inside Applicable this evaluates to null, not false --
    # which is the three-valued verdict, expressed in the target language.
    lines += ['define "Compliant":\n  not "Applicable" or "Satisfied"', ""]

    # CQL has no deontic content: it is an expression language, so "Violated"
    # means the same thing whether the source required the action or merely
    # offered it. The distinction therefore CANNOT live in the lowering -- it
    # can only live at the verdict layer, which is a property of the target
    # language rather than a shortcut taken here (RESULTS.md R17d).
    #
    # What the library can do is carry the operator so a consumer is not left
    # to infer it: `Deontic` is emitted as a literal, and for a permission
    # `Violated` is defined as false, so even a consumer that reads only that
    # define cannot turn a declined option into a violation.
    deontic = dsl.deontic_of(rule)
    lines += [f"""define "Deontic":\n  '{deontic}'""", ""]
    if deontic == "permission":
        lines += ['// The source PERMITS this action; it does not require it.',
                  '// Not taking a permitted option is not a violation.',
                  'define "OptionTaken":\n  "Applicable" and "Satisfied"', "",
                  'define "OptionNotTaken":\n  "Applicable" and not "Satisfied"', "",
                  'define "Violated":\n  false', ""]
    else:
        lines += ['define "Violated":\n  "Applicable" and not "Satisfied"', ""]

    return CqlLibrary(name, "\n".join(lines), tuple(valuesets),
                      tuple(codesystems), holes)

# ---------------------------------------------------------------- checking

CQL_KEYWORDS = {"and", "or", "not", "exists", "where", "from", "return", "sort",
                "by", "during", "in", "as", "null", "true", "false", "define",
                "context", "library", "using", "include", "called", "version",
                "valueset", "codesystem", "code", "display", "Interval",
                "Today", "Last", "First", "Quantity", "System", "Boolean"}

def check_cql(text: str) -> list[dsl.Issue]:
    """Deterministic structural checks, offline.

    This is NOT a substitute for the official cql-to-elm translator -- it
    cannot see a type error and does not know the FHIR model. It exists so the
    obvious breakage (unbalanced delimiters, a define referring to a name
    nothing declares) is caught without a JVM, and so `repair` has something to
    push back on when no compiler is wired in."""
    out: list[dsl.Issue] = []
    if not re.match(r"^library\s+\w+", text.strip()):
        out.append(dsl.Issue("library", "MISSING_LIBRARY_HEADER",
                             "a CQL library must start with `library <name>`"))
    if "context " not in text:
        out.append(dsl.Issue("library", "MISSING_CONTEXT",
                             "no `context` declared; add `context Patient`"))
    for open_, close in (("(", ")"), ("[", "]"), ("{", "}")):
        if text.count(open_) != text.count(close):
            out.append(dsl.Issue("library", "UNBALANCED_DELIMITER",
                                 f"{text.count(open_)} '{open_}' vs "
                                 f"{text.count(close)} '{close}'"))
    # every quoted identifier used must be declared somewhere
    declared = set(re.findall(r'^\s*(?:define|valueset|code|codesystem)\s+"([^"]+)"',
                              text, re.M))
    used = set(re.findall(r'"([^"]+)"', text))
    literals = set(re.findall(r"'([^']*)'", text))
    for name in sorted(used - declared):
        if name in CQL_KEYWORDS or name in literals or name in CODESYSTEM_URLS:
            continue
        out.append(dsl.Issue(name, "UNDECLARED_IDENTIFIER",
                             f'"{name}" is used but never declared',
                             "add a define, valueset or code declaration for it"))
    # A define whose body is on the following line is normal CQL; only flag one
    # with genuinely nothing after the colon.
    for m in re.finditer(r'define\s+("[^"]+")\s*:[^\S\n]*\n\s*\n', text):
        out.append(dsl.Issue(m.group(1), "EMPTY_DEFINE",
                             f"{m.group(1)} has no body"))
    return out

def compile_cql(text: str,
                compiler: Callable[[str], list[str]] | None = None) -> list[dsl.Issue]:
    """Structural checks, plus the real translator when one is injected.

    `compiler(text) -> [error strings]` is the seam for the official Java
    cql-to-elm translator (DEV_PLAN P6). It is injected rather than imported so
    the whole pipeline, and every test, runs without a JVM -- and so the
    ablation "what does the compiler gate actually buy" is a one-argument
    change rather than a rewrite."""
    issues = check_cql(text)
    if compiler is None:
        return issues
    try:
        for msg in compiler(text) or []:
            issues.append(dsl.Issue("compiler", "CQL_COMPILE_ERROR", msg))
    except Exception as exc:
        issues.append(dsl.Issue("compiler", "COMPILER_UNAVAILABLE",
                                f"translator did not run: {exc}",
                                "structural checks only; this is NOT a compile pass",
                                severity="warn"))
    return issues

# ---------------------------------------------------------------- repair loop

@dataclass
class Synthesis:
    """What stage 4 produces: an artifact, or an honest refusal."""
    statement_id: str
    rule: dsl.Rule | None = None
    library: CqlLibrary | None = None
    refusal: dsl.Refusal | None = None
    attempts: int = 0
    repairs: tuple[str, ...] = ()          # what each round was told to fix
    issues: tuple[dsl.Issue, ...] = ()

    @property
    def ok(self) -> bool:
        return self.library is not None and self.refusal is None

def synthesize(statement: str, reg: dsl.Registry,
               formalize: Callable[[str, dsl.Registry], dict],
               *, statement_id: str = "s", compiler: Callable | None = None,
               max_repairs: int = 2, source_page: int | None = None,
               source_span: str | None = None) -> Synthesis:
    """Formalize -> validate -> lower -> compile, repairing on failure.

    BOUNDED, then refuse. An unbounded repair loop converges on whatever
    happens to compile, which is precisely the failure this project argues
    against: self-correction is unreliable (arXiv:2502.12065), so the loop is
    given a small budget and falls through to a refusal rather than grinding
    until something passes."""
    from dataclasses import replace as _replace
    feedback: list[str] = []
    last: list[dsl.Issue] = []

    for attempt in range(max_repairs + 1):
        prompt = statement if not feedback else (
            statement + "\n\nThe previous attempt was rejected:\n"
            + "\n".join(f"  - {f}" for f in feedback)
            + "\nFix exactly these problems. If they cannot be fixed without "
              "inventing something the source does not state, refuse instead.")
        try:
            obj = dsl.from_decoded(formalize(prompt, reg))
        except Exception as exc:
            return Synthesis(statement_id, attempts=attempt + 1,
                             repairs=tuple(feedback),
                             refusal=dsl.Refusal(statement_id, "beyond_dsl",
                                                 f"encoding failed: {exc}"))
        if isinstance(obj, dsl.Refusal):
            return Synthesis(statement_id, attempts=attempt + 1,
                             repairs=tuple(feedback), refusal=obj)

        rule = _replace(obj, rule_id=statement_id, source_page=source_page,
                        source_span=source_span or obj.source_span)
        issues = dsl.validate_rule(rule, reg)
        if not [i for i in issues if i.severity == "error"]:
            issues = issues + dsl.check_nondegenerate(rule, reg)
        errors = [i for i in issues if i.severity == "error"]

        if not errors:
            lib = rule_to_cql(rule, reg, library_name=_slug(statement_id))
            cerrors = [i for i in compile_cql(lib.text, compiler)
                       if i.severity == "error"]
            if not cerrors:
                return Synthesis(statement_id, rule=rule, library=lib,
                                 attempts=attempt + 1, repairs=tuple(feedback),
                                 issues=tuple(issues))
            errors, last = cerrors, cerrors
        else:
            last = issues
        feedback = [f"{i.code}: {i.message}" + (f" ({i.hint})" if i.hint else "")
                    for i in errors]

    return Synthesis(statement_id, attempts=max_repairs + 1,
                     repairs=tuple(feedback), issues=tuple(last),
                     refusal=dsl.Refusal(
                         statement_id, "beyond_dsl",
                         f"no valid encoding after {max_repairs + 1} attempts: "
                         + "; ".join(i.code for i in last)))
