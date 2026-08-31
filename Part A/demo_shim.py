"""Index + model hooks, wired the same way demo.py wires them.

Exists so the benchmarks do not re-implement index construction and drift from
what the pipeline actually runs -- a benchmark measuring a differently-built
index measures the wrong thing."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

def wire(live: bool):
    import bind
    import demo
    if not live:
        return bind.fixture_index(), None, None
    import llm
    return demo._build_index(True), llm.propose_mentions, llm.select_candidate
