"""The demo pages must not make claims the data contradicts.

Every user-visible failure in this app has had one shape: a value hardcoded in
an HTML file drifting away from data that gets regenerated. Rule ids that no
longer exist after a re-run; a heading saying "one rule" for a page that uses
two; a card claiming "both encoded rules are permissions" after the document
was re-run into twenty-three; titles overwritten with bare document ids.

Each was found by a person reading the page. This file finds them by running,
and covers the whole class rather than the instance that happened to be
noticed: identifiers must resolve, scenario variables must be ones the rule
actually uses, and every number in the copy must equal the number in the data.
"""
import json
import re
from pathlib import Path

import pytest

HERE = Path(__file__).parent
PAGES = {n: (HERE / n).read_text(encoding="utf-8")
         for n in ("demo.html", "demo-toy.html")}

WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
         "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
         "twelve": 12, "seventeen": 17, "twenty-three": 23}


@pytest.fixture(scope="module")
def client():
    import api
    api.app.config["TESTING"] = True
    return api.app.test_client()


def _json(client, path):
    r = client.get(path)
    assert r.status_code == 200, f"{path} -> HTTP {r.status_code}"
    return r.get_json()


def _referenced_guideline_ids():
    ids = set()
    for source in PAGES.values():
        ids |= set(re.findall(r'id: "([a-z0-9][a-z0-9-]*)"', source))
        ids |= set(re.findall(r'GUIDELINE_ID = "([^"]+)"', source))
    return sorted(ids)


def _referenced_rule_ids():
    out = []
    for name, source in PAGES.items():
        for rid in re.findall(r'rule_id: "([^"]+)"', source):
            gid = rid.split(":")[0].lower() if ":" in rid else "fixtures"
            out.append((name, gid, rid))
    return sorted(set(out))


def _js_to_json(text):
    text = re.sub(r"([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:", r'\1"\2":', text)
    return re.sub(r",(\s*[}\]])", r"\1", text)


def _scenario_bodies():
    out = []
    for name, source in PAGES.items():
        for m in re.finditer(r"body:\s*(\{.*?\}\s*\})\s*,?\s*\}", source, re.S):
            try:
                body = json.loads(_js_to_json(m.group(1)))
            except json.JSONDecodeError:
                continue
            if body.get("rule_id"):
                out.append((name, body))
    return out


@pytest.mark.parametrize("gid", _referenced_guideline_ids())
def test_every_referenced_guideline_exists(client, gid):
    assert client.get(f"/guidelines/{gid}").status_code == 200


@pytest.mark.parametrize("name,gid,rid", _referenced_rule_ids(),
                         ids=lambda v: str(v))
def test_every_referenced_rule_exists(client, name, gid, rid):
    rules = _json(client, f"/guidelines/{gid}/rules")
    assert any(r["rule_id"] == rid for r in rules), (
        f"{name} references {rid}, which {gid} no longer has -- a re-run "
        f"renumbers rules, so the page must be updated with them"
    )


@pytest.mark.parametrize("name,body", _scenario_bodies(),
                         ids=lambda v: str(v)[:40])
def test_scenarios_only_pin_variables_the_rule_uses(client, name, body):
    """A stray variable is silently ignored, so the scenario would demonstrate
    something other than what its label claims."""
    rid = body["rule_id"]
    gid = rid.split(":")[0].lower() if ":" in rid else "fixtures"
    rule = next((r for r in _json(client, f"/guidelines/{gid}/rules")
                 if r["rule_id"] == rid), None)
    assert rule, f"{rid} missing"
    sent = set(body.get("patient") or {}) | set(body.get("plan") or {})
    stray = sent - set(rule.get("variables") or [])
    assert not stray, f"{name}/{rid} pins {sorted(stray)}, unused by the rule"


def _doc_notes():
    out = []
    for block in re.split(r'\n\s*\{\s*\n\s*id: "', PAGES["demo.html"])[1:]:
        gid = block.split('"', 1)[0]
        note = " ".join(re.findall(r'note: "(.*?)",\n', block, re.S)[:1])
        note = " ".join(note.replace('" +', "").replace('"', "").split())
        if note:
            out.append((gid, note))
    return out


@pytest.mark.parametrize("gid,note", _doc_notes())
def test_the_copy_agrees_with_the_data(client, gid, note):
    meta = _json(client, f"/guidelines/{gid}")
    residue = _json(client, f"/guidelines/{gid}/residue")
    rules = meta["rule_count"]
    refused = sum((residue.get("by_reason") or {}).values())

    for m in re.finditer(r"\b([A-Za-z-]+|\d+)\s+rules?\b", note, re.I):
        token = m.group(1).lower()
        claimed = int(token) if token.isdigit() else WORDS.get(token)
        if claimed is not None:
            assert claimed == rules, (
                f"{gid}: copy says {m.group(0)!r}, data says {rules} rules")

    for m in re.finditer(r"\b(\d+)\s+recommendations\b", note, re.I):
        assert int(m.group(1)) == rules + refused, (
            f"{gid}: copy says {m.group(0)!r}, data says {rules + refused}")

    for m in re.finditer(r"\b(\d+)\s+of them PERMISSIONS", note, re.I):
        got = sum(1 for r in _json(client, f"/guidelines/{gid}/rules")
                  if str(r.get("category", "")).lower() == "permission")
        # category may not carry deontic force; fall back to the bundle
        if got == 0:
            bundle = json.loads((HERE / f"bundle.{gid.upper()}.json").read_text())
            got = sum(1 for r in bundle["rules"] if r.get("deontic") == "permission")
        assert int(m.group(1)) == got, (
            f"{gid}: copy says {m.group(0)!r}, data says {got}")


# --------------------------------------------------------------------------
# The checker page (/check) has its own worked cases, separate from /demo's.
# They were silently lost once already -- a stylesheet replacement removed the
# dropdown, the EXAMPLES array and its wiring together, and nothing failed
# because every test pointed at /demo. These assert the checker offers cases
# for every guideline /demo does, and that each one actually runs.
# --------------------------------------------------------------------------
UI = (HERE / "ui.html").read_text(encoding="utf-8")


def _ui_example_guidelines():
    block = re.search(r"const GUIDELINE_EXAMPLES = \{(.*?)\n\};", UI, re.S)
    assert block, "ui.html has no GUIDELINE_EXAMPLES at all"
    return {m.group(1): len(re.findall(r"label:", m.group(2)))
            for m in re.finditer(r'"([a-z0-9_-]+)":\s*\[(.*?)\n  \]',
                                 block.group(1), re.S)}


def test_the_checker_still_has_its_dropdown():
    """The select, the array and the wiring were removed together once."""
    for needle in ('id="example"', "const EXAMPLES = [", "GUIDELINE_EXAMPLES",
                   "populateExamples()", "loadExample("):
        assert needle in UI, f"ui.html lost {needle!r}"


def test_the_checker_offers_cases_for_every_guideline_the_demo_does():
    demo_ids = {gid for gid, _ in _doc_notes()} - {"fixtures"}
    ui_ids = set(_ui_example_guidelines())
    missing = demo_ids - ui_ids
    assert not missing, (
        f"/check has no worked cases for {sorted(missing)}, so those "
        f"guidelines open with empty {{}} boxes and unguessable variable names"
    )


@pytest.mark.parametrize("gid", sorted(_ui_example_guidelines()))
def test_each_guideline_has_more_than_one_case(gid):
    assert _ui_example_guidelines()[gid] >= 2, (
        f"{gid} has only one worked case; the point is to contrast verdicts")
