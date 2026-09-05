library Demopmc123553500130 version '1.0.000'

using FHIR version '4.0.1'
include FHIRHelpers version '4.0.1' called FHIRHelpers

codesystem "ATC": 'http://www.whocc.no/atc'
codesystem "RxNorm": 'http://www.nlm.nih.gov/research/umls/rxnorm'

valueset "SGLT2 Inhibitors codes": 'urn:oid:2.16.840.1.113762.1.4.1178.30' version '20260418'
valueset "Sglt2 Inhibitors codes": 'urn:oid:2.16.840.1.113762.1.4.1178.30' version '20260418'

code "GLP-1 Receptor Agonist codes": 'N0000178480' from "ATC" display 'GLP-1 Receptor Agonist'
code "Glp-1 Receptor Agonist codes": 'N0000178480' from "ATC"
code "Tirzepatide codes": '2601723' from "RxNorm"
code "tirzepatide codes": '2601723' from "RxNorm" display 'tirzepatide'

context Patient

/* PMC12355350:0130
   action: recommend SGLT-2 inhibitor initiation or continuation
   source: In adults at higher risk of cardiovascular and kidney complications, tirzepatide should generally not replace medications effective in reducing the risk of these complications. If replacing a GLP-1 receptor agonist with tirzepatide, initiation or continuation of an SGLT-2 inhibitor is indicated.
   PARTIAL: 1 unbound predicate(s) left null: has_cardiovascular_kidney_complications
*/

// UNBOUND: the source states this predicate but does not define it.
// Left null so CQL's three-valued logic propagates the uncertainty.
define "cardiovascular and kidney complications":
  null as Boolean
define "GLP-1 Receptor Agonist":
  exists ([MedicationRequest: "GLP-1 Receptor Agonist codes"])
define "SGLT2 Inhibitors":
  exists ([MedicationRequest: "SGLT2 Inhibitors codes"])
define "tirzepatide":
  exists ([MedicationRequest: "tirzepatide codes"])

define "Applicable":
  ("cardiovascular and kidney complications"
  and "tirzepatide"
  and not "GLP-1 Receptor Agonist")

define "Satisfied":
  "SGLT2 Inhibitors"

define "Compliant":
  not "Applicable" or "Satisfied"

define "Deontic":
  'obligation'

define "Violated":
  "Applicable" and not "Satisfied"
