#!/usr/bin/env python3
"""
gen_agent_logs.py — robust stress-test CSV of per-conflict agent resolution
events. Reuses the cascade_routing engine but runs it across a curated grid of
deployment regimes (benign -> realistic -> adversarial) crossed with every
policy, so the resulting CSV cannot be cherry-picked: each policy is forced to
face the corner where it breaks.

Per row = one resolved contention group. Regime provenance is carried on every
row so any slice is reproducible:

  seed, regime, source_write_prob, lag, deps_per_field, contention_width_lo,
  contention_width_hi, frac_zero_tol, value_drift, tol_safety,
  fresh_loser_redo_prob, policy, policy_tag, rounds

plus the per-event data:

  field, level, read_time, resolve_time, n_writers, n_stale, win_tier,
  top_confidence, dependency_values, arm, recomputes, silent_error

Run (from the repo root):
  python scripts/gen_agent_logs.py > agent_logs.csv
  (writes UTF-8; pipe to a file via your shell, NOT PowerShell `>` which
   re-encodes to UTF-16 and corrupts the CSV.)
"""
from __future__ import annotations

import csv
import os
import random
import sys
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cascade import cascade_routing as cr

# ----------------------------- regime grid -----------------------------------
# Each regime pins a coherent deployment corner. We don't do a giant cross
# product; we force every policy to face its specific failure mode.

def R(**kw):
    base = dict(
        rounds=8000,
        n_levels=3, fields_per_level=6, deps_per_field=3,
        source_write_prob=0.25, contention_prob=0.30,
        width=(2, 3), lag=4, value_drift=0.08,
        frac_zero_tol=0.30, zero_tol=0.01, gen_tol=0.25,
        fresh_loser_redo_prob=0.0, tol_safety=1.0,
        route_threshold=0.05, global_materiality=0.20,
    )
    base.update(kw)
    return cr.Config(**base)


REGIMES = [
    # name,            Config
    ("benign",         R(source_write_prob=0.05, lag=1, value_drift=0.04,
                          frac_zero_tol=0.0, deps_per_field=1)),
    ("realistic",      R(source_write_prob=0.25, lag=4, value_drift=0.08,
                          frac_zero_tol=0.30, deps_per_field=3)),
    ("high_churn",     R(source_write_prob=0.60, lag=4, value_drift=0.08,
                          frac_zero_tol=0.30, deps_per_field=3)),
    ("extreme_lag",    R(source_write_prob=0.25, lag=16, value_drift=0.08,
                          frac_zero_tol=0.30, deps_per_field=3)),
    ("wide_deps",      R(source_write_prob=0.25, lag=4, value_drift=0.08,
                          frac_zero_tol=0.30, deps_per_field=6)),
    ("high_contention", R(source_write_prob=0.25, lag=4, value_drift=0.08,
                          frac_zero_tol=0.30, width=(4, 8))),
    ("all_price_like", R(source_write_prob=0.25, lag=4, value_drift=0.08,
                          frac_zero_tol=1.0)),       # every field is zero-tol
    ("wild_drift",     R(source_write_prob=0.25, lag=4, value_drift=0.20,
                          frac_zero_tol=0.30)),
    ("calm_but_wide",  R(source_write_prob=0.10, lag=2, value_drift=0.04,
                          frac_zero_tol=0.30, deps_per_field=6, width=(4, 8))),
    ("storm",          R(source_write_prob=0.50, lag=8, value_drift=0.15,
                          frac_zero_tol=0.50, deps_per_field=4, width=(3, 6))),
    # adversarial: high churn + long lag + wild drift + many price-like fields.
    # The regime where cascade's silent errors should explode and OCC's aborts
    # should dominate. This is the "do not ship" corner.
    ("adversarial_all", R(source_write_prob=0.60, lag=16, value_drift=0.20,
                          frac_zero_tol=0.70, deps_per_field=4, width=(3, 6))),
]

# Policy variants run under EVERY regime. Each is tagged with the swept param
# so the per-policy cross-regime table can be sliced out of the CSV.
def policy_variants(regime: cr.Config):
    out = []
    out.append(("occ",                  replace(regime, policy="occ")))
    # control arm: OCC accounting on the VALUE predicate. Splits the
    # staleness-predicate win from the routing/arbitration win.
    out.append(("occ_value",            replace(regime, policy="occ_value",
                                               global_materiality=0.20)))
    out.append(("cascade_mat0.20",      replace(regime, policy="cascade",
                                               global_materiality=0.20)))
    out.append(("hybrid_safety1",       replace(regime, policy="hybrid",
                                               tol_safety=1.0)))
    # honest hybrid: tolerance MEASURED imperfectly (unbiased log-normal noise).
    # Unlike safety1's tautological zero, these leak -> the real safety story.
    out.append(("hybrid_noise0.5",      replace(regime, policy="hybrid",
                                               tol_safety=1.0, tol_est_noise=0.5)))
    out.append(("hybrid_noise1.0",      replace(regime, policy="hybrid",
                                               tol_safety=1.0, tol_est_noise=1.0)))
    # tolerance over-estimation (silent-error story) — bites cascade hardest
    out.append(("cascade_safety2",      replace(regime, policy="cascade",
                                               global_materiality=0.40)))
    out.append(("cascade_safety5",      replace(regime, policy="cascade",
                                               global_materiality=1.00)))
    out.append(("hybrid_safety2",       replace(regime, policy="hybrid",
                                               tol_safety=2.0)))
    out.append(("hybrid_safety5",       replace(regime, policy="hybrid",
                                               tol_safety=5.0)))
    # fresh-loser redo assumption (recompute story) — hybrid only
    out.append(("hybrid_redo0.5",       replace(regime, policy="hybrid",
                                               fresh_loser_redo_prob=0.5)))
    out.append(("hybrid_redo1.0",       replace(regime, policy="hybrid",
                                               fresh_loser_redo_prob=1.0)))
    return out

SEEDS = (0, 7, 42)


# ----------------------------- logging run -----------------------------------
def dep_str(deps, w):
    return "|".join(f"{d}:{w.read_rev[d]}:{w.read_val[d]:.6g}" for d in deps)


def run_with_logs(cfg: cr.Config, regime_name: str, seed: int,
                  policy_tag: str, writer):
    cfg = replace(cfg, seed=seed)
    rng = random.Random(cfg.seed)
    F = cr.build(cfg, rng)
    src = [f for f in F.values() if f.level == 0]
    der = [f for f in F.values() if f.level > 0]
    topo = sorted(F.values(), key=lambda f: f.level)
    pending, since = {}, {}
    w_lo, w_hi = cfg.width
    for r in range(cfg.rounds):
        for f in src:
            if rng.random() < cfg.source_write_prob:
                cr.bump(f, rng, cfg)
        for f in der:
            if f.id in pending:
                continue
            if rng.random() < cfg.contention_prob:
                grp = [cr.Write(2, rng.choice(cfg.conf_levels),
                                {d: F[d].rev for d in f.deps},
                                {d: F[d].value for d in f.deps})
                       for _ in range(rng.randint(w_lo, w_hi))]
                pending[f.id] = grp
                since[f.id] = r
        for f in topo:
            g = pending.get(f.id)
            if g is None or r - since[f.id] < cfg.lag:
                continue
            committed, redo, silent, arm = cr.resolve(f, g, F, cfg, rng)
            read_time = since[f.id]
            resolve_time = r
            n_stale = sum(1 for w in g
                          if (cr.rev_stale(w, F) if f.policy == "occ"
                              else cr.drift(w, F) > f.materiality))
            if arm in ("WINNER", "FORK", "OCC_COMMIT"):
                fresh = [w for w in g if (not cr.rev_stale(w, F)
                                          if f.policy == "occ"
                                          else cr.drift(w, F) <= f.materiality)]
                bt = min(w.tier for w in fresh)
                top = [w for w in fresh if w.tier == bt]
                bc = max(w.conf for w in top)
                top = [w for w in top if w.conf == bc]
                win_tier = top[0].tier
                top_conf = top[0].conf
            else:
                win_tier = -1
                top_conf = -1.0
            dep_vals = dep_str(f.deps, g[0]) if f.deps else ""
            writer.writerow({
                "seed": seed,
                "regime": regime_name,
                "source_write_prob": cfg.source_write_prob,
                "lag": cfg.lag,
                "deps_per_field": cfg.deps_per_field,
                "contention_width_lo": w_lo,
                "contention_width_hi": w_hi,
                "frac_zero_tol": cfg.frac_zero_tol,
                "value_drift": cfg.value_drift,
                "tol_safety": cfg.tol_safety,
                "fresh_loser_redo_prob": cfg.fresh_loser_redo_prob,
                "rounds": cfg.rounds,
                "policy": f.policy,
                "policy_tag": policy_tag,
                "field": f.id,
                "level": f.level,
                "read_time": read_time,
                "resolve_time": resolve_time,
                "n_writers": len(g),
                "n_stale": n_stale,
                "win_tier": win_tier,
                "top_confidence": f"{top_conf:.4f}",
                "dependency_values": dep_vals,
                "arm": arm,
                "recomputes": redo,
                "silent_error": silent,
                "true_tol": f"{f.true_tol:.6g}",
            })
            if committed:
                cr.bump(f, rng, cfg)
            del pending[f.id]
            del since[f.id]


# ----------------------------- driver ---------------------------------------
def main():
    fields = [
        "seed", "regime", "source_write_prob", "lag", "deps_per_field",
        "contention_width_lo", "contention_width_hi", "frac_zero_tol",
        "value_drift", "tol_safety", "fresh_loser_redo_prob", "rounds",
        "policy", "policy_tag", "field", "level",
        "read_time", "resolve_time", "n_writers", "n_stale",
        "win_tier", "top_confidence", "dependency_values", "arm",
        "recomputes", "silent_error", "true_tol",
    ]
    w = csv.DictWriter(sys.stdout, fieldnames=fields, lineterminator="\n")
    w.writeheader()
    n_runs = 0
    for regime_name, regime_cfg in REGIMES:
        for seed in SEEDS:
            for policy_tag, cfg in policy_variants(regime_cfg):
                run_with_logs(cfg, regime_name, seed, policy_tag, w)
                n_runs += 1
    print(f"# generated {n_runs} runs across {len(REGIMES)} regimes x "
          f"{len(SEEDS)} seeds", file=sys.stderr)


if __name__ == "__main__":
    main()
