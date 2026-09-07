"""Every consumer of the expression union must name every node of it.

WHY THIS FILE EXISTS. `Delta` was added to the DSL as one dataclass. It has
fifteen consumers -- the decoding schema, the validator, the Z3 lowering, the
CQL lowering, the variable walker, the event walker, the hole stubber, the
canonicaliser, the quantity extractor, the serialiser, and Track B's loader and
var-reference check. Python raises nothing when an `isinstance` chain does not
mention a new variant; the walker silently returns as if the node were not
there. So each consumer that had not been updated surfaced one live run later,
as a different symptom, and looked like a new and unrelated bug:

    run 1   the node was never offered      (no events declared)
    run 2   still not offered               (no numeric variable to change)
    run 3   events: [] in the bundle        (demo.py's walker knew only Since)
    run 3   Identifier already in use       (the baseline stubbed AND lowered)
    run 3   real but value is bool          (canonicalisation merged on code)

Every one of those is the same defect. The unit tests could not find them
because a unit test exercises the path its author already had in mind.

This test does not exercise paths. It reads the source, finds every dispatch
over the expression union, and asserts each names every variant -- so a NEW
walker is covered the moment it is written, and adding a node to the DSL breaks
the suite until every consumer has been updated. Deliberate omissions are
listed below WITH THEIR REASON; an omission with no entry here is a bug.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import glob
import os
import re

VARIANTS = {"Cmp", "Between", "Since", "Delta", "Not", "And", "Or"}

# (file, qualified walker) -> (omitted variants, why that is correct)
EXEMPT = {
    ("dsl.py", "_vars_in"): (
        {"Since"}, "Since names an EventDef, which is a separate namespace "
                   "from variables; it has no .var to report"),
    ("dsl.py", "open_holes.walk"): (
        {"Since"}, "an event is not a variable and so cannot be an unbound "
                   "hole; its absence is reported as UNKNOWN_EVENT instead"),
    ("dsl.py", "delta_baselines.walk"): (
        {"Cmp", "Between", "Since"}, "only Delta carries a baseline"),
    ("dsl.py", "events_in.walk"): (
        {"Cmp", "Between"}, "neither references an event"),
    ("dsl.py", "rename_vars"): (
        {"Since"}, "events live in a separate namespace and are not renamed "
                   "by a variable alias map"),
}

_DEF = re.compile(r"(\s*)def (\w+)")
_ISINSTANCE = re.compile(r"isinstance\(\s*\w+\s*,\s*\(?([A-Za-z_\.,\s]+?)\)?\s*\)")

def _walkers():
    """{(basename, qualified name): (variants named, line)} for every function
    in the codebase that dispatches on the expression union."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    files = (glob.glob(os.path.join(root, "*.py"))
             + glob.glob(os.path.join(root, "..", "patient_checker", "*.py")))
    found = {}
    for path in sorted(files):
        base = os.path.basename(path)
        if base.startswith("test_"):
            continue
        stack = []
        for i, line in enumerate(open(path, encoding="utf-8").read().splitlines(), 1):
            m = _DEF.match(line)
            if m:
                indent = len(m.group(1))
                while stack and stack[-1][0] >= indent:
                    stack.pop()
                stack.append((indent, m.group(2), i))
            for mm in _ISINSTANCE.finditer(line):
                named = {n.strip().replace("dsl.", "")
                         for n in mm.group(1).split(",")} & VARIANTS
                if named and stack:
                    key = (base, ".".join(s[1] for s in stack))
                    found.setdefault(key, [set(), stack[-1][2]])
                    found[key][0] |= named
    return found

def test_every_expression_walker_handles_every_node():
    walkers = _walkers()
    assert len(walkers) >= 12, ("the scanner found almost nothing, which means "
                                "it stopped matching the code rather than that "
                                "the code became clean")
    bad = []
    for (base, name), (named, line) in sorted(walkers.items()):
        missing = VARIANTS - named
        allowed, _why = EXEMPT.get((base, name), (set(), ""))
        unexplained = missing - allowed
        if unexplained:
            bad.append(f"{base}:{line} {name}() is silent on "
                       f"{', '.join(sorted(unexplained))} -- it will treat such "
                       f"a node as absent and return a usable-looking value")
    assert not bad, ("a node type reaches a walker that does not name it:\n  "
                     + "\n  ".join(bad)
                     + "\n\nEither handle it, or add it to EXEMPT with the "
                       "reason the omission is correct.")

def test_the_exemptions_are_still_needed():
    """A stale exemption is a hole: it would silence a real omission later."""
    walkers = _walkers()
    stale = [f"{base}:{name}" for (base, name), (allowed, _why) in EXEMPT.items()
             if (base, name) in walkers
             and not (allowed & (VARIANTS - walkers[(base, name)][0]))]
    assert not stale, f"exemption no longer applies, delete it: {stale}"
    unknown = [k for k in EXEMPT if k not in walkers]
    assert not unknown, f"exemption names a walker that no longer exists: {unknown}"

def test_every_exemption_states_a_reason():
    for key, (_omitted, why) in EXEMPT.items():
        assert len(why) > 20, f"{key} is exempted without an explanation"
