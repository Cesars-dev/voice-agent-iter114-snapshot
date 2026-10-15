#!/usr/bin/env python3
"""iter23 T2 — deployed (Retell era) side extraction.

Reads the last-3-batches manifest (T1), parses every selected payload's transcript
into agent turns, fetches (and caches) the public knowledge_base_retrieved_contents
URL for per-turn KB pulls, and tallies sales-KB output fingerprints per persona class.

READ-ONLY: writes only its --out JSON report (+ an HTTP GET per unique KB contents URL,
cached under --kb-cache). Never touches agents/KBs/graph.
"""
import argparse, glob, json, os, random, re, urllib.request
from collections import defaultdict

SALES_KBS = {"sales-language-kb", "sales-psychology-kb", "pain-points-kb"}
ALL_KB_HINTS = re.compile(r"\{Title:\s*([^}]+?)\}")

def load_fingerprints(path):
    fps = []
    for line in open(path):
        m = re.match(r"- \*\*(.+?)\*\* \((.+?)\) — regex: `(.+?)`\s*$", line.strip())
        if m:
            fps.append({"name": m.group(1), "kb": m.group(2), "rx": re.compile(m.group(3), re.M)})
    return fps

def parse_transcript(transcript):
    """Split flat 'User:/Agent:' transcript into ordered turns (multi-line safe)."""
    turns = []
    for raw in transcript.split("\n"):
        line = raw.rstrip()
        if line.startswith("User: "):
            turns.append({"who": "user", "text": line[6:]})
        elif line.startswith("Agent: "):
            turns.append({"who": "agent", "text": line[7:]})
        elif line.startswith("Agent:"):
            turns.append({"who": "agent", "text": line[6:]})
        elif line.startswith("User:"):
            turns.append({"who": "user", "text": line[5:]})
        elif turns and line:
            turns[-1]["text"] += "\n" + line
    agent_turns, prev, seq = [], object(), []
    user_turns = []
    for t in turns:
        if t["who"] == "user":
            txt = t["text"].strip()
            if txt:
                user_turns.append(txt)
                seq.append({"who": "u", "text": txt})
            continue
        txt = t["text"].strip()
        if not txt or txt == prev:      # dedupe consecutive identical
            continue
        prev = txt
        seq.append({"who": "a", "idx": len(agent_turns), "text": txt})
        agent_turns.append(txt)
    return agent_turns, user_turns, seq

def kb_pulls(url, cache):
    if not url:
        return None
    cp = os.path.join(cache, re.sub(r"[^A-Za-z0-9]", "", url[-80:]) + ".json")
    if os.path.exists(cp):
        data = json.load(open(cp))
    else:
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.load(r)
        json.dump(data, open(cp, "w"))
    per_turn = {}
    for t in data:
        titles = set()
        for c in t.get("contexts", []):
            m = ALL_KB_HINTS.search(c)
            if m:
                titles.add(m.group(1).strip())
        per_turn[t["response_id"]] = sorted(titles)
    return per_turn  # response_id -> set of KB titles (None if url absent)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--fingerprints", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--kb-cache", default="/tmp/opencode/iter23_kb_cache")
    args = ap.parse_args()
    os.makedirs(args.kb_cache, exist_ok=True)

    manifest = json.load(open(args.manifest))
    fps = load_fingerprints(args.fingerprints)
    classes = {}
    conv_records = []
    for cls, batches in manifest["deployed"].items():
        cstats = {"fingerprint_hits": defaultdict(int), "examples": defaultdict(list),
                  "kb_turns": defaultdict(int), "kb_chunks_turns": 0, "agent_turns": 0,
                  "conversations": 0, "kb_url_missing": 0}
        for b in batches:
            for conv in b["conversations"]:
                path = os.path.join(args.corpus, conv["file"])
                d = json.load(open(path))
                agent_lines, user_lines, seq = parse_transcript(d.get("transcript", ""))
                pulls = kb_pulls(conv.get("kb_contents_url"), args.kb_cache)
                rec = {"class": cls, "file": conv["file"], "chat_id": conv["chat_id"],
                       "agent_id": conv["agent_id"], "date_utc": conv["date_utc"],
                       "chat_status": conv["chat_status"],
                       "kb_url_missing": pulls is None,
                       "agent_turns": len(agent_lines), "agent_lines": agent_lines,
                       "user_lines": user_lines, "sequence": seq,
                       "kb_pulls_per_turn": pulls,
                       "sales_kb_turns": {}, "fingerprint_hits": []}
                if pulls is None:
                    cstats["kb_url_missing"] += 1
                else:
                    for rid, titles in pulls.items():
                        for t in titles:
                            cstats["kb_turns"][t] += 1
                        if SALES_KBS & set(titles):
                            rec["sales_kb_turns"][rid] = sorted(SALES_KBS & set(titles))
                for i, line in enumerate(agent_lines, start=1):
                    for fp in fps:
                        if fp["rx"].search(line):
                            cstats["fingerprint_hits"][fp["name"]] += 1
                            rec["fingerprint_hits"].append(
                                {"turn": i, "fp": fp["name"], "kb": fp["kb"],
                                 "kb_pulled_same_turn": bool(
                                     pulls and any(fp["kb"] in t for t in pulls.get(i, []))),
                                 "aidx": i - 1, "line": line[:400]})
                            if len(cstats["examples"][fp["name"]]) < 3:
                                cstats["examples"][fp["name"]].append(
                                    {"file": conv["file"], "turn": i, "aidx": i - 1, "line": line[:400]})
                cstats["agent_turns"] += len(agent_lines)
                cstats["conversations"] += 1
                conv_records.append(rec)
        classes[cls] = {
            "conversations": cstats["conversations"],
            "agent_turns": cstats["agent_turns"],
            "kb_url_missing_conversations": cstats["kb_url_missing"],
            "kb_pull_turns_by_kb": dict(sorted(cstats["kb_turns"].items(),
                                               key=lambda x: -x[1])),
            "sales_kb_turns_total": sum(v for k, v in cstats["kb_turns"].items()
                                        if k in SALES_KBS),
            "fingerprint_hits": dict(cstats["fingerprint_hits"]),
            "examples": {k: v for k, v in cstats["examples"].items()},
        }
    out = {"side": "deployed-retell", "fingerprints_used": [f["name"] for f in fps],
           "classes": classes, "conversations": conv_records}
    json.dump(out, open(args.out, "w"), indent=2)
    print("wrote", args.out)
    for cls, s in sorted(classes.items()):
        fp_total = sum(s["fingerprint_hits"].values())
        print(f"{cls:8s} convs={s['conversations']:2d} agent_turns={s['agent_turns']:4d} "
              f"sales_kb_turns={s['sales_kb_turns_total']:4d} fp_hits={fp_total:4d} "
              f"no_kb_url={s['kb_url_missing_conversations']}")
    random.seed(23)
    for r in random.sample(conv_records, min(10, len(conv_records))):
        print("SAMPLE", r["file"][:32], "→", (r["agent_lines"][0][:110] if r["agent_lines"] else "<none>"))

if __name__ == "__main__":
    main()
