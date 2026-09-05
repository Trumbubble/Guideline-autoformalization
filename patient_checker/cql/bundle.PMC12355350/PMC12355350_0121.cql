library Demopmc123553500121 version '1.0.000'

using FHIR version '4.0.1'
include FHIRHelpers version '4.0.1' called FHIRHelpers

codesystem "ATC": 'http://www.whocc.no/atc'

valueset "SGLT2 Inhibitors codes": 'urn:oid:2.16.840.1.113762.1.4.1178.30' version '20260418'
valueset "Sglt2 Inhibitors codes": 'urn:oid:2.16.840.1.113762.1.4.1178.30' version '20260418'

code "GLP-1 Receptor Agonist codes": 'N0000178480' from "ATC" display 'GLP-1 Receptor Agonist'
code "Glp-1 Receptor Agonist codes": 'N0000178480' from "ATC"

context Patient

/* PMC12355350:0121
   action: recommend GLP-1 receptor agonist in combination with SGLT-2 inhibitor
   source: SGLT-2 inhibitors and GLP-1 receptor agonists can be combined.
*/

define "GLP-1 Receptor Agonist":
  exists ([MedicationRequest: "GLP-1 Receptor Agonist codes"])
define "SGLT2 Inhibitors":
  exists ([MedicationRequest: "SGLT2 Inhibitors codes"])

define "Applicable":
  "SGLT2 Inhibitors"

define "Satisfied":
  "GLP-1 Receptor Agonist"

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
