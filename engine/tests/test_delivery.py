"""Per-state delivery profiles — hermetic units (V5).

Covers:
  - the table is COMPLETE: every deployed state in agent/llm.json + "begin"
    has a profile (a new state without one fails here — keep the table full);
  - every value sits inside the documented provider ranges (a miscalibrated
    table can never 400 a live request);
  - lookup semantics: unknown/None state -> DEFAULT -> pure .env behavior;
  - resolve_delivery: None when TTS_DELIVERY_PROFILES=false, provider-correct
    keys when on, {} for all-None profiles;
  - Cartesia: overrides merge OVER the .env globals into generation_config,
    and out-of-range values are clamped;
  - ElevenLabs: voice_settings ride the FIRST text message of each new
    context (and only that one), merged over the .env base;
  - CallSession._speak_chunk passes the resolved profile of the turn's state.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from diallux.media.delivery import (
    DEFAULT_PROFILE,
    DELIVERY_PROFILES,
    profile_for,
    provider_overrides,
    resolve_delivery,
)

BASE = dict(openai_api_key="t", retell_api_key="t", langfuse_enabled=False)


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# table completeness + ranges
# --------------------------------------------------------------------------- #
def test_table_covers_every_deployed_state_plus_begin():
    llm_json = json.loads((Path(__file__).resolve().parents[1] / "agent" / "llm.json").read_text())
    deployed = {s.get("name") for s in llm_json["states"]}   # states is a LIST
    assert deployed <= set(DELIVERY_PROFILES.keys()), (
        f"states without a delivery profile: {deployed - set(DELIVERY_PROFILES.keys())}")
    assert "begin" in DELIVERY_PROFILES          # the begin_message greeting


def test_all_values_within_documented_ranges():
    for name, prof in DELIVERY_PROFILES.items():
        c = prof.get("cartesia") or {}
        e = prof.get("elevenlabs") or {}
        for f, v in c.items():
            if v is None or v == "":
                continue
            if f == "speed":
                assert 0.6 <= v <= 1.5, (name, f, v)
            elif f == "volume":
                assert 0.5 <= v <= 2.0, (name, f, v)
            elif f == "emotion":
                assert isinstance(v, str) and v, (name, f, v)
        for f, v in e.items():
            if v is None:
                continue
            if f in ("stability", "style"):
                assert 0.0 <= v <= 1.0, (name, f, v)
            elif f == "speed":
                assert 0.7 <= v <= 1.2, (name, f, v)


def test_emotion_used_sparingly():
    with_emotion = [k for k, p in DELIVERY_PROFILES.items()
                    if (p.get("cartesia") or {}).get("emotion")]
    assert len(with_emotion) <= 5, "emotion is salt, not sauce: max a few states"


# --------------------------------------------------------------------------- #
# lookup + resolve semantics
# --------------------------------------------------------------------------- #
def test_unknown_or_none_state_falls_back_to_default():
    assert profile_for(None) is DEFAULT_PROFILE
    assert profile_for("NotAState") is DEFAULT_PROFILE
    # all-None fields -> provider_overrides yields {} -> pure .env behavior
    assert provider_overrides(DEFAULT_PROFILE, "cartesia") == {}
    assert provider_overrides(DEFAULT_PROFILE, "elevenlabs") == {}


def test_resolve_delivery_off_returns_none():
    s = Settings(**BASE, tts_delivery_profiles=False)
    assert resolve_delivery(s, "Intake") is None


def test_resolve_delivery_cartesia_keys():
    s = Settings(**BASE, tts_provider="cartesia") # iter66: provider default is EL now; pin cartesia profile explicitly
    assert resolve_delivery(s, "Intake") == {"speed": 1.12, "emotion": "happy"}
    assert resolve_delivery(s, "contact_details") == {"speed": 0.99, "emotion": "calm"}


def test_resolve_delivery_elevenlabs_keys():
    s = Settings(**BASE, tts_provider="elevenlabs")
    ov = resolve_delivery(s, "Offer")
    assert ov == {"stability": 0.45, "style": 0.35, "speed": 1.05}


def test_provider_overrides_clamps_out_of_range():
    hot = {"cartesia": {"speed": 3.0}, "elevenlabs": {"speed": 0.1}}
    assert provider_overrides(hot, "cartesia") == {"speed": 1.5}
    assert provider_overrides(hot, "elevenlabs") == {"speed": 0.7}


# --------------------------------------------------------------------------- #
# Cartesia merge (FakeWS seam, same as test_tts_providers)
# --------------------------------------------------------------------------- #
class FakeWS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, raw: str):
        self.sent.append(json.loads(raw))

    async def close(self):
        pass


def test_cartesia_overrides_win_over_env_globals():
    from diallux.media.cartesia_tts import CartesiaTTS
    s = Settings(**BASE, cartesia_speed=1.0, cartesia_emotion="calm")
    tts = CartesiaTTS(s, on_audio=None)
    ws = FakeWS()
    tts._ws = ws

    async def go():
        await tts.speak("ctx", "Hi.", continue_=True,
                        overrides={"speed": 0.99, "emotion": "calm"})
    _run(go())
    # profile speed wins over the .env 1.0; unset volume stays unset
    assert ws.sent[0]["generation_config"] == {"speed": 0.99, "emotion": "calm"}


def test_cartesia_overrides_clamped_and_env_volume_survives():
    from diallux.media.cartesia_tts import CartesiaTTS
    s = Settings(**BASE, cartesia_speed=1.0, cartesia_volume=1.2)
    tts = CartesiaTTS(s, on_audio=None)
    ws = FakeWS()
    tts._ws = ws

    async def go():
        await tts.speak("ctx", "Hi.", continue_=True, overrides={"speed": 9.9})
    _run(go())
    assert ws.sent[0]["generation_config"] == {"speed": 1.5, "volume": 1.2}


def test_cartesia_no_overrides_is_exact_v4_behavior():
    from diallux.media.cartesia_tts import CartesiaTTS
    s = Settings(**BASE, cartesia_speed=1.05, cartesia_volume=1.0, cartesia_emotion="calm")
    tts = CartesiaTTS(s, on_audio=None)
    ws = FakeWS()
    tts._ws = ws

    async def go():
        await tts.speak("ctx", "Hi.", continue_=True, overrides=None)
    _run(go())
    assert ws.sent[0]["generation_config"] == {"speed": 1.05, "volume": 1.0, "emotion": "calm"}


# --------------------------------------------------------------------------- #
# ElevenLabs voice_settings on the first message of each new context
# --------------------------------------------------------------------------- #
def test_elevenlabs_voice_settings_first_message_per_context():
    from diallux.media.elevenlabs_tts import ElevenLabsTTS
    s = Settings(**BASE, elevenlabs_stability=0.55, elevenlabs_style=0.15,
                 elevenlabs_similarity_boost=0.8, elevenlabs_speed=1.0)
    tts = ElevenLabsTTS(s, on_audio=None)
    ws = FakeWS()
    tts._ws = ws

    async def go():
        await tts.speak("ctx-A", "Hello ", True,
                        overrides={"stability": 0.7, "style": 0.05, "speed": 0.95})
        await tts.speak("ctx-A", "there.", False)      # same ctx: no repeat
        await tts.speak("ctx-B", "Next turn", False,
                        overrides={"stability": 0.5, "style": 0.3, "speed": 1.02})
    _run(go())
    # first message of ctx-A carries merged settings (iter66: + context_id + flush)
    m0 = ws.sent[0]
    assert m0["text"] == "Hello "
    assert m0["context_id"] == "ctx-A"
    assert m0["flush"] is True
    assert m0["voice_settings"]["stability"] == 0.7
    assert m0["voice_settings"]["style"] == 0.05
    assert m0["voice_settings"]["speed"] == 0.95
    # base fields the profile doesn't touch ride along from .env
    assert m0["voice_settings"]["similarity_boost"] == 0.8
    assert m0["voice_settings"]["use_speaker_boost"] is True
    # second chunk of the same context: plain text (no repeat settings)
    assert "voice_settings" not in ws.sent[1]
    # end-of-turn ctx-A: flush chunk then close_context (sent[2])
    assert ws.sent[2] == {"context_id": "ctx-A", "close_context": True}
    # first message of ctx-B carries ITS settings (sent[3])
    m2 = ws.sent[3]
    assert m2["text"] == "Next turn"
    assert m2["context_id"] == "ctx-B"
    assert m2["flush"] is True
    assert m2["voice_settings"]["stability"] == 0.5
    assert m2["voice_settings"]["speed"] == 1.02
    # then ctx-B's close_context (sent[4])
    assert ws.sent[4] == {"context_id": "ctx-B", "close_context": True}


def test_elevenlabs_no_overrides_keeps_plain_messages():
    from diallux.media.elevenlabs_tts import ElevenLabsTTS
    s = Settings(**BASE)
    tts = ElevenLabsTTS(s, on_audio=None)
    ws = FakeWS()
    tts._ws = ws

    async def go():
        await tts.speak("ctx", "Hi.", True, overrides=None)
    _run(go())
    assert ws.sent == [{"text": "Hi.", "context_id": "ctx", "flush": True}]
    assert "voice_settings" not in ws.sent[0]


def test_elevenlabs_merged_settings_clamped():
    from diallux.media.elevenlabs_tts import ElevenLabsTTS
    s = Settings(**BASE)
    tts = ElevenLabsTTS(s, on_audio=None)
    merged = tts._merged_voice_settings({"stability": 2.0, "style": -1.0, "speed": 9.0})
    assert merged["stability"] == 1.0
    assert merged["style"] == 0.0
    assert merged["speed"] == 1.2


# --------------------------------------------------------------------------- #
# CallSession seam: the turn's state drives what the TTS receives
# --------------------------------------------------------------------------- #
class FakeTTS:
    def __init__(self):
        self.calls: list[tuple] = []

    async def speak(self, ctx, text, continue_, overrides=None):
        self.calls.append((ctx, text, continue_, overrides))


def test_session_speak_chunk_passes_turn_state_profile():
    from diallux.media.session import CallSession
    sess = object.__new__(CallSession)          # only the attrs _speak_chunk uses
    sess.settings = Settings(**BASE, tts_provider="cartesia")  # profiles on, cartesia
    sess.tts = FakeTTS()
    sess._tts_context = "ctx-1"
    sess._turn_state = "contact_details"
    sess.tts_normalize = False
    sess.tts_phone_style = "digits"

    _run(sess._speak_chunk("My number is six five one.", True))
    ctx, text, cont, overrides = sess.tts.calls[0]
    assert overrides == {"speed": 0.99, "emotion": "calm"}   # slow+calm for digits

    sess._turn_state = "Offer"
    _run(sess._speak_chunk("Let me tell you what we do.", True))
    assert sess.tts.calls[1][3] == {"speed": 1.15}
