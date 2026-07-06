#!/usr/bin/env python3
"""
e2e_local_swarm.py — end-to-end test of the MCP server with 3 agents.

Spawns the real server process, drives it through JSON-RPC:
  1. configure the DAG
  2. 3 agents read overlapping deps
  3. churn a dep between reads (stale one agent)
  4. 3 agents propose to the same field
  5. check the outcome (arm, summary, winner)
  6. test error cases (missing field, unknown tool, unknown arg)
  7. test a second field with different tolerance (OCC routing)
  8. test audit canary at 100%
  9. verify no crashes, no stalls, clean JSON-RPC throughout

Run:  python tests/e2e_local_swarm.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class McpClient:
    """Minimal JSON-RPC client over stdio for the cascade MCP server."""

    def __init__(self, proc):
        self._proc = proc
        self._id = 0

    def _send(self, msg: dict) -> None:
        line = json.dumps(msg) + "\n"
        self._proc.stdin.write(line)
        self._proc.stdin.flush()

    def _recv(self, timeout: float = 5.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self._proc.stdout.readline()
            if not line:
                time.sleep(0.05)
                continue
            line = line.strip()
            if not line:
                continue
            return json.loads(line)
        raise TimeoutError("no response within %.1fs" % timeout)

    def initialize(self) -> dict:
        self._send({
            "jsonrpc": "2.0", "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "e2e-test", "version": "1.0"}}})
        r = self._recv()
        assert "result" in r, f"init failed: {r}"
        # send initialized notification
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return r["result"]

    def call(self, tool: str, args: dict, timeout: float = 5.0) -> dict:
        self._id += 1
        self._send({
            "jsonrpc": "2.0", "id": self._id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args}})
        r = self._recv(timeout)
        if "error" in r:
            return {"_error": r["error"]}
        result = r.get("result", {})
        if result.get("isError"):
            text = result["content"][0]["text"]
            return {"_isError": text}
        text = result["content"][0]["text"]
        return json.loads(text)

    def close(self):
        self._proc.terminate()
        try:
            self._proc.wait(timeout=3)
        except Exception:
            self._proc.kill()


def main():
    print("=" * 70)
    print("E2E LOCAL SWARM TEST (3 agents through the real MCP server)")
    print("=" * 70)

    proc = subprocess.Popen(
        [sys.executable, "-m", "cascade.server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, bufsize=1,
        cwd=REPO_ROOT, encoding="utf-8")
    client = McpClient(proc)
    passed = 0
    failed = 0

    def check(label, condition, detail=""):
        nonlocal passed, failed
        if condition:
            passed += 1
            print(f"  PASS  {label}")
        else:
            failed += 1
            print(f"  FAIL  {label}  {detail}")

    try:
        # 0. initialize
        info = client.initialize()
        check("initialize", info["serverInfo"]["name"] == "cascade-routing-controller",
              str(info))

        # 1. configure
        r = client.call("configure", {
            "n_levels": 3, "fields_per_level": 6, "deps_per_field": 3,
            "frac_zero_tol": 0.30, "zero_tol": 0.01, "gen_tol": 0.25,
            "tol_safety": 1.0, "route_threshold": 0.05,
            "value_drift": 0.08, "fresh_loser_redo_prob": 0.0,
            "seed": 42, "policy_mode": "hybrid",
            "global_materiality": 0.20,
            "audit_canary_prob": 1.0,  # canary on every commit
        })
        check("configure", r.get("status") == "configured", str(r))
        fields = r.get("fields", [])
        check("configure has 18 fields", len(fields) == 18, f"got {len(fields)}")

        # Find a cascade-routed field (gen_tol=0.25) and its deps
        gf = client.call("get_field", {"field": "L1_0"})
        check("get_field L1_0", "policy" in gf, str(gf))
        field1 = "L1_0"
        deps1 = gf["deps"]

        # 2. 3 agents read the same deps
        for i in range(3):
            r = client.call("read_state", {
                "fields": deps1, "agent_id": f"agent_{i}",
                "write_field": field1})
            check(f"read_state agent_{i}", deps1[0] in r.get("fields", {}), str(r))

        # 3. churn one dep between reads — agent_2 reads again (stale)
        r = client.call("churn", {"field": deps1[0]})
        check(f"churn {deps1[0]}", r.get("rev", -1) >= 1, str(r))
        # agent_2 re-reads (gets fresh rev)
        r = client.call("read_state", {
            "fields": deps1, "agent_id": "agent_2",
            "write_field": field1})
        check("read_state agent_2 (post-churn)", r["fields"][deps1[0]]["rev"] >= 1,
              str(r))

        # 4. 3 agents propose to the same field (expected_writers=3)
        results = []
        for i in range(3):
            r = client.call("propose_update", {
                "field": field1, "proposed_value": 1.0 + i * 0.01,
                "confidence": 0.80 + i * 0.05,
                "authority_tier": 2,
                "tolerance": 0.25,
                "agent_id": f"agent_{i}",
                "expected_writers": 3})
            results.append(r)
        last = results[-1]
        check("propose batch resolves", last.get("status") == "resolved", str(last))
        check("arm is valid",
              last.get("arm") in {"WINNER", "FORK", "RECOMPUTE",
                                  "OCC_COMMIT", "OCC_ALLABORT"},
              str(last.get("arm")))
        check("summary present", "summary" in last, str(last))
        check("audit_check fired", last.get("audit_check") == 1, str(last))
        check("n_writers == 3", last.get("n_writers") == 3, str(last))
        print(f"       arm={last['arm']}  summary={last['summary']}")

        # 5. test a second field — 2 agents, clean reads, expect WINNER
        field2 = "L1_1"
        gf2 = client.call("get_field", {"field": field2})
        deps2 = gf2["deps"]
        for i in range(2):
            client.call("read_state", {
                "fields": deps2, "agent_id": f"b_{i}",
                "write_field": field2})
        r0 = client.call("propose_update", {
            "field": field2, "proposed_value": 1.0,
            "confidence": 0.70, "authority_tier": 2,
            "tolerance": 0.25, "agent_id": "b_0",
            "expected_writers": 2})
        r1 = client.call("propose_update", {
            "field": field2, "proposed_value": 1.0,
            "confidence": 0.95, "authority_tier": 2,
            "tolerance": 0.25, "agent_id": "b_1",
            "expected_writers": 2})
        check("second field resolves", r1.get("status") == "resolved", str(r1))
        check("second field WINNER (higher conf wins)",
              r1.get("arm") == "WINNER",
              f"arm={r1.get('arm')}")
        check("winner tier=2", r1.get("win_tier") == 2, str(r1))
        check("winner conf=0.95", abs(r1.get("top_confidence", 0) - 0.95) < 1e-6,
              str(r1))

        # 6. error: missing field
        r = client.call("get_field", {"field": "L99_99"})
        check("missing field -> error", r.get("_isError") is not None
              or "not found" in str(r.get("_isError", "")),
              str(r))

        # 7. error: unknown tool
        r = client.call("bogus_tool", {})
        check("unknown tool -> error", r.get("_isError") is not None
              or r.get("_error") is not None,
              str(r))

        # 8. error: unknown arg
        r = client.call("configure", {
            "n_levels": 3, "fields_per_level": 6, "deps_per_field": 3,
            "frac_zero_tol": 0.30, "zero_tol": 0.01, "gen_tol": 0.25,
            "tol_safety": 1.0, "route_threshold": 0.05,
            "value_drift": 0.08, "fresh_loser_redo_prob": 0.0,
            "seed": 0, "BOGUS": True})
        check("unknown arg -> error", r.get("_isError") is not None, str(r))

        # 9. reconfigure and test OCC-routed field (zero_tol)
        r = client.call("configure", {
            "n_levels": 2, "fields_per_level": 4, "deps_per_field": 2,
            "frac_zero_tol": 1.0,  # ALL fields zero-tol -> OCC
            "zero_tol": 0.01, "gen_tol": 0.25,
            "tol_safety": 1.0, "route_threshold": 0.05,
            "value_drift": 0.08, "fresh_loser_redo_prob": 0.0,
            "seed": 7, "policy_mode": "hybrid",
            "global_materiality": 0.20})
        check("reconfigure (all zero-tol)", r.get("status") == "configured",
              str(r))
        occ_field = "L1_0"
        gf3 = client.call("get_field", {"field": occ_field})
        check("zero-tol field routes to OCC",
              gf3.get("policy") == "occ", str(gf3))
        deps3 = gf3["deps"]
        for i in range(2):
            client.call("read_state", {
                "fields": deps3, "agent_id": f"c_{i}",
                "write_field": occ_field})
        r0 = client.call("propose_update", {
            "field": occ_field, "proposed_value": 1.0,
            "confidence": 0.9, "authority_tier": 2,
            "tolerance": 0.01, "agent_id": "c_0",
            "expected_writers": 2})
        r1 = client.call("propose_update", {
            "field": occ_field, "proposed_value": 1.0,
            "confidence": 0.9, "authority_tier": 2,
            "tolerance": 0.01, "agent_id": "c_1",
            "expected_writers": 2})
        check("OCC field resolves", r1.get("status") == "resolved", str(r1))
        check("OCC field arm=OCC_COMMIT",
              r1.get("arm") == "OCC_COMMIT",
              f"arm={r1.get('arm')}")
        check("OCC predicate_passed=rev",
              r1.get("predicate_passed") == "rev", str(r1))

        # 10. stale OCC — churn deps after read, then propose
        client.call("churn", {"field": deps3[0]})
        r0 = client.call("propose_update", {
            "field": occ_field, "proposed_value": 1.0,
            "confidence": 0.9, "authority_tier": 2,
            "tolerance": 0.01, "agent_id": "c_0",
            "expected_writers": 1})
        check("stale OCC -> OCC_ALLABORT",
              r0.get("arm") == "OCC_ALLABORT",
              f"arm={r0.get('arm')}")
        check("stale OCC not committed",
              r0.get("committed") is False, str(r0))

    finally:
        client.close()
        # drain stderr for diagnostics
        stderr = proc.stderr.read() if proc.stderr else ""
        if failed:
            print("\n--- STDERR (server logs) ---")
            for line in stderr.strip().split("\n"):
                if line.strip():
                    print(f"  {line}")

    print()
    print(f"{'=' * 70}")
    print(f"RESULT: {passed}/{passed + failed} passed", end="")
    if failed:
        print(f" ({failed} FAILED)")
        sys.exit(1)
    else:
        print(" — local swarm routing verified")
        sys.exit(0)


if __name__ == "__main__":
    main()
