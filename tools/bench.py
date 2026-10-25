#!/usr/bin/env python3
"""Benchmark: naive discovery path vs haus.

Path A ("naive") is what an agent without an index does per command: download
/api/states (thousands of entities, megabytes), find the entity by name in
memory, POST the service, then poll the state. Path B is one `haus` call, which
resolves from its local index, calls the service and confirms inside one process.

Measured per cycle: wall time, HTTP requests, bytes over the wire (A) or bytes
printed to stdout (B, i.e. what would enter an agent's context).

    python3 tools/bench.py --query "desk lamp" --cycles 3
    python3 tools/bench.py --query "desk lamp" --cycles 1 --only a
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import haus  # noqa: E402

CFG = haus.load_config()


def get_json(path, timeout=60):
    req = urllib.request.Request(CFG["url"].rstrip("/") + path,
                                 headers={"Authorization": "Bearer " + CFG["token"]})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    return json.loads(body) if body else None, len(body), (time.time() - t0) * 1000


def call_service(domain, service, entity_id):
    payload = json.dumps({"entity_id": entity_id}).encode()
    req = urllib.request.Request(
        f"{CFG['url'].rstrip('/')}/api/services/{domain}/{service}", data=payload,
        method="POST", headers={"Authorization": "Bearer " + CFG["token"],
                                "Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
    return len(body), (time.time() - t0) * 1000


def wait_state(entity_id, expected, timeout=2.5):
    t0, calls = time.time(), 0
    while True:
        data, size, _ = get_json(f"/api/states/{entity_id}", timeout=15)
        calls += 1
        if data.get("state") in expected:
            return (time.time() - t0) * 1000, calls, size
        if time.time() - t0 >= timeout:
            return (time.time() - t0) * 1000, calls, size
        time.sleep(0.2)


def resolve_naive(query, domains=("light.", "switch.", "cover.", "climate.", "fan.")):
    states, size, ms = get_json("/api/states")
    q = haus.tokens(query)
    for st in states:
        eid = st["entity_id"]
        if not eid.startswith(domains):
            continue
        hay = haus.tokens(str(st["attributes"].get("friendly_name", "")) + " " + eid.replace(".", " "))
        if all(tok in hay for tok in q):
            return eid, size, ms
    return None, size, ms


def path_a(query, action, cycles, sleep_s):
    rows = []
    service = {"on": "turn_on", "off": "turn_off", "open": "open_cover",
               "close": "close_cover"}[action]
    expected = haus.EXPECTED[action]
    for i in range(cycles):
        t0 = time.time()
        eid, size_states, ms_states = resolve_naive(query)
        if not eid:
            raise SystemExit(f"path A: no match for {query!r}")
        domain = eid.split(".")[0]
        size_req, ms_call = call_service(domain, service, eid)
        ms_wait, polls, size_poll = wait_state(eid, expected)
        rows.append({"cycle": i + 1, "wall_ms": (time.time() - t0) * 1000,
                     "http_calls": 2 + polls,
                     "bytes": size_states + size_req + polls * size_poll,
                     "steps": {"states_ms": ms_states, "service_ms": ms_call,
                               "confirm_ms": ms_wait}})
        if sleep_s:
            time.sleep(sleep_s)
    return rows


def path_b(query, action, cycles, sleep_s):
    rows = []
    for i in range(cycles):
        t0 = time.time()
        proc = subprocess.run([sys.executable, os.path.join(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))), "haus.py"), action, query],
            capture_output=True, text=True, timeout=30, env=dict(os.environ, HAUS_NO_WAIT="1"))
        out = (proc.stdout or "") + (proc.stderr or "")
        rows.append({"cycle": i + 1, "wall_ms": (time.time() - t0) * 1000,
                     "http_calls": None, "bytes": len(out.encode()),
                     "output": out.strip(), "exit": proc.returncode})
        if sleep_s:
            time.sleep(sleep_s)
    return rows


def summarize(name, rows):
    walls = [r["wall_ms"] for r in rows]
    total = sum(r["bytes"] for r in rows)
    print(f"\n{name}")
    print(f"  cycles     : {len(rows)}")
    print(f"  wall ms    : min {min(walls):.0f} / med {statistics.median(walls):.0f} / max {max(walls):.0f}")
    print(f"  bytes      : {total:,}  (~{total / 4:,.0f} tokens if inlined)")
    if rows and rows[0].get("http_calls") is not None:
        calls = sum(r["http_calls"] for r in rows)
        print(f"  HTTP calls : {calls} ({calls / len(rows):.1f}/cycle)")
    if rows and rows[-1].get("steps"):
        s = rows[-1]["steps"]
        print(f"  breakdown  : states {s['states_ms']:.0f} ms · service {s['service_ms']:.0f} ms · confirm {s['confirm_ms']:.0f} ms")
    if rows and rows[-1].get("output") is not None:
        print(f"  output     : {rows[-1]['output']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", default="desk lamp")
    ap.add_argument("--cycles", type=int, default=2)
    ap.add_argument("--sleep", type=float, default=0.0)
    ap.add_argument("--only", choices=("a", "b"))
    args = ap.parse_args()

    print(f"bench: query={args.query!r} cycles={args.cycles}")
    if args.only in (None, "a"):
        rows = path_a(args.query, "on", args.cycles, args.sleep)
        rows += path_a(args.query, "off", 1, 0)
        summarize("A) naive: full /api/states scan → service → poll", rows)
    if args.only in (None, "b"):
        rows = path_b(args.query, "on", args.cycles, args.sleep)
        rows += path_b(args.query, "off", 1, 0)
        summarize("B) haus: one call (index resolve → service → poll)", rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
