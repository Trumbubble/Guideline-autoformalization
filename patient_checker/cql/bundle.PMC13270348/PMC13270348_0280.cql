library Demopmc132703480280 version '1.0.000'

using FHIR version '4.0.1'
include FHIRHelpers version '4.0.1' called FHIRHelpers

codesystem "SNOMED": 'http://snomed.info/sct'

valueset "Non Opioid Analgesics codes": 'urn:oid:2.16.840.1.113762.1.4.1260.173' version '20240313'
valueset "Opioids codes": 'urn:oid:2.16.840.1.113762.1.4.1248.120' version '20260203'

code "Pain Management codes": '278414003' from "SNOMED"
code "Pain management codes": '278414003' from "SNOMED" display 'Pain management'

context Patient

/* PMC13270348:0280
   action: recommend multidisciplinary pain management including simple analgesics, opioids, interventional procedures, and behavioural pain management interventions
   source: In children, a multidisciplinary approach to pain management, including the use of simple analgesics, opioids, interventional procedures, and behavioural pain management interventions should be considered.
   PARTIAL: 4 unbound predicate(s) left null: has_behavioural_pain_management_interventions, has_children, has_interventional_procedures, has_multidisciplinary_approach
*/

// UNBOUND: the source states this predicate but does not define it.
// Left null so CQL's three-valued logic propagates the uncertainty.
define "behavioural pain management interventions":
  null as Boolean
// UNBOUND: the source states this predicate but does not define it.
// Left null so CQL's three-valued logic propagates the uncertainty.
define "In children":
  null as Boolean
// UNBOUND: the source states this predicate but does not define it.
// Left null so CQL's three-valued logic propagates the uncertainty.
define "interventional procedures":
  null as Boolean
// UNBOUND: the source states this predicate but does not define it.
// Left null so CQL's three-valued logic propagates the uncertainty.
define "a multidisciplinary approach":
  null as Boolean
define "Opioids":
  exists ([MedicationRequest: "Opioids codes"])
define "Non Opioid Analgesics":
  exists ([MedicationRequest: "Non Opioid Analgesics codes"])
define "Pain management":
  exists ([MedicationRequest: "Pain management codes"])

define "Applicable":
  "In children"

define "Satisfied":
  ("a multidisciplinary approach"
  and "Non Opioid Analgesics"
  and "Opioids"
  and "interventional procedures"
  and "behavioural pain management interventions")

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
