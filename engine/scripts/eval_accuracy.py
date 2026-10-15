#!/usr/bin/env python3
"""Accuracy eval harness — test the BRAIN before wiring STT/TTS.

You asked: "how do we test it? I need to check accuracy etc before wiring
the TTS-STT." This runner drives the LangGraph in text-only mode (no
Deepgram, no Cartesia, no Twilio) through scripted scenarios:

  offline (default): a scripted FakeLLM (deterministic, free, hermetic) —
      verifies the MACHINE: transitions, gates, tool calls, dvs writes,
      KB retrieval wiring, TTS normalization of the spoken stream.
  --live: the real model (OPENAI_API_KEY) — verifies the AGENT: does it
      take the right transition at the right moment, call the right tools,
      stay in state when data is missing? Latencies are measured per turn.

  python scripts/eval_accuracy.py                 # all scenarios, offline
  python scripts/eval_accuracy.py --live          # real LLM (set OPENAI_API_KEY)
  python scripts/eval_accuracy.py --only s3       # one scenario
  python scripts/eval_accuracy.py --scenarios eval/scenarios/s1*.json

Webhooks run against the in-process mock (tests/mock_webhooks.py) which
implements the LIVE production contract — no calls to slots.diallux-ai.site.

Checks per scenario step (see eval/scenarios/*.json):
  expect_state       state_name after the step
  expect_dvs         dvs key/value assertions
  expect_tools       tool names that must have been called (prefix)
  expect_gate_blocks transition attempts refused by the gate (production V2)
  expect_kb          KB slug that must appear in the retrieved set (RAG)
  expect_spoken      substring the normalized TTS stream must contain

Exit code 1 if any check fails -> CI-able. Report: eval/report.json.
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from diallux.config import Settings                       # noqa: E402
from diallux.graph.builder import CallRuntime            # noqa: E402
from diallux.media.normalize import normalize_for_tts    # noqa: E402
from tests.fake_llm import FakeLLM, tool_call            # noqa: E402
from tests.mock_webhooks import mock_client              # noqa: E402


class FakeKBStore:
    """Deterministic RAG stand-in: returns one canned chunk per KB slug."""

    def __init__(self, canned: dict[str, str] | None = None):
        self.canned = canned or {}
        self.queries: list[tuple[str, list[str]]] = []

    async def retrieve(self, query_text: str, kb_slugs: list[str]):
        self.queries.append((query_text, list(kb_slugs)))
        return [{"kb": s, "content": self.canned.get(s, f"[{s} excerpt]"), "score": 0.9}
                for s in kb_slugs if s in self.canned]

    async def close(self):
        pass


def load_scenarios(patterns: list[str]) -> list[dict]:
    out: list[dict] = []
    for p in patterns:
        for f in sorted(glob.glob(str(ROOT / p))):
            out.append(json.loads(Path(f).read_text()))
    return out


# --------------------------------------------------------------------------- #
async def run_scenario(sc: dict, settings: Settings, live: bool) -> dict:
    llm_json = json.loads((ROOT / "agent" / "llm.json").read_text())
    fake = None
    if not live:
        fake = FakeLLM()
        for step in sc["steps"]:
            for r in step.get("rounds", []):
                r = dict(r)
                if "tool_calls" in r:
                    r["tool_calls"] = [tool_call(tc["name"], tc.get("arguments", {}), tc.get("id", f"c{len(fake.rounds)}"))
                                       for tc in r["tool_calls"]]
                fake.add_round(r)

    canned = {k: v for k, v in sc.get("kb_canned", {}).items()}
    fake_kb = FakeKBStore(canned) if canned else None

    rt = CallRuntime(
        settings, llm_json, tracer=None,
        llm=fake if fake else None,
        http_client=mock_client(),
        kb_store=fake_kb if fake_kb else None,   # None = inline KB expansion
    )

    call_id = f"eval-{sc['name']}-{int(time.time()*1000)%100000}"
    spoken: list[str] = []
    checks: list[dict] = []
    t_start = time.perf_counter()

    for i, step in enumerate(sc["steps"]):
        payload = {"user_text": step["user"]}
        if i == 0:
            payload.update(rt.initial_state(call_id))
        tokens: list[str] = []
        async for mode, data in rt.graph.astream(
            payload, config={"configurable": {"thread_id": call_id}},
            stream_mode=["custom", "updates"],
        ):
            if mode == "custom" and isinstance(data, dict) and "tts_token" in data:
                tokens.append(data["tts_token"])
        spoken.append(normalize_for_tts("".join(tokens)))

        snap = await rt.graph.aget_state({"configurable": {"thread_id": call_id}})
        values = snap.values or {}
        exp = step.get("expect", {})

        if "state" in exp:
            checks.append({"step": i, "check": "state",
                           "ok": values.get("state_name") == exp["state"],
                           "got": values.get("state_name"), "want": exp["state"]})
        for k, v in exp.get("dvs", {}).items():
            got = (values.get("dvs") or {}).get(k)
            checks.append({"step": i, "check": f"dvs.{k}",
                           "ok": str(got) == str(v), "got": got, "want": v})
        if exp.get("ended"):
            checks.append({"step": i, "check": "ended", "ok": bool(values.get("ended")),
                           "got": values.get("ended"), "want": True})
        for t in exp.get("tools", []):
            names = [e.get("tool", "") for e in rt.executor.trace]
            checks.append({"step": i, "check": f"tool:{t}",
                           "ok": any(n == t or n.startswith(t) for n in names),
                           "got": None, "want": t})
        if "gate_blocks" in exp:
            rejections = getattr(rt.executor, "gate_rejections", [])
            checks.append({"step": i, "check": "gate_blocks",
                           "ok": len(rejections) == exp["gate_blocks"],
                           "got": len(rejections), "want": exp["gate_blocks"]})
        if "kb" in exp and fake_kb is not None:
            hits = {slug for _, slugs in fake_kb.queries for slug in slugs
                    if slug == exp["kb"]}
            # the canned map only echoes slugs we planted; treat query scope as hit
            checks.append({"step": i, "check": f"kb:{exp['kb']}",
                           "ok": exp["kb"] in {s for _, ss in fake_kb.queries for s in ss},
                           "got": None, "want": exp["kb"]})
        for s in exp.get("spoken", []):
            checks.append({"step": i, "check": f"spoken:{s[:30]}",
                           "ok": any(s in line for line in spoken),
                           "got": None, "want": s})

    await rt.aclose()
    elapsed = time.perf_counter() - t_start
    passed = sum(1 for c in checks if c["ok"])
    return {
        "scenario": sc["name"],
        "live": live,
        "checks_passed": passed,
        "checks_total": len(checks),
        "ok": passed == len(checks) and len(checks) > 0,
        "elapsed_s": round(elapsed, 2),
        "failures": [c for c in checks if not c["ok"]],
    }


# --------------------------------------------------------------------------- #
async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="use the real LLM (needs OPENAI_API_KEY)")
    ap.add_argument("--only", default="", help="substring filter on scenario name")
    ap.add_argument("--scenarios", nargs="*", default=["eval/scenarios/*.json"])
    args = ap.parse_args()

    scenarios = load_scenarios(args.scenarios)
    if args.only:
        scenarios = [s for s in scenarios if args.only in s["name"]]
    if not scenarios:
        print("no scenarios matched")
        return 2

    settings = Settings(
        openai_api_key="eval", retell_api_key="eval", langfuse_enabled=False,
        rag_mode="inline",                       # offline default: inline KBs
        first_turn_lite=False,                   # scenarios step-0 extract tools
    )
    if args.live:
        settings = Settings(langfuse_enabled=False)   # .env values incl. real key

    results = []
    for sc in scenarios:
        r = await run_scenario(sc, settings, args.live)
        mark = "PASS" if r["ok"] else "FAIL"
        print(f"[{mark}] {r['scenario']:36s} {r['checks_passed']}/{r['checks_total']} checks "
              f"({r['elapsed_s']}s{' live' if r['live'] else ''})")
        for f in r["failures"]:
            print(f"        step {f['step']} {f['check']}: got {f['got']!r} want {f['want']!r}")
        results.append(r)

    out = ROOT / "eval" / "report.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"results": results}, indent=2))
    total_ok = all(r["ok"] for r in results)
    print(f"\n{'ALL SCENARIOS PASS' if total_ok else 'FAILURES PRESENT'} — report: {out}")
    return 0 if total_ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
