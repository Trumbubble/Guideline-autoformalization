library Demopmc132703480046 version '1.0.000'

using FHIR version '4.0.1'
include FHIRHelpers version '4.0.1' called FHIRHelpers

codesystem "SNOMED": 'http://snomed.info/sct'

code "Magnetic Resonance Cholangiopancreatography codes": '314635004' from "SNOMED"
code "Magnetic resonance cholangiopancreatography codes": '314635004' from "SNOMED" display 'Magnetic resonance cholangiopancreatography'
code "Pancreatic duct anomaly codes": '253815007' from "SNOMED" display 'Pancreatic duct anomaly'
code "Pancreatitis codes": '75694006' from "SNOMED" display 'Pancreatitis'

context Patient

/* PMC13270348:0046
   action: recommend MRCP
   source: MRCP may be useful to assess the cause of pancreatitis related to ductal anomalies and routine follow‐up MRCP is not recommended.
*/

define "Pancreatic duct anomaly":
  exists ([Condition: "Pancreatic duct anomaly codes"])
define "Pancreatitis":
  exists ([Condition: "Pancreatitis codes"])
define "Magnetic resonance cholangiopancreatography":
  exists ([MedicationRequest: "Magnetic resonance cholangiopancreatography codes"])

define "Applicable":
  ("Pancreatitis"
  and "Pancreatic duct anomaly")

define "Satisfied":
  "Magnetic resonance cholangiopancreatography"

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
