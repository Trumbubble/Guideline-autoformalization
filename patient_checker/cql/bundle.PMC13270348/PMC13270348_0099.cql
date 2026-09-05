library Demopmc132703480099 version '1.0.000'

using FHIR version '4.0.1'
include FHIRHelpers version '4.0.1' called FHIRHelpers

codesystem "SNOMED": 'http://snomed.info/sct'

valueset "Opioids codes": 'urn:oid:2.16.840.1.113762.1.4.1248.120' version '20260203'

code "Acute pancreatitis codes": '197456007' from "SNOMED" display 'Acute pancreatitis'
code "Pain Management codes": '278414003' from "SNOMED"
code "Pain codes": '22253000' from "SNOMED" display 'Pain'
code "Pain management codes": '278414003' from "SNOMED" display 'Pain management'

context Patient

/* PMC13270348:0099
   action: recommend opioid analgesia
   strength: conditional (GRADE suggest)
   source: In patients with AP and severe pain, we suggest that safety concerns should not delay the timely use of opioids to ensure adequate pain management.
*/

define "Acute pancreatitis":
  exists ([Condition: "Acute pancreatitis codes"])
define "Pain":
  exists ([Condition: "Pain codes"])
define "Opioids":
  exists ([MedicationRequest: "Opioids codes"])
define "Pain management":
  exists ([MedicationRequest: "Pain management codes"])

define "Applicable":
  ("Acute pancreatitis"
  and "Pain")

define "Satisfied":
  "Opioids"

define "Compliant":
  not "Applicable" or "Satisfied"

define "Deontic":
  'obligation'

define "Violated":
  "Applicable" and not "Satisfied"
