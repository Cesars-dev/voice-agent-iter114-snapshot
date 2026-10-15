"""Deepgram streaming STT client (raw WebSocket, no SDK).

Two backends, env-selectable:

  mode=flux   wss://api.deepgram.com/v2/listen?model=flux-general-en
              Conversational STT with model-integrated end-of-turn:
                TurnInfo.StartOfTurn   -> barge-in signal
                TurnInfo.EndOfTurn     -> user finished; full turn transcript
              (~260ms p50 end-of-turn detection at defaults)

  mode=nova3  wss://api.deepgram.com/v1/listen?model=nova-3
              Classic streaming: interim_results + endpointing(ms) + vad_events.
                Results(speech_final) -> end of utterance
                SpeechStarted         -> barge-in signal

Both take Twilio's native mulaw/8000 audio UNCHANGED (zero transcoding) and
batch 20ms Twilio frames into 80ms chunks (Deepgram's recommended size).

iter21: transport="browser" swaps the codec layer only — 16-bit linear16 PCM
at settings.browser_sample_rate (Chrome AudioContext resample), same 80ms
batching; everything downstream (EOT, callbacks, reconnect) is unchanged.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable

import websockets

log = logging.getLogger("diallux.stt")

OnEOT = Callable[[str], Awaitable[None]]            # end of turn transcript
OnStartOfTurn = Callable[[], Awaitable[None]]       # user started speaking (barge-in)
OnState = Callable[[dict], Awaitable[None]]         # raw protocol event (observability)


class DeepgramSTT:
    FRAME_MS = 80

    def __init__(self, settings, on_eot: OnEOT, on_start_of_turn: OnStartOfTurn,
                 on_state: OnState | None = None, on_disconnect=None,
                 on_eager=None, on_turn_resumed=None, transport: str | None = None,
                 on_update=None):
        self.settings = settings
        self.transport = transport or settings.audio_transport
        # twilio: mulaw 8k = 160 bytes / 20ms; browser: linear16 = 2 bytes/sample
        if self.transport == "browser":
            rate = settings.browser_sample_rate
            self.encoding = "linear16"
            self.sample_rate = rate
            self.BYTES_PER_FRAME = int(rate * 2 * (self.FRAME_MS / 1000))
        else:
            self.encoding = "mulaw"
            self.sample_rate = 8000
            self.BYTES_PER_FRAME = 160 * (self.FRAME_MS // 20)
        self.on_eot = on_eot
        self.on_start_of_turn = on_start_of_turn
        self.on_state = on_state
        self.on_disconnect = on_disconnect            # V2: mid-call reconnect hook
        self.on_eager = on_eager                      # V2: EagerEndOfTurn (speculative)
        self.on_turn_resumed = on_turn_resumed        # V2: cancel speculative turn
        self.on_update = on_update                    # iter59 T3: speech-window fire hook
        self._last_rx: float = 0.0            # FIX 7 (PORT 7): recv heartbeat
        self._ws = None
        self._recv_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._buffer = bytearray()
        self._closed = False
        self._final_buffer: list[str] = []          # nova3: accumulate finals until speech_final

    # ------------------------------------------------------------------ #
    def _url(self) -> str:
        s = self.settings
        if s.deepgram_mode == "flux":
            params = {
                "model": s.deepgram_model,
                "encoding": self.encoding,
                "sample_rate": str(self.sample_rate),
                "eot_threshold": str(s.deepgram_eot_threshold),
                "eot_timeout_ms": str(s.deepgram_eot_timeout_ms),
            }
            if s.deepgram_eager_eot_threshold is not None:
                params["eager_eot_threshold"] = str(s.deepgram_eager_eot_threshold)
            if s.deepgram_keyterms:
                params["keyterms"] = ",".join(s.deepgram_keyterms)
            from urllib.parse import urlencode
            return "wss://api.deepgram.com/v2/listen?" + urlencode(params)
        from urllib.parse import urlencode
        return "wss://api.deepgram.com/v1/listen?" + urlencode({
            "model": s.deepgram_model or "nova-3",
            "encoding": self.encoding,
            "sample_rate": str(self.sample_rate),
            "channels": "1",
            "interim_results": "true" if s.deepgram_nova_interim else "false",
            "vad_events": "true",
            "endpointing": str(s.deepgram_endpointing_ms),
            "smart_format": "true",
            "punctuate": "true",
        })

    @property
    def last_rx(self) -> float:
        """FIX 7: monotonic time of the last inbound frame (0 = never)."""
        return self._last_rx

    async def connect(self, ws=None):
        """Connect (3-attempt retry), or ADOPT a prewarmed socket (iter59 T5:
        ws passed in skips websockets.connect — the recv loop below binds the
        OWNING session's callbacks). An adopted socket keeps the pool's
        keepalive task; on adoption failure the caller falls back to a fresh
        connect (a call never fails on the pool)."""
        if ws is not None:
            self._ws = ws
            self._last_rx = time.monotonic()   # FIX 7: adopted socket stamps on bind
            self._recv_task = asyncio.create_task(self._recv_loop())
            log.info("deepgram adopted prewarmed socket (mode=%s)",
                     self.settings.deepgram_mode)
            return
        headers = {"Authorization": f"Token {self.settings.deepgram_api_key}"}
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                self._ws = await websockets.connect(
                    self._url(), additional_headers=headers, max_size=None, open_timeout=10
                )
                break
            except Exception as exc:
                last_exc = exc
                await asyncio.sleep(0.5 * (attempt + 1))
        else:
            raise ConnectionError(f"Deepgram connect failed: {last_exc}")
        self._recv_task = asyncio.create_task(self._recv_loop())
        self._last_rx = time.monotonic()           # FIX 7: fresh socket stamps on bind
        if self.settings.deepgram_mode == "nova3":
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        log.info("deepgram connected (mode=%s)", self.settings.deepgram_mode)

    # ------------------------------------------------------------------ #
    async def send_audio(self, audio_bytes: bytes):
        """Queue inbound frames (mulaw 8k or linear16 @ browser rate); forwarded
        in 80ms batches."""
        if self._ws is None or self._closed:
            return
        self._buffer.extend(audio_bytes)
        while len(self._buffer) >= self.BYTES_PER_FRAME:
            chunk = bytes(self._buffer[: self.BYTES_PER_FRAME])
            del self._buffer[: self.BYTES_PER_FRAME]
            try:
                await self._ws.send(chunk)
            except Exception as exc:
                log.warning("deepgram send failed: %s", exc)

    async def close(self):
        self._closed = True
        # iter61 AUD-13: when close() runs FROM the recv task — the reconnect
        # path — cancelling it would abort the reconnect itself; the loop is
        # already dying. Never cancel the current task.
        cur = asyncio.current_task()
        for task in (self._recv_task, self._keepalive_task):
            if task and task is not cur:
                task.cancel()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    async def _keepalive_loop(self):
        try:
            while not self._closed:
                await asyncio.sleep(5.0)
                if self._ws:
                    await self._ws.send(json.dumps({"type": "KeepAlive"}))
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.warning("deepgram keepalive stopped: %s", exc)

    async def _recv_loop(self):
        try:
            while not self._closed:
                raw = await self._ws.recv()
                self._last_rx = time.monotonic()   # FIX 7: heartbeat on every frame
                if isinstance(raw, bytes):
                    continue
                msg = json.loads(raw)
                if self.on_state:
                    await self.on_state(msg)
                await self._handle(msg)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            if not self._closed:
                log.error("deepgram recv loop ended: %s", exc)
                if self.on_disconnect:
                    # iter61 AUD-13: the hook CLOSES/CANCELS this recv task
                    # (close() cancels _recv_task) — awaiting it from inside
                    # the recv task self-cancelled the reconnect silently
                    # (CancelledError is not an Exception). Schedule it on
                    # its own task; this recv task just returns.
                    asyncio.get_running_loop().create_task(
                        self.on_disconnect())

    async def _handle(self, msg: dict):
        mtype = msg.get("type")
        if mtype == "TurnInfo":                      # Flux v2
            event = msg.get("event")
            if getattr(self.settings, "deepgram_raw_dump", False):
                # iter93 T1 (data-first, FIND-35): raw TurnInfo payload —
                # confidence semantics + event interleaving evidence.
                log.info("flux raw dump: %s", json.dumps(msg)[:1200])
            transcript = (msg.get("transcript") or "").strip()
            if event == "EndOfTurn" and transcript:
                await self.on_eot(transcript)
            elif event == "StartOfTurn":
                await self.on_start_of_turn()
            elif event == "EagerEndOfTurn" and transcript and self.on_eager:
                await self.on_eager(transcript)
            elif event == "Update" and transcript and self.on_update:
                # iter59 T3: flux Update = ~every 0.25 s of audio, transcript
                # CUMULATIVE within the turn (docs: no is_final field — finality
                # is EagerEndOfTurn/EndOfTurn only). First non-empty Update =
                # ~0.3 s into speech: the hybrid lane-A / speech-window fire.
                await self.on_update(transcript)
            elif event == "TurnResumed" and self.on_turn_resumed:
                await self.on_turn_resumed()
            # stateless Updates (empty transcript) / other events: ignored
        elif mtype == "Results":                     # nova3 v1
            if not msg.get("is_final"):
                return
            alt = ((msg.get("channel") or {}).get("alternatives") or [{}])[0]
            text = (alt.get("transcript") or "").strip()
            if text:
                self._final_buffer.append(text)
            if msg.get("speech_final"):
                full = " ".join(self._final_buffer).strip()
                self._final_buffer = []
                if full:
                    await self.on_eot(full)
        elif mtype == "SpeechStarted":               # nova3 VAD -> barge-in
            await self.on_start_of_turn()
        elif mtype == "UtteranceEnd":                # optional hard cut
            full = " ".join(self._final_buffer).strip()
            self._final_buffer = []
            if full:
                await self.on_eot(full)
        elif mtype in ("Metadata",):
            pass
        elif msg.get("type") == "Error" or "error" in str(mtype).lower():
            log.error("deepgram error event: %s", json.dumps(msg)[:400])
