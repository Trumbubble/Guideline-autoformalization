library Demopmc132703480210 version '1.0.000'

using FHIR version '4.0.1'
include FHIRHelpers version '4.0.1' called FHIRHelpers

codesystem "SNOMED": 'http://snomed.info/sct'

valueset "Opioids codes": 'urn:oid:2.16.840.1.113762.1.4.1248.120' version '20260203'

code "Pain Management codes": '278414003' from "SNOMED"
code "Pain management codes": '278414003' from "SNOMED" display 'Pain management'

context Patient

/* PMC13270348:0210
   action: recommend multidisciplinary approach to pain management
   strength: extrapolated from postoperative setting; may be considered
   source: The utilization of a multidisciplinary approach to pain management can be extrapolated from the postoperative setting, which has been shown to decrease postoperative opioid use and may be considered.
*/

define "Pain management":
  exists ([MedicationRequest: "Pain management codes"])
define "Opioids":
  exists ([MedicationRequest: "Opioids codes"])

define "Applicable":
  "Opioids"

define "Satisfied":
  "Pain management"

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
