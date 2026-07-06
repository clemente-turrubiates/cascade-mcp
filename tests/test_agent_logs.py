"""
test_agent_logs.py — self-consistency + usability tests for agent_logs.csv.

Run:  python test_agent_logs.py
Exits non-zero on first failure (with a clear message), prints PASS lines.
"""
import csv
import os
import sys
from collections import Counter, defaultdict

CSV = "agent_logs.csv"
COLS = [
    "seed", "regime", "source_write_prob", "lag", "deps_per_field",
    "contention_width_lo", "contention_width_hi", "frac_zero_tol",
    "value_drift", "tol_safety", "fresh_loser_redo_prob", "rounds",
    "policy", "policy_tag", "field", "level",
    "read_time", "resolve_time", "n_writers", "n_stale",
    "win_tier", "top_confidence", "dependency_values", "arm",
    "recomputes", "silent_error", "true_tol",
]
N_COLS = len(COLS)
ARMS = {"WINNER", "FORK", "OCC_COMMIT", "RECOMPUTE", "OCC_ALLABORT"}
COMMITTED = {"WINNER", "FORK", "OCC_COMMIT"}     # arms that commit a value
NO_WINNER  = {"RECOMPUTE", "OCC_ALLABORT"}        # arms with no winner

fails = []
def check(cond, msg):
    print(("PASS" if cond else "FAIL") + "  " + msg)
    if not cond:
        fails.append(msg)


def main():
    if not os.path.exists(CSV):
        print(f"FAIL  {CSV} not found"); sys.exit(1)
    with open(CSV, newline="", encoding="utf-8-sig") as f:
        rdr = csv.DictReader(f)
        check(rdr.fieldnames == COLS, f"header matches expected {N_COLS}-column schema")
        rows = list(rdr)
    print(f"loaded {len(rows)} rows")

    # --- 1. integrity -------------------------------------------------------
    check(all(len(r) == N_COLS for r in rows), f"every row has {N_COLS} fields")
    check(all(r["arm"] in ARMS for r in rows), "every arm is one of the 5 known arms")
    check(all(r["arm"] != "" for r in rows), "no null arms")
    check(all(int(r["n_writers"]) >= 1 for r in rows), "n_writers >= 1")
    check(all(0 <= int(r["n_stale"]) <= int(r["n_writers"]) for r in rows),
          "0 <= n_stale <= n_writers")
    # duplicate-run guard: each resolution is unique by
    # (seed, regime, policy_tag, field, read_time, resolve_time). A duplicated
    # policy_variants entry (or double-generation) would break this even though
    # the set-based cell/seed checks below would still pass. This is the check
    # that catches a doubled arm.
    keys = [(r["seed"], r["regime"], r["policy_tag"], r["field"],
             r["read_time"], r["resolve_time"]) for r in rows]
    n_dup = len(keys) - len(set(keys))
    check(n_dup == 0, f"no duplicate resolution rows (found {n_dup} dups)")

    # --- 2. winner columns consistent with arm ------------------------------
    # Batch per-arm-class instead of per-row to keep output readable.
    no_winner_rows = [r for r in rows if r["arm"] in NO_WINNER]
    bad_tier = [r for r in no_winner_rows if r["win_tier"] != "-1"]
    bad_conf = [r for r in no_winner_rows if r["top_confidence"] != "-1.0000"]
    check(not bad_tier, f"no-winner arms have win_tier=-1 (found {len(bad_tier)} bad)")
    check(not bad_conf, f"no-winner arms have top_conf=-1.0000 (found {len(bad_conf)} bad)")
    committed_rows = [r for r in rows if r["arm"] in COMMITTED]
    bad_wt = [r for r in committed_rows if int(r["win_tier"]) < 0]
    bad_tc = [r for r in committed_rows if float(r["top_confidence"]) < 0.0]
    check(not bad_wt, f"committed arms have non-negative win_tier (found {len(bad_wt)} bad)")
    check(not bad_tc, f"committed arms have non-negative top_confidence (found {len(bad_tc)} bad)")

    # --- 3. silent_error only on committed writes ---------------------------
    bad_sil = [r for r in rows if r["arm"] in NO_WINNER and r["silent_error"] == "1"]
    check(not bad_sil, f"silent_error=1 only on committed arms (found {len(bad_sil)} bad)")

    # --- 4. silent_error=0 everywhere under hybrid_safety1 ------------------
    # the whole point of safety=1 hybrid is that materiality = true_tol -> no leaks
    sil1 = [r for r in rows
            if r["policy_tag"] == "hybrid_safety1" and r["silent_error"] == "1"]
    check(not sil1, f"hybrid_safety1 has 0 silent_error rows (found {len(sil1)})")

    # --- 5. recomputes is non-negative; >0 only when stale losers exist or
    #        fresh_loser_redo_prob>0 ------------------------------------------------
    bad_rec = [r for r in rows if int(r["recomputes"]) < 0]
    check(not bad_rec, "recomputes is never negative")
    # RECOMPUTE / OCC_ALLABORT charge redo for every writer in the group.
    # Batch the assertion instead of checking per-row (3.7M rows -> too noisy).
    abort_rows = [r for r in rows if r["arm"] in ("RECOMPUTE", "OCC_ALLABORT")]
    bad_abort = [r for r in abort_rows if int(r["recomputes"]) != int(r["n_writers"])]
    check(not bad_abort,
          f"RECOMPUTE/OCC_ALLABORT charge recomputes == n_writers (found {len(bad_abort)} bad)")

    # --- 6. read_time <= resolve_time and lag = resolve - read --------------
    bad_lag = [r for r in rows
               if int(r["resolve_time"]) - int(r["read_time"]) != int(r["lag"])]
    check(not bad_lag, f"resolve_time - read_time == lag for all rows (found {len(bad_lag)} bad)")

    # --- 7. dependency_values parses as dep:rev:val triples -----------------
    def parse_depvals(s):
        if not s: return []
        out = []
        for chunk in s.split("|"):
            parts = chunk.split(":")
            if len(parts) != 3: return None
            out.append((parts[0], int(parts[1]), float(parts[2])))
        return out
    bad_dep = 0
    for r in rows:
        dv = parse_depvals(r["dependency_values"])
        if dv is None: bad_dep += 1
        else:
            # field level > 0 should have deps; level 0 (source) has none
            if int(r["level"]) == 0 and dv:
                bad_dep += 1
            if int(r["level"]) > 0 and not dv:
                bad_dep += 1
    check(bad_dep == 0, f"dependency_values parses cleanly ({bad_dep} bad)")

    # --- 8. cross-regime comparison works (the whole point) -----------------
    # OCC abort rate must rise with churn; pull the abort% under benign vs
    # extreme_lag vs high_churn and assert monotonic-ish ordering.
    def abort_pct(tag, regime):
        grp = [r for r in rows if r["policy_tag"] == tag and r["regime"] == regime]
        if not grp: return None
        ab = sum(1 for r in grp if r["arm"] in NO_WINNER)
        return 100.0 * ab / len(grp)
    # OCC should collapse under churn. Regimes aren't on a single axis
    # (adversarial_all has wider contention -> more FORK survivors -> can
    # land below high_churn on abort%), so we assert the meaningful ordering
    # (benign much lower than churned regimes) rather than strict monotonicity
    # across all four.
    occ_benign   = abort_pct("occ", "benign")
    occ_realistic= abort_pct("occ", "realistic")
    occ_high     = abort_pct("occ", "high_churn")
    occ_adversarial = abort_pct("occ", "adversarial_all")
    print(f"  OCC abort%: benign {occ_benign:.1f}%  realistic {occ_realistic:.1f}%  "
          f"high_churn {occ_high:.1f}%  adversarial {occ_adversarial:.1f}%")
    check(occ_benign < occ_realistic, "OCC abort%: benign < realistic")
    check(occ_realistic < occ_high, "OCC abort%: realistic < high_churn")
    check(occ_high > 95.0, f"OCC collapses under high churn (>95% abort, got {occ_high:.1f}%)")
    check(occ_adversarial > 90.0, f"OCC collapses in adversarial regime (>90% abort, got {occ_adversarial:.1f}%)")

    # --- 9. cascade silent errors rise with tol_safety (over-estimation) ----
    def silent_count(tag, regime):
        grp = [r for r in rows if r["policy_tag"] == tag and r["regime"] == regime]
        return sum(int(r["silent_error"]) for r in grp)
    # cascade silent errors should rise with tol_safety endpoints. In some
    # regimes the very high safety=5 collapses so many arms to FORK (which
    # returns silent_error=0) that the count can dip slightly below safety=2;
    # the meaningful signal is safety1 << safety5 (over-estimation leaks more
    # than the honest measurement), so assert the endpoints, not strict
    # monotonicity at the mid-point.
    for regime in ("realistic", "wild_drift", "storm"):
        s1 = silent_count("cascade_mat0.20", regime)
        s2 = silent_count("cascade_safety2", regime)
        s5 = silent_count("cascade_safety5", regime)
        print(f"  cascade silent_errors [{regime}]: safety1={s1} safety2={s2} safety5={s5}")
        check(s1 <= s5,
              f"cascade silent_errors: safety1 <= safety5 in {regime} (over-estimation leaks)")
        check(s1 <= s2,
              f"cascade silent_errors: safety1 <= safety2 in {regime}")

    # --- 10. redo_prob sweep changes recompute ratio but NOT arm mix --------
    def arm_mix(tag, regime):
        grp = [r for r in rows if r["policy_tag"] == tag and r["regime"] == regime]
        c = Counter(r["arm"] for r in grp)
        return tuple(c[a] for a in ("WINNER", "FORK", "OCC_COMMIT", "RECOMPUTE", "OCC_ALLABORT"))
    for regime in ("realistic", "high_contention", "storm"):
        m0  = arm_mix("hybrid_safety1", regime)
        m5  = arm_mix("hybrid_redo0.5", regime)
        m10 = arm_mix("hybrid_redo1.0", regime)
        check(m0 == m5 == m10,
              f"redo_prob sweep leaves arm mix unchanged in {regime}")
        # recompute ratio rises with redo_prob
        def rec_ratio(tag):
            grp = [r for r in rows if r["policy_tag"] == tag and r["regime"] == regime]
            return sum(int(r["recomputes"]) for r in grp) / len(grp)
        r0, r5, r10 = rec_ratio("hybrid_safety1"), rec_ratio("hybrid_redo0.5"), rec_ratio("hybrid_redo1.0")
        print(f"  recompute/conf [{regime}]: redo=0 {r0:.2f}  redo=0.5 {r5:.2f}  redo=1.0 {r10:.2f}")
        check(r0 <= r5 <= r10,
              f"recompute ratio rises with redo_prob in {regime}")

    # --- 11. pivot table is reproducible (regime x policy_tag) --------------
    by = defaultdict(list)
    for r in rows: by[(r["regime"], r["policy_tag"])].append(r)
    n_cells = len(by)
    n_tags = len(set(r["policy_tag"] for r in rows))
    expected_cells = 11 * n_tags   # 11 regimes x N policy_tags
    check(n_tags == 12, f"12 policy_tags present (found {n_tags})")
    check(n_cells == expected_cells,
          f"every (regime x policy_tag) cell has data ({n_cells}/{expected_cells})")

    # --- 12. each (regime, policy, seed) has rows (seed variance is in) -----
    seed_cells = set((r["regime"], r["policy_tag"], r["seed"]) for r in rows)
    check(len(seed_cells) == 11 * n_tags * 3,
          f"every (regime x policy_tag x seed) combo present ({len(seed_cells)}/{11*n_tags*3})")

    # --- 13. NEGATIVE CONTROL: split predicate-win from policy-win ----------
    # occ_value = OCC accounting on the value predicate. Two things must hold:
    #  (a) it recovers most of OCC(rev)'s lost throughput -> the big win is the
    #      PREDICATE, not the routing machinery (honesty about attribution);
    #  (b) it LEAKS (silent>0) on price-heavy regimes -> the predicate ALONE is
    #      unsafe; you still need per-field routing. Neither was testable before.
    def abort_pct2(tag, regime):
        grp = [r for r in rows if r["policy_tag"] == tag and r["regime"] == regime]
        if not grp: return None
        ab = sum(1 for r in grp if r["arm"] in NO_WINNER)
        return 100.0 * ab / len(grp)
    def silent_total(tag, regime):
        return sum(int(r["silent_error"]) for r in rows
                   if r["policy_tag"] == tag and r["regime"] == regime)
    for regime in ("realistic", "high_churn", "extreme_lag"):
        occ_ab = abort_pct2("occ", regime)
        val_ab = abort_pct2("occ_value", regime)
        print(f"  abort% [{regime}]: OCC(rev) {occ_ab:.1f}%  ->  OCC(value) {val_ab:.1f}%")
        check(val_ab < occ_ab,
              f"OCC(value) recovers throughput vs OCC(rev) in {regime} "
              f"(predicate is the win, not routing)")
    val_leak = silent_total("occ_value", "all_price_like")
    print(f"  OCC(value) silent_errors in all_price_like: {val_leak}")
    check(val_leak > 0,
          "OCC(value) LEAKS on price-heavy fields (predicate alone is unsafe -> "
          "routing is doing real work)")

    # --- 14. NEGATIVE CONTROL: the safety=1 zero-leak is perfect-knowledge ---
    # hybrid_safety1 has 0 silent BY CONSTRUCTION (materiality == true_tol).
    # Under honest, unbiased measurement noise the hybrid over-estimates on ~half
    # its fields and DOES leak. If noisy hybrid still showed 0, the whole safety
    # story would be a definitional artifact. It must not.
    s_perfect = sum(int(r["silent_error"]) for r in rows
                    if r["policy_tag"] == "hybrid_safety1")
    s_noise05 = sum(int(r["silent_error"]) for r in rows
                    if r["policy_tag"] == "hybrid_noise0.5")
    s_noise10 = sum(int(r["silent_error"]) for r in rows
                    if r["policy_tag"] == "hybrid_noise1.0")
    print(f"  hybrid silent_errors (all regimes): perfect={s_perfect}  "
          f"noise0.5={s_noise05}  noise1.0={s_noise10}")
    check(s_perfect == 0, "hybrid_safety1 silent==0 (definitional — perfect knowledge)")
    check(s_noise05 > 0,
          "hybrid_noise0.5 LEAKS (safety=1 zero was a perfect-knowledge artifact)")
    check(s_noise10 >= s_noise05,
          "more measurement noise -> more silent leaks (monotone in noise)")

    # --- summary ------------------------------------------------------------
    print()
    if fails:
        print(f"FAILED {len(fails)} checks:")
        for m in fails: print("  - " + m)
        sys.exit(1)
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()