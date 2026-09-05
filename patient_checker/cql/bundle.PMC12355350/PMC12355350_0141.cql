library Demopmc123553500141 version '1.0.000'

using FHIR version '4.0.1'
include FHIRHelpers version '4.0.1' called FHIRHelpers

codesystem "ATC": 'http://www.whocc.no/atc'
codesystem "RxNorm": 'http://www.nlm.nih.gov/research/umls/rxnorm'
codesystem "SNOMED": 'http://snomed.info/sct'

valueset "SGLT2 Inhibitors codes": 'urn:oid:2.16.840.1.113762.1.4.1178.30' version '20260418'
valueset "Sglt2 Inhibitors codes": 'urn:oid:2.16.840.1.113762.1.4.1178.30' version '20260418'

code "GLP-1 Receptor Agonist codes": 'N0000178480' from "ATC" display 'GLP-1 Receptor Agonist'
code "Glp-1 Receptor Agonist codes": 'N0000178480' from "ATC"
code "Tirzepatide codes": '2601723' from "RxNorm"
code "Weight loss codes": '816160009' from "SNOMED" display 'Weight loss'
code "tirzepatide codes": '2601723' from "RxNorm" display 'tirzepatide'

context Patient

/* PMC12355350:0141
   action: recommend SGLT-2 inhibitor
   strength: indicated
   source: For adults prioritising initiation of tirzepatide over GLP-1 receptor agonist therapy due to larger weight loss effects, initiation or continuation of an SGLT-2 inhibitor is indicated.
*/

define "Weight loss":
  exists ([Condition: "Weight loss codes"])
define "GLP-1 Receptor Agonist":
  exists ([MedicationRequest: "GLP-1 Receptor Agonist codes"])
define "SGLT2 Inhibitors":
  exists ([MedicationRequest: "SGLT2 Inhibitors codes"])
define "tirzepatide":
  exists ([MedicationRequest: "tirzepatide codes"])

define "Applicable":
  "tirzepatide"

define "Satisfied":
  "SGLT2 Inhibitors"

define "Compliant":
  not "Applicable" or "Satisfied"

define "Deontic":
  'obligation'

define "Violated":
  "Applicable" and not "Satisfied"
