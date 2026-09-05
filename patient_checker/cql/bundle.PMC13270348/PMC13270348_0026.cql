library Demopmc132703480026 version '1.0.000'

using FHIR version '4.0.1'
include FHIRHelpers version '4.0.1' called FHIRHelpers

codesystem "SNOMED": 'http://snomed.info/sct'

code "Acute pancreatitis codes": '197456007' from "SNOMED" display 'Acute pancreatitis'
code "Implements Aseptic Technique codes": '370822003' from "SNOMED"
code "Implements aseptic technique codes": '370822003' from "SNOMED" display 'Implements aseptic technique'
code "Pain Management codes": '278414003' from "SNOMED"
code "Pain management codes": '278414003' from "SNOMED" display 'Pain management'

context Patient

/* PMC13270348:0026
   action: recommend epidural analgesia for AP pain management
   source: Epidural analgesia is a safe option with appropriate monitoring and aseptic technique and may be considered for AP pain management where local expertise is available.
   PARTIAL: 1 unbound predicate(s) left null: has_epidural_analgesia
*/

define "Implements aseptic technique":
  exists ([MedicationRequest: "Implements aseptic technique codes"])
define "Acute pancreatitis":
  exists ([Condition: "Acute pancreatitis codes"])
// UNBOUND: the source states this predicate but does not define it.
// Left null so CQL's three-valued logic propagates the uncertainty.
define "Epidural analgesia":
  null as Boolean
define "Pain management":
  exists ([MedicationRequest: "Pain management codes"])

define "Applicable":
  "Acute pancreatitis"

define "Satisfied":
  ("Epidural analgesia"
  and "Implements aseptic technique")

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
