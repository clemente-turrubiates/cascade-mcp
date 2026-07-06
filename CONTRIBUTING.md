# Contributing

## One-command setup

```bash
git clone https://github.com/clemente-turrubiates/cascade-mcp.git
cd cascade-mcp
pip install -e ".[dev]"
```

This installs the package in editable mode plus `pytest` and `ruff`.

## Verify everything works

```bash
python -m tests.test_router_unit      # 13 unit tests (~0s)
python -m tests.test_mcp_wrapper      # 43 wrapper checks (~5 min, generates ~900 MB CSV)
python -m cascade.cascade_routing     # run the sim experiments
```

Fast inner loop — unit tests + lint in under 2 seconds:

```bash
pytest tests/test_router_unit.py -v
ruff check .
ruff format --check .
```

## Lint + format

```bash
ruff check .        # lint (E, F, I, B, SIM, W)
ruff format .       # format
ruff format --check .  # CI check (no writes)
```

Ruff config lives in `pyproject.toml`. Line length is 100, target is py310.
The sim/test files are exempt from E501 (long lines) since they use inline
output formatting; production code (`cascade/server.py`,
`cascade/cascade_routing.py`) should stay under 100.

## Console output conventions

The sim (`cascade_routing.main`) prints experiment results in a fixed format:

- Each `rep()` line shows: `label`, `recomp/conf`, `recomp/commit`, `silent`,
  `conflicts`, and (if active) `audit N/M`.
- A second indented line shows arm breakdown: `WINNER=N  FORK=N  RECOMPUTE=N`.
- The arm legend is printed once at the top of `main()`.

The MCP server's `propose_update` result includes a `"summary"` field — a
human-readable one-liner like:

```
FORK: ties deferred to a human (fork); (FORK_CONF_TIE: confidence tie after calibration); committed
RECOMPUTE: all writes stale, re-run needed; 2/2 writes stale; not committed
WINNER: a write won on authority->confidence; committed
```

This is what a developer scanning MCP tool output reads first. The structured
fields (`arm`, `fork_reason`, `n_stale`, etc.) are for programmatic consumers.

## Cutting a release

See [RELEASING.md](RELEASING.md). Summary:

1. Bump `version` in `pyproject.toml`.
2. Add a `## [X.Y.Z]` section in `CHANGELOG.md` + update compare links.
3. `git commit -am "Release vX.Y.Z" && git push origin main`
4. `git tag -a vX.Y.Z -m "cascade-mcp vX.Y.Z" && git push origin vX.Y.Z`

The publish workflow builds, uploads to PyPI (trusted publishing, no tokens),
and creates a GitHub Release automatically.