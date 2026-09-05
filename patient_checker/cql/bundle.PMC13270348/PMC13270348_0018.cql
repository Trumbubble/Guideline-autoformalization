library Demopmc132703480018 version '1.0.000'

using FHIR version '4.0.1'
include FHIRHelpers version '4.0.1' called FHIRHelpers

codesystem "SNOMED": 'http://snomed.info/sct'

valueset "Opioids codes": 'urn:oid:2.16.840.1.113762.1.4.1248.120' version '20260203'

code "Severe pain codes": '76948002' from "SNOMED" display 'Severe pain'

context Patient

/* PMC13270348:0018
   action: recommend opioid analgesia
   source: Opioids are not associated with increased adverse outcomes and should not be delayed due to safety concerns to ensure adequate severe pain control.
*/

define "Severe pain":
  exists ([Condition: "Severe pain codes"])
define "Opioids":
  exists ([MedicationRequest: "Opioids codes"])

define "Applicable":
  "Severe pain"

define "Satisfied":
  "Opioids"

define "Compliant":
  not "Applicable" or "Satisfied"

define "Deontic":
  'obligation'

define "Violated":
  "Applicable" and not "Satisfied"
