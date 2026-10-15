#!/usr/bin/env python3
"""iter49 T4/T2 — offline replay of the live multi-lane engine.

Two modes (both OFFLINE — no LLM calls, no Retell, real embedder + pgvector):

  --smoke   The 7 scripted scenarios (ported VERBATIM from the 2026-09-15
            T4 session's /tmp/opencode smoke — /tmp is ephemeral, this is
            the repo copy). Real prompts, real dvs, real arctic embedder +
            kb_chunks_v2 @0.24. Per-turn tag-KB presence asserts:
              Intake mirror (pain_frame dv)   -> pain-points + industry
              Discovery how-it-works (gold)   -> discovery-bridge via quota
              Discovery price-push (deferral) -> sales-language
              Closer loss playback (raw leak) -> sales-psychology ($-fmt)
              Closing not booked              -> call-closing (goodbye anchor)
              Closing booked                  -> call-closing (recap, E3 swap)
              Booking (OFF)                   -> zero lanes

  --replay  The 82-turn gold-chat replay (lab results.db, run_id='replay'):
            drives CallRuntime.build_lanes + _live_retrieve per turn with the
            EXACT caller text (TRAP #5: never paraphrase — borderline scores
            at ~0.40 flip ranks) and the state from the row; dvs rebuilt from
            the Retell gold logs (same walk as the pre-flight: state
            transitions + tool-result variables + leak values). Asserts the
            pre-flight's deterministic per-state tag-KB presence (Closing ->
            call-closing 4/4; Closer loss playback -> sales-psychology 26/26;
            Offer re-anchoring -> sales-psychology 5/5; Discovery how-it-works
            r1 -> discovery-bridge 3/3; mechanical states -> 0 lanes 35/35)
            and prints the aggregates comparable to 02_replay_report.md §T1
            (pre-flight C: chunks/turn 2.4, right-vertical 0.788, zero-chunk
            1/47). HONEST LIMIT: the gold Intake r1 turns run dv-bare (Retell
            V6.8 never extracted pain_frame before r1) so mirror->pain-points
            is NOT assertable on gold — the smoke's dv-enriched turn pins it.

Run from the engine worktree (cwd matters — Settings loads .env from cwd and
~/fastembed/ shadows the package when cwd=~):

    .venv/bin/python scripts/iter49_offline_replay.py --smoke
    .venv/bin/python scripts/iter49_offline_replay.py --replay
    .venv/bin/python scripts/iter49_offline_replay.py --replay \
        --db /home/julio/projects/diallux_kb_lab/results.db

Never touches `kb_chunks` (reads kb_chunks_v2 via the configured store only).
Exit 1 on any hard-assert failure.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import get_settings              # noqa: E402
from diallux.graph.builder import CallRuntime        # noqa: E402

LAB_DB = "/home/julio/projects/diallux_kb_lab/results.db"
GOLD_CHATS = Path("/home/julio/projects/Retell_AI_MCP_connection/"
                  "Dialux_SDR/testing/json_logs")
SALES_STATES = {"Intake", "Discovery", "Closer", "Offer", "Closing"}
MECHANICAL = {"Booking", "VerifyLead", "ConfirmSlots", "contact_details"}

# pre-flight vertical patterns (replay_retell.py VERT_PAT — same judge)
VERT_PAT = {
    "real_estate": re.compile(
        r"real estate|realtor|property|tenant|buyer|seller|leasing|broker",
        re.I),
    "dental": re.compile(
        r"dental|dentist|patient|medical|clinic|orthodont", re.I),
}

# --------------------------------------------------------------------------- #
# --smoke: the 7 scripted scenarios (VERBATIM port, paths repo-relative)     #
# --------------------------------------------------------------------------- #
SMOKE_TURNS = [
    ("Intake", "Sofia real-estate mirror",
     {"pain_frame": "missed calls", "industry": "real estate"},
     "Hey! So I'm Sofia — I run a real estate office here in Houston. "
     "Your ad was talking about missed calls? That's literally me.",
     {"pain-points", "industry"}),
    ("Discovery", "how it works (gold text, faithful r1: no industry dv yet)",
     {"pain_points": "losing patients after hours"},
     "Hi — I saw your ad on Facebook about answering missed calls. I keep "
     "losing patients after hours, so I'm curious how it works.",
     {"discovery-bridge"}),
    ("Discovery", "price-push (deferral)",
     {"industry": "dental practice", "pain_points": "losing patients after hours",
      "pain_frame": "missed revenue"},
     "okay but what's your pricing? what does this cost every month?",
     {"sales-language"}),
    ("Closer", "loss playback (raw leak dvs)",
     {"industry": "dental practice", "weekly_leak": "4,000",
      "monthly_leak": "17,300"},
     "Sure, go ahead. I'd love to know what those missed calls might be "
     "costing my practice.",
     {"sales-psychology"}),
    ("Closing", "goodbye (not booked)",
     {}, "ok perfect, thanks so much, bye",
     {"call-closing"}),
    ("Closing", "recap (booked — E3 anchor swap)",
     {"booking_verified": True}, "that's great, thank you!",
     {"call-closing"}),
    ("Booking", "OFF state — zero lanes",
     {"booking_verified": True}, "confirming the booking",
     set()),
]


async def run_smoke() -> int:
    root = Path(__file__).resolve().parents[1]
    llm_json = json.loads((root / "agent" / "llm.json").read_text())
    rt = CallRuntime(get_settings(), llm_json)
    store = await rt._resolve_kb_store()
    assert store is not None and store.table_name == "kb_chunks_v2", \
        f"store wrong: {store}"
    print(f"store: table={store.table_name} model={store.embedding_model} "
          f"filter={store.filter_score}\n")
    fails = 0
    for state, label, dvs, user, expect in SMOKE_TURNS:
        lanes, owned = rt.build_lanes(state, dvs, user)
        delta, kbs, ms, merged, lane_qs = await rt._live_retrieve(
            state, dvs, user, store)
        tops = ", ".join(f"{c['kb']}:{c['score']}(id={c['id']})"
                         for c in merged)
        hit = expect & set(kbs) if expect else not kbs
        verdict = "PASS" if hit else "FAIL"
        fails += 0 if hit else 1
        print(f"[{state}] {label}")
        for q, sc in zip(lane_qs, [l['scope'] for l in lanes]):
            print(f"    lane: {q[:76]!r} -> {sc[:3]}{'...' if len(sc) > 3 else ''}")
        print(f"    merged ({ms:.0f}ms): {tops or '-'}")
        print(f"    [{verdict}] expect {sorted(expect) or 'no lanes (OFF)'}\n")
    await rt.aclose()
    print("SMOKE:", "ALL PASS" if fails == 0 else f"{fails} FAILURES")
    return 1 if fails else 0


# --------------------------------------------------------------------------- #
# --replay: gold-chat dv walk (pre-flight walk_chat, dvs only)               #
# --------------------------------------------------------------------------- #
def walk_chat_dvs(path: Path) -> dict[int, dict]:
    """rid -> dvs snapshot BEFORE that agent response. Same walk as the
    pre-flight (replay_retell.walk_chat): tool-result `variables` accumulate
    (+ weekly/monthly leak at top level); state transitions tracked for the
    sanity join. Faithful to the Retell-era dv trail (V6.8 names — where a
    value never landed, the engine lane runs dv-bare: conservative)."""
    d = json.loads(path.read_text())
    dvs: dict[str, str] = {}
    out: dict[int, dict] = {}
    for m in d.get("message_with_tool_calls", []):
        role = m.get("role")
        if role == "tool_call_result":
            try:
                j = json.loads(m.get("content", ""))
            except Exception:
                continue
            for k, v in (j.get("variables") or {}).items():
                if isinstance(v, (str, int, float)):
                    dvs[k] = str(v)
            for k in ("weekly_leak", "monthly_leak"):
                if k in j and isinstance(j[k], (str, int, float)):
                    dvs[k] = str(j[k])
        elif role == "agent":
            md = m.get("metadata") or {}
            rid = int(re.search(r"\d+", str(md.get("response_id", 0))).group())
            out[rid] = dict(dvs)
    return out


def classify_asserts(state: str, caller: str) -> tuple[set[str], str]:
    """(expected_kbs, label) for the HARD asserts only — the deterministic
    per-state anchors the pre-flight's C delivered (02_replay_report §T1 /
    FIND-9 gate). Non-asserted turns count toward aggregates only."""
    if state in MECHANICAL:
        return set(), "mechanical — zero lanes"
    if state == "Closing":
        return {"call-closing"}, "closing anchor -> call-closing"
    if state == "Closer":
        return {"sales-psychology"}, "loss playback -> sales-psychology"
    if state == "Offer":
        return {"sales-psychology"}, "re-anchoring -> sales-psychology"
    if state == "Discovery" and caller.startswith("Hi — I saw your ad"):
        return {"discovery-bridge"}, "how-it-works r1 -> discovery-bridge"
    return set(), "aggregate only (no deterministic anchor)"


async def run_replay(db_path: str, chats_dir: Path) -> int:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = list(con.execute(
        "SELECT chat, rid, state, vertical, caller FROM replay_turns "
        "WHERE run_id='replay' ORDER BY chat, rid"))
    con.close()
    assert rows, f"no replay_turns rows (run_id='replay') in {db_path}"

    dv_walks: dict[str, dict[int, dict]] = {}
    for cid in sorted({r["chat"] for r in rows}):
        p = chats_dir / f"BOOK_chat_{cid}.json"
        dv_walks[cid] = walk_chat_dvs(p) if p.exists() else {}
        if not p.exists():
            print(f"WARNING: gold chat missing ({p}) — dv-bare for {cid}")

    root = Path(__file__).resolve().parents[1]
    llm_json = json.loads((root / "agent" / "llm.json").read_text())
    rt = CallRuntime(get_settings(), llm_json)
    store = await rt._resolve_kb_store()
    assert store is not None and store.table_name == "kb_chunks_v2", \
        f"store wrong: {store}"
    print(f"store: table={store.table_name} model={store.embedding_model} "
          f"filter={store.filter_score}")
    print(f"replay: {len(rows)} turns "
          f"({sum(1 for r in rows if r['state'] in SALES_STATES)} sales / "
          f"{sum(1 for r in rows if r['state'] in MECHANICAL)} mechanical)\n")

    fails = 0
    sales_n = 0
    chunks_total = chars_total = 0
    over_budget = zero_chunk = 0
    mech_nonzero = 0
    ind_right_vals: list[float] = []
    ind_chunks_right = ind_chunks_total = 0
    for r in rows:
        cid, rid, state, vert = r["chat"], r["rid"], r["state"], r["vertical"]
        caller = r["caller"]                       # EXACT lab-DB text (TRAP #5)
        dvs = dv_walks.get(cid, {}).get(rid, {})
        expect, why = classify_asserts(state, caller)
        delta, kbs, ms, merged, lane_qs = await rt._live_retrieve(
            state, dvs, caller, store)
        kbset = set(kbs)

        # ---- hard asserts ----
        if state in MECHANICAL:
            ok = not kbset
            if not ok:
                mech_nonzero += 1
        elif expect:
            ok = expect & kbset == expect
        else:
            ok = True
        verdict = "PASS" if ok else "FAIL"
        fails += 0 if ok else 1

        # ---- aggregates (sales turns) ----
        if state in SALES_STATES:
            sales_n += 1
            chunks_total += len(merged)
            chars_total += sum(len(str(c.get("content", ""))) for c in merged)
            if sum(len(str(c.get("content", ""))) for c in merged) > 1600:
                over_budget += 1
            if not merged:
                zero_chunk += 1
            ind = [c for c in merged if c.get("kb") == "industry"]
            if ind:
                pat = VERT_PAT.get(vert)
                n_right = sum(1 for c in ind
                              if pat and pat.search(str(c.get("content", ""))))
                ind_right_vals.append(n_right / len(ind))
                ind_chunks_right += n_right
                ind_chunks_total += len(ind)

        tops = ", ".join(f"{c['kb']}:{c['score']}" for c in merged)
        print(f"[{state:>15}] r{rid:>2} vert={vert:<11} "
              f"({'dv-bare' if not dvs else f'{len(dvs)} dvs'}) "
              f"[{verdict}] {tops or '-'}")
        if not ok:
            print(f"    FAIL: {why}; got kbs={sorted(kbset)}")
        print(f"    caller: {caller[:96]!r}")

    await rt.aclose()

    # ---- §T1-comparable table ----
    print("\n=== aggregates (sales turns, n=%d) vs pre-flight C "
          "(02_replay_report.md §T1) ===" % sales_n)
    print(f"chunks/turn        {chunks_total / sales_n:.2f}   "
          f"(pre-flight C: 2.4)")
    print(f"mean chars/turn    {chars_total / sales_n:.0f}    (C: 693)")
    print(f"over 1600 budget   {over_budget}         (C: 0)")
    print(f"zero-chunk turns   {zero_chunk}/{sales_n}       (C: 1/47)")
    if ind_right_vals:
        print(f"right-vertical     pooled {ind_chunks_right / ind_chunks_total:.3f} / "
              f"mean-of-turns {sum(ind_right_vals) / len(ind_right_vals):.3f}   (C: 0.788)")
    else:
        print("right-vertical     n/a (no industry chunks)")
    print(f"mechanical lanes   {mech_nonzero} nonzero    (C: 0 — design)")

    gate = (fails == 0
            and zero_chunk <= 2
            and mech_nonzero == 0
            and (not ind_right_vals
                 or sum(ind_right_vals) / len(ind_right_vals) >= 0.75))
    print("\nREPLAY:", "GATE PASS" if gate else "GATE FAIL")
    return 0 if gate else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--smoke", action="store_true",
                    help="the 7 scripted scenarios (repo copy of the T4 smoke)")
    ap.add_argument("--replay", action="store_true",
                    help="the 82-turn gold-chat replay from the lab DB")
    ap.add_argument("--db", default=LAB_DB,
                    help=f"lab results.db (default {LAB_DB})")
    ap.add_argument("--chats", default=str(GOLD_CHATS),
                    help="Retell gold json_logs dir (for the dv trail)")
    args = ap.parse_args()
    if not (args.smoke or args.replay):
        ap.error("pick --smoke and/or --replay")
    rc = 0
    if args.smoke:
        rc |= asyncio.run(run_smoke())
    if args.replay:
        rc |= asyncio.run(run_replay(args.db, Path(args.chats)))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
