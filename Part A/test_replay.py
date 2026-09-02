"""Replay a real recorded run of the whole pipeline, offline.

This is the test that would have caught every defect in the `Delta` work. Each
one -- the empty `events: []`, the duplicate CQL identifier, the boolean merged
into a real, the node never offered at all -- needed a REAL document, a REAL
model response and the FULL stage chain to appear. Unit tests over hand-built
objects cannot produce that combination, so the user produced it, one paid run
at a time, and each symptom looked like a fresh unrelated bug.

`demo.py --live` now tapes its model responses and its statement list. This
replays them: same statements, same model outputs, same stages, no network.

Skips cleanly when no tape exists, so the suite still runs on a fresh clone.
Record one with:

    python3 demo.py --live --doc PMC13074134 --max 5 --out /tmp/b.json
"""
import glob
import json
import os
import re
import sys

import pytest

import bind
import cassette
import cql
import dsl
import gate

HERE = os.path.dirname(os.path.abspath(__file__))
TAPES = os.path.join(HERE, "cassettes")
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "Part B"))

def _tapes():
    return sorted(p for p in glob.glob(os.path.join(TAPES, "*.jsonl"))
                  if os.path.exists(p.replace(".jsonl", ".statements.json")))

pytestmark = pytest.mark.skipif(
    not _tapes(), reason="no recorded run; see the module docstring to make one")

@pytest.fixture(params=_tapes() or [None], ids=lambda p: os.path.basename(p or "-"))
def replayed(request):
    """Every triage from one recorded run, produced offline."""
    tape = request.param
    side = json.load(open(tape.replace(".jsonl", ".statements.json"), encoding="utf-8"))
    old = dict(os.environ)
    os.environ[cassette.ENV_PATH] = tape
    os.environ[cassette.ENV_MODE] = "replay"
    cassette.reset()
    try:
        import llm
        index = bind.SqliteIndex(os.path.join(HERE, "terms.db"))
        out = []
        for sid, text in side["statements"]:
            try:
                out.append(gate.triage(
                    text, index, statement_id=sid,
                    mention_llm=llm.propose_mentions,
                    select_llm=llm.select_candidate,
                    formalize=lambda t, r: llm.formalize(
                        t, r, effort="medium", abbrevs=side.get("abbrevs")),
                    source_span=text, doc_abbrevs=side.get("abbrevs")))
            except cassette.CassetteMiss as miss:
                # SKIP, never pass. The prompt this code now sends is not the
                # prompt that was taped -- usually because the registry or the
                # instructions changed -- so the tape cannot answer for it. A
                # green run here would be a lie, and briefly was one: the miss
                # was being caught upstream and returned as a refusal, so the
                # harness reported success while replaying nothing.
                pytest.skip(f"tape is stale, re-record with demo.py --live: {miss}")
        yield out
    finally:
        os.environ.clear(); os.environ.update(old)
        cassette.reset()

def test_the_recorded_run_still_completes(replayed):
    """Not 'produces good rules' -- just that the stage chain does not raise on
    real input. Three separate runs died here, on input a unit test never had."""
    assert replayed, "the tape produced no triages at all"

def test_every_rule_still_validates_against_its_own_registry(replayed):
    """A rule shipped against a registry that does not contain its variables is
    the R17a bug, and it has recurred at three different call sites."""
    for t in replayed:
        if t.rule is None or t.registry is None:
            continue
        errs = [i for i in dsl.validate_rule(t.rule, t.registry)
                if i.severity == "error"]
        assert not errs, f"{t.statement_id}: {[(i.code, i.message) for i in errs]}"

def test_every_rule_lowers_to_cql_without_a_duplicate_define(replayed):
    """`Identifier "..." is already in use` -- the library did not translate at
    all, and nothing upstream noticed."""
    for t in replayed:
        if t.rule is None or t.registry is None:
            continue
        reg = t.registry
        stubs = dsl.hole_defs(reg, t.open_holes, t.rule)
        if stubs:
            reg = dsl.Registry([reg[n] for n in reg.names()] + stubs,
                               [reg.event(e) for e in reg.event_names()])
        lib = cql.rule_to_cql(t.rule, reg, library_name=f"R{abs(hash(t.statement_id))}")
        errs = [i.message for i in cql.check_cql(lib.text) if i.severity == "error"]
        assert not errs, f"{t.statement_id}: {errs[:2]}"
        defs = re.findall(r'^define "([^"]+)"', lib.text, re.M)
        assert len(defs) == len(set(defs)), f"{t.statement_id}: duplicate define"

def test_the_bundle_round_trips_into_track_b(replayed, tmp_path):
    """The bundle is the Track A/B interface. A rule Track B rejects at load is
    a rule that was never delivered, however green Track A looked."""
    from from_dsl import load_bundle
    rules = [t.rule for t in replayed if t.rule is not None]
    if not rules:
        pytest.skip("this recorded run encoded nothing")
    seen, events = {}, {}
    for t in replayed:
        if t.rule is None or t.registry is None:
            continue
        for n in t.registry.names():
            seen.setdefault(n, t.registry[n])
        for e in dsl.events_in(t.rule):
            events.setdefault(e, (t.registry.event(e)
                                  if e in t.registry.event_names()
                                  else dsl.EventDef(e, e.replace("_", " "))))
        reg_so_far = dsl.Registry(list(seen.values()), list(events.values()))
        for v in dsl.hole_defs(reg_so_far, t.open_holes, t.rule):
            seen.setdefault(v.name, v)
    reg = dsl.Registry(list(seen.values()), list(events.values()))
    out = str(tmp_path / "bundle.json")
    dsl.write_bundle(out, reg, rules, [])
    _loaded, _refusals, _reg, rejected = load_bundle(out)
    assert not rejected, f"Track B rejected: {rejected}"
