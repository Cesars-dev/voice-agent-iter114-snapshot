#!/usr/bin/env python3
"""call_forensics.py — one command for the full call-forensics matrix.

iter86 (owner-ordered SDK; stdlib only: urllib+json+sqlite3+argparse).
Replaces the manual per-session forensics (research/surgeon/iter83-t4-wave1/
05_b3_latency_forensics.md). Reads the SAME sources used by hand: the
Langfuse trace (turn reports + GENERATION rounds + eager/warm/await spans),
the uvicorn journal text (round-usage ttft/cache + warm-done ms + await_warm
outcomes), and the SQLite call ledger (runs/calls).

Env: engine/.next to the script (LANGFUSE_HOST/KEYS) — same pattern as lf.py.

Modes
-----
  --trace <langfuse_trace_id>          per-turn full matrix from Langfuse
                                       (works for micbridge AND diallux-call)
  --journal <journal.txt>              join graph-log lines (round usage ttft/
                                       cache, warm done, await_warm, eager
                                       fires) into the matrix; needs --trace
  --run <ledger_run_id>                ledger runs/calls rows for a run
  --compare <trace_a> <trace_b>        harness-vs-prod fidelity table
                                       (turns/min, inter-turn gaps, barge
                                       rate, user-perceived)
  --costs [--hours N] [--name SUB]     blended token/cost estimate (lf.py
                                       price table is PLACEHOLDER — flagged)

Trace id may be a PREFIX (resolved via a traces search in the recent window).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sqlite3
import sys
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# ----------------------------------------------------------------- env/langfuse
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


def _get(path: str, timeout: int = 45) -> dict:
    host, pk, sk = _env()
    req = urllib.request.Request(
        host + path,
        headers={"Authorization": "Basic " + base64.b64encode(f"{pk}:{sk}".encode()).decode()},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def resolve_trace(prefix: str) -> str:
    try:
        d = _get(f"/api/public/traces/{prefix}?detailed=true")
        if d.get("data") is not None or "observations" in d:
            return d
    except Exception:
        pass
    return None


def fetch_trace(tid_or_prefix: str) -> tuple[str, list[dict]]:
    """Return (full_id, observations). Accepts an id prefix."""
    if len(tid_or_prefix) >= 30:
        for p in (f"/api/public/traces/{tid_or_prefix}?detailed=true",):
            try:
                d = _get(p)
                return d["id"], d.get("observations", [])
            except Exception:
                pass
    # prefix search: last 14 days
    frm = (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    page = 1
    while page <= 50:
        d = _get(f"/api/public/traces?fromTimestamp={frm}&limit=100&page={page}")
        data = d.get("data", [])
        if not data:
            break
        for t in data:
            if t["id"] == tid_or_prefix or t["id"].startswith(tid_or_prefix):
                full = _get(f"/api/public/traces/{t['id']}?detailed=true")
                return full["id"], full.get("observations", [])
        if len(data) < 100:
            break
        page += 1
    raise SystemExit(f"trace not found: {tid_or_prefix}")


# ----------------------------------------------------------------- journal join
ROUND_RE = re.compile(
    r"round usage: turn=(\d+) state=(\S+) input=(\d+) cache_read=(\d+) output=(\d+)"
    r" ttft=(\S+)ms state_in_delta=(\w+) delta_tokens=(\d+)")
WARM_RE = re.compile(r"warm done key=(\S+) shape=(\S+) outcome=(\S+) degraded=(\S+) ms=(\d+)")
AWAIT_RE = re.compile(r"await_warm key=(\S+) outcome=(\S+) waited_ms=(\d+) cap_ms=(\d+)")
EAGER_RE = re.compile(r"eager turn started \(speculative\)")
FINAL_RE = re.compile(r"turn (\d+) final report: (\{.*\})")
REPORT_RE = re.compile(r"turn (\d+) report: (\{.*\})")


def parse_journal(path: str) -> list[dict]:
    """Parse journal text (journalctl -o short-iso) into ordered events with
    timestamps: [{ts, kind, fields...}]."""
    events = []
    ts_re = re.compile(r"^(\S+T\S+)\S* \S+")
    for line in open(path, encoding="utf-8", errors="replace"):
        m_ts = ts_re.match(line)
        ts = m_ts.group(1) if m_ts else None
        for kind, rx, xform in (
            ("round", ROUND_RE, lambda m: {"turn": int(m.group(1)), "state": m.group(2),
                                           "input": int(m.group(3)), "cache_read": int(m.group(4)),
                                           "output": int(m.group(5)),
                                           "ttft_ms": None if m.group(6) == "None" else
                                           float(m.group(6)),
                                           "state_in_delta": m.group(7) == "True",
                                           "delta_tokens": int(m.group(8))}),
            ("warm", WARM_RE, lambda m: {"key": m.group(1), "shape": m.group(2),
                                         "outcome": m.group(3), "degraded": m.group(4),
                                         "ms": int(m.group(5))}),
            ("await", AWAIT_RE, lambda m: {"key": m.group(1), "outcome": m.group(2),
                                           "waited_ms": int(m.group(3)), "cap_ms": int(m.group(4))}),
            ("eager_start", EAGER_RE, lambda m: {}),
        ):
            m = rx.search(line)
            if m:
                ev = {"ts": ts, "kind": kind}
                ev.update(xform(m))
                events.append(ev)
                break
        else:
            m = FINAL_RE.search(line) or REPORT_RE.search(line)
            if m:
                try:
                    events.append({"ts": ts, "kind": "report", "turn": int(m.group(1)),
                                   **json.loads(m.group(2))})
                except Exception:
                    pass
    return events


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ----------------------------------------------------------------- trace model
class CallData:
    def __init__(self, trace_id: str, obs: list[dict], journal: list[dict] | None = None):
        self.trace_id = trace_id
        self.obs = obs
        self.journal = journal or []
        gens = [o for o in obs if o.get("type") == "GENERATION"]
        gens.sort(key=lambda o: o["startTime"])
        self.generations = []
        for g in gens:
            name = g.get("name") or ""
            state = name[4:] if name.startswith("llm:") else name
            u = g.get("usage") or {}
            usage = u if isinstance(u, dict) else {}
            det = usage.get("inputTokenDetails") or usage.get("input_token_details") or {}
            cache = det.get("cache_read") if isinstance(det, dict) else 0
            self.generations.append({
                "id": g.get("id"), "state": state, "start": g["startTime"], "end": g["endTime"],
                "latency_ms": None if not (g.get("startTime") and g.get("endTime")) else
                round((parse_ts(g["endTime"]) - parse_ts(g["startTime"])).total_seconds() * 1000),
                "input": usage.get("input") or (usage.get("input") if isinstance(usage, dict) else 0),
                "output": usage.get("output") if isinstance(usage, dict) else 0,
                "cache_read": cache or 0,
                "model": g.get("model"),
            })
        # turn reports: prefer :final spans, fall back to turn:N spans
        self.turns: dict[int, dict] = {}
        for o in sorted(obs, key=lambda x: x["startTime"]):
            name = o.get("name") or ""
            if not name.startswith("turn:"):
                continue
            if name.endswith(":final"):
                idx = int(name.split(":")[1])
                out = o.get("output") or {}
                if isinstance(out, dict):
                    self.turns[idx] = {**self.turns.get(idx, {}), **out}
            else:
                out = o.get("output") or {}
                if isinstance(out, dict):
                    self.turns[int(name.split(":")[1])] = {**out, **self.turns.get(int(name.split(":")[1]), {})}
        self.spans = {}
        for o in obs:
            if o.get("type") == "SPAN":
                key = o.get("name") or ""
                self.spans.setdefault(key.split(":")[0] if not key.startswith("turn:") else key, []) \
                    .append(o) if False else None
        self.named = {}
        for o in obs:
            if o.get("type") == "SPAN":
                self.named.setdefault(o.get("name") or "", []).append(o)
        for k in list(self.named):
            self.named[k].sort(key=lambda o: o["startTime"])
        self.trace_start = min((o["startTime"] for o in obs), default=None)
        self.trace_end = max((o["endTime"] for o in obs if o.get("endTime")), default=None)

    def duration_s(self) -> float:
        if not self.trace_start or not self.trace_end:
            return 0.0
        return round((parse_ts(self.trace_end) - parse_ts(self.trace_start)).total_seconds(), 1)

    def user_perceived(self, rep: dict) -> float | None:
        """Confirmed-EOT → audio-out = S2L − head_start + L2T + T2A (ms)."""
        s2l = rep.get("stt_eot_to_llm_first_ms")
        l2t = rep.get("llm_first_to_tts_first_ms")
        t2a = rep.get("tts_first_to_audio_out_ms")
        hs = rep.get("head_start_ms") or 0
        if None in (s2l, l2t, t2a):
            return None
        return round(s2l - hs + l2t + t2a, 1)


def pctl(vals: list[float], p: float) -> float:
    if not vals:
        return float("nan")
    vals = sorted(vals)
    k = max(0, min(len(vals) - 1, int(round((len(vals) - 1) * p / 100))))
    return round(vals[k], 1)


# ----------------------------------------------------------------- rendering
def render_trace(cd: CallData, journal: list[dict] | None) -> str:
    o = []
    o.append(f"trace {cd.trace_id}  obs={len(cd.obs)}  gens={len(cd.generations)} "
             f"turn_reports={len(cd.turns)}  span={cd.duration_s()}s")
    if journal:
        km = Counter(ev['kind'] for ev in journal)
        o.append(f"journal events: {dict(km)}")
    o.append("")
    o.append("== per-turn matrix (report + user-perceived) ==")
    o.append("turn | state | round ttft(real) | in/cache/out | S2L(eager-anchor) | L2T | T2A | E2Eresp | HS | uperm(conf-EOT) | barge | eager-match | resumed")
    uperm_all, s2l_all, e2e_all = [], [], []
    turn_rows = []
    for idx in sorted(cd.turns):
        rep = cd.turns[idx]
        # rounds for this turn: journal first
        jr = [ev for ev in (journal or []) if ev["kind"] == "round" and ev["turn"] == idx]
        ttfts = [ev["ttft_ms"] for ev in jr if ev["ttft_ms"] is not None]
        states = ",".join(dict.fromkeys(ev["state"] for ev in jr)) or "?"
        tok = jr[-1] if jr else None
        up = cd.user_perceived(rep)
        row = {"turn": idx, "states": states,
               "ttft": ttfts[0] if len(ttfts) == 1 and rep.get("eager") and False else
               (ttfts[-1] if ttfts else None),
               "killed": sum(1 for t in ttfts[:0]),
               "n_rounds": len(jr),
               "in/c/out": f"{tok['input']}/{tok['cache_read']}/{tok['output']}" if tok else None,
               "S2L": rep.get("stt_eot_to_llm_first_ms"),
               "L2T": rep.get("llm_first_to_tts_first_ms"),
               "T2A": rep.get("tts_first_to_audio_out_ms"),
               "E2E": rep.get("e2e_response_ms"),
               "HS": rep.get("head_start_ms"),
               "uperm": up,
               "barge": rep.get("barge_in"),
               "match": rep.get("eager_final_match"),
               "resumed": rep.get("resumed_count")}
        turn_rows.append(row)
        if rep.get("stt_eot_to_llm_first_ms") is not None:
            s2l_all.append(rep["stt_eot_to_llm_first_ms"])
        if rep.get("e2e_response_ms") is not None:
            e2e_all.append(rep["e2e_response_ms"])
        if up is not None:
            uperm_all.append(up)
    hdr = f"{'turn':>4} | {'state':16} | {'ttft':>7} | {'in/c/out':>16} | {'S2L':>7} | {'L2T':>6} | {'T2A':>5} | {'E2Eres':>7} | {'HS':>5} | {'uperm':>7} | {'barge':>5} | {'match':>5} | {'res':>3}"
    o.append(hdr)
    for r in turn_rows:
        o.append(f"{r['turn']:>4} | {r['states'][:16]:16} | "
                 f"{(('%.0f' % r['ttft']) if r['ttft'] is not None else '-'):>7} | "
                 f"{r['in/c/out'] or '-':>16} | "
                 f"{(('%.0f' % r['S2L']) if r['S2L'] is not None else '-'):>7} | "
                 f"{(('%.0f' % r['L2T']) if r['L2T'] is not None else '-'):>6} | "
                 f"{(('%.1f' % r['T2A']) if r['T2A'] is not None else '-'):>5} | "
                 f"{(('%.0f' % r['E2E']) if r['E2E'] is not None else '-'):>7} | "
                 f"{(('%.0f' % r['HS']) if r['HS'] is not None else '-'):>5} | "
                 f"{(('%.0f' % r['uperm']) if r['uperm'] is not None else '-'):>7} | "
                 f"{(str(r['barge'])[:5] if r['barge'] is not None else '-'):>5} | "
                 f"{(str(r['match'])[:5] if r['match'] is not None else '-'):>5} | "
                 f"{(str(r['res'] or '-') if 'res' in r else '-'):>3}")
    if turn_rows:
        ttfts = [r["ttft"] for r in turn_rows if r["ttft"] is not None]
        o.append("")
        o.append(f"round-ttft (journal) n={len(ttfts)} p50={pctl(ttfts, 50)} p90={pctl(ttfts, 90)}" if ttfts
                 else "round-ttft: no journal join (--journal)")
        o.append(f"S2L(eager-anchor) n={len(s2l_all)} p50={pctl(s2l_all, 50)} p90={pctl(s2l_all, 90)}")
        o.append(f"E2Eresponse       n={len(e2e_all)} p50={pctl(e2e_all, 50)} p90={pctl(e2e_all, 90)}")
        o.append(f"user-perceived (conf-EOT→audio-out) n={len(uperm_all)} p50={pctl(uperm_all, 50)} "
                 f"p90={pctl(uperm_all, 90)} max={max(uperm_all) if uperm_all else '-'}")
    # eager + warm + await spans
    nth = len(cd.named.get("eager_hold:fire", []))
    nrel = len(cd.named.get("eager_hold:release", []))
    nfs = len(cd.named.get("eager_hold:false_start", []))
    o.append(f"\neager fires={nth} releases={nrel} false_starts={nfs}  "
             f"await_warm spans={len(cd.named.get('await_warm', []))}")
    waits = [sp["metadata"].get("waited_ms", 0) for sp in cd.named.get("await_warm", [])
             if isinstance(sp.get("metadata"), dict)]
    outc = Counter((sp.get("metadata") or {}).get("outcome") for sp in cd.named.get("await_warm", []))
    o.append(f"await_warm outcomes={dict(Counter((sp.get('metadata') or {}).get('outcome') for sp in cd.named.get('await_warm', [])))}"
             f" total waited={sum(waits)}ms")
    warm_names = {k: len(v) for k, v in cd.named.items() if k.startswith("warm:")}
    o.append(f"warm spans: {warm_names}")
    # round usage cache_miss table (journal)
    if journal:
        cache0 = [ev for ev in journal if ev["kind"] == "round" and ev["cache_read"] == 0
                  and ev["input"] > 1000]
        o.append(f"\nrounds with cache_read=0 (cold, input>1000): {len(cache0)}")
        for ev in cache0:
            o.append(f"  {ev['ts']} turn={ev['turn']} state={ev['state']} input={ev['input']} "
                     f"ttft={ev['ttft_ms']}")
        o.append("warm done events:")
        for ev in journal:
            if ev["kind"] == "warm":
                o.append(f"  {ev['ts'].split('T')[1][:12]} key={ev['key']} shape={ev['shape']} "
                         f"outcome={ev['outcome']} ms={ev['ms']}")
        o.append("await_warm events:")
        for ev in journal:
            if ev["kind"] == "await":
                o.append(f"  {ev['ts'].split('T')[1][:12]} key={ev['key']} outcome={ev['outcome']} "
                         f"waited_ms={ev['waited_ms']} cap={ev['cap_ms']}")
    return "\n".join(o)


def trace_stats(cd: CallData) -> dict:
    durs = cd.duration_s()
    turnreports = [rep for rep in cd.turns.values()]
    ups = [cd.user_perceived(rep) for rep in turnreports]
    ups = [u for u in ups if u is not None]
    ss = [parse_ts(g["start"]) for g in cd.generations if g.get("start")]
    ss.sort()
    gaps = [(ss[i+1] - ss[i]).total_seconds() for i in range(len(ss)-1)]
    barge = [rep.get("barge_in") for rep in turnreports if rep.get("barge_in") is not None]
    return {
        "gens": len(cd.generations), "turn_reports": len(cd.turns),
        "dur_s": durs, "turns_per_min": round(len(turnreports) / (durs / 60.0), 2) if durs else 0,
        "gap_p50": pctl(gaps, 50), "gap_p90": pctl(gaps, 90),
        "uperm_n": len(ups), "uperm_p50": pctl(ups, 50), "uperm_p90": pctl(ups, 90),
        "barge_rate": round(sum(1 for b in barge if b) / len(barge), 2) if barge else 0,
    }


def render_compare(a: CallData, b: CallData, la: str, lb: str) -> str:
    sa, sb = trace_stats(a), trace_stats(b)

    def key_row(k, fmt="{:>10}"):
        va, vb = sa.get(k), sb.get(k)
        return f"{k:>18} | {fmt.format(va) if va is not None else '-':>10} | {fmt.format(vb) if vb is not None else '-':>10}"
    o = [f"== compare {la} vs {lb} ==",
         f"{'metric':>18} | {'A':>10} | {'B':>10}",
         key_row("gens"), key_row("turn_reports"), key_row("dur_s"), key_row("turns_per_min"),
         key_row("gap_p50"), key_row("gap_p90"),
         key_row("uperm_n"), key_row("uperm_p50"), key_row("uperm_p90"), key_row("barge_rate"),
         "",
         "note: A trace", la, "      B trace", lb,
         "gap = inter-GENERATION-start delta (LLM call starts; includes agent speak time + caller wait;",
         "a coarse caller-pace proxy — see report caveats)"]
    return "\n".join(o)


def render_costs(hours: float, name: str | None) -> str:
    frm = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    page, models = 1, {}
    while page <= 20:
        d = _get(f"/api/public/traces?fromTimestamp={frm}&limit=100&page={page}")
        data = d.get("data", [])
        if not data:
            break
        for t in data:
            if name and name not in (t.get("name") or ""):
                continue
            try:
                full = _get(f"/api/public/traces/{t['id']}?detailed=true")
            except Exception:
                continue
            for g in full.get("observations", []):
                if g.get("type") != "GENERATION":
                    continue
                u = g.get("usage") or {}
                tod = (g.get("model") or "?"), u.get("input") or 0, u.get("output") or 0
                if isinstance(u, dict):
                    key, it, ot = tod
                    k = models.setdefault(key, [0, 0, 0])
                    k[0] += 1; k[1] += it; k[2] += ot
        if len(data) < 100:
            break
        page += 1
    o = [f"== costs last {hours}h ({'name~' + name if name else 'all traces'}) ==",
         "PRICE TABLE = lf.py PLACEHOLDER blend — verify before quoting:",
         "  gpt-5.x-ish in 1.25/M out 10/M, gpt-4.1-mini 0.40/1.60, text-embed ~0.02/M"]
    prices = {"gpt-5": (1.25, 10.0), "gpt-4.1-mini": (0.40, 1.60), "text-embed": (0.02, 0.02)}
    tot = 0.0
    for m, (n, it, ot) in sorted(models.items(), key=lambda x: -x[1][1]):
        pin, pout = None, None
        for pk, (pi, po) in prices.items():
            if m.startswith(pk) or pk.split("-")[0] in m or m.split("-")[:1] == pk.split("-")[:1]:
                pin, pout = pi, po
                break
        if pin is None:
            pin, pout = 1.0, 2.0
        cost = it * pin / 1e6 + ot * pout / 1e6
        tot += cost
        o.append(f"  {m:40} gens={n:5} in={it:>9,} out={ot:>8,} est=${cost:,.2f}")
    o.append(f"  TOTAL est ${tot:,.2f}")
    return "\n".join(o)


def render_run(run_id: str, ledger: str) -> str:
    con = sqlite3.connect(ledger)
    con.row_factory = sqlite3.Row
    o = [f"== ledger run {run_id} =="]
    for tab in ("runs", "calls", "rounds", "findings"):
        try:
            rows = con.execute(f"SELECT * FROM {tab} LIMIT 1").fetchall()
        except sqlite3.Error as e:
            o.append(f"  table {tab}: {e}")
            continue
        if not rows:
            o.append(f"  table {tab}: empty")
            continue
    for tab, sql in (
            ("runs", "SELECT * FROM runs WHERE run_id=?"),
            ("calls", "SELECT * FROM calls WHERE run_id=? ORDER BY trace_id"),
            ("findings", "SELECT * FROM findings WHERE run_id=? ORDER BY rowid")):
        try:
            rows = [dict(r) for r in con.execute(sql, (run_id,)).fetchall()]
        except sqlite3.Error:
            rows = []
        o.append(f"-- {tab} ({len(rows)}) --")
        for r in rows[:50]:
            o.append(json.dumps(r, default=str)[:600])
    return "\n".join(o)


# ----------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace")
    ap.add_argument("--journal")
    ap.add_argument("--run")
    ap.add_argument("--ledger", default="/home/julio/projects/clean_diallux_SDR/research/surgeon/iter48-rag-truth/ledger.db")
    ap.add_argument("--compare", nargs=2, metavar=("TRACE_A", "TRACE_B"))
    ap.add_argument("--label-a", default="A")
    ap.add_argument("--label-b", default="B")
    ap.add_argument("--costs", action="store_true")
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--name")
    ap.add_argument("--out")
    args = ap.parse_args()

    outs = []
    if args.compare:
        histories = []
        for label, tid in ((args.label_a, args.compare[0]), (args.label_b, args.compare[1])):
            full, obs = fetch_trace(tid)
            histories.append((CallData(full, obs), full))
        cd, full = histories[0][0], histories[0][1]
        cd2, full2 = histories[1][0], histories[1][1]
        outs.append(render_compare(cd, cd2, full, full2))
    if args.run:
        outs.append(render_run(args.run, args.ledger))
    if args.costs:
        outs.append(render_costs(args.hours, args.name))
    if args.trace:
        full, obs = fetch_trace(args.trace)
        journal = parse_journal(args.journal) if args.journal else None
        cd = CallData(full, obs, journal)
        outs.append(render_trace(cd, journal))

    txt = "\n\n".join(outs) + "\n"
    if args.out:
        Path(args.out).write_text(txt)
        print(f"written {args.out} ({len(txt)} bytes)")
    else:
        print(txt)


if __name__ == "__main__":
    main()
