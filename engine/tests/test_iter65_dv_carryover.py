"""iter65 T3 — dv carryover DAILY pins (daily→weekly ×5, ×5 in code).

  1. calculate_monthly_leak: daily 4 · 50% · $650 → weekly $6,500;
     result + dvs_patch carry the resolved missed_calls_weekly (20).
  2. Explicit weekly WINS over daily.
  3. leak_inputs_present is daily-aware (daily-only → True).
  4. missed_calls_daily survives DynamicVariables.from_flat (schema write).
  5. Deterministic auto-fire patches weekly_leak + missed_calls_weekly
     from daily-only capture.
  6. JS (llm.json calculate_monthly_leak) parity with the Python port.
  7. Retell validator math parity on the daily case (4·50%·650 → $6,500).
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import math
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from diallux.config import Settings
from diallux.graph.builder import CallRuntime
from diallux.graph.tools import calculate_monthly_leak, leak_inputs_present
from diallux.schema import DynamicVariables
from tests.fake_llm import FakeLLM
from tests.mock_webhooks import mock_client

ROOT = Path(__file__).resolve().parents[1]
LLM_JSON = json.loads((ROOT / "agent" / "llm.json").read_text())
VALIDATE_PY = Path("/home/julio/projects/Retell_AI_MCP_connection/"
                   "validator_endpoint/validate.py")

SETTINGS = Settings(openai_api_key="test", retell_api_key="test",
                    langfuse_enabled=False, rag_min_query_chars=0)


def make_rt(rounds: list[dict], settings: Settings | None = None):
    fake = FakeLLM(rounds)
    rt = CallRuntime(settings or SETTINGS, LLM_JSON, tracer=None, llm=fake,
                     http_client=mock_client(), kb_store=None)
    return rt, fake


def run_round(rt: CallRuntime, call_id: str, user_text: str,
              state: str = "Closer", persona_dvs: dict | None = None):
    tokens: list[str] = []

    async def go():
        config = {"configurable": {"thread_id": call_id}}
        payload = rt.initial_state(call_id, persona_dvs)
        payload["state_name"] = state
        payload["user_text"] = user_text
        async for mode, d in rt.graph.astream(payload, config=config,
                                              stream_mode=["updates", "custom"]):
            if mode == "custom" and isinstance(d, dict) and "tts_token" in d:
                tokens.append(d["tts_token"])
        return (await rt.graph.aget_state(config)).values

    return asyncio.run(go()), tokens


# --------------------------------------------------------------------------- #
# tools.py math
# --------------------------------------------------------------------------- #
def test_daily_to_weekly_math_and_patch():
    """daily 4 · 50% · $650 → weekly 20 · 50% · 650 = $6,500."""
    result, patch = calculate_monthly_leak({
        "missed_calls_daily": 4, "close_rate_pct": 50, "avg_job_value": 650})
    assert result["ok"] is True
    assert result["weekly_leak"] == "6,500"
    assert result["missed_calls_weekly"] == 20
    assert result["inputs_used"]["missed_calls_daily"] == 4
    assert patch["weekly_leak"] == "6,500"
    assert patch["missed_calls_weekly"] == 20
    assert "monthly_leak" in patch


def test_explicit_weekly_wins():
    """weekly 10 + daily 8 → the explicit weekly drives the math."""
    result, patch = calculate_monthly_leak({
        "missed_calls_weekly": 10, "missed_calls_daily": 8,
        "close_rate_pct": 50, "avg_job_value": 650})
    assert result["ok"] is True
    assert result["weekly_leak"] == f"{math.floor(10 * 0.5 * 650):,}"
    assert "missed_calls_weekly" not in patch   # weekly given, no patch needed


def test_leak_inputs_present_daily_aware():
    assert leak_inputs_present({"missed_calls_daily": 4,
                                "close_rate_pct": 50, "avg_job_value": 650})
    assert leak_inputs_present({"missed_calls_weekly": 20,
                                "close_rate_pct": 50, "avg_job_value": 650})
    assert not leak_inputs_present({"close_rate_pct": 50, "avg_job_value": 650})


# --------------------------------------------------------------------------- #
# schema
# --------------------------------------------------------------------------- #
def test_schema_daily_survives_from_flat():
    dvs = DynamicVariables.from_flat({"missed_calls_daily": "4",
                                      "close_rate_pct": "50"})
    assert dvs.missed_calls_daily == 4          # coerced digit string
    assert dvs.close_rate_pct == 50


# --------------------------------------------------------------------------- #
# deterministic auto-fire (builder) with daily-only capture
# --------------------------------------------------------------------------- #
def test_auto_fire_daily_only_patches_weekly():
    """A capture round (extract_leak_inputs with daily-only numbers) routes
    through the deterministic node → leak math fires → dvs carry
    weekly_leak + the resolved missed_calls_weekly."""
    persona = {"missed_calls_daily": "4", "close_rate_pct": "50",
               "avg_job_value": "650"}
    capture = {"id": "c1", "name": "extract_leak_inputs", "arguments": json.dumps({
        "missed_calls_daily": "4", "close_rate_pct": "50", "avg_job_value": "650"})}
    rt, _ = make_rt([
        {"tokens": ["Let me", " run that", " math", "."], "tool_calls": [capture]},
        {"tokens": ["That's about", " 6,500 a week", "."]},
    ])
    state, _ = run_round(rt, "dv65-1", "yeah", persona_dvs=persona)
    dvs = state.get("dvs") or {}
    assert dvs.get("weekly_leak") == "6,500"
    assert dvs.get("missed_calls_weekly") == 20


# --------------------------------------------------------------------------- #
# JS parity (llm.json calculate_monthly_leak code)
# --------------------------------------------------------------------------- #
def _run_js(dv: dict) -> dict:
    code = None
    for s in LLM_JSON["states"]:
        for t in s.get("tools", []):
            if t.get("name") == "calculate_monthly_leak":
                code = t["code"]
    assert code is not None
    payload = json.dumps({"code": code, "dv": dv})
    js = ("const payload = %s;\n"
          "const fn = new Function('dv', payload.code + '\\n');\n"
          "console.log(JSON.stringify(fn(payload.dv)));") % payload
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True,
                         timeout=15)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_js_parity_daily_case():
    js = _run_js({"missed_calls_daily": "4", "close_rate_pct": "50",
                  "avg_job_value": "650"})
    py, patch = calculate_monthly_leak({"missed_calls_daily": "4",
                                        "close_rate_pct": "50",
                                        "avg_job_value": "650"})
    assert js["ok"] is True
    assert js["weekly_leak"] == py["weekly_leak"] == "6,500"
    assert js["monthly_leak"] == py["monthly_leak"]
    assert int(js["missed_calls_weekly"]) == patch["missed_calls_weekly"] == 20


def test_js_parity_weekly_wins():
    js = _run_js({"missed_calls_weekly": "10", "missed_calls_daily": "8",
                  "close_rate_pct": "50", "avg_job_value": "650"})
    py, _ = calculate_monthly_leak({"missed_calls_weekly": 10,
                                    "missed_calls_daily": 8,
                                    "close_rate_pct": 50, "avg_job_value": 650})
    assert js["weekly_leak"] == py["weekly_leak"]
    assert "missed_calls_weekly" not in js or js["ok"] is True


# --------------------------------------------------------------------------- #
# Retell validator parity (cross-repo, additive)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not VALIDATE_PY.exists(), reason="Retell repo not present")
def test_validator_parity_daily_case():
    spec = importlib.util.spec_from_file_location("iter65_validate", VALIDATE_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    res = mod.validate_lead({
        "first_name": "Sam", "selected_time": "2:00 pm",
        "slot_options": "2:00 pm|3:00 pm",
        "missed_calls_daily": 4, "close_rate_percent": 50,
        "avg_job_value": 650, "callback_number": "+13125550123",
        "prospect_timezone": "America/Chicago"})
    assert res["ok"] is True
    assert res["weekly_leak"] == "$6,500"
    assert res["inputs_used"]["missed_calls_per_week"] == 20
