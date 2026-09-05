library Demopmc123553500050 version '1.0.000'

using FHIR version '4.0.1'
include FHIRHelpers version '4.0.1' called FHIRHelpers

codesystem "RxNorm": 'http://www.nlm.nih.gov/research/umls/rxnorm'

valueset "Obesity codes": 'urn:oid:2.16.840.1.113762.1.4.1272.8' version '20250318'

code "Tirzepatide codes": '2601723' from "RxNorm"
code "tirzepatide codes": '2601723' from "RxNorm" display 'tirzepatide'

context Patient

/* PMC12355350:0050
   action: recommend tirzepatide
   strength: weak recommendation in favour
   source: Across risk strata: weak recommendation in favour of tirzepatide in adults with obesity.
*/

define "Obesity":
  exists ([Condition: "Obesity codes"])
define "tirzepatide":
  exists ([MedicationRequest: "tirzepatide codes"])

define "Applicable":
  "Obesity"

define "Satisfied":
  "tirzepatide"

define "Compliant":
  not "Applicable" or "Satisfied"

define "Deontic":
  'obligation'

define "Violated":
  "Applicable" and not "Satisfied"
