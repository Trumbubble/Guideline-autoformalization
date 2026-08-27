"""Track B fixture library: a Registry + ten dsl.Rules + a SpecClause view
over each. See CHECKER.md for what this is, why it goes through the DSL
instead of raw Z3, and what each field means."""

import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "Part A"))

import dsl  # noqa: E402
from dsl import And, Between, Cmp, EventDef, Or, Registry, Rule, Since, VarDef  # noqa: E402

# ---------------------------------------------------------------- registry

REGISTRY = Registry(
    [
        VarDef("ace_inhibitor_on", "bool", None, "prescribed an ACE inhibitor",
               role="action"),
        VarDef("potassium", "real", "mmol/L", "serum potassium",
               binding="LOINC:2823-3", plausible=(1.5, 9.0)),
        VarDef("apixaban_on", "bool", None, "prescribed apixaban", role="action"),
        VarDef("apixaban_dose_reduced", "bool", None,
               "apixaban prescribed at the reduced 2.5 mg twice-daily dose",
               role="action"),
        VarDef("patient_age_years", "real", "years", "patient age",
               plausible=(0, 120)),
        VarDef("patient_weight_kg", "real", "kg", "patient body weight",
               plausible=(20, 300)),
        VarDef("serum_creatinine_mg_dl", "real", "mg/dL", "serum creatinine",
               binding="LOINC:2160-0", plausible=(0.1, 20)),
        VarDef("rivaroxaban_on", "bool", None, "prescribed rivaroxaban",
               role="action"),
        VarDef("rivaroxaban_dose_reduced", "bool", None,
               "rivaroxaban prescribed at the reduced dose", role="action"),
        # Shared by rivaroxaban and digoxin -- a patient has one kidney,
        # not one creatinine clearance per drug.
        VarDef("creatinine_clearance_ml_min", "real", "mL/min",
               "creatinine clearance", plausible=(0, 200)),
        VarDef("warfarin_on", "bool", None, "prescribed warfarin", role="action"),
        VarDef("nsaid_on", "bool", None, "prescribed an NSAID", role="action"),
        VarDef("clarithromycin_on", "bool", None, "prescribed clarithromycin",
               role="action"),
        VarDef("simvastatin_on", "bool", None, "prescribed simvastatin",
               role="action"),
        VarDef("nonselective_beta_blocker_on", "bool", None,
               "prescribed a non-selective beta-blocker", role="action"),
        VarDef("asthma_diagnosis", "bool", None, "diagnosis of asthma"),
        VarDef("digoxin_on", "bool", None, "prescribed digoxin", role="action"),
        VarDef("digoxin_dose_reduced", "bool", None,
               "digoxin prescribed at a reduced dose", role="action"),
    ],
    events=[
        # Shared by the ACE inhibitor and digoxin monitoring rules -- one
        # most-recent potassium check per patient, not one per drug.
        EventDef("potassium_check", "serum potassium measured",
                  "LOINC:2823-3"),
        EventDef("inr_check", "INR measured", "LOINC:6301-6"),
    ],
)

# ---------------------------------------------------------------- rules
#
# Fixture provenance is hand-authored, not extracted from a real document --
# source_page is a nominal placeholder, required by validate_rule but not
# meaningful here.
_PAGE = 1

_ace_hyperkalemia_trigger = Cmp("potassium", ">", 5.0)
_apixaban_age = Cmp("patient_age_years", ">=", 80)
_apixaban_weight = Cmp("patient_weight_kg", "<=", 60)
_apixaban_creatinine = Cmp("serum_creatinine_mg_dl", ">=", 1.5)
_rivaroxaban_contraindication_trigger = Cmp("creatinine_clearance_ml_min", "<", 15)
_rivaroxaban_dose_tier = Between(
    "creatinine_clearance_ml_min", 15, 50, lo_inclusive=True, hi_inclusive=False
)
_digoxin_renal_trigger = Cmp("creatinine_clearance_ml_min", "<", 60)

RULES = [
    Rule(
        rule_id="ace_inhibitor_hyperkalemia_contraindication",
        # Contrapositive of "if on ACEi, potassium must be <= 5.0": the
        # action atom (ace_inhibitor_on) has to live in `then`, since that's
        # the only thing a care plan decides.
        when=_ace_hyperkalemia_trigger,
        then=Cmp("ace_inhibitor_on", "==", False),
        action="avoid", target="ACE inhibitor",
        source_page=_PAGE,
        source_span=(
            "An ACE inhibitor should not be started in a patient whose "
            "serum potassium is above 5.0 mmol/L."
        ),
    ),
    Rule(
        rule_id="ace_inhibitor_potassium_monitoring",
        when=Cmp("ace_inhibitor_on", "==", True),
        then=Since("potassium_check", 14),
        action="monitor", target="serum potassium",
        source_page=_PAGE,
        source_span=(
            "Serum potassium and renal function should be checked "
            "within 14 days of starting or increasing the dose of an "
            "ACE inhibitor."
        ),
    ),
    Rule(
        rule_id="apixaban_renal_dose_reduction",
        # >= 2 of 3 criteria, written as an explicit Or of pairwise Ands --
        # not a cardinality shortcut. P3's own lesson: an AND-for-OR slip
        # here is exactly the failure mode that typechecks and is wrong.
        when=And([
            Cmp("apixaban_on", "==", True),
            Or([
                And([_apixaban_age, _apixaban_weight]),
                And([_apixaban_age, _apixaban_creatinine]),
                And([_apixaban_weight, _apixaban_creatinine]),
            ]),
        ]),
        then=Cmp("apixaban_dose_reduced", "==", True),
        action="recommend", target="reduced apixaban dose (2.5 mg twice daily)",
        source_page=_PAGE,
        source_span=(
            "Reduce the apixaban dose to 2.5 mg twice daily if at least "
            "two of the following apply: age 80 years or older, body "
            "weight 60 kg or less, or serum creatinine 1.5 mg/dL or "
            "greater."
        ),
    ),
    Rule(
        rule_id="rivaroxaban_contraindication",
        when=_rivaroxaban_contraindication_trigger,
        then=Cmp("rivaroxaban_on", "==", False),
        action="avoid", target="rivaroxaban",
        source_page=_PAGE,
        source_span=(
            "Rivaroxaban should be avoided if creatinine clearance is "
            "below 15 mL/min."
        ),
    ),
    Rule(
        rule_id="rivaroxaban_dose_reduction",
        # Between forces the inclusivity decision the original prose left
        # implicit -- see differential_check.py for what happens when a
        # second reading picks the other one.
        when=And([Cmp("rivaroxaban_on", "==", True), _rivaroxaban_dose_tier]),
        then=Cmp("rivaroxaban_dose_reduced", "==", True),
        action="recommend", target="reduced rivaroxaban dose",
        source_page=_PAGE,
        source_span=(
            "A reduced dose of rivaroxaban should be used if creatinine "
            "clearance is 15 mL/min or above but below 50 mL/min."
        ),
    ),
    Rule(
        rule_id="warfarin_nsaid_bleeding_interaction",
        when=And([Cmp("warfarin_on", "==", True), Cmp("nsaid_on", "==", True)]),
        then=Since("inr_check", 7),
        action="monitor", target="INR",
        source_page=_PAGE,
        source_span=(
            "Concurrent use of an NSAID with warfarin increases bleeding "
            "risk and should be avoided; where unavoidable, INR should "
            "be monitored at least weekly."
        ),
    ),
    Rule(
        rule_id="clarithromycin_simvastatin_contraindication",
        when=Cmp("clarithromycin_on", "==", True),
        then=Cmp("simvastatin_on", "==", False),
        action="avoid", target="simvastatin",
        source_page=_PAGE,
        source_span=(
            "Concomitant use of clarithromycin, a strong CYP3A4 "
            "inhibitor, with simvastatin is contraindicated because of "
            "increased risk of myopathy and rhabdomyolysis."
        ),
    ),
    Rule(
        rule_id="nonselective_beta_blocker_asthma_contraindication",
        when=Cmp("asthma_diagnosis", "==", True),
        then=Cmp("nonselective_beta_blocker_on", "==", False),
        action="avoid", target="non-selective beta-blocker",
        source_page=_PAGE,
        source_span=(
            "Non-selective beta-blockers are contraindicated in "
            "patients with a diagnosis of asthma because of the risk of "
            "bronchospasm."
        ),
    ),
    Rule(
        rule_id="digoxin_renal_dose_reduction",
        when=And([Cmp("digoxin_on", "==", True), _digoxin_renal_trigger]),
        then=Cmp("digoxin_dose_reduced", "==", True),
        action="recommend", target="reduced digoxin dose",
        source_page=_PAGE,
        source_span=(
            "Digoxin dose should be reduced in patients with a "
            "creatinine clearance below 60 mL/min."
        ),
    ),
    Rule(
        rule_id="digoxin_potassium_monitoring",
        when=Cmp("digoxin_on", "==", True),
        then=Since("potassium_check", 30),
        action="monitor", target="serum potassium",
        source_page=_PAGE,
        source_span=(
            "Serum potassium should be monitored within 30 days in "
            "patients prescribed digoxin, given the increased risk of "
            "toxicity with hypokalemia."
        ),
    ),
]

# Fail loudly at import time rather than shipping an unvalidated rule.
for _rule in RULES:
    _issues = dsl.validate_rule(_rule, REGISTRY) + dsl.check_nondegenerate(_rule, REGISTRY)
    if _issues:
        raise AssertionError(
            f"{_rule.rule_id} failed DSL validation: "
            + "; ".join(f"{i.code}: {i.message}" for i in _issues)
        )

RULES_BY_ID = {rule.rule_id: rule for rule in RULES}

ENV = dsl.make_env(REGISTRY)
DOMAIN_AXIOMS = dsl.domain_axioms(REGISTRY, ENV)

# ---------------------------------------------------------------- Track B view

CATEGORY_BY_RULE_ID = {
    "ace_inhibitor_hyperkalemia_contraindication": "contraindication",
    "ace_inhibitor_potassium_monitoring": "monitoring",
    "apixaban_renal_dose_reduction": "dose_adjustment",
    "rivaroxaban_contraindication": "contraindication",
    "rivaroxaban_dose_reduction": "dose_adjustment",
    "warfarin_nsaid_bleeding_interaction": "drug_interaction",
    "clarithromycin_simvastatin_contraindication": "drug_interaction",
    "nonselective_beta_blocker_asthma_contraindication": "contraindication",
    "digoxin_renal_dose_reduction": "dose_adjustment",
    "digoxin_potassium_monitoring": "monitoring",
}


@dataclass(frozen=True, eq=False)
class Boundary:
    """One numeric threshold inside a rule's predicate, ready to be tested."""

    variable: object  # the Z3 Real/Int being constrained
    description: str
    operator: str  # "<=" | "<" | ">=" | ">", the rule's compliance direction
    threshold: float
    step: float  # smallest realistic increment for this variable's unit
    context: tuple = field(default_factory=tuple)  # extra constraints that isolate this boundary


@dataclass(frozen=True, eq=False)
class SpecClause:
    """A Track-B-shaped view over one validated dsl.Rule."""

    rule_id: str
    title: str
    category: str
    drug: str
    predicate: object  # z3 BoolRef = dsl.rule_to_z3(rule, ENV)
    threshold: tuple = field(default_factory=tuple)
    window_days: int = 0
    source: str = ""
    source_sentence: str = ""
    variables: tuple = field(default_factory=tuple)
    var_refs: dict = field(default_factory=dict)
    boundaries: tuple = field(default_factory=tuple)


def _vars_and_events(expr) -> tuple:
    variables, events = set(), set()

    def walk(e):
        if isinstance(e, (Cmp, Between)):
            variables.add(e.var)
        elif isinstance(e, Since):
            events.add(e.event)
        elif isinstance(e, dsl.Not):
            walk(e.operand)
        elif isinstance(e, (And, Or)):
            for operand in e.operands:
                walk(operand)

    walk(expr)
    return variables, events


def _var_refs_for(rule: Rule) -> dict:
    when_vars, when_events = _vars_and_events(rule.when)
    then_vars, then_events = _vars_and_events(rule.then)
    refs = {name: ENV[name] for name in when_vars | then_vars}
    for event in when_events | then_events:
        refs[dsl.occurred_var(event)] = ENV[dsl.occurred_var(event)]
        refs[dsl.days_since_var(event)] = ENV[dsl.days_since_var(event)]
    return refs


# Boundary metadata: the compliance-direction operator/threshold for each
# rule, hand-derived (contraposition, De Morgan) from the Rule's actual
# `when`/`then` above -- see the inline comment on each for the derivation.
# `context` pins every other free variable so the boundary's effect is
# isolated; where a rule's obligation is a `Since`, the occurrence flag
# must be pinned True in context too, or the day-count boundary is moot.
_BOUNDARIES_BY_RULE_ID = {
    # phi = Implies(K>5.0, not on). At K=5.0 the antecedent is false so phi
    # is vacuously true regardless of `on`; compliance flips to depending on
    # `on` only once K>5.0. With on=True pinned: compliant iff K<=5.0.
    "ace_inhibitor_hyperkalemia_contraindication": (
        Boundary(
            variable=ENV["potassium"],
            description="serum potassium (mmol/L)",
            operator="<=",
            threshold=_ace_hyperkalemia_trigger.value,
            step=0.1,
            context=(ENV["ace_inhibitor_on"] == True,),
        ),
    ),
    "ace_inhibitor_potassium_monitoring": (
        Boundary(
            variable=ENV[dsl.days_since_var("potassium_check")],
            description="days since last potassium check",
            operator="<=",
            threshold=14,
            step=1,
            context=(
                ENV["ace_inhibitor_on"] == True,
                ENV[dsl.occurred_var("potassium_check")] == True,
            ),
        ),
    ),
    # Each criterion's boundary is tested with the other two pinned so that
    # exactly one of them is already met -- the criterion under test is
    # then the deciding vote across the 2-of-3 gate.
    "apixaban_renal_dose_reduction": (
        Boundary(
            variable=ENV["patient_age_years"],
            description="patient age (years) -- deciding vote, weight met",
            operator=">=",
            threshold=_apixaban_age.value,
            step=1,
            context=(
                ENV["apixaban_on"] == True,
                ENV["patient_weight_kg"] == 55,
                ENV["serum_creatinine_mg_dl"] == 1.0,
            ),
        ),
        Boundary(
            variable=ENV["patient_weight_kg"],
            description="patient body weight (kg) -- deciding vote, creatinine met",
            operator="<=",
            threshold=_apixaban_weight.value,
            step=1,
            context=(
                ENV["apixaban_on"] == True,
                ENV["patient_age_years"] == 70,
                ENV["serum_creatinine_mg_dl"] == 2.0,
            ),
        ),
        Boundary(
            variable=ENV["serum_creatinine_mg_dl"],
            description="serum creatinine (mg/dL) -- deciding vote, age met",
            operator=">=",
            threshold=_apixaban_creatinine.value,
            step=0.1,
            context=(
                ENV["apixaban_on"] == True,
                ENV["patient_age_years"] == 85,
                ENV["patient_weight_kg"] == 70,
            ),
        ),
    ),
    "rivaroxaban_contraindication": (
        Boundary(
            variable=ENV["creatinine_clearance_ml_min"],
            description="creatinine clearance (mL/min) -- absolute contraindication",
            operator=">=",
            threshold=_rivaroxaban_contraindication_trigger.value,
            step=1,
            context=(ENV["rivaroxaban_on"] == True,),
        ),
    ),
    "rivaroxaban_dose_reduction": (
        Boundary(
            variable=ENV["creatinine_clearance_ml_min"],
            description="creatinine clearance (mL/min) -- dose-reduction tier, dose not reduced",
            operator=">=",
            threshold=_rivaroxaban_dose_tier.hi,
            step=1,
            context=(
                ENV["rivaroxaban_on"] == True,
                ENV["rivaroxaban_dose_reduced"] == False,
            ),
        ),
    ),
    "warfarin_nsaid_bleeding_interaction": (
        Boundary(
            variable=ENV[dsl.days_since_var("inr_check")],
            description="days since last INR check",
            operator="<=",
            threshold=7,
            step=1,
            context=(
                ENV["warfarin_on"] == True,
                ENV["nsaid_on"] == True,
                ENV[dsl.occurred_var("inr_check")] == True,
            ),
        ),
    ),
    "digoxin_renal_dose_reduction": (
        Boundary(
            variable=ENV["creatinine_clearance_ml_min"],
            description="creatinine clearance (mL/min) -- dose-reduction gate",
            operator=">=",
            threshold=_digoxin_renal_trigger.value,
            step=1,
            context=(ENV["digoxin_on"] == True,),
        ),
    ),
    "digoxin_potassium_monitoring": (
        Boundary(
            variable=ENV[dsl.days_since_var("potassium_check")],
            description="days since last potassium check",
            operator="<=",
            threshold=30,
            step=1,
            context=(
                ENV["digoxin_on"] == True,
                ENV[dsl.occurred_var("potassium_check")] == True,
            ),
        ),
    ),
}


def _build_spec_clauses() -> list:
    clauses = []
    for rule in RULES:
        clauses.append(
            SpecClause(
                rule_id=rule.rule_id,
                title=f"{rule.action}: {rule.target}",
                category=CATEGORY_BY_RULE_ID[rule.rule_id],
                drug=rule.target,
                predicate=dsl.rule_to_z3(rule, ENV),
                source=f"hand-authored fixture, DSL rule {rule.rule_id!r}",
                source_sentence=rule.source_span,
                var_refs=_var_refs_for(rule),
                variables=tuple(sorted(_var_refs_for(rule))),
                boundaries=_BOUNDARIES_BY_RULE_ID.get(rule.rule_id, ()),
            )
        )
    return clauses


REAL_RULES = _build_spec_clauses()


if __name__ == "__main__":
    from z3 import Not, Solver, sat

    print(f"{len(RULES)} rules, all clean under validate_rule + check_nondegenerate\n")
    for clause in REAL_RULES:
        compliant = Solver()
        compliant.add(*DOMAIN_AXIOMS, clause.predicate)
        violating = Solver()
        violating.add(*DOMAIN_AXIOMS, Not(clause.predicate))
        compliant_ok = compliant.check() == sat
        violating_ok = violating.check() == sat
        status = "OK" if compliant_ok and violating_ok else "DEGENERATE"
        print(f"{clause.rule_id:50s} [{clause.category:16s}] {status}")
        print(f"    {clause.source_sentence}")
