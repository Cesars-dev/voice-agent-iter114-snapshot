"""iter33 OTEL tail-loss regression — blocking tail confirm in Tracer.finish().

Hermetic: FakeLLM + mock webhooks, no live keys. The "server" is a mocked
_tail_observation_count (same contract as the Langfuse public API count).

Covers:
  1. 3-turn chain run ending end_call produces the tail shape the confirm
     protects (chain_done + booking tools + Closing/ended).
  2. wait_for_tail returns once the count stops growing (2 equal polls).
  3. finish() invokes the confirm path (no manual sleep needed by callers).
  4. fail-open: disabled / unknown trace never raises, never blocks.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from diallux.graph.builder import CallRuntime
from diallux.observability.tracer import Tracer
from tests.fake_llm import FakeLLM, tool_call
from tests.mock_webhooks import mock_client, BOOKING_UID

ROOT = Path(__file__).resolve().parents[1]
LLM_JSON = json.loads((ROOT / "agent" / "llm.json").read_text())

PERSONA = {"first_name": "Maria", "last_name": "Gonzales",
           "company_name": "Bright Smile Dental", "prospect_timezone": "America/Chicago",
           "callback_number": "+13124001234"}


def make_stub_tracer(counts: list[int]) -> Tracer:
    """Tracer with no SDK/server: only the confirm logic is live."""
    tr = Tracer.__new__(Tracer)
    tr.enabled = True
    tr.lf = object()  # truthy stand-in for "exporter present"
    tr.root = None
    tr.trace_id = "tail-test-trace"
    tr.session_id = "tail-test-session"
    tr._meta = {}
    tr._started = 0
    tr.tail_ok = None
    tr.tail_seen = -1
    it = iter(counts)

    def fake_count() -> int:
        try:
            return next(it)
        except StopIteration:
            return counts[-1]

    tr._tail_observation_count = fake_count  # type: ignore[method-assign]
    return tr


# --------------------------------------------------------------------------- #
def test_chain_3turn_end_call_tail_shape():
    """3-turn hermetic chain run ending end_call: the tail finish() protects."""
    fake = FakeLLM()
    fake.add_round({"tokens": [], "tool_calls": [
        tool_call("extract_confirm_details", {"selected_time": "2026-09-04T14:30:00"}),
        tool_call("validate_lead", {"first_name": "Maria", "last_name": "Gonzales",
                                     "company_name": "Bright Smile Dental",
                                     "callback_number": "+13124001234",
                                     "prospect_timezone": "America/Chicago",
                                     "selected_time": "2026-09-04T14:30:00"}),
        tool_call("transition_to_VerifyLead", {"slot_verified": True}, "t1"),
    ]})
    fake.add_round({"tokens": ["You", "'re", " booked", "."]})
    fake.add_round({"tokens": ["Bye", "!"], "tool_calls": [tool_call("end_call", {})]})

    settings = Settings(openai_api_key="test", retell_api_key="test", langfuse_enabled=False)
    rt = CallRuntime(settings, LLM_JSON, tracer=None, llm=fake, http_client=mock_client())

    async def go():
        config = {"configurable": {"thread_id": "tail1"}}
        for i, text in enumerate(["tomorrow works", "thanks"]):
            payload = {"user_text": text}
            if i == 0:
                payload.update(rt.initial_state("tail1", persona_dvs=PERSONA))
                payload["state_name"] = "ConfirmSlots"
            async for _m, _d in rt.graph.astream(payload, config=config, stream_mode=["updates"]):
                pass
        return (await rt.graph.aget_state(config)).values

    state = asyncio.run(go())
    assert state["state_name"] == "Closing"
    assert state["ended"] is True
    assert state["dvs"]["booking_uid"] == BOOKING_UID
    assert state["dvs"]["chain_done"] is True
    names = [e["tool"] for e in rt.executor.trace]
    for step in ("verify_lead_data", "create_livecall_booking",
                 "record_booking_uid", "transition_to_Closing"):
        assert step in names


def test_wait_for_tail_returns_when_stable():
    tr = make_stub_tracer([0, 2, 5, 5])
    t0 = time.monotonic()
    assert tr.wait_for_tail(timeout_s=60.0, poll_s=0.01) == 5
    assert tr.tail_ok is True  # census 0: anything that lands is complete
    assert time.monotonic() - t0 < 30.0


def test_wait_for_tail_census_completeness():
    """Server count vs LOCAL census: quiescent-but-short is NOT complete."""
    tr = make_stub_tracer([3, 5, 5])
    tr._started = 5  # 5 observations created locally (root + 4 children)
    assert tr.wait_for_tail(timeout_s=60.0, poll_s=0.01) == 5
    assert tr.tail_ok is True


def test_wait_for_tail_shortfall_flags_loud():
    """Stable below census -> tail_ok False (the Gene/Frank battery shape)."""
    tr = make_stub_tracer([8, 8, 8, 8, 8])
    tr._started = 12
    t0 = time.monotonic()
    assert tr.wait_for_tail(timeout_s=60.0, poll_s=0.01) == 8
    assert tr.tail_ok is False
    assert time.monotonic() - t0 < 30.0


def test_finish_invokes_confirm_path():
    tr = make_stub_tracer([3, 3])
    calls: list[str] = []
    orig = tr.wait_for_tail

    def spy(timeout_s: float = 60.0, poll_s: float = 5.0) -> int:
        calls.append("wait_for_tail")
        return orig(timeout_s=timeout_s, poll_s=0.01)

    tr.wait_for_tail = spy  # type: ignore[method-assign]

    class FakeRoot:
        def update(self, **_kw):
            pass

        def end(self):
            calls.append("root.end")

    class FakeLF:
        def flush(self):
            calls.append("flush")

    tr.root = FakeRoot()  # type: ignore[assignment]
    tr.lf = FakeLF()  # type: ignore[assignment]
    tr.finish(output={"tail": "ok"})
    assert calls[0] == "root.end"
    assert "flush" in calls
    assert "wait_for_tail" in calls  # confirm path invoked, no caller sleep needed


def test_tail_confirm_fail_open():
    tr = Tracer.__new__(Tracer)
    tr.enabled = False
    tr.lf = None
    tr.root = None
    tr.trace_id = None
    tr.session_id = "off"
    tr._meta = {}
    tr._started = 0
    tr.tail_ok = None
    tr.tail_seen = -1
    t0 = time.monotonic()
    assert tr.wait_for_tail(timeout_s=60.0, poll_s=0.01) == -1
    assert tr.tail_ok is None
    assert time.monotonic() - t0 < 5.0
    tr.finish(output={"x": 1})  # must not raise with root=None
