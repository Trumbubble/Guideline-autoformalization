#!/usr/bin/env node
/**
 * Execute a compiled CQL library (ELM JSON) against a FHIR bundle.
 *
 * The second half of DEV_PLAN P6. `cql-execution` does NOT parse CQL -- it
 * consumes ELM only -- which is why the Java translator is unavoidable and why
 * this script takes ELM, not the .cql it came from.
 *
 *   node execute.js <main.json> <bundle.json> [valuesets.json] [deps...]
 *
 * Prints one JSON object: every define's value for the patient. A define that
 * evaluates to null is reported as null, not as false -- that distinction is
 * the three-valued verdict, and collapsing it here would undo the reason holes
 * are encoded as null in the first place.
 */
const fs = require('fs');
const cql = require('cql-execution');
const cqlfhir = require('cql-exec-fhir');

const [, , mainPath, bundlePath, vsPath, ...depPaths] = process.argv;
if (!mainPath || !bundlePath) {
  console.error('usage: execute.js <main-elm.json> <fhir-bundle.json> [valuesets.json] [dep-elm.json...]');
  process.exit(2);
}

const read = p => JSON.parse(fs.readFileSync(p, 'utf8'));

// Dependencies (FHIRHelpers and friends) are looked up by identifier/version.
const repo = new cql.Repository(
  Object.fromEntries(depPaths.map(p => {
    const elm = read(p);
    return [elm.library.identifier.id, elm];
  }))
);

const library = new cql.Library(read(mainPath), repo);

// Value sets must be supplied as expanded code lists: the engine resolves
// membership locally and never calls a terminology server, so an artifact
// executes deterministically against the versions it was bound to.
const codeService = new cql.CodeService(vsPath ? read(vsPath) : {});

const executor = new cql.Executor(library, codeService);
const patientSource = cqlfhir.PatientSource.FHIRv401();
patientSource.loadBundles([read(bundlePath)]);

executor.exec(patientSource).then(results => {
  const out = {};
  for (const [pid, res] of Object.entries(results.patientResults || {})) {
    out[pid] = {};
    for (const [name, value] of Object.entries(res)) {
      if (name === 'Patient') continue;
      out[pid][name] = value === undefined ? null : summarise(value);
    }
  }
  console.log(JSON.stringify({ results: out, errors: results.errors || [] }, null, 1));
}).catch(err => {
  console.error('EXEC-FAILURE: ' + (err && err.message ? err.message : err));
  process.exit(2);
});

function summarise(v) {
  if (v === null || v === undefined || typeof v !== 'object') return v;
  if (Array.isArray(v)) return `[${v.length} items]`;
  // cql-execution wraps a FHIR Quantity so that .value and .unit are themselves
  // objects; reading them naively prints "[object Object] [object Object]".
  const num = unwrap(v.value), unit = unwrap(v.unit);
  if (num !== undefined && num !== null) {
    return unit ? `${num} ${unit}` : num;
  }
  if (v.isDateTime || v.isDate) return v.toString();
  return v.constructor ? v.constructor.name : 'object';
}

function unwrap(x) {
  if (x === null || x === undefined) return x;
  if (typeof x !== 'object') return x;
  if (x.value !== undefined) return unwrap(x.value);
  return undefined;
}
