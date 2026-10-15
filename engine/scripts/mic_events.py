#!/usr/bin/env python3
"""mic_events.py — one-command forensic pull for mic/voice-call events.

Sources:
  1. the uvicorn voice-server log (default: newest /tmp/opencode/voice_server_*.log)
     — carries the whole socket lifecycle: page opens, WS gates, adoptions,
     STT/TTS errors + drops, reconnects, audio flow, warm rounds, session
     start/stop, mic_stats.
  2. --langfuse: the per-call rag/llm/tool spans for the same window
     (trace name "diallux-call") via the lf.py SDK — no extra deps.

Usage:
  scripts/mic_events.py                                  # newest log, last 400 lines, full timeline
  scripts/mic_events.py --window "04:59-05:01"           # wall-clock slice
  scripts/mic_events.py --sid 351691ef                   # one call
  scripts/mic_events.py --tag ERR,DROP                   # only failures/drops
  scripts/mic_events.py --window "05:00-05:10" --langfuse
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ---- event classification -------------------------------------------------- #
RULES = [
    ("PAGE",    re.compile(r'"GET /mic')),
    ("WS-OK",   re.compile(r"WebSocket /mic/ws.*accepted")),
    ("WS-GATE", re.compile(r"mic ws rejected \(([^)]*)\)")),
    ("START",   re.compile(r"(call|mic bridge session) ([0-9a-f]{8,}) started")),
    ("STOP",    re.compile(r"call ([0-9a-f]{8,}) stopped \(([^)]*)\)")),
    ("ADOPT-STT", re.compile(r"deepgram adopted prewarmed socket")),
    ("ADOPT-TTS", re.compile(r"cartesia adopted prewarmed socket")),
    ("STT-ERR", re.compile(r"deepgram (error event|recv loop ended)")),
    ("STT-DEAD", re.compile(r"deepgram dropped mid-call")),
    ("RECONNECT", re.compile(r"(deepgram connected \(mode|stt reconnect failed)")),
    ("TTS-DEAD", re.compile(r"cartesia recv loop ended")),
    ("TTS-ERR", re.compile(r"cartesia .*error|tts (speak|send) failed")),
    ("WARM",    re.compile(r"(prewarm: stt/tts/rag|warm done|prewarm: idle .* respawned)")),
    ("AUDIO",   re.compile(r"mic audio (FIRST chunk|flowing)")),
    ("STATS",   re.compile(r"mic page stats")),
    ("LLM",     re.compile(r"(round usage:|v1/chat/completions)")),
    ("TOOL",    re.compile(r"tool called|book-")),
    ("TURN",    re.compile(r"turn failed|graceful")),
    ("LANGFUSE", re.compile(r"(langfuse|\[langfuse\])")),
]
TAGS = set(RULES)


def classify(line: str):
    for tag, rx in RULES:
        m = rx.search(line)
        if m:
            return tag, m
    return None, None


def _pick_log(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    logs = sorted(glob.glob("/tmp/opencode/voice_server_*.log"),
                  key=os.path.getmtime)
    if not logs:
        sys.exit("no /tmp/opencode/voice_server_*.log found — pass --log")
    return Path(logs[-1])


def _in_window(ts: str, window: str | None) -> bool:
    if not window:
        return True
    m = re.match(r"(\d\d:\d\d)-(\d\d:\d\d)$", window.strip())
    if not m:
        return True
    lo, hi = m.group(1), m.group(2)
    hhmm = ts[11:16]
    return lo <= hhmm <= hi


# tags that belong to the ACTIVE call even when the log line lacks the sid
_CALL_TAGS = {"START", "STOP", "ADOPT-STT", "ADOPT-TTS", "STT-ERR", "STT-DEAD",
              "RECONNECT", "TTS-DEAD", "TTS-ERR", "AUDIO", "STATS"}
_WINDOW_TAGS = {"PAGE", "WS-OK", "WS-GATE", "WARM", "LANGFUSE"}


def timeline(lines: list[str], sids: set[str] | None, window: str | None,
             tags: set[str] | None):
    prev = None
    out = []
    for line in lines:
        # uvicorn/diallux lines: "2026-09-21 04:59:44,483 diallux.x ..."
        m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3}) (.*)", line)
        if not m:
            continue
        ts, ms, rest = m.group(1), m.group(2), m.group(3)
        if not _in_window(ts, window):
            continue
        tag, _ = classify(line)
        if tag is None:
            continue
        if tags and tag not in tags:
            continue
        if sids and not any(s in line for s in sids) \
                and tag not in (_WINDOW_TAGS | _CALL_TAGS):
            continue
        t = f"{ts[11:19]}.{ms}"
        cur = _secs(ts, ms)
        delta = f" +{cur - prev:.2f}s" if prev is not None else ""
        prev = cur
        out.append(f"{t}{delta:<12} [{tag:<9}] {rest.strip()[:220]}")
    return out


def _secs(ts: str, ms: str) -> float:
    h, mi, s = (int(x) for x in ts[11:19].split(":"))
    return h * 3600 + mi * 60 + s + int(ms) / 1000.0


# ---- langfuse ---------------------------------------------------------------- #
def langfuse_spans(window: str | None, limit: int = 20):
    """Mic-call traces are named 'micbridge-<sid>' (session.py Tracer)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("lf", ROOT / "scripts" / "lf.py")
    lf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lf)
    if window:
        m = re.match(r"(\d\d:\d\d)-(\d\d:\d\d)$", window.strip())
        end = m.group(2) if m else "23:59"
        hh, mm = map(int, end.split(":"))
        import time as _t
        now = _t.localtime()
        end_h = now.tm_hour * 3600 + now.tm_min * 60 - (now.tm_hour * 3600 + hh * 3600 + mm * 60)
        hours_back = max(0.02, (-end_h) / 3600.0)
    else:
        hours_back = 6.0
    traces = lf._traces(hours_back, "micbridge-", limit)
    for t in traces:
        obs = lf._obs(t["id"])
        print(f"\n=== trace {t['id'][:12]}  {t.get('timestamp')}  {t.get('name')}  ({len(obs)} obs)")
        for o in obs:
            name = o.get("name") or ""
            if not re.search(r"(rag|llm:|tool:|stt|warm|session|eot_silence|tts_first)", name):
                continue
            out = o.get("output") or {}
            keys = ("state", "ms", "mode", "degraded", "await_ms", "chunks",
                    "query", "stop_reason", "call_sid")
            brief = {k: out[k] for k in keys if k in out}
            print(f"  {o.get('startTime', '')[11:23]}  {name:<16} "
                  f"{json.dumps(brief)[:200] if brief else ''}")


def main():
    a = argparse.ArgumentParser()
    a.add_argument("--log", help="server log path (default: newest voice_server_*.log)")
    a.add_argument("--last", type=int, default=400, help="only inspect last N lines (0=all)")
    a.add_argument("--window", help='"HH:MM-HH:MM" wall-clock slice')
    a.add_argument("--sid", action="append", help="filter by call sid (repeatable)")
    a.add_argument("--tag", help="comma list of tags to keep, e.g. ERR,TTS-DEAD")
    a.add_argument("--langfuse", action="store_true", help="also pull rag/llm/tool spans")
    a.add_argument("--limit", type=int, default=10, help="langfuse trace limit")
    args = a.parse_args()

    log = _pick_log(args.log)
    lines = log.read_text(errors="replace").splitlines()
    if args.last:
        lines = lines[-args.last:]
    tags = {t.strip().upper() for t in args.tag.split(",")} if args.tag else None
    sids = set(args.sid) if args.sid else None
    rows = timeline(lines, sids, args.window, tags)
    print(f"--- {log} ({len(rows)} events) ---")
    for r in rows:
        print(r)
    if args.langfuse:
        langfuse_spans(args.window, args.limit)


if __name__ == "__main__":
    main()
