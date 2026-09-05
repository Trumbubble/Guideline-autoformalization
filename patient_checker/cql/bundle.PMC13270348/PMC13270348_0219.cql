library Demopmc132703480219 version '1.0.000'

using FHIR version '4.0.1'
include FHIRHelpers version '4.0.1' called FHIRHelpers

codesystem "SNOMED": 'http://snomed.info/sct'

valueset "NSAIDs codes": 'urn:oid:2.16.840.1.113762.1.4.1213.9' version '20190917'
valueset "Nsaids codes": 'urn:oid:2.16.840.1.113762.1.4.1213.9' version '20190917'

code "Third trimester pregnancy codes": '41587001' from "SNOMED" display 'Third trimester pregnancy'

context Patient

/* PMC13270348:0219
   action: avoid NSAIDs
   strength: contraindicated
   source: The use of NSAIDs is contraindicated in the third trimester of pregnancy.
*/

define "Third trimester pregnancy":
  exists ([Condition: "Third trimester pregnancy codes"])
define "NSAIDs":
  exists ([MedicationRequest: "NSAIDs codes"])

define "Applicable":
  "Third trimester pregnancy"

define "Satisfied":
  not "NSAIDs"

define "Compliant":
  not "Applicable" or "Satisfied"

define "Deontic":
  'obligation'

define "Violated":
  "Applicable" and not "Satisfied"
