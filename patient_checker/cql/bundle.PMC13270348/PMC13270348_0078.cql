library Demopmc132703480078 version '1.0.000'

using FHIR version '4.0.1'
include FHIRHelpers version '4.0.1' called FHIRHelpers

codesystem "SNOMED": 'http://snomed.info/sct'

code "Assessment Using Numeric Pain Rating Scale codes": '1284856004' from "SNOMED"
code "Assessment using Numeric Pain Rating Scale codes": '1284856004' from "SNOMED" display 'Assessment using Numeric Pain Rating Scale'

context Patient

/* PMC13270348:0078
   action: recommend NRS scale for pain assessment
   source: The NRS scale may be preferred due to better compliance in most studies.
   PARTIAL: 1 unbound predicate(s) left null: has_better_compliance_most_studies
*/

// UNBOUND: the source states this predicate but does not define it.
// Left null so CQL's three-valued logic propagates the uncertainty.
define "better compliance in most studies":
  null as Boolean
define "Assessment using Numeric Pain Rating Scale":
  exists ([MedicationRequest: "Assessment using Numeric Pain Rating Scale codes"])

define "Applicable":
  "better compliance in most studies"

define "Satisfied":
  "Assessment using Numeric Pain Rating Scale"

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
