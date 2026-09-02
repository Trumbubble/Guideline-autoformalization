"""The guideline library: the fixture set plus any uploaded, processed
guidelines, each its own dsl bundle. Uses Part A's segment/gate/bind/dsl
modules to turn an uploaded document into a bundle -- nothing here
duplicates that logic, and nothing is written into Part A. See CHECKER.md."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

_PART_A = str(Path(__file__).resolve().parent.parent / "Part A")
if _PART_A not in sys.path:
    sys.path.insert(0, _PART_A)

import bind  # noqa: E402
import dsl  # noqa: E402
import gate  # noqa: E402
import segment  # noqa: E402

from real_rules import REAL_RULES

STORE_DIR = Path(__file__).parent / "guidelines"
INDEX_PATH = STORE_DIR / "index.json"
FIXTURES_ID = "fixtures"


class IngestError(RuntimeError):
    """Raised rather than returning an empty bundle -- a failed upload must
    not look like a guideline with zero rules."""


def _load_index() -> list:
    if not INDEX_PATH.exists():
        return []
    with INDEX_PATH.open() as fh:
        return json.load(fh)


def _save_index(entries: list) -> None:
    STORE_DIR.mkdir(exist_ok=True)
    with INDEX_PATH.open("w") as fh:
        json.dump(entries, fh, indent=1)


def _slug(title: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "guideline"
    return f"{base}-{uuid.uuid4().hex[:6]}"


def _blocks_from_text(text: str, doc_id: str) -> list:
    """Plain pasted text -> one Block per paragraph.

    Not a Part A loader: load_pdf/load_html/load_jats all recover real
    document layout, and pasted text has none to recover. This is the
    minimal honest stand-in, one step below what segment.py does for a
    structured document.
    """
    blocks = []
    pos = 0
    for i, para in enumerate(re.split(r"\n\s*\n", text)):
        para = para.strip()
        if not para:
            continue
        start = text.find(para, pos)
        if start < 0:
            start = pos
        end = start + len(para)
        pos = end
        blocks.append(
            segment.Block(
                text=para,
                kind="section_text",
                prov=segment.Provenance(
                    doc_id=doc_id, locator=f"p{i}", char_start=start, char_end=end
                ),
            )
        )
    return blocks


def _blocks_for_upload(doc_id, *, file_bytes=None, filename=None, text=None) -> list:
    if text is not None:
        return _blocks_from_text(text, doc_id)

    ext = (filename or "").rsplit(".", 1)[-1].lower()
    if ext == "pdf":
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
            fh.write(file_bytes)
            path = fh.name
        try:
            return segment.load_pdf(path, doc_id)
        finally:
            os.unlink(path)
    if ext in ("html", "htm"):
        return segment.load_html(file_bytes, doc_id)
    if ext == "xml":
        return segment.load_jats(file_bytes, doc_id)
    return _blocks_from_text(file_bytes.decode("utf-8", errors="replace"), doc_id)


@contextlib.contextmanager
def _temporary_api_key(api_key: str | None):
    """Use `api_key` for ANTHROPIC_API_KEY for the duration of one call, then
    restore whatever was there before -- so a key typed into the browser is
    used for exactly the upload that carried it and never lingers in the
    server process afterward. A blank/missing key is a no-op: the pipeline
    falls back to whatever `llm.load_env()` already put in the environment
    (a server-side `.env`), unchanged.

    `llm.client()` caches its Anthropic client in a module global the first
    time it's called, so switching keys between requests needs that cache
    dropped too, or the second request would silently keep using the first
    request's key."""
    if not api_key:
        yield
        return
    import llm

    previous = os.environ.get("ANTHROPIC_API_KEY")
    os.environ["ANTHROPIC_API_KEY"] = api_key
    llm._client = None
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = previous
        llm._client = None


_AUTH_ERROR_MARKERS = ("authentication_error", "invalid_api_key", "invalid x-api-key")


def _looks_like_auth_failure(refusals) -> bool:
    """True if every refusal looks like the model call itself failed on
    credentials, not like the statement genuinely couldn't be encoded --
    e.g. a bad or expired key rejects every single formalize() call
    identically, which reads very differently from real content refusals."""
    if not refusals:
        return False
    return all(
        any(marker in (r.reason_text or "").lower() for marker in _AUTH_ERROR_MARKERS)
        for r in refusals
    )


def server_has_api_key() -> bool:
    """Whether the server's own environment already has a usable key --
    e.g. from the repo-root `.env` -- so the UI can tell a user whether
    pasting their own key is necessary or just an override."""
    import llm

    llm.load_env()
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _events_in(rule) -> list:
    """Delegates. This was a third copy of the same walker (demo.py had one,
    cql.py had another) and it handled `Since` only, so a rule with a `Delta`
    shipped with `events: []` and was rejected at load as UNKNOWN_EVENT. One
    walker, in the module that owns the node types."""
    return list(dsl.events_in(rule))


def _merged_registry(triages, index, mention_llm, select_llm) -> "dsl.Registry":
    """One registry a bundle can carry: every triage's own bound vocabulary,
    every open hole, and every event a surviving rule refers to. Same
    construction as demo.py's `_merge_registries` -- duplicated rather than
    imported, since demo.py is a script, not a library, and Part A is not to
    be modified for this."""
    seen, events = {}, {}
    for t in triages:
        if not t.encodable:
            continue
        bindings, _ = bind.bind_statement(t.text, index, mention_llm, select_llm)
        for v in bind.registry_from_bindings(bindings)._v.values():
            seen.setdefault(v.name, v)
        for h in t.open_holes:
            seen.setdefault(h, dsl.VarDef(h, "bool", None, h, unbound=True))
        if t.rule is not None:
            for e in _events_in(t.rule):
                events.setdefault(e, dsl.EventDef(e, e.replace("_", " ")))
    return dsl.Registry(list(seen.values()), list(events.values()))


def ingest(title: str, *, file_bytes=None, filename=None, text=None,
           live: bool = False, api_key: str | None = None) -> dict:
    """Turn an uploaded document (or pasted text) into a stored bundle.

    Runs Part A's real pipeline -- segment -> gate.triage (bind + formalize)
    per statement -> dsl.write_bundle -- exactly as demo.py does, just over
    an arbitrary uploaded document instead of a fixed statement list.

    Formalizing a statement into a checkable dsl.Rule is a model step
    (gate.triage returns no rule without `formalize`); `live=False` still
    segments and triages so the upload reports something instead of nothing,
    but produces zero rules and only the refusals pure binder evidence can
    support. Pass live=True to actually synthesize rules -- this needs
    ANTHROPIC_API_KEY, either already in the server's environment or passed
    per-call as `api_key` (e.g. typed into the browser), which is used only
    for this call and never persisted -- see `_temporary_api_key`.
    """
    if not (file_bytes or text):
        raise IngestError("no document provided")

    guideline_id = _slug(title)
    blocks = _blocks_for_upload(
        guideline_id, file_bytes=file_bytes, filename=filename, text=text
    )
    if not blocks:
        raise IngestError("no text could be extracted from the upload")

    stmts, manifest = segment.segment(blocks, guideline_id)

    mention_llm = select_llm = formalize = None
    if live:
        import llm

        mention_llm, select_llm = llm.propose_mentions, llm.select_candidate
        formalize = lambda t, r: llm.formalize(t, r, effort="medium")

    index = bind.fixture_index()
    with _temporary_api_key(api_key if live else None):
        triages = [
            gate.triage(
                s.text, index, statement_id=s.statement_id,
                mention_llm=mention_llm, select_llm=select_llm, formalize=formalize,
                source_page=s.prov.page, source_span=s.text,
            )
            for s in stmts
        ]

        rules, refusals = [], []
        needs_model = 0
        for t in triages:
            if t.rule is not None:
                rules.append(t.rule)
                continue
            ref = t.as_refusal()
            if ref is not None:
                refusals.append(ref)
            elif t.decision == "encode":
                needs_model += 1  # binder evidence alone can't say encode or refuse

        reg = _merged_registry(triages, index, mention_llm, select_llm)

    if live and not rules and _looks_like_auth_failure(refusals):
        raise IngestError(
            "The Anthropic API rejected the key (authentication_error). "
            "Check that it's correct and hasn't expired."
        )

    STORE_DIR.mkdir(exist_ok=True)
    bundle_path = STORE_DIR / f"{guideline_id}.json"
    # Findings travel with the bundle so /guidelines/<id>/report can show the
    # cautions -- a rule that compiled but that a check flagged. Same builder
    # demo.py uses; the alternative is a second copy of it, which is exactly
    # how the same walker bug appeared in three files (RESULTS.md R34).
    import report as _report
    findings = [_report.finding(t, reg) for t in triages]
    dsl.write_bundle(str(bundle_path), reg, rules, refusals, findings)

    # Re-load through the same path get_guideline() uses, so the stored
    # counts match what /guidelines/<id>/rules will actually serve -- a rule
    # that writes clean can still be rejected as VACUOUS_RULE on reload.
    from from_dsl import load_bundle

    loaded_rules, _loaded_refusals, _reg, rejected = load_bundle(str(bundle_path))

    entry = {
        "id": guideline_id,
        "title": title,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_filename": filename,
        "live": live,
        "statements": manifest.get("statements", 0),
        "rule_count": len(loaded_rules),
        "refusal_count": len(refusals),
        "needs_model_count": needs_model,
        "rejected_count": len(rejected),
    }
    entries = _load_index()
    entries.append(entry)
    _save_index(entries)
    return entry


def register_bundle(bundle_path, title: str | None = None,
                    guideline_id: str | None = None) -> dict:
    """Put a bundle Track A wrote on disk into the library the UI serves.

    demo.py and this module both produce bundles, but only this one's landed in
    the store -- so a document formalized from the CLI was invisible to the
    dashboard, and its report (which the UI has a route for) unreachable. The
    two halves were never actually connected.

    Copies rather than referencing in place: the store is what `get_guideline`
    reads, and a bundle that can be moved or overwritten out from under the
    index is a stale-read waiting to happen. Re-registering the same id
    replaces it, so re-running a document updates the library instead of
    stacking duplicates."""
    from from_dsl import load_bundle

    src = Path(bundle_path)
    if not src.exists():
        raise IngestError(f"no bundle at {src}")
    stem = src.stem
    guideline_id = guideline_id or _slug(stem.replace("bundle.", "") or "bundle")
    title = title or stem.replace("bundle.", "").replace("_", " ")

    STORE_DIR.mkdir(exist_ok=True)
    dest = STORE_DIR / f"{guideline_id}.json"
    shutil.copyfile(src, dest)

    with dest.open(encoding="utf-8") as fh:
        raw = json.load(fh)
    loaded_rules, refusals, _reg, rejected = load_bundle(str(dest))
    entry = {
        "id": guideline_id,
        "title": title,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_filename": src.name,
        "live": True,
        "statements": len(raw.get("findings") or []) or None,
        "rule_count": len(loaded_rules),
        "refusal_count": len(refusals),
        "needs_model_count": 0,
        "rejected_count": len(rejected),
    }
    entries = [e for e in _load_index() if e.get("id") != guideline_id]
    _save_index(entries + [entry])
    return entry


def list_guidelines() -> list:
    fixture_entry = {
        "id": FIXTURES_ID,
        "title": "Hand-authored fixtures",
        "created_at": None,
        "source_filename": None,
        "live": False,
        "statements": None,
        "rule_count": len(REAL_RULES),
        "refusal_count": 0,
        "needs_model_count": 0,
        "rejected_count": 0,
        "builtin": True,
    }
    return [fixture_entry] + [dict(e, builtin=False) for e in _load_index()]


def get_guideline(guideline_id: str):
    """-> (rules, refusals, registry_or_None, meta), or None if unknown.

    Fixture rules already carry var_refs/predicate as a SpecClause; a Track A
    bundle needs `from_dsl.load_bundle` to lower it the same way.
    """
    if guideline_id == FIXTURES_ID:
        meta = next(g for g in list_guidelines() if g["id"] == FIXTURES_ID)
        return list(REAL_RULES), [], None, meta

    meta = next((e for e in _load_index() if e["id"] == guideline_id), None)
    if meta is None:
        return None
    from from_dsl import load_bundle

    bundle_path = STORE_DIR / f"{guideline_id}.json"
    # Track B re-verifies before it loads: a bundle can be written by an
    # older Track A or hand-edited, and a VACUOUS_RULE that survived doesn't
    # fail loudly -- it reports CERTIFIED-compliant for everyone. Rejects
    # are OUR defect, not the guideline's residue, so they travel separately
    # from refusals rather than being folded into that count.
    rules, refusals, reg, rejected = load_bundle(str(bundle_path))
    return rules, refusals, reg, dict(meta, builtin=False, rejected=rejected)
