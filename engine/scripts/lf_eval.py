#!/usr/bin/env python
"""lf_eval.py — Langfuse datasets + eval ladder for the llm2llm battery.

Wraps the harness (tests/llm2llm/harness.py) with the SOP ladder, an assassin
persona gate, a persona dataset in Langfuse, and post-run scoring.

Usage (env auto-loads from .env):
  python scripts/lf_eval.py bootstrap                 # idempotent: dataset 'dialux-personas'
  python scripts/lf_eval.py ladder [--agent-model M]  # SOP order: happy -> stress -> curve
  python scripts/lf_eval.py ladder --assassin         # ALSO runs assassin-type personas (opt-in ONLY)
  python scripts/lf_eval.py status                    # recent json_logs vs Langfuse scores
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.llm2llm.personas import PERSONAS  # noqa: E402

DATASET_NAME = "dialux-personas"
HARNESS = ROOT / "tests" / "llm2llm" / "harness.py"
LOGS = ROOT / "tests" / "llm2llm" / "json_logs"

# SOP ladder order: happy -> stress -> curve -> gatekeepers -> breakers.
# The assassin persona (Larry, tagged "assassin": True — its ONLY purpose is to
# break the agent) is NEVER in the default ladder — it runs once, on demand,
# behind the explicit --assassin flag. The 25-persona registry has exactly one.
LADDER = ["happy", "stress", "curve", "gatekeepers", "breakers"]


def is_assassin(p: dict) -> bool:
    return bool(p.get("assassin")) or p.get("type") == "assassin" or "assassin" in p["name"].lower()


def _lf():
    from langfuse import Langfuse
    return Langfuse()


def _rest(path: str) -> dict:
    """GET the Langfuse REST API directly (SDK 4.15 pydantic models are stricter
    than the self-hosted server — e.g. dataset items lack media_references)."""
    from lf import _get
    return _get(path)


def _post(path: str, body: dict) -> dict:
    from lf import _env
    import base64
    import json as _json
    import urllib.request
    host, pk, sk = _env()
    req = urllib.request.Request(
        f"{host}{path}", method="POST",
        data=_json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Basic " + base64.b64encode(f"{pk}:{sk}".encode()).decode()},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


# ---------------------------------------------------------------- bootstrap
def cmd_bootstrap(_):
    try:
        ds = _rest(f"/api/public/datasets/{DATASET_NAME}")
        page = _rest(f"/api/public/dataset-items?datasetId={ds['id']}&limit=100")
        items = [it.get("input") or {} for it in (page.get("data") or [])]
        print(f"dataset '{DATASET_NAME}' exists ({len(items)} items)")
    except Exception:
        try:
            _post("/api/public/datasets", {"name": DATASET_NAME})
            print(f"dataset '{DATASET_NAME}' created")
        except Exception:
            pass  # 400 = already exists (race) — re-read below
        ds = _rest(f"/api/public/datasets/{DATASET_NAME}")
        page = _rest(f"/api/public/dataset-items?datasetId={ds['id']}&limit=100")
        items = [it.get("input") or {} for it in (page.get("data") or [])]
    have = {it.get("persona") for it in items if isinstance(it, dict)}
    added = 0
    for p in PERSONAS:
        if p["name"] in have:
            continue
        _post("/api/public/dataset-items", {
            "datasetName": DATASET_NAME,
            "input": {"persona": p["name"]},
            "metadata": {"type": p["type"], "expect": p["expect"],
                         "assassin": is_assassin(p)},
        })
        added += 1
    print(f"added {added} item(s); registry = {len(PERSONAS)} personas "
          f"({sum(is_assassin(p) for p in PERSONAS)} assassin, gated behind --assassin)")
    return 0


# ---------------------------------------------------------------- ladder
def cmd_ladder(a):
    from tests.llm2llm.personas import persona_groups
    groups = persona_groups()
    gated = [p["name"] for p in PERSONAS if is_assassin(p)]
    if a.assassin and gated:
        print(f"ASSASSIN MODE — including {len(gated)} break-the-agent persona(s): {gated}")
        stages = LADDER + ["assassin"]
    elif gated and not a.assassin:
        print(f"(assassin persona(s) {gated} EXCLUDED — pass --assassin to include, on purpose, once)")
        stages = LADDER
    else:
        stages = LADDER

    rc = 0
    for stage in stages:
        if stage == "assassin":
            names = ",".join(gated)
        else:
            names = stage
        print(f"\n=== stage: {stage} → harness --personas {names} ===")
        cmd = [str(ROOT / ".venv" / "bin" / "python"), str(HARNESS),
               "--personas", names, "--rag", "--langfuse",
               "--max-turns", str(a.max_turns), "--agent-model", a.agent_model]
        r = subprocess.run(cmd, cwd=ROOT)
        rc = rc or r.returncode
        if r.returncode != 0 and stage == "happy":
            print("HAPPY FAILED — SOP: fix the agent before stress. Stopping the ladder.")
            return rc
    return rc


# ---------------------------------------------------------------- status
def cmd_status(a):
    """Recent json_logs (last N minutes) vs the harness_result scores in Langfuse."""
    from datetime import datetime, timezone
    lf = _lf()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=a.since_min)
    traces = {t.id: t for t in lf.api.trace.list(from_timestamp=cutoff, limit=100).data}
    by_persona = {}
    for t in traces.values():
        meta = (getattr(t, "metadata", None) or {})
        name = meta.get("persona") if isinstance(meta, dict) else None
        if name:
            by_persona.setdefault(name, t)   # list is newest-first: keep the newest trace
    scored = {}
    for t in by_persona.values():
        full = lf.api.trace.get(t.id)
        for s in (full.scores or []):
            if s.name == "harness_result":
                scored[t.metadata["persona"]] = s.value
    rows = sorted(LOGS.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:a.limit]
    print(f"{'persona':46s} {'langfuse score':>14s}  json_log")
    for p in rows:
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        name = d.get("persona", "?")
        s = scored.get(name)
        s = ("—" if s is None else ("PASS" if s == 1 else "FAIL"))
        fresh = "fresh" if (time.time() - p.stat().st_mtime) < a.since_min * 60 else "    "
        print(f"{name[:46]:46s} {s:>14s}  {fresh} {p.name}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("bootstrap")
    l = sub.add_parser("ladder")
    l.add_argument("--agent-model", default="gpt-4.1")
    l.add_argument("--max-turns", type=int, default=28)
    l.add_argument("--assassin", action="store_true",
                   help="OPT-IN ONLY: include assassin-type persona(s) (designed to break the agent)")
    s = sub.add_parser("status")
    s.add_argument("--since-min", type=int, default=120)
    s.add_argument("--limit", type=int, default=15)
    a = ap.parse_args()
    sys.exit({"bootstrap": cmd_bootstrap, "ladder": cmd_ladder, "status": cmd_status}[a.cmd](a))


if __name__ == "__main__":
    main()
