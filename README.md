# cascade-mcp

[![PyPI](https://img.shields.io/pypi/v/cascade-mcp)](https://pypi.org/project/cascade-mcp/)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://pypi.org/project/cascade-mcp/)
[![CI](https://github.com/clemente-turrubiates/cascade-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/clemente-turrubiates/cascade-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Cascade-resolution routing for concurrent multi-agent writes — a resolution
router that decides, per conflict, whether a write **wins**, **forks** to a
human, or must be **recomputed**, plus an MCP server that exposes the router as
tools and a stress-test suite that proves the behavior can't be cherry-picked.

> **Status: research harness + MCP demo, not production.** Good for
> coordinating a local swarm of agents in a single session where losing state
> on restart is acceptable. Persistence, auth, and multi-process serialization
> come next — see [Current limitations & roadmap](#current-limitations--roadmap).

## Thesis

**cascade is the cheap arm, not a correctness mechanism.** Correctness lives
in the router. The router's correctness reduces to **tolerance-estimate
integrity**. There are three independent ways integrity fails, each reachable
as a `configure` knob and quantified in `cascade_routing`'s experiment `[13]`:

| corruption path        | knob                       | who can exploit it         |
| :--------------------- | :------------------------- | :------------------------- |
| config lies about tol  | `tol_safety`               | the operator (don't lie)   |
| write self-certifies   | `trust_writer_tolerance`   | any writer (one write)     |
| honest imperfect meas. | `tol_est_noise`            | nobody — measurement noise |

The **audit canary** (`audit_canary_prob`) is what saves you when routing is
wrong: on a sampled fraction of cascade commits, also run the OCC rev-check
and record disagreements. This gives the system an *observable* estimate of its
own leak rate **without `true_tol` ground truth** — the instrument you'd
actually need to trust this in the wild. Experiment `[13b]` shows the canary
detecting leaks that `silent_error` can't (silent_err=0 while audit
19014/19653 disagree at `writer_tol_inflation=2`).

**Trust boundary:** a writer-supplied `tolerance` in `propose_update` is
advisory and ignored by default. The field's `true_tol` is set at `configure`
and is immutable at write time. Set `trust_writer_tolerance=True` to turn the
self-certifying-writer hole back ON as a switchable regime (for measurement),
not a silent bug.

## What's here

The core question: when many agents write to the same field over a dependency
DAG, how do you resolve conflicts without either silently committing wrong
values (pure cascade) or overpaying in wasted re-runs (pure OCC)? The **hybrid**
policy routes zero-tolerance fields to OCC and tolerant fields to a
provenance-weighted cascade. Every conflict lands in one of a few arms:

- **WINNER** — a live (non-stale) write wins on authority → confidence. No
  re-run, no human. This is the win over OCC.
- **FORK** — two+ fresh writes tie; defer to a human/high-tier agent instead of
  silently dropping one.
- **RECOMPUTE** — every competing write is premise-stale; there's no correct
  value to pick, so re-run. Here you're no better than OCC.

## Layout

```
cascade/                 importable package
  cascade_routing.py     core resolution router (OCC vs cascade vs hybrid)
  server.py              MCP stdio server wrapping the router as tools
  cascade_sim.py         standalone go/no-go regime simulator
scripts/                 data-generation / audit utilities
  gen_agent_logs.py      emit agent_logs.csv across the regime × policy grid
  audit_cherrypick.py    adversarial read of agent_logs.csv
  validate_logs.py       quick sanity checks on a generated CSV
tests/                   verification suite
  test_agent_logs.py     43-check self-consistency + usability suite over the CSV
  test_mcp_wrapper.py    routes the regime grid through the MCP wrapper and
                         re-runs the suite to prove the wrapper preserves behavior
```

Large simulation outputs (`agent_logs.csv`, `agent_logs_mcp.csv`, ~900 MB each)
are regenerable and are gitignored.

## Requirements

- Python ≥ 3.10 (developed on 3.13)
- [`mcp`](https://pypi.org/project/mcp/) — installed automatically as a dependency

## Install & attach to an MCP client

Once published to PyPI, no clone or virtualenv is needed — [`uvx`](https://docs.astral.sh/uv/)
runs the server in an ephemeral environment:

```
uvx cascade-mcp
```

To attach the router to **Claude Desktop** or **Cursor**, add this to your
`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "cascade": {
      "command": "uvx",
      "args": ["cascade-mcp"]
    }
  }
}
```

The MCP server exposes five tools: `configure`, `read_state`, `propose_update`,
`churn`, `get_field`.

### Integrity knobs (`configure`)

| knob                      | default | what it does                                                |
| :------------------------ | :------ | :---------------------------------------------------------- |
| `tol_safety`              | 1.0     | systematic bias on tolerance estimate (config lying)        |
| `tol_est_noise`           | 0.0     | log-normal spread on tolerance estimate (honest measurement)|
| `trust_writer_tolerance`  | false   | let writers redefine `true_tol` at write time (the hole)    |
| `audit_canary_prob`       | 0.0     | fraction of cascade commits that also run the OCC check     |
| `hmac_secret`             | `""`    | secret for read-set HMAC; inject a real key to enforce integrity |

`propose_update` results surface `predicate_passed` (rev vs value),
`configured_materiality`, `configured_true_tol`, `audit_check`,
`audit_disagreement`, `fork_reason` (FORK_CONF_TIE if a conf tie persisted
after calibration), and `hmac_failures` — so a caller can't be blind to which
predicate cleared or whether integrity checks failed.

## Usage (from source)

Clone the repo and run everything **from the repo root**.

Run the MCP server (stdio):

```
python -m cascade.server
```

Run the standalone simulator:

```
python -m cascade.cascade_sim
```

Generate the stress-test CSV (writes UTF-8 — pipe via a POSIX shell, **not**
PowerShell `>`, which re-encodes to UTF-16 and corrupts the file):

```
python scripts/gen_agent_logs.py > agent_logs.csv
```

Verify the generated CSV:

```
python -m tests.test_agent_logs        # 43-check suite
python scripts/audit_cherrypick.py     # adversarial cross-checks
```

Verify the MCP wrapper preserves the router's behavior end-to-end (wire-protocol
smoke test → regime grid through the wrapper → re-run the suite):

```
python -m tests.test_mcp_wrapper
```

Run the 13 unit tests for the router's decision logic (OCC/cascade/fork routing,
HMAC enforcement, calibration, audit canary):

```
python -m tests.test_router_unit
```

## What it's good for

- **Coordinating a local agent swarm in one session.** You driving 2-3 agents
  through opencode/Claude Desktop on your machine. In-memory is fine — it's one
  process, one session; restart just means re-running. Hybrid routing, FORK to
  human, and the audit canary give you structured conflict outcomes instead of
  last-writer-wins.
- **Prototyping multi-agent coordination patterns.** Exploring when agents
  should fork to a human vs recompute vs pick a winner — this lets you exercise
  those arms through a real MCP interface without building the router yourself.
- **Validating the routing thesis.** The sim (`cascade_routing.main`) runs 5
  experiments quantifying each corruption path and the audit canary's detection
  rate. The 43-check wrapper suite proves the MCP server preserves the router's
  behavior. That's research output you can cite.

## Current limitations & roadmap

This is an **in-memory research harness**. The routing logic is tested and
correct; the gaps are all in the deployment layer around the router, not in
the router's decisions. Being explicit about them:

| limitation | status | fix |
| :--------- | :----- | :--- |
| **In-memory only** — restart loses all state | known | add a persistence layer (SQLite/WAL + recovery); the router is pure, so backing it with a DB instead of a dict doesn't change the decisions |
| **HMAC secret defaults to `""`** — integrity contract is enforced (present-but-wrong rejects), but no deployment has injected a real key | by design | `configure(hmac_secret=<real-key>)` — the router can't ship your secret |
| **No concurrency control on the server** — `propose_update` batch accumulation is racy under parallel writers if you scale beyond one MCP process | known | MCP stdio serializes per-server; for multi-process, move pending-batch state to a lock or single-writer queue |
| **`true_tol` is simulator-known** — in production you estimate it; noise >0 leaks silent errors | inherent | add per-field tolerance calibration from historical data; the router's contract is "given a tolerance, route" |
| **No auth / durable audit log / quotas** | known | the audit canary counts in-memory; a deployment needs structured logging + alerting |
| **`tol_safety` / `route_threshold` / `materiality` need real tuning** | defaults are research guesses | calibrate for your actual workload; the knobs are exposed so you can |

**What's not on this list** (because it's done): the routing logic, the trust
boundary (writer-asserted tolerance/confidence is advisory by default), the
audit canary (observable without ground truth), the HMAC enforcement, the
calibration mechanism, and the test suite (13 unit + 43 wrapper checks). The
router's correctness is validated; production is a deployment project, not a
router rewrite.
