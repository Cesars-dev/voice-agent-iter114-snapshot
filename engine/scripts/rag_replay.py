"""iter63 RAG-truth digest v2 — pin-aware. Source of truth: Langfuse rag spans
(full outputs incl pinned/kbs/lanes) + json_log transcripts + deterministic
re-run of the store retrieval (pin-excluded scopes) + store.pinned_by_tag."""
import asyncio, glob, json, os, re, sqlite3, sys
sys.path.insert(0, ".")
sys.path.insert(0, "scripts")
from diallux.config import Settings
from diallux.graph.builder import CallRuntime, ragmod
from tests.mock_webhooks import mock_client
import lf

LEDGER = "/home/julio/projects/clean_diallux_SDR/research/surgeon/iter48-rag-truth/ledger.db"
import argparse
_ap = argparse.ArgumentParser()
_ap.add_argument("--run", required=True)
_ap.add_argument("--db", default=LEDGER)
_ap.add_argument("--out", default="/tmp/opencode/rag_replay.jsonl")
_args = _ap.parse_args()
RUN = _args.run
LEDGER = _args.db
OUT = _args.out
LLM_JSON = json.loads(open("agent/llm.json").read())


def slug_of(name: str) -> str:
    return "".join(ch for ch in name.split("—")[0] if ch.isalnum()).lower()


async def main():
    rt = CallRuntime(Settings(), LLM_JSON, tracer=None, llm=None,
                     http_client=mock_client(), kb_store=...)
    store = await rt._resolve_kb_store()
    con = sqlite3.connect(LEDGER)
    con.row_factory = sqlite3.Row
    calls = con.execute(
        "SELECT trace_id, trace_name, ts FROM calls WHERE run_id=? ORDER BY ts",
        (RUN,)).fetchall()
    files = sorted(glob.glob("tests/llm2llm/json_logs/*.json"),
                   key=os.path.getmtime)[-6:]
    by_name = {}
    for c in calls:
        by_name.setdefault(c["trace_name"], []).append(c)
    fh = open(OUT, "w")
    for path in files:
        d = json.load(open(path))
        tname = f"llm2llm-graph-{slug_of(d['persona'])}"
        prior = [p for p in files[:files.index(path)] if json.load(open(p))["persona"] == d["persona"]]
        cand = by_name.get(tname, [])
        tid = cand[len(prior)]["trace_id"] if len(prior) < len(cand) else cand[0]["trace_id"]
        # Langfuse rag spans (chronological)
        obs = lf._obs(tid)
        spans = []
        for s in obs:
            if (s.get("name") or "") != "rag":
                continue
            o = s.get("output") or {}
            if isinstance(o, str):
                try:
                    o = json.loads(o)
                except Exception:
                    o = {}
            spans.append((s.get("startTime"), o))
        spans.sort(key=lambda x: x[0] or "")
        turns = {t["caller"].strip(): t for t in d["transcript"]}
        final_dvs = d.get("final_dvs") or {}
        for _, o in spans:
            q = o.get("query") or ""
            parts = [p for p in q.split("; ") if p]
            laneA_parts, laneB_q = [], ""
            if parts:
                candq = parts[-1]
                m = re.search(r"\(their industry: (.*)\)$", candq)
                if m or any(candq.strip() in u or u.startswith(candq.strip())
                            for u in turns):
                    laneB_q, laneA_parts = candq, parts[:-1]
                else:
                    laneA_parts = parts
            industry = ""
            m = re.search(r"\(their industry: (.*)\)$", laneB_q)
            if m:
                industry = m.group(1)
                utt = laneB_q[:laneB_q.rindex("(their industry: ")].strip()
            else:
                utt = laneB_q.strip()
            turn = turns.get(utt) or next(
                (t for u, t in turns.items()
                 if utt and (u.startswith(utt) or utt.startswith(u))), None)
            state = o.get("state") or "?"
            pinned_tag = o.get("pinned") or ""
            scope = [s for s in rt.kb_slugs_for(state) if s != "industry"] \
                if pinned_tag else rt.kb_slugs_for(state)
            # iter64: mirror the engine's pre-pin gate — industry leaves the
            # reconstructed scope until pinned OR the industry dv was known
            # (parsed from the laneB query anchor above).
            if not pinned_tag and not (industry or "").strip():
                scope = [s for s in scope if s != "industry"]
            norm = (industry or "").strip().lower()
            for kw, kbs in ragmod.__dict__.get("_", {}) or ():
                pass
            from diallux.graph.builder import _INDUSTRY_KB_TRIGGERS
            for kw, kbs in _INDUSTRY_KB_TRIGGERS.items():
                if kw in norm:
                    scope = scope + [k for k in kbs if k not in scope]
            lanes, owned = [], []
            for p in laneA_parts:
                matched = None
                try:
                    for t in ragmod.parse_refer_tags(rt.prompts.get(state, "")):
                        if p.startswith(t["what"]):
                            matched = t
                            break
                except Exception:
                    pass
                if matched:
                    lanes.append({"query": p, "scope": [matched["kb"]]})
                    owned.append({matched["kb"]})
                else:
                    lanes.append({"query": p, "scope": list(scope)})
                    owned.append(set())
            if laneB_q:
                lanes.append({"query": laneB_q, "scope": list(scope)})
                owned.append(set())
            res = []
            if lanes:
                if laneB_q:
                    res.extend(await store.retrieve_lanes(lanes[:-1]))
                    r_b = await store.retrieve_lanes([lanes[-1]])
                    res.append(r_b[0] if r_b else [])
                else:
                    res.extend(await store.retrieve_lanes(lanes))
            merged = ragmod.merge_lane_chunks(
                res, owned, top_k=int(Settings().rag_top_k),
                char_budget=int(Settings().rag_char_budget))
            pin_chunks = []
            if pinned_tag:
                try:
                    pin_chunks = await store.pinned_by_tag(pinned_tag)
                except Exception:
                    pin_chunks = []
            entry = {
                "trace": tid[:12], "call": d["persona"][:34],
                "turn": turn["turn"] if turn else "?",
                "state": state,
                "lf": {"chunks": o.get("chunks"), "kbs": o.get("kbs"),
                       "lanes": o.get("lanes"), "pinned": o.get("pinned"),
                       "degraded": o.get("degraded"),
                       "zero_hit": o.get("zero_hit"),
                       "await_ms": o.get("await_ms"), "ms": o.get("ms")},
                "caller": (turn["caller"][:220] if turn else utt[:220]),
                "agent": (turn["agent"][:700] if turn else ""),
                "pool": [
                    {"lane": i, "query": lanes[i]["query"][:140],
                     "scope": lanes[i]["scope"],
                     "chunks": [{"kb": c.get("kb"), "id": c.get("id"),
                                 "score": round(c.get("score") or 0, 3),
                                 "content": (c.get("content") or "")[:300]}
                                for c in (res[i] or [])]}
                    for i in range(len(lanes))],
                "served_vector": [{"kb": c.get("kb"), "score": round(c.get("score") or 0, 3),
                                   "content": (c.get("content") or "")[:280]}
                                  for c in merged],
                "pin_chunks": [{"kb": c.get("kb"), "score": round(c.get("score") or 0, 3),
                                "content": (c.get("content") or "")[:280]}
                               for c in (pin_chunks or [])],
                "pin_tag": pinned_tag,
                "industry": industry,
            }
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print("done, lines:", sum(1 for _ in open(OUT)))
    await rt.aclose()

asyncio.run(main())
