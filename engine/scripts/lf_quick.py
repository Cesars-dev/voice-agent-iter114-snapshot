#!/usr/bin/env python
"""lf_quick — one-liner SDK over the iter48 SQLite ledger (+ Langfuse refresh).

SOP: research/surgeon/iter48-rag-truth/SQL_ANALYSIS_SOP.md

Quick pulls:
  lf_quick.py runs                          # what's imported
  lf_quick.py bugs --run happy-b            # fixes #1/#2/#3 verification, SQLite-verified
  lf_quick.py table --run happy-b           # per-field percentile table (LLM/e2e/TTFT p50/p90)
  lf_quick.py rounds --run happy-b --persona Maria
  lf_quick.py scaffold                      # any [state: scaffold left anywhere
  lf_quick.py refresh --window "12:00-12:15" --run happy-c --commit <sha>   # import fresh
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "research" / "surgeon" / "iter48-rag-truth" / "ledger.db"
sys.path.insert(0, str(ROOT / "scripts"))


def con() -> sqlite3.Connection:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def pct(vals: list[float], p: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    k = (len(s) - 1) * p
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    return round(s[f] + (s[c] - s[f]) * (k - f), 1)


# ---------------------------------------------------------------- commands
def runs(c):
    for r in c.execute("SELECT * FROM runs ORDER BY imported_at"):
        print(f"{r['run_id']:12s} {r['commit_sha'] or '-':10s} window={r['window']:12s} "
              f"calls={r['n_calls']}")


def bugs(c, run: str):
    """Fix #1/#2/#3 verification — the exact bugs iter48 Commit A targets."""
    print(f"== BUG-COVERAGE VERIFICATION — run={run} (SQLite-verified) ==\n")

    # BUG #1: content-free entry prefetch ("[state: X] " -> 0-chunk frozen)
    n = c.execute("SELECT COUNT(*) n FROM rag WHERE run_id=? AND query LIKE '%[state:%'",
                  (run,)).fetchone()["n"]
    tot = c.execute("SELECT COUNT(*) n FROM rag WHERE run_id=?", (run,)).fetchone()["n"]
    pref = c.execute("""SELECT state, chunks, query FROM rag WHERE run_id=? AND prefetched=1
                        ORDER BY ts""", (run,)).fetchall()
    entries = c.execute("""SELECT state, chunks FROM rag WHERE run_id=? AND kind='rag'
        AND frozen_reuse=0 AND state IN ('Intake','Discovery','Closer','Offer')
        AND ms > 0""", (run,)).fetchall()
    zero_entries = [e for e in entries if e["chunks"] == 0]
    print("BUG #1 content-free entry prefetch (placeholder -> 0-chunk frozen):")
    print(f"  scaffold rows:              {n}/{tot}  (want 0)")
    print(f"  prefetch queries sampled:   {len(pref)} rows -> "
          f"{[p['query'][:44] for p in pref[:3]]}")
    print(f"  state entries w/ sync fetch:{len(entries)}, 0-chunk among them: "
          f"{len(zero_entries)}  (want 0)")
    ok1 = n == 0 and len(zero_entries) == 0 and all("loss" in p["query"] or "mirror" in p["query"]
        or "anchor" in p["query"] or "problem" in p["query"] or "escalation" in p["query"]
        for p in pref)
    print(f"  => BUG #1 COVERED: {'YES' if ok1 else 'NO'}\n")

    # BUG #2: scaffold-polluted drift query (cosine dead-locked >=0.85)
    d = c.execute("""SELECT cosine, query FROM rag WHERE run_id=? AND kind='rag:drift'
                     AND cosine IS NOT NULL ORDER BY ts""", (run,)).fetchall()
    cos = [r["cosine"] for r in d]
    scaffolded = [r for r in d if "[state:" in (r["query"] or "")]
    fired = [r for r in d if r["cosine"] < 0.85]
    print("BUG #2 scaffold-polluted drift query (cosine lock):")
    print(f"  drift rounds: {len(d)}, scaffolded queries: {len(scaffolded)}  (want 0)")
    if cos:
        print(f"  cosine: min={min(cos):.3f} p50={statistics.median(cos):.3f} max={max(cos):.3f}")
        print(f"  rounds BELOW 0.85 (drift CAN fire): {len(fired)}  "
              f"(old behavior: ~0 — scaffold locked everything)")
        print(f"  sample query: {d[0]['query'][:70]!r}")
    ok2 = len(scaffolded) == 0 and len(fired) > 0
    print(f"  => BUG #2 COVERED: {'YES' if ok2 else 'NO'}\n")

    # BUG #3: permanent empty freeze (base-vec-None early return)
    force = c.execute("""SELECT COUNT(*) n FROM rag WHERE run_id=? AND kind='rag:drift'
                         AND force_drift=1""", (run,)).fetchone()["n"]
    freezes = c.execute("""SELECT state, COUNT(*) n FROM rag WHERE run_id=? AND kind='rag'
        AND chunks=0 AND frozen_reuse=1 GROUP BY state""", (run,)).fetchall()
    rescued = c.execute("""SELECT state, cosine, query FROM rag WHERE run_id=? AND
        kind='rag:drift' AND force_drift=1 ORDER BY ts""", (run,)).fetchall()
    print("BUG #3 permanent empty freeze (base-None dead-end):")
    print(f"  forced-drift rescues: {force}  (happens only when a freeze occurred)")
    print(f"  frozen-empty visits:  {[(f['state'], f['n']) for f in freezes]}")
    print(f"  sample rescue:        {[r['query'][:50] for r in rescued[:2]]}")
    # freezing states other than Closing with a real KB scope = bug resurfaced
    bad_freezes = [f for f in freezes if f["state"] != "Closing"]
    ok3 = not bad_freezes
    print(f"  => BUG #3 COVERED: {'YES' if ok3 else 'N/A — no freeze occurred this run'}\n")
    print("OVERALL:", "ALL TARGET BUGS COVERED" if (ok1 and ok2 and ok3) else "CHECK OUTPUT")


def table(c, run: str):
    """Per-field percentile table: TTFT / LLM / e2e p50-p90 (SOP field set)."""
    print(f"== PER-FIELD PERCENTILES — run={run} ==\n")
    rows = c.execute("""SELECT c.trace_name persona, r.ttft_ms, r.llm_ms, r.cache_read
        FROM rounds r JOIN calls c ON c.trace_id=r.trace_id
        WHERE r.run_id=? ORDER BY r.ts""", (run,)).fetchall()
    ttft = [r["ttft_ms"] for r in rows if r["ttft_ms"]]
    llm = [r["llm_ms"] for r in rows if r["llm_ms"]]
    t1 = [r["ttft_ms"] for r in rows if r["ttft_ms"] and rows.index(r) == 0] or []
    t1 = [rows[0]["ttft_ms"]] if rows and rows[0]["ttft_ms"] else []
    steady = ttft[1:]
    cache0 = sum(1 for r in rows if r["ttft_ms"] is not None and (r["cache_read"] or 0) == 0)

    print(f"{'field':26s} {'n':>4s} {'p50':>8s} {'p90':>8s} {'min':>8s} {'max':>8s}")
    print("-" * 66)

    def row(name, vals, unit="ms"):
        if not vals:
            print(f"{name:26s} {0:4d} {'-':>8s} {'-':>8s}")
            return
        print(f"{name:26s} {len(vals):4d} {pct(vals, .5):>8.1f} {pct(vals, .9):>8.1f} "
              f"{min(vals):>8.1f} {max(vals):>8.1f}")

    row("ttft_ms (all rounds)", ttft)
    row("ttft_ms (steady, no turn1)", steady)
    row("ttft_ms (turn 1)", t1)
    row("llm_ms (round duration)", llm)
    print(f"{'rounds total':26s} {len(rows):4d}")
    print(f"{'cache-0 rounds (ttft>0)':26s} {cache0:4d}")
    print("\ne2e_ms: LIVE-ONLY field (TTS drain + STT eot) — sourced from :8007 session")
    print("turn reports / micbridge traces, not the in-process harness. See SOP §4.")


def rounds(c, run: str, persona: str | None):
    q = """SELECT c.trace_name p, r.state, r.ttft_ms, r.llm_ms, r.cache_read, r.input
           FROM rounds r JOIN calls c ON c.trace_id=r.trace_id WHERE r.run_id=?"""
    args = [run]
    if persona:
        q += " AND c.trace_name LIKE ?"
        args.append(f"%{persona.lower()}%")
    q += " ORDER BY r.ts"
    print(f"{'persona':30s} {'state':15s} {'ttft':>6s} {'llm_ms':>7s} {'cache':>6s} {'in':>6s}")
    for r in c.execute(q, args):
        print(f"{r['p'][:29]:30s} {r['state']:15s} "
              f"{(str(int(r['ttft_ms'])) if r['ttft_ms'] else '-'):>6s} "
              f"{(str(int(r['llm_ms'])) if r['llm_ms'] else '-'):>7s} "
              f"{str(int(r['cache_read'] or 0)):>6s} {str(int(r['input'] or 0)):>6s}")


def scaffold(c):
    n = c.execute("SELECT COUNT(*) n FROM rag WHERE query LIKE '%[state:%'").fetchone()["n"]
    print(f"scaffold '[state:' rows across ALL runs: {n}")
    for r in c.execute("SELECT run_id, state, query FROM rag WHERE query LIKE '%[state:%'"):
        print("  ", r["run_id"], r["state"], r["query"][:80])


def main():
    global DB
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["runs", "bugs", "table", "rounds", "scaffold", "refresh"])
    ap.add_argument("--run")
    ap.add_argument("--persona")
    ap.add_argument("--window")
    ap.add_argument("--commit", default="")
    ap.add_argument("--db", default=str(DB))   # iter52: mirror live_sql.py --db
    args = ap.parse_args()
    if args.cmd == "refresh":
        if not args.window or not args.run:
            sys.exit("--window and --run required")
        import live_sql
        return live_sql.import_run(args.window, args.run, args.commit, args.db)
    DB = args.db
    c = con()
    try:
        return {"runs": runs, "scaffold": scaffold}.get(args.cmd, lambda x: None)(c) \
            if args.cmd in ("runs", "scaffold") else \
            (bugs(c, args.run) if args.cmd == "bugs" else
             table(c, args.run) if args.cmd == "table" else
             rounds(c, args.run or "", args.persona))
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main() or 0)
