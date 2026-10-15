"""iter59 T5: transport prewarm — idle Deepgram flux WS + idle Cartesia WS +
warm arctic embed + the greeting phrase cache, built at SERVER START.

Why (master plan §T5): app.py had NO startup hook — Deepgram/Cartesia
connected per call (~1.4 s + ~0.5 s on the greeting hot path) and the first
embed loaded the ONNX session lazily. Sessions ADOPT from the pool
(connect(ws=None)); adoption failure NEVER fails a call (fallback: fresh
connect — a call never fails on the pool).

Cartesia docs evidence (01_assessment.md §2c): a new HTTPS/TLS handshake is
"often on the same order as TTFB for the audio itself" — a kept-open WS
amortizes it. Prewarm = their amortization, moved to server start.

The greeting phrase cache (owner-approved "pre recorded" sentence, the
Retell stock-phrase model): the begin_message is synthesized ONCE here as
raw bytes matching the live output_format + generation_config, then spliced
by CartesiaTTS.speak on every call — no API hit, no synthesis wait.

Flag: settings.call_prewarm (default False = iter58 exact).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import websockets

log = logging.getLogger("diallux.prewarm")

# The pool: {"stt": websockets-socket, "tts": websockets-socket}
pool: dict[str, object] = {}
_companion_tasks: dict[str, asyncio.Task] = {}
# iter61 AUD-12: spawn age tracking for the TTL maintainer (Cartesia idle-
# times-out silent sockets server-side after ~minutes — observed death after
# ~20 min; an old pool entry is a guaranteed-dead adoption).
PREWARM_SOCKET_TTL = 240
_spawn_ts: dict[str, float] = {}
_maintainer_task: object = None
_warm_diag: dict = {}


def _effective_eager_threshold(settings) -> str | None:
    """The same None->default resolution session.start applies to the eager
    threshold (G3: settings are lru_cached/shared) — the prewarmed URL must
    be byte-identical to the per-call one."""
    thr = getattr(settings, "deepgram_eager_eot_threshold", None)
    eager_on = getattr(settings, "deepgram_eager_eot", False) or thr is not None
    if eager_on and (getattr(settings, "deepgram_eager", True)):
        thr = thr if thr is not None else 0.6
    if thr is None:
        return None
    return str(thr)


def _stt_url(settings) -> str:
    """Byte-identical DeepgramSTT._url() for mode=flux (the deployed mode),
    including the transport codec resolution (browser linear16 vs twilio
    mulaw) the session applies."""
    from urllib.parse import urlencode
    s = settings
    transport = s.audio_transport
    if transport == "browser":
        encoding, sample_rate = "linear16", s.browser_sample_rate
    else:
        encoding, sample_rate = "mulaw", 8000
    params = {
        "model": s.deepgram_model,
        "encoding": encoding,
        "sample_rate": str(sample_rate),
        "eot_threshold": str(s.deepgram_eot_threshold),
        "eot_timeout_ms": str(s.deepgram_eot_timeout_ms),
    }
    thr = _effective_eager_threshold(s)
    if thr is not None:
        params["eager_eot_threshold"] = thr
    if getattr(s, "deepgram_keyterms", None):
        params["keyterms"] = ",".join(s.deepgram_keyterms)
    return "wss://api.deepgram.com/v2/listen?" + urlencode(params)


async def _spawn_idle_stt(settings) -> None:
    """One idle flux WS (no keepalive — iter61 AUD-11, see below)."""
    headers = {"Authorization": f"Token {settings.deepgram_api_key}"}
    ws = await websockets.connect(_stt_url(settings),
                                  additional_headers=headers,
                                  max_size=None, open_timeout=10)
    pool["stt"] = ws
    _spawn_ts["stt"] = time.monotonic()
    # iter61 AUD-11: v2 flux has NO client keepalive variant (server rejects
    # it: CloseStream|ForceEndTurn|Configure are the only valid client types)
    # — the v1 frame queued an UNPARSABLE error that killed the first
    # adoption. Idle liveness = maintainer TTL (AUD-12).


async def _spawn_idle_tts(settings) -> None:
    """One idle TTS WS pooled for adoption. iter66: provider-gated —
    Cartesia (default) or ElevenLabs multi-stream-input; the pool must
    never contain provider-mismatched sockets (prewarm.py:97 audit C1)."""
    if getattr(settings, "tts_provider", "cartesia") == "elevenlabs":
        await _spawn_idle_tts_elevenlabs(settings)
        return
    base = (f"wss://api.cartesia.ai/tts/websocket"
            f"?cartesia_version={settings.cartesia_version}")
    headers = {"X-API-Key": settings.cartesia_api_key}
    try:
        ws = await websockets.connect(base, additional_headers=headers,
                                      max_size=None, open_timeout=10)
    except Exception:
        ws = await websockets.connect(base + f"&api_key={settings.cartesia_api_key}",
                                      max_size=None, open_timeout=10)
    pool["tts"] = ws
    _spawn_ts["tts"] = time.monotonic()
    # drain: swallow the idle socket's messages (welcome/error frames) so its
    # buffer never fills; adoption closes this task's usefulness silently.
    async def _drain():
        try:
            while True:
                await ws.recv()
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
    task = asyncio.get_running_loop().create_task(_drain())
    # iter60 AUD-1: register the companion so get("tts") can cancel it — a
    # live _drain recv on an adopted socket races the session's recv loop
    # (websockets 16.1.1 ConcurrencyError → dead recv loop → mute call).
    _companion_tasks["tts"] = task


async def _spawn_idle_tts_elevenlabs(settings) -> None:
    """One idle ElevenLabs multi-stream-input WS (iter66). URL comes from
    the SHARED connect_url() — byte-identical to the adapter's connect
    (the C1 root cause was duplicated URL strings). EL idle-kills sockets
    at ~20s, so ONE companion task keepalives {"text": " "} every
    EL_KEEPALIVE_S and opportunistically drains; it is registered in
    _companion_tasks so get("tts") cancels it BEFORE adoption (iter60
    AUD-1: a live recv racing the session's recv loop = ConcurrencyError
    = mute call)."""
    from .elevenlabs_tts import connect_url, EL_KEEPALIVE_S
    fmt = "pcm_16000" if settings.audio_transport == "browser" else "ulaw_8000"
    base = connect_url(settings, fmt)
    headers = {"xi-api-key": settings.elevenlabs_api_key}
    try:
        ws = await websockets.connect(base, additional_headers=headers,
                                      max_size=None, open_timeout=10)
    except Exception:
        ws = await websockets.connect(base + f"&xi-api-key={settings.elevenlabs_api_key}",
                                      max_size=None, open_timeout=10)
    pool["tts"] = ws
    _spawn_ts["tts"] = time.monotonic()
    async def _keepalive_drain():
        try:
            while True:
                await asyncio.sleep(EL_KEEPALIVE_S)
                try:
                    await ws.send(json.dumps({"text": " "}))
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(ws.recv(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
    task = asyncio.get_running_loop().create_task(_keepalive_drain())
    _companion_tasks["tts"] = task



async def _warm_rag(settings) -> float:
    """Resolve the KB store singleton + ONE warm embed batch — loads the
    ONNX session off the call path (the ~1.1 s cold load never lands on a
    caller)."""
    from .. import rag as ragmod
    t0 = time.perf_counter()
    store = await ragmod.get_kb_store(settings)
    if store is None:
        return 0.0
    await store._ensure_embed_fn()
    if store.embed_fn is not None:
        await store.embed_fn(["warm"])
    return round((time.perf_counter() - t0) * 1000, 1)


async def _warm_phrase_cache(settings) -> float:
    """Synthesize the greeting ONCE through a throwaway Cartesia socket and
    stash the raw audio bytes in the module cache (owner-approved pre-record).
    Reuses the REAL CartesiaTTS protocol code + the exact delivery overrides
    the session greeting uses (resolve_delivery("begin")) so cache bytes and
    live bytes are identical. iter66: Cartesia-ONLY — phrase cache stays
    gated off for ElevenLabs (resolved decision; EL greeting rides the
    normal flush path)."""
    from .cartesia_tts import CartesiaTTS, _PHRASE_CACHE, _b64d
    from .delivery import resolve_delivery
    if getattr(settings, "tts_provider", "cartesia") != "cartesia":
        return 0.0
    if not getattr(settings, "tts_phrase_cache", True):
        return 0.0
    greeting = ""
    try:
        llm_json = json.loads(open(settings.agent_llm_json).read())
        greeting = (llm_json.get("begin_message") or "").strip()
    except Exception:
        return 0.0
    if not greeting:
        return 0.0
    overrides = resolve_delivery(settings, "begin") or {}
    chunks: list[str] = []
    synth = CartesiaTTS(settings, on_audio=_collect(chunks))
    t0 = time.perf_counter()
    try:
        await synth.connect()
        ctx = synth.new_context_id()
        await synth.speak(ctx, greeting, continue_=False, overrides=overrides)
        # settle detection: no new chunks for 3 consecutive ticks (0.3 s),
        # bounded ~30 s — the recv loop has no consumed done marker here.
        last = 0
        still = 0
        for _ in range(300):
            await asyncio.sleep(0.1)
            if len(chunks) != last:
                last = len(chunks)
                still = 0
            else:
                still += 1
                if chunks and still >= 3:
                    break
        await synth.close()
    except Exception as exc:
        log.warning("phrase-cache warm synth failed (non-fatal): %s", exc)
        return 0.0
    if not chunks:
        return 0.0
    speed = settings.cartesia_speed
    volume = settings.cartesia_volume
    emotion = settings.cartesia_emotion
    if overrides:
        speed = overrides.get("speed", speed)
        volume = overrides.get("volume", volume)
        emotion = overrides.get("emotion", emotion)
    key = synth._phrase_key(greeting, synth._gen_cfg(speed, volume, emotion))
    # iter60 AUD-2: each data field is a COMPLETE base64 string (own padding)
    # — joining the strings then decoding once truncates at the first padded
    # chunk; decode per chunk, join BYTES.
    _PHRASE_CACHE[key] = b"".join(_b64d(c) for c in chunks)
    return round((time.perf_counter() - t0) * 1000, 1)


def _collect(chunks: list[str]):
    async def _on_audio(data: str, context_id: str):
        chunks.append(data)
    return _on_audio


async def prewarm_startup(settings) -> dict:
    """Server-start hook (app lifespan; call_prewarm=True only). Best-effort
    per stage — ANY failure logs and moves on; the flag-off surface is
    byte-exact iter58. Returns the diag dict for /health + logs."""
    global _maintainer_task
    diag: dict = {}
    timings = {}
    try:
        t = time.perf_counter()
        await _spawn_idle_stt(settings)
        timings["stt"] = round((time.perf_counter() - t) * 1000)
    except Exception as exc:
        log.warning("prewarm stt failed: %s", exc)
    try:
        t = time.perf_counter()
        await _spawn_idle_tts(settings)
        timings["tts"] = round((time.perf_counter() - t) * 1000)
    except Exception as exc:
        log.warning("prewarm tts failed: %s", exc)
    try:
        timings["rag"] = await _warm_rag(settings)
    except Exception as exc:
        log.warning("prewarm rag failed: %s", exc)
    try:
        timings["phrase_cache"] = await _warm_phrase_cache(settings)
    except Exception as exc:
        log.warning("prewarm phrase cache failed: %s", exc)
    _maintainer_task = asyncio.get_running_loop().create_task(
        _maintainer(settings))
    diag = {"prewarm": "on", **timings,
            "pool": {k: "ready" for k in pool}}
    log.info("prewarm: stt/tts/rag %s", json.dumps(diag))
    return diag


async def _close_quiet(ws) -> None:
    """Best-effort socket close (AUD-12 TTL eviction) — never raises."""
    try:
        await ws.close()
    except Exception:
        pass


async def _maintain_once(settings) -> None:
    """One maintainer tick. iter61 AUD-12: also respawns sockets that aged
    past PREWARM_SOCKET_TTL — Cartesia idle-times-out silent sockets
    server-side (~minutes; observed death after ~20 min), so an old pool
    entry is a guaranteed-dead adoption. Never raises."""
    for kind in ("stt", "tts"):
        if kind in pool:
            age = time.monotonic() - _spawn_ts.get(kind, 0.0)
            if age <= PREWARM_SOCKET_TTL:
                continue
            old = pool.pop(kind)
            _companion_tasks.pop(kind, None)
            _spawn_ts.pop(kind, None)
            try:
                asyncio.get_running_loop().create_task(_close_quiet(old))
            except Exception:
                pass
            log.info("prewarm: idle %s respawned (ttl %ss)", kind,
                     PREWARM_SOCKET_TTL)
        if kind not in pool:
            try:
                if kind == "stt":
                    await _spawn_idle_stt(settings)
                else:
                    await _spawn_idle_tts(settings)
                log.info("prewarm: idle %s respawned", kind)
            except Exception as exc:
                log.warning("prewarm %s respawn failed: %s", kind, exc)


async def _maintainer(settings):
    """Respawn-on-close AND respawn-on-age (iter61 AUD-12): the pool keeps
    ONE idle socket per kind. When a session adopts (pool slot empties) a
    replacement is spawned; when a pooled socket ages past PREWARM_SOCKET_TTL
    it is closed + respawned so no call ever adopts a provider-idle-killed
    corpse. Never raises."""
    while True:
        try:
            await asyncio.sleep(5.0)
            await _maintain_once(settings)
        except asyncio.CancelledError:
            return
        except Exception:
            pass


def get(kind: str, max_age_s: float | None = None):
    """Pop one prewarmed socket for adoption (None when empty).
    iter60 AUD-1: cancel the idle companion task FIRST — a live _drain
    recv on an adopted TTS socket races the session's recv loop
    (websockets 16.1.1 ConcurrencyError → dead recv loop → mute call).
    FIX 7 (PORT 7): max_age_s — refuse probable corpses (an idle socket
    older than the gate is closed; the caller falls back to a fresh
    connect — a call never fails on the pool). max_age_s None/0 = off."""
    t = _companion_tasks.pop(kind, None)
    if t is not None:
        t.cancel()
    ts = _spawn_ts.get(kind)
    if max_age_s and ts is not None \
            and (time.monotonic() - ts) > max_age_s:
        old = pool.pop(kind, None)
        _spawn_ts.pop(kind, None)
        if old is not None:
            try:
                asyncio.get_event_loop().create_task(_close_quiet(old))
            except Exception:
                pass
        log.info("prewarm: refused stale %s (age > %.0fs)", kind, max_age_s)
        return None
    _spawn_ts.pop(kind, None)   # iter61 AUD-12: adopted socket leaves TTL tracking
    return pool.pop(kind, None)
