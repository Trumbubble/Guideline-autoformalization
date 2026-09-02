# Paper — GenAI4Health @ NeurIPS 2026, Demonstration Track

**Deadline: 5 Sep 2026, 11:59pm AoE. 5 pages max (excl. refs/appendix). Double-blind.**

## Overleaf setup
1. New Project → Upload Project → zip this `paper/` folder. It is self-contained:
   `neurips_2026.sty` and `checklist.tex` are already in here.
2. Menu → Compiler: **pdfLaTeX**.

### Venue options — already set, don't change them by accident
This is a **double-blind workshop**, so the package option is `dblblindworkshop`:

    \usepackage[dblblindworkshop]{neurips_2026}
    \workshoptitle{GenAI for Health}

- **not** `[final]` — that de-anonymises you (desk-reject risk for a
  double-blind track)
- **not** the bare `{neurips_2026}` — that formats for the main conference
- `\workshoptitle` is required, or the page footer reads `Workshop: .`
- for the camera-ready in October: `[dblblindworkshop, final]`
- in anonymous mode the style file **ignores** `\author` and prints its own
  block, so fill the real details in now and they appear when you add `final`
- the style loads `natbib`, hence `\bibliographystyle{plainnat}`

### Checklist — VERIFY THIS
`checklist.tex` ships with the NeurIPS template and states in bold that papers
without it are **desk rejected**. That is the *main conference* rule, and the
GenAI4Health CFP does not mention a checklist for the demonstration track.
**Check the OpenReview submission form before the deadline.** It does not count
against the page limit, so if in any doubt, uncomment `\input{checklist}` at the
bottom of `main.tex` and fill it in.
4. `\draftmodetrue` shows the guidance notes; set `\draftmodefalse` to see the
   true page count. **Submit with notes off and `neurips_2026` un-optioned.**

## Figures — OFF by default

`main.tex` ships with `\usesvgfalse`, so it compiles to labelled placeholder
boxes with no external tooling. To turn the real figures on:

1. `\usesvgtrue` in the preamble
2. Overleaf → Menu → Compiler → **Allow shell escape** (the `svg` package
   shells out to Inkscape)
3. Overleaf → Menu → **Clear cached files** — a failed run leaves a stale
   `svg-inkscape/` directory that gets reused and fails again

**Why it failed the first time:** `\includesvg` defaults to Inkscape's
`--export-latex` mode, which pulls the SVG's text labels out and typesets them
with LaTeX — so every `−`, `φ`, `∧`, `∈` in the diagrams became
"Unicode character not set up for use with LaTeX", and `unbound_concept` became
"Missing $ inserted". Fixed two ways: `inkscapelatex=false` (Inkscape renders
the text into the PDF itself), and `\DeclareUnicodeCharacter` for all 12
non-ASCII glyphs the figures contain, so either mode works.

No shell-escape available? Convert once outside Overleaf and swap `\figsvg` for
`\includegraphics`:

    rsvg-convert -f pdf -o architecture.pdf architecture.svg

| file | size | where |
|---|---|---|
| `architecture.svg` | 1636x1319 | Fig 1, §System — ~4.4in tall at full width |
| `language.svg` | 1400x880 | appendix — ~3.5in, too big for the body |
| `deontic.svg` | 1400x336 | ~1.3in, cheap — promote to body if room |
| `lowering.svg` | 1400x340 | ~1.3in, cheap |

Still to make: `stub_comparison.png` (§Necessity) and the three demo
screenshots (§Demonstration). Both are screenshots of the real UI.

## Three days: suggested order
- **Day 1** — write §1, §2, §5 (they need no new results). Take the three demo
  screenshots and record the video.
- **Day 2** — write `Part B/tests/ablation.py`, fill Table 1. Build the stub
  comparison figure.
- **Day 3** — §3, §4, abstract. Verify every orange `\num{}` against current
  code. Anonymisation pass. Page-count pass with notes off.

## Do not
- Wire `cql-to-elm` (right fix, wrong week — it will find things).
- Chase clinician annotators (cannot get two + a kappa by Friday).
- Rewrite `RESULTS.md` (cite it as an artefact).
- Try to raise 36% (your own analysis says a higher number means you are
  encoding what you should refuse).
