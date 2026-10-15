#!/usr/bin/env python
"""lf.py — fast Langfuse/OTEL query SDK for the v5 build.

Usage (env auto-loaded from .env):
  python scripts/lf.py health                    # server + pipeline check
  python scripts/lf.py traces [--hours 24] [--name SUBSTR] [--limit N]
  python scripts/lf.py show <trace_id>           # observation map (sequence)
  python scripts/lf.py gens <trace_id> [--state ConfirmSlots] [--last N] [--input]
  python scripts/lf.py tools <trace_id> [--name SUBSTR]
  python scripts/lf.py usage [--hours 48]        # tokens + cost estimate
  python scripts/lf.py costs [--hours 24] [--name SUBSTR] [--limit N]  # exact per-model costs
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


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
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - ts * 3600))


def _traces(hours: float, name: str | None, limit: int) -> list[dict]:
    q = f"/api/public/traces?fromTimestamp={_iso(hours)}&limit={limit}"
    d = _get(q)
    ts = d.get("data", [])
    if name:
        ts = [t for t in ts if name.lower() in (t.get("name") or "").lower()]
    return ts


def _obs(trace_id: str) -> list[dict]:
    tid = trace_id
    if len(tid) < 32:  # short id -> resolve against recent traces
        for t in _traces(168.0, None, 100):
            if t["id"].startswith(tid):
                tid = t["id"]
                break
    d = _get(f"/api/public/traces/{tid}")
    obs = d.get("observations", [])
    obs.sort(key=lambda o: o.get("startTime") or "")
    return obs


def _usage(obs: list[dict]) -> tuple[int, int, int]:
    tin = tout = n = 0
    for o in obs:
        if (o.get("type") or "").upper() == "GENERATION":
            n += 1
            u = o.get("usageDetails") or {}
            tin += u.get("input", 0) or 0
            tout += u.get("output", 0) or 0
    return tin, tout, n


# ---------------------------------------------------------------- commands
def cmd_health(_):
    host, _, _ = _env()
    try:
        h = _get("/api/public/health")
        print(f"server: OK {h.get('version')} @ {host}")
    except Exception as e:
        print(f"server: FAIL ({e})")
        return 1
    ts = _traces(1.0, None, 1)
    print(f"ingest pipeline: {'OK' if ts else 'NO RECENT TRACES (worker/clickhouse down?)'}")
    return 0


def cmd_traces(a):
    for t in _traces(a.hours, a.name, a.limit):
        tin, tout, n = _usage(_obs(t["id"]))
        print(f"{(t.get('timestamp') or '')[11:19]} {t.get('id')[:12]} {t.get('name')}"
              f"  gens={n} in={tin/1000:.0f}k out={tout/1000:.1f}k")
    return 0


def cmd_show(a):
    obs = _obs(a.trace_id)
    kinds = {}
    for o in obs:
        k = (o.get("type") or "?").lower()
        kinds[k] = kinds.get(k, 0) + 1
        name = o.get("name") or "?"
        extra = ""
        if k == "tool":
            out = o.get("output") or {}
            extra = f" status={out.get('status')}" if isinstance(out, dict) else ""
        print(f"  {(o.get('startTime') or '')[11:19]} {k:11s} {name}{extra}")
    print("counts:", kinds)
    return 0


def cmd_gens(a):
    obs = _obs(a.trace_id)
    gens = [o for o in obs if (o.get("type") or "").upper() == "GENERATION"]
    if a.state:
        gens = [g for g in gens if a.state.lower() in (g.get("name") or "").lower()]
    seq = [(g.get("name") or "?").replace("llm:", "") for g in gens]
    print("state sequence:", " -> ".join(seq) if seq else "(none)")
    sel = gens[-a.last:] if a.last else gens
    for g in sel:
        out = g.get("output") or {}
        print(f"\n=== {g.get('name')} @ {(g.get('startTime') or '')[11:19]} "
              f"lat={((g.get('endTime') or '') > (g.get('startTime') or '')) and 'ok'}")
        if isinstance(out, dict):
            print("  text:", repr((out.get("text") or "")[:180]))
            print("  tool_calls:", [tc.get("name") for tc in out.get("tool_calls", [])])
        u = g.get("usageDetails") or {}
        print(f"  tokens: in={u.get('input', 0)} out={u.get('output', 0)}")
        if a.input:
            inp = g.get("input")
            msgs = inp if isinstance(inp, list) else (inp or {}).get("messages", [])
            for m in msgs[-3:]:
                c = m.get("content") or ""
                print(f"  [{m.get('role')}] {len(c)}ch: {c[:300]!r}")
    return 0


def cmd_pair(a):
    """iter97c/track (owner order 2026-10-08): eager speak/record pairing + second-batch
    detection from a trace's generations, BY MACHINE. For gen i: spoken_i = output.text;
    recorded_i = the LAST [assistant] entry in gen i's input (= what history holds for
    turn i-1). pair(j) = spoken_j vs recorded_{j+1}. Also flags SECOND-BATCH emissions:
    consecutive gens whose newest user message is IDENTICAL (agent emitted 2 rounds for
    the same caller material = the 'two batches generated' ear signature), and the
    echo-insert signature (a user message appearing 2+ times inside ONE gen input)."""
    import hashlib
    obs = _obs(a.trace_id)
    gens = [o for o in obs if (o.get("type") or "").upper() == "GENERATION"]
    gens.sort(key=lambda g: g.get("startTime") or "")
    spoken, rec_last, last_user, ts = [], [], [], []
    for g in gens:
        out = g.get("output") or {}
        spoken.append((out.get("text") or "") if isinstance(out, dict) else "")
        inp = g.get("input")
        msgs = inp if isinstance(inp, list) else (inp or {}).get("messages", [])
        am = [m for m in msgs if m.get("role") == "assistant"]
        rec_last.append((am[-1].get("content") or "") if am else "")
        um = [m for m in msgs if m.get("role") == "user"]
        last_user.append((um[-1].get("content") or "") if um else "")
        ts.append((g.get("startTime") or "")[11:19])
    n = len(gens)
    print(f"trace {a.trace_id}: {n} gens")
    mm = 0
    for j in range(n - 1):
        sp, rc = spoken[j].strip(), rec_last[j + 1].strip()
        match = sp[:80] == rc[:80]
        if not match:
            mm += 1
        print(f"pair T{j:02d} @{ts[j]}->{ts[j + 1]}  {'MATCH ' if match else 'MISMATCH'}"
              f"  spoken[{len(sp):3d}]: {sp[:72]!r}")
        if not match:
            print(f"           recorded[{len(rc):3d}]: {rc[:72]!r}")
    print(f"pairing: {n - 1 - mm}/{n - 1} MATCH, {mm} MISMATCH")
    # second-batch: same newest-user answered by consecutive gens
    sb = 0
    for j in range(n - 1):
        if last_user[j] and last_user[j] == last_user[j + 1]:
            sb += 1
            print(f"SECOND-BATCH @{ts[j]}->{ts[j + 1]}: same caller material answered twice")
    # echo inserts: user content seen 2+ times within one gen input
    ech = 0
    for g, t in zip(gens, ts):
        inp = g.get("input")
        msgs = inp if isinstance(inp, list) else (inp or {}).get("messages", [])
        seen = {}
        for m in msgs:
            if m.get("role") == "user":
                c = (m.get("content") or "")
                k = c.lower().replace(",", "").replace(".", "").replace("’", "'").strip()
                seen.setdefault(k, []).append(c)
        for k, vs in seen.items():
            if len(vs) > 1:
                ech += 1
                print(f"ECHO @{t}: user entry present {len(vs)}x (variants) in one turn input: {vs[0][:60]!r}")
    print(f"second-batch pairs: {sb}, turns with dup/variant user entries: {ech}")
    return 0


def cmd_tools(a):
    obs = _obs(a.trace_id)
    for o in obs:
        if (o.get("type") or "").upper() != "TOOL":
            continue
        n = o.get("name") or ""
        if a.name and a.name.lower() not in n.lower():
            continue
        out = o.get("output")
        lat = ""
        st, en = o.get("startTime"), o.get("endTime")
        if st and en:
            from datetime import datetime
            try:
                lat = f" {int((datetime.fromisoformat(en.replace('Z','+00:00')) - datetime.fromisoformat(st.replace('Z','+00:00'))).total_seconds()*1000)}ms"
            except Exception:
                pass
        print(f"{(o.get('startTime') or '')[11:19]} {n:38s}{lat}  {json.dumps(out)[:200]}")
    return 0


def cmd_usage(a):
    tin = tout = gens = 0
    for t in _traces(a.hours, None, 100):
        i, o, n = _usage(_obs(t["id"]))
        tin += i; tout += o; gens += n
    m = 1_000_000
    print(f"traces scanned: last {a.hours}h | generations: {gens}")
    print(f"tokens: input={tin/m:.2f}M output={tout/m:.3f}M")
    print(f"cost est (blended gpt-5.1+gpt-4o): ${tin/m*1.9 + tout/m*10:.2f}")
    return 0


# USD per 1M tokens (input, output) — list prices. Override per model with
# env LANGFUSE_MODEL_PRICES='{"gpt-4.1":[2.0,8.0], ...}' (exact prices change;
# per-generation `model` labels come straight from the trace observations).
_DEFAULT_PRICES = {
    "gpt-4.1": (2.0, 8.0), "gpt-4.1-mini": (0.4, 1.6), "gpt-4.1-nano": (0.1, 0.4),
    "gpt-4o": (2.5, 10.0), "gpt-4o-mini": (0.15, 0.6),
    "gpt-5.1": (1.25, 10.0), "gpt-5.2": (1.25, 10.0), "gpt-5.4": (1.25, 10.0),
}


def _prices() -> dict:
    import json as _json
    out = dict(_DEFAULT_PRICES)
    try:
        out.update({k: tuple(v) for k, v in
                    _json.loads(os.environ.get("LANGFUSE_MODEL_PRICES", "{}")).items()})
    except Exception:
        pass
    return out


def cmd_costs(a):
    """Exact per-model token + cost totals (SDK-grade: real per-generation model labels)."""
    per: dict[str, list[int]] = {}
    traces = _traces(a.hours, a.name, a.limit)
    for t in traces:
        for o in _obs(t["id"]):
            if (o.get("type") or "").upper() != "GENERATION":
                continue
            model = o.get("model") or "unknown"
            u = o.get("usageDetails") or {}
            vals = per.setdefault(model, [0, 0, 0])
            vals[0] += u.get("input", 0) or 0
            vals[1] += u.get("output", 0) or 0
            vals[2] += 1
    prices = _prices()
    total = 0.0
    print(f"{'model':20s} {'gens':>5s} {'in':>9s} {'out':>9s} {'cost':>9s}")
    unpriced = False
    for model, (tin, tout, n) in sorted(per.items(), key=lambda kv: -kv[1][0]):
        pin, pout = prices.get(model, (None, None))
        if pin is None:
            unpriced = True
            cost_s = "?"
        else:
            cost = tin / 1e6 * pin + tout / 1e6 * pout
            total += cost
            cost_s = f"${cost:.2f}"
        print(f"{model:20s} {n:>5d} {tin:>9,} {tout:>9,} {cost_s:>9s}")
    if unpriced:
        print("(? = no price for that model — set LANGFUSE_MODEL_PRICES)")
    print(f"TOTAL (last {a.hours}h, {len(traces)} traces): ${total:.2f}")
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("health")
    t = sub.add_parser("traces"); t.add_argument("--hours", type=float, default=24); t.add_argument("--name"); t.add_argument("--limit", type=int, default=20)
    s = sub.add_parser("show"); s.add_argument("trace_id")
    g = sub.add_parser("gens"); g.add_argument("trace_id"); g.add_argument("--state"); g.add_argument("--last", type=int, default=0); g.add_argument("--input", action="store_true")
    tl = sub.add_parser("tools"); tl.add_argument("trace_id"); tl.add_argument("--name")
    pa = sub.add_parser("pair"); pa.add_argument("trace_id")
    u = sub.add_parser("usage"); u.add_argument("--hours", type=float, default=48)
    c = sub.add_parser("costs"); c.add_argument("--hours", type=float, default=24); c.add_argument("--name"); c.add_argument("--limit", type=int, default=200)
    a = p.parse_args()
    sys.exit({
        "health": cmd_health, "traces": cmd_traces, "show": cmd_show,
        "gens": cmd_gens, "tools": cmd_tools, "pair": cmd_pair,
        "usage": cmd_usage, "costs": cmd_costs,
    }[a.cmd](a))


if __name__ == "__main__":
    main()
