"""TTS provider tests — hermetic fake WebSockets.

- Cartesia: generation_config (speed/volume/emotion) rides every request;
  contexts/cancel semantics unchanged.
- ElevenLabs: stream-input message sequence (text chunks + flush), voice
  settings on the first message, ulaw_8000 + model in the URL, barge-in
  drops cancelled-context audio.
- Factory: provider switch by settings.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from diallux.media.tts_factory import create_tts, tts_label


BASE = dict(openai_api_key="t", retell_api_key="t", langfuse_enabled=False,
            audio_transport="twilio",   # hermetic: the working .env may set browser
            )


class FakeWS:
    """websockets stand-in: records sends, replays canned receives."""

    def __init__(self, canned: list[dict] | None = None):
        self.sent: list[dict] = []
        self.canned = canned or []
        self.closed = False

    async def send(self, raw: str):
        self.sent.append(json.loads(raw))

    async def recv(self):
        if self.canned:
            import copy
            return json.dumps(copy.deepcopy(self.canned.pop(0)))
        # block forever-ish; recv loop will be cancelled on close
        await asyncio.sleep(30)
        return "{}"

    async def close(self):
        self.closed = True


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Cartesia
# --------------------------------------------------------------------------- #
def test_cartesia_generation_config_sent_every_request():
    from diallux.media.cartesia_tts import CartesiaTTS
    s = Settings(**BASE, cartesia_speed=1.12, cartesia_volume=1.0,
                 cartesia_emotion="calm")
    tts = CartesiaTTS(s, on_audio=None)
    ws = FakeWS()
    tts._ws = ws

    async def go():
        await tts.speak("ctx-1", "Hello there.", continue_=True)
        await tts.speak("ctx-1", "Bye.", continue_=False)
    _run(go())
    assert len(ws.sent) == 2
    for msg in ws.sent:
        assert msg["generation_config"] == {"speed": 1.12, "volume": 1.0, "emotion": "calm"}
        assert msg["output_format"]["encoding"] == "pcm_mulaw"
        assert msg["output_format"]["sample_rate"] == 8000


def test_cartesia_generation_config_omitted_when_unset():
    from diallux.media.cartesia_tts import CartesiaTTS
    s = Settings(**BASE, cartesia_speed=None)   # pinned: working .env sets 1.05 (Linda)
    tts = CartesiaTTS(s, on_audio=None)
    ws = FakeWS()
    tts._ws = ws

    async def go():
        await tts.speak("ctx-1", "Hi.", continue_=False)
    _run(go())
    assert "generation_config" not in ws.sent[0]


def test_cartesia_cancel_frame():
    from diallux.media.cartesia_tts import CartesiaTTS
    tts = CartesiaTTS(Settings(**BASE), on_audio=None)
    ws = FakeWS()
    tts._ws = ws

    async def go():
        await tts.cancel("ctx-9")
    _run(go())
    assert ws.sent == [{"context_id": "ctx-9", "cancel": True}]


# --------------------------------------------------------------------------- #
# ElevenLabs
# --------------------------------------------------------------------------- #
def test_elevenlabs_message_sequence_and_url():
    import websockets
    from diallux.media import elevenlabs_tts as elt

    captured: dict = {}
    orig_connect = websockets.connect

    async def fake_connect(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("additional_headers")
        return FakeWS()

    websockets.connect = fake_connect
    try:
        s = Settings(**BASE, tts_provider="elevenlabs",
                     elevenlabs_api_key="xi-key", elevenlabs_voice_id="chloe-id",
                     elevenlabs_model_id="eleven_flash_v2_5",
                     elevenlabs_stability=0.4, elevenlabs_speed=1.05)

        async def go():
            tts = elt.ElevenLabsTTS(s, on_audio=None)
            await tts.connect()
            ws: FakeWS = tts._ws
            await tts.speak("ctx-1", "Hello ", True)
            await tts.speak("ctx-1", "there.", False)
            await tts.cancel("ctx-1")
            return ws
        ws = _run(go())
        assert "elevenlabs.io/v1/text-to-speech/chloe-id/multi-stream-input" in captured["url"]
        assert "output_format=ulaw_8000" in captured["url"]
        assert "model_id=eleven_flash_v2_5" in captured["url"]
        assert "inactivity_timeout=" in captured["url"]
        assert captured["headers"] == {"xi-api-key": "xi-key"}
        # text messages carry flush:true (sentence-shaped chunks); no
        # voice_settings slot without overrides (merged only when provided)
        assert "voice_settings" not in ws.sent[0]
        assert ws.sent[0]["text"] == "Hello " and ws.sent[0]["flush"] is True
        texts = [m.get("text") for m in ws.sent[1:]]
        assert "there." in texts
        # continue_=False closes the CONTEXT (never a bare {"text": ""})
        assert ws.sent[-1] == {"context_id": "ctx-1", "close_context": True}
    finally:
        websockets.connect = orig_connect


def test_elevenlabs_cancelled_context_audio_swallowed():
    from diallux.media import elevenlabs_tts as elt
    s = Settings(**BASE, elevenlabs_api_key="k", elevenlabs_voice_id="v")
    got: list[str] = []

    async def on_audio(payload: str, ctx: str):
        got.append(payload)

    async def go():
        tts = elt.ElevenLabsTTS(s, on_audio=on_audio)
        tts._ws = FakeWS()
        await tts.speak("ctx-A", "some text", True)
        await tts.cancel("ctx-A")          # barge-in mid-generation
        # server keeps streaming frames for the cancelled generation...
        await tts._handle_message({"audio": "AAA", "contextId": "ctx-A",
                                   "is_final": False})
        await tts._handle_message({"audio": "BBB", "contextId": "ctx-A",
                                   "is_final": True})   # then finalizes
        await tts.speak("ctx-B", "next turn", True)
        await tts._handle_message({"audio": "CCC", "contextId": "ctx-B",
                                   "is_final": True})
    _run(go())
    assert got == ["CCC"]              # cancelled-context frames dropped


def test_elevenlabs_empty_chunk_with_continue_is_noop():
    from diallux.media import elevenlabs_tts as elt
    s = Settings(**BASE, elevenlabs_api_key="k", elevenlabs_voice_id="v")

    async def go():
        tts = elt.ElevenLabsTTS(s, on_audio=None)
        tts._ws = FakeWS()
        await tts.speak("ctx", "", True)      # empty + continue -> ignored
        await tts.speak("ctx", "", False)     # empty + final -> flush only
    _run(go())


# --------------------------------------------------------------------------- #
# factory
# --------------------------------------------------------------------------- #
def test_factory_switches_provider():
    s_cart = Settings(**BASE, tts_provider="cartesia")
    from diallux.media.cartesia_tts import CartesiaTTS
    assert isinstance(create_tts(s_cart, None), CartesiaTTS)

    s_def = Settings(**BASE)   # default provider = elevenlabs (iter80 flip)
    from diallux.media.elevenlabs_tts import ElevenLabsTTS
    assert isinstance(create_tts(s_def, None), ElevenLabsTTS)

    s_el = Settings(**BASE, tts_provider="elevenlabs")
    from diallux.media.elevenlabs_tts import ElevenLabsTTS
    assert isinstance(create_tts(s_el, None), ElevenLabsTTS)

    assert tts_label(s_cart).startswith("cartesia/")
    assert tts_label(s_el).startswith("elevenlabs/")
