library Demopmc132703480088 version '1.0.000'

using FHIR version '4.0.1'
include FHIRHelpers version '4.0.1' called FHIRHelpers

codesystem "SNOMED": 'http://snomed.info/sct'

valueset "Opioids codes": 'urn:oid:2.16.840.1.113762.1.4.1248.120' version '20260203'

code "Analgesic codes": '373265006' from "SNOMED"
code "Analgesic codes": '373265006' from "SNOMED" display 'Analgesic'
code "Severe pain codes": '76948002' from "SNOMED" display 'Severe pain'

context Patient

/* PMC13270348:0088
   action: recommend opioid analgesia upon presentation
   strength: may be warranted
   source: For patients with severe pain, however, this approach may be insufficient. As such, more potent analgesics such as opioids may be warranted upon presentation.
*/

define "Severe pain":
  exists ([Condition: "Severe pain codes"])
define "Analgesic":
  exists ([MedicationRequest: "Analgesic codes"])
define "Opioids":
  exists ([MedicationRequest: "Opioids codes"])

define "Applicable":
  "Severe pain"

define "Satisfied":
  "Opioids"

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
