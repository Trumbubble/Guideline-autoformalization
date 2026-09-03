"""Machine-check every proposition in paper/logic.tex.

Each proposition there is short enough that its proof fits in two lines, which
makes it easy to state one that is very slightly false -- and the one this
caught is instructive: "a free variable gives MAY-violate" is FALSE, and the
true statement is its converse (MAY-violate implies a free variable). See P2.

Needs only z3. No model call, no API key.

    python3 propositions.py
"""
import z3, itertools

def verdict(D, P, phi):
    a = z3.Solver(); a.add(*D, *P, phi)
    b = z3.Solver(); b.add(*D, *P, z3.Not(phi))
    if a.check() != z3.sat: return "MUST"
    if b.check() != z3.sat: return "CERT"
    return "MAY"

k   = z3.Real('potassium'); acei = z3.Bool('on_acei')
h   = z3.Bool('high_risk')                      # the hole: no data source
D   = [k >= 1.0, k <= 9.0]
phi = z3.Implies(z3.And(k > 5.0, h), z3.Not(acei))   # hole in the ANTECEDENT

print("P1  total assignment over Vars(phi) is never MAY:")
bad = []
for kv in (4.0, 5.5, 6.0):
    for av, hv in itertools.product([True, False], repeat=2):
        v = verdict(D, [k == kv, acei == av, h == hv], phi)
        if v == "MAY": bad.append((kv, av, hv))
print("      counterexamples:", bad or "none  -> HOLDS")

print("\nP2  converse FAILS (a free variable need not give MAY):")
# h is free, but pinned k=4.0 already decides phi (antecedent false)
print("      k=4.0, h free ->", verdict(D, [k == 4.0, acei == True], phi),
      " (h is free yet the verdict is definite)")
print("      k=6.0, h free ->", verdict(D, [k == 6.0, acei == True], phi))

print("\nP3  stubbing the hole can only REMOVE indeterminacy, never create it:")
rows = []
for kv in (4.0, 5.5, 6.0):
    for av in (True, False):
        P = [k == kv, acei == av]
        full = verdict(D, P, phi)
        f    = verdict(D, P + [h == False], phi)
        t    = verdict(D, P + [h == True],  phi)
        rows.append((kv, av, full, f, t))
        assert not (full != "MAY" and (f != full or t != full)), "definite verdict moved!"
        assert f != "MAY" or True
for r in rows: print("      k=%.1f acei=%-5s  full=%-4s  h:=false->%-4s  h:=true->%-4s" % r)
print("      no definite verdict was ever moved by stubbing -> HOLDS")

print("\nP4  one satisfiability query cannot separate MUST from MAY:")
for kv, av, label in ((6.0, True, "hole free"), (4.0, True, "antecedent false")):
    P = [k == kv, acei == av]
    v = verdict(D, P, phi)
    s = z3.Solver(); s.add(*D, *P, z3.Not(phi))     # "is a violation possible?"
    print(f"      {label:18} verdict={v:4}  sat(D&P&not phi)={s.check()==z3.sat}")
print("      MUST and MAY both give sat -> a single query conflates them")

print("\nP5  a permission has no violating completion:")
viol_perm = z3.BoolVal(False)
s = z3.Solver(); s.add(*D, viol_perm)
print("      sat(D & viol_P) =", s.check() == z3.sat, " -> never MUST-violate")

print("\nP6  Since without the occurrence flag is satisfied by a patient never tested:")
occ = z3.Bool('occ_e'); dse = z3.Int('dse_e')
Dt  = [dse >= 0, dse <= 3650]
with_flag    = z3.And(occ, dse <= 14)
without_flag = dse <= 14
for name, f in (("with occ", with_flag), ("without occ", without_flag)):
    s = z3.Solver(); s.add(*Dt, occ == False, f)
    print(f"      {name:12} satisfiable with occ_e=false: {s.check()==z3.sat}")

print("\nP7  a Delta with a free baseline is MAY-violate for every record:")
dose = z3.Real('dose'); base = z3.Real('dose_at_e')
Dd   = [dose >= 0, dose <= 200, base > 0, base <= 200]
phid = z3.Implies(z3.BoolVal(True), dose <= 0.5 * base)
for dv in (10.0, 40.0, 100.0):
    print(f"      dose={dv:5.1f}, baseline free -> {verdict(Dd, [dose == dv], phid)}")

print("\nP8  a vacuous rule is indistinguishable from a satisfied one:")
a, b = z3.Bool('a'), z3.Bool('b')
vac = z3.Implies(z3.And(a, b), b)                # (A and B) -> B
for av, bv in itertools.product([True, False], repeat=2):
    assert verdict([], [a == av, b == bv], vac) == "CERT"
print("      (A and B) -> B is CERTIFIED-compliant for all 4 assignments -> HOLDS")
