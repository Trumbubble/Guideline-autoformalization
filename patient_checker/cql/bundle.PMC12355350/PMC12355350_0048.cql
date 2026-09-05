library Demopmc123553500048 version '1.0.000'

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
code "Heart failure codes": '84114007' from "SNOMED" display 'Heart failure'
code "finerenone codes": '2562811' from "RxNorm" display 'finerenone'

context Patient

/* PMC12355350:0048
   action: recommend SGLT-2 inhibitors or GLP-1 receptor agonists
   strength: strong recommendation in favour
   source: • Higher risk (established CVD and/or CKD at higher risk of complications, or established heart failure): strong recommendation in favour of SGLT-2 inhibitors or GLP-1 receptor agonists; and a weak recommendation in favour of finerenone in adults with CKD.
   PARTIAL: 2 unbound predicate(s) left null: has_higher_risk, has_higher_risk_complications
*/

define "Chronic kidney disease":
  exists ([Condition: "Chronic kidney disease codes"])
define "Cardiovascular disease":
  exists ([Condition: "Cardiovascular disease codes"])
define "Heart failure":
  exists ([Condition: "Heart failure codes"])
// UNBOUND: the source states this predicate but does not define it.
// Left null so CQL's three-valued logic propagates the uncertainty.
define "Higher risk":
  null as Boolean
// UNBOUND: the source states this predicate but does not define it.
// Left null so CQL's three-valued logic propagates the uncertainty.
define "at higher risk of complications":
  null as Boolean
define "finerenone":
  exists ([MedicationRequest: "finerenone codes"])
define "Glucagon-like peptide 1 receptor agonist-containing product":
  exists ([MedicationRequest: "Glucagon-like peptide 1 receptor agonist-containing product codes"])
define "SGLT2 Inhibitors":
  exists ([MedicationRequest: "SGLT2 Inhibitors codes"])

define "Applicable":
  "Higher risk"

define "Satisfied":
  ("SGLT2 Inhibitors"
  or "Glucagon-like peptide 1 receptor agonist-containing product")

define "Compliant":
  not "Applicable" or "Satisfied"

define "Deontic":
  'obligation'

define "Violated":
  "Applicable" and not "Satisfied"
