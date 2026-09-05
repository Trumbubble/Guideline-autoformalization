"""The demo page's sample patients must actually run.

demo.html hard-codes rule ids and patient/plan payloads so a visitor can click
a scenario and see a verdict without first learning a guideline's variable
names. Those ids are only valid for the bundle that produced them: re-running a
document through the pipeline renumbers its rules and can rebind an antecedent
to a different variable, at which point a scenario silently starts returning
"unknown rule_id" in the browser and nothing in the test suite notices.

That is exactly what happened when PMC12355350 was re-run over the full
document: PMC12355350:0049 ceased to exist, its successor bound
`has_higher_risk` where the scenario still sent `has_cardiovascular_disease_cvd`,
and the page broke in a way no unit test could see because every unit test
builds its own rules by hand.

So this walks demo.html itself, and for every scenario asserts the rule exists
and still returns the verdict the page advertises.
"""
import json
import re
from pathlib import Path

import pytest

DEMO = Path(__file__).parent / "demo.html"


def _js_object_to_json(text: str) -> str:
    """The scenarios are JS object literals: bare keys, trailing commas."""
    text = re.sub(r"([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:", r'\1"\2":', text)
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    return text


def _scenarios():
    """-> [(guideline_id, label, expect, body), ...] read from demo.html."""
    source = DEMO.read_text(encoding="utf-8")
    out = []
    # each doc block starts `id: "<guideline>"` and owns the scenarios after it
    for block in re.split(r'\n\s*\{\s*\n\s*id: "', source)[1:]:
        gid = block.split('"', 1)[0]
        # Two shapes: the guideline scenarios advertise a verdict via
        # `expect`, the fixture scenarios have none (they run against all ten
        # rules at once, so no single verdict is promised). Both must at least
        # execute; only the first kind has a verdict to check.
        for m in re.finditer(
            r'label:\s*"([^"]*)",\s*(?:expect:\s*"([^"]*)",\s*)?body:\s*(\{.*?\}\s*\})\s*,?\s*\}',
            block, re.S):
            label, expect, body = m.group(1), m.group(2), m.group(3)
            try:
                parsed = json.loads(_js_object_to_json(body))
            except json.JSONDecodeError as exc:      # a malformed scenario is a bug
                pytest.fail(f"{gid}/{label}: body is not parseable ({exc}): {body}")
            out.append((gid, label, expect, parsed))
    return out


@pytest.fixture(scope="module")
def client():
    import api
    api.app.config["TESTING"] = True
    return api.app.test_client()


SCENARIOS = _scenarios()


def test_the_page_actually_declares_scenarios():
    """A parser that silently matches nothing would make every test below
    vacuously pass, which is the failure this whole file exists to prevent."""
    assert len(SCENARIOS) >= 8, f"only parsed {len(SCENARIOS)} scenarios from demo.html"
    assert any(e for _, _, e, _ in SCENARIOS), "no scenario advertises a verdict"


@pytest.mark.parametrize("gid,label,expect,body", SCENARIOS,
                         ids=[f"{g}:{l}" for g, l, _, _ in SCENARIOS])
def test_every_demo_scenario_returns_the_verdict_it_advertises(
        client, gid, label, expect, body):
    response = client.post(f"/guidelines/{gid}/check", json=body)
    assert response.status_code == 200, f"{gid}/{label}: HTTP {response.status_code}"

    payload = response.get_json()
    results = payload if isinstance(payload, list) else payload.get("results", [payload])
    assert results, f"{gid}/{label}: no result returned"

    first = results[0]
    assert "error" not in first, f"{gid}/{label}: {first['error']}"
    # A scenario with no advertised verdict still has to run -- reaching here
    # already proves the rule id resolved and the checker returned a result.
    if expect:
        assert first.get("verdict") == expect, (
            f"{gid}/{label}: page says {expect}, checker says {first.get('verdict')}"
        )
