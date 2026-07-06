"""
audit_cherrypick.py — adversarial read of agent_logs.csv.
Streams the CSV once and computes the things test_agent_logs.py does NOT check,
specifically the angles where a rigged benchmark would hide.
"""
import csv
from collections import defaultdict, Counter

CSV = "agent_logs.csv"
COMMITTED = {"WINNER", "FORK", "OCC_COMMIT"}
NO_WINNER = {"RECOMPUTE", "OCC_ALLABORT"}

# per (policy_tag, regime) aggregates
agg = defaultdict(lambda: dict(n=0, commits=0, aborts=0, redo=0, silent=0,
                               forks=0, winners=0))
win_tiers = Counter()
# does hybrid EVER beat/lose to OCC per regime? track occ vs hybrid_safety1
# also track: for OCC arms, is n_stale==n_writers on abort, and n_stale distribution

with open(CSV, newline="", encoding="utf-8-sig") as f:
    rdr = csv.DictReader(f)
    for r in rdr:
        tag, reg, arm = r["policy_tag"], r["regime"], r["arm"]
        a = agg[(tag, reg)]
        a["n"] += 1
        a["redo"] += int(r["recomputes"])
        a["silent"] += int(r["silent_error"])
        if arm in COMMITTED: a["commits"] += 1
        if arm in NO_WINNER: a["aborts"] += 1
        if arm == "FORK": a["forks"] += 1
        if arm == "WINNER": a["winners"] += 1
        if r["win_tier"] != "-1":
            win_tiers[r["win_tier"]] += 1

print("=== A. authority tier of every committed winner (is provenance ever exercised?) ===")
print("   win_tier distribution over committed rows:", dict(win_tiers))

print("\n=== B. hybrid_safety1 vs OCC head-to-head, PER REGIME ===")
print("   (silent = wrong commits; redo/commit = wasted reruns; abort% = throughput loss)")
print(f"   {'regime':<17}{'OCC abort%':>11}{'OCC redo/cmt':>13}{'OCC silent':>11}"
      f"{'HYB abort%':>12}{'HYB redo/cmt':>13}{'HYB silent':>11}")
regimes = ["benign","realistic","high_churn","extreme_lag","wide_deps",
           "high_contention","all_price_like","wild_drift","calm_but_wide",
           "storm","adversarial_all"]
for reg in regimes:
    o = agg[("occ", reg)]; h = agg[("hybrid_safety1", reg)]
    def cells(d):
        ab = 100.0*d["aborts"]/max(d["n"],1)
        rc = d["redo"]/max(d["commits"],1)
        return ab, rc, d["silent"]
    oab, orc, osi = cells(o); hab, hrc, hsi = cells(h)
    print(f"   {reg:<17}{oab:>10.1f}%{orc:>13.2f}{osi:>11}"
          f"{hab:>11.1f}%{hrc:>13.2f}{hsi:>11}")

print("\n=== C. pure cascade(0.20) silent errors PER REGIME (is it shown failing?) ===")
for reg in regimes:
    c = agg[("cascade_mat0.20", reg)]
    print(f"   {reg:<17} silent={c['silent']:>6}  commits={c['commits']:>7}  "
          f"forks={c['forks']:>6}")

print("\n=== E. PREDICATE vs POLICY: OCC(rev) -> OCC(value) -> cascade -> hybrid ===")
print("   (recompute/commit = rerun cost; silent = wrong commits)")
print(f"   {'regime':<17}{'OCC(rev)':>20}{'OCC(value)':>20}{'cascade0.20':>20}{'hybrid_s1':>20}")
print(f"   {'':<17}{'redo/cmt  silent':>20}{'redo/cmt  silent':>20}"
      f"{'redo/cmt  silent':>20}{'redo/cmt  silent':>20}")
for reg in regimes:
    def rc_si(tag):
        d = agg[(tag, reg)]
        return d["redo"]/max(d["commits"],1), d["silent"]
    orc, osi = rc_si("occ"); vrc, vsi = rc_si("occ_value")
    crc, csi = rc_si("cascade_mat0.20"); hrc, hsi = rc_si("hybrid_safety1")
    print(f"   {reg:<17}{orc:>11.2f}{osi:>9}{vrc:>11.2f}{vsi:>9}"
          f"{crc:>11.2f}{csi:>9}{hrc:>11.2f}{hsi:>9}")

print("\n=== F. TAUTOLOGY CHECK: hybrid silent under perfect vs noisy tolerance ===")
for tag in ("hybrid_safety1", "hybrid_noise0.5", "hybrid_noise1.0"):
    tot = sum(agg[(tag, reg)]["silent"] for reg in regimes)
    print(f"   {tag:<18} total silent_errors across all regimes: {tot}")

print("\n=== D. does hybrid ever pay MORE recompute than OCC? (the un-tested direction) ===")
worse = []
for reg in regimes:
    o = agg[("occ", reg)]; h = agg[("hybrid_safety1", reg)]
    orc = o["redo"]/max(o["commits"],1); hrc = h["redo"]/max(h["commits"],1)
    if hrc > orc:
        worse.append((reg, orc, hrc))
if worse:
    for reg, orc, hrc in worse:
        print(f"   {reg:<17} OCC redo/cmt {orc:.2f} < HYBRID {hrc:.2f}  <-- hybrid worse here")
else:
    print("   (none — hybrid never pays more recompute/commit than OCC in any regime)")
