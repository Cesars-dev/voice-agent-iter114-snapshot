#!/usr/bin/env python3
"""latency.py — v5 latency extraction SDK.

Pulls per-call latency composition from json_logs (turn wall time) and
Langfuse (GENERATION / TOOL / rag spans) and prints a compact analysis:
LLM-only p50/p90, RAG p50, tool p50, per-turn breakdown, token stats,
prompt-cache estimate. No LLM needed to read the output.

Usage:
  python scripts/latency.py --hours 6 [--name SUBSTR] [--json]
  python scripts/latency.py --logs tests/llm2llm/json_logs [--json]
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import statistics as st
import sys
import time
import urllib.request
from pathlib import Path


def _get(host: str, auth: str, path: str) -> dict | list:
    req = urllib.request.Request(host + path, headers={"Authorization": auth})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def _pct(vals: list[float], q: float) -> float:
    if not vals:
        return 0.0
    v = sorted(vals)
    return round(v[min(int(q * len(v)), len(v) - 1)], 0)


def _auth() -> tuple[str, str]:
    env = Path(__file__).resolve().parents[1] / ".env"
    for line in env.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())
    host = os.environ["LANGFUSE_HOST"].rstrip("/")
    auth = "Basic " + base64.b64encode(
        f"{os.environ['LANGFUSE_PUBLIC_KEY']}:{os.environ['LANGFUSE_SECRET_KEY']}".encode()
    ).decode()
    return host, auth


def pull_traces(host: str, auth: str, hours: int, name: str) -> list[dict]:
    frm = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - hours * 3600))
    out, page = [], 1
    while True:
        d = _get(host, auth, f"/api/public/traces?fromTimestamp={frm}&limit=100&page={page}")
        items = [t for t in d.get("data", []) if name in (t.get("name") or "")]
        out += items
        if page * 100 >= d.get("meta", {}).get("totalItems", 0) or not d.get("data"):
            break
        page += 1
    return out


def analyze_trace(host: str, auth: str, trace: dict) -> dict:
    tid = trace["id"]
    obs = []
    page = 1
    while True:
        d = _get(host, auth, f"/api/public/observations?traceId={tid}&limit=100&page={page}")
        obs += d.get("data", [])
        if page * 100 >= d.get("meta", {}).get("totalItems", 0) or not d.get("data"):
            break
        page += 1
    gens, tools, rags = [], [], []
    for o in obs:
        t = o.get("type")
        dur = None
        if o.get("startTime") and o.get("endTime"):
            from datetime import datetime
            s = datetime.fromisoformat(o["startTime"].replace("Z", "+00:00"))
            e = datetime.fromisoformat(o["endTime"].replace("Z", "+00:00"))
            dur = (e - s).total_seconds() * 1000
        if t == "GENERATION" and dur is not None:
            usage = o.get("usageDetails") or {}
            gens.append({"ms": dur,
                         "in_tok": usage.get("input", usage.get("inputTokens")) or 0,
                         "out_tok": usage.get("output", usage.get("outputTokens")) or 0,
                         "cache_read": usage.get("cache_read", 0) or 0,
                         "model": o.get("model"), "name": o.get("name")})
        elif t == "TOOL" and dur is not None:
            tools.append({"ms": dur, "name": o.get("name")})
        elif t == "SPAN" and o.get("name") == "rag":
            outp = o.get("output") or {}
            ms = outp.get("ms") if isinstance(outp, dict) else None
            rags.append({"ms": ms if ms is not None else (dur or 0), "cached": outp.get("cached") if isinstance(outp, dict) else None})
    gms = [g["ms"] for g in gens]
    cache_read = sum(g["cache_read"] for g in gens)
    in_tok = sum(g["in_tok"] for g in gens)
    return {
        "trace": trace["name"], "id": tid[:8],
        "gens": len(gens),
        "llm_p50": _pct(gms, .5), "llm_p90": _pct(gms, .9), "llm_mean": round(st.mean(gms), 0) if gms else 0,
        "llm_total_s": round(sum(gms) / 1000, 1),
        "in_tok_mean": round(st.mean([g["in_tok"] for g in gens]), 0) if gens else 0,
        "in_tok_total": in_tok,
        "cache_read_total": cache_read,
        "cache_hit_pct": round(100 * cache_read / in_tok, 1) if in_tok else 0.0,
        "models": sorted({g["model"] for g in gens}),
        "tools": len(tools), "tool_p50": _pct([t["ms"] for t in tools], .5),
        "tool_total_s": round(sum(t["ms"] for t in tools) / 1000, 1),
        "rag": len(rags), "rag_p50": _pct([r["ms"] for r in rags], .5),
        "rag_cached": sum(1 for r in rags if r.get("cached")),
        "rag_total_s": round(sum(r["ms"] for r in rags) / 1000, 1),
    }


def from_json_logs(logdir: str) -> list[dict]:
    rows = []
    for f in sorted(glob.glob(f"{logdir}/*.json")):
        d = json.loads(Path(f).read_text())
        ms = sorted(d.get("turn_ms") or [])
        if not ms:
            continue
        tools_flat = [t for r in d.get("transcript", []) for t in r.get("tools", [])]
        rows.append({
            "trace": d.get("call_id", Path(f).stem), "turns": d.get("turns"),
            "turn_p50": _pct([m for m in ms], .5), "turn_p90": _pct(ms, .9),
            "wall_s": d.get("wall_s"), "tools": len(tools_flat),
            "end_calls": tools_flat.count("end_call"),
            "p50_ms_logged": d.get("p50_ms"),
        })
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=6)
    ap.add_argument("--name", default="llm2llm-graph-")
    ap.add_argument("--logs", default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if a.logs:
        rows = from_json_logs(a.logs)
        print(json.dumps(rows, indent=1) if a.json else
              f"{'call':34}{'turns':>6}{'turnP50':>9}{'turnP90':>9}{'wall':>8}{'tools':>7}{'end':>5}")
        if not a.json:
            for r in rows:
                print(f"{r['trace'][:33]:34}{r['turns']:>6}{r['turn_p50']:>8.0f}ms{r['turn_p90']:>8.0f}ms{r['wall_s']:>7.1f}s{r['tools']:>7}{r['end_calls']:>5}")
        return
    host, auth = _auth()
    traces = pull_traces(host, auth, a.hours, a.name)
    rows = [analyze_trace(host, auth, t) for t in traces]
    rows.sort(key=lambda r: r["trace"])
    if a.json:
        print(json.dumps(rows, indent=1))
        return
    hdr = f"{'call':30}{'gens':>5}{'LLMp50':>8}{'LLMp90':>8}{'LLMmean':>9}{'in_tok':>8}{'cache%':>8}{'LLMtot':>8}{'ragP50':>8}{'toolP50':>9}{'toolTot':>9}"
    print(hdr)
    for r in rows:
        print(f"{r['trace'][:29]:30}{r['gens']:>5}{r['llm_p50']:>7.0f}ms{r['llm_p90']:>7.0f}ms{r['llm_mean']:>8.0f}ms"
              f"{r['in_tok_mean']:>8.0f}{r['cache_hit_pct']:>7.1f}%{r['llm_total_s']:>7.1f}s{r['rag_p50']:>7.0f}ms{r['tool_p50']:>8.0f}ms{r['tool_total_s']:>8.1f}s")
    if rows:
        tot_in = sum(r["in_tok_total"] for r in rows)
        tot_cr = sum(r["cache_read_total"] for r in rows)
        hit = f"{100*tot_cr/tot_in:.1f}%" if tot_in else "n/a"
        print(f"\nBATTERY: gens={sum(r['gens'] for r in rows)}"
              f"  LLM p50={_pct([r['llm_p50'] for r in rows], .5):.0f}ms"
              f"  in_tok/call={tot_in/len(rows):.0f}"
              f"  cache_hit={hit}"
              f"  rag_cached={sum(r['rag_cached'] for r in rows)}/{sum(r['rag'] for r in rows)}")


if __name__ == "__main__":
    main()
