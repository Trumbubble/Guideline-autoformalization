library Demopmc132703480140 version '1.0.000'

using FHIR version '4.0.1'
include FHIRHelpers version '4.0.1' called FHIRHelpers

codesystem "SNOMED": 'http://snomed.info/sct'

code "Implements Aseptic Technique codes": '370822003' from "SNOMED"
code "Implements aseptic technique codes": '370822003' from "SNOMED" display 'Implements aseptic technique'
code "Monitoring Of Patient codes": '182777000' from "SNOMED"
code "Monitoring of patient codes": '182777000' from "SNOMED" display 'Monitoring of patient'

context Patient

/* PMC13270348:0140
   action: monitor strict aseptic technique and close monitoring during epidural analgesia
   strength: conditional
   source: We suggest that epidural analgesia may be considered when expertise is available, and strict aseptic techniques and close monitoring are essential.
   PARTIAL: 2 unbound predicate(s) left null: has_epidural_analgesia, has_expertise_available
*/

define "Monitoring of patient":
  exists ([MedicationRequest: "Monitoring of patient codes"])
// UNBOUND: the source states this predicate but does not define it.
// Left null so CQL's three-valued logic propagates the uncertainty.
define "epidural analgesia":
  null as Boolean
// UNBOUND: the source states this predicate but does not define it.
// Left null so CQL's three-valued logic propagates the uncertainty.
define "when expertise is available":
  null as Boolean
define "Implements aseptic technique":
  exists ([MedicationRequest: "Implements aseptic technique codes"])

define "Applicable":
  ("epidural analgesia"
  and "when expertise is available")

define "Satisfied":
  ("Implements aseptic technique"
  and "Monitoring of patient")

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
