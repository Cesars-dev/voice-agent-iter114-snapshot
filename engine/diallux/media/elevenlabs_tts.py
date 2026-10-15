"""ElevenLabs streaming TTS adapter (raw WebSocket, no SDK).

Same interface as CartesiaTTS, so CallSession/SentenceGate/barge-in code is
provider-agnostic. iter66 rewrite: migrated from the older `stream-input`
endpoint onto the **multi-stream-input** WebSocket — the endpoint with
NATIVE server-side contexts, which gives us exact Cartesia semantics
(real barge-in cancel, per-context audio routing).

Protocol (elevenlabs.io/docs, /v1/text-to-speech/<voice>/multi-stream-input):
  - Connect:  wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}
              /multi-stream-input?model_id=<model>&output_format=<fmt>
              &inactivity_timeout=<s>
              Auth: `xi-api-key` header (query-param fallback).
              output_format is TRANSPORT-AWARE (iter66): browser lane plays
              raw PCM -> pcm_16000; twilio lane -> ulaw_8000 (Twilio Media
              Streams' native format, base64 frames forwarded UNCHANGED,
              zero transcoding).
  - Send (snake_case): {"text": s, "context_id": id, "flush": true}
              (flush on EVERY complete-sentence message — docs best
              practice); voice_settings may ride the FIRST message of a
              context; {"context_id": id, "close_context": true} ends a
              context; {"close_socket": true} ends everything; keepalive
              is {"text": " "} (a SPACE — a bare "" closes the socket).
  - Recv (mixed casing): {"audio": <base64>, "contextId": id,
              "is_final": bool}.

Context semantics (native now — no more emulation):
  - speak(ctx, text, continue_=True)  -> text+flush into the context
  - speak(ctx, text, continue_=False) -> text+flush, then close_context
  - ("", False) gate closing marker   -> close_context ONLY (never a
    context-less empty text — that shape CLOSES the whole socket)
  - cancel(ctx)                       -> close_context (real server-side
    stop; in-flight frames for that contextId are swallowed client-side)
  - 5 concurrent contexts per socket; we use 1 per turn (session opens a
    fresh context per turn), so at most 1 is ever open.
  - Context inactivity 20s default; `inactivity_timeout` ws-level param
    (max 180) + prewarm keepalive {"text": " "} every 15s keep pooled
    sockets adoptable (EL idle-kills far sooner than Cartesia).

Latency notes: eleven_flash_v2_5 is the low-latency tier (~75ms, $0.05/1k
chars); Multilingual v2 sounds best but TTFB is higher. `optimize_
streaming_latency` is DEPRECATED — removed from the URL.

V5 per-state delivery profiles (media/delivery.py): speak(overrides=...)
attaches merged voice_settings (stability/style/speed over the .env base)
to the FIRST text message of each new context. Clamped to 0-1 / 0.7-1.2.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Awaitable, Callable

import websockets

log = logging.getLogger("diallux.tts.elevenlabs")

EL_KEEPALIVE_S = 15   # prewarm keepalive cadence (prewarm.py imports this)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))

OnAudio = Callable[[str, str], Awaitable[None]]     # (base64_payload, context_id)


def connect_url(settings, output_format: str) -> str:
    """Single source of truth for the connect URL (adapter + prewarm share
    it — kills the duplication that caused the prewarm/provider mismatch)."""
    return (f"wss://api.elevenlabs.io/v1/text-to-speech/{settings.elevenlabs_voice_id}"
            f"/multi-stream-input?model_id={settings.elevenlabs_model_id}"
            f"&output_format={output_format}"
            f"&inactivity_timeout={settings.elevenlabs_inactivity_timeout}")


class ElevenLabsTTS:
    """Drop-in CartesiaTTS replacement over the multi-stream-input WebSocket."""

    def __init__(self, settings, on_audio: OnAudio, transport: str | None = None):
        self.settings = settings
        self.on_audio = on_audio
        # transport-aware output format (iter66): browser lane plays raw PCM
        self.transport = transport or settings.audio_transport
        self.output_format = "pcm_16000" if self.transport == "browser" else "ulaw_8000"
        self._ws = None
        self._recv_task: asyncio.Task | None = None
        self._closed = False
        self.first_byte_ts: float | None = None
        self._send_lock = asyncio.Lock()
        self._open_contexts: set[str] = set()      # contexts with text sent, not closed
        self._settings_ctx: set[str] = set()       # contexts that already carried voice_settings
        self._cancelled: set[str] = set()          # barge-in race guard (in-flight frames)

    # ------------------------------------------------------------------ #
    async def connect(self, ws=None):
        """Fresh connect, or ADOPT a prewarmed socket (ws= skips
        websockets.connect). No connect-time init message on
        multi-stream-input — voice_settings ride the first message of
        each context."""
        if ws is not None:
            self._ws = ws
            self._recv_task = asyncio.create_task(self._recv_loop())
            log.info("elevenlabs adopted prewarmed socket")
            return
        url = connect_url(self.settings, self.output_format)
        headers = {"xi-api-key": self.settings.elevenlabs_api_key}
        try:
            self._ws = await websockets.connect(url, additional_headers=headers,
                                                max_size=None, open_timeout=10)
        except Exception:
            # some gateways dislike custom headers on upgrade: key as query param
            self._ws = await websockets.connect(
                url + f"&xi-api-key={self.settings.elevenlabs_api_key}",
                max_size=None, open_timeout=10)
        self._recv_task = asyncio.create_task(self._recv_loop())
        log.info("elevenlabs connected (model=%s voice=%s fmt=%s)",
                 self.settings.elevenlabs_model_id, self.settings.elevenlabs_voice_id,
                 self.output_format)

    async def close(self):
        self._closed = True
        if self._recv_task:
            self._recv_task.cancel()
        if self._ws:
            try:
                await self._send_json({"close_socket": True})   # documented graceful end
            except Exception:
                pass
            try:
                await self._ws.close()
            except Exception:
                pass

    async def _send_json(self, obj: dict):
        if self._ws is None or self._closed:
            return
        try:
            async with self._send_lock:
                await self._ws.send(json.dumps(obj))
        except Exception as exc:
            log.warning("elevenlabs send failed: %s", exc)

    # ------------------------------------------------------------------ #
    async def speak(self, context_id: str, text: str, continue_: bool,
                    overrides: dict | None = None):
        """One SentenceGate chunk. Text messages ALWAYS carry flush:true
        (docs best practice: flush at the end of complete sentences).
        continue_=False additionally closes the context (frees one of the
        5 context slots; session opens a fresh context per turn).
        ("", False) is the gate's closing marker (sentence_gate.py) ->
        close_context only — NEVER a bare {"text": ""}, which closes the
        whole socket. overrides (V5 delivery profiles): voice_settings for
        THIS context — merged over the .env base and sent with the first
        text message of the context."""
        if self._ws is None or self._closed:
            return
        if not text and continue_:
            return
        if text:
            msg: dict = {"text": text, "context_id": context_id, "flush": True}
            if overrides and context_id not in self._settings_ctx:
                msg["voice_settings"] = self._merged_voice_settings(overrides)
                self._settings_ctx.add(context_id)
            self._open_contexts.add(context_id)
            await self._send_json(msg)
        if not continue_ and (text or context_id in self._open_contexts):
            await self._send_json({"context_id": context_id, "close_context": True})
            self._open_contexts.discard(context_id)
            self._settings_ctx.discard(context_id)

    def _merged_voice_settings(self, overrides: dict) -> dict:
        """Profile fields win over the .env base; clamped to API ranges."""
        s = self.settings
        stability = _clamp(overrides.get("stability", s.elevenlabs_stability), 0.0, 1.0)
        style = _clamp(overrides.get("style", s.elevenlabs_style), 0.0, 1.0)
        speed = _clamp(overrides.get("speed", s.elevenlabs_speed), 0.7, 1.2)
        return {
            "stability": stability,
            "similarity_boost": s.elevenlabs_similarity_boost,
            "style": style,
            "use_speaker_boost": True,
            "speed": speed,
        }

    async def cancel(self, context_id: str):
        """Barge-in: REAL server-side cancel (close_context) — in-flight
        frames for that contextId are swallowed by the recv router until
        its is_final clears the guard."""
        if self._ws is None or self._closed:
            return
        self._cancelled.add(context_id)
        await self._send_json({"context_id": context_id, "close_context": True})
        self._open_contexts.discard(context_id)
        self._settings_ctx.discard(context_id)

    async def keepalive(self):
        """Socket keepalive — documented shape {"text": " "} (a SPACE;
        a bare "" closes the socket)."""
        await self._send_json({"text": " "})

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
                await self._handle_message(json.loads(raw))
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            if not self._closed:
                log.error("elevenlabs recv loop ended: %s", exc)

    async def _handle_message(self, msg: dict):
        """RECV frame: {"audio": b64, "contextId": id, "is_final": bool}
        (camelCase contextId, snake_case is_final). Routing by contextId;
        is_final clears ONLY that context's cancel-guard."""
        audio = msg.get("audio")
        ctx = msg.get("contextId", "")
        final = bool(msg.get("is_final"))
        if audio:
            if ctx and ctx in self._cancelled:
                return                      # in-flight frames of a cancelled turn: swallow
            if self.first_byte_ts is None:
                self.first_byte_ts = time.perf_counter()
            await self.on_audio(audio, ctx)
        if final:
            self._cancelled.discard(ctx)
            self._open_contexts.discard(ctx)
