library Demopmc132703480077 version '1.0.000'

using FHIR version '4.0.1'
include FHIRHelpers version '4.0.1' called FHIRHelpers

codesystem "SNOMED": 'http://snomed.info/sct'

code "Assessment Using Numeric Pain Rating Scale codes": '1284856004' from "SNOMED"
code "Assessment using Numeric Pain Rating Scale codes": '1284856004' from "SNOMED" display 'Assessment using Numeric Pain Rating Scale'
code "Pain Assessment codes": '225399009' from "SNOMED"
code "Pain assessment codes": '225399009' from "SNOMED" display 'Pain assessment'

context Patient

/* PMC13270348:0077
   action: recommend unidimensional pain scale (VAS or NRS) for acute pancreatitis pain assessment, NRS preferred
   strength: NRS may be preferred due to better compliance in most studies
   source: Both unidimensional scales are equally suited for acute pain assessment in general and can be used for AP pain. The NRS scale may be preferred due to better compliance in most studies.
   PARTIAL: 2 unbound predicate(s) left null: has_ap_pain, has_unidimensional_scales
*/

define "Pain assessment":
  exists ([MedicationRequest: "Pain assessment codes"])
// UNBOUND: the source states this predicate but does not define it.
// Left null so CQL's three-valued logic propagates the uncertainty.
define "AP pain":
  null as Boolean
// UNBOUND: the source states this predicate but does not define it.
// Left null so CQL's three-valued logic propagates the uncertainty.
define "unidimensional scales":
  null as Boolean
define "Assessment using Numeric Pain Rating Scale":
  exists ([MedicationRequest: "Assessment using Numeric Pain Rating Scale codes"])

define "Applicable":
  "AP pain"

define "Satisfied":
  ("unidimensional scales"
  or "Assessment using Numeric Pain Rating Scale")

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
