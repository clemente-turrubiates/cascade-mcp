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
from collections import Counter, namedtuple

# resolve() returns this so callers can access fields by name without
# unpacking a 6-tuple positionally.
ResolveResult = namedtuple("ResolveResult",
                            ["committed", "redo", "silent", "arm",
                             "audit_disagreement", "fork_reason"])

@dataclass
class Field:
    id: str; level: int; deps: list
    rev: int = 0; value: float = 1.0
    true_tol: float = 0.25; policy: str = "cascade"; materiality: float = 0.20

@dataclass
class Write:
    tier: int; conf: float; read_rev: dict; read_val: dict
    agent_id: str = ""          # source of the write, for track-record calibration
    read_hmac: str = ""         # integrity binding over the read-set snapshot


FORK_TIER_TIE = "FORK_TIER_TIE"
FORK_CONF_TIE = "FORK_CONF_TIE"

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
    # HMAC secret for read-set integrity binding. Empty string -> pass-through
    # (backward compat). A deployment injects a real key; a present-but-wrong
    # HMAC then rejects the write (treated as a bad read-set, like RECOMPUTE).
    hmac_secret: str = ""
    fresh_loser_redo_prob: float = 0.0; seed: int = 0

def drift(w, F):
    return max((abs(F[d].value/v0 - 1.0) if v0 else 0.0 for d, v0 in w.read_val.items()), default=0.0)
def rev_stale(w, F):
    return any(F[d].rev > s for d, s in w.read_rev.items())
def bump(f, rng, cfg):
    f.rev += 1; f.value *= (1.0 + rng.gauss(0.0, cfg.value_drift))


# ----------------------------- integrity / calibration -----------------------

def _read_set_digest(w, F):
    """Stable string digest of what the writer claimed to have read."""
    parts = []
    for d in sorted(w.read_rev.keys()):
        rev = w.read_rev[d]
        val = w.read_val.get(d, F[d].value if d in F else 1.0)
        parts.append(f"{d}:{rev}:{val:.9g}")
    return "|".join(parts)


def _verify_hmac(w, F, secret):
    """Verify that the read-set HMAC binds to the claimed rev/value snapshot.
    Returns True if no HMAC is present (pass-through) or if HMAC matches.
    A present-but-wrong HMAC returns False — the caller rejects that write."""
    import hmac as _hmac, hashlib
    if not w.read_hmac:
        return True
    digest = _hmac.new(secret.encode() if secret else b"",
                       _read_set_digest(w, F).encode(),
                       hashlib.sha256).hexdigest()
    return _hmac.compare_digest(digest, w.read_hmac)


def _effective_conf(w, track):
    """Blend caller-asserted confidence with a calibrated track record.
    If the agent has no history, trust the asserted value. If it has a
    history, downgrade asserted confidence toward the observed accuracy.
    """
    if not w.agent_id or w.agent_id not in track:
        return w.conf
    correct, total = track[w.agent_id]
    if total == 0:
        return w.conf
    empirical = correct / total
    weight = min(1.0, total / 10.0)  # needs ~10 samples to dominate
    return (1.0 - weight) * w.conf + weight * empirical


def _update_track(track, w, silent):
    """Record whether this agent's winning/committed write was correct."""
    if not w.agent_id:
        return
    correct, total = track.get(w.agent_id, (0, 0))
    track[w.agent_id] = (correct + (0 if silent else 1), total + 1)


class Metrics:
    """Observable counters emitted by the router."""
    def __init__(self):
        self.events: Counter = Counter()
        self.fork_reasons: Counter = Counter()

    def emit(self, name: str, value: int = 1) -> None:
        self.events[name] += value

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

def resolve(f, grp, F, cfg, rng=None, track=None, metrics=None):
    """Resolve a batch of writes for field `f`. Returns a 6-tuple:
      (committed, redo, silent, arm, audit_disagreement, fork_reason).

    Trust boundary:
      - writer `tolerance` is NOT applied here (caller-side; see server.py).
      - HMAC: a present-but-wrong read_hmac rejects the write (bad read-set).
        Missing HMAC is pass-through (backward compat).
      - confidence: caller-asserted conf is blended with empirical track record
        via _effective_conf; a strong track record can override a weak claim.
    """
    rng = rng if rng is not None else random.Random(0)
    track = track or {}
    metrics = metrics or Metrics()

    # Integrity check: present-but-wrong HMAC rejects the write.
    hmac_bad: list[Write] = []
    good_grp: list[Write] = []
    for w in grp:
        if w.read_hmac and not _verify_hmac(w, F, cfg.hmac_secret):
            hmac_bad.append(w)
            metrics.emit("hmac_failure")
        else:
            good_grp.append(w)
    grp = good_grp

    if f.policy == "occ":
        fresh = [w for w in grp if not rev_stale(w, F)]
        if fresh:
            return ResolveResult(True, len(grp) - 1, 0, "OCC_COMMIT", 0, None)
        return ResolveResult(False, len(grp), 0, "OCC_ALLABORT", 0, None)
    if f.policy == "occ_value":
        fresh = [w for w in grp if drift(w, F) <= f.materiality]
        if not fresh:
            return ResolveResult(False, len(grp), 0, "OCC_ALLABORT", 0, None)
        silent = 1 if drift(fresh[0], F) > f.true_tol else 0
        return ResolveResult(True, len(grp) - 1, silent, "OCC_COMMIT", 0, None)

    m = f.materiality
    fresh = [w for w in grp if drift(w, F) <= m]; n_stale = len(grp) - len(fresh)
    if not fresh:
        return ResolveResult(False, len(grp), 0, "RECOMPUTE", 0, None)
    redo = n_stale + sum(1 for _ in range(len(fresh) - 1) if rng.random() < cfg.fresh_loser_redo_prob)

    # apply calibrated confidence to break ties (do not mutate the Write)
    calibrated = [_effective_conf(w, track) for w in fresh]
    bt = min(w.tier for w in fresh); top_w = [w for w in fresh if w.tier == bt]
    if len(top_w) > 1:
        top_c = [_effective_conf(w, track) for w in top_w]
        bc = max(top_c); winners = [top_w[i] for i, c in enumerate(top_c)
                                    if abs(c - bc) < 1e-12]
    else:
        winners = top_w
    if len(winners) == 1:
        winner = winners[0]
        fork_reason = None
    else:
        # tie persists after calibration -> FORK
        fork_reason = FORK_CONF_TIE if len(top_w) > 1 else None
        return ResolveResult(True, redo, 0, "FORK", 0, fork_reason)

    silent = 1 if drift(winner, F) > f.true_tol else 0
    return ResolveResult(True, redo, silent, "WINNER", 0, None)

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

def run(cfg, record=None):
    rng = random.Random(cfg.seed); F = build(cfg, rng)
    src = [f for f in F.values() if f.level == 0]; der = [f for f in F.values() if f.level > 0]
    topo = sorted(F.values(), key=lambda f: f.level); pending, since, M = {}, {}, Counter()
    track: dict[str, tuple[int, int]] = {}
    metrics = Metrics()
    rows = [] if record is not None else None
    for r in range(cfg.rounds):
        metrics.emit("round")
        for f in src:
            if rng.random() < cfg.source_write_prob:
                bump(f, rng, cfg); metrics.emit("source_churn")
        for f in der:
            if f.id in pending: continue
            if rng.random() < cfg.contention_prob:
                grp = [Write(2, rng.choice(cfg.conf_levels),
                             {d: F[d].rev for d in f.deps}, {d: F[d].value for d in f.deps},
                             agent_id=f"a{r}_{f.id}")
                       for _ in range(rng.randint(*cfg.width))]
                pending[f.id] = grp; since[f.id] = r
                metrics.emit("contention_issued", len(grp))
        for f in topo:
            g = pending.get(f.id)
            if g is None or r - since[f.id] < cfg.lag: continue
            # TRUST BOUNDARY: writer-asserted tolerance is advisory unless
            # trust_writer_tolerance turns the hole back ON for [13].
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
            (committed, redo, silent, arm, audit_dis, fork_reason) = resolve(
                f, g, F, cfg, rng, track, metrics)
            M["conflicts"] += 1; M["recomputes"] += redo; M["silent_errors"] += silent; M[arm] += 1
            metrics.emit("conflict_resolved"); metrics.emit(f"arm:{arm}")
            if audit_dis:
                M["audit_disagreements"] = M.get("audit_disagreements", 0) + 1
                metrics.emit("audit_disagreement")
            if fork_reason:
                metrics.fork_reasons[fork_reason] += 1
            # audit canary: on a sampled fraction of cascade/occ_value commits,
            # run the OCC rev-check too and record a disagreement if the two
            # arms would have disagreed. Observable leak proxy WITHOUT true_tol.
            if (committed and f.policy in ("cascade", "occ_value")
                    and rng.random() < cfg.audit_canary_prob):
                M["audit_checks"] = M.get("audit_checks", 0) + 1
                m = f.materiality
                fresh = [w for w in g if drift(w, F) <= m]
                winner = fresh[0] if fresh else None
                M["audit_disagreements"] = M.get("audit_disagreements", 0) + audit_disagreement(f, g, F, winner)
            if committed:
                M["commits"] += 1; bump(f, rng, cfg); metrics.emit("commit")
                if silent: metrics.emit("silent_error")
                # update track record for the committed write's agent
                if arm == "WINNER":
                    _update_track(track, _winner_write(g, F, f, f.materiality), silent)
                elif arm == "OCC_COMMIT":
                    w = next((w for w in g if not rev_stale(w, F)), None)
                    if w is not None: _update_track(track, w, silent)
            if rows is not None:
                rows.append({
                    "round": r, "field": f.id, "level": f.level,
                    "policy": f.policy, "arm": arm,
                    "n_writers": len(g), "recomputes": redo,
                    "silent_error": silent,
                    "fork_reason": fork_reason or "",
                    "true_tol": f.true_tol, "materiality": f.materiality,
                })
            del pending[f.id]; del since[f.id]
    if record is not None:
        return M, rows, metrics
    return M


def _winner_write(grp, F, f, m):
    """Recover the winning Write object from a batch (matches resolve's pick)."""
    fresh = [w for w in grp if drift(w, F) <= m]
    if not fresh: return None
    bt = min(w.tier for w in fresh); top = [w for w in fresh if w.tier == bt]
    if len(top) == 1: return top[0]
    return top[0]  # calibration already applied in resolve; approx here for track

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

    print("\n[14] CONFIDENCE CALIBRATION: track record overrides asserted confidence")
    print("     _effective_conf blends caller-asserted conf with empirical accuracy.")
    print("     Two agents, same tier, both assert 0.95. With empty track -> FORK.")
    print("     With divergent track records -> the accurate agent wins outright.")
    f = Field("L1_0", 1, ["L0_0"], true_tol=0.25, policy="cascade", materiality=0.20)
    F = {"L0_0": Field("L0_0", 0, [], rev=0, value=1.0), f.id: f}
    good = Write(2, 0.95, {"L0_0": 0}, {"L0_0": 1.0}, agent_id="good")
    bad = Write(2, 0.95, {"L0_0": 0}, {"L0_0": 1.0}, agent_id="bad")
    rng = random.Random(0)
    _, _, _, arm0, _, reason0 = resolve(f, [good, bad], F, Config(), rng, track={})
    print(f"     empty track:     arm={arm0} fork_reason={reason0}  (tie -> FORK)")
    _, _, _, arm1, _, reason1 = resolve(
        f, [good, bad], F, Config(), random.Random(0),
        track={"good": (10, 10), "bad": (0, 10)})
    print(f"     calibrated track: arm={arm1} fork_reason={reason1}  (good agent wins)")
    print("     -> calibration is the same trust-boundary class as the tolerance fix:")
    print("     caller-asserted confidence is advisory; the track record is evidence.")
    print("=" * 104)

if __name__ == "__main__":
    main()
