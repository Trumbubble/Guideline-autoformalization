library Demopmc132703480020 version '1.0.000'

using FHIR version '4.0.1'
include FHIRHelpers version '4.0.1' called FHIRHelpers

codesystem "SNOMED": 'http://snomed.info/sct'

code "Acute pancreatitis codes": '197456007' from "SNOMED" display 'Acute pancreatitis'
code "Patient Controlled Analgesia codes": '241716000' from "SNOMED"
code "Patient controlled analgesia codes": '241716000' from "SNOMED" display 'Patient controlled analgesia'

context Patient

/* PMC13270348:0020
   action: avoid patient_controlled_analgesia
   strength: not routinely recommended; use cautiously
   source: Patient‐controlled analgesia should be used cautiously and is not routinely recommended in AP
*/

define "Acute pancreatitis":
  exists ([Condition: "Acute pancreatitis codes"])
define "Patient controlled analgesia":
  exists ([MedicationRequest: "Patient controlled analgesia codes"])

define "Applicable":
  "Acute pancreatitis"

define "Satisfied":
  not "Patient controlled analgesia"

define "Compliant":
  not "Applicable" or "Satisfied"

define "Deontic":
  'obligation'

define "Violated":
  "Applicable" and not "Satisfied"
