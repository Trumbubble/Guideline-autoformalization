library Kdigo31 version '1.0.000'

using FHIR version '4.0.1'
include FHIRHelpers version '4.0.1' called FHIRHelpers

codesystem "LOINC": 'http://loinc.org'

valueset "ACE inhibitors codes": 'urn:oid:2.16.840.1.113762.1.4.1078.9' version '20241228'
valueset "Diabetes codes": 'urn:oid:2.16.840.1.113883.3.464.1003.103.12.1001' version '20250115'

code "urine ACR codes": '9318-7' from "LOINC" display 'urine ACR'

context Patient

/* kdigo-3.1
   action: recommend ACEi
   strength: 1B
   source: /a/b/c
*/

define "urine ACR": // recorded in mg/g
  Last([Observation: "urine ACR codes"] O
    where O.status in {'final', 'amended', 'corrected'}
    sort by effective).value as Quantity
define "Diabetes":
  exists ([Condition: "Diabetes codes"])
define "ACE inhibitors":
  exists ([MedicationRequest: "ACE inhibitors codes"])

define "Applicable":
  ("Diabetes"
  and "urine ACR" >= 30 'mg/g')

define "Satisfied":
  "ACE inhibitors"

define "Compliant":
  not "Applicable" or "Satisfied"

define "Violated":
  "Applicable" and not "Satisfied"
