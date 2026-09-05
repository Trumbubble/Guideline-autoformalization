library Demopmc132703480279 version '1.0.000'

using FHIR version '4.0.1'
include FHIRHelpers version '4.0.1' called FHIRHelpers

codesystem "SNOMED": 'http://snomed.info/sct'

code "Abdominal pain codes": '21522001' from "SNOMED" display 'Abdominal pain'
code "Acupuncture codes": '44868003' from "SNOMED"
code "Acupuncture codes": '44868003' from "SNOMED" display 'Acupuncture'
code "Acute pancreatitis codes": '197456007' from "SNOMED" display 'Acute pancreatitis'
code "Oral Intake codes": '870690008' from "SNOMED"
code "Oral intake codes": '870690008' from "SNOMED" display 'Oral intake'

context Patient

/* PMC13270348:0279
   action: recommend acupuncture
   strength: should be considered where expertise is available
   source: Acupuncture appears to be effective in relieving abdominal pain and reducing the time to oral intake without significant side effects in patients with AP and should be considered where expertise is available.
   PARTIAL: 1 unbound predicate(s) left null: has_where_expertise_available
*/

define "Acupuncture":
  exists ([MedicationRequest: "Acupuncture codes"])
define "Abdominal pain":
  exists ([Condition: "Abdominal pain codes"])
define "Acute pancreatitis":
  exists ([Condition: "Acute pancreatitis codes"])
// UNBOUND: the source states this predicate but does not define it.
// Left null so CQL's three-valued logic propagates the uncertainty.
define "where expertise is available":
  null as Boolean
define "Oral intake":
  exists ([MedicationRequest: "Oral intake codes"])

define "Applicable":
  ("Acute pancreatitis"
  and "where expertise is available")

define "Satisfied":
  "Acupuncture"

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
