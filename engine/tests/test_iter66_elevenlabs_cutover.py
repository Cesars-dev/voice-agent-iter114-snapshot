"""iter66 pin tests — ElevenLabs multi-stream-input cutover (hermetic).

Pins (action plan §5, tasks/surgeon/eleven_labs_migration/02_action_plan.md):
1. URL/format/latency-param-absent (pcm_16000 browser / ulaw_8000 twilio)
2. Message sequence: text+flush per sentence; voice_settings FIRST message
   of a context only; end-of-turn close_context; ("", False) marker
3. Socket-killer guard: no {"text": ""} without context_id ever sent
4. cancel -> close_context exactly; cancelled frames swallowed; routing by contextId
5. Recv casing {"contextId"}/{ "is_final"} routes + clears the guard per context
6. ws= adoption (no websockets.connect) + factory transport pass-through
7. Prewarm provider gate: EL URL, companion registered+cancellable, phrase cache 0.0
8. Keepalive shape {"text": " "}

FakeWS seam from test_tts_providers.py. No network, no real keys.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings

BASE = dict(openai_api_key="t", retell_api_key="t", langfuse_enabled=False,
            audio_transport="browser", tts_provider="elevenlabs",
            elevenlabs_api_key="xi-test", elevenlabs_voice_id="chloe-test",
            elevenlabs_model_id="eleven_flash_v2_5")


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
        await asyncio.sleep(30)
        return "{}"

    async def close(self):
        self.closed = True


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Pin 1 — URL: multi-stream-input, model, transport-aware format,
# inactivity_timeout=180, NO optimize_streaming_latency
# --------------------------------------------------------------------------- #
def test_pin1_connect_url_shape():
    from diallux.media.elevenlabs_tts import connect_url
    s = Settings(**BASE)
    url = connect_url(s, "pcm_16000")
    assert "/v1/text-to-speech/chloe-test/multi-stream-input" in url
    assert "model_id=eleven_flash_v2_5" in url
    assert "output_format=pcm_16000" in url
    assert "inactivity_timeout=180" in url
    assert "optimize_streaming_latency" not in url
    assert "stream-input?" not in url.split("multi-stream-input")[0] + "stream-input"


def test_pin1_output_format_transport_aware():
    from diallux.media.elevenlabs_tts import ElevenLabsTTS
    browser = ElevenLabsTTS(Settings(**BASE), on_audio=None)              # .env/browser
    twilio = ElevenLabsTTS(Settings(**{**BASE, "audio_transport": "twilio"}),
                           on_audio=None)
    explicit = ElevenLabsTTS(Settings(**{**BASE, "audio_transport": "twilio"}),
                             on_audio=None, transport="browser")
    assert browser.output_format == "pcm_16000"
    assert twilio.output_format == "ulaw_8000"
    assert explicit.output_format == "pcm_16000"    # factory pass-through wins


# --------------------------------------------------------------------------- #
# Pin 2 — message sequence: flush per sentence, voice_settings first-message
# only, end-of-turn close_context, ("", False) marker -> close_context only
# --------------------------------------------------------------------------- #
def test_pin2_message_sequence():
    from diallux.media.elevenlabs_tts import ElevenLabsTTS
    s = Settings(**BASE, elevenlabs_stability=0.4, elevenlabs_speed=1.05)
    tts = ElevenLabsTTS(s, on_audio=None)
    ws = FakeWS()
    tts._ws = ws

    async def go():
        await tts.speak("c1", "Hi there.", True, overrides={"stability": 0.4})
        await tts.speak("c1", "How can I help?", False, overrides={"stability": 0.4})
        await tts.speak("c2", "Second turn.", False, overrides={"stability": 0.4})
    _run(go())
    # sentence 1: text+flush, voice_settings attached (first message of ctx)
    assert ws.sent[0]["text"] == "Hi there."
    assert ws.sent[0]["context_id"] == "c1"
    assert ws.sent[0]["flush"] is True
    assert ws.sent[0]["voice_settings"]["stability"] == 0.4
    # sentence 2: same context — NO repeat voice_settings, then close_context
    assert "voice_settings" not in ws.sent[1]
    assert ws.sent[2] == {"context_id": "c1", "close_context": True}
    # new turn: voice_settings ride again (fresh context) + close
    assert ws.sent[3]["context_id"] == "c2"
    assert "voice_settings" in ws.sent[3]
    assert ws.sent[4] == {"context_id": "c2", "close_context": True}


def test_pin2_empty_final_marker_closes_context_only():
    from diallux.media.elevenlabs_tts import ElevenLabsTTS
    tts = ElevenLabsTTS(Settings(**BASE), on_audio=None)
    ws = FakeWS()
    tts._ws = ws

    async def go():
        await tts.speak("c1", "Greeting.", True)   # context opened
        await tts.speak("c1", "", False)           # gate closing marker ("", False)
    _run(go())
    assert ws.sent[0]["text"] == "Greeting."
    assert ws.sent[1] == {"context_id": "c1", "close_context": True}
    assert len(ws.sent) == 2
    assert all(m.get("text") != "" for m in ws.sent)


# --------------------------------------------------------------------------- #
# Pin 3 — socket-killer guard: NOTHING ever sent has text == "" w/o context_id
# --------------------------------------------------------------------------- #
def test_pin3_no_socket_killer_shape_on_wire():
    from diallux.media.elevenlabs_tts import ElevenLabsTTS
    tts = ElevenLabsTTS(Settings(**BASE), on_audio=None)
    ws = FakeWS()
    tts._ws = ws

    async def go():
        await tts.speak("c1", "One.", True)
        await tts.speak("c1", "Two.", False)
        await tts.speak("c2", "", True)
        await tts.speak("c2", "", False)
        await tts.cancel("c3")
        await tts.close()
    _run(go())
    for m in ws.sent:
        if m.get("text", None) == "":
            raise AssertionError(f"socket-killer shape sent: {m}")


# --------------------------------------------------------------------------- #
# Pin 4 — cancel: exact close_context frame; in-flight frames swallowed;
# audio routes by contextId
# --------------------------------------------------------------------------- #
def test_pin4_cancel_and_context_routing():
    from diallux.media.elevenlabs_tts import ElevenLabsTTS
    got: list[tuple[str, str]] = []

    async def on_audio(payload: str, ctx: str):
        got.append((payload, ctx))

    tts = ElevenLabsTTS(Settings(**BASE), on_audio=on_audio)
    ws = FakeWS()
    tts._ws = ws

    async def go():
        await tts.cancel("cX")
        assert ws.sent == [{"context_id": "cX", "close_context": True}]
        await tts._handle_message({"audio": "P1", "contextId": "cX", "is_final": False})
        await tts._handle_message({"audio": "P2", "contextId": "cX", "is_final": True})
        # guard cleared by is_final: a DIFFERENT context routes through
        await tts._handle_message({"audio": "P3", "contextId": "cY", "is_final": False})
    _run(go())
    # cancelled ctx frames swallowed; P3 routes with ITS OWN contextId
    assert got == [("P3", "cY")]


# --------------------------------------------------------------------------- #
# Pin 5 — recv casing: contextId (camel) + is_final (snake)
# --------------------------------------------------------------------------- #
def test_pin5_recv_casing_contextid_is_final():
    from diallux.media.elevenlabs_tts import ElevenLabsTTS
    got: list[tuple[str, str]] = []

    async def on_audio(payload: str, ctx: str):
        got.append((payload, ctx))

    tts = ElevenLabsTTS(Settings(**BASE), on_audio=on_audio)
    ws = FakeWS(canned=[{"audio": "Q1", "contextId": "c5", "is_final": False},
                        {"audio": "Q2", "contextId": "c5", "is_final": True}])
    tts._ws = ws
    # hermetic path: drive the handler directly (recv loop shape is pinned by
    # adoption test below); the WIRE casing is what we pin here.
    _run(tts._handle_message({"audio": "Q1", "contextId": "c5", "is_final": False}))
    _run(tts._handle_message({"audio": "Q2", "contextId": "c5", "is_final": True}))
    assert got == [("Q1", "c5"), ("Q2", "c5")]
    # old stream-input casing (isFinal) must NOT be honored as the final marker:
    got.clear()

    async def go2():
        tts._cancelled.add("cOld")
        await tts._handle_message({"audio": "Z", "contextId": "cOld", "isFinal": True})
    _run(go2())
    assert "cOld" in tts._cancelled     # isFinal is ignored (snake_case only)


# --------------------------------------------------------------------------- #
# Pin 6 — ws= adoption + factory transport pass-through
# --------------------------------------------------------------------------- #
def test_pin6_adoption_no_fresh_connect():
    import websockets
    from diallux.media.elevenlabs_tts import ElevenLabsTTS

    def _boom(*a, **k):
        raise AssertionError("websockets.connect must NOT be called on adoption")

    orig = websockets.connect
    websockets.connect = _boom
    try:
        async def go():
            tts = ElevenLabsTTS(Settings(**BASE), on_audio=None)
            ws = FakeWS()
            await tts.connect(ws=ws)
            assert tts._ws is ws
            assert tts._recv_task is not None and not tts._recv_task.done()
            await tts.close()
        _run(go())
    finally:
        websockets.connect = orig


def test_pin6_factory_passes_transport():
    from diallux.media.elevenlabs_tts import ElevenLabsTTS
    from diallux.media.tts_factory import create_tts
    s = Settings(**{**BASE, "audio_transport": "twilio"})
    tts = create_tts(s, None, transport="browser")
    assert isinstance(tts, ElevenLabsTTS)
    assert tts.output_format == "pcm_16000"    # NOT ulaw_8000: transport was passed
    tts2 = create_tts(s, None)
    assert tts2.output_format == "ulaw_8000"   # falls back to settings transport


# --------------------------------------------------------------------------- #
# Pin 7 — prewarm provider gate
# --------------------------------------------------------------------------- #
def test_pin7_prewarm_provider_gate():
    import diallux.media.prewarm as pw
    import websockets
    from diallux.media.elevenlabs_tts import EL_KEEPALIVE_S

    captured: dict = {}
    orig_connect = websockets.connect
    saved_pool = dict(pw.pool)
    saved_companions = dict(pw._companion_tasks)
    saved_ts = dict(pw._spawn_ts)
    pw.pool.clear(); pw._companion_tasks.clear(); pw._spawn_ts.clear()

    async def fake_connect(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("additional_headers")
        return FakeWS()

    websockets.connect = fake_connect
    try:
        s = Settings(**BASE)   # tts_provider=elevenlabs, transport=browser

        async def go():
            await pw._spawn_idle_tts(s)
            task = pw._companion_tasks.get("tts")
            assert task is not None and not task.done()
            pw.get("tts")                # adoption cancels the companion FIRST
            await asyncio.sleep(0)
            assert task.cancelled() or task.done()
        _run(go())
        assert "elevenlabs.io" in captured["url"]
        assert "multi-stream-input" in captured["url"]
        assert "output_format=pcm_16000" in captured["url"]
        assert "cartesia.io" not in captured["url"]
        assert captured["headers"] == {"xi-api-key": "xi-test"}
        assert EL_KEEPALIVE_S == 15

        # phrase cache: gated off for EL (returns 0.0 without any synthesis)
        async def go2():
            return await pw._warm_phrase_cache(s)
        assert _run(go2()) == 0.0
    finally:
        websockets.connect = orig_connect
        pw.pool.clear(); pw.pool.update(saved_pool)
        pw._companion_tasks.clear(); pw._companion_tasks.update(saved_companions)
        pw._spawn_ts.clear(); pw._spawn_ts.update(saved_ts)


def test_pin7_prewarm_cartesia_path_unchanged():
    import diallux.media.prewarm as pw
    import websockets
    saved_pool = dict(pw.pool)
    saved_companions = dict(pw._companion_tasks)
    saved_ts = dict(pw._spawn_ts)
    pw.pool.clear(); pw._companion_tasks.clear(); pw._spawn_ts.clear()
    captured: dict = {}
    orig_connect = websockets.connect

    async def fake_connect(url, **kwargs):
        captured["url"] = url
        return FakeWS()

    websockets.connect = fake_connect
    try:
        s = Settings(**{**BASE, "tts_provider": "cartesia"},
                     cartesia_api_key="ck")
        _run(pw._spawn_idle_tts(s))
        assert "cartesia.ai" in captured["url"]
        assert "elevenlabs.io" not in captured["url"]
    finally:
        websockets.connect = orig_connect
        pw.pool.clear(); pw.pool.update(saved_pool)
        pw._companion_tasks.clear(); pw._companion_tasks.update(saved_companions)
        pw._spawn_ts.clear(); pw._spawn_ts.update(saved_ts)


# --------------------------------------------------------------------------- #
# Pin 8 — keepalive: {"text": " "} (never {"text": ""})
# --------------------------------------------------------------------------- #
def test_pin8_keepalive_shape():
    from diallux.media.elevenlabs_tts import ElevenLabsTTS
    tts = ElevenLabsTTS(Settings(**BASE), on_audio=None)
    ws = FakeWS()
    tts._ws = ws
    _run(tts.keepalive())
    assert ws.sent == [{"text": " "}]      # a SPACE, not the socket-killer ""
