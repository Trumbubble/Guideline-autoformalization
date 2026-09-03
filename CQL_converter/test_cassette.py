"""The tape has to be trustworthy, or a green suite means nothing."""
import json
import os

import pytest

import cassette

@pytest.fixture
def tape(tmp_path, monkeypatch):
    p = tmp_path / "t.jsonl"
    monkeypatch.setenv(cassette.ENV_PATH, str(p))
    cassette.reset()
    yield str(p)
    cassette.reset()

SCHEMA = {"type": "object"}

def test_a_recorded_response_replays_verbatim(tape, monkeypatch):
    monkeypatch.setenv(cassette.ENV_MODE, "record")
    k = cassette.key_for("m", "sys", "user", SCHEMA)
    cassette.record(k, {"choice": 2, "reason": "x"})
    monkeypatch.setenv(cassette.ENV_MODE, "replay")
    cassette.reset()
    assert cassette.replay(k) == {"choice": 2, "reason": "x"}

def test_k_samples_replay_in_order_not_collapsed(tape, monkeypatch):
    """`gate` calls the formalizer K times with an IDENTICAL prompt and reads
    the SPREAD of answers as the vagueness signal. A cassette that returned one
    value per prompt would make every statement agree 1.00 -- a fixture that
    silently deletes the measurement it exists to preserve."""
    monkeypatch.setenv(cassette.ENV_MODE, "record")
    k = cassette.key_for("m", "s", "u", SCHEMA)
    for i in range(3):
        cassette.record(k, {"n": i})
    monkeypatch.setenv(cassette.ENV_MODE, "replay")
    cassette.reset()
    assert [cassette.replay(k)["n"] for _ in range(3)] == [0, 1, 2]
    assert cassette.replay(k)["n"] == 0, "cycles rather than crashing at K+1"

def test_a_missing_prompt_is_an_error_not_a_silent_fallthrough(tape, monkeypatch):
    """A stale tape must fail loudly. Falling through to the network would make
    a passing test mean nothing, and returning {} would be this codebase's
    signature bug -- a failure that returns a usable value."""
    monkeypatch.setenv(cassette.ENV_MODE, "replay")
    with pytest.raises(cassette.CassetteMiss):
        cassette.replay(cassette.key_for("m", "s", "never recorded", SCHEMA))

def test_the_key_ignores_nothing_that_changes_the_answer(tape):
    base = cassette.key_for("m", "sys", "user", SCHEMA)
    assert cassette.key_for("other", "sys", "user", SCHEMA) != base, "model"
    assert cassette.key_for("m", "other", "user", SCHEMA) != base, "system"
    assert cassette.key_for("m", "sys", "other", SCHEMA) != base, "user"
    assert cassette.key_for("m", "sys", "user", {"type": "string"}) != base, "schema"
    # and it is stable across dict ordering, or a replay would miss at random
    a = cassette.key_for("m", "s", "u", {"a": 1, "b": 2})
    b = cassette.key_for("m", "s", "u", {"b": 2, "a": 1})
    assert a == b

def test_mode_is_off_unless_a_path_is_set(monkeypatch):
    monkeypatch.delenv(cassette.ENV_PATH, raising=False)
    assert cassette.mode() == "off"

def test_llm_replays_without_a_network_call(tape, monkeypatch):
    """The wiring, not just the store: ask_json must return the taped value
    before it builds a request."""
    import llm
    monkeypatch.setenv(cassette.ENV_MODE, "record")
    k = cassette.key_for(llm.DEFAULT_MODEL, "S", "U", SCHEMA)
    cassette.record(k, {"ok": True})
    monkeypatch.setenv(cassette.ENV_MODE, "replay")
    cassette.reset()
    monkeypatch.setattr(llm, "client", lambda: (_ for _ in ()).throw(
        AssertionError("ask_json reached the network in replay mode")))
    assert llm.ask_json("S", "U", SCHEMA, model_id=llm.DEFAULT_MODEL) == {"ok": True}
