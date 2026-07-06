#!/usr/bin/env python3
"""
server.py — MCP server that wraps cascade_routing.py as a concurrency
controller for AI agents. Two main tools:

  read_state(fields, agent_id)
      Returns current value+rev of the requested fields and secretly logs
      them as that agent's read-set (the rev/value snapshot the agent saw).

  propose_update(field, proposed_value, confidence, authority_tier,
                 tolerance, agent_id, expected_writers)
      Records the agent's proposed write against their logged read-set. When
      the batch for `field` reaches `expected_writers`, runs the hybrid
      resolve_field router (cr.resolve) on the whole batch, commits/bumps on
      a winning arm, and returns the outcome. Earlier calls in the batch
      return {"status": "pending"}; the final call returns the full result.

A configure tool builds the dependency DAG with per-field tolerance/policy
(mirrors cr.build). The resolver is called directly on cr.resolve/cr.bump so
outcomes are identical to in-process simulation.

The tool business logic lives in async *_impl functions so it can be unit-
tested in-process without the stdio transport; the @mcp decorators are thin
adapters that JSON-(de)serialize and call the impls.

Run as a stdio MCP server:  python -m cascade.server
"""
from __future__ import annotations
import json
import math
import os
import random
import sys
from dataclasses import asdict, replace
from typing import Any

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from cascade import cascade_routing as cr


# ----------------------------- shared state ----------------------------------
# Module-global so the stdio server and in-process tests share one engine.

class CascadeState:
    """Holds the field store, per-agent read-set log, and pending batches."""

    def __init__(self) -> None:
        self.fields: dict[str, cr.Field] = {}
        # (agent_id, field_id_being_written) -> {dep_id: rev}
        self.read_rev: dict[tuple[str, str], dict[str, int]] = {}
        # (agent_id, field_id_being_written) -> {dep_id: value}
        self.read_val: dict[tuple[str, str], dict[str, float]] = {}
        self.pending: dict[str, list[cr.Write]] = {}
        # regime-level config (set by configure)
        self.tol_safety: float = 1.0
        self.route_threshold: float = 0.05
        self.value_drift: float = 0.08
        self.fresh_loser_redo_prob: float = 0.0
        self.n_levels = 3
        self.fields_per_level = 6
        self.deps_per_field = 3
        self.frac_zero_tol = 0.30
        self.zero_tol = 0.01
        self.gen_tol = 0.25
        # policy_mode set by configure; per-write re-routing only happens
        # in "hybrid" mode (mirrors cr.build). "occ" and "cascade" fix the
        # field's policy at configure time and propose_update must NOT clobber it.
        self.policy_mode: str = "hybrid"
        self.global_materiality: float = 0.20
        # multiplicative log-normal noise on tolerance ESTIMATE (mirrors
        # cr.Config.tol_est_noise). >0 -> hybrid over-estimates on ~half its
        # fields even at tol_safety=1 -> silent errors (perfect-knowledge
        # artifact test).
        self.tol_est_noise: float = 0.0
        self.rng: random.Random = random.Random(0)

    def reset(self) -> None:
        self.fields.clear()
        self.read_rev.clear()
        self.read_val.clear()
        self.pending.clear()


state = CascadeState()


# ----------------------------- tool implementations --------------------------
# Pure async functions: take parsed python args, return python dicts.
# The MCP decorators below adapt these to JSON wire types.

async def configure_impl(
    n_levels: int, fields_per_level: int, deps_per_field: int,
    frac_zero_tol: float, zero_tol: float, gen_tol: float,
    tol_safety: float, route_threshold: float, value_drift: float,
    fresh_loser_redo_prob: float, seed: int,
    policy_mode: str = "hybrid", global_materiality: float = 0.20,
    tol_est_noise: float = 0.0,
) -> dict:
    """Build the DAG exactly like cr.build, then store regime config."""
    state.rng = random.Random(seed)
    state.n_levels = n_levels
    state.fields_per_level = fields_per_level
    state.deps_per_field = deps_per_field
    state.frac_zero_tol = frac_zero_tol
    state.zero_tol = zero_tol
    state.gen_tol = gen_tol
    state.tol_safety = tol_safety
    state.route_threshold = route_threshold
    state.value_drift = value_drift
    state.fresh_loser_redo_prob = fresh_loser_redo_prob
    state.policy_mode = policy_mode
    state.global_materiality = global_materiality
    state.tol_est_noise = tol_est_noise
    state.reset()
    # build DAG (mirrors cr.build)
    by: list[list[str]] = []
    for lvl in range(n_levels):
        ids = [f"L{lvl}_{i}" for i in range(fields_per_level)]
        by.append(ids)
        for fid in ids:
            if lvl == 0:
                deps: list[str] = []
            else:
                pool = [x for p in by[:lvl] for x in p]
                deps = state.rng.sample(pool, min(deps_per_field, len(pool)))
            f = cr.Field(id=fid, level=lvl, deps=deps)
            if lvl > 0:
                f.true_tol = zero_tol if state.rng.random() < frac_zero_tol else gen_tol
                measured = f.true_tol * tol_safety
                if tol_est_noise > 0.0:
                    measured *= math.exp(state.rng.gauss(0.0, tol_est_noise))
                if policy_mode == "occ":
                    f.policy = "occ"
                elif policy_mode == "occ_value":
                    f.policy = "occ_value"
                    f.materiality = global_materiality
                elif policy_mode == "cascade":
                    f.policy = "cascade"
                    f.materiality = global_materiality
                else:  # hybrid
                    if measured < route_threshold:
                        f.policy = "occ"
                    else:
                        f.policy = "cascade"
                        f.materiality = measured
            state.fields[fid] = f
    return {"status": "configured", "fields": list(state.fields.keys())}


async def read_state_impl(fields: list[str], agent_id: str,
                           write_field: str = "") -> dict:
    """Return current value+rev of requested fields; secretly log them as the
    caller's read-set keyed by (agent_id, write_field). write_field is the
    field the agent intends to propose later (its deps are what's read)."""
    snapshot = {}
    log_rev: dict[str, int] = {}
    log_val: dict[str, float] = {}
    for fid in fields:
        f = state.fields[fid]
        snapshot[fid] = {"rev": f.rev, "value": f.value}
        log_rev[fid] = f.rev
        log_val[fid] = f.value
    if write_field:
        state.read_rev[(agent_id, write_field)] = log_rev
        state.read_val[(agent_id, write_field)] = log_val
    return {"fields": snapshot}


async def churn_impl(field: str) -> dict:
    """Source-field churn: bump rev+value (mirrors the sim's source churn)."""
    f = state.fields[field]
    f.rev += 1
    f.value *= (1.0 + state.rng.gauss(0.0, state.value_drift))
    return {"field": field, "rev": f.rev, "value": f.value}


async def propose_update_impl(
    field: str, proposed_value: Any, confidence: float,
    authority_tier: int, tolerance: float, agent_id: str,
    expected_writers: int,
) -> dict:
    """Record a proposed write. When the batch reaches expected_writers, run
    cr.resolve on the whole batch and return the outcome. Uses `tolerance` as
    the field's true_tol for this write and re-derives policy/materiality from
    it (mirrors cr.build's hybrid branch), so callers passing the field's
    constant true_tol reproduce the sim exactly."""
    f = state.fields[field]
    # look up the agent's logged read-set for this field
    read_rev = state.read_rev.get((agent_id, field), {})
    read_val = state.read_val.get((agent_id, field), {})
    # if no logged read-set, snapshot current (agent didn't call read_state)
    if not read_rev:
        read_rev = {d: state.fields[d].rev for d in f.deps}
        read_val = {d: state.fields[d].value for d in f.deps}
    w = cr.Write(tier=authority_tier, conf=confidence,
                 read_rev=dict(read_rev), read_val=dict(read_val))
    state.pending.setdefault(field, []).append(w)
    batch = state.pending[field]
    if len(batch) < expected_writers:
        return {"status": "pending", "field": field,
                "received": len(batch), "expected": expected_writers}
    # batch complete -> resolve
    # Set the field's true_tol from the caller-declared tolerance. Re-derive
    # routing ONLY in hybrid mode AND only if the caller's tolerance differs
    # from what configure_impl already set (mirrors cr.build's hybrid branch).
    # In occ/cascade modes the policy/materiality were fixed at configure time
    # and must NOT be clobbered per-write. Re-deriving when tolerance is
    # unchanged would also wipe any tol_est_noise applied at build time.
    if abs(f.true_tol - tolerance) > 1e-12:
        f.true_tol = tolerance
        if state.policy_mode == "hybrid":
            measured = tolerance * state.tol_safety
            if state.tol_est_noise > 0.0:
                measured *= math.exp(state.rng.gauss(0.0, state.tol_est_noise))
            if measured < state.route_threshold:
                f.policy = "occ"
            else:
                f.policy = "cascade"
                f.materiality = measured
    cfg = cr.Config(value_drift=state.value_drift,
                    fresh_loser_redo_prob=state.fresh_loser_redo_prob,
                    tol_safety=state.tol_safety,
                    tol_est_noise=state.tol_est_noise,
                    route_threshold=state.route_threshold)
    committed, redo, silent, arm = cr.resolve(f, batch, state.fields, cfg, state.rng)
    # staleness accounting at resolve time (for the CSV). occ uses rev-stale;
    # occ_value, cascade, and hybrid all use the value-predicate drift.
    if f.policy == "occ":
        n_stale = sum(1 for ww in batch if cr.rev_stale(ww, state.fields))
    else:
        n_stale = sum(1 for ww in batch if cr.drift(ww, state.fields) > f.materiality)
    # winner tier/conf
    if arm in ("WINNER", "FORK", "OCC_COMMIT"):
        fresh = [ww for ww in batch
                 if (not cr.rev_stale(ww, state.fields) if f.policy == "occ"
                      else cr.drift(ww, state.fields) <= f.materiality)]
        bt = min(ww.tier for ww in fresh)
        top = [ww for ww in fresh if ww.tier == bt]
        bc = max(ww.conf for ww in top)
        top = [ww for ww in top if ww.conf == bc]
        win_tier = top[0].tier
        top_conf = top[0].conf
    else:
        win_tier = -1
        top_conf = -1.0
    if committed:
        cr.bump(f, state.rng, cfg)
    del state.pending[field]
    return {
        "status": "resolved", "field": field, "arm": arm,
        "recomputes": redo, "silent_error": silent,
        "committed": committed, "n_writers": len(batch), "n_stale": n_stale,
        "win_tier": win_tier, "top_confidence": top_conf,
    }


async def get_field_impl(field: str) -> dict:
    """Inspect a field's current state (for tests/debugging)."""
    f = state.fields[field]
    return {"id": f.id, "level": f.level, "deps": f.deps, "rev": f.rev,
            "value": f.value, "true_tol": f.true_tol,
            "policy": f.policy, "materiality": f.materiality}


# ----------------------------- MCP server wiring -----------------------------

server = Server("cascade-routing-controller")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="configure",
            description="Build the dependency DAG and set regime parameters.",
            inputSchema={
                "type": "object",
                "properties": {
                    "n_levels": {"type": "integer"},
                    "fields_per_level": {"type": "integer"},
                    "deps_per_field": {"type": "integer"},
                    "frac_zero_tol": {"type": "number"},
                    "zero_tol": {"type": "number"},
                    "gen_tol": {"type": "number"},
                    "tol_safety": {"type": "number"},
                    "route_threshold": {"type": "number"},
                    "value_drift": {"type": "number"},
                    "fresh_loser_redo_prob": {"type": "number"},
                    "seed": {"type": "integer"},
                    "policy_mode": {"type": "string"},
                    "global_materiality": {"type": "number"},
                },
                "required": ["n_levels", "fields_per_level", "deps_per_field",
                             "frac_zero_tol", "zero_tol", "gen_tol",
                             "tol_safety", "route_threshold", "value_drift",
                             "fresh_loser_redo_prob", "seed"],
            },
        ),
        types.Tool(
            name="read_state",
            description="Read current value+rev of fields and log them as the "
                        "caller's read-set (the premise snapshot for a later write).",
            inputSchema={
                "type": "object",
                "properties": {
                    "fields": {"type": "array", "items": {"type": "string"}},
                    "agent_id": {"type": "string"},
                    "write_field": {"type": "string"},
                },
                "required": ["fields", "agent_id", "write_field"],
            },
        ),
        types.Tool(
            name="propose_update",
            description="Propose a write. When the batch for `field` reaches "
                        "`expected_writers`, runs the hybrid resolve_field router.",
            inputSchema={
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "proposed_value": {},
                    "confidence": {"type": "number"},
                    "authority_tier": {"type": "integer"},
                    "tolerance": {"type": "number"},
                    "agent_id": {"type": "string"},
                    "expected_writers": {"type": "integer"},
                },
                "required": ["field", "proposed_value", "confidence",
                             "authority_tier", "tolerance", "agent_id",
                             "expected_writers"],
            },
        ),
        types.Tool(
            name="churn",
            description="Bump a source field's rev+value (simulates upstream churn).",
            inputSchema={
                "type": "object",
                "properties": {"field": {"type": "string"}},
                "required": ["field"],
            },
        ),
        types.Tool(
            name="get_field",
            description="Inspect a field's current state.",
            inputSchema={
                "type": "object",
                "properties": {"field": {"type": "string"}},
                "required": ["field"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name == "configure":
        r = await configure_impl(**arguments)
    elif name == "read_state":
        r = await read_state_impl(**arguments)
    elif name == "propose_update":
        r = await propose_update_impl(**arguments)
    elif name == "churn":
        r = await churn_impl(**arguments)
    elif name == "get_field":
        r = await get_field_impl(**arguments)
    else:
        raise ValueError(f"unknown tool: {name}")
    return [types.TextContent(type="text", text=json.dumps(r, default=str))]


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def cli() -> None:
    """Synchronous entry point for the ``cascade-mcp`` console script."""
    import asyncio
    asyncio.run(main())


if __name__ == "__main__":
    cli()