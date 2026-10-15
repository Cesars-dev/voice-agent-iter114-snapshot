"""iter33 per-state latency meter — READ-ONLY Langfuse REST reader.

Answers: LLM calls per state, latency per LLM call, latency per round.
Pulls /api/public/traces for a window (same pattern as silent_rounds.py),
then each trace's GENERATION observations. Per observation:
  state    = observation name (format llm:<State>)
  latency  = endTime - startTime (seconds)
  silent   = empty (output or {}).get("text","")
Prints (a) per-state aggregate table, (b) per-trace per-call list in
startTime order (state + seconds), (c) TOTAL row. Sort by state name.

Usage:
  .venv/bin/python scripts/state_latency.py \
      --from 2026-09-08T10:19:30Z --to 2026-09-08T10:27:00Z
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import urllib.request
from pathlib import Path


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


def _get(host: str, auth: str, path: str) -> dict:
    req = urllib.request.Request(host + path, headers={"Authorization": auth})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _gen_text(output) -> str:
    if isinstance(output, dict):
        return str(output.get("text") or "")
    return str(output or "")


def pull_traces(host: str, auth: str, frm: str, to: str, name: str) -> list[dict]:
    out, page = [], 1
    while True:
        d = _get(host, auth,
                 f"/api/public/traces?fromTimestamp={frm}&toTimestamp={to}"
                 f"&limit=100&page={page}")
        items = [t for t in d.get("data", []) if name in (t.get("name") or "")]
        out += items
        meta = d.get("meta", {})
        if page * 100 >= meta.get("totalItems", 0) or not d.get("data"):
            break
        page += 1
    return out


def trace_gens(host: str, auth: str, trace_id: str) -> list[dict]:
    out, page = [], 1
    while True:
        d = _get(host, auth,
                 f"/api/public/observations?traceId={trace_id}"
                 f"&type=GENERATION&limit=100&page={page}")
        out += d.get("data", [])
        meta = d.get("meta", {})
        if page * 100 >= meta.get("totalItems", 0) or not d.get("data"):
            break
        page += 1
    return out


def _iso_s(v) -> float:
    """Langfuse ISO-8601 Z timestamp -> epoch seconds."""
    if not v:
        return 0.0
    s = str(v).replace("Z", "+00:00")
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(s)
        return dt.timestamp()
    except ValueError:
        return 0.0


def _pct(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    sv = sorted(vals)
    k = max(0, min(len(sv) - 1, int(round((p / 100.0) * (len(sv) - 1)))))
    return sv[k]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="frm", required=True)
    ap.add_argument("--to", dest="to", required=True)
    ap.add_argument("--name", default="llm2llm-graph-")
    a = ap.parse_args()

    host, auth = _auth()
    traces = pull_traces(host, auth, a.frm, a.to, a.name)
    traces.sort(key=lambda t: (t.get("timestamp") or ""))

    # per-observation records: {trace, state, lat_s, silent}
    recs = []
    per_trace = []          # [(trace_name, [ (idx, state, lat_s, silent) ])]
    for t in traces:
        gens = trace_gens(host, auth, t["id"])
        gens.sort(key=lambda g: (g.get("startTime") or ""))
        rows = []
        for i, g in enumerate(gens, 1):
            state = (g.get("name") or "?").replace("llm:", "")
            lat = _iso_s(g.get("endTime")) - _iso_s(g.get("startTime"))
            silent = not _gen_text(g.get("output")).strip()
            recs.append({"trace": t.get("name"), "state": state,
                         "lat": lat, "silent": silent})
            rows.append((i, state, lat, silent))
        per_trace.append((t.get("name"), rows))

    # (a) per-state aggregate
    states: dict[str, dict] = {}
    for r in recs:
        s = states.setdefault(r["state"], {"calls": 0, "lats": [], "silent": 0})
        s["calls"] += 1
        s["lats"].append(r["lat"])
        if r["silent"]:
            s["silent"] += 1
    print(f"{'state':22}{'calls':>7}{'p50(s)':>9}{'p90(s)':>9}{'mean(s)':>9}"
          f"{'silent':>8}{'silent%':>9}")
    for name in sorted(states):
        s = states[name]
        lats = s["lats"]
        print(f"{name[:21]:22}{s['calls']:>7}{_pct(lats, 50):>9.2f}"
              f"{_pct(lats, 90):>9.2f}{sum(lats) / len(lats):>9.2f}"
              f"{s['silent']:>8}"
              f"{(100 * s['silent'] / s['calls']):>8.1f}%")
    tot = len(recs)
    tot_sil = sum(1 for r in recs if r["silent"])
    all_lats = [r["lat"] for r in recs]
    print(f"{'TOTAL':22}{tot:>7}{_pct(all_lats, 50):>9.2f}{_pct(all_lats, 90):>9.2f}"
          f"{sum(all_lats) / max(tot, 1):>9.2f}{tot_sil:>8}"
          f"{(100 * tot_sil / max(tot, 1)):>8.1f}%")

    # (b) per-trace per-call list (startTime order)
    print()
    for tname, rows in per_trace:
        print(f"## {tname}  ({len(rows)} gens)")
        for i, state, lat, silent in rows:
            print(f"  {i:>3}  {state[:20]:21} {lat:>7.2f}s  "
                  f"{'SILENT' if silent else 'speech'}")

    # (c) per-round latency histogram (latency buckets across all gens)
    print()
    buckets = [(0, 1), (1, 2), (2, 3), (3, 4), (4, 6), (6, 10), (10, 999)]
    print("latency histogram (all gens):")
    for lo, hi in buckets:
        n = sum(1 for r in recs if lo <= r["lat"] < hi)
        print(f"  {lo:>2}-{hi if hi < 999 else '∞':>3}s  {n:>4}  "
              f"{'#' * max(0, n // 5)}")


if __name__ == "__main__":
    main()
