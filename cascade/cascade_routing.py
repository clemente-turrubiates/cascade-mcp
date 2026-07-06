#!/usr/bin/env python3
"""
cascade_routing.py — pure OCC vs pure cascade vs per-field HYBRID.

THESIS (so reviewers stop relitigating cascade):
  cascade is the cheap arm, not a correctness mechanism. Correctness lives in
  the router. The router's correctness reduces to tolerance-estimate integrity.
  There are three independent ways integrity fails, each quantified in `main()`:
    [12] tol_safety bias            (config lies about tolerance estimate)
    [13] writer-asserted tolerance  (write self-certifies its own error bar)
    noise (tol_est_noise)           (honest but imperfect measurement)
  The audit canary (audit_canary) is what saves you when routing is wrong: a
  self-detectable leak proxy that needs NO ground-truth true_tol.

Fair head-to-head: forked/committed fields keep contending (no freeze), so
conflict volumes are comparable across policies. Tracks BOTH costs:
  recomputes    wasted expensive re-runs (OCC overpays these under churn)
  silent_errors committed-but-actually-wrong values (cascade risks these)
  audit_disagreements  cascade commits an OCC rev-check would have rejected
                       (the canary; observable WITHOUT true_tol ground truth)

Each field has a TRUE tolerance (drift its answer can absorb). Hybrid routes
zero-tolerance fields to OCC (safe) and tolerant fields to the semantic cascade
with materiality = measured tolerance. tol_safety>1 models OVER-estimating it.
fresh_loser_redo_prob models the assumption that a fresh loser re-runs anyway.

TRUST BOUNDARY: a writer-supplied tolerance is NOT ground truth. The field's
true_tol is set at configure and is immutable at write time. The
trust_writer_tolerance knob (wired in server.py, modeled in experiment [13])
turns the self-certifying-writer hole back ON so it can be measured in the same
silent_errors currency as everything else.
"""
from __future__ import annotations
import math
import random
from dataclasses import dataclass
from collections import Counter

@dataclass
class Field:
    id: str; level: int; deps: list
    rev: int = 0; value: float = 1.0
    true_tol: float = 0.25; policy: str = "cascade"; materiality: float = 0.20

@dataclass
class Write:
    tier: int; conf: float; read_rev: dict; read_val: dict

@dataclass
class Config:
    n_levels: int = 3; fields_per_level: int = 6; deps_per_field: int = 3
    conf_levels: tuple = (0.5, 0.7, 0.85, 0.95, 0.99)
    rounds: int = 12000
    source_write_prob: float = 0.25; contention_prob: float = 0.30
    width: tuple = (2, 3); lag: int = 4; value_drift: float = 0.08
    frac_zero_tol: float = 0.30; zero_tol: float = 0.01; gen_tol: float = 0.25
    policy: str = "hybrid"; global_materiality: float = 0.20
    route_threshold: float = 0.05; tol_safety: float = 1.0
    # multiplicative log-normal noise on the per-field tolerance ESTIMATE.
    # tol_safety is systematic bias; tol_est_noise is the honest "you measured
    # it, but imperfectly" spread. At >0 the hybrid over-estimates on ~half its
    # fields even with tol_safety=1 -> silent errors appear (the safety=1 zero
    # is a perfect-knowledge artifact, not a property of the design).
    tol_est_noise: float = 0.0
    # If True, a writer-supplied tolerance redefines the field's true_tol AND
    # re-derives routing (the self-certifying-writer hole, Finding 2). Default
    # False: true_tol is configure-authoritative and immutable at write time.
    # Exposed so [13] can reproduce the hole as a switchable regime, not a bug.
    trust_writer_tolerance: bool = False
    # Audit canary fraction: on this fraction of cascade commits, also run the
    # OCC rev-check and record a disagreement if the two disagree. Gives the
    # system an OBSERVABLE estimate of its own leak rate WITHOUT true_tol
    # ground truth — the thing you'd actually need to trust this in the wild.
    audit_canary_prob: float = 0.0
    # Writer-asserted tolerance inflation factor for [13]. >1 -> every writer
    # declares true_tol*scale, so under trust_writer_tolerance=True the field
    # gets a looser error bar AND a self-certifying write. Models the
    # unprivileged-writer attack path.
    writer_tol_inflation: float = 1.0
    fresh_loser_redo_prob: float = 0.0; seed: int = 0

def drift(w, F):
    return max((abs(F[d].value/v0 - 1.0) if v0 else 0.0 for d, v0 in w.read_val.items()), default=0.0)
def rev_stale(w, F):
    return any(F[d].rev > s for d, s in w.read_rev.items())
def bump(f, rng, cfg):
    f.rev += 1; f.value *= (1.0 + rng.gauss(0.0, cfg.value_drift))

def build(cfg, rng):
    F, by = {}, []
    for lvl in range(cfg.n_levels):
        ids = [f"L{lvl}_{i}" for i in range(cfg.fields_per_level)]; by.append(ids)
        for fid in ids:
            deps = [] if lvl == 0 else rng.sample(
                [x for p in by[:lvl] for x in p], min(cfg.deps_per_field, cfg.fields_per_level*lvl))
            f = Field(fid, lvl, deps)
            if lvl > 0:
                f.true_tol = cfg.zero_tol if rng.random() < cfg.frac_zero_tol else cfg.gen_tol
                measured = f.true_tol * cfg.tol_safety
                if cfg.tol_est_noise > 0.0:
                    measured *= math.exp(rng.gauss(0.0, cfg.tol_est_noise))
                if cfg.policy == "occ": f.policy = "occ"
                # occ_value: OCC decision rule (commit-any-fresh, all losers
                # rerun, no fork/authority) on the VALUE predicate. Isolates the
                # staleness-predicate win from the routing/arbitration win.
                elif cfg.policy == "occ_value":
                    f.policy, f.materiality = "occ_value", cfg.global_materiality
                elif cfg.policy == "cascade": f.policy, f.materiality = "cascade", cfg.global_materiality
                else:
                    if measured < cfg.route_threshold: f.policy = "occ"
                    else: f.policy, f.materiality = "cascade", measured
            F[fid] = f
    return F

def resolve(f, grp, F, cfg, rng):
    if f.policy == "occ":
        fresh = [w for w in grp if not rev_stale(w, F)]
        if fresh: return True, len(grp) - 1, 0, "OCC_COMMIT"
        return False, len(grp), 0, "OCC_ALLABORT"
    if f.policy == "occ_value":
        # value predicate + OCC accounting: commit one fresh, ALL losers rerun
        # (no free adoption), no fork/authority. Can leak, because a flat global
        # materiality has no idea of this field's true tolerance.
        fresh = [w for w in grp if drift(w, F) <= f.materiality]
        if not fresh: return False, len(grp), 0, "OCC_ALLABORT"
        silent = 1 if drift(fresh[0], F) > f.true_tol else 0
        return True, len(grp) - 1, silent, "OCC_COMMIT"
    m = f.materiality
    fresh = [w for w in grp if drift(w, F) <= m]; n_stale = len(grp) - len(fresh)
    if not fresh: return False, len(grp), 0, "RECOMPUTE"
    redo = n_stale + sum(1 for _ in range(len(fresh) - 1) if rng.random() < cfg.fresh_loser_redo_prob)
    bt = min(w.tier for w in fresh); top = [w for w in fresh if w.tier == bt]
    if len(top) > 1:
        bc = max(w.conf for w in top); top = [w for w in top if w.conf == bc]
    if len(top) > 1: return True, redo, 0, "FORK"
    silent = 1 if drift(top[0], F) > f.true_tol else 0
    return True, redo, silent, "WINNER"

def audit_disagreement(f, grp, F, winner):
    """Canary check for cascade commits: would the OCC rev-predicate have
    rejected this commit? Returns 1 if the OCC check would have aborted
    (the winner is rev-stale w.r.t. its logged read-set) while the cascade
    value-predicate committed. This is OBSERVABLE WITHOUT true_tol — it's
    purely the two routing arms disagreeing on the same batch. A sustained
    non-zero rate is the system's self-detected signal that its tolerance
    estimates are mis-routing fields (the [13] leak proxy)."""
    if winner is None: return 0
    return 1 if rev_stale(winner, F) else 0

def run(cfg):
    rng = random.Random(cfg.seed); F = build(cfg, rng)
    src = [f for f in F.values() if f.level == 0]; der = [f for f in F.values() if f.level > 0]
    topo = sorted(F.values(), key=lambda f: f.level); pending, since, M = {}, {}, Counter()
    for r in range(cfg.rounds):
        for f in src:
            if rng.random() < cfg.source_write_prob: bump(f, rng, cfg)
        for f in der:
            if f.id in pending: continue
            if rng.random() < cfg.contention_prob:
                grp = [Write(2, rng.choice(cfg.conf_levels),
                             {d: F[d].rev for d in f.deps}, {d: F[d].value for d in f.deps})
                       for _ in range(rng.randint(*cfg.width))]
                pending[f.id] = grp; since[f.id] = r
        for f in topo:
            g = pending.get(f.id)
            if g is None or r - since[f.id] < cfg.lag: continue
            # TRUST BOUNDARY: writer-asserted tolerance is advisory unless
            # trust_writer_tolerance turns the hole back ON for [13]. When ON,
            # every writer declares true_tol*writer_tol_inflation and the
            # field's true_tol is redefined per-write — both the routing and
            # the silent_error bar relax, so the write self-certifies.
            if cfg.trust_writer_tolerance and cfg.writer_tol_inflation != 1.0:
                f.true_tol = f.true_tol * cfg.writer_tol_inflation
                if cfg.policy == "hybrid":
                    measured = f.true_tol * cfg.tol_safety
                    if cfg.tol_est_noise > 0.0:
                        measured *= math.exp(rng.gauss(0.0, cfg.tol_est_noise))
                    if measured < cfg.route_threshold:
                        f.policy = "occ"
                    else:
                        f.policy = "cascade"; f.materiality = measured
            committed, redo, silent, arm = resolve(f, g, F, cfg, rng)
            M["conflicts"] += 1; M["recomputes"] += redo; M["silent_errors"] += silent; M[arm] += 1
            # audit canary: on a sampled fraction of cascade commits, run the
            # OCC rev-check too and record a disagreement if it would have
            # rejected. Observable leak proxy WITHOUT true_tol ground truth.
            if (committed and f.policy in ("cascade", "occ_value")
                    and rng.random() < cfg.audit_canary_prob):
                M["audit_checks"] += 1
                # winner = the first fresh write (the one cascade committed)
                m = f.materiality
                fresh = [w for w in g if drift(w, F) <= m]
                winner = fresh[0] if fresh else None
                M["audit_disagreements"] += audit_disagreement(f, g, F, winner)
            if committed: M["commits"] += 1; bump(f, rng, cfg)   # keeps contending; no freeze
            del pending[f.id]; del since[f.id]
    return M

def rep(label, M):
    c = M["conflicts"] or 1; cm = M["commits"] or 1
    line = (f"  {label:<32} {M['recomputes']/c:4.2f} recompute/conflict   "
            f"{M['recomputes']/cm:5.2f} recompute/commit   "
            f"silent_err {M['silent_errors']:>4}   conflicts {M['conflicts']:>6}")
    if M.get("audit_checks", 0) > 0:
        ad = M["audit_disagreements"]; ac = M["audit_checks"]
        line += f"   audit {ad}/{ac} disagree"
    print(line)

def main():
    print("=" * 104)
    print("POLICY HEAD-TO-HEAD (lag=4; 30% price-like tol~0.01, 70% estimate-like tol~0.25)")
    print("=" * 104)
    print("\n[10] pure OCC(rev)  vs  OCC(value)  vs  pure CASCADE(0.20)  vs  HYBRID(routed)")
    rep("pure OCC (rev-staleness)", run(Config(policy="occ")))
    rep("OCC (value-staleness 0.20)", run(Config(policy="occ_value", global_materiality=0.20)))
    rep("pure CASCADE (global 0.20)", run(Config(policy="cascade", global_materiality=0.20)))
    rep("HYBRID (measured tol)", run(Config(policy="hybrid")))
    print("     OCC(value) vs OCC(rev): the throughput gain that is JUST the predicate,")
    print("     not the routing. OCC(value) silent_err > 0: the predicate alone is unsafe.")

    print("\n[10b] HYBRID under NOISY (imperfect) tolerance measurement, tol_safety=1")
    print("     the safety=1 zero-leak is a perfect-knowledge artifact; noise leaks:")
    for nz in (0.0, 0.5, 1.0):
        rep(f"hybrid tol_est_noise={nz:.1f}", run(Config(policy="hybrid", tol_est_noise=nz)))

    print("\n[11] survives the 'fresh loser adopts winner free' assumption being switched off?")
    for p in (0.0, 0.5, 1.0):
        rep(f"hybrid redo_prob={p:.1f}", run(Config(policy="hybrid", fresh_loser_redo_prob=p)))

    print("\n[12] over-estimating tolerance: PURE CASCADE (bites hard) vs HYBRID (only cascade-routed fields)")
    for s in (1.0, 2.0, 5.0):
        rep(f"cascade tol_safety={s:.0f}", run(Config(policy="cascade", global_materiality=0.20 * s)))
    for s in (1.0, 2.0, 5.0):
        rep(f"hybrid  tol_safety={s:.0f}", run(Config(policy="hybrid", tol_safety=s)))
    print("     Measure tolerance conservatively (safety<=1) -> silent_err stays 0.")

    print("\n[13] TRUST BOUNDARY: writer-asserted tolerance (the self-cert hole)")
    print("     trust_writer_tolerance=True lets each writer redefine true_tol.")
    print("     A looser tolerance relaxes BOTH routing AND the silent_error bar,")
    print("     so the write is correct by construction. With audit_canary on, the")
    print("     system detects the leak WITHOUT true_tol ground truth.")
    for infl in (1.0, 2.0, 5.0):
        rep(f"hybrid writer_infl={infl:.0f} OFF",
            run(Config(policy="hybrid")))  # baseline: advisory ignored
    for infl in (1.0, 2.0, 5.0):
        rep(f"hybrid writer_infl={infl:.0f} ON (hole)",
            run(Config(policy="hybrid", trust_writer_tolerance=True,
                       writer_tol_inflation=infl)))
    print("     -> silent_err collapses to ~0 under the hole (self-certified);")
    print("     the metric that's supposed to catch the misroute is the number the")
    print("     attacker just supplied. No configure call, no elevated authority.")
    print("\n[13b] audit canary: the self-detectable leak proxy (no true_tol oracle)")
    for infl in (1.0, 2.0, 5.0):
        rep(f"hybrid infl={infl:.0f} canary=1.0",
            run(Config(policy="hybrid", trust_writer_tolerance=True,
                       writer_tol_inflation=infl, audit_canary_prob=1.0)))
    print("     -> audit_disagreements rises with inflation even though silent_err")
    print("     was self-certified to ~0. The canary sees what silent_err can't.")
    print("=" * 104)

if __name__ == "__main__":
    main()
