"""iter98 B1 — OPENAI_SERVICE_TIER knob pins.

Pins:
  1. Settings default `openai_service_tier == ""` (flag OFF by default).
  2. Flag-off BYTE-PARITY (the merge gate):
     a. reasoning model (gpt-5.4, reasoning_effort="none", verbosity="medium",
        tier "") serializes to exactly the pre-iter98 model_kwargs bytes —
        {"reasoning_effort": "none", "verbosity": "medium"} — no tier key.
     b. non-reasoning model (gpt-4.1, tier "") carries NO model_kwargs at all
        (pre-iter98 non-reasoning builds never had one — the hoist must not
        introduce it).
  3. Tier ON: "service_tier" rides the SAME model_kwargs dict for the hot llm,
     the warm twin (_warm_llm inherits **kwargs), and the verbosity twin.
  4. Boot prewarm is NOT tiered: llm_boot_prewarm's request never carries
     service_tier even with the knob set (16 throwaway tokens at 2x price =
     waste; cross-tier cache read is preserved).
  5. /health exposes the resolved tier string.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from diallux.graph import llm as llm_mod
from diallux.graph.llm import StreamingLLM, llm_boot_prewarm, shared_llm_http_client


def _settings(**kw) -> Settings:
    base = dict(openai_api_key="test", langfuse_enabled=False)
    base.update(kw)
    return Settings(**base)


class _StubChat:
    """Captures constructor kwargs; never invoked at runtime."""
    kwargs_stack: list[dict] = []

    def __init__(self, **kwargs):
        _StubChat.kwargs_stack.append(kwargs)


def _capture(on: Settings, monkeypatch) -> dict:
    _StubChat.kwargs_stack = []
    monkeypatch.setattr(llm_mod, "ChatOpenAI", _StubChat)
    StreamingLLM(on)
    return _StubChat.kwargs_stack[-1]


# --------------------------------------------------------------------------- #
# 1. default
# --------------------------------------------------------------------------- #
def test_tier_default_empty():
    assert Settings(openai_api_key="test").openai_service_tier == ""


# --------------------------------------------------------------------------- #
# 2. byte-parity (flag off)
# --------------------------------------------------------------------------- #
def test_flagoff_reasoning_model_bytes_equal_pre_iter98(monkeypatch):
    """The pre-iter98 construction, spelled out EXACTLY as llm.py built it
    before the hoist (decision dict per the old code path), canonicalized to
    bytes and compared against the flag-off bytes of the new build."""
    s = _settings(openai_model="gpt-5.4", openai_service_tier="")
    kw = _capture(s, monkeypatch)
    mk = json.dumps(kw["model_kwargs"], sort_keys=True, separators=(",", ":"))
    # pre-change algorithm (verbatim intent of the old block):
    pre = {}
    if s.openai_reasoning_effort:
        pre["reasoning_effort"] = s.openai_reasoning_effort
    if s.openai_verbosity:
        pre["verbosity"] = s.openai_verbosity
    pre_bytes = json.dumps(pre, sort_keys=True, separators=(",", ":"))
    assert mk == pre_bytes == '{"reasoning_effort":"none","verbosity":"medium"}'
    assert "service_tier" not in kw["model_kwargs"]


def test_flagoff_nonreasoning_model_has_no_model_kwargs(monkeypatch):
    s = _settings(openai_model="gpt-4.1", openai_service_tier="")
    kw = _capture(s, monkeypatch)
    assert "model_kwargs" not in kw


def test_flagoff_warm_twin_inherits_no_tier(monkeypatch):
    """_warm_llm is constructed with the same kwargs = parity to main llm."""
    s = _settings(openai_model="gpt-5.4", openai_service_tier="")
    _StubChat.kwargs_stack = []
    monkeypatch.setattr(llm_mod, "ChatOpenAI", _StubChat)
    StreamingLLM(s)
    lls = _StubChat.kwargs_stack
    assert len(lls) >= 2  # main + warm
    a = json.dumps(lls[0]["model_kwargs"], sort_keys=True, separators=(",", ":"))
    b = json.dumps(lls[-1]["model_kwargs"], sort_keys=True, separators=(",", ":"))
    assert a == b


# --------------------------------------------------------------------------- #
# 3. tier on
# --------------------------------------------------------------------------- #
def test_tier_on_hot_llm(monkeypatch):
    s = _settings(openai_model="gpt-5.4", openai_service_tier="fast")
    kw = _capture(s, monkeypatch)
    assert kw["model_kwargs"]["service_tier"] == "fast"
    # nothing else moved
    assert kw["model_kwargs"]["reasoning_effort"] == "none"
    assert kw["model_kwargs"]["verbosity"] == "medium"


def test_tier_on_warm_twin(monkeypatch):
    s = _settings(openai_model="gpt-5.4", openai_service_tier="fast")
    _StubChat.kwargs_stack = []
    monkeypatch.setattr(llm_mod, "ChatOpenAI", _StubChat)
    StreamingLLM(s)
    lls = _StubChat.kwargs_stack
    warm = next(k for k in lls if k.get("max_completion_tokens") == s.prewarm_max_completion_tokens)
    assert warm["model_kwargs"]["service_tier"] == "fast"


def test_tier_on_verbosity_twin(monkeypatch):
    s = _settings(openai_model="gpt-5.4", openai_service_tier="fast",
                  openai_verbosity="medium")
    _StubChat.kwargs_stack = []
    monkeypatch.setattr(llm_mod, "ChatOpenAI", _StubChat)
    sl = StreamingLLM(s)
    sl._verbosity_llms = {"low": sl._llm}
    sl.llm_for_verbosity("high")
    twin = _StubChat.kwargs_stack[-1]
    assert twin["model_kwargs"]["service_tier"] == "fast"
    assert twin["model_kwargs"]["verbosity"] == "high"


# --------------------------------------------------------------------------- #
# 4. boot prewarm NOT tiered
# --------------------------------------------------------------------------- #
def test_boot_prewarm_never_tiered(monkeypatch):
    s = _settings(openai_model="gpt-5.4", openai_service_tier="fast")
    _StubChat.kwargs_stack = []
    monkeypatch.setattr(llm_mod, "ChatOpenAI", _StubChat)
    asyncio.run(llm_boot_prewarm(s))
    kw = _StubChat.kwargs_stack[-1]
    assert "model_kwargs" not in kw or "service_tier" not in kw.get("model_kwargs", {})
    assert kw["http_async_client"] is shared_llm_http_client()


# --------------------------------------------------------------------------- #
# 5. /health field
# --------------------------------------------------------------------------- #
def test_health_carries_service_tier(monkeypatch):
    import diallux.app as app_mod

    async def go():
        monkeypatch.setattr(app_mod, "get_settings", lambda: _settings(openai_service_tier="fast"))
        h = await app_mod.health()
        assert h["service_tier"] == "fast"
        monkeypatch.setattr(app_mod, "get_settings", lambda: _settings(openai_service_tier=""))
        h = await app_mod.health()
        assert h["service_tier"] == ""

    asyncio.run(go())
