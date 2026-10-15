"""iter32 silent-round meter — ground truth = Langfuse GENERATION output.text.

A round is SILENT when the model's generation produced empty text (tool-call
JSON still counts as output tokens, so zero-token checks hide silent rounds;
the text field is the truth). Pulls /api/public/traces for a window, then each
trace's GENERATION observations, and prints per-trace + totals.

Usage:
  .venv/bin/python scripts/silent_rounds.py --from 2026-09-07T16:22:00Z \
      --to 2026-09-07T16:45:00Z [--name llm2llm-graph-]
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import time
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
    """output can be a dict ({"text": ...}) or a plain string."""
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="frm", required=True,
                    help="window start, e.g. 2026-09-07T16:22:00Z")
    ap.add_argument("--to", dest="to", required=True,
                    help="window end, e.g. 2026-09-07T16:45:00Z")
    ap.add_argument("--name", default="llm2llm-graph-")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    host, auth = _auth()
    traces = pull_traces(host, auth, a.frm, a.to, a.name)
    traces.sort(key=lambda t: (t.get("timestamp") or ""))

    rows = []
    tot_g = tot_s = 0
    for t in traces:
        gens = trace_gens(host, auth, t["id"])
        silent = sum(1 for g in gens if not _gen_text(g.get("output")).strip())
        speech = len(gens) - silent
        rows.append({"trace": t.get("name"), "id": t["id"][:8],
                     "gens": len(gens), "speech": speech, "silent": silent,
                     "silent_pct": round(100 * silent / len(gens), 1) if gens else 0.0})
        tot_g += len(gens)
        tot_s += silent

    if a.json:
        print(json.dumps(rows + [{"TOTAL": {"gens": tot_g, "silent": tot_s,
                "silent_pct": round(100 * tot_s / tot_g, 1) if tot_g else 0.0}}], indent=1))
        return
    print(f"{'trace':34}{'gens':>6}{'speech':>8}{'silent':>8}{'silent%':>9}")
    for r in rows:
        print(f"{(r['trace'] or '?')[:33]:34}{r['gens']:>6}{r['speech']:>8}"
              f"{r['silent']:>8}{r['silent_pct']:>8}%")
    pct = round(100 * tot_s / tot_g, 1) if tot_g else 0.0
    print(f"{'TOTAL':34}{tot_g:>6}{tot_g - tot_s:>8}{tot_s:>8}{pct:>8}%")


if __name__ == "__main__":
    main()
