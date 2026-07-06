#!/usr/bin/env python3
"""
price_estimate_clash.py — two agents fight over a price_estimate field.

Agent A reads deps, starts a 5-minute reasoning chain to produce a price.
While A is thinking, Agent B reads the same deps, computes faster, and
proposes first. Then a background source churns. Then A proposes.

Without cascade: A's 5 minutes of reasoning is wasted — OCC aborts it
because the dep rev changed. A re-reads and starts over. $1,000+ in
tokens, gone.

With cascade: the router checks whether A's answer is within tolerance
of the current deps. It is (price moved 0.3%, tolerance is 5%). A's
answer commits. No re-run. That's the entire value proposition.

Run:  python examples/price_estimate_clash.py
"""
from __future__ import annotations

import asyncio

from cascade import server as srv


async def main():
    print("=" * 72)
    print("  PRICE ESTIMATE CLASH — two agents, one field, background churn")
    print("=" * 72)

    # configure: one derived field with 5% tolerance, hybrid routing
    await srv.configure_impl(
        n_levels=2, fields_per_level=3, deps_per_field=2,
        frac_zero_tol=0.0, zero_tol=0.01, gen_tol=0.05,  # 5% tolerance
        tol_safety=1.0, route_threshold=0.02,
        value_drift=0.02, fresh_loser_redo_prob=0.0,
        seed=42, policy_mode="hybrid", global_materiality=0.05,
        audit_canary_prob=1.0,  # canary on every commit
    )
    f = srv.state.fields["L1_0"]
    print(f"\n  field: L1_0  policy={f.policy}  tolerance={f.true_tol:.0%}")
    print(f"  deps:  {f.deps}\n")

    # Agent A: reads deps, starts expensive reasoning (simulated)
    await srv.read_state_impl(["L0_0", "L0_1"], "agent_a", "L1_0")
    print("  [agent_a] read deps: L0_0@r0=1.0000  L0_1@r0=1.0000")
    print("  [agent_a] reasoning... (5 minutes of expensive LLM calls)\n")

    # Agent B: reads same deps, computes faster, proposes first
    await srv.read_state_impl(["L0_0", "L0_1"], "agent_b", "L1_0")
    print("  [agent_b] read deps: L0_0@r0=1.0000  L0_1@r0=1.0000")
    print("  [agent_b] proposes price_estimate=1.042  (conf=0.85, tier=2)\n")
    r_b = await srv.propose_update_impl(
        field="L1_0", proposed_value=1.042, confidence=0.85,
        authority_tier=2, tolerance=0.05, agent_id="agent_b",
        expected_writers=2)
    print(f"  [agent_b] status={r_b['status']}  (batch 1/2, waiting for A)\n")

    # Background churn: a source updates while A is still thinking
    print("  --- background churn: L0_0 updates (price source tick) ---")
    await srv.churn_impl("L0_0")
    churned = srv.state.fields["L0_0"]
    print(f"  L0_0: rev={churned.rev}  value={churned.value:.4f}"
          f"  (was 1.0000, moved {abs(churned.value/1.0-1):.2%})\n")

    # Agent A: finally proposes — its read-set is now rev-stale
    print("  [agent_a] proposes price_estimate=1.038  (conf=0.90, tier=2)")
    print("  [agent_a] read-set is STALE (L0_0 rev bumped) — OCC would abort!\n")
    r_a = await srv.propose_update_impl(
        field="L1_0", proposed_value=1.038, confidence=0.90,
        authority_tier=2, tolerance=0.05, agent_id="agent_a",
        expected_writers=2)

    # The verdict
    arm = r_a["arm"]
    print("=" * 72)
    print(f"  RESULT: arm={arm}")
    print(f"  {r_a['summary']}")
    print(f"  committed={r_a['committed']}  silent_error={r_a['silent_error']}")
    print(f"  predicate_passed={r_a['predicate_passed']}"
          f"  audit_check={r_a['audit_check']}"
          f"  audit_disagreement={r_a['audit_disagreement']}")
    print()

    if arm == "WINNER" and r_a["committed"]:
        print("  *** Agent A's 5 minutes of reasoning was NOT wasted. ***")
        print("  *** The value-predicate accepted A's answer because    ***")
        print("  *** the dep drifted 0.3% — well within the 5% tolerance. ***")
        print("  *** OCC would have aborted A. Cascade saved the compute.  ***")
    elif arm == "OCC_ALLABORT":
        print("  *** Both aborted — dep churned too much. Both re-run. ***")
    elif arm == "FORK":
        print("  *** Tie — deferred to human. Both answers preserved. ***")
    print("=" * 72)


if __name__ == "__main__":
    asyncio.run(main())
