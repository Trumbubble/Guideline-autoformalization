library Demopmc123553500120 version '1.0.000'

using FHIR version '4.0.1'
include FHIRHelpers version '4.0.1' called FHIRHelpers

codesystem "SNOMED": 'http://snomed.info/sct'

valueset "SGLT2 Inhibitors codes": 'urn:oid:2.16.840.1.113762.1.4.1178.30' version '20260418'
valueset "Sglt2 Inhibitors codes": 'urn:oid:2.16.840.1.113762.1.4.1178.30' version '20260418'

code "Heart failure codes": '84114007' from "SNOMED" display 'Heart failure'

context Patient

/* PMC12355350:0120
   action: recommend SGLT-2 inhibitors
   source: Adults with established heart failure may favour SGLT-2 inhibitors over alternatives.
*/

define "Heart failure":
  exists ([Condition: "Heart failure codes"])
define "SGLT2 Inhibitors":
  exists ([MedicationRequest: "SGLT2 Inhibitors codes"])

define "Applicable":
  "Heart failure"

define "Satisfied":
  "SGLT2 Inhibitors"

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
