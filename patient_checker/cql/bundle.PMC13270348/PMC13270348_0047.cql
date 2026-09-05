library Demopmc132703480047 version '1.0.000'

using FHIR version '4.0.1'
include FHIRHelpers version '4.0.1' called FHIRHelpers

codesystem "SNOMED": 'http://snomed.info/sct'

valueset "Opioids codes": 'urn:oid:2.16.840.1.113762.1.4.1248.120' version '20260203'

code "Alcohol-induced acute pancreatitis codes": '235942001' from "SNOMED" display 'Alcohol-induced acute pancreatitis'
code "Pain Relief codes": '182970005' from "SNOMED"
code "Pain relief codes": '182970005' from "SNOMED" display 'Pain relief'

context Patient

/* PMC13270348:0047
   action: recommend opioid analgesia at discharge
   strength: no strong evidence; option
   source: No strong evidence linking alcoholic aetiology to increased opioid use at discharge; opioids may be considered to ensure adequate pain relief at discharge in both alcohol and biliary AP
   PARTIAL: 1 unbound predicate(s) left null: has_alcohol_biliary_ap
*/

// UNBOUND: the source states this predicate but does not define it.
// Left null so CQL's three-valued logic propagates the uncertainty.
define "alcohol and biliary AP":
  null as Boolean
define "Alcohol-induced acute pancreatitis":
  exists ([Condition: "Alcohol-induced acute pancreatitis codes"])
define "Opioids":
  exists ([MedicationRequest: "Opioids codes"])
define "Opioids":
  exists ([MedicationRequest: "Opioids codes"])
define "Pain relief":
  exists ([MedicationRequest: "Pain relief codes"])

define "Applicable":
  "alcohol and biliary AP"

define "Satisfied":
  "Opioids"

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
