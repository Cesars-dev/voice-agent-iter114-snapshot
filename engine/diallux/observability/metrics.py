"""Prometheus metrics (V2) — exported at GET /metrics.

The latency budget as histograms, so Grafana (or `promtool`) can answer
"are we under 1200ms?" with a real p50/p95:

  diallux_turn_e2e_response_ms     user stopped speaking -> caller hears reply
  diallux_stt_eot_to_llm_first_ms  Deepgram EndOfTurn -> first LLM token
  diallux_llm_first_to_tts_first   first LLM token -> first Cartesia audio
  diallux_tts_to_audio_out_ms      Cartesia audio -> frame handed to Twilio
  diallux_turn_total_ms            EndOfTurn -> graph invocation finished
  diallux_llm_round_s              per LLM round
  diallux_calls_total, diallux_bargeins_total, diallux_bookings_total,
  diallux_gate_rejections_total    hard-gate refused transitions (V2)
"""
from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest

REGISTRY = CollectorRegistry()

TURN_E2E_MS = Histogram(
    "diallux_turn_e2e_response_ms", "User stopped speaking -> caller hears the reply",
    buckets=(200, 400, 600, 800, 1000, 1200, 1600, 2000, 3000), registry=REGISTRY)
STT_TO_LLM_FIRST_MS = Histogram(
    "diallux_stt_eot_to_llm_first_ms", "Deepgram EndOfTurn -> first LLM token",
    buckets=(50, 100, 200, 300, 500, 800, 1200), registry=REGISTRY)
LLM_FIRST_TO_TTS_MS = Histogram(
    "diallux_llm_first_to_tts_first_ms", "First LLM token -> first Cartesia audio chunk",
    buckets=(50, 100, 200, 300, 500, 800), registry=REGISTRY)
TTS_TO_AUDIO_OUT_MS = Histogram(
    "diallux_tts_to_audio_out_ms", "First Cartesia audio -> first Twilio media frame",
    buckets=(10, 25, 50, 100, 200, 400), registry=REGISTRY)
TURN_TOTAL_MS = Histogram(
    "diallux_turn_total_ms", "EndOfTurn -> graph invocation finished",
    buckets=(200, 500, 1000, 2000, 4000, 8000, 15000), registry=REGISTRY)
LLM_ROUND_S = Histogram(
    "diallux_llm_round_s", "LLM round duration",
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0), registry=REGISTRY)

MIC_CHUNK_INTERARRIVAL_MS = Histogram(
    "diallux_mic_chunk_interarrival_ms", "Mic WS chunk inter-arrival (nominal 50 ms/chunk)",
    buckets=(20, 35, 50, 65, 80, 100, 150, 200, 300, 500), registry=REGISTRY)

CALLS = Counter("diallux_calls_total", "Calls started", registry=REGISTRY)
BARGEINS = Counter("diallux_bargeins_total", "User barge-ins that interrupted the agent", registry=REGISTRY)
BOOKINGS = Counter("diallux_bookings_total", "Calls that reached a verified booking", registry=REGISTRY)
GATE_REJECTIONS = Counter("diallux_gate_rejections_total", "Hard-gate refused transitions", registry=REGISTRY)
TOOL_ERRORS = Counter("diallux_tool_errors_total", "Tool executions that returned ok=false", registry=REGISTRY)


def observe_turn_report(report: dict):
    if report.get("e2e_response_ms") is not None:
        TURN_E2E_MS.observe(report["e2e_response_ms"])
    if report.get("stt_eot_to_llm_first_ms") is not None:
        STT_TO_LLM_FIRST_MS.observe(report["stt_eot_to_llm_first_ms"])
    if report.get("llm_first_to_tts_first_ms") is not None:
        LLM_FIRST_TO_TTS_MS.observe(report["llm_first_to_tts_first_ms"])
    if report.get("tts_first_to_audio_out_ms") is not None:
        TTS_TO_AUDIO_OUT_MS.observe(report["tts_first_to_audio_out_ms"])
    if report.get("e2e_turn_ms") is not None:
        TURN_TOTAL_MS.observe(report["e2e_turn_ms"])


def render() -> bytes:
    return generate_latest(REGISTRY)
