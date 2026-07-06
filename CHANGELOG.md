# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-07-06

### Added
- **HMAC read-set integrity**: `Write.read_hmac` + `Config.hmac_secret` +
  `_read_set_digest`/`_verify_hmac`. A present-but-wrong HMAC rejects the write
  (RECOMPUTE) and emits an `hmac_failure` metric. Missing HMAC stays
  pass-through (backward compat). Closes the decorative-verification hole
  where `_verify_hmac` returned `False` and nobody acted on it. `hmac_secret`
  is a new `configure` knob.
- **Confidence calibration**: `_effective_conf` blends caller-asserted
  confidence with an empirical track record (Bayesian-ish shrinkage toward
  observed accuracy). `_update_track` records correctness after commits.
  Same trust-boundary class as the tolerance fix: caller-asserted confidence
  is advisory, the track record is evidence. Experiment `[14]` shows it
  flipping FORK→WINNER with divergent track records.
- **fork_reason**: FORK arms now carry `FORK_CONF_TIE` so the downstream
  arbiter knows why (was a bare `FORK` with no reason). Surfaced in
  `propose_update` results.
- **Metrics class**: emits counters — `round`, `source_churn`,
  `conflict_resolved`, `arm:*`, `commit`, `silent_error`, `hmac_failure`,
  `audit_disagreement`. Wired into `run`.
- **ResolveResult namedtuple**: `resolve()` returns named fields
  (`committed`, `redo`, `silent`, `arm`, `audit_disagreement`,
  `fork_reason`) instead of a positional 6-tuple.
- **tests/test_router_unit.py**: 13 unit tests covering OCC/cascade/fork
  routing, hybrid routing, calibration flip, HMAC enforcement (reject
  mismatched, pass-through missing), and the audit canary (rev_stale based,
  no `true_tol` oracle).

### Changed
- `propose_update` result now includes `fork_reason` and `hmac_failures`.
- `configure` inputSchema + `call_tool` allowlist now include `hmac_secret`.
- `Write` dataclass has `agent_id` and `read_hmac` fields.

## [0.2.1] - 2026-07-06

### Changed
- README: document the integrity model (thesis table for the three corruption
  paths), the trust boundary, the audit canary, and the integrity knobs table
  so PyPI's project page reflects the 0.2.0 behavior change.

## [0.2.0] - 2026-07-06

### Changed
- **Trust boundary closed (breaking-ish):** `propose_update`'s `tolerance`
  argument is now advisory and ignored by default. The field's `true_tol` is
  configure-authoritative and immutable at write time. Previously a
  writer-supplied tolerance unconditionally redefined `true_tol` (and in hybrid
  mode re-routed the field), which also redefined the number `silent_error` is
  measured against — the write self-certified. Set `trust_writer_tolerance=True`
  at configure to turn the hole back ON as a switchable regime for experiment
  `[13]` rather than relying on the unconditional bug.
- `call_tool` now rejects unknown arguments via a per-tool allowlist instead of
  forwarding `**arguments` blind. A schema-ignoring client can no longer reach
  half-wired `configure_impl` params.
- `propose_update` result now surfaces `predicate_passed` (rev vs value),
  `configured_materiality`, `configured_true_tol`, `audit_check`, and
  `audit_disagreement`, so a caller can't be blind to which predicate cleared.

### Added
- `trust_writer_tolerance` configure knob (default `False`): gates the
  self-certifying-writer hole as a measurable regime instead of a silent bug.
- `audit_canary_prob` configure knob: on a sampled fraction of
  cascade/occ_value commits, also runs the OCC rev-check and records
  disagreements. Observable leak rate WITHOUT `true_tol` ground truth — the
  instrument that makes the router trustable in the wild when routing is wrong.
- `tol_est_noise` now exposed in the `configure` inputSchema (was reachable
  only by schema-ignoring clients; now symmetric with the impl signature).
- `cr.audit_disagreement` helper and `audit_disagreements` counter in `cr.run`.
- Experiment `[13]` in `cascade_routing.main`: reproduces the self-cert hole
  (silent_err collapses to 0 under `writer_tol_inflation>1`) and `[13b]` shows
  the canary detecting the leak (audit 19014/19653 disagree at infl=2).
- Thesis notes atop both `cascade_routing.py` and `server.py` framing the
  integrity model: cascade is the cheap arm, not a correctness mechanism;
  correctness lives in the router; router correctness reduces to
  tolerance-estimate integrity; three independent corruption paths, each
  quantified.

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

[Unreleased]: https://github.com/clemente-turrubiates/cascade-mcp/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/clemente-turrubiates/cascade-mcp/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/clemente-turrubiates/cascade-mcp/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/clemente-turrubiates/cascade-mcp/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/clemente-turrubiates/cascade-mcp/releases/tag/v0.1.0
