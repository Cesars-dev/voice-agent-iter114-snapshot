"""Cartesia Sonic TTS client (raw WebSocket, no SDK).

Protocol (docs.cartesia.ai — /tts/websocket, cartesia_version 2026-08-14):
  - Connect:  wss://api.cartesia.ai/tts/websocket?cartesia_version=<v>
              Auth: `X-API-Key` header (server-side key); falls back to
              `?api_key=` query param if the handshake rejects headers.
  - Send:     {"model_id", "transcript", "voice", "output_format",
              "language", "context_id", "continue", "max_buffer_delay_ms",
              "generation_config"}
  - Recv:     {"type":"chunk","data":<base64 audio>,"done":bool,
              "status_code":int,"step_time":ms,"context_id":...}
  - Cancel:   {"context_id", "cancel": true}          (barge-in)
  - Finish:   last chunk with "continue": false (or empty transcript + false)

Output format is pcm_mulaw @ 8000 Hz — Twilio Media Streams' NATIVE format,
so the base64 `data` field is forwarded to Twilio UNCHANGED (zero transcoding,
zero audio processing on the speak path).

iter21: transport="browser" requests raw pcm_s16le at settings.browser_sample_rate
(16-bit LE PCM the browser plays directly); the caller (/mic ws) forwards the
decoded bytes as binary ws frames instead of Twilio media events.

Contexts (per docs): one context per agent turn keeps prosody across the
sentence chunks; `continue: true` on every chunk except the last.

generation_config (docs: Volume, Speed, and Emotion; 2026-08-14):
  - speed   0.6–1.5   (double; default 1.0)
  - volume  0.5–2.0   (double; default 1.0)
  - emotion  one of the documented list, English only, BETA: "guidance, not a
              strict adjustment" — the model still follows the transcript's
              emotional subtext. Best results on emotive-tagged voices.
  Sent per REQUEST (there is no persistent account-level setting), so we
  attach it on every speak() — cheap (2-3 JSON fields).
  Base tuning lives in .env: CARTESIA_SPEED / CARTESIA_VOLUME / CARTESIA_EMOTION.
  V5: speak(overrides=...) from media/delivery.py per-state profiles MERGES
  OVER the .env globals (profile wins for the fields it sets; clamped to the
  documented ranges so the table can never 400 a request).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Awaitable, Callable

import websockets

log = logging.getLogger("diallux.tts")

# iter59 T5: stock-phrase cache (owner-approved pre-record; the Cartesia
# documented pattern — docs.cartesia.ai "Caching Audio for Stock Responses"):
# repeated EXACT phrases (the greeting) are synthesized once as raw bytes
# matching output_format + generation_config and spliced into the stream
# without an API hit — faster first audio, zero credits. Module-level (per
# PROCESS) so a per-call CartesiaTTS instance reuses what prewarm built.
_PHRASE_CACHE: dict[str, bytes] = {}


def _b64e(data: bytes) -> str:
    import base64
    return base64.b64encode(data).decode()


def _b64d(data: str) -> bytes:
    import base64
    return base64.b64decode(data)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))

OnAudio = Callable[[str, str], Awaitable[None]]     # (base64_mulaw_payload, context_id)


class CartesiaTTS:
    def __init__(self, settings, on_audio: OnAudio, transport: str | None = None):
        self.settings = settings
        self.transport = transport or settings.audio_transport
        if self.transport == "browser":
            self.output_format = {
                "container": "raw",
                "encoding": "pcm_s16le",
                "sample_rate": settings.browser_sample_rate,
            }
        else:
            self.output_format = {
                "container": "raw",
                "encoding": "pcm_mulaw",
                "sample_rate": settings.cartesia_sample_rate,
            }
        self.on_audio = on_audio
        self._ws = None
        self._recv_task: asyncio.Task | None = None
        self._closed = False
        self.first_byte_ts: float | None = None

    # ------------------------------------------------------------------ #
    async def connect(self, ws=None):
        """Connect (3-attempt-safe handshake + recv loop), or ADOPT a
        prewarmed socket (iter59 T5: ws provided skips websockets.connect —
        the recv loop below binds the OWNING session's callbacks)."""
        if ws is not None:
            self._ws = ws
            self._recv_task = asyncio.create_task(self._recv_loop())
            log.info("cartesia adopted prewarmed socket (model=%s)",
                     self.settings.cartesia_model_id)
            return
        base = f"wss://api.cartesia.ai/tts/websocket?cartesia_version={self.settings.cartesia_version}"
        headers = {"X-API-Key": self.settings.cartesia_api_key}
        try:
            self._ws = await websockets.connect(base, additional_headers=headers, max_size=None, open_timeout=10)
        except Exception:
            # older gateway: key as query param
            fallback = base + f"&api_key={self.settings.cartesia_api_key}"
            self._ws = await websockets.connect(fallback, max_size=None, open_timeout=10)
        self._recv_task = asyncio.create_task(self._recv_loop())
        log.info("cartesia connected (model=%s)", self.settings.cartesia_model_id)

    async def close(self):
        self._closed = True
        if self._recv_task:
            self._recv_task.cancel()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    async def speak(self, context_id: str, text: str, continue_: bool,
                    overrides: dict | None = None):
        """Stream one transcript chunk into a context.
        Empty text with continue_=False is the documented way to close a context.
        overrides (V5 delivery profiles): speed/volume/emotion that win over
        the .env globals for this chunk's state."""
        if self._ws is None or self._closed:
            return
        if not text and continue_:
            return
        speed = self.settings.cartesia_speed
        volume = self.settings.cartesia_volume
        emotion = self.settings.cartesia_emotion
        if overrides:
            speed = overrides.get("speed", speed)
            volume = overrides.get("volume", volume)
            emotion = overrides.get("emotion", emotion)
        req = {
            "model_id": self.settings.cartesia_model_id,
            "transcript": text,
            "voice": self.settings.cartesia_voice_id,
            "output_format": self.output_format,
            "language": self.settings.cartesia_language,
            "context_id": context_id,
            "continue": continue_,
            "max_buffer_delay_ms": self.settings.cartesia_max_buffer_delay_ms
            if self.settings.cartesia_buffering == "custom" else self.settings.cartesia_max_buffer_delay_ms,
        }
        # per-request voice guidance (speed/volume/emotion) — docs: include on
        # EVERY request; there is no persistent setting. Profile overrides
        # (V5) already merged above; clamp so a bad value can never 400.
        gen_cfg: dict = self._gen_cfg(speed, volume, emotion)
        if gen_cfg:
            req["generation_config"] = gen_cfg
        # iter59 T5: stock-phrase cache — an EXACT repeated phrase (greeting)
        # with identical model/voice/format/generation bytes is spliced from
        # the pre-generated raw audio; the API is never touched.
        if getattr(self.settings, "tts_phrase_cache", True) and text \
                and not continue_:
            key = self._phrase_key(text, gen_cfg)
            audio = _PHRASE_CACHE.get(key)
            if audio is not None:
                await self._emit_cached(context_id, audio)
                return
        try:
            await self._ws.send(json.dumps(req))
        except Exception as exc:
            log.warning("cartesia send failed: %s", exc)

    @staticmethod
    def _gen_cfg(speed, volume, emotion) -> dict:
        """The per-request generation_config (speak + prewarm share the
        exact construction so cache bytes and live bytes are identical)."""
        gen_cfg: dict = {}
        if speed is not None:
            gen_cfg["speed"] = _clamp(speed, 0.6, 1.5)
        if volume is not None:
            gen_cfg["volume"] = _clamp(volume, 0.5, 2.0)
        if emotion:
            gen_cfg["emotion"] = emotion
        return gen_cfg

    def _phrase_key(self, text: str, gen_cfg: dict) -> str:
        import hashlib
        raw = json.dumps({
            "text": text, "fmt": self.output_format,
            "model": self.settings.cartesia_model_id,
            "voice": self.settings.cartesia_voice_id,
            "language": self.settings.cartesia_language,
            "gen": gen_cfg,
        }, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()

    async def _emit_cached(self, context_id: str, audio: bytes):
        """Splice cached raw audio: emit in ~100 ms chunks through on_audio
        (same delivery shape as the recv loop; first_byte_ts stamped)."""
        step = max(1, int(self.output_format.get("sample_rate", 8000)) // 10)
        for i in range(0, len(audio), step):
            if self.first_byte_ts is None:
                self.first_byte_ts = time.perf_counter()
            await self.on_audio(_b64e(audio[i:i + step]), context_id)

    async def cancel(self, context_id: str):
        """Barge-in: stop anything not yet generated for this context."""
        if self._ws is None or self._closed:
            return
        try:
            await self._ws.send(json.dumps({"context_id": context_id, "cancel": True}))
        except Exception as exc:
            log.warning("cartesia cancel failed: %s", exc)

    @staticmethod
    def new_context_id() -> str:
        return str(uuid.uuid4())

    # ------------------------------------------------------------------ #
    async def _recv_loop(self):
        try:
            while not self._closed:
                raw = await self._ws.recv()
                if isinstance(raw, bytes):
                    continue
                msg = json.loads(raw)
                mtype = msg.get("type")
                if mtype == "chunk":
                    if self.first_byte_ts is None:
                        self.first_byte_ts = time.perf_counter()
                    data = msg.get("data")
                    if data:
                        await self.on_audio(data, msg.get("context_id", ""))
                elif mtype == "error":
                    # cancel-of-finished-context is a normal barge-in race
                    # (StartOfTurn cancels the previous, already-done context)
                    msg_txt = json.dumps(msg)[:400]
                    if "already been cancelled" in msg_txt or "Invalid context ID" in msg_txt:
                        log.debug("cartesia benign cancel race: %s", msg_txt)
                    else:
                        log.error("cartesia error: %s", msg_txt)
                # timestamps / done / phoneme_timestamps: not needed on the hot path
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            if not self._closed:
                log.error("cartesia recv loop ended: %s", exc)
