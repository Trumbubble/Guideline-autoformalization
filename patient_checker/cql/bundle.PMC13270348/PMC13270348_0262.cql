library Demopmc132703480262 version '1.0.000'

using FHIR version '4.0.1'
include FHIRHelpers version '4.0.1' called FHIRHelpers

codesystem "SNOMED": 'http://snomed.info/sct'

valueset "Opioids codes": 'urn:oid:2.16.840.1.113762.1.4.1248.120' version '20260203'

code "Alcohol-induced acute pancreatitis codes": '235942001' from "SNOMED" display 'Alcohol-induced acute pancreatitis'
code "Gallstone acute pancreatitis codes": '235941008' from "SNOMED" display 'Gallstone acute pancreatitis'
code "Pain codes": '22253000' from "SNOMED" display 'Pain'

context Patient

/* PMC13270348:0262
   action: recommend opioid analgesia at discharge
   source: Hence, opioids may be considered to ensure adequate pain relief for both alcohol and biliary etiology AP at discharge.
*/

define "Alcohol-induced acute pancreatitis":
  exists ([Condition: "Alcohol-induced acute pancreatitis codes"])
define "Gallstone acute pancreatitis":
  exists ([Condition: "Gallstone acute pancreatitis codes"])
define "Pain":
  exists ([Condition: "Pain codes"])
define "Opioids":
  exists ([MedicationRequest: "Opioids codes"])

define "Applicable":
  ("Alcohol-induced acute pancreatitis"
  or "Gallstone acute pancreatitis")

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
