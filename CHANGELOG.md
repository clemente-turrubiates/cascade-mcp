# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-07-06

### Added
- Cascade-resolution routing router (`cascade.cascade_routing`): pure OCC vs pure
  cascade vs per-field **hybrid** conflict resolution, classifying every conflict
  into a WINNER / FORK / RECOMPUTE arm.
- MCP stdio server (`cascade.server`) exposing five tools: `configure`,
  `read_state`, `propose_update`, `churn`, and `get_field`.
- Standalone go/no-go regime simulator (`cascade.cascade_sim`).
- Stress-test suite under `tests/` and `scripts/`: log generator, 43-check
  self-consistency suite, adversarial audit, and an MCP-wrapper equivalence test
  proving the server preserves the router's behavior end-to-end.
- Packaging for `uvx cascade-mcp` / PyPI (hatchling), MIT licensed.

[Unreleased]: https://github.com/clemente-turrubiates/cascade-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/clemente-turrubiates/cascade-mcp/releases/tag/v0.1.0
