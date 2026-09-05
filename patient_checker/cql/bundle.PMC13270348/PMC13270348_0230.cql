library Demopmc132703480230 version '1.0.000'

using FHIR version '4.0.1'
include FHIRHelpers version '4.0.1' called FHIRHelpers

codesystem "SNOMED": 'http://snomed.info/sct'

code "Acute pancreatitis codes": '197456007' from "SNOMED" display 'Acute pancreatitis'
code "Imaging codes": '363679005' from "SNOMED"
code "Imaging codes": '363679005' from "SNOMED" display 'Imaging'
code "Pain codes": '22253000' from "SNOMED" display 'Pain'

context Patient

/* PMC13270348:0230
   action: recommend imaging to rule out complications
   strength: no specific studies reporting on its usefulness in this cohort
   source: In recurrent or escalating pain after AP, there might be a clinical indication for imaging to rule out complications although there are no specific studies reporting on its usefulness in this cohort.
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
  "Imaging"

define "Compliant":
  not "Applicable" or "Satisfied"

define "Deontic":
  'permission'

// The source PERMITS this action; it does not require it.
// Not taking a permitted option is not a violation.
define "OptionTaken":
  "Applicable" and "Satisfied"

define "OptionNotTaken":
  "Applicable" and not "Satisfied"

define "Violated":
  false
