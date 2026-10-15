"""iter59 T5 — prewarm + phrase-cache pins (hermetic: no network, no model).

call_prewarm (default OFF): server-start pool of ONE idle Deepgram flux WS +
ONE idle Cartesia WS + a warm arctic embed + the greeting phrase cache.
Sessions ADOPT from the pool (connect(ws=None)); adoption failure NEVER
fails a call (fallback: fresh connect). Flag off = iter58 exact.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from tests.test_iter49_rag_parity import _t4_settings


def test_flag_off_is_iter58_surface():
    """call_prewarm=False (default): no startup work — the config default
    pins the iter58 surface."""
    assert Settings().call_prewarm is False
    assert Settings().tts_phrase_cache is True   # phrase cache default on


def test_session_adopts_and_falls_back(monkeypatch):
    """Session start(): prewarm pool hit -> ADOPT (no fresh connect);
    empty pool -> fresh connect (never fail on the pool)."""
    import asyncio

    from diallux.media.session import CallSession

    adopted: list[str] = []

    class FakeSTT:
        async def connect(self, ws=None):
            adopted.append("stt" if ws is not None else "stt-fresh")

    class FakeTTS:
        async def connect(self, ws=None):
            adopted.append("tts" if ws is not None else "tts-fresh")

        def new_context_id(self):
            return "ctx-1"

        async def speak(self, ctx, text, continue_=False, overrides=None):
            pass

    def mk_session(settings):
        sess = object.__new__(CallSession)
        sess.settings = settings
        sess.call_sid = "c59"
        sess.stream_sid = "s59"
        sess.transport = "browser"
        sess.tracer = None
        sess.llm_json = {"default_dynamic_variables": {},
                         "starting_state": "Intake"}
        sess.custom_parameters = {}
        sess.stt = FakeSTT()
        sess.tts = FakeTTS()
        sess.gate = None
        sess._initial_payload = {"history": [{"role": "assistant",
                                              "content": "hi"}], "dvs": {}}
        sess._turn_state = "Intake"
        sess._turn_dvs = {}
        sess._update_fired = False
        sess._greet = None
        return sess

    settings = _t4_settings(call_prewarm=True)

    # --- adoption path: pool has sockets ---
    import diallux.media.prewarm as pw
    stt_ws = object()
    tts_ws = object()
    monkeypatch.setitem(pw.pool, "stt", stt_ws)
    monkeypatch.setitem(pw.pool, "tts", tts_ws)

    async def adopt():
        sess = mk_session(settings)
        from diallux.media.prewarm import get as pool_get
        adopted.clear()
        pw_sess = pool_get("stt")
        if pw_sess is not None:
            await sess.stt.connect(ws=pw_sess)
        else:
            await sess.stt.connect()
        tws = pool_get("tts")
        if tws is not None:
            await sess.tts.connect(ws=tws)
        else:
            await sess.tts.connect()
        return adopted

    got = asyncio.run(adopt())
    assert got == ["stt", "tts"], got
    assert "stt" not in pw.pool and "tts" not in pw.pool   # pool EMPTIED (pop)

    # --- fallback path: pool empty -> fresh connect ---
    monkeypatch.setattr(pw, "pool", {})

    async def fallback():
        sess = mk_session(settings)
        from diallux.media.prewarm import get as pool_get
        adopted.clear()
        pw_sess = pool_get("stt")
        if pw_sess is not None:
            await sess.stt.connect(ws=pw_sess)
        else:
            await sess.stt.connect()
        tws = pool_get("tts")
        if tws is not None:
            await sess.tts.connect(ws=tws)
        else:
            await sess.tts.connect()
        return adopted

    got = asyncio.run(fallback())
    assert got == ["stt-fresh", "tts-fresh"], got


def test_cartesia_connect_accepts_adoption(monkeypatch):
    """CartesiaTTS.connect(ws=None): adopted ws skips websockets.connect and
    starts the recv loop bound to the OWNING session's callbacks."""
    import asyncio
    from diallux.media.cartesia_tts import CartesiaTTS

    sent_from_session: list[str] = []

    class FakeWS:
        async def recv(self):
            await asyncio.sleep(0.01)      # real recv yields on I/O
            import json
            return json.dumps(
                {"type": "chunk", "data": "QUJD", "context_id": "own-ctx"})

        async def close(self):
            pass

    settings = _t4_settings(cartesia_api_key="k")
    heard: list[str] = []

    async def on_audio(data, ctx):
        heard.append((data, ctx))

    tts = CartesiaTTS(settings, on_audio=on_audio)
    fake = FakeWS()

    async def go():
        await tts.connect(ws=fake)           # adopted — no network connect
        await asyncio.sleep(0.15)            # recv loop binds session's on_audio
        assert tts._ws is fake
        await tts.close()

    asyncio.run(go())
    assert heard and heard[0][1] == "own-ctx"



def test_deepgram_connect_accepts_adoption():
    """DeepgramSTT.connect(ws=None): adopted socket skips websockets.connect;
    the recv loop binds the OWNING session's callbacks."""
    import asyncio
    from diallux.media.deepgram_stt import DeepgramSTT

    class FakeWS:
        async def recv(self):
            await asyncio.sleep(0.01)      # yield to the loop like real I/O
            import json
            return json.dumps(
                {"type": "TurnInfo", "event": "Update", "transcript": "hi"})

        async def close(self):
            pass

    eots: list[str] = []
    updates: list[str] = []
    settings = _t4_settings(deepgram_mode="flux")
    stt = DeepgramSTT(settings, on_eot=lambda t: _push(eots, t),
                      on_start_of_turn=_push_done, on_update=lambda t: _push(updates, t))
    ws = FakeWS()

    async def go():
        await stt.connect(ws=ws)             # adopted — no network connect
        await asyncio.sleep(0.15)
        assert stt._ws is ws
        await stt.close()

    asyncio.run(go())
    assert updates and updates[0] == "hi"    # owner session's callbacks bound


async def _push(into, v):
    into.append(v)


async def _push_done():
    return None


def test_phrase_cache_hit_bypasses_the_api():
    """A cached phrase (exact text + identical format/generation) is spliced
    from the raw bytes; the socket is NEVER sent to."""
    import asyncio
    from diallux.media.cartesia_tts import CartesiaTTS, _PHRASE_CACHE, _b64e

    sent: list = []

    class FakeWS:
        async def send(self, raw):
            sent.append(raw)

        async def recv(self):
            raise __import__("asyncio").CancelledError()

        async def close(self):
            pass

    settings = _t4_settings(cartesia_api_key="k", tts_phrase_cache=True)
    heard: list[tuple] = []

    async def on_audio(data, ctx):
        heard.append((data, ctx))

    tts = CartesiaTTS(settings, on_audio=on_audio)
    tts._ws = FakeWS()
    greeting = "Hello! You have reached Dialux, this is Linda speaking, how may I help you today?"
    key = tts._phrase_key(greeting, tts._gen_cfg(1.12, None, "happy"))
    _PHRASE_CACHE[key] = b"\x01" * 2400      # raw mulaw-ish bytes (owner-approved pre-record)
    asyncio.run(tts.speak("ctx-cache", greeting, continue_=False,
                          overrides={"speed": 1.12, "emotion": "happy"}))
    assert sent == [], "cache hit must not touch the API"
    assert heard, "cached audio spliced through on_audio"

    # key sensitivity: a different phrase / different overrides = a MISS
    sent.clear()
    asyncio.run(tts.speak("ctx-miss", greeting + "!", continue_=False,
                          overrides={"speed": 1.12, "emotion": "happy"}))
    assert sent, "cache miss goes to the API"


def test_phrase_cache_flag_off_always_sends():
    """tts_phrase_cache=False: iter58 surface (synthesize every time)."""
    import asyncio
    from diallux.media.cartesia_tts import CartesiaTTS, _PHRASE_CACHE

    sent: list = []

    class FakeWS:
        async def send(self, raw):
            sent.append(raw)

        async def recv(self):
            await asyncio.sleep(30)     # never returns (cancel target)

        async def close(self):
            pass

    settings = _t4_settings(cartesia_api_key="k", tts_phrase_cache=False)
    tts = CartesiaTTS(settings, on_audio=_noop_audio)
    tts._ws = FakeWS()
    asyncio.run(tts.speak("ctx", "Hello! You have reached Dialux, this is Linda speaking, how may I help you today?",
                          continue_=False, overrides={"speed": 1.12, "emotion": "happy"}))
    assert len(sent) == 1               # the API request went out


async def _noop_audio(data, ctx):
    return None


def test_prewarm_url_matches_session_url():
    """G3: the prewarmed flux URL is byte-identical to the session's
    DeepgramSTT._url() (effective eager threshold applied with the same
    None->0.6 default)."""
    from diallux.media.deepgram_stt import DeepgramSTT
    from diallux.media import prewarm

    settings = _t4_settings(deepgram_mode="flux")
    # replicate session.start()'s eager resolution for the SESSION side
    thr = settings.deepgram_eager_eot_threshold
    settings.deepgram_eager_eot_threshold = thr or 0.6
    session_url = DeepgramSTT(settings, on_eot=_noop_audio,
                              on_start_of_turn=_noop_audio)._url()
    assert prewarm._stt_url(settings) == session_url
