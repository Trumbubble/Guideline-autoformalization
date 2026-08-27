"""Hand-authored library of real guideline rules for Track B.

Each ``SpecClause`` below is a small, typed, self-contained fixture: a Z3
predicate over realistically-named variables, the numeric threshold(s) the
predicate turns on, and the guideline sentence it was authored from. These
are written by hand so Track B has real material to test against without
waiting on a document-extraction pipeline. `partial_spec_check.py` has the
original metformin/eGFR example this library extends beyond.

The ``source_sentence`` fields are representative paraphrases of well
established prescribing guidance (BNF, FDA labeling, NICE/ESC guidelines),
not verbatim extracted quotes -- treat them as fixtures to exercise the
verifier, not as a substitute for checking the primary source.

Each numeric threshold in a rule is also recorded as a ``Boundary``: the Z3
variable it constrains, the comparison operator, the threshold itself, a
step size at the variable's realistic reporting precision, and any extra
constraints ("context") needed to isolate that one threshold's effect from
the rest of the predicate. `synthesize_boundary_tests.py` walks these
boundaries and asks Z3 to solve for concrete patient bundles on both sides
of each one, instead of hand-picking test numbers.
"""

from dataclasses import dataclass, field

from z3 import And, Bool, BoolRef, If, Implies, Int, Not, Real, Sum


@dataclass(frozen=True, eq=False)
class Boundary:
    """One numeric threshold inside a rule's predicate, ready to be tested."""

    variable: object  # the Z3 Real/Int being constrained
    description: str
    operator: str  # "<=" | "<" | ">=" | ">", as used in the predicate
    threshold: float
    step: float  # smallest realistic increment for this variable's unit
    context: tuple = field(default_factory=tuple)  # extra constraints that isolate this boundary


@dataclass(frozen=True, eq=False)
class SpecClause:
    """One typed guideline rule, ready to hand to a Z3 solver."""

    rule_id: str
    title: str
    category: str  # contraindication | monitoring | dose_adjustment | drug_interaction
    drug: str
    predicate: BoolRef
    threshold: tuple = field(default_factory=tuple)
    window_days: int = 0
    source: str = ""
    source_sentence: str = ""
    variables: tuple = field(default_factory=tuple)
    var_refs: dict = field(default_factory=dict)
    boundaries: tuple = field(default_factory=tuple)


def _build_rules() -> list:
    rules = []

    # -- ACE inhibitor: hyperkalemia at initiation -----------------------
    ace_inhibitor_on = Bool("ace_inhibitor_on")
    potassium = Real("serum_potassium_mmol_l")
    rules.append(
        SpecClause(
            rule_id="ace_inhibitor_hyperkalemia_contraindication",
            title="ACE inhibitor contraindicated with pre-existing hyperkalemia",
            category="contraindication",
            drug="ace_inhibitor",
            predicate=Implies(ace_inhibitor_on, potassium <= 5.0),
            threshold=(5.0,),
            source="BNF / NICE hypertension guidance",
            source_sentence=(
                "An ACE inhibitor should not be started in a patient whose "
                "serum potassium is above 5.0 mmol/L."
            ),
            variables=("ace_inhibitor_on", "serum_potassium_mmol_l"),
            var_refs={
                "ace_inhibitor_on": ace_inhibitor_on,
                "serum_potassium_mmol_l": potassium,
            },
            boundaries=(
                Boundary(
                    variable=potassium,
                    description="serum potassium (mmol/L)",
                    operator="<=",
                    threshold=5.0,
                    step=0.1,
                    context=(ace_inhibitor_on == True,),
                ),
            ),
        )
    )

    # -- ACE inhibitor: post-initiation monitoring window -----------------
    days_since_ace_inhibitor_potassium_check = Int(
        "days_since_ace_inhibitor_potassium_check"
    )
    rules.append(
        SpecClause(
            rule_id="ace_inhibitor_potassium_monitoring",
            title="Potassium and renal function recheck after ACE inhibitor start",
            category="monitoring",
            drug="ace_inhibitor",
            predicate=Implies(
                ace_inhibitor_on,
                days_since_ace_inhibitor_potassium_check <= 14,
            ),
            threshold=(14.0,),
            window_days=14,
            source="NICE NG203 (chronic kidney disease)",
            source_sentence=(
                "Serum potassium and renal function should be checked "
                "within 14 days of starting or increasing the dose of an "
                "ACE inhibitor."
            ),
            variables=(
                "ace_inhibitor_on",
                "days_since_ace_inhibitor_potassium_check",
            ),
            var_refs={
                "ace_inhibitor_on": ace_inhibitor_on,
                "days_since_ace_inhibitor_potassium_check": (
                    days_since_ace_inhibitor_potassium_check
                ),
            },
            boundaries=(
                Boundary(
                    variable=days_since_ace_inhibitor_potassium_check,
                    description="days since last potassium check",
                    operator="<=",
                    threshold=14,
                    step=1,
                    context=(ace_inhibitor_on == True,),
                ),
            ),
        )
    )

    # -- Apixaban: multi-criteria dose reduction by renal function --------
    apixaban_on = Bool("apixaban_on")
    apixaban_dose_reduced = Bool("apixaban_dose_reduced")
    patient_age_years = Real("patient_age_years")
    patient_weight_kg = Real("patient_weight_kg")
    serum_creatinine = Real("serum_creatinine_mg_dl")
    age_criterion = patient_age_years >= 80
    weight_criterion = patient_weight_kg <= 60
    creatinine_criterion = serum_creatinine >= 1.5
    criteria_met = Sum(
        If(age_criterion, 1, 0),
        If(weight_criterion, 1, 0),
        If(creatinine_criterion, 1, 0),
    )
    rules.append(
        SpecClause(
            rule_id="apixaban_renal_dose_reduction",
            title="Apixaban dose reduction: 2-of-3 criteria",
            category="dose_adjustment",
            drug="apixaban",
            predicate=Implies(
                And(apixaban_on, criteria_met >= 2), apixaban_dose_reduced
            ),
            threshold=(80.0, 60.0, 1.5),
            source="FDA prescribing information (apixaban)",
            source_sentence=(
                "Reduce the apixaban dose to 2.5 mg twice daily if at least "
                "two of the following apply: age 80 years or older, body "
                "weight 60 kg or less, or serum creatinine 1.5 mg/dL or "
                "greater."
            ),
            variables=(
                "apixaban_on",
                "apixaban_dose_reduced",
                "patient_age_years",
                "patient_weight_kg",
                "serum_creatinine_mg_dl",
            ),
            var_refs={
                "apixaban_on": apixaban_on,
                "apixaban_dose_reduced": apixaban_dose_reduced,
                "patient_age_years": patient_age_years,
                "patient_weight_kg": patient_weight_kg,
                "serum_creatinine_mg_dl": serum_creatinine,
            },
            # Each criterion's boundary is tested with the other two pinned
            # so that exactly one of them is already met -- the criterion
            # under test is then the deciding vote across the 2-of-3 gate.
            boundaries=(
                Boundary(
                    variable=patient_age_years,
                    description="patient age (years) -- deciding vote, weight met",
                    operator=">=",
                    threshold=80,
                    step=1,
                    context=(
                        apixaban_on == True,
                        patient_weight_kg == 55,
                        serum_creatinine == 1.0,
                    ),
                ),
                Boundary(
                    variable=patient_weight_kg,
                    description="patient body weight (kg) -- deciding vote, creatinine met",
                    operator="<=",
                    threshold=60,
                    step=1,
                    context=(
                        apixaban_on == True,
                        patient_age_years == 70,
                        serum_creatinine == 2.0,
                    ),
                ),
                Boundary(
                    variable=serum_creatinine,
                    description="serum creatinine (mg/dL) -- deciding vote, age met",
                    operator=">=",
                    threshold=1.5,
                    step=0.1,
                    context=(
                        apixaban_on == True,
                        patient_age_years == 85,
                        patient_weight_kg == 70,
                    ),
                ),
            ),
        )
    )

    # -- Rivaroxaban: tiered dosing / contraindication by CrCl ------------
    rivaroxaban_on = Bool("rivaroxaban_on")
    rivaroxaban_dose_reduced = Bool("rivaroxaban_dose_reduced")
    creatinine_clearance = Real("creatinine_clearance_ml_min")
    rules.append(
        SpecClause(
            rule_id="rivaroxaban_renal_tiering",
            title="Rivaroxaban avoided or dose-reduced by creatinine clearance",
            category="dose_adjustment",
            drug="rivaroxaban",
            predicate=And(
                Implies(rivaroxaban_on, creatinine_clearance >= 15),
                Implies(
                    And(rivaroxaban_on, creatinine_clearance < 50),
                    rivaroxaban_dose_reduced,
                ),
            ),
            threshold=(15.0, 50.0),
            source="FDA prescribing information (rivaroxaban)",
            source_sentence=(
                "Rivaroxaban should be avoided if creatinine clearance is "
                "below 15 mL/min; a reduced dose should be used if "
                "creatinine clearance is between 15 and 50 mL/min."
            ),
            variables=(
                "rivaroxaban_on",
                "rivaroxaban_dose_reduced",
                "creatinine_clearance_ml_min",
            ),
            var_refs={
                "rivaroxaban_on": rivaroxaban_on,
                "rivaroxaban_dose_reduced": rivaroxaban_dose_reduced,
                "creatinine_clearance_ml_min": creatinine_clearance,
            },
            boundaries=(
                Boundary(
                    variable=creatinine_clearance,
                    description="creatinine clearance (mL/min) -- absolute contraindication",
                    operator=">=",
                    threshold=15,
                    step=1,
                    context=(rivaroxaban_on == True,),
                ),
                Boundary(
                    variable=creatinine_clearance,
                    description="creatinine clearance (mL/min) -- dose-reduction tier, dose not reduced",
                    operator=">=",
                    threshold=50,
                    step=1,
                    context=(
                        rivaroxaban_on == True,
                        rivaroxaban_dose_reduced == False,
                    ),
                ),
            ),
        )
    )

    # -- Warfarin + NSAID: bleeding-risk interaction -----------------------
    warfarin_on = Bool("warfarin_on")
    nsaid_on = Bool("nsaid_on")
    inr_monitoring_frequency_days = Real("inr_monitoring_frequency_days")
    rules.append(
        SpecClause(
            rule_id="warfarin_nsaid_bleeding_interaction",
            title="Warfarin + NSAID requires tighter INR monitoring",
            category="drug_interaction",
            drug="warfarin",
            predicate=Implies(
                And(warfarin_on, nsaid_on), inr_monitoring_frequency_days <= 7
            ),
            threshold=(7.0,),
            source="BNF interactions guidance",
            source_sentence=(
                "Concurrent use of an NSAID with warfarin increases bleeding "
                "risk and should be avoided; where unavoidable, INR should "
                "be monitored at least weekly."
            ),
            variables=("warfarin_on", "nsaid_on", "inr_monitoring_frequency_days"),
            var_refs={
                "warfarin_on": warfarin_on,
                "nsaid_on": nsaid_on,
                "inr_monitoring_frequency_days": inr_monitoring_frequency_days,
            },
            boundaries=(
                Boundary(
                    variable=inr_monitoring_frequency_days,
                    description="INR monitoring frequency (days)",
                    operator="<=",
                    threshold=7,
                    step=1,
                    context=(warfarin_on == True, nsaid_on == True),
                ),
            ),
        )
    )

    # -- Clarithromycin + simvastatin: absolute contraindication ----------
    clarithromycin_on = Bool("clarithromycin_on")
    simvastatin_on = Bool("simvastatin_on")
    rules.append(
        SpecClause(
            rule_id="clarithromycin_simvastatin_contraindication",
            title="Clarithromycin contraindicated with simvastatin",
            category="drug_interaction",
            drug="simvastatin",
            predicate=Not(And(clarithromycin_on, simvastatin_on)),
            source="FDA prescribing information (simvastatin)",
            source_sentence=(
                "Concomitant use of clarithromycin, a strong CYP3A4 "
                "inhibitor, with simvastatin is contraindicated because of "
                "increased risk of myopathy and rhabdomyolysis."
            ),
            variables=("clarithromycin_on", "simvastatin_on"),
            var_refs={
                "clarithromycin_on": clarithromycin_on,
                "simvastatin_on": simvastatin_on,
            },
            # Purely boolean -- no numeric threshold to boundary-test.
        )
    )

    # -- Non-selective beta-blocker in asthma: contraindication -----------
    nonselective_beta_blocker_on = Bool("nonselective_beta_blocker_on")
    asthma_diagnosis = Bool("asthma_diagnosis")
    rules.append(
        SpecClause(
            rule_id="nonselective_beta_blocker_asthma_contraindication",
            title="Non-selective beta-blocker contraindicated in asthma",
            category="contraindication",
            drug="nonselective_beta_blocker",
            predicate=Implies(asthma_diagnosis, Not(nonselective_beta_blocker_on)),
            source="BNF respiratory contraindications",
            source_sentence=(
                "Non-selective beta-blockers are contraindicated in "
                "patients with a diagnosis of asthma because of the risk of "
                "bronchospasm."
            ),
            variables=("nonselective_beta_blocker_on", "asthma_diagnosis"),
            var_refs={
                "nonselective_beta_blocker_on": nonselective_beta_blocker_on,
                "asthma_diagnosis": asthma_diagnosis,
            },
            # Purely boolean -- no numeric threshold to boundary-test.
        )
    )

    # -- Digoxin: renal dose adjustment + potassium monitoring ------------
    digoxin_on = Bool("digoxin_on")
    digoxin_dose_reduced = Bool("digoxin_dose_reduced")
    digoxin_creatinine_clearance = Real("digoxin_creatinine_clearance_ml_min")
    days_since_digoxin_potassium_check = Int(
        "days_since_digoxin_potassium_check"
    )
    rules.append(
        SpecClause(
            rule_id="digoxin_renal_dose_and_monitoring",
            title="Digoxin dose reduction and potassium monitoring by renal function",
            category="dose_adjustment",
            drug="digoxin",
            predicate=And(
                Implies(
                    And(digoxin_on, digoxin_creatinine_clearance < 60),
                    digoxin_dose_reduced,
                ),
                Implies(
                    digoxin_on, days_since_digoxin_potassium_check <= 30
                ),
            ),
            threshold=(60.0, 30.0),
            window_days=30,
            source="BNF / NICE narrow-therapeutic-index drug guidance",
            source_sentence=(
                "Digoxin dose should be reduced in patients with a "
                "creatinine clearance below 60 mL/min, and serum potassium "
                "should be monitored within 30 days, given the increased "
                "risk of toxicity with hypokalemia."
            ),
            variables=(
                "digoxin_on",
                "digoxin_dose_reduced",
                "digoxin_creatinine_clearance_ml_min",
                "days_since_digoxin_potassium_check",
            ),
            var_refs={
                "digoxin_on": digoxin_on,
                "digoxin_dose_reduced": digoxin_dose_reduced,
                "digoxin_creatinine_clearance_ml_min": digoxin_creatinine_clearance,
                "days_since_digoxin_potassium_check": (
                    days_since_digoxin_potassium_check
                ),
            },
            boundaries=(
                Boundary(
                    variable=digoxin_creatinine_clearance,
                    description="creatinine clearance (mL/min) -- dose-reduction gate, monitoring already satisfied",
                    operator=">=",
                    threshold=60,
                    step=1,
                    context=(
                        digoxin_on == True,
                        days_since_digoxin_potassium_check == 0,
                    ),
                ),
                Boundary(
                    variable=days_since_digoxin_potassium_check,
                    description="days since last potassium check -- monitoring gate, renal function normal",
                    operator="<=",
                    threshold=30,
                    step=1,
                    context=(
                        digoxin_on == True,
                        digoxin_creatinine_clearance == 70,
                    ),
                ),
            ),
        )
    )

    return rules


REAL_RULES = _build_rules()


if __name__ == "__main__":
    from z3 import Solver, sat

    for rule in REAL_RULES:
        compliant = Solver()
        compliant.add(rule.predicate)
        violating = Solver()
        violating.add(Not(rule.predicate))
        compliant_ok = compliant.check() == sat
        violating_ok = violating.check() == sat
        status = "OK" if compliant_ok and violating_ok else "DEGENERATE"
        print(f"{rule.rule_id:50s} [{rule.category:16s}] {status}")
        print(f"    {rule.source_sentence}")
