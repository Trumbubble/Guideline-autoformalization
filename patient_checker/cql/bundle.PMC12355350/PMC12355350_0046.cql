library Demopmc123553500046 version '1.0.000'

using FHIR version '4.0.1'
include FHIRHelpers version '4.0.1' called FHIRHelpers

codesystem "RxNorm": 'http://www.nlm.nih.gov/research/umls/rxnorm'
codesystem "SNOMED": 'http://snomed.info/sct'

valueset "SGLT2 Inhibitors codes": 'urn:oid:2.16.840.1.113762.1.4.1178.30' version '20260418'
valueset "Sglt2 Inhibitors codes": 'urn:oid:2.16.840.1.113762.1.4.1178.30' version '20260418'

code "Cardiovascular disease codes": '49601007' from "SNOMED" display 'Cardiovascular disease'
code "Chronic kidney disease codes": '709044004' from "SNOMED" display 'Chronic kidney disease'
code "Finerenone codes": '2562811' from "RxNorm"
code "Glucagon-Like Peptide 1 Receptor Agonist-Containing Product codes": '416636000' from "SNOMED"
code "Glucagon-like peptide 1 receptor agonist-containing product codes": '416636000' from "SNOMED" display 'Glucagon-like peptide 1 receptor agonist-containing product'
code "finerenone codes": '2562811' from "RxNorm" display 'finerenone'

context Patient

/* PMC12355350:0046
   action: recommend SGLT-2 inhibitors or GLP-1 receptor agonists
   strength: weak
   source: • Moderate risk (more than three cardiovascular risk factors without established CVD or CKD; or established CVD and/or CKD at lower risk of complications): weak recommendation in favour of SGLT-2 inhibitors or GLP-1 receptor agonists; and a weak recommendation against finerenone in adults with CKD.
   PARTIAL: 3 unbound predicate(s) left null: has_cardiovascular_risk_factors, has_lower_risk_complications, has_more_than_three_cardiovascular_risk
*/

// UNBOUND: the source states this predicate but does not define it.
// Left null so CQL's three-valued logic propagates the uncertainty.
define "cardiovascular risk factors":
  null as Boolean
define "Chronic kidney disease":
  exists ([Condition: "Chronic kidney disease codes"])
define "Cardiovascular disease":
  exists ([Condition: "Cardiovascular disease codes"])
// UNBOUND: the source states this predicate but does not define it.
// Left null so CQL's three-valued logic propagates the uncertainty.
define "at lower risk of complications":
  null as Boolean
// UNBOUND: the source states this predicate but does not define it.
// Left null so CQL's three-valued logic propagates the uncertainty.
define "more than three cardiovascular risk factors":
  null as Boolean
define "finerenone":
  exists ([MedicationRequest: "finerenone codes"])
define "Glucagon-like peptide 1 receptor agonist-containing product":
  exists ([MedicationRequest: "Glucagon-like peptide 1 receptor agonist-containing product codes"])
define "SGLT2 Inhibitors":
  exists ([MedicationRequest: "SGLT2 Inhibitors codes"])

define "Applicable":
  ("more than three cardiovascular risk factors"
  or "at lower risk of complications")

define "Satisfied":
  ("SGLT2 Inhibitors"
  or "Glucagon-like peptide 1 receptor agonist-containing product")

define "Compliant":
  not "Applicable" or "Satisfied"

define "Deontic":
  'obligation'

define "Violated":
  "Applicable" and not "Satisfied"
