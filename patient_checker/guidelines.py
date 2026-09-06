"""The guideline library: the fixture set plus any uploaded, processed
guidelines, each its own dsl bundle. Uses CQL_converter's segment/gate/bind/dsl
modules to turn an uploaded document into a bundle -- nothing here
duplicates that logic, and nothing is written into CQL_converter. See CHECKER.md."""

from __future__ import annotations

import concurrent.futures as cf
import contextlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

# Concurrent statements per live upload. Each is ~4 sequential model round
# trips, so this is the wall-clock multiplier; it is bounded because every
# worker holds an API connection and the account's rate limit is shared with
# whatever else is running. Override with GF_LIVE_WORKERS.
_LIVE_WORKERS = max(1, int(os.environ.get("GF_LIVE_WORKERS", "8")))

_PART_A = str(Path(__file__).resolve().parent.parent / "CQL_converter")
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


# ------------------------------------------------------------ live progress
#
# A live ingest can run to hundreds of sequential model calls (one
# statement's worth of the pipeline is a REST round trip, not a function
# call), so it runs in a background thread and reports progress here rather
# than blocking the request for as long as it takes. Keyed by guideline_id;
# an entry is consumed (and removed) the first time a client reads a
# terminal status, so this never accumulates finished runs forever.
_PROGRESS_LOCK = threading.Lock()
_PROGRESS: dict[str, dict] = {}

# guideline_id -> ((mtime_ns, size), loaded) for bundles already lowered.
# See get_guideline for why this exists and how it is invalidated.
_BUNDLE_CACHE_LOCK = threading.Lock()
_BUNDLE_CACHE: dict[str, tuple] = {}


def _progress_set(guideline_id: str, **fields) -> None:
    with _PROGRESS_LOCK:
        _PROGRESS.setdefault(guideline_id, {}).update(fields)


def get_progress(guideline_id: str) -> dict | None:
    """A snapshot of a tracked live ingest, or None -- either this id was
    never run through the tracked (live) path, or a caller already consumed
    its terminal status. Not the source of truth for a finished guideline;
    that's always the stored bundle, read through get_guideline()."""
    with _PROGRESS_LOCK:
        state = _PROGRESS.get(guideline_id)
        if state is None:
            return None
        snapshot = dict(state)
        if snapshot.get("status") in ("done", "error"):
            del _PROGRESS[guideline_id]
        return snapshot


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

    Not a CQL_converter loader: load_pdf/load_html/load_jats all recover real
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
    """Union of the registries the rules were WRITTEN AGAINST. Same
    construction as demo.py's `_merge_registries` -- duplicated rather than
    imported, since demo.py is a script, not a library, and CQL_converter is not to
    be modified for this.

    This copy used to re-bind each statement from scratch, which demo.py's
    original was fixed not to do (RESULTS.md R17a) -- the fix never reached
    here. It is wrong twice over. Correctness: binding runs through a model
    selector, so the second call can disagree with the first, and the
    delivered rule then references a variable the delivered registry does not
    contain (Track B rejects it as UNKNOWN_VAR -- the safety net working, not
    the pipeline working). Cost: it is a second full binding pass, three-ish
    model calls per encodable statement, after every statement has already
    been bound once.

    Re-binding is kept only as the fallback for a Triage from an older run
    that has no registry attached."""
    seen, events = {}, {}
    for t in triages:
        if not t.encodable:
            continue
        if t.registry is not None:
            for n in t.registry.names():
                seen.setdefault(n, t.registry[n])
        else:
            bindings, _ = bind.bind_statement(t.text, index, mention_llm, select_llm)
            for v in bind.registry_from_bindings(bindings)._v.values():
                seen.setdefault(v.name, v)
        # Events BEFORE holes: a `Delta` baseline is derived from an event, and
        # `hole_defs` needs the event declared to recognise it as the lowering's
        # to emit rather than the registry's (RESULTS.md R34).
        if t.rule is not None:
            for e in _events_in(t.rule):
                events.setdefault(e, (t.registry.event(e)
                                      if t.registry is not None
                                      and e in t.registry.event_names()
                                      else dsl.EventDef(e, e.replace("_", " "))))
        _reg_so_far = dsl.Registry(list(seen.values()), list(events.values()))
        for v in dsl.hole_defs(_reg_so_far, t.open_holes, t.rule):
            seen.setdefault(v.name, v)
    return dsl.Registry(list(seen.values()), list(events.values()))


def _build_index(live: bool):
    """Local terms.db plus live remote fallback when `live`, the fixture set
    otherwise -- same construction as demo.py's `_build_index`, duplicated
    rather than imported for the same reason `_merged_registry` is: demo.py
    is a script, not a library, and CQL_converter is not to be modified for
    this. Getting this wrong (fixture_index() regardless of `live`) is why a
    real live upload previously bound against 22 fixture concepts instead of
    terms.db's 634k and came back with everything refused."""
    if not live:
        return bind.fixture_index()
    local = bind.SqliteIndex(os.path.join(_PART_A, "terms.db"))
    for system, loader in [(bind.LOINC, bind.load_loinc),
                            (bind.SNOMED, bind.load_snomed),
                            (bind.RXNORM, bind.load_rxnorm)]:
        if local.version(system):
            continue
        try:
            loader(local)
        except FileNotFoundError:
            pass  # degrades to the free endpoints via RemoteIndex below
    return bind.CachedIndex(local, bind.RemoteIndex())


def _raw_source_path(guideline_id: str, filename: str | None) -> Path:
    """Where the ORIGINAL uploaded bytes/text live, so a guideline first
    ingested with live=False can later be reprocessed with the model without
    re-uploading the document from scratch -- ingest() itself only ever
    persisted the resulting bundle, never the input it was built from."""
    raw_dir = STORE_DIR / "raw"
    if filename:
        return raw_dir / f"{guideline_id}{Path(filename).suffix or '.bin'}"
    return raw_dir / f"{guideline_id}.txt"


def ingest(title: str, *, file_bytes=None, filename=None, text=None,
           live: bool = False, api_key: str | None = None,
           owner: str | None = None) -> dict:
    """Turn an uploaded document (or pasted text) into a stored bundle.

    Runs CQL_converter's real pipeline -- segment -> gate.triage (bind + formalize)
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

    live=False returns the finished entry, same as always. live=True instead
    returns {"id", "status": "processing", "total"} immediately and runs the
    actual per-statement work in a background thread -- a real document can
    be a few hundred sequential model calls, which is not a request any
    caller should sit blocked on. Poll get_progress(guideline_id) for status.
    """
    if not (file_bytes or text):
        raise IngestError("no document provided")
    guideline_id = _slug(title)
    return _ingest_into(
        guideline_id, title, file_bytes=file_bytes, filename=filename,
        text=text, live=live, api_key=api_key, owner=owner,
    )


def reprocess(guideline_id: str, *, api_key: str | None = None) -> dict:
    """Re-run ingest, live, against an existing guideline's ORIGINAL upload,
    in place -- the fix for "uploaded without live, got zero rules, and then
    had no way to go back and process it with the model" (the raw content
    ingest() saves via `_raw_source_path` is what makes this possible; a
    guideline registered from a Track A bundle via `register_bundle` has no
    raw upload behind it and can't be reprocessed this way). Always live, so
    always backgrounded -- see ingest()'s docstring for the returned shape
    and get_progress()."""
    meta = next((e for e in _load_index() if e["id"] == guideline_id), None)
    if meta is None:
        raise IngestError(f"unknown guideline {guideline_id!r}")
    raw_path = _raw_source_path(guideline_id, meta.get("source_filename"))
    if not raw_path.exists():
        raise IngestError(
            "the original upload for this guideline isn't available to "
            "reprocess -- try uploading it again with 'process live' checked"
        )
    if meta.get("source_filename"):
        file_bytes, text = raw_path.read_bytes(), None
    else:
        file_bytes, text = None, raw_path.read_text(encoding="utf-8")
    return _ingest_into(
        guideline_id, meta["title"], file_bytes=file_bytes,
        filename=meta.get("source_filename"), text=text,
        live=True, api_key=api_key,
    )


def _ingest_into(guideline_id: str, title: str, *, file_bytes=None,
                 filename=None, text=None, live: bool = False,
                 api_key: str | None = None, owner: str | None = None) -> dict:
    """Fast part only: extract text, segment it, and either run the rest
    inline (live=False -- no model calls, so there's nothing worth tracking)
    or hand it to a background thread and return a 'processing' marker the
    caller can poll get_progress() against (live=True -- see _run_ingest)."""
    blocks = _blocks_for_upload(
        guideline_id, file_bytes=file_bytes, filename=filename, text=text
    )
    if not blocks:
        raise IngestError("no text could be extracted from the upload")

    raw_path = _raw_source_path(guideline_id, filename)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    if text is not None:
        raw_path.write_text(text, encoding="utf-8")
    else:
        raw_path.write_bytes(file_bytes)

    stmts, manifest = segment.segment(blocks, guideline_id)

    if not live:
        return _run_ingest(guideline_id, title, filename, live, api_key,
                           stmts, manifest, owner=owner)

    total = len(stmts)
    _progress_set(guideline_id, done=0, total=total, status="processing")
    threading.Thread(
        target=_run_ingest_tracked,
        args=(guideline_id, title, filename, live, api_key, stmts, manifest,
              owner),
        daemon=True,
    ).start()
    return {"id": guideline_id, "status": "processing", "total": total}


CQL_DIR = Path(__file__).parent / "cql"


def _lower_and_compile(guideline_id: str, rules, reg) -> dict:
    """Lower every rule to CQL and put it through the REAL HL7 translator.

    Without this the upload path produced no CQL at all, so "executable" was
    a claim about the DSL rather than about anything a site could run, and
    nothing the pipeline emitted had been seen by a compiler. `demo.py --live`
    already gated its output this way (`compiler = toolchain.cql_to_elm`);
    this is the same gate on the path the UI actually uses.

    The translator is a JVM subprocess, ~0.6s per rule, which is nothing
    beside the model calls that produced the rule. Its absence is recorded,
    never silently treated as a pass: a missing toolchain and a clean compile
    must not look alike (RESULTS.md R33a is exactly that bug).
    """
    import cql as _cql

    out = {"cql_written": 0, "cql_compiled": 0, "cql_errors": 0,
           "compiler_available": False}
    if not rules:
        return out
    try:
        import toolchain
        compiler = toolchain.cql_to_elm if toolchain.available() else None
    except Exception:
        compiler = None
    out["compiler_available"] = compiler is not None

    dest = CQL_DIR / guideline_id
    dest.mkdir(parents=True, exist_ok=True)
    for rule in rules:
        try:
            lib = _cql.rule_to_cql(rule, reg)
        except Exception:
            # A rule that cannot be lowered is a real finding, but it must not
            # take the whole upload down with it.
            out["cql_errors"] += 1
            continue
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", rule.rule_id)
        (dest / f"{safe}.cql").write_text(lib.text, encoding="utf-8")
        out["cql_written"] += 1
        if compiler is None:
            continue
        try:
            errs = compiler(lib.text)
        except Exception:
            out["cql_errors"] += 1
            continue
        if errs:
            out["cql_errors"] += 1
        else:
            out["cql_compiled"] += 1
    return out


def _run_ingest(guideline_id, title, filename, live, api_key, stmts,
                manifest, *, track: bool = False, owner: str | None = None) -> dict:
    """The actual pipeline run: gate.triage per statement, then the bundle
    write. Identical to what this function used to do inline; the only
    change is the `track` flag, which reports each statement's completion
    to _PROGRESS so a poller can watch it happen instead of getting one
    result at the end."""
    mention_llm = select_llm = formalize = None
    if live:
        import llm

        mention_llm, select_llm = llm.propose_mentions, llm.select_candidate
        formalize = lambda t, r: llm.formalize(t, r, effort="medium")

    index = _build_index(live)
    total = len(stmts)
    with _temporary_api_key(api_key if live else None):

        def _one(s):
            return gate.triage(
                s.text, index, statement_id=s.statement_id,
                mention_llm=mention_llm, select_llm=select_llm, formalize=formalize,
                source_page=s.prov.page, source_span=s.text,
            )

        # Statements are independent -- one statement's triage reads nothing
        # another writes -- and each is ~4 sequential model round trips
        # (propose mentions, select a code per mention, formalize), so a real
        # document spent almost all of its wall clock waiting on the network
        # one statement at a time. gate.triage already pools internally for
        # K-sampling and llm.USAGE is RLock-guarded, so the accounting holds.
        #
        # Sequential when a cassette is active: record and replay key K samples
        # as an ordered list per prompt, and interleaving threads would write
        # the tape in an order replay cannot reproduce.
        import cassette
        workers = 1 if (cassette.mode() != "off" or not live) else _LIVE_WORKERS
        if workers <= 1:
            triages = []
            for i, s in enumerate(stmts):
                triages.append(_one(s))
                if track:
                    _progress_set(guideline_id, done=i + 1, total=total,
                                  status="processing")
        else:
            triages = [None] * total
            done = 0
            with cf.ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_one, s): i for i, s in enumerate(stmts)}
                for fut in cf.as_completed(futures):
                    # Results land out of order; the bundle must not, so they
                    # go back into their own slot rather than an append order
                    # that would vary run to run.
                    triages[futures[fut]] = fut.result()
                    done += 1
                    if track:
                        _progress_set(guideline_id, done=done, total=total,
                                      status="processing")

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

        # _merged_registry re-binds every encodable statement to build the
        # bundle's vocabulary (see its own docstring) -- real model calls,
        # same as the loop above, just not one-per-statement-visible the
        # same way. Without this, progress looks stalled at 100% for
        # however long this takes, which is exactly the "is it stuck?"
        # question tracking exists to answer.
        if track:
            _progress_set(guideline_id, status="finalizing")
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

    if track:
        _progress_set(guideline_id, status="compiling")
    cql_stats = _lower_and_compile(guideline_id, rules, reg)

    # Re-load through the same path get_guideline() uses, so the stored
    # counts match what /guidelines/<id>/rules will actually serve -- a rule
    # that writes clean can still be rejected as VACUOUS_RULE on reload.
    from from_dsl import load_bundle

    loaded_rules, _loaded_refusals, _reg, rejected = load_bundle(str(bundle_path))

    entry = {
        "id": guideline_id,
        "title": title,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        # Which browser uploaded this; see _visible_to(). None means the
        # pre-loaded demo set, visible to everyone.
        "owner": owner,
        "source_filename": filename,
        "live": live,
        "statements": manifest.get("statements", 0),
        "rule_count": len(loaded_rules),
        "refusal_count": len(refusals),
        "needs_model_count": needs_model,
        "rejected_count": len(rejected),
        # Whether the emitted CQL was seen by the official translator, and
        # what it said. `compiler_available: false` is reported rather than
        # folded into the pass count, so "nobody compiled it" cannot read as
        # "it compiles".
        **cql_stats,
    }
    entries = [e for e in _load_index() if e.get("id") != guideline_id]
    entries.append(entry)
    _save_index(entries)
    return entry


def _run_ingest_tracked(guideline_id, title, filename, live, api_key,
                        stmts, manifest, owner: str | None = None) -> None:
    """Thread target: _run_ingest with tracking on, reporting its outcome
    to _PROGRESS instead of returning/raising to a caller that isn't there."""
    try:
        entry = _run_ingest(guideline_id, title, filename, live, api_key,
                            stmts, manifest, track=True, owner=owner)
        _progress_set(guideline_id, status="done", result=entry)
    except Exception as exc:  # a background thread has no caller to raise to
        _progress_set(guideline_id, status="error", error=str(exc))


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
    # Carry forward the flags that describe WHO an entry belongs to and
    # WHERE it is shown. Re-running a document through demo.py rewrites its
    # bundle and lands here; without this it would silently stop being a
    # demo entry and reappear in everybody's personal library.
    previous = next((e for e in _load_index() if e.get("id") == guideline_id), None)
    if previous:
        for carried in ("demo", "owner"):
            if previous.get(carried) is not None:
                entry[carried] = previous[carried]
        # A re-run changes the rules, not what the guideline is called.
        if previous.get("title"):
            entry["title"] = previous["title"]
    entries = [e for e in _load_index() if e.get("id") != guideline_id]
    _save_index(entries + [entry])
    return entry


# demo.html links to these two ids by fixed name -- not the `title-hex` ids
# ingest() generates -- so it can point at a fixed URL. Each is backed by
# Track A's own committed bundle (checked into git, unlike guidelines/
# itself, which is gitignored runtime data).
_DEMO_SEEDS = [
    ("pmc12355350", "PMC12355350",
     "Type 2 diabetes: cardiovascular, kidney and weight-loss therapeutics "
     "(living guideline)"),
    ("pmc13270348", "PMC13270348",
     "Pain management in acute pancreatitis (European interdisciplinary)"),
]


def seed_demo_guidelines() -> None:
    """Register demo.html's two fixed-id guidelines from their committed
    bundles, if this machine's local store doesn't have them yet.

    Without this, "no setup" (CHECKER.md's own description of the demo
    page) was only true on whichever machine happened to have someone run
    register_bundle() by hand at some point -- a fresh clone 404s on
    GET /guidelines/pmc12355350 instead. Skips (rather than re-registering)
    an id that is already present, so this costs nothing on every normal
    startup after the first."""
    # Backfill the flag on a store that predates it, so an already-seeded
    # deployment stops listing these in the personal library and starts
    # refusing to delete them.
    entries = _load_index()
    demo_ids = {gid for gid, _, _ in _DEMO_SEEDS}
    changed = False
    for entry in entries:
        if entry.get("id") in demo_ids and not entry.get("demo"):
            entry["demo"] = True
            changed = True
    if changed:
        _save_index(entries)

    existing = {e.get("id") for e in entries}
    for guideline_id, doc_id, title in _DEMO_SEEDS:
        if guideline_id in existing:
            continue
        bundle_path = Path(__file__).parent / "bundles" / f"bundle.{doc_id}.json"
        if not bundle_path.exists():
            continue
        try:
            registered = register_bundle(str(bundle_path), title=title,
                                         guideline_id=guideline_id)
            # Mark it as demo content: hidden from the personal library,
            # and refusing delete/rename like the built-in fixture set.
            fresh = _load_index()
            for entry in fresh:
                if entry.get("id") == registered["id"]:
                    entry["demo"] = True
            _save_index(fresh)
        except Exception as exc:  # a broken seed must not block the server starting
            print(f"[warn] could not seed demo guideline {guideline_id!r}: {exc}")


def delete_guideline(guideline_id: str, owner: str | None = None) -> dict:
    """Remove an uploaded guideline: its index entry, its bundle, and the raw
    upload kept for reprocessing. The fixture set is built in, not stored, so
    it cannot be deleted -- refused rather than silently ignored, since a
    delete that reports success and changes nothing is the worse failure."""
    if guideline_id == FIXTURES_ID:
        raise IngestError("the built-in fixture set cannot be deleted")
    entries = _load_index()
    meta = next((e for e in entries if e.get("id") == guideline_id), None)
    if meta is None:
        raise IngestError(f"unknown guideline {guideline_id!r}")
    if is_demo(meta):
        raise IngestError(
            "this is a built-in demo guideline and cannot be deleted")
    if not _owns(meta, owner):
        raise IngestError("this guideline was uploaded from another browser")

    (STORE_DIR / f"{guideline_id}.json").unlink(missing_ok=True)
    raw = _raw_source_path(guideline_id, meta.get("source_filename"))
    raw.unlink(missing_ok=True)
    # Older entries may have been stored under the other raw naming, so clear
    # both rather than leaving an orphan behind for a later id collision.
    _raw_source_path(guideline_id, None).unlink(missing_ok=True)

    _save_index([e for e in entries if e.get("id") != guideline_id])
    with _PROGRESS_LOCK:
        _PROGRESS.pop(guideline_id, None)
    with _BUNDLE_CACHE_LOCK:
        _BUNDLE_CACHE.pop(guideline_id, None)
    return {"id": guideline_id, "deleted": True}


def rename_guideline(guideline_id: str, title: str, owner: str | None = None) -> dict:
    """Change a guideline's display title in place.

    The id is left alone deliberately: it names the bundle on disk, the raw
    upload, and every URL the dashboard has already handed out, so renaming
    the title is a label change and nothing more."""
    if guideline_id == FIXTURES_ID:
        raise IngestError("the built-in fixture set cannot be renamed")
    title = (title or "").strip()
    if not title:
        raise IngestError("a title is required")
    entries = _load_index()
    meta = next((e for e in entries if e.get("id") == guideline_id), None)
    if meta is None:
        raise IngestError(f"unknown guideline {guideline_id!r}")
    if is_demo(meta):
        raise IngestError(
            "this is a built-in demo guideline and cannot be renamed")
    if not _owns(meta, owner):
        raise IngestError("this guideline was uploaded from another browser")
    meta["title"] = title
    _save_index(entries)
    return meta


# --- per-browser ownership -------------------------------------------------
#
# There is no login here. A visitor's browser mints a random id and sends it
# as X-GF-Client; an upload records it as the entry's `owner`, and the
# listing hides entries owned by somebody else. That keeps one visitor's
# uploads out of another's dashboard, which is what it is for.
#
# It is NOT access control: the id is client-supplied and forgeable, and a
# guideline fetched directly by id is still served to anyone who has the id.
# Treat anything uploaded here as public, and do not rely on this to keep a
# document private.
#
# Entries with no `owner` are the pre-loaded demo set (and anything uploaded
# before this existed). They stay visible to everyone on purpose.
# The demo page (/demo) walks a fixed list of guideline ids and shows each
# one with hand-written scenarios. Those entries have to survive whatever a
# visitor does on the main dashboard, so they are marked `demo` in the index:
# they are not listed in the personal library, and they refuse delete and
# rename the way the built-in fixture set does. A visitor should never have
# to upload anything to see the demo.
def is_demo(entry: dict) -> bool:
    return bool(entry.get("demo"))


def _visible_to(entry: dict, owner: str | None) -> bool:
    if is_demo(entry):
        return False          # /demo lists these; the library does not
    entry_owner = entry.get("owner")
    if not entry_owner:
        return True
    return bool(owner) and entry_owner == owner


def _owns(entry: dict, owner: str | None) -> bool:
    """May this caller mutate the entry? An unowned entry stays mutable so
    the pre-loaded set can still be tidied up; an owned one is its owner's."""
    entry_owner = entry.get("owner")
    if not entry_owner:
        return True
    return bool(owner) and entry_owner == owner


def list_guidelines(owner: str | None = None) -> list:
    fixture_entry = {
        "id": FIXTURES_ID,
        "title": "Hand-authored fixtures",
        # Says where these came from, because "fixtures" alone reads as though
        # they fell out of the pipeline like every other entry, and they did
        # not -- they are the checker's own test set, written by hand.
        "description": (
            "Written by hand for the checker, not extracted from any document "
            "-- ACE inhibitor with hyperkalaemia, DOAC dosing by renal "
            "function, drug-drug interaction flags. Each one is run through "
            "the same validator and non-degeneracy check a formalized rule "
            "gets, at import time."
        ),
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
    return [fixture_entry] + [
        dict(e, builtin=False, mine=bool(e.get("owner")) and e.get("owner") == owner)
        for e in _load_index()
        if _visible_to(e, owner)
    ]


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
    # Loading lowers every rule through z3, which is neither free nor
    # thread-safe (dsl.Z3_LOCK), and one dashboard or demo page view fans out
    # into several requests that each want the same bundle. Cache the lowered
    # result and key it on the file's mtime and size, so a re-upload or a
    # reprocess -- which rewrites this path -- invalidates it, while repeated
    # reads of an unchanged bundle cost nothing.
    try:
        st = bundle_path.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        return None
    with _BUNDLE_CACHE_LOCK:
        hit = _BUNDLE_CACHE.get(guideline_id)
    if hit is not None and hit[0] == stamp:
        rules, refusals, reg, rejected = hit[1]
    else:
        # Track B re-verifies before it loads: a bundle can be written by an
        # older Track A or hand-edited, and a VACUOUS_RULE that survived doesn't
        # fail loudly -- it reports CERTIFIED-compliant for everyone. Rejects
        # are OUR defect, not the guideline's residue, so they travel separately
        # from refusals rather than being folded into that count.
        rules, refusals, reg, rejected = load_bundle(str(bundle_path))
        with _BUNDLE_CACHE_LOCK:
            _BUNDLE_CACHE[guideline_id] = (stamp, (rules, refusals, reg, rejected))
    return rules, refusals, reg, dict(meta, builtin=False, rejected=rejected)
