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
"""

from dataclasses import dataclass, field

from z3 import And, Bool, BoolRef, If, Implies, Not, Real, Sum


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
        )
    )

    # -- ACE inhibitor: post-initiation monitoring window -----------------
    potassium_checked = Bool("potassium_checked_within_14_days")
    rules.append(
        SpecClause(
            rule_id="ace_inhibitor_potassium_monitoring",
            title="Potassium and renal function recheck after ACE inhibitor start",
            category="monitoring",
            drug="ace_inhibitor",
            predicate=Implies(ace_inhibitor_on, potassium_checked),
            window_days=14,
            source="NICE NG203 (chronic kidney disease)",
            source_sentence=(
                "Serum potassium and renal function should be checked "
                "within 14 days of starting or increasing the dose of an "
                "ACE inhibitor."
            ),
            variables=("ace_inhibitor_on", "potassium_checked_within_14_days"),
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
        )
    )

    # -- Digoxin: renal dose adjustment + potassium monitoring ------------
    digoxin_on = Bool("digoxin_on")
    digoxin_dose_reduced = Bool("digoxin_dose_reduced")
    digoxin_creatinine_clearance = Real("digoxin_creatinine_clearance_ml_min")
    digoxin_potassium_checked = Bool("digoxin_potassium_checked_within_30_days")
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
                Implies(digoxin_on, digoxin_potassium_checked),
            ),
            threshold=(60.0,),
            window_days=30,
            source="BNF / NICE narrow-therapeutic-index drug guidance",
            source_sentence=(
                "Digoxin dose should be reduced in patients with a "
                "creatinine clearance below 60 mL/min, and serum potassium "
                "should be monitored, given the increased risk of toxicity "
                "with hypokalemia."
            ),
            variables=(
                "digoxin_on",
                "digoxin_dose_reduced",
                "digoxin_creatinine_clearance_ml_min",
                "digoxin_potassium_checked_within_30_days",
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
