library Demopmc132703480040 version '1.0.000'

using FHIR version '4.0.1'
include FHIRHelpers version '4.0.1' called FHIRHelpers

codesystem "SNOMED": 'http://snomed.info/sct'

code "Acute pancreatitis codes": '197456007' from "SNOMED" display 'Acute pancreatitis'
code "Pain Management codes": '278414003' from "SNOMED"
code "Pain management codes": '278414003' from "SNOMED" display 'Pain management'

context Patient

/* PMC13270348:0040
   action: recommend multidisciplinary pain management
   strength: extrapolated from postoperative settings; may be considered
   source: Multidisciplinary pain management can be extrapolated from postoperative settings and may be considered in paediatric AP
*/

define "Acute pancreatitis":
  exists ([Condition: "Acute pancreatitis codes"])
define "Pain management":
  exists ([MedicationRequest: "Pain management codes"])

define "Applicable":
  "Acute pancreatitis"

define "Satisfied":
  "Pain management"

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
