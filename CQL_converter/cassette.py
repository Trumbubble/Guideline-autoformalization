"""Record every model response once; replay them offline forever after.

A cassette records one real run so the same document, statements, and model
outputs replay deterministically and for free in tests, catching regressions
before anyone spends another API call. Because `gate` relies on K
identical-prompt calls actually disagreeing, responses are stored as a list
per key rather than collapsed to one value, and a prompt with no recorded
response raises `CassetteMiss` in replay mode instead of silently falling
through to the network.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading

ENV_PATH = "GUIDELINE_CASSETTE"
ENV_MODE = "GUIDELINE_CASSETTE_MODE"

class CassetteMiss(KeyError):
    """A prompt with no recorded response, in replay mode."""

_LOCK = threading.Lock()
_TAPE: dict | None = None
_TAPE_PATH: str | None = None
_CURSOR: dict[str, int] = {}

def key_for(model: str, system: str, user: str, schema: dict) -> str:
    """Stable across processes: no id()s, no dict ordering."""
    blob = json.dumps({"model": model, "system": system, "user": user,
                       "schema": schema}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()

def mode() -> str:
    """'record', 'replay', or 'off'."""
    if not os.environ.get(ENV_PATH):
        return "off"
    m = (os.environ.get(ENV_MODE) or "replay").lower()
    if m not in {"record", "replay"}:
        raise ValueError(f"{ENV_MODE} must be record|replay, got {m!r}")
    return m

def path() -> str:
    return os.environ[ENV_PATH]

def _load() -> dict:
    global _TAPE, _TAPE_PATH, _CURSOR
    p = path()
    if _TAPE is not None and _TAPE_PATH == p:
        return _TAPE
    tape: dict[str, list] = {}
    if os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                tape.setdefault(row["key"], []).append(row["response"])
    _TAPE, _TAPE_PATH, _CURSOR = tape, p, {}
    return tape

def replay(key: str, *, note: str = "") -> dict:
    """The next recorded response for this prompt. Cycles once exhausted, so a
    tape recorded at K=8 still serves a K=16 run rather than crashing."""
    with _LOCK:
        tape = _load()
        if key not in tape:
            raise CassetteMiss(
                f"no recorded response for this prompt ({note[:80]!r}). The "
                f"tape at {path()} is stale: re-record it, or the test is "
                f"asserting against a prompt the code no longer sends.")
        seq = tape[key]
        i = _CURSOR.get(key, 0)
        _CURSOR[key] = i + 1
        return seq[i % len(seq)]

def record(key: str, response: dict, *, note: str = "") -> None:
    with _LOCK:
        with open(path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"key": key, "note": note[:200],
                                 "response": response}, ensure_ascii=False) + "\n")
        global _TAPE
        _TAPE = None          # force a reload if this process also replays

def reset() -> None:
    """Drop the in-memory tape. For tests that swap cassettes."""
    global _TAPE, _TAPE_PATH, _CURSOR
    with _LOCK:
        _TAPE, _TAPE_PATH, _CURSOR = None, None, {}

def stats() -> dict:
    tape = _load()
    return {"prompts": len(tape), "responses": sum(len(v) for v in tape.values())}
