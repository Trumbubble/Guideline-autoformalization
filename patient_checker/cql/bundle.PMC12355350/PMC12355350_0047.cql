library Demopmc123553500047 version '1.0.000'

using FHIR version '4.0.1'
include FHIRHelpers version '4.0.1' called FHIRHelpers

codesystem "RxNorm": 'http://www.nlm.nih.gov/research/umls/rxnorm'
codesystem "SNOMED": 'http://snomed.info/sct'

code "Chronic kidney disease codes": '709044004' from "SNOMED" display 'Chronic kidney disease'
code "Finerenone codes": '2562811' from "RxNorm"
code "finerenone codes": '2562811' from "RxNorm" display 'finerenone'

context Patient

/* PMC12355350:0047
   action: avoid finerenone
   strength: weak
   source: and a weak recommendation against finerenone in adults with CKD.
*/

define "Chronic kidney disease":
  exists ([Condition: "Chronic kidney disease codes"])
define "finerenone":
  exists ([MedicationRequest: "finerenone codes"])

define "Applicable":
  "Chronic kidney disease"

define "Satisfied":
  not "finerenone"

define "Compliant":
  not "Applicable" or "Satisfied"

define "Deontic":
  'obligation'

define "Violated":
  "Applicable" and not "Satisfied"
