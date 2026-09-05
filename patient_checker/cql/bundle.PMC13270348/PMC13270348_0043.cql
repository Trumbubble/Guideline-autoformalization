library Demopmc132703480043 version '1.0.000'

using FHIR version '4.0.1'
include FHIRHelpers version '4.0.1' called FHIRHelpers

codesystem "SNOMED": 'http://snomed.info/sct'

code "Acute pancreatitis codes": '197456007' from "SNOMED" display 'Acute pancreatitis'
code "Imaging codes": '363679005' from "SNOMED"
code "Imaging codes": '363679005' from "SNOMED" display 'Imaging'
code "Pain codes": '22253000' from "SNOMED" display 'Pain'

context Patient

/* PMC13270348:0043
   action: avoid routine scheduled imaging after discharge following acute pancreatitis
   source: Routine scheduled imaging is not recommended for patients discharged with pain after acute pancreatitis; however, imaging should be considered in cases of recurrent or escalating pain to exclude complications.
*/

define "Acute pancreatitis":
  exists ([Condition: "Acute pancreatitis codes"])
define "Pain":
  exists ([Condition: "Pain codes"])
define "Imaging":
  exists ([MedicationRequest: "Imaging codes"])

define "Applicable":
  ("Acute pancreatitis"
  and "Pain")

define "Satisfied":
  not "Imaging"

define "Compliant":
  not "Applicable" or "Satisfied"

define "Deontic":
  'obligation'

define "Violated":
  "Applicable" and not "Satisfied"
