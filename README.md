# Guideline-autoformalization

## Synthetic verifier benchmark

The original minimal reproduction remains in `partial_spec_check.py`. The
larger reproducible benchmark is in `benchmark.py`:

```sh
python3 -m pip install -r requirements.txt
python3 benchmark.py
```

It generates 200 synthetic patients, 25 bounded-horizon clauses, and partial
plans covering 14 of 30 candidate actions. Per-check results are written to
`benchmark_results.csv`; aggregate rates, classifications, minimized latent
counterexample sizes, and solve times are printed as a CSV-style table.

For a MAY result, `D and P and not phi` is satisfiable, so a literal UNSAT/MUS
core cannot exist. The benchmark reports a minimized set of latent action
assignments that makes `phi` unsatisfiable, which is the corresponding
counterexample core for a MAY-violate result.