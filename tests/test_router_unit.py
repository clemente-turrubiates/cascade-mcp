#!/usr/bin/env python3
"""
test_router_unit.py — unit tests for the cascade router's decision logic,
integrity primitives, and calibration. Run:
    python -m tests.test_router_unit
    pytest tests/test_router_unit.py -v   (if pytest installed)
"""
from __future__ import annotations
import random
import sys
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
from cascade import cascade_routing as cr


def test_resolve_occ_commits_when_fresh():
    f = cr.Field("L1_0", 1, ["L0_0"], true_tol=0.01, policy="occ")
    F = {"L0_0": cr.Field("L0_0", 0, [], rev=1, value=1.0), f.id: f}
    grp = [cr.Write(2, 0.9, {"L0_0": 1}, {"L0_0": 1.0})]
    r = cr.resolve(f, grp, F, cr.Config(), random.Random(0))
    assert r.committed and r.arm == "OCC_COMMIT" and r.silent == 0 and r.fork_reason is None


def test_resolve_occ_aborts_when_all_stale():
    f = cr.Field("L1_0", 1, ["L0_0"], true_tol=0.01, policy="occ")
    F = {"L0_0": cr.Field("L0_0", 0, [], rev=2, value=1.0), f.id: f}
    grp = [cr.Write(2, 0.9, {"L0_0": 1}, {"L0_0": 1.0})]
    r = cr.resolve(f, grp, F, cr.Config(), random.Random(0))
    assert not r.committed and r.arm == "OCC_ALLABORT" and r.fork_reason is None


def test_cascade_winner_picks_highest_confidence():
    f = cr.Field("L1_0", 1, ["L0_0"], true_tol=0.25, policy="cascade", materiality=0.20)
    F = {"L0_0": cr.Field("L0_0", 0, [], rev=0, value=1.0), f.id: f}
    grp = [cr.Write(2, 0.70, {"L0_0": 0}, {"L0_0": 1.0}),
           cr.Write(2, 0.95, {"L0_0": 0}, {"L0_0": 1.0})]
    r = cr.resolve(f, grp, F, cr.Config(), random.Random(0))
    assert r.arm == "WINNER" and r.silent == 0 and r.fork_reason is None


def test_cascade_forks_on_conf_tie():
    f = cr.Field("L1_0", 1, ["L0_0"], true_tol=0.25, policy="cascade", materiality=0.20)
    F = {"L0_0": cr.Field("L0_0", 0, [], rev=0, value=1.0), f.id: f}
    grp = [cr.Write(2, 0.95, {"L0_0": 0}, {"L0_0": 1.0}),
           cr.Write(2, 0.95, {"L0_0": 0}, {"L0_0": 1.0})]
    r = cr.resolve(f, grp, F, cr.Config(), random.Random(0))
    assert r.arm == "FORK" and r.fork_reason == cr.FORK_CONF_TIE


def test_hybrid_routes_zero_tol_to_occ():
    cfg = cr.Config(policy="hybrid", route_threshold=0.05, tol_safety=1.0)
    rng = random.Random(0)
    F = cr.build(cfg, rng)
    zero_tol_fields = [f for f in F.values() if f.level > 0 and f.true_tol == cfg.zero_tol]
    assert zero_tol_fields
    for f in zero_tol_fields:
        assert f.policy == "occ", f"expected zero-tol field {f.id} to route OCC"


def test_hybrid_routes_nonzero_tol_to_cascade():
    cfg = cr.Config(policy="hybrid", route_threshold=0.05, tol_safety=1.0)
    rng = random.Random(0)
    F = cr.build(cfg, rng)
    nonzero = [f for f in F.values() if f.level > 0 and f.true_tol != cfg.zero_tol]
    assert nonzero
    for f in nonzero:
        assert f.policy == "cascade", f"expected non-zero-tol field {f.id} to route CASCADE"


def test_confidence_calibration_prefers_accurate_agent():
    f = cr.Field("L1_0", 1, ["L0_0"], true_tol=0.25, policy="cascade", materiality=0.20)
    F = {"L0_0": cr.Field("L0_0", 0, [], rev=0, value=1.0), f.id: f}
    good = cr.Write(2, 0.70, {"L0_0": 0}, {"L0_0": 1.0}, agent_id="good")
    bad = cr.Write(2, 0.95, {"L0_0": 0}, {"L0_0": 1.0}, agent_id="bad")
    track = {"good": (10, 10), "bad": (0, 10)}  # good perfect, bad always wrong
    r = cr.resolve(f, [good, bad], F, cr.Config(), random.Random(0), track=track)
    assert r.arm == "WINNER", "calibrated confidence should let the accurate agent win"


def test_calibration_flips_winner_with_divergent_records():
    """Experiment [14] core claim: empty track -> FORK, divergent -> WINNER."""
    f = cr.Field("L1_0", 1, ["L0_0"], true_tol=0.25, policy="cascade", materiality=0.20)
    F = {"L0_0": cr.Field("L0_0", 0, [], rev=0, value=1.0), f.id: f}
    good = cr.Write(2, 0.95, {"L0_0": 0}, {"L0_0": 1.0}, agent_id="good")
    bad = cr.Write(2, 0.95, {"L0_0": 0}, {"L0_0": 1.0}, agent_id="bad")
    r0 = cr.resolve(f, [good, bad], F, cr.Config(), random.Random(0), track={})
    assert r0.arm == "FORK" and r0.fork_reason == cr.FORK_CONF_TIE
    r1 = cr.resolve(f, [good, bad], F, cr.Config(), random.Random(0),
                    track={"good": (10, 10), "bad": (0, 10)})
    assert r1.arm == "WINNER" and r1.fork_reason is None


def test_hmac_present_but_mismatched_rejects_write():
    f = cr.Field("L1_0", 1, ["L0_0"], true_tol=0.25, policy="cascade", materiality=0.20)
    F = {"L0_0": cr.Field("L0_0", 0, [], rev=0, value=1.0), f.id: f}
    metrics = cr.Metrics()
    w = cr.Write(2, 0.99, {"L0_0": 0}, {"L0_0": 1.0}, agent_id="x", read_hmac="deadbeef")
    r = cr.resolve(f, [w], F, cr.Config(), random.Random(0), metrics=metrics)
    assert r.arm == "RECOMPUTE", "present-and-wrong HMAC must reject the write"
    assert metrics.events.get("hmac_failure", 0) == 1


def test_missing_hmac_stays_pass_through():
    f = cr.Field("L1_0", 1, ["L0_0"], true_tol=0.25, policy="cascade", materiality=0.20)
    F = {"L0_0": cr.Field("L0_0", 0, [], rev=0, value=1.0), f.id: f}
    w = cr.Write(2, 0.99, {"L0_0": 0}, {"L0_0": 1.0}, agent_id="x")
    r = cr.resolve(f, [w], F, cr.Config(), random.Random(0))
    assert r.arm == "WINNER"


def test_hmac_with_secret_rejects_wrong_digest():
    f = cr.Field("L1_0", 1, ["L0_0"], true_tol=0.25, policy="cascade", materiality=0.20)
    F = {"L0_0": cr.Field("L0_0", 0, [], rev=0, value=1.0), f.id: f}
    w = cr.Write(2, 0.99, {"L0_0": 0}, {"L0_0": 1.0}, agent_id="x",
                 read_hmac="0" * 64)
    r = cr.resolve(f, [w], F, cr.Config(hmac_secret="real-secret"),
                   random.Random(0))
    assert r.arm == "RECOMPUTE", "HMAC computed under a different secret must reject"


def test_audit_canary_flags_rev_stale_winner():
    """The audit canary uses rev_stale (no true_tol oracle needed)."""
    f = cr.Field("L1_0", 1, ["L0_0"], true_tol=0.25, policy="cascade", materiality=0.60)
    # winner is rev-stale (read_rev says 0 but field rev is 1) yet within materiality
    F = {"L0_0": cr.Field("L0_0", 0, [], rev=1, value=1.5), f.id: f}
    grp = [cr.Write(2, 0.99, {"L0_0": 0}, {"L0_0": 1.0})]
    winner = grp[0]
    assert cr.audit_disagreement(f, grp, F, winner) == 1


def test_audit_canary_passes_when_winner_rev_fresh():
    f = cr.Field("L1_0", 1, ["L0_0"], true_tol=0.25, policy="cascade", materiality=0.20)
    F = {"L0_0": cr.Field("L0_0", 0, [], rev=0, value=1.0), f.id: f}
    grp = [cr.Write(2, 0.99, {"L0_0": 0}, {"L0_0": 1.0})]
    assert cr.audit_disagreement(f, grp, F, grp[0]) == 0


# ---- harness ----

def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS {t.__name__}")
        except AssertionError as e:
            print(f"  FAIL {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()