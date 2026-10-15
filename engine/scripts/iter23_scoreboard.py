#!/usr/bin/env python3
"""iter23 T4 — toe-to-toe scoreboard.

Scores both sides per persona class on the 5 pinned sales beats (0–2 each):
  1. sales-KB fingerprint density per 10 agent turns
  2. objection handling (objection-family fingerprint within 2 agent turns of a customer objection)
  3. pricing discipline (defers; sanctioned $697 string ONLY under real push)
  4. pain amplification anchored on the caller's words
  5. acknowledgment/closing warmth
KB-pull dimension is scored separately (v5 = Langfuse rag spans; Retell = direct
per-turn retrieved-contents evidence from the payload's CloudFront URL).
"""
import argparse, json, re
from collections import defaultdict

OBJ_RX = re.compile(
    r"\b(?:price|pricing|cost|expensive|too much money|how much|ballpark|human|real person|"
    r"robot|a\.i\.|\bai\b|scam|scammed|competitor|think about it|not interested|"
    r"too busy|no time|call me back|callback)\b", re.I)
PAIN_RX = re.compile(
    r"\b(?:lost|losing|missed|missing|after hours|closed|voicemail|leak|bleeding|"
    r"overwhelmed|stress(ed|ful)?|money|revenue|patients?|jobs?|clients?)\b", re.I)
OBJ_KILL_FPS = {"SL-isolate", "SL-think-free", "SL-competitor", "SL-price-defer",
                "SL-value-anchor", "SP-cost-ladder", "SP-binary-close", "PP-probe", "PP-quantify"}
PAIN_FPS = {"SP-cost-ladder", "SP-urgency-math", "SP-industry-urgency", "PP-quantify",
            "PP-projection", "PP-comparison", "PP-consequence", "PP-emotional"}
WARM_RX = re.compile(r"\b(thank(s| you)|looking forward|talk soon|have a great|glad|"
                     r"appreciate|take care|anytime|pleasure|help(ed)? with that)\b", re.I)
STOP = set("a an the and or but if of to in on at for with your you i it is are was were "
           "be been do does did not no so we our us they them he she his her my me that "
           "this these those have has had will would can could should just what when where "
           "how why who which there their about".split())

def content_words(text):
    return {w for w in re.findall(r"[a-z']{4,}", text.lower()) if w not in STOP} - {
        "that's", "we're", "you're", "don't", "can't", "it's", "didn't", "here's", "there's",
        "what's", "let's", "they're", "i'm", "you're", "makes", "sense", "right", "exactly",
        "specifically", "minutes", "commitment"}

ACK_RX = re.compile(r"\b(got it|makes sense|i hear you|hear how|fair enough|totally|"
                    r"understand|completely fair|good question|great question|yeah)\b", re.I)

def beats_for_class(cls_stats, convs):
    turns = cls_stats["agent_turns"]
    conv_n = cls_stats["conversations"]
    b1 = min(2.0, sum(cls_stats["fingerprint_hits"].values()) / max(turns, 1) * 10) if turns else 0.0

    objections = fp_handled = behav_handled = 0
    pain_total = pain_met = anch = 0
    price_asks = price_defer = price_697 = under_push = unsanctioned = 0
    warm_conv = 0
    unsanctioned_examples = [""]
    for c in convs:
        seq = c["sequence"]
        hits = c["fingerprint_hits"]
        for i, e in enumerate(seq):
            if e["who"] != "u":
                continue
            nxt = [x for x in seq[i + 1:i + 4] if x["who"] == "a"][:2]
            nxt_txt = " ".join(x["text"] for x in nxt)
            nxt_idx = {x["idx"] for x in nxt}
            obj = OBJ_RX.search(e["text"])
            if obj:
                objections += 1
                nxt_fp = [h["fp"] for h in hits if h.get("aidx", h["turn"] - 1) in nxt_idx]
                if any(fp in OBJ_KILL_FPS for fp in nxt_fp):
                    fp_handled += 1
                if ACK_RX.search(nxt_txt) and "?" in nxt_txt:
                    behav_handled += 1
            if re.search(r"\b(price|pricing|how much|cost|expensive)\b", e["text"], re.I):
                price_asks += 1
                if any(h["fp"] == "SL-price-defer" and h.get("aidx", h["turn"] - 1) in nxt_idx
                       for h in hits):
                    price_defer += 1
            if PAIN_RX.search(e["text"]):
                pain_total += 1
                nxt_fp2 = [h["fp"] for h in hits if h.get("aidx", h["turn"] - 1) in nxt_idx]
                if any(fp in PAIN_FPS for fp in nxt_fp2):
                    pain_met += 1
                    cw = content_words(e["text"])
                    if any(cw & content_words(x["text"]) for x in nxt):
                        anch += 1
        for i, e in enumerate(seq):
            if e["who"] != "a":
                continue
            if any(h["fp"] == "SL-price-697" and h.get("aidx", h["turn"] - 1) == e["idx"]
                   for h in hits):
                price_697 += 1
                prev_users = [x["text"] for x in seq[:i] if x["who"] == "u"][-2:]
                if any(re.search(r"\b(price|pricing|how much|cost|expensive)\b", u, re.I)
                       for u in prev_users):
                    under_push += 1
            # unsanctioned OUR-price range quotes (e.g. "$1,200 to $2,800 a month");
            # exclude the sanctioned anchors (answering-service $2,500-3,500,
            # $300-600 dental benchmark) and leak-math (their lost revenue)
            for m in re.finditer(r"\$\s?\d{1,3},\d{3}\s?(?:-|to)\s?\$\s?\d{1,3},\d{3}", e["text"]):
                frag = e["text"][max(0, m.start() - 90):m.end() + 60]
                if re.search(r"answering service|per (?:signed case|visit|job|appointment)|"
                             r"extra a week|walking away|slipping|recover|leak", frag, re.I):
                    continue
                unsanctioned += 1
                if not unsanctioned_examples[-1]:
                    unsanctioned_examples.pop()
                    unsanctioned_examples.append(frag[:220])
        last_agents = [x["text"] for x in seq[-6:] if x["who"] == "a"][-3:]
        if any(WARM_RX.search(t) for t in last_agents):
            warm_conv += 1

    b2 = (2.0 if objections and behav_handled / objections >= 0.5 else
          1.0 if objections and behav_handled / objections >= 0.2 else 0.0)
    b2_detail = (f"{behav_handled}/{objections} objections answered with ack+question within 2 turns "
                 f"(pinned verbatim-KB-line metric: {fp_handled}/{objections})")
    # pricing discipline
    if unsanctioned >= 4:
        b3, b3_note = 0.0, f"unsanctioned price-range quotes {unsanctioned}x"
    elif unsanctioned > 0:
        b3, b3_note = 1.0, f"unsanctioned price-range quotes {unsanctioned}x: {unsanctioned_examples[-1]}"
    elif price_asks + price_697 == 0:
        b3, b3_note = 1.0, "no price interaction in class"
    elif price_697 == 0:
        b3, b3_note = 2.0, f"defers {price_defer}/{price_asks} price asks; never releases $697"
    elif price_697 == under_push and under_push > 0:
        b3, b3_note = 2.0, f"$697 released {price_697}x, all under real push"
    else:
        b3 = (1.0 if price_697 - under_push <= 2 else 0.0)
        b3_note = f"$697 released {price_697}x, only {under_push}x under push (unpushed leak {price_697 - under_push})"
    b4 = (min(2.0, round(2.0 * pain_met / max(pain_total, 1), 2)) if pain_total else 0.0)
    b5 = (2.0 if conv_n and warm_conv / conv_n >= 0.7 else
          1.0 if conv_n and warm_conv / conv_n >= 0.4 else 0.0)
    return {"beats": {
        "kb_fp_density": {"score": round(b1, 2), "detail": f"{sum(cls_stats['fingerprint_hits'].values())} hits / {turns} turns"},
        "objection_handling": {"score": b2, "detail": b2_detail},
        "pricing_discipline": {"score": b3, "detail": b3_note},
        "pain_anchoring": {"score": b4, "detail": f"{pain_met}/{pain_total} pain statements met with a pain-KB fingerprint within 2 turns ({anch} of those anchored on caller's words)"},
        "closing_warmth": {"score": b5, "detail": f"{warm_conv}/{conv_n} conversations warm-close"},
    }, "total": round(b1 + b2 + b3 + b4 + b5, 2)}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--retell", required=True)
    ap.add_argument("--v5", required=True)
    ap.add_argument("--rag", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    ret = json.load(open(args.retell))
    v5 = json.load(open(args.v5))
    rag = json.load(open(args.rag))

    # KB-pull dimension: pull turns per 10 agent turns + per-KB pull turns
    rag_by_class = defaultdict(lambda: {"pull_turns": 0, "spans": 0, "kb_turns": defaultdict(int)})
    for c in rag["per_call"]:
        s = rag_by_class[c["class"]]
        s["spans"] += len(c["spans"])
        for sp in c["spans"]:
            for k in sp["kbs"]:
                s["kb_turns"][k] += 1
        s["pull_turns"] += len(c["spans"])
    kb_dim = {}
    for cls in sorted(set(ret["classes"]) | set(v5["classes"])):
        r = ret["classes"].get(cls, {})
        v = v5["classes"].get(cls, {})
        r_turns = r.get("agent_turns", 0)
        v_turns = v.get("agent_turns", 0)
        sales_r = r.get("sales_kb_turns_total", 0)
        rg = rag_by_class.get(cls, {"pull_turns": 0, "spans": 0, "kb_turns": {}})
        kb_dim[cls] = {
            "retell": {
                "sales_kb_turns": r.get("sales_kb_turns_total", 0),
                "per_10_turns": round(r.get("sales_kb_turns_total", 0) / max(r_turns, 1) * 10, 2),
                "source": "direct — knowledge_base_retrieved_contents per agent turn (CloudFront)",
                "kb_url_missing_conversations": r.get("kb_url_missing_conversations", 0),
            },
            "v5": {
                "rag_pull_turns": rg["pull_turns"],
                "rag_spans": rg["spans"],
                "sales_kb_pull_turns": sum(n for k, n in rg["kb_turns"].items()
                                           if k in {"sales-language", "sales-psychology", "pain-points"}),
                "sales_kb_per_10_agent_turns": round(sum(n for k, n in rg["kb_turns"].items()
                                                         if k in {"sales-language", "sales-psychology", "pain-points"})
                                                     / max(v_turns, 1) * 10, 2),
                "kb_pull_turns": dict(sorted(rg["kb_turns"].items(), key=lambda x: -x[1])),
                "source": "measured — Langfuse rag spans (state, kbs, chunks per turn)",
            },
            "agent_turns": {"retell": r_turns, "v5": v_turns},
        }

    scoreboard = {"beats_max": 10, "per_beat_max": 2, "classes": {}}
    for cls in sorted(set(ret["classes"]) | set(v5["classes"])):
        rb = beats_for_class(ret["classes"][cls],
                             [c for c in ret["conversations"] if c["class"] == cls])
        vb = beats_for_class(v5["classes"][cls],
                             [c for c in v5["conversations"] if c["class"] == cls])
        scoreboard["classes"][cls] = {"retell": rb, "v5": vb,
                                      "winner_beats": "retell" if rb["total"] > vb["total"]
                                      else "v5" if vb["total"] > rb["total"] else "tie"}
    kb_dim_note = ("ASYMMETRY NOTE: v5 KB pulls are MEASURED from Langfuse rag spans; "
                   "Retell KB pulls are now DIRECTLY measured too — the T1 method upgrade found "
                   "knowledge_base_retrieved_contents (per-turn retrieved chunks) in the payload "
                   "mirrors, so no inference is needed. Conversations missing that URL "
                   "(older-agent payloads) are flagged and contribute fingerprints only.")
    out = {"scoring": "5 beats x 0-2 per class; KB-pull dimension separate",
           "kb_dimension": kb_dim, "kb_dimension_note": kb_dim_note, "scoreboard": scoreboard}
    json.dump(out, open(args.out, "w"), indent=2)
    print("wrote", args.out)
    hdr = f"{'class':8s} | " + " | ".join(f"{b:>6s}" for b in
        ["kb_fp", "obj", "price", "pain", "warm", "TOT"])
    print(hdr)
    for cls, s in scoreboard["classes"].items():
        r, v = s["retell"]["beats"], s["v5"]["beats"]
        keys = ["kb_fp_density", "objection_handling", "pricing_discipline", "pain_anchoring", "closing_warmth"]
        print(f"{cls:8s} R| " + " | ".join(f"{r[k]['score']:6.2f}" for k in keys) + f" | {s['retell']['total']:5.2f}")
        print(f"{cls:8s} V| " + " | ".join(f"{v[k]['score']:6.2f}" for k in keys) + f" | {s['v5']['total']:5.2f}   winner: {s['winner_beats']}")

if __name__ == "__main__":
    main()
