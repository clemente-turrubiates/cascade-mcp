#!/usr/bin/env python3
"""
test_mcp_wrapper.py — route the 3.7M-row regime grid through the MCP server's
tool implementations and verify the wrapper preserves the cascade-routing
behavior. Two layers of verification:

  1. WIRE-PROTOCOL SMOKE TEST  (real stdio MCP client <-> server.py)
     Lists tools, calls configure/read_state/propose_update, checks the
     structured responses come back intact over JSON-RPC. Proves the wrapper
     is a real MCP server, not just importable functions.

  2. REGIME-GRID THROUGH THE WRAPPER  (in-process calls to the *_impl funcs)
     Reproduces the 11-regime x 12-policy x 3-seed grid from gen_agent_logs.py
     but every read/resolve goes through server.read_state_impl /
     propose_update_impl instead of touching cr directly. Emits
     agent_logs_mcp.csv with the same 26-column schema.

Then runs test_agent_logs.py's 43-check suite against the wrapper-produced
CSV. If it still passes, the API wrapper preserves every dynamic the
synthetic charts measure.

Run (from the repo root):  python -m tests.test_mcp_wrapper
"""
from __future__ import annotations

import asyncio
import csv
import json
import os
import subprocess
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
from cascade import server as srv


# ----------------------------- 1. wire-protocol smoke test -------------------
async def stdio_smoke():
    """Boot server.py as a subprocess and exercise the MCP wire protocol."""
    print("=" * 70)
    print("[1] WIRE-PROTOCOL SMOKE TEST  (real stdio MCP client <-> server.py)")
    print("=" * 70)
    # Use the mcp client SDK if available; fall back to a hand-rolled JSON-RPC
    # client so the test runs even if mcp.client isn't installed.
    try:
        from mcp.client.session import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
        have_client_sdk = True
    except ImportError:
        have_client_sdk = False

    if have_client_sdk:
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "cascade.server"], cwd=REPO_ROOT)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = [t.name for t in tools.tools]
                print(f"  list_tools -> {names}")
                assert "read_state" in names and "propose_update" in names
                # configure
                r = await session.call_tool("configure", {
                    "n_levels": 3, "fields_per_level": 6, "deps_per_field": 3,
                    "frac_zero_tol": 0.30, "zero_tol": 0.01, "gen_tol": 0.25,
                    "tol_safety": 1.0, "route_threshold": 0.05,
                    "value_drift": 0.08, "fresh_loser_redo_prob": 0.0,
                    "seed": 7, "policy_mode": "hybrid",
                    "global_materiality": 0.20,
                })
                cfg = json.loads(r.content[0].text)
                assert cfg["status"] == "configured"
                print(f"  configure -> {cfg['status']}, {len(cfg['fields'])} fields")
                # read_state
                r = await session.call_tool("read_state", {
                    "fields": ["L0_0", "L0_1"], "agent_id": "a1", "write_field": "L1_0",
                })
                rs = json.loads(r.content[0].text)
                assert "L0_0" in rs["fields"]
                print(f"  read_state -> {rs['fields']}")
                # churn a source so the read-set becomes stale
                r = await session.call_tool("churn", {"field": "L0_0"})
                ch = json.loads(r.content[0].text)
                assert ch["rev"] > rs["fields"]["L0_0"]["rev"]
                print(f"  churn L0_0 -> rev {ch['rev']} (was {rs['fields']['L0_0']['rev']})")
                # propose_update x2 to fill a batch of 2 writers
                for agent in ("a1", "a2"):
                    r = await session.call_tool("propose_update", {
                        "field": "L1_0", "proposed_value": 1.23,
                        "confidence": 0.85, "authority_tier": 2,
                        "tolerance": 0.25, "agent_id": agent,
                        "expected_writers": 2,
                    })
                    res = json.loads(r.content[0].text)
                    print(f"  propose_update({agent}) -> status={res['status']}"
                          + (f" arm={res['arm']} n_stale={res['n_stale']}"
                             if res['status'] == "resolved" else ""))
                    if res['status'] == 'resolved':
                        assert res['arm'] in {"WINNER", "FORK", "RECOMPUTE",
                                              "OCC_COMMIT", "OCC_ALLABORT"}
        print("  WIRE PROTOCOL OK (client SDK round-trip)")
        return True
    else:
        print("  mcp.client SDK not installed; falling back to direct subprocess "
              "JSON-RPC smoke test.")
        # Hand-rolled: launch server.py, send initialize/list_tools, parse.
        # This still proves the stdio transport works even without the client SDK.
        proc = subprocess.Popen(
            [sys.executable, "-m", "cascade.server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1, cwd=REPO_ROOT,
        )
        try:
            # Send initialize + initialized notification + list_tools
            init = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}\n'
            proc.stdin.write(init); proc.stdin.flush()
            line = proc.stdout.readline()
            assert '"result"' in line and '"serverInfo"' in line, f"init failed: {line}"
            proc.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n'); proc.stdin.flush()
            proc.stdin.write('{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n'); proc.stdin.flush()
            line = proc.stdout.readline()
            assert '"read_state"' in line and '"propose_update"' in line, \
                f"tools/list failed: {line[:200]}"
            print("  tools/list -> read_state, propose_update present (JSON-RPC OK)")
            return True
        finally:
            proc.terminate()
            try: proc.wait(timeout=3)
            except: proc.kill()


# ----------------------------- 2. regime grid through wrapper ----------------
REGIMES = [
    ("benign",          dict(source_write_prob=0.05, lag=1, value_drift=0.04,
                              frac_zero_tol=0.0, deps_per_field=1)),
    ("realistic",       dict(source_write_prob=0.25, lag=4, value_drift=0.08,
                              frac_zero_tol=0.30, deps_per_field=3)),
    ("high_churn",      dict(source_write_prob=0.60, lag=4, value_drift=0.08,
                              frac_zero_tol=0.30, deps_per_field=3)),
    ("extreme_lag",     dict(source_write_prob=0.25, lag=16, value_drift=0.08,
                              frac_zero_tol=0.30, deps_per_field=3)),
    ("wide_deps",       dict(source_write_prob=0.25, lag=4, value_drift=0.08,
                              frac_zero_tol=0.30, deps_per_field=6)),
    ("high_contention", dict(source_write_prob=0.25, lag=4, value_drift=0.08,
                              frac_zero_tol=0.30, width=(4, 8))),
    ("all_price_like",  dict(source_write_prob=0.25, lag=4, value_drift=0.08,
                              frac_zero_tol=1.0)),
    ("wild_drift",      dict(source_write_prob=0.25, lag=4, value_drift=0.20,
                              frac_zero_tol=0.30)),
    ("calm_but_wide",   dict(source_write_prob=0.10, lag=2, value_drift=0.04,
                              frac_zero_tol=0.30, deps_per_field=6, width=(4, 8))),
    ("storm",           dict(source_write_prob=0.50, lag=8, value_drift=0.15,
                              frac_zero_tol=0.50, deps_per_field=4, width=(3, 6))),
    ("adversarial_all", dict(source_write_prob=0.60, lag=16, value_drift=0.20,
                              frac_zero_tol=0.70, deps_per_field=4, width=(3, 6))),
]

# (policy_tag, policy_mode, tol_safety, fresh_loser_redo_prob, global_materiality,
#  tol_est_noise)
POLICIES = [
    ("occ",              "occ",      1.0, 0.0, 0.20, 0.0),
    ("occ_value",        "occ_value", 1.0, 0.0, 0.20, 0.0),
    ("cascade_mat0.20",  "cascade",  1.0, 0.0, 0.20, 0.0),
    ("hybrid_safety1",   "hybrid",   1.0, 0.0, 0.20, 0.0),
    ("hybrid_noise0.5",  "hybrid",   1.0, 0.0, 0.20, 0.5),
    ("hybrid_noise1.0",  "hybrid",   1.0, 0.0, 0.20, 1.0),
    # over-estimation is modeled via materiality (0.20*safety), NOT tol_safety:
    # cascade resolution ignores tol_safety, and the direct generator
    # (gen_agent_logs.py) keeps tol_safety=1.0 here, so match it for row-for-row
    # provenance equality.
    ("cascade_safety2", "cascade",  1.0, 0.0, 0.40, 0.0),
    ("cascade_safety5", "cascade",  1.0, 0.0, 1.00, 0.0),
    ("hybrid_safety2",   "hybrid",   2.0, 0.0, 0.20, 0.0),
    ("hybrid_safety5",   "hybrid",   5.0, 0.0, 0.20, 0.0),
    ("hybrid_redo0.5",   "hybrid",   1.0, 0.5, 0.20, 0.0),
    ("hybrid_redo1.0",   "hybrid",   1.0, 1.0, 0.20, 0.0),
]
SEEDS = (0, 7, 42)
CONF_LEVELS = (0.5, 0.7, 0.85, 0.95, 0.99)


async def run_regime_through_wrapper(regime_name, regime, policy_tag, policy_mode,
                                      tol_safety, redo_prob, global_mat,
                                      tol_est_noise, seed, writer):
    """Reproduce gen_agent_logs.run_with_logs but call the MCP wrapper's
    *_impl functions instead of cr directly."""
    # configure DAG via the wrapper
    cfg = dict(
        n_levels=3, fields_per_level=6,
        deps_per_field=regime.get("deps_per_field", 3),
        frac_zero_tol=regime["frac_zero_tol"], zero_tol=0.01, gen_tol=0.25,
        tol_safety=tol_safety, route_threshold=0.05,
        value_drift=regime["value_drift"], fresh_loser_redo_prob=redo_prob,
        seed=seed, policy_mode=policy_mode, global_materiality=global_mat,
        tol_est_noise=tol_est_noise,
    )
    await srv.configure_impl(**cfg)
    # configure_impl set srv.state.rng = random.Random(seed) and used it to
    # build the DAG. Do NOT overwrite srv.state.rng here — that would desync
    # the stream from what configure already consumed (the sim's churn/
    # contention draws must continue from the same stream).
    fields = srv.state.fields
    src = [f for f in fields.values() if f.level == 0]
    der = [f for f in fields.values() if f.level > 0]
    topo = sorted(fields.values(), key=lambda f: f.level)
    width = regime.get("width", (2, 3))
    swp = regime["source_write_prob"]
    lag = regime["lag"]
    rounds = 8000
    contention_prob = 0.30

    # per-field pending: track issued_round and the writes (mirrors sim)
    pending: dict[str, list] = {}

    for r in range(rounds):
        # (a) source churn — call the wrapper's churn tool
        for f in src:
            if srv.state.rng.random() < swp:
                await srv.churn_impl(f.id)
        # (b) issue contention: each writer calls read_state then propose_update
        for f in der:
            if f.id in pending: continue
            if srv.state.rng.random() < contention_prob:
                w = srv.state.rng.randint(*width)
                # Draw confidence NOW (at issue time) to match the direct sim's
                # rng draw order — cr.run draws conf inside the contention loop,
                # not at resolve time. Drawing later would desync the stream.
                writer_confs = [srv.state.rng.choice(CONF_LEVELS) for _ in range(w)]
                # Each writer reads the field's deps via the wrapper, then
                # proposes. We call the impls directly (in-process) — same code
                # path the stdio server's call_tool invokes, just without JSON.
                read_args_per_writer = []
                for wi in range(w):
                    agent_id = f"{f.id}_w{wi}_r{r}"
                    await srv.read_state_impl(fields=f.deps, agent_id=agent_id,
                                                write_field=f.id)
                    read_args_per_writer.append(agent_id)
                # record pending; resolve when batch matures at r+lag
                pending[f.id] = (read_args_per_writer, r, writer_confs)
        # (c) resolve matured batches in topological order
        for f in topo:
            if f.id not in pending: continue
            agents, issued, writer_confs = pending[f.id]
            if r - issued < lag: continue
            expected_writers = len(agents)
            # all writers propose with their logged read-set; the LAST call
            # triggers the resolve and returns the outcome
            outcome = None
            for ai, agent_id in enumerate(agents):
                conf = writer_confs[ai]
                # tolerance = field's true_tol (caller-declared, matches sim)
                res = await srv.propose_update_impl(
                    field=f.id, proposed_value=1.0, confidence=conf,
                    authority_tier=2, tolerance=f.true_tol,
                    agent_id=agent_id, expected_writers=expected_writers,
                )
                if res["status"] == "resolved":
                    outcome = res
            assert outcome is not None, f"no resolution for {f.id} at round {r}"
            # emit CSV row (same schema as agent_logs.csv)
            dep_vals = "|".join(
                f"{d}:{outcome and fields[d].rev}:{fields[d].value:.6g}"
                for d in f.deps) if f.deps else ""
            # read_time = issued round; resolve_time = r; dependency_values
            # uses the FIRST writer's logged read-set snapshot (matches the
            # original generator's representative-snapshot convention)
            first_agent = agents[0]
            rr = srv.state.read_rev.get((first_agent, f.id), {})
            rv = srv.state.read_val.get((first_agent, f.id), {})
            dep_vals = "|".join(f"{d}:{rr.get(d, 0)}:{rv.get(d, 1.0):.6g}"
                                for d in f.deps) if f.deps else ""
            writer.writerow({
                "seed": seed, "regime": regime_name,
                "source_write_prob": swp, "lag": lag,
                "deps_per_field": regime.get("deps_per_field", 3),
                "contention_width_lo": width[0], "contention_width_hi": width[1],
                "frac_zero_tol": regime["frac_zero_tol"],
                "value_drift": regime["value_drift"], "tol_safety": tol_safety,
                "fresh_loser_redo_prob": redo_prob, "rounds": rounds,
                "policy": f.policy, "policy_tag": policy_tag,
                "field": f.id, "level": f.level,
                "read_time": issued, "resolve_time": r,
                "n_writers": outcome["n_writers"], "n_stale": outcome["n_stale"],
                "win_tier": outcome["win_tier"],
                "top_confidence": f"{outcome['top_confidence']:.4f}",
                "dependency_values": dep_vals, "arm": outcome["arm"],
                "recomputes": outcome["recomputes"],
                "silent_error": outcome["silent_error"],
                "true_tol": f"{f.true_tol:.6g}",
            })
            del pending[f.id]


async def main_async():
    t0 = time.time()
    # 1. wire protocol smoke test
    wire_ok = await stdio_smoke()
    if not wire_ok:
        print("WIRE PROTOCOL FAILED"); sys.exit(1)

    # 2. regime grid through the wrapper -> agent_logs_mcp.csv
    print()
    print("=" * 70)
    print("[2] REGIME GRID THROUGH MCP WRAPPER  -> agent_logs_mcp.csv")
    print("=" * 70)
    out = "agent_logs_mcp.csv"
    cols = [
        "seed", "regime", "source_write_prob", "lag", "deps_per_field",
        "contention_width_lo", "contention_width_hi", "frac_zero_tol",
        "value_drift", "tol_safety", "fresh_loser_redo_prob", "rounds",
        "policy", "policy_tag", "field", "level",
        "read_time", "resolve_time", "n_writers", "n_stale",
        "win_tier", "top_confidence", "dependency_values", "arm",
        "recomputes", "silent_error", "true_tol",
    ]
    n_runs = 0
    total_runs = len(REGIMES) * len(SEEDS) * len(POLICIES)
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        for regime_name, regime in REGIMES:
            for seed in SEEDS:
                for policy_tag, mode, tsafe, redo, gmat, noise in POLICIES:
                    await run_regime_through_wrapper(
                        regime_name, regime, policy_tag, mode, tsafe, redo, gmat,
                        noise, seed, w)
                    n_runs += 1
                    print(f"  done {regime_name:<16} seed={seed} {policy_tag:<18} "
                          f"({n_runs}/{total_runs})")
    dt = time.time() - t0
    sz = os.path.getsize(out) / 1e6
    print(f"\nWROTE {out}  ({n_runs} runs, {dt:.0f}s)")
    print(f"  size: {sz:.1f} MB")

    # 3. run the 33-point suite against the wrapper CSV
    print()
    print("=" * 70)
    print("[3] 43-CHECK TEST SUITE AGAINST agent_logs_mcp.csv")
    print("=" * 70)
    # patch the CSV path in test_agent_logs by monkeypatching before import
    from tests import test_agent_logs as tal
    tal.CSV = out
    tal.main()  # raises SystemExit(1) on failure


if __name__ == "__main__":
    asyncio.run(main_async())
