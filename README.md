# cascade-mcp

[![PyPI](https://img.shields.io/pypi/v/cascade-mcp)](https://pypi.org/project/cascade-mcp/)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://pypi.org/project/cascade-mcp/)
[![CI](https://github.com/clemente-turrubiates/cascade-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/clemente-turrubiates/cascade-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Cascade-resolution routing for concurrent multi-agent writes — a resolution
router that decides, per conflict, whether a write **wins**, **forks** to a
human, or must be **recomputed**, plus an MCP server that exposes the router as
tools and a stress-test suite that proves the behavior can't be cherry-picked.

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
