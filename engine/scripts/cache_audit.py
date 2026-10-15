#!/usr/bin/env python
"""cache_audit.py — prompt-cache audit SDK (per turn / per run).

Answers: how many turns ran, how many got cached tokens, how many tokens.
Effective cache read = usageDetails.cache_read (iter108 fix 2026-10-12 records
BOTH fields) or fallback usageDetails.priority_cache_read (fast-tier raw).
NOTE: traces recorded BEFORE the fix on service_tier=fast are BLIND — the API
reported the counter as prompt_tokens_details.priority_cache_read and the old
tracer stored only cache_read=0. This tool cannot resurrect those.

Usage (env auto-loaded from engine/.env):
  cache_audit.py --hours 24 [--name diallux-call]        # scan recent traces
  cache_audit.py --trace <trace_id>                      # per-generation detail
  cache_audit.py --run <run_id>                          # trace_ids from the ledger
Exit codes: 0 ok. Stdlib only.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sqlite3
import statistics
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = Path("/home/julio/projects/clean_diallux_SDR/research/surgeon/iter48-rag-truth/ledger.db")


def _env() -> tuple[str, str, str]:
    env = {}
    envfile = ROOT / ".env"
    if envfile.exists():
        for line in envfile.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env.setdefault(k.strip(), v.strip())
    host = os.environ.get("LANGFUSE_HOST") or env.get("LANGFUSE_HOST") or "http://localhost:3001"
    pk = os.environ.get("LANGFUSE_PUBLIC_KEY") or env.get("LANGFUSE_PUBLIC_KEY") or ""
    sk = os.environ.get("LANGFUSE_SECRET_KEY") or env.get("LANGFUSE_SECRET_KEY") or ""
    return host.rstrip("/"), pk, sk


def _get(path: str) -> dict:
    host, pk, sk = _env()
    req = urllib.request.Request(
        f"{host}{path}",
        headers={"Authorization": "Basic " + base64.b64encode(f"{pk}:{sk}".encode()).decode()},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _iso(hours: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - hours * 3600))


def _trace_gens(trace_id: str) -> list[dict]:
    d = _get(f"/api/public/traces/{trace_id}")
    gens = [o for o in d.get("observations", []) if (o.get("type") or "").upper() == "GENERATION"]
    gens.sort(key=lambda o: o.get("startTime") or "")
    return gens


def _cached_of(g: dict) -> int:
    ud = g.get("usageDetails") or {}
    return int(ud.get("cache_read") or ud.get("priority_cache_read") or 0)


def _stats(trace_id: str) -> dict:
    gens = _trace_gens(trace_id)
    cached = [_cached_of(g) for g in gens]
    ttfts = [(g.get("usageDetails") or {}).get("ttft_ms") for g in gens]
    ttfts = [t for t in ttfts if t]
    n_cached = sum(1 for c in cached if c)
    states: dict[str, list[int]] = {}
    for g in gens:
        key = (g.get("name") or "").replace("llm:", "")
        states.setdefault(key, []).append(_cached_of(g))
    return {
        "trace_id": trace_id,
        "gens": len(gens),
        "cached_gens": n_cached,
        "cached_sum": sum(cached),
        "cached_p50": statistics.median(cached) if cached else 0,
        "ttft_p50": statistics.median(ttfts) if ttfts else None,
        "states": {k: (sum(1 for x in v if x), len(v)) for k, v in states.items()},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--name", default=None, help="trace-name filter (substring)")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--trace", default=None)
    ap.add_argument("--run", default=None, help="ledger run_id -> its trace_ids")
    args = ap.parse_args()

    ids: list[str] = []
    if args.trace:
        ids = [args.trace]
    elif args.run:
        con = sqlite3.connect(LEDGER)
        ids = [r[0] for r in con.execute("SELECT DISTINCT trace_id FROM calls WHERE run_id=?", (args.run,))]
        if not ids:
            print(f"run {args.run}: no calls rows in ledger")
            return 1
    else:
        tr = _get(f"/api/public/traces?fromTimestamp={_iso(args.hours)}&limit={args.limit}")["data"]
        if args.name:
            tr = [t for t in tr if args.name.lower() in (t.get("name") or "").lower()]
        ids = [t["id"] for t in tr]

    print(f"{'trace':14s} {'when':20s} {'gens':>4s} {'cached':>6s} {'tok_sum':>8s} {'ttft_p50':>8s}")
    for tid in ids:
        s = _stats(tid)
        # when?
        d = _get(f"/api/public/traces/{tid}")
        ts = (d.get("timestamp") or "")[:19].replace("T", " ")
        flag = "  <-- BLIND?(0 across fast run)" if s["cached_gens"] == 0 else ""
        print(f"{tid[:12]:14s} {ts:20s} {s['gens']:>4d} {s['cached_gens']:>6d} {s['cached_sum']:>8d} "
              f"{str(s['ttft_p50']):>8s}{flag}")
        for st, (c, n) in sorted(s["states"].items()):
            print(f"    {st:20s} {c}/{n} cached")
    if len(ids) == 1:
        print("\nper-generation:")
        for i, g in enumerate(_trace_gens(ids[0]), 1):
            ud = g.get("usageDetails") or {}
            print(f"  g{i:02d} {(g.get('name') or ''):22s} in={str(ud.get('input')):>6s} "
                  f"cache_read={ud.get('cache_read')} priority={ud.get('priority_cache_read')} "
                  f"ttft={ud.get('ttft_ms')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
