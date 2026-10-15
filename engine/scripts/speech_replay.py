#!/usr/bin/env python
"""speech_replay.py — SDK: reconstruct what the TTS engine SPOKE vs CUT on a
mic/live call by replaying the call's LLM output stream (Langfuse) through the
REAL speech-filter chain (iter65 sanitizer -> iter41/42 exact window ->
iter59 semantic stem dedupe with the live embedder).

Usage (from engine/):
    set -a; . ./.env; set +a
    .venv/bin/python scripts/speech_replay.py <trace-id|micbridge-<sid>> [--all] [--fast-flush]

  <trace-id>       Langfuse trace id or >=8-char prefix (micbridge-<sid> works)
  --all            print every sentence (default: summary + CUT lines only)
  --fast-flush     simulate tts_gate_fast_first_flush=True (default: env/False)

Evidence tool — read-only, no engine behavior change. iter68 T1 ground truth.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diallux.graph.builder import (  # noqa: E402
    _SENTENCE_SPLIT_RE,
    _cosine,
    _fast_flush_cut,
    _norm_sentence,
    _sanitize_spoken,
)
from diallux.rag import local_embed_fn  # noqa: E402


class _S:
    """Settings stub mirroring the live lane knobs (env overridable)."""

    def __init__(self, fast_flush: bool | None = None):
        self.tts_sanitize_tokens = _envbool("TTS_SANITIZE_TOKENS", True)
        self.tts_dedupe_sentences = _envbool("TTS_DEDUPE_SENTENCES", True)
        self.tts_dedupe_window = int(os.getenv("TTS_DEDUPE_WINDOW", "6"))
        self.tts_dedupe_semantic = _envbool("TTS_DEDUPE_SEMANTIC", True)
        self.tts_dedupe_cos_threshold = float(os.getenv("TTS_DEDUPE_COS_THRESHOLD", "0.90"))
        self.tts_dedupe_stem_window = int(os.getenv("TTS_DEDUPE_STEM_WINDOW", "12"))
        self.tts_gate_fast_first_flush = _envbool(
            "TTS_GATE_FAST_FIRST_FLUSH", True) if fast_flush is None else fast_flush
        self.tts_gate_fast_first_flush_chars = 28


def _envbool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def _resolve_trace(ref: str):
    """ref = full/short trace id OR micbridge-<sid>. Returns (trace_id, name)."""
    from langfuse import Langfuse
    lf = Langfuse()
    cutoff = None
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    name = ref if ref.startswith("micbridge-") else None
    found = []
    page = 1
    while True:
        resp = lf.api.trace.list(limit=100, page=page, from_timestamp=cutoff)
        for t in resp.data:
            if (t.id == ref or t.id.startswith(ref)
                    or (name and (t.name or "") == name)):
                found.append(t)
        if page >= (resp.meta.total_pages or 1) or found:
            break
        page += 1
    if not found:
        raise SystemExit(f"trace not found: {ref}")
    t = found[0]
    return t.id, (t.name or "")


def _gens(trace) -> list[dict]:
    gens = [o for o in trace.observations
            if (o.type or "").upper() == "GENERATION"]
    gens.sort(key=lambda o: o.start_time)
    out = []
    for g in gens:
        text = ""
        o = g.output
        if isinstance(o, dict):
            text = o.get("content") or o.get("text") or ""
        elif isinstance(o, str):
            text = o
        # last user utterance in the input = turn fingerprint for window resets
        last_user = ""
        inp = g.input
        if isinstance(inp, list):
            for m in reversed(inp):
                if isinstance(m, dict) and m.get("role") == "user":
                    last_user = str(m.get("content") or "")
                    break
        out.append({"name": g.name, "t": g.start_time, "text": text,
                    "last_user": last_user})
    return out


async def replay(gens: list[dict], s: _S, embed_fn, show_all: bool):
    exact: list[str] = []
    stems: list[dict] = []
    prev_user = None
    cuts = {"sanitizer": 0, "exact": 0, "semantic": 0}
    spoken_n = 0
    for g in gens:
        if g["last_user"] != prev_user:      # new turn -> exact window resets
            exact = []
            prev_user = g["last_user"]
        if show_all:
            print(f"\n--- {g['name']} @ {g['t'].strftime('%H:%M:%S')}  OUTPUT: {g['text']!r}")
        text = g["text"]
        idx = 0
        parts = []
        for m in _SENTENCE_SPLIT_RE.finditer(text):
            parts.append(text[idx:m.end()])
            idx = m.end()
        if idx < len(text):
            parts.append(text[idx:])
        spoke_any = False
        for raw in parts:
            if not raw.strip():
                continue
            # fast-flush simulation (pre-sanitizer clause cut, iter46 T5)
            if s.tts_gate_fast_first_flush and not spoke_any:
                cut = _fast_flush_cut(raw, s)
                if 0 < cut < len(raw):
                    head, raw = raw[:cut], raw[cut:]
                    head_s = _sanitize_spoken(head, s)
                    if head_s is None:
                        cuts["sanitizer"] += 1
                        print(f"  [CUT sanitizer] {head!r}")
                    elif show_all:
                        print(f"  [SPOKEN fast-flush fragment] {head_s!r}")
                    else:
                        print(f"  [SPOKEN fragment] {head_s!r}")
                    if head_s is not None:
                        exact.append(_norm_sentence(head_s))
                        spoken_n += 1
                        spoke_any = True
            sent = _sanitize_spoken(raw, s)
            if sent is None:
                cuts["sanitizer"] += 1
                print(f"  [CUT sanitizer] {raw!r}")
                continue
            key = _norm_sentence(sent)
            if key and key in exact and spoke_any:
                cuts["exact"] += 1
                print(f"  [CUT exact-window] {sent!r}")
                continue
            if "?" in sent and stems and s.tts_dedupe_semantic:
                vec = (await embed_fn([sent.strip()[:1000]]))[0]
                best = max((_cosine(vec, e["vec"]) for e in stems if e.get("vec")),
                           default=0.0)
                if best >= s.tts_dedupe_cos_threshold:
                    cuts["semantic"] += 1
                    print(f"  [CUT semantic re-ask cos={best:.4f}] {sent!r}")
                    continue
            spoken_n += 1
            spoke_any = True
            if key:
                exact.append(key)
                if len(exact) > s.tts_dedupe_window:
                    del exact[:len(exact) - s.tts_dedupe_window]
            if "?" in sent and s.tts_dedupe_semantic:
                vec = (await embed_fn([sent.strip()[:1000]]))[0]
                if vec is not None and not any(e["text"] == key for e in stems):
                    stems.append({"text": key, "vec": vec})
                    if len(stems) > s.tts_dedupe_stem_window:
                        del stems[:len(stems) - s.tts_dedupe_stem_window]
            if show_all:
                print(f"  [SPOKEN] {sent!r}")
        if not spoke_any and show_all:
            print("  (flat round: nothing spoken)")
    return cuts, spoken_n


def _sid_of(trace) -> str:
    name = trace.name or ""
    return name[len("micbridge-"):] if name.startswith("micbridge-") else ""


def _live_facts(sid: str, trace) -> None:
    """Prewarm/config/latency facts for the call, parsed from the lane journal.
    Providers: Deepgram STT (flux), Cartesia/EL TTS, OpenAI LLM boot prewarm."""
    import json
    import subprocess
    import statistics as st
    sid = _sid_of(trace)
    start = trace.timestamp.strftime("%Y-%m-%d %H:%M:%S")
    end = (trace.timestamp + __import__("datetime").timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
    if not sid:
        print("\n== LIVE FACTS: not a micbridge trace — skipped")
        return
    try:
        out = subprocess.run(
            ["journalctl", "--user", "-u", "diallux-8024.service",
             "--since", start, "--until", end, "--no-pager"],
            capture_output=True, text=True, timeout=30).stdout
    except Exception as exc:
        print(f"\n== LIVE FACTS: journalctl unavailable ({exc})")
        return

    def scan(pat):
        return [l.split("python", 1)[-1].strip() for l in out.splitlines() if pat in l]

    print("\n== LIVE FACTS (provider config + prewarm, from the serving journal)")
    for l in scan("cartesia connected") + scan("cartesia adopted")[:1]:
        print(f"  TTS  cartesia: {l}")
    for l in scan("elevenlabs"):
        print(f"  TTS  elevenlabs: {l}")
    for l in scan("deepgram adopted") + scan("deepgram connected"):
        print(f"  STT  {l}")
    for l in scan("boot prewarm"):
        print(f"  LLM  boot prewarm: {l}")
    for l in scan("prewarm: stt/tts/rag"):
        print(f"  PREWARM {l}")
    for l in scan("warm done"):
        print(f"  WARM {l}")
    for l in scan("await_warm"):
        if "waited_ms=0" not in l:
            print(f"  AWAIT-WAITED {l}")
    if "should be specified explicitly" in out:
        print("  LLM params delivered via model_kwargs (reasoning_effort/verbosity) — langchain deprecation warning present")

    # per-turn latency from the turn final reports
    import re
    rows = []
    for l in out.splitlines():
        if "final report" not in l:
            continue
        m = re.search(r"\{.*\}", l)
        if m:
            try:
                rows.append(json.loads(m.group(0)))
            except Exception:
                pass
    rows.sort(key=lambda r: r.get("turn", 0))
    if not rows:
        print("  (no turn final reports in window)")
        return
    print(f"  TURN REPORTS n={len(rows)}:")
    def stat(key):
        v = [r[key] for r in rows if r.get(key) is not None]
        if not v:
            return "n/a"
        v.sort()
        return f"p50={st.median(v):.0f} p90={v[min(len(v)-1, int((len(v)-1)*0.9))]:.0f} max={max(v):.0f}"
    for key in ("stt_eot_to_llm_first_ms", "llm_first_to_tts_first_ms",
                "e2e_response_ms", "e2e_turn_ms"):
        print(f"    {key:28s} {stat(key)}")
    t1 = next((r for r in rows if r.get("turn") == 1), None)
    if t1:
        print(f"    TURN-1: ttft={t1.get('stt_eot_to_llm_first_ms')} "
              f"e2e={t1.get('e2e_response_ms')} (steady-state p50 above = the comparison)")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("ref", help="trace id / prefix / micbridge-<sid>")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--fast-flush", action="store_true")
    args = ap.parse_args()

    trace_id, name = _resolve_trace(args.ref)
    from langfuse import Langfuse
    trace = Langfuse().api.trace.get(trace_id)
    gens = _gens(trace)
    print(f"trace {trace_id}  name={name}  gens={len(gens)}")
    if not gens:
        return
    s = _S(fast_flush=True if args.fast_flush else None)
    print(f"knobs: sanitize={s.tts_sanitize_tokens} exact={s.tts_dedupe_sentences}"
          f"(w={s.tts_dedupe_window}) semantic={s.tts_dedupe_semantic}"
          f"(thr={s.tts_dedupe_cos_threshold}) fast_flush={s.tts_gate_fast_first_flush}")
    embed_fn = await local_embed_fn(
        os.getenv("RAG_EMBEDDING_MODEL", "snowflake/snowflake-arctic-embed-m"),
        os.getenv("LOCAL_EMBED_CACHE_DIR", "/home/julio/fastembed/models"), threads=4)
    cuts, spoken = await replay(gens, s, embed_fn, args.all)
    print(f"\n== SUMMARY {name}: spoken={spoken} cut: "
          f"sanitizer={cuts['sanitizer']} exact={cuts['exact']} "
          f"semantic={cuts['semantic']}")
    _live_facts(args.ref, trace)


if __name__ == "__main__":
    asyncio.run(main())
