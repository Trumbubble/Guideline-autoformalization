library Demopmc132703480061 version '1.0.000'

using FHIR version '4.0.1'
include FHIRHelpers version '4.0.1' called FHIRHelpers

codesystem "SNOMED": 'http://snomed.info/sct'

code "Acute postoperative pain codes": '107401000119105' from "SNOMED" display 'Acute postoperative pain'
code "Referral To Acute Pain Service codes": '307373005' from "SNOMED"
code "Referral to acute pain service codes": '307373005' from "SNOMED" display 'Referral to acute pain service'
code "Seen By Pain Management Specialist codes": '305652004' from "SNOMED"
code "Seen by pain management specialist codes": '305652004' from "SNOMED" display 'Seen by pain management specialist'

context Patient

/* PMC13270348:0061
   action: refer acute pain services or consultation by pain specialists
   source: acute pain services or similar consultations by pain specialists are recommended for acute postoperative pain as well
*/

define "Referral to acute pain service":
  exists ([MedicationRequest: "Referral to acute pain service codes"])
define "Seen by pain management specialist":
  exists ([MedicationRequest: "Seen by pain management specialist codes"])
define "Acute postoperative pain":
  exists ([Condition: "Acute postoperative pain codes"])

define "Applicable":
  "Acute postoperative pain"

define "Satisfied":
  ("Referral to acute pain service"
  or "Seen by pain management specialist")

define "Compliant":
  not "Applicable" or "Satisfied"

define "Deontic":
  'obligation'

define "Violated":
  "Applicable" and not "Satisfied"
