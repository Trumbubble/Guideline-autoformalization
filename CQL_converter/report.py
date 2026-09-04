"""A bundle as a page a person can read.

The pipeline's outputs were a JSON bundle (an interface artifact, for Track B)
and terminal scrollback (ephemeral). Neither is something you can hand to a
clinician or a governance committee, and the committee is the audience: the
unit of review is one guideline sentence beside the rule it became, and the
question is whether the rule says what the sentence said.

So this renders exactly that comparison, plus the things a reviewer must not
have to dig for -- what was REFUSED and why, and which checks fired on a rule
that compiled anyway. A report that showed only what encoded would let a reader
conclude the tool had covered the document.

    python3 report.py ../Part\\ B/bundle.PMC13074134.json -o report.html

Self-contained: no network, no assets, prints to PDF from any browser.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import html
import json
import os

import dsl

# Checks that do not block an encoding but mean it may not say what the
# sentence said. These are the reason the report exists.
CAUTION = {"CIRCULAR_BASELINE", "DROPPED_THRESHOLD", "ACTION_TARGET_MISMATCH",
           "DROPPED_CONCEPT_RISK", "UNUSED_BOUND_CONCEPT", "REGISTRY_TRUNCATED",
           "TERMINOLOGY_UNAVAILABLE", "PARTIAL_ENCODING", "VACUOUS_RULE"}

DECISION_LABEL = {"encode": "Encoded", "partial": "Partial", "refuse": "Refused"}

REASON_PROSE = {
    "unbound_concept": "a clinical concept in the sentence could not be resolved "
                       "to a terminology code",
    "beyond_dsl": "the sentence needs a construct this logic does not have",
    "vague_predicate": "the sentence turns on a judgement it does not define",
    "defeasible": "the guideline hedged: the action is discretionary, not required",
    "not_patient_level": "the sentence is about a service or a system, not a patient",
    "unsupported_temporal": "the timing the sentence describes cannot be expressed",
}

def _e(x) -> str:
    return html.escape("" if x is None else str(x))

# Runs before the stylesheet paints, so a stored choice never flashes the wrong
# theme for a frame. Same `gf_theme` key the checker's pages use, so the report
# inherits whatever the dashboard was set to; a report saved to disk and opened
# on its own simply finds nothing stored and follows the OS.
THEME_PREPAINT = """<script>
(function(){try{var s=localStorage.getItem("gf_theme");
if(s==="light"||s==="dark"){document.documentElement.setAttribute("data-theme",s);}
}catch(e){}})();
</script>"""

THEME_TOGGLE = """<button class="theme-toggle" id="theme-toggle" type="button"
 aria-label="Toggle colour theme" title="Toggle colour theme">
<svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
<circle class="ring" cx="9" cy="9" r="7"></circle>
<circle class="dot" id="theme-dot" cx="9" cy="9" r="3"></circle>
</svg></button>"""

THEME_SCRIPT = """<script>
(function(){
  var KEY="gf_theme";
  function current(){var s=null;try{s=localStorage.getItem(KEY);}catch(e){}
    if(s==="light"||s==="dark")return s;
    return window.matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light";}
  function refresh(){var d=current()==="dark";
    document.getElementById("theme-dot").setAttribute("r",d?"4.5":"2.5");
    document.getElementById("theme-toggle").title=
      d?"Switch to light mode":"Switch to dark mode";}
  document.getElementById("theme-toggle").addEventListener("click",function(){
    var n=current()==="dark"?"light":"dark";
    document.documentElement.setAttribute("data-theme",n);
    try{localStorage.setItem(KEY,n);}catch(e){}
    refresh();});
  refresh();
})();
</script>"""

CSS = """
:root{--bg:#f8f5f1;--fg:#27221b;--muted:#756f67;--line:#dfdcd7;--card:#fffdf9;
--ok:#006f36;--part:#864d00;--no:#963a34;--accent:#25559b;--code:#f2f0ec}
@media(prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#191713;
--fg:#f1ece5;--muted:#a7a29c;
--line:#37332f;--card:#23201d;--ok:#80d498;--part:#edb163;--no:#ff9f94;
--accent:#76acf1;--code:#13110d}}
/* The explicit choice the app's other pages store, honoured here too so a
   report opened from the dashboard does not snap back to the OS setting. */
:root[data-theme="dark"]{color-scheme:dark;--bg:#191713;--fg:#f1ece5;--muted:#a7a29c;
--line:#37332f;--card:#23201d;--ok:#80d498;--part:#edb163;--no:#ff9f94;
--accent:#76acf1;--code:#13110d}
:root[data-theme="light"]{color-scheme:light}
.theme-toggle{border:1px solid var(--line);background:var(--card);color:var(--muted);
border-radius:999px;padding:0;width:34px;height:34px;display:inline-flex;
align-items:center;justify-content:center;cursor:pointer;float:right}
.theme-toggle:hover{color:var(--accent);border-color:var(--accent)}
.theme-toggle svg{display:block}
.theme-toggle circle.ring{fill:none;stroke:currentColor;stroke-width:1.6}
.theme-toggle circle.dot{fill:currentColor}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:16px/1.6 "Archivo","Helvetica Neue",Helvetica,Arial,sans-serif}
.wrap{max-width:920px;margin:0 auto;padding:40px 24px 80px}
.back{display:inline-block;margin-bottom:18px;font-size:13.5px;color:var(--accent);
text-decoration:none}
.back:hover{text-decoration:underline}
@media print{.back{display:none}}
h1{font-size:26px;margin:0 0 4px;letter-spacing:-.01em}
h2{font-size:15px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);
margin:44px 0 14px;font-weight:600}
.sub{color:var(--muted);font-size:14px;margin:0 0 28px}
.bar{display:flex;height:10px;border-radius:5px;overflow:hidden;margin:18px 0 8px;
background:var(--line)}
.bar span{display:block}
.legend{display:flex;gap:18px;flex-wrap:wrap;font-size:13px;color:var(--muted)}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:18px 20px;margin:0 0 14px}
details.card>summary{list-style:none;cursor:pointer;margin:-18px -20px;
padding:18px 20px 18px 40px;border-radius:10px;position:relative}
details.card>summary::-webkit-details-marker{display:none}
details.card>summary::before{content:"\\25B8";position:absolute;left:20px;top:18px;
color:var(--muted);transition:transform .15s}
details.card[open]>summary::before{transform:rotate(90deg)}
details.card>summary:hover{background:color-mix(in srgb,var(--fg) 3%,transparent)}
details.card>summary blockquote{margin-bottom:0;border-left:none;padding-left:0}
details.card>summary .head{margin-bottom:6px}
details.card>.body{padding-top:14px;margin-top:14px;border-top:1px solid var(--line)}
.controls{display:flex;gap:10px;align-items:center;margin:0 0 14px}
.controls button{font:13px inherit;color:var(--accent);background:none;
border:1px solid var(--line);border-radius:6px;padding:4px 11px;cursor:pointer}
.controls button:hover{border-color:var(--accent)}
.hint{font-size:12.5px;color:var(--muted)}
.card.encode{border-left:4px solid var(--ok)}
.card.partial{border-left:4px solid var(--part)}
.card.refuse{border-left:4px solid var(--no)}
.head{display:flex;justify-content:space-between;align-items:baseline;gap:12px;
margin-bottom:10px}
.id{font:12px "JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted)}
.badge{font-size:12px;font-weight:600;padding:2px 9px;border-radius:20px;
white-space:nowrap}
.badge.encode{background:color-mix(in srgb,var(--ok) 16%,transparent);color:var(--ok)}
.badge.partial{background:color-mix(in srgb,var(--part) 18%,transparent);color:var(--part)}
.badge.refuse{background:color-mix(in srgb,var(--no) 15%,transparent);color:var(--no)}
blockquote{margin:0 0 14px;padding:10px 0 10px 14px;border-left:2px solid var(--line);
color:var(--fg);font-size:15px}
.label{font-size:11px;text-transform:uppercase;letter-spacing:.07em;
color:var(--muted);margin:14px 0 5px;font-weight:600}
.rule.logic{font-size:13.5px;color:var(--accent)}
.rule{font:14px/1.7 "JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--code);
border-radius:7px;padding:12px 14px;white-space:pre-wrap;overflow-x:auto}
.why{font-size:14px;color:var(--fg)}
ul{margin:6px 0 0;padding-left:20px;font-size:14px}
li{margin:3px 0}
.hole{font:12px "JetBrains Mono",ui-monospace,Menlo,monospace;background:color-mix(in srgb,var(--part) 15%,transparent);
padding:1px 6px;border-radius:4px;margin-right:5px;display:inline-block}
.caution{border-left:3px solid var(--part);padding:8px 12px;margin:8px 0 0;
background:color-mix(in srgb,var(--part) 7%,transparent);border-radius:0 6px 6px 0;
font-size:13.5px}
.caution code{font-weight:600;color:var(--part)}
details{margin-top:14px}
summary{cursor:pointer;font-size:13px;color:var(--accent);user-select:none}
pre{background:var(--code);border-radius:7px;padding:14px;overflow-x:auto;
font:12.5px/1.55 "JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
margin:10px 0 0}
button,input,select,textarea{font-family:inherit}
code,kbd,samp{font-family:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,monospace}
table{border-collapse:collapse;width:100%;font-size:14px;margin-top:8px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line)}
th{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
td.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
footer{margin-top:56px;padding-top:20px;border-top:1px solid var(--line);
font-size:13px;color:var(--muted)}
@media print{body{background:#fff}.card{break-inside:avoid}
.controls{display:none}details.card>.body{display:block}
details:not(.card){display:none}}
"""

def variable_rows(rule, reg) -> list[dict]:
    """Every name the rule stands on, with the code it resolved to.

    This is the part a governance committee actually audits: not that the logic
    is well-formed, but that `on_sglt_2_inhibitors` is the value set they think
    it is. An unbound name is shown as such rather than omitted -- a rule whose
    free variables are invisible reads as more complete than it is."""
    used = dsl._vars_in(rule.when) | dsl._vars_in(rule.then)
    rows = []
    for n in sorted(used):
        if n not in reg:
            rows.append({"name": n, "type": "?", "binding": None, "unbound": True})
            continue
        v = reg[n]
        rows.append({"name": n, "type": v.type, "unit": v.unit,
                     "description": v.description,
                     "binding": v.binding, "unbound": bool(v.unbound),
                     "codes": len(v.codes or ())})
    for ev in dsl.events_in(rule):
        e = reg.event(ev) if reg.has_event(ev) else None
        rows.append({"name": ev, "type": "event",
                     "description": getattr(e, "description", "") or "",
                     "binding": getattr(e, "binding", None),
                     "unbound": bool(getattr(e, "unbound", True))})
    return rows

def finding(t, reg, cql_files: dict | None = None) -> dict:
    """One statement's outcome, in the form a reader needs: the sentence, the
    decision, the rule in ENGLISH, and every check that fired.

    Lives here, not in demo.py, because Track B's upload path builds the
    same bundle from the same triages. Two copies of a walker is how R34
    happened three times over.

    `t` is a gate.Triage; typed loosely so this module does not import the
    gate just to render a page."""
    out = {"statement_id": t.statement_id,
           "statement": t.text,
           "decision": t.decision,
           "reason_code": t.reason_code,
           "confidence": round(float(t.confidence or 0.0), 2),
           "open_holes": list(t.open_holes),
           "evidence": [{"source": e.source, "code": e.code, "detail": e.detail}
                        for e in t.evidence]}
    if t.rule is not None:
        out["rule_id"] = t.rule.rule_id
        out["action"] = f"{t.rule.action} {t.rule.target}".strip()
        out["deontic"] = t.rule.deontic
        out["strength"] = t.rule.strength
        out["logic"] = dsl.rule_to_logic(t.rule)
        out["variables"] = variable_rows(t.rule, reg)
        try:
            out["english"] = dsl.rule_to_english(t.rule, reg)
        except Exception:
            # A gloss is a convenience. It must never be the reason a bundle
            # fails to be written.
            out["english"] = None
    if cql_files and t.statement_id in cql_files:
        out["cql"] = cql_files[t.statement_id]
    return out


def _caution_block(f: dict) -> str:
    rows = [e for e in f.get("evidence", []) if e.get("code") in CAUTION]
    if not rows:
        return ""
    out = ['<div class="label">Checks that fired</div>']
    for e in rows:
        out.append(f'<div class="caution"><code>{_e(e["code"])}</code> '
                   f'{_e(e.get("detail", ""))}</div>')
    return "\n".join(out)

def _statement_card(f: dict, cql_root: str) -> str:
    """One statement, collapsed.

    Everything used to render open. That reads fine for the five-statement
    demo and is unusable for a real guideline -- a 60-statement document became
    a wall no one scrolls, and the SOURCE SENTENCE, which is the thing a
    reviewer is actually scanning for, was buried between an encoding and a
    CQL block. Collapsed, the page is a list of the guideline's own sentences
    with a verdict against each; open one and the encoding is underneath."""
    d = f.get("decision", "refuse")
    parts = [f'<details class="card {d}">',
             '<summary>',
             '<div class="head">',
             f'<span class="id">{_e(f.get("statement_id"))}</span>',
             f'<span class="badge {d}">{DECISION_LABEL.get(d, d)}</span>',
             '</div>',
             f'<blockquote>{_e(f.get("statement"))}</blockquote>',
             '</summary>',
             '<div class="body">']

    if f.get("english"):
        parts += ['<div class="label">Encoded as</div>',
                  f'<div class="rule">{_e(f["english"])}</div>']
    if f.get("action"):
        parts.append(f'<div class="why" style="margin-top:8px">'
                     f'<strong>Action:</strong> {_e(f["action"])}</div>')
    if f.get("strength"):
        # Verbatim from the source. The reader is checking our encoding against
        # the guideline's own grading, so ours must not be the only one shown.
        parts.append(f'<div class="why" style="margin-top:4px;color:var(--muted)">'
                     f'<strong>Strength, as the guideline states it:</strong> '
                     f'{_e(f["strength"])}</div>')

    if f.get("open_holes"):
        parts += ['<div class="label">Left undefined, on purpose</div>',
                  '<div>' + "".join(f'<span class="hole">{_e(h)}</span>'
                                    for h in f["open_holes"]) + '</div>',
                  '<div class="why" style="margin-top:6px;color:var(--muted)">'
                  'These are free variables. Until a value is supplied the '
                  'verdict for this rule is <em>cannot tell</em>, not '
                  '<em>compliant</em>.</div>']

    if d == "refuse":
        code = f.get("reason_code") or "unknown"
        parts += ['<div class="label">Not encoded because</div>',
                  f'<div class="why">{_e(REASON_PROSE.get(code, code))} '
                  f'<span class="id">({_e(code)})</span></div>']
        model = next((e for e in f.get("evidence", [])
                      if e.get("source") == "model"), None)
        if model and model.get("detail"):
            parts.append(f'<div class="why" style="margin-top:8px;color:var(--muted)">'
                         f'{_e(model["detail"][:600])}</div>')

    parts.append(_caution_block(f))

    if f.get("logic") or f.get("variables"):
        inner = []
        if f.get("logic"):
            inner.append('<div class="label">&phi; = when &rarr; then</div>'
                         f'<div class="rule logic">{_e(f["logic"])}</div>')
        if f.get("variables"):
            inner.append('<div class="label">What each name is bound to</div>'
                         '<table><tr><th>Variable</th><th>Type</th>'
                         '<th>Terminology</th></tr>')
            for v in f["variables"]:
                if v.get("unbound"):
                    code = '<em style="color:var(--part)">left free &mdash; no code</em>'
                elif v.get("binding"):
                    n = v.get("codes") or 0
                    code = (_e(v["binding"])
                            + (f' <span class="id">(+{n} codes)</span>' if n > 1 else ""))
                else:
                    code = '<span class="id">&mdash;</span>'
                inner.append(f'<tr><td><span class="id">{_e(v["name"])}</span><br>'
                             f'<span style="font-size:12.5px;color:var(--muted)">'
                             f'{_e(v.get("description") or "")}</span></td>'
                             f'<td>{_e(v.get("type"))}'
                             + (f' <span class="id">{_e(v["unit"])}</span>'
                                if v.get("unit") else "")
                             + f'</td><td style="word-break:break-all">{code}</td></tr>')
            inner.append('</table>')
        parts += ['<details><summary>Show the logic and its bindings</summary>',
                  "".join(inner), '</details>']

    if f.get("cql"):
        path = os.path.join(cql_root, os.path.basename(f["cql"]))
        try:
            text = open(path, encoding="utf-8").read()
        except OSError:
            text = None
        if text:
            parts += ['<details><summary>Show the executable CQL '
                      f'({len(text.splitlines())} lines)</summary>',
                      f'<pre>{_e(text)}</pre></details>']
    parts.append('</div></details>')
    return "\n".join(parts)

def render(bundle: dict, *, title: str, cql_root: str = ".",
           home_url: str | None = None, home_label: str = "All guidelines") -> str:
    """`home_url` is emitted as a back link only when one is passed, labelled
    `home_label` -- the caller knows where the reader came from, this module
    does not. A reader who opened the report from the demo should go back to
    the demo, not to a library they were never in.

    The report is served two ways: from the Flask app, where "/" is the
    guideline library, and as a file opened straight from disk, where there is
    no server and a link to "/" would go nowhere. So the caller that knows it
    is inside the app supplies the URL, and the standalone file simply has no
    back link rather than a broken one."""
    findings = bundle.get("findings") or []
    if not findings:
        # An older bundle, written before findings existed. Render what is
        # there rather than an empty page, and say so -- a report that silently
        # showed nothing would be the same failure this project keeps meeting.
        reg, rules, _refusals = dsl.bundle_from_dict(bundle)
        english, logic, varrows = {}, {}, {}
        for r in rules:
            try:
                english[r.rule_id] = dsl.rule_to_english(r, reg)
                logic[r.rule_id] = dsl.rule_to_logic(r)
                varrows[r.rule_id] = variable_rows(r, reg)
            except Exception:
                pass
        findings = ([{"statement_id": r.get("rule_id"),
                      "statement": r.get("source_span"), "decision": "encode",
                      "english": english.get(r.get("rule_id")),
                      "logic": logic.get(r.get("rule_id")),
                      "variables": varrows.get(r.get("rule_id")),
                      "strength": r.get("strength"),
                      "action": f"{r.get('action','')} {r.get('target','')}".strip()}
                     for r in bundle.get("rules", [])]
                    + [{"statement_id": r.get("rule_id"),
                        "statement": r.get("source_span"), "decision": "refuse",
                        "reason_code": r.get("reason_code"),
                        "evidence": [{"source": "model", "code": r.get("reason_code"),
                                      "detail": r.get("reason_text")}]}
                       for r in bundle.get("refusals", [])])

    n = {k: sum(1 for f in findings if f.get("decision") == k)
         for k in ("encode", "partial", "refuse")}
    total = max(1, len(findings))
    pct = {k: 100.0 * v / total for k, v in n.items()}
    stamp = _dt.date.today().isoformat()

    back = (f'<a class="back" href="{_e(home_url)}">&larr; {_e(home_label)}</a>'
            if home_url else "")
    # A <link> ahead of the stylesheet is fetched with the document; an
    # @import inside <style> is discovered only after the CSS parses, which
    # is what made the first paint fall back to the system face.
    fonts = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
             '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
             '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
             'family=Archivo:wght@400;500;600&amp;'
             'family=JetBrains+Mono:wght@400;500&amp;display=swap">')
    body = [f"<title>{_e(title)}</title>", fonts, THEME_PREPAINT, f"<style>{CSS}</style>",
            '<div class="wrap">',
            THEME_TOGGLE,
            back,
            f"<h1>{_e(title)}</h1>",
            f'<p class="sub">{len(findings)} recommendation statement(s) '
            f'&middot; generated {stamp} &middot; '
            f'{len(bundle.get("registry", {}).get("variables", []))} bound '
            f'variable(s)</p>',
            '<div class="bar">'
            f'<span style="width:{pct["encode"]:.1f}%;background:var(--ok)"></span>'
            f'<span style="width:{pct["partial"]:.1f}%;background:var(--part)"></span>'
            f'<span style="width:{pct["refuse"]:.1f}%;background:var(--no)"></span>'
            '</div>',
            '<div class="legend">'
            f'<span><i class="dot" style="background:var(--ok)"></i>'
            f'{n["encode"]} encoded</span>'
            f'<span><i class="dot" style="background:var(--part)"></i>'
            f'{n["partial"]} partial &mdash; encoded with a named gap</span>'
            f'<span><i class="dot" style="background:var(--no)"></i>'
            f'{n["refuse"]} refused</span></div>']

    if n["refuse"]:
        buckets: dict[str, int] = {}
        for f in findings:
            if f.get("decision") == "refuse":
                buckets[f.get("reason_code") or "unknown"] = \
                    buckets.get(f.get("reason_code") or "unknown", 0) + 1
        body += ['<h2>Why statements were refused</h2>',
                 '<table><tr><th>Reason</th><th>Meaning</th><th class="n">n</th></tr>']
        for code, count in sorted(buckets.items(), key=lambda x: -x[1]):
            body.append(f'<tr><td><span class="id">{_e(code)}</span></td>'
                        f'<td>{_e(REASON_PROSE.get(code, ""))}</td>'
                        f'<td class="n">{count}</td></tr>')
        body.append('</table>')

    body.append('<h2>Statement by statement</h2>')
    body.append('<div class="controls">'
                '<button type="button" onclick="setAll(true)">Expand all</button>'
                '<button type="button" onclick="setAll(false)">Collapse all</button>'
                '<span class="hint">or click any statement</span></div>')
    order = {"encode": 0, "partial": 1, "refuse": 2}
    for f in sorted(findings, key=lambda x: (order.get(x.get("decision"), 3),
                                             str(x.get("statement_id")))):
        body.append(_statement_card(f, cql_root))

    body += ['<footer>',
             '<strong>What this report does not claim.</strong> A refusal is a '
             'statement this tool declined to encode &mdash; it is not a '
             'judgement about the guideline. A rule shown here typechecked and '
             'compiled; that is not the same as being clinically correct, which '
             'is what the sentence beside it is for. Statements marked '
             '<em>partial</em> carry named free variables and answer '
             '<em>cannot tell</em> until those are supplied.',
             '</footer></div>',
             # Inline and tiny: the page must stay a single file that opens
             # from disk with no server and no network.
             '<script>function setAll(open){'
             'document.querySelectorAll("details.card").forEach(function(d){'
             'd.open=open;});}</script>',
             THEME_SCRIPT]
    return "\n".join(body)

def main(argv=None):
    ap = argparse.ArgumentParser(description="Render a bundle as a readable report.")
    ap.add_argument("bundle")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--title", default=None)
    a = ap.parse_args(argv)
    with open(a.bundle, encoding="utf-8") as fh:
        bundle = json.load(fh)
    stem = os.path.splitext(os.path.basename(a.bundle))[0]
    title = a.title or stem.replace("bundle.", "").replace("_", " ")
    out = a.out or os.path.join(os.path.dirname(a.bundle) or ".", f"{stem}.html")
    cql_root = os.path.join(os.path.dirname(a.bundle) or ".", "cql", stem)
    html_text = render(bundle, title=f"{title} — formalization report",
                       cql_root=cql_root)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html_text)
    print(f"wrote {out}  ({len(html_text):,} bytes, self-contained)")
    return out

if __name__ == "__main__":
    main()
