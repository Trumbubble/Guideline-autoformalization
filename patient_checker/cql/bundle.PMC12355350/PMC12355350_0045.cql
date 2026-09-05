library Demopmc123553500045 version '1.0.000'

using FHIR version '4.0.1'
include FHIRHelpers version '4.0.1' called FHIRHelpers

codesystem "SNOMED": 'http://snomed.info/sct'

valueset "SGLT2 Inhibitors codes": 'urn:oid:2.16.840.1.113762.1.4.1178.30' version '20260418'
valueset "Sglt2 Inhibitors codes": 'urn:oid:2.16.840.1.113762.1.4.1178.30' version '20260418'

code "CVD - cardiovascular disease codes": '49601007' from "SNOMED" display 'CVD - cardiovascular disease'
code "Chronic kidney disease codes": '709044004' from "SNOMED" display 'Chronic kidney disease'
code "Glucagon-Like Peptide 1 Receptor Agonist-Containing Product codes": '416636000' from "SNOMED"
code "Glucagon-like peptide 1 receptor agonist-containing product codes": '416636000' from "SNOMED" display 'Glucagon-like peptide 1 receptor agonist-containing product'

context Patient

/* PMC12355350:0045
   action: avoid SGLT-2 inhibitors or GLP-1 receptor agonists
   strength: weak
   source: • Lower risk (three or fewer cardiovascular risk factors without established cardiovascular disease (CVD) or chronic kidney disease (CKD)): weak recommendation against SGLT-2 inhibitors or GLP-1 receptor agonists.
   PARTIAL: 2 unbound predicate(s) left null: has_cardiovascular_risk_factors, has_three_fewer_cardiovascular_risk_factors
*/

define "CVD - cardiovascular disease":
  exists ([Condition: "CVD - cardiovascular disease codes"])
// UNBOUND: the source states this predicate but does not define it.
// Left null so CQL's three-valued logic propagates the uncertainty.
define "cardiovascular risk factors":
  null as Boolean
define "Chronic kidney disease":
  exists ([Condition: "Chronic kidney disease codes"])
// UNBOUND: the source states this predicate but does not define it.
// Left null so CQL's three-valued logic propagates the uncertainty.
define "three or fewer cardiovascular risk factors":
  null as Boolean
define "Glucagon-like peptide 1 receptor agonist-containing product":
  exists ([MedicationRequest: "Glucagon-like peptide 1 receptor agonist-containing product codes"])
define "SGLT2 Inhibitors":
  exists ([MedicationRequest: "SGLT2 Inhibitors codes"])

define "Applicable":
  ("three or fewer cardiovascular risk factors"
  and not "CVD - cardiovascular disease"
  and not "Chronic kidney disease")

define "Satisfied":
  (not "SGLT2 Inhibitors"
  and not "Glucagon-like peptide 1 receptor agonist-containing product")

define "Compliant":
  not "Applicable" or "Satisfied"

define "Deontic":
  'obligation'

define "Violated":
  "Applicable" and not "Satisfied"
