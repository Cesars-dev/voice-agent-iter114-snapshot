#!/usr/bin/env python3
"""iter23 T3 — v5 (LangGraph state machine) side extraction + Langfuse rag-span audit.

READ-ONLY: parses the v1.8-iter20 corpus files selected by the T1 manifest, applies the
same fingerprint engine as T2, and pulls Langfuse `rag` spans (GET only, basic auth from
.env) to build the state → KB → pulls matrix per call. Calls without Langfuse traces are
kept with aggregate rag_stats + fingerprints and flagged trace_missing.
"""
import argparse, base64, calendar, glob, json, os, random, re, time, urllib.request
from collections import defaultdict
from iter23_extract import load_fingerprints  # same engine, same parsing rules

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def lf_env():
    env = {}
    for line in open(os.path.join(ROOT, ".env")):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env.setdefault(k.strip(), v.strip())
    host = os.environ.get("LANGFUSE_HOST") or env.get("LANGFUSE_HOST") or "http://localhost:3001"
    pk = os.environ.get("LANGFUSE_PUBLIC_KEY") or env.get("LANGFUSE_PUBLIC_KEY") or env.get("LANGFUSE_PK") or ""
    sk = os.environ.get("LANGFUSE_SECRET_KEY") or env.get("LANGFUSE_SECRET_KEY") or env.get("LANGFUSE_SK") or ""
    return host.rstrip("/"), pk, sk

class LF:
    def __init__(self):
        self.host, self.pk, self.sk = lf_env()
    def get(self, path):
        req = urllib.request.Request(
            self.host + path,
            headers={"Authorization": "Basic " + base64.b64encode(f"{self.pk}:{self.sk}".encode()).decode()})
        last = None
        for _ in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.load(r)
            except Exception as e:  # transient 5xx on big traces
                last = e
                time.sleep(1.5)
        raise last
    def all_traces(self, days=10):
        from_ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - days * 24 * 3600))
        out, page = [], 1
        while True:
            d = self.get(f"/api/public/traces?fromTimestamp={from_ts}&limit=100&page={page}")
            b = d.get("data", [])
            out += b
            if len(b) < 100:
                break
            page += 1
        return out

def agent_lines_v5(transcript):
    """[{turn, caller, agent, state}] -> deduped agent turn texts with state + user texts + sequence."""
    lines, prev, seq = [], object(), []
    user_lines = []
    for t in transcript:
        cu = (t.get("caller") or "").strip()
        if cu:
            user_lines.append(cu)
            seq.append({"who": "u", "text": cu})
        txt = (t.get("agent") or "").strip()
        if not txt or txt == prev:
            continue
        prev = txt
        seq.append({"who": "a", "idx": len(lines), "turn": t.get("turn"),
                    "state": t.get("state"), "text": txt})
        lines.append({"turn": t.get("turn"), "state": t.get("state"), "text": txt})
    return lines, user_lines, seq

def match_trace(by_name, path, d):
    """Find the llm2llm-graph-<slug> trace whose startTime ≈ call start (mtime - wall_s)."""
    cid = d["call_id"]
    slug = cid[4:cid.rfind("-")] if cid.startswith("l2l-") else cid
    want = f"llm2llm-graph-{slug}"
    target = os.path.getmtime(path) - (d.get("wall_s") or 0)
    best, best_dt = None, 1e18
    for t in by_name.get(want, []):
        st = t.get("timestamp") or t.get("startTime")
        if not st:
            continue
        ts = calendar.timegm(time.strptime(st[:19], "%Y-%m-%dT%H:%M:%S"))
        dt = abs(ts - target)
        if dt < best_dt:
            best, best_dt = t, dt
    if best is None or best_dt > 900:   # >15 min away = no match
        return None
    return best

def fetch_rag(lf, trace):
    full = lf.get(f"/api/public/traces/{trace['id']}")
    rag = [o for o in full.get("observations", []) if (o.get("name") or "") == "rag"]
    return rag

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--fingerprints", required=True)
    ap.add_argument("--out-extract", required=True)
    ap.add_argument("--out-rag", required=True)
    ap.add_argument("--fallback-corpus", default=None,
                    help="v1.7-iter19 json_logs: substitute untraced selections with the "
                         "latest traced same-persona call from this corpus")
    args = ap.parse_args()
    manifest = json.load(open(args.manifest))
    fps = load_fingerprints(args.fingerprints)
    lf = LF()
    traces = lf.all_traces(days=10)
    by_name = defaultdict(list)
    for t in traces:
        by_name.setdefault(t.get("name") or "", []).append(t)
    rag_cache = {}          # trace_id -> rag spans
    used_traces = set()     # trace ids already consumed by a substitution

    def rag_spans(trace):
        tid = trace["id"]
        if tid not in rag_cache:
            rag_cache[tid] = fetch_rag(lf, trace)
        return rag_cache[tid]

    def substitution_for(persona_prefix, n_needed=1):
        """Latest traced same-persona calls in the fallback corpus, not yet used."""
        out = []
        for f in sorted(glob.glob(os.path.join(args.fallback_corpus, persona_prefix + "*.json")),
                        key=os.path.getmtime, reverse=True):
            try:
                dd = json.load(open(f))
            except Exception:
                continue
            tr = match_trace(by_name, f, dd)
            if tr is None or tr["id"] in used_traces:
                continue
            try:
                rag_spans(tr)          # verify the trace is fetchable
            except Exception:
                continue
            out.append((f, dd, tr))
            if len(out) >= n_needed_total[0]:
                break
        return out

    n_needed_total = [0]

    conv_records, rag_records = [], []
    class_stats = {}
    for persona, pv in manifest["v5"].items():
        cls = pv["class"]
        prefix = pv["selected"][0]["file"].split("_")[0]
        cstats = class_stats.setdefault(cls, {
            "fingerprint_hits": defaultdict(int), "examples": defaultdict(list),
            "agent_turns": 0, "conversations": 0, "trace_missing": 0,
            "rag_pull_turns": 0})
        # resolve selections: keep every manifest conversation; an untraced iter20
        # call is substituted by a traced same-persona iter19 call when available,
        # otherwise it STAYS with trace_id=None (fingerprints + rag_stats only)
        resolved = []
        n_missing = 0
        for sel in pv["selected"]:
            path0 = os.path.join(args.corpus, sel["file"])
            d0 = json.load(open(path0))
            resolved.append((sel, path0, d0))
            if match_trace(by_name, path0, d0) is None:
                n_missing += 1
        n_needed_total[0] = n_missing
        if n_missing and args.fallback_corpus:
            prefix = pv["selected"][0]["file"].split("_")[0]
            pool = iter(substitution_for(prefix, n_missing))
        else:
            pool = None
        for sel, path0, d0 in resolved:
            best = match_trace(by_name, path0, d0)
            if best is None and pool is not None:
                nxt = next(pool, None)
                if nxt is not None:
                    f19, d19, tr19 = nxt
                    used_traces.add(tr19["id"])
                    sel = dict(sel, file=os.path.basename(f19), call_id=d19["call_id"],
                               substituted_from="v1.7-iter19")
                    path0, d0, best = f19, d19, tr19
            path, d = path0, d0
            lines, user_lines, seq = agent_lines_v5(d.get("transcript", []))
            rec = {"persona": persona, "class": cls, "file": sel["file"],
                   "call_id": sel["call_id"], "pass": sel["pass"],
                   "substituted_from": sel.get("substituted_from"),
                   "agent_turns": len(lines), "agent_lines": [l["text"] for l in lines],
                   "user_lines": user_lines, "sequence": seq,
                   "turn_states": {str(l["turn"]): l["state"] for l in lines},
                   "rag_stats": d.get("rag_stats") or {}, "trace_id": None,
                   "fingerprint_hits": []}
            if best is None:
                cstats["trace_missing"] += 1
            else:
                rec["trace_id"] = best["id"]
                try:
                    rag = rag_spans(best)
                except Exception:
                    cstats["trace_missing"] += 1
                    rec["trace_id"] = None
                    rag = []
            if rec["trace_id"]:
                rrec = {"call_id": sel["call_id"], "persona": persona, "class": cls, "trace_id": best["id"],
                        "substituted_from": sel.get("substituted_from"),
                        "spans": [], "matrix": defaultdict(lambda: defaultdict(
                            lambda: {"pulls": 0, "chunks": 0, "zero": 0}))}
                for o in sorted(rag, key=lambda x: x.get("startTime") or ""):
                    out = o.get("output") or {}
                    state, kbs, chunks = out.get("state"), out.get("kbs") or [], out.get("chunks") or 0
                    rrec["spans"].append({"state": state, "kbs": kbs, "chunks": chunks})
                    for k in kbs:
                        cell = rrec["matrix"][state][k]
                        cell["pulls"] += 1
                        cell["chunks"] += chunks
                        if chunks == 0:
                            cell["zero"] += 1
                    cstats["rag_pull_turns"] += 1
                rrec["matrix"] = {s: dict(k) for s, k in rrec["matrix"].items()}
                rag_records.append(rrec)
            for li, l in enumerate(lines):
                for fp in fps:
                    if fp["rx"].search(l["text"]):
                        cstats["fingerprint_hits"][fp["name"]] += 1
                        rec["fingerprint_hits"].append({"turn": l["turn"], "aidx": li,
                                                        "state": l["state"],
                                                        "fp": fp["name"], "kb": fp["kb"],
                                                        "line": l["text"][:400]})
                        if len(cstats["examples"][fp["name"]]) < 3:
                            cstats["examples"][fp["name"]].append(
                                {"file": sel["file"], "turn": l["turn"], "line": l["text"][:400]})
            cstats["agent_turns"] += len(lines)
            cstats["conversations"] += 1
            conv_records.append(rec)

    for cls, s in class_stats.items():
        s["fingerprint_hits"] = dict(s["fingerprint_hits"])
        s["examples"] = dict(s["examples"])
    out = {"side": "v5-langgraph", "corpus": args.corpus,
           "fingerprints_used": [f["name"] for f in fps],
           "classes": class_stats, "conversations": conv_records}
    json.dump(out, open(args.out_extract, "w"), indent=2)
    rag_out = {"side": "v5-langgraph", "source": "Langfuse rag spans (llm2llm-graph-* traces)",
               "per_call": rag_records}
    json.dump(rag_out, open(args.out_rag, "w"), indent=2)
    print("wrote", args.out_extract)
    print("wrote", args.out_rag)
    for cls, s in sorted(class_stats.items()):
        fp_total = sum(s["fingerprint_hits"].values())
        print(f"{cls:8s} convs={s['conversations']:2d} agent_turns={s['agent_turns']:4d} "
              f"fp_hits={fp_total:4d} trace_missing={s['trace_missing']}")
    random.seed(23)
    for r in random.sample(conv_records, min(10, len(conv_records))):
        print("SAMPLE", r["file"][:32], "→", (r["agent_lines"][0][:110] if r["agent_lines"] else "<none>"))

if __name__ == "__main__":
    main()
