"""
The real CQL toolchain, as injectable callables (DEV_PLAN P6).

`cql.synthesize(..., compiler=...)` and Track B's checker both take their
external tools as parameters so the pipeline runs offline. This module supplies
the real implementations: the official HL7 cql-to-elm translator, and
cql-execution over a FHIR bundle.

    import cql, toolchain
    out = cql.synthesize(stmt, reg, formalize, compiler=toolchain.cql_to_elm)

Nothing here is imported by any pipeline stage, and importing it starts no
process. `available()` says whether the toolchain is built; if it is not,
`cql_to_elm` RAISES rather than returning "no errors" -- a missing compiler
must never be indistinguishable from a clean compile.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile

ENGINE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "Part B", "engine")
TIMEOUT = 120

class ToolchainMissing(RuntimeError):
    """Raised rather than returning an empty error list. Silence must not read
    as success -- that mistake already cost a whole guideline run once."""

def available() -> bool:
    return (os.path.exists(os.path.join(ENGINE, "cp.txt"))
            and os.path.exists(os.path.join(ENGINE, "classes", "Translate.class")))

def _require() -> str:
    if not available():
        raise ToolchainMissing(
            f"CQL toolchain not built. Run: bash {os.path.join(ENGINE, 'setup.sh')}")
    with open(os.path.join(ENGINE, "cp.txt")) as fh:
        return fh.read().strip()

_ERR = re.compile(r"^ERROR(?: \[line (\d+):(\d+)\])?: (.*)$")

def cql_to_elm(text: str, *, out_path: str | None = None) -> list[str]:
    """Translate CQL and return the translator's errors, one string each.

    This is the `compiler` argument `cql.synthesize` expects. An empty list
    means it compiled -- which now means the FHIR types resolved, not merely
    that the brackets balanced.

    The library is written into the engine directory so `DefaultLibrarySource
    Provider` can find FHIRHelpers next to it; every generated library includes
    it, and without a sibling copy every translation fails on the include."""
    cp = _require()
    with tempfile.NamedTemporaryFile("w", suffix=".cql", dir=ENGINE,
                                     delete=False) as fh:
        fh.write(text)
        src = fh.name
    dst = out_path or src.replace(".cql", ".json")
    try:
        p = subprocess.run(["java", "-cp", f"{os.path.join(ENGINE,'classes')}:{cp}",
                            "Translate", src, dst],
                           capture_output=True, text=True, timeout=TIMEOUT, cwd=ENGINE)
        errors = []
        for line in (p.stderr or "").splitlines():
            m = _ERR.match(line.strip())
            if m:
                where = f"line {m.group(1)}:{m.group(2)} " if m.group(1) else ""
                errors.append(f"{where}{m.group(3)}")
            elif line.startswith("DRIVER-FAILURE:"):
                # The driver itself broke. That is NOT a clean compile, and it
                # is not a CQL error the model can repair either.
                raise ToolchainMissing(line)
        return errors
    except subprocess.TimeoutExpired:
        raise ToolchainMissing(f"translator timed out after {TIMEOUT}s")
    finally:
        for p_ in (src, dst if out_path is None else None):
            if p_ and os.path.exists(p_):
                try: os.unlink(p_)
                except OSError: pass

def translate_to_file(text: str, elm_path: str) -> list[str]:
    """Translate and KEEP the ELM, for execution."""
    return cql_to_elm(text, out_path=elm_path)

def execute(elm_path: str, bundle_path: str, valuesets_path: str | None = None,
            deps: list[str] | None = None) -> dict:
    """Run compiled ELM against a FHIR bundle. Returns {patient_id: {define: value}}.

    A define that evaluates to null comes back as None, not False. That
    distinction IS the three-valued verdict -- an unbound predicate is encoded
    as `null` precisely so CQL's own logic propagates it, and collapsing it
    here would undo that."""
    _require()
    deps = deps or [os.path.join(ENGINE, "FHIRHelpers.json")]
    args = ["node", os.path.join(ENGINE, "execute.js"), elm_path, bundle_path]
    args.append(valuesets_path or os.path.join(ENGINE, "valuesets.json"))
    args += [d for d in deps if os.path.exists(d)]
    p = subprocess.run(args, capture_output=True, text=True, timeout=TIMEOUT, cwd=ENGINE)
    if p.returncode != 0:
        raise RuntimeError(f"execution failed: {(p.stderr or '').strip()[:300]}")
    return json.loads(p.stdout)
