#!/usr/bin/env python
"""call.py — call-analysis SDK: one command to know everything about a test call.

Reads harness logs (tests/llm2llm/json_logs/*.json) and optionally joins the
Langfuse trace (webhook payloads, gate rejection reasons, slot ground truth).

Usage:
  python scripts/call.py list [--hours 48]              # every call, outcome/pass/turns
  python scripts/call.py show <substr>                  # full JSON dump of one call
  python scripts/call.py sop <substr> [--trace <tid>]   # SOP auto-checks + JSON dossier
  python scripts/call.py dvs <substr>                   # final DVS + echo-contamination check

Exit codes: 0 ok, 1 not found / checks failed.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "tests" / "llm2llm" / "json_logs"

REQUIRED_IF_BOOKED = ("first_name", "callback_number", "selected_time", "booking_uid", "slot_verified")

# tools that must appear exactly once in a clean booking call
ONCE_TOOLS = {"create_livecall_booking", "record_booking_uid", "end_call", "transition_to_Booking"}


def _find(substr: str) -> Path:
    hits = sorted(LOGS.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    hits = [h for h in hits if substr.lower() in h.name.lower()]
    if not hits:
        print(json.dumps({"error": "no log matches", "substr": substr}), file=sys.stderr)
        sys.exit(1)
    return hits[0]


def _load(p: Path) -> dict:
    return json.load(open(p))


def _name_from_transcript(d: dict) -> str | None:
    """Heuristic: first 'my name is X' / 'I'm X' caller utterance."""
    for t in d.get("transcript", []):
        c = (t.get("caller") or "")
        m = (re.search(r"my (?:first )?name is ([A-Z][a-z]+)", c, re.I)
             or re.search(r"I'?m ([A-Z][a-z]+)\b", c))
        if m:
            return m.group(1).capitalize()
    return None


def auto_checks(d: dict) -> list[dict]:
    """Deterministic SOP subset — judgment calls stay with the analyst."""
    out = []
    fd = d.get("final_dvs", {})
    tools = d.get("tools", [])

    # 1. echo-contamination: transcript-declared name vs final dvs
    said = _name_from_transcript(d)
    if said and fd.get("first_name") and said.lower() != str(fd.get("first_name")).lower():
        out.append({"check": "echo_contamination", "severity": "HIGH",
                    "detail": f"caller identified as {said!r} but final_dvs.first_name="
                              f"{fd.get('first_name')!r} (webhook echo overwrote per-run data?)"})

    # 2. required dvs present when booked
    if d.get("booked"):
        missing = [k for k in REQUIRED_IF_BOOKED if not fd.get(k)]
        if missing:
            out.append({"check": "required_dvs_missing", "severity": "HIGH", "detail": missing})

    # 3. tool call counts
    for t in ONCE_TOOLS:
        n = tools.count(t)
        if n > 1:
            out.append({"check": "tool_called_multiple", "severity": "MED",
                        "detail": f"{t} fired {n}x (retries hide downstream flakiness)"})

    # 4. transitions attempted but bounced
    gr = d.get("gate_rejections", 0)
    if gr:
        out.append({"check": "gate_rejections", "severity": "INFO",
                    "detail": f"{gr} transition attempt(s) bounced by gate (self-corrected: {d.get('pass')})"})

    # 5. booking attempts vs success
    b = d.get("booking", {})
    if d.get("expect") == "book" and b.get("fired") and not b.get("booked"):
        out.append({"check": "booking_failed", "severity": "HIGH", "detail": b})

    # 6. latency
    p50 = d.get("p50_ms")
    if p50 and p50 > 8000:
        out.append({"check": "slow_turns", "severity": "INFO",
                    "detail": f"p50 turn {p50:.0f}ms (inline-KB mode ~20k tok/gen; pgvector target <3s)"})
    return out


def _dossier(d: dict, p: Path) -> dict:
    return {
        "log": p.name,
        "persona": d.get("persona"), "expect": d.get("expect"), "outcome": d.get("outcome"),
        "pass": d.get("pass"), "ended": d.get("ended"), "turns": d.get("turns"),
        "turns_p50_ms": d.get("p50_ms"), "wall_s": d.get("wall_s"),
        "booking": d.get("booking"), "gate_rejections": d.get("gate_rejections"),
        "blocked_transitions": d.get("blocked_transitions"),
        "final_dvs": d.get("final_dvs"), "tools_order": d.get("tools"),
        "final_state": d.get("final_state"), "rag_stats": d.get("rag_stats"),
        "transcript": [{"turn": t.get("turn"), "caller": t.get("caller"),
                        "agent": t.get("agent"), "tools": t.get("tools")}
                       for t in d.get("transcript", [])],
        "auto_checks": auto_checks(d),
    }


def cmd_list(a):
    cutoff = time.time() - a.hours * 3600
    rows = []
    for p in sorted(LOGS.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        if p.stat().st_mtime < cutoff:
            continue
        try:
            d = _load(p)
        except Exception:
            continue
        rows.append({"log": p.name, "persona": d.get("persona"), "outcome": d.get("outcome"),
                     "pass": d.get("pass"), "turns": d.get("turns"),
                     "p50_ms": d.get("p50_ms"), "gate_rej": d.get("gate_rejections")})
    print(json.dumps(rows, indent=1))
    return 0


def cmd_show(a):
    p = _find(a.substr)
    print(json.dumps(_dossier(_load(p), p), indent=1))
    return 0


def cmd_sop(a):
    p = _find(a.substr)
    d = _load(p)
    dsr = _dossier(d, p)
    dsr["sop_note"] = ("auto_checks = deterministic subset of CALL-ANALYSIS-SOP "
                       "(contamination, required dvs, tool counts, gates, booking, latency). "
                       "Prompt-following / hallucination / redundancy / flow judgment: read transcript below.")
    dsr["trace_hint"] = "join OTEL payloads: lf.py gens <tid> / lf.py tools <tid> (trace id via lf.py traces)"
    print(json.dumps(dsr, indent=1))
    bad = [c for c in dsr["auto_checks"] if c.get("severity") in ("HIGH", "MED")]
    return 1 if bad else 0


def cmd_dvs(a):
    p = _find(a.substr)
    d = _load(p)
    print(json.dumps({"log": p.name, "final_dvs": d.get("final_dvs"),
                      "auto_checks": auto_checks(d)}, indent=1))
    return 0


def cmd_turns(a):
    """Verbatim transcript slice: caller/agent text + tools per turn."""
    d = _find_d(a)
    tr = d.get("transcript", [])
    lo = a.frm if a.frm is not None else 1
    hi = a.to if a.to is not None else (tr[-1]["turn"] if tr else 0)
    hits = [t for t in tr if lo <= t["turn"] <= hi
            and (not a.tool or any(a.tool in x for x in (t.get("tools") or [])))]
    print(json.dumps({"log": _find(a.substr).name, "turns_shown": len(hits),
                      "transcript": hits}, indent=1))
    return 0


def cmd_lat(a):
    """Latency profile from per-turn turn_ms[]: p50/p90, slowest turns."""
    d = _find_d(a)
    ms = d.get("turn_ms") or []
    s = sorted(ms)
    p50 = s[len(s)//2] if s else d.get("p50_ms")
    p90 = s[int(len(s)*0.9)] if s else None
    worst = sorted(range(len(ms)), key=lambda i: -ms[i])[:5]
    print(json.dumps({
        "log": _find(a.substr).name, "turns": len(ms), "p50_ms": round(p50 or 0),
        "p90_ms": round(p90) if p90 else None,
        "slowest_turns": [{"turn": i+1, "ms": round(ms[i])} for i in worst],
        "turn_ms": [round(x) for x in ms]}, indent=1))
    return 0


def _find_d(a):
    return _load(_find(a.substr))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    l = sub.add_parser("list"); l.add_argument("--hours", type=float, default=48)
    s = sub.add_parser("show"); s.add_argument("substr")
    so = sub.add_parser("sop"); so.add_argument("substr")
    dv = sub.add_parser("dvs"); dv.add_argument("substr")
    t = sub.add_parser("turns", help="verbatim transcript slice")
    t.add_argument("substr"); t.add_argument("--frm", type=int, default=None)
    t.add_argument("--to", type=int, default=None)
    t.add_argument("--tool", default="", help="only turns whose tools contain this substring")
    la = sub.add_parser("lat", help="latency profile (p50/p90/slowest turns)")
    la.add_argument("substr")
    a = p.parse_args()
    sys.exit({"list": cmd_list, "show": cmd_show, "sop": cmd_sop, "dvs": cmd_dvs,
              "turns": cmd_turns, "lat": cmd_lat}[a.cmd](a))


if __name__ == "__main__":
    main()
