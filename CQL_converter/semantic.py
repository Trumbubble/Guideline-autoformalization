"""Semantic terminology retrieval: matching on MEANING, not on letters.

Keyword search can't bridge guideline prose and clinical terminology when a
pair shares no letters at all ("kidney function" vs. "Renal function" score
0.93 with no common token), so this module embeds terms into a meaning
vector and searches nearest-neighbour instead. It's a separate module so
`torch` never enters `bind.py`'s import path, and `bind` only imports it as
a fallback once lexical search has already failed.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import threading

# Biomedical by default. Measured on the pairs above, a general-purpose model
# (all-MiniLM-L6-v2) handles morphology well -- kidney/renal 0.95 -- and domain
# synonymy badly: BP-lowering/antihypertensive 0.36 against PubMedBERT's 0.53,
# CGM/glucose 0.32 against 0.52. MiniLM is 8x faster to index (2.3 min vs 18)
# and remains the right choice when the index is being rebuilt often.
DEFAULT_MODEL = "NeuML/pubmedbert-base-embeddings"
FAST_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

# Below this, a "nearest" term is not a match -- it is the closest of a bad lot.
# The whole point of the module is to add candidates the lexical index missed,
# so a floor that admits noise would spend the selector's attention rather than
# save it.
MIN_SIMILARITY = 0.45

class SemanticUnavailable(RuntimeError):
    """Dependencies, model weights or a built index are missing. Raised rather
    than returning [] so a caller cannot mistake "not installed" for "no such
    concept" -- the failure class this project keeps rediscovering."""

class SemanticIndex:
    """Vector search over the local terminology, cached to disk per kind."""

    def __init__(self, db_path: str, model: str = DEFAULT_MODEL,
                 cache_dir: str | None = None, device: str | None = None):
        self.db_path = db_path
        self.model_name = model
        self.cache_dir = cache_dir or os.path.join(
            os.path.dirname(os.path.abspath(db_path)), "semantic")
        self._device = device
        self._model = None
        self._tok = None
        self._loaded: dict[str, tuple] = {}
        self._lock = threading.RLock()

    # -- model ---------------------------------------------------------
    def _ensure_model(self):
        # Locked for the same reason the caller-side flag is: two threads both
        # loading a 400 MB model is waste, and a half-assigned `self._model`
        # read by the other is worse.
        with self._lock:
            if self._model is not None:
                return
            self._load_model()

    def _load_model(self):
        try:
            # transformers prints a weight-loading progress bar and an
            # "embeddings.position_ids | UNEXPECTED" table to stderr. Both are
            # benign -- position_ids is a buffer this architecture does not use
            # -- but they land in the middle of the pipeline's own output and
            # read as an error. Quietened here rather than by the caller, so
            # every entry point gets it.
            os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
            os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
            os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
            import torch
            import transformers
            transformers.logging.set_verbosity_error()
            from transformers import AutoModel, AutoTokenizer
        except Exception as exc:
            raise SemanticUnavailable(
                f"torch/transformers not importable: {exc}") from exc
        try:
            self._tok = AutoTokenizer.from_pretrained(self.model_name)
            self._model = AutoModel.from_pretrained(self.model_name).eval()
        except Exception as exc:
            raise SemanticUnavailable(
                f"could not load {self.model_name!r}: {exc}") from exc
        if self._device is None:
            self._device = ("mps" if torch.backends.mps.is_available()
                            else "cuda" if torch.cuda.is_available() else "cpu")
        self._model = self._model.to(self._device)

    def embed(self, texts: list[str], batch: int = 256):
        """Mean-pooled, L2-normalised, so cosine similarity is a dot product."""
        self._ensure_model()
        import torch
        out = []
        for i in range(0, len(texts), batch):
            chunk = texts[i:i + batch]
            b = self._tok(chunk, padding=True, truncation=True, max_length=24,
                          return_tensors="pt").to(self._device)
            with torch.no_grad():
                h = self._model(**b).last_hidden_state
            m = b["attention_mask"].unsqueeze(-1).float()
            v = (h * m).sum(1) / m.sum(1).clamp(min=1e-9)
            out.append(torch.nn.functional.normalize(v, dim=1).cpu())
        if out:
            return torch.cat(out)
        # Zero rows, but the model's OWN width -- hardcoding 384 was right for
        # MiniLM and wrong for PubMedBERT (768), so an empty batch would have
        # produced a matrix that fails to multiply against the index.
        width = int(self._model.config.hidden_size)
        return torch.zeros((0, width))

    # -- index ---------------------------------------------------------
    def _paths(self, kind: str) -> tuple[str, str]:
        tag = self.model_name.split("/")[-1]
        base = os.path.join(self.cache_dir, f"{kind}.{tag}")
        return base + ".npy", base + ".tsv"

    def build(self, kind: str, verbose: bool = True) -> int:
        """Embed every term of one kind. One-time; cached to disk.

        Per KIND rather than all at once: a run touches the two or three kinds
        its mentions actually use, and paying 18 minutes for `condition` when
        the statement only needs `lab` is waste that would stop anyone using
        this."""
        import numpy as np
        vec_path, meta_path = self._paths(kind)
        os.makedirs(self.cache_dir, exist_ok=True)
        con = sqlite3.connect(self.db_path)
        rows = con.execute(
            "SELECT term, system, code FROM concept WHERE kind=?", (kind,)).fetchall()
        con.close()
        if not rows:
            raise SemanticUnavailable(f"no {kind!r} terms in {self.db_path}")
        if verbose:
            print(f"  embedding {len(rows):,} {kind} terms with "
                  f"{self.model_name.split('/')[-1]}...", file=sys.stderr)
        vecs = self.embed([r[0] for r in rows]).numpy().astype("float32")
        np.save(vec_path, vecs)
        with open(meta_path, "w", encoding="utf-8") as fh:
            for term, system, code in rows:
                fh.write(f"{code}\t{system}\t{term}\n")
        if verbose:
            print(f"  wrote {vec_path} ({vecs.nbytes / 1e6:.0f} MB)", file=sys.stderr)
        return len(rows)

    def _load(self, kind: str):
        with self._lock:
            if kind in self._loaded:
                return self._loaded[kind]
            import numpy as np
            vec_path, meta_path = self._paths(kind)
            if not (os.path.exists(vec_path) and os.path.exists(meta_path)):
                raise SemanticUnavailable(
                    f"no semantic index for {kind!r}; run "
                    f"`python3 semantic.py --build --kind {kind}`")
            vecs = np.load(vec_path)
            meta = [line.rstrip("\n").split("\t", 2)
                    for line in open(meta_path, encoding="utf-8")]
            self._loaded[kind] = (vecs, meta)
            return self._loaded[kind]

    def available(self, kind: str) -> bool:
        return all(os.path.exists(p) for p in self._paths(kind))

    def search(self, phrase: str, kind: str, k: int = 8,
               min_similarity: float = MIN_SIMILARITY) -> list[tuple]:
        """(code, system, term, similarity), best first. Raises rather than
        returning [] when the index is missing."""
        import numpy as np
        vecs, meta = self._load(kind)
        q = self.embed([phrase]).numpy().astype("float32")[0]
        sims = vecs @ q
        # One code can carry several terms; keep its best and dedupe, or the
        # top-k fills with synonyms of a single concept.
        order = np.argsort(-sims)[:k * 12]
        best: dict[str, tuple] = {}
        for i in order:
            s = float(sims[i])
            if s < min_similarity:
                break
            code, system, term = meta[i]
            if code not in best or s > best[code][3]:
                best[code] = (code, system, term, round(s, 4))
        return sorted(best.values(), key=lambda t: -t[3])[:k]

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="terms.db")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--fast", action="store_true",
                    help=f"use {FAST_MODEL} -- 8x faster to index, weaker on "
                         "domain synonyms")
    ap.add_argument("--kind", action="append",
                    help="repeatable; default is every clinical kind")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--search")
    a = ap.parse_args(argv)
    ix = SemanticIndex(a.db, FAST_MODEL if a.fast else a.model)
    kinds = a.kind or ["lab", "condition", "procedure", "drug", "drug_class"]
    if a.build:
        for kind in kinds:
            try:
                ix.build(kind)
            except SemanticUnavailable as exc:
                print(f"  {kind}: {exc}", file=sys.stderr)
        return 0
    if a.search:
        for kind in kinds:
            try:
                hits = ix.search(a.search, kind)
            except SemanticUnavailable as exc:
                print(f"  {kind}: {exc}")
                continue
            print(f"--- {kind}")
            for code, system, term, sim in hits:
                print(f"   {sim:.3f}  {term[:58]:60s} {system.rsplit('/', 1)[-1]}:{code}")
        return 0
    ap.error("pass --build or --search")

if __name__ == "__main__":
    raise SystemExit(main())
