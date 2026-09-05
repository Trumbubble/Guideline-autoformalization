library Demopmc123553500127 version '1.0.000'

using FHIR version '4.0.1'
include FHIRHelpers version '4.0.1' called FHIRHelpers

codesystem "RxNorm": 'http://www.nlm.nih.gov/research/umls/rxnorm'
codesystem "SNOMED": 'http://snomed.info/sct'

valueset "SGLT2 Inhibitors codes": 'urn:oid:2.16.840.1.113762.1.4.1178.30' version '20260418'
valueset "Sglt2 Inhibitors codes": 'urn:oid:2.16.840.1.113762.1.4.1178.30' version '20260418'

code "Finerenone codes": '2562811' from "RxNorm"
code "Glucagon-Like Peptide 1 Receptor Agonist-Containing Product codes": '416636000' from "SNOMED"
code "Glucagon-like peptide 1 receptor agonist-containing product codes": '416636000' from "SNOMED" display 'Glucagon-like peptide 1 receptor agonist-containing product'
code "Tirzepatide codes": '2601723' from "RxNorm"
code "finerenone codes": '2562811' from "RxNorm" display 'finerenone'
code "tirzepatide codes": '2601723' from "RxNorm" display 'tirzepatide'

context Patient

/* PMC12355350:0127
   action: avoid GLP-1 receptor agonists
   source: Tirzepatide should not be given in combination with GLP-1 receptor agonists, but can be combined with SGLT-2 inhibitors and finerenone.
*/

define "finerenone":
  exists ([MedicationRequest: "finerenone codes"])
define "Glucagon-like peptide 1 receptor agonist-containing product":
  exists ([MedicationRequest: "Glucagon-like peptide 1 receptor agonist-containing product codes"])
define "SGLT2 Inhibitors":
  exists ([MedicationRequest: "SGLT2 Inhibitors codes"])
define "tirzepatide":
  exists ([MedicationRequest: "tirzepatide codes"])

define "Applicable":
  "tirzepatide"

define "Satisfied":
  not "Glucagon-like peptide 1 receptor agonist-containing product"

define "Compliant":
  not "Applicable" or "Satisfied"

define "Deontic":
  'obligation'

define "Violated":
  "Applicable" and not "Satisfied"
