library Demopmc132703480044 version '1.0.000'

using FHIR version '4.0.1'
include FHIRHelpers version '4.0.1' called FHIRHelpers

codesystem "SNOMED": 'http://snomed.info/sct'

code "Endoscopic Ultrasonography codes": '450556004' from "SNOMED"
code "Endoscopic ultrasonography codes": '450556004' from "SNOMED" display 'Endoscopic ultrasonography'

context Patient

/* PMC13270348:0044
   action: recommend endoscopic ultrasound (EUS)
   source: EUS may be considered when aetiology remains unclear.
   PARTIAL: 1 unbound predicate(s) left null: has_aetiology_remains_unclear
*/

define "Endoscopic ultrasonography":
  exists ([MedicationRequest: "Endoscopic ultrasonography codes"])
// UNBOUND: the source states this predicate but does not define it.
// Left null so CQL's three-valued logic propagates the uncertainty.
define "aetiology remains unclear":
  null as Boolean

define "Applicable":
  "aetiology remains unclear"

define "Satisfied":
  "Endoscopic ultrasonography"

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
