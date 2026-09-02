# Paper — GenAI4Health @ NeurIPS 2026, Demonstration Track

**Deadline: 5 Sep 2026, 11:59pm AoE. 5 pages max (excl. refs/appendix). Double-blind.**

## Overleaf setup
1. New Project → Upload Project → zip this `paper/` folder.
2. Add `neurips_2026.sty` from the workshop/NeurIPS site (not included here).
3. Menu → Compiler: **pdfLaTeX**. Enable **shell-escape** if `\includesvg` errors.
4. `\draftmodetrue` shows the guidance notes; set `\draftmodefalse` to see the
   true page count. **Submit with notes off and `neurips_2026` un-optioned.**

## Figures
`figures/*.svg` are standalone and theme-resolved for print (white ground).
Overleaf runs Inkscape, so `\includesvg` keeps them vector. If that fails,
convert once and swap in `\includegraphics`:

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
