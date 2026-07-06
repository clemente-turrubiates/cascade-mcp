#!/usr/bin/env python3
"""
cascade_sim.py — go/no-go instrumentation for the read-set-validated,
provenance-weighted resolution cascade (the "agent-mesh" thesis).

It simulates concurrent agent writes over a configurable dependency DAG and
classifies every conflict-resolution event into one of three arms:

  FRESH_WINNER  a live (non-stale) write wins on authority->confidence.
                Fully automatic, no re-run, no human. THIS is the value:
                the case where you beat OCC (no retry) and CodeCRDT (no LLM).

  FORK          two+ fresh writes tie on (authority, confidence). Lossless:
                you defer to a human/high-tier agent instead of silently
                dropping. Better than silent corruption, but not hands-off.

  RECOMPUTE     every competing write is premise-stale (all-stale). There is
                no correct value to pick -> you must re-run. Here you are NO
                BETTER than S-Bus OCC. This arm is the thesis-eroder.

Decision rule of thumb:
  - high FRESH_WINNER across realistic churn      -> strong GO
  - RECOMPUTE dominates once churn is realistic   -> "just use OCC", NO-GO
  - FORK meaningful only if confidence is coarse  -> design implication

The resolve() function below mirrors the Rust resolve_field() exactly:
staleness gate -> authority tier -> confidence -> fork-on-tie.
"""

from __future__ import annotations
import random
from dataclasses import dataclass, field
from collections import Counter
from typing import Optional


# ----------------------------- data model -----------------------------------

@dataclass
class Field:
    id: str
    level: int
    deps: list[str]
    rev: int = 0            # monotonic per-field revision, bumped on commit
    value: float = 1.0      # semantic content; perturbed on every commit/churn
    forked: bool = False


@dataclass
class Write:
    field_id: str
    agent_id: int
    authority_tier: int     # lower = more authoritative (Tier 0 = human/verified)
    confidence: float
    read_set: dict[str, int]     # dep_id -> dep.rev observed at read time
    read_vals: dict[str, float]  # dep_id -> dep.value observed at read time
    issued_round: int


# ----------------------------- config ----------------------------------------

@dataclass
class Config:
    # DAG shape
    n_levels: int = 3
    fields_per_level: int = 6
    deps_per_field: int = 3          # read-set size for derived fields

    # agents / authority
    n_agents: int = 8
    # P(tier). Tier 0 = human/verified source, Tier 1 = analyst, Tier 2 = swarm.
    tier_probs: tuple[float, float, float] = (0.0, 0.0, 1.0)  # homogeneous swarm

    # confidence: a coarse discrete scale (realistic for LLM/scraper self-report).
    # Set continuous=True to make exact ties vanish (fork arm collapses).
    confidence_levels: tuple[float, ...] = (0.5, 0.7, 0.85, 0.95, 0.99)
    continuous_confidence: bool = False

    # dynamics
    rounds: int = 4000
    source_write_prob: float = 0.25  # P(a source field churns each round)
    contention_prob: float = 0.30    # P(a derived field draws concurrent writes/round)
    contention_width: tuple[int, int] = (2, 3)  # inclusive range of concurrent writers
    resolution_lag: int = 1          # rounds between read and resolve (time pressure)

    # --- staleness model ---------------------------------------------------
    # rev-staleness (default): a write is stale iff ANY dependency's rev moved.
    #   This is the harsh upper bound — it flags "HQ city updated" as
    #   invalidating a revenue estimate that never touched the HQ field's value.
    # semantic-staleness: a write is stale iff a dependency's VALUE moved past
    #   `materiality` (relative). A rev can advance while the value drifts
    #   below threshold -> the write survives. This is CoAgent's "did the
    #   conflict actually invalidate my premise" test, made deterministic.
    semantic_staleness: bool = False
    materiality: float = 0.10        # relative value move that counts as stale
    value_drift: float = 0.08        # std-dev of relative value move per event

    seed: int = 0


# ----------------------------- core resolve ----------------------------------
# Mirrors Rust resolve_field(). Pure function of the competing writes + the
# already-resolved upstream revs (resolution happens in topological order, so
# every dependency is at its winning rev before this is called).

FRESH_WINNER = "FRESH_WINNER"
FORK = "FORK"
RECOMPUTE = "RECOMPUTE"


def is_stale(w: Write, fields: dict[str, Field], cfg: Config) -> bool:
    if not cfg.semantic_staleness:
        # rev-staleness: stale iff any dependency advanced past the recorded rev
        return any(fields[dep].rev > seen for dep, seen in w.read_set.items())
    # semantic-staleness: stale iff any dependency's VALUE moved past materiality
    for dep, seen_val in w.read_vals.items():
        now = fields[dep].value
        base = abs(seen_val) or 1.0
        if abs(now - seen_val) / base > cfg.materiality:
            return True
    return False


def resolve(writes: list[Write], fields: dict[str, Field],
            cfg: Config) -> tuple[str, Optional[Write]]:
    fresh = [w for w in writes if not is_stale(w, fields, cfg)]

    if len(fresh) == 0:
        return RECOMPUTE, None
    if len(fresh) == 1:
        return FRESH_WINNER, fresh[0]

    # authority: lowest tier number wins
    best_tier = min(w.authority_tier for w in fresh)
    top = [w for w in fresh if w.authority_tier == best_tier]
    if len(top) == 1:
        return FRESH_WINNER, top[0]

    # confidence: highest wins
    best_conf = max(w.confidence for w in top)
    winners = [w for w in top if w.confidence == best_conf]
    if len(winners) == 1:
        return FRESH_WINNER, winners[0]

    # exact tie on (authority, confidence) -> lossless fork
    return FORK, None


# ----------------------------- simulation ------------------------------------

def build_dag(cfg: Config, rng: random.Random) -> dict[str, Field]:
    fields: dict[str, Field] = {}
    by_level: list[list[str]] = []
    for lvl in range(cfg.n_levels):
        ids = [f"L{lvl}_{i}" for i in range(cfg.fields_per_level)]
        by_level.append(ids)
        for fid in ids:
            if lvl == 0:
                deps: list[str] = []
            else:
                pool = [x for prev in by_level[:lvl] for x in prev]
                k = min(cfg.deps_per_field, len(pool))
                deps = rng.sample(pool, k)
            fields[fid] = Field(id=fid, level=lvl, deps=deps)
    return fields


def draw_tier(cfg: Config, rng: random.Random) -> int:
    r, acc = rng.random(), 0.0
    for tier, p in enumerate(cfg.tier_probs):
        acc += p
        if r <= acc:
            return tier
    return len(cfg.tier_probs) - 1


def draw_conf(cfg: Config, rng: random.Random) -> float:
    if cfg.continuous_confidence:
        return rng.random()
    return rng.choice(cfg.confidence_levels)


def perturb(f: Field, cfg: Config, rng: random.Random) -> None:
    # a commit/churn moves the field's value by a random relative amount.
    # some events move it a lot (material), some barely (immaterial) — which
    # is exactly what semantic-staleness gets to distinguish and rev-staleness
    # cannot. Multiplicative so value never hits 0 (read_vals stay well-defined).
    f.value *= (1.0 + rng.gauss(0.0, cfg.value_drift))


def run(cfg: Config) -> Counter:
    rng = random.Random(cfg.seed)
    fields = build_dag(cfg, rng)
    sources = [f for f in fields.values() if f.level == 0]
    derived = [f for f in fields.values() if f.level > 0]
    topo = sorted(fields.values(), key=lambda f: f.level)

    pending: dict[str, list[Write]] = {}   # field_id -> concurrent writes
    pending_since: dict[str, int] = {}
    arms: Counter = Counter()

    for r in range(cfg.rounds):
        # (a) source churn — single accepted write, bumps rev (not a conflict)
        for f in sources:
            if rng.random() < cfg.source_write_prob:
                f.rev += 1
                perturb(f, cfg, rng)

        # (b) issue contention on derived fields not already contended
        for f in derived:
            if f.id in pending or f.forked:
                continue
            if rng.random() < cfg.contention_prob:
                w = rng.randint(*cfg.contention_width)
                writes = []
                for _ in range(w):
                    read_set = {d: fields[d].rev for d in f.deps}
                    read_vals = {d: fields[d].value for d in f.deps}
                    writes.append(Write(
                        field_id=f.id,
                        agent_id=rng.randrange(cfg.n_agents),
                        authority_tier=draw_tier(cfg, rng),
                        confidence=draw_conf(cfg, rng),
                        read_set=read_set,
                        read_vals=read_vals,
                        issued_round=r,
                    ))
                pending[f.id] = writes
                pending_since[f.id] = r

        # (c) resolve matured groups in topological order (upstream first, so an
        #     upstream commit this round can invalidate a downstream group)
        for f in topo:
            grp = pending.get(f.id)
            if grp is None:
                continue
            if r - pending_since[f.id] < cfg.resolution_lag:
                continue
            arm, winner = resolve(grp, fields, cfg)
            arms[arm] += 1
            if arm == FRESH_WINNER:
                f.rev += 1                      # commit winner, bump rev (cascades)
                perturb(f, cfg, rng)            # ...and move value (may be immaterial)
            elif arm == FORK:
                f.rev += 1
                perturb(f, cfg, rng)
                f.forked = True                 # deferred; freeze for this run
            # RECOMPUTE: nothing committed (losers would re-run)
            del pending[f.id]
            del pending_since[f.id]

    return arms


def summarize(arms: Counter) -> dict[str, float]:
    total = sum(arms.values()) or 1
    return {k: 100.0 * arms.get(k, 0) / total for k in (FRESH_WINNER, FORK, RECOMPUTE)}


def pct_line(label: str, arms: Counter) -> str:
    s = summarize(arms)
    total = sum(arms.values())
    return (f"{label:<28} "
            f"winner {s[FRESH_WINNER]:5.1f}%   "
            f"fork {s[FORK]:5.1f}%   "
            f"recompute {s[RECOMPUTE]:5.1f}%   "
            f"(n={total})")


# ----------------------------- experiments -----------------------------------

def main() -> None:
    print("=" * 92)
    print("CASCADE GO/NO-GO  —  resolution-arm distribution")
    print("winner = automatic win (beats OCC+CodeCRDT) | fork = lossless defer | "
          "recompute = no better than OCC")
    print("=" * 92)

    base = Config()
    print("\n[1] BASELINE  (homogeneous Tier-2 swarm, coarse confidence, lag=1)")
    print("    " + pct_line("baseline", run(base)))

    print("\n[2] CHURN / TIME-PRESSURE SWEEP  (resolution_lag = rounds between read and resolve)")
    print("    higher lag = more upstream churn lands in the read->resolve window")
    for lag in (0, 1, 2, 4, 8):
        cfg = Config(resolution_lag=lag)
        print("    " + pct_line(f"lag={lag}", run(cfg)))

    print("\n[3] SOURCE-CHURN SWEEP  (P a source field changes each round; lag=2)")
    for p in (0.05, 0.15, 0.30, 0.50, 0.75):
        cfg = Config(source_write_prob=p, resolution_lag=2)
        print("    " + pct_line(f"source_write_prob={p:.2f}", run(cfg)))

    print("\n[4] CONFIDENCE GRANULARITY  (does the FORK arm survive real-valued confidence?)")
    print("    " + pct_line("coarse 5-level", run(Config())))
    print("    " + pct_line("continuous float", run(Config(continuous_confidence=True)))
          )

    print("\n[5] AUTHORITY DIVERSITY  (homogeneous swarm vs. mixed tiers w/ occasional human)")
    print("    " + pct_line("homogeneous (all Tier2)", run(Config(tier_probs=(0.0, 0.0, 1.0)))))
    print("    " + pct_line("some analysts (T1/T2)", run(Config(tier_probs=(0.0, 0.35, 0.65)))))
    print("    " + pct_line("rare human (T0/T1/T2)", run(Config(tier_probs=(0.1, 0.3, 0.6)))))

    print("\n[6] DEPENDENCY WIDTH  (read-set size; more deps = more staleness surface, lag=2)")
    for d in (1, 2, 4, 6):
        cfg = Config(deps_per_field=d, resolution_lag=2)
        print("    " + pct_line(f"deps_per_field={d}", run(cfg)))

    print("\n[7] SEMANTIC STALENESS  (does 'value moved' beat 'rev moved'? the thesis test)")
    print("    rev-staleness flags ANY upstream change; semantic clears immaterial ones.")
    print("    -- materiality sweep (lag=4, where rev-staleness had 94% recompute) --")
    print("    " + pct_line("rev-staleness (baseline)",
                            run(Config(resolution_lag=4))))
    for m in (0.05, 0.10, 0.20, 0.40):
        cfg = Config(resolution_lag=4, semantic_staleness=True, materiality=m)
        print("    " + pct_line(f"semantic  materiality={m:.2f}", run(cfg)))
    print("    -- same lag sweep, semantic mode (compare to [2]) --")
    for lag in (0, 1, 2, 4, 8):
        cfg = Config(resolution_lag=lag, semantic_staleness=True, materiality=0.20)
        print("    " + pct_line(f"semantic lag={lag}", run(cfg)))

    print("\n" + "=" * 92)
    print("Read the FRESH_WINNER column against your expected deployment churn.")
    print("If winner stays high where your real churn lives  -> GO.")
    print("If recompute swamps it there                      -> the DAG machinery")
    print("buys little over plain OCC; ship OCC instead.")
    print("=" * 92)


if __name__ == "__main__":
    main()
