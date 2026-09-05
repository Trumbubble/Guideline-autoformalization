library Demopmc132703480012 version '1.0.000'

using FHIR version '4.0.1'
include FHIRHelpers version '4.0.1' called FHIRHelpers

codesystem "SNOMED": 'http://snomed.info/sct'

code "Chronic pancreatitis codes": '235494005' from "SNOMED" display 'Chronic pancreatitis'
code "Pain Assessment codes": '225399009' from "SNOMED"
code "Pain assessment codes": '225399009' from "SNOMED" display 'Pain assessment'

context Patient

/* PMC13270348:0012
   action: recommend multidimensional pain assessment
   source: Multidimensional pain assessment may be considered given its established benefit in chronic pancreatitis.
*/

define "Chronic pancreatitis":
  exists ([Condition: "Chronic pancreatitis codes"])
define "Pain assessment":
  exists ([MedicationRequest: "Pain assessment codes"])

define "Applicable":
  "Chronic pancreatitis"

define "Satisfied":
  "Pain assessment"

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
