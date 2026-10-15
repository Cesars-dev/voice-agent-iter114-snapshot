"""CallSession — V3 production orchestration.

V1 flow (Twilio -> Deepgram -> LangGraph -> gate -> Cartesia -> Twilio) plus:

  - mark-based hangup: after end_call audio, a `mark` frame confirms Twilio
    finished PLAYING the goodbye before we close the stream (timer fallback)
  - STT auto-reconnect: Deepgram socket death mid-call transparently reconnects
  - buffer-overflow watchdog: Twilio warning 31931 (media discarded) is logged
    + counted, not silently ignored
  - eager end-of-turn (optional): EagerEndOfTurn starts the LLM speculatively;
    TurnResumed cancels it (Deepgram guarantees the eager transcript equals
    the final EndOfTurn transcript when no resume happens)
  - Prometheus metrics per turn + per call (observability/metrics.py)
  - V3: provider-agnostic TTS (media/tts_factory.py — Cartesia OR ElevenLabs,
    both native ulaw/mulaw @ 8000) and deterministic speech normalization on
    every spoken sentence (media/normalize.py — the Retell-native layer).
  - V5: per-state delivery profiles (media/delivery.py — the state machine
    controls emotion/speed; the profile follows the state whose prompt
    generated the speech: snapshotted at turn END, applied from the next
    turn, exactly like Retell's per-state prompt semantics).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from ..config import Settings
from ..graph.builder import CallRuntime
from ..observability.latency import TurnClock
from ..observability.tracer import Tracer
from .deepgram_stt import DeepgramSTT
from .delivery import resolve_delivery
from .normalize import normalize_for_tts
from .sentence_gate import SentenceGate
from .tts_factory import create_tts, tts_label

log = logging.getLogger("diallux.session")

HANGUP_GRACE_S = 1.2   # fallback when hangup_mode=timer or no mark arrives
PAUSE_LADDER_CLOSE_GRACE_S = 1.2   # FIX 2: grace after close-rung speech before hangup


def _stamp_clock_event(clock: TurnClock, data: dict) -> None:
    """iter112 clock truth (iter97 port): stamp the turn clock from builder
    custom events.

    First-only per turn: the graph emits `{"llm_dispatch": True}` when a round
    dispatches to the LLM and `{"llm_first_event": True}` at the first SDK
    event of ANY type; later rounds repeat them but the turn-level stamp is
    the FIRST one (the turn clock owns the user-perceived budget). Under the
    speak-first pipeline the graph emits both from the SPEAK stream only."""
    if "llm_dispatch" in data:
        if clock.t_llm_dispatch is None:
            clock.mark_llm_dispatch()
    elif "llm_first_event" in data:
        if clock.t_llm_first_event is None:
            clock.mark_llm_first_event()


def _ssml_jitter(settings) -> str:
    """Per-sentence inline Cartesia SSML: volume/speed ratios with natural
    jitter (t11 voicemail recipe finding: mechanical + reliable; kills the
    robotic flatness). Occasionally a sentence dips slightly more — real
    speech isn't uniform. Tags are forward-applying and open at the request
    boundary, so streaming never reads them aloud."""
    import random
    j_s = getattr(settings, "tts_jitter_speed", 0.06)
    j_v = getattr(settings, "tts_jitter_volume", 0.10)
    s = random.uniform(1.0 - j_s, 1.0 + j_s)
    v = random.uniform(1.0 - j_v, 1.0 + j_v)
    if random.random() < 0.10:                   # occasional natural dip
        s *= 0.97
        v *= 0.94
    s = max(0.6, min(1.5, s))
    v = max(0.5, min(2.0, v))
    return f'<volume ratio="{v:.2f}"/><speed ratio="{s:.2f}"/> '


class CallSession:
    # iter43 T1: EOT telemetry defaults (class-level: some tests build
    # CallSession objects without __init__)
    _eager_transcript: str | None = None
    _resumed_count = 0
    # iter55 T0 (F-06): class-level default — some tests build CallSession
    # objects without __init__; the greeting anchors must never AttributeError.
    _greet: dict | None = None
    # iter65 T4: mic-span anchors (class-level for the same no-__init__ tests)
    _t_speech_growth: float | None = None
    _last_update_len: int = 0
    # FIX 5 (iter84): post-interrupt backoff deadline (class-level for the
    # no-__init__ tests; stamped on real barge-in, read+cleared one-shot).
    _backoff_until: float | None = None
    # iter93 T2 (FIND-35 epoch discipline) — replaces the _eager_held boolean:
    #   _epoch           monotonic generation counter (bumped in sync zones)
    #   _held            epoch whose audio the gate is holding (None = open)
    #   _tts_ctx_epoch   epoch of the current TTS context (stale-cancel guard)
    #   _turn_task_epoch epoch of the live _run_turn (adopt-eligibility guard)
    # Class-level: no-__init__ tests.
    _epoch: int = 0
    _held: int | None = None
    _tts_ctx_epoch: int = 0
    _turn_task_epoch: int = 0

    def __init__(
        self,
        settings: Settings,
        ws,
        stream_sid: str,
        call_sid: str,
        custom_parameters: dict | None = None,
        llm_json: dict | None = None,
        tracer: Tracer | None = None,
        transport: str | None = None,
    ):
        self.settings = settings
        self.ws = ws
        self.stream_sid = stream_sid
        self.call_sid = call_sid
        self.llm_json = llm_json or json.loads(open(settings.agent_llm_json).read())
        self.custom_parameters = custom_parameters or {}
        # iter21: codec layer only. twilio = 8k mulaw Media Streams frames;
        # browser = 16-bit PCM @ browser_sample_rate (/mic ws). Everything
        # after the STT ingest / TTS output format is identical.
        self.transport = transport or settings.audio_transport

        self.tracer = tracer
        self.stt: DeepgramSTT | None = None
        self.tts: CartesiaTTS | None = None
        self.gate: SentenceGate | None = None
        self.runtime: CallRuntime | None = None

        self._turn_task: asyncio.Task | None = None
        self._writer_task: asyncio.Task | None = None
        self._epoch = 0
        self._held: int | None = None
        self._tts_ctx_epoch = 0
        self._turn_task_epoch = 0
        self._outbox: asyncio.Queue[dict | None] = asyncio.Queue()
        self._clock = TurnClock()
        self._speak_clock: TurnClock | None = None   # turn whose TTS is speaking
        self._tts_context: str | None = None
        self._turn_state: str | None = None   # V5: state whose profile voices the current turn
        self._first_turn = True
        self._initial_payload: dict | None = None
        # iter104 PT-1 (FIND-37): set on the growth-rerun path — the fresh
        # run's ingest REPLACES the previous variant of this utterance in
        # history (consumed by _run_turn; never leaks across turns).
        self._supersede_fragment: str | None = None
        self._stopped = False
        self._ended = False
        self._pending_end_mark: str | None = None
        self._turn_mark_seq = 0
        self.turn_reports: list[dict] = []
        # iter38 F2: the graph thread config, shared by _run_turn /
        # _late_turn_report / stop (the 2026-09-09 disaster call died on a
        # NameError here: `config` was a local of _run_turn, so the late
        # report's aget_state raised and the post-goodbye hangup never fired).
        self._thread_config = {"configurable": {"thread_id": self.call_sid}}
        self._real_turns = 0                       # iter38 F3: hard cap counter
        self._cap_closed = False                   # iter38 F3: graceful close fires once
        self._call_start = 0.0                     # iter38 F3: wall-clock guard (set in start())
        # iter43 T1: EOT telemetry — eager transcript + false-start counter
        self._eager_transcript: str | None = None  # transcript seen at EagerEndOfTurn
        self._resumed_count = 0                    # TurnResumed (false-start) counter
        # iter55 T0 (F-06): dedicated greeting-runway anchors — independent
        # of the turn-0 clock, so turn 1's clock swap never contaminates them.
        self._greet: dict | None = None
        # iter59 T3: flux-Update speech-window fire guard. BOOLEAN — clocks
        # swap at EOT/EagerEOT, NOT StartOfTurn, so turn-index keying is wrong
        # mid-speech (C8). Cleared in _on_eot/_on_eager_eot AFTER the fire
        # decision, and in _on_start_of_turn.
        self._update_fired = False
        # iter65 T4: mic-span anchors — last transcript-growth instant +
        # its length (eot_silence_wait + tts_first_byte spans).
        self._t_speech_growth: float | None = None
        self._last_update_len = 0

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    async def start(self):
        known = set(self.llm_json.get("default_dynamic_variables", {}))
        persona = {k: v for k, v in self.custom_parameters.items() if k in known}

        # iter21: browser bridge calls trace as micbridge-<session> with the
        # SAME span/score schema — only the trace name differs.
        session_name = (f"micbridge-{self.call_sid}" if self.transport == "browser"
                        else "diallux-call")
        self.tracer = self.tracer or Tracer(
            session_name=session_name,
            metadata={
                "agent": "diallux-langgraph-production",
                "llm_model": self.settings.openai_model,
                "call_sid": self.call_sid,
                "stream_sid": self.stream_sid,
                "transport": self.transport,
                "stt_mode": self.settings.deepgram_mode,
                "eager_eot": self.settings.deepgram_eager_eot,
                "tts": tts_label(self.settings),
                "rag_mode": self.settings.rag_mode,
            },
            enabled=self.settings.langfuse_enabled,
        )
        self.runtime = CallRuntime(self.settings, self.llm_json, tracer=self.tracer)
        self._initial_payload = dict(self.runtime.initial_state(self.call_sid, persona))
        self._turn_state = self._initial_payload.get("state_name") or self.llm_json.get("starting_state", "Intake")
        # iter49 T3: last-known dvs (updated post-turn) — the eager-EOT
        # live-retrieve fire uses these; dvs refinement lands next round
        # (pack degrade semantics).
        self._turn_dvs: dict = dict(self._initial_payload.get("dvs") or {})

        # eager end-of-turn wiring
        eager = self.settings.deepgram_eager_eot or self.settings.deepgram_eager_eot_threshold is not None
        self.stt = DeepgramSTT(
            self.settings,
            on_eot=self._on_eot,
            on_start_of_turn=self._on_start_of_turn,
            on_disconnect=self._on_stt_disconnect,
            on_state=self._on_stt_state,          # iter43 T1: STT state telemetry
            on_eager=self._on_eager_eot if (eager and self.settings.deepgram_eager) else None,
            on_turn_resumed=self._on_turn_resumed if (eager and self.settings.deepgram_eager) else None,
            on_update=self._on_stt_update,        # iter59 T3: speech-window/hybrid fire
            transport=self.transport,
        )
        if eager and self.settings.deepgram_eager:
            self.settings.deepgram_eager_eot_threshold = self.settings.deepgram_eager_eot_threshold or 0.6

        self.tts = create_tts(self.settings, on_audio=self._on_tts_audio,
                              transport=self.transport)
        self.gate = SentenceGate(self._speak_chunk,
                                 managed=self.settings.cartesia_buffering == "managed",
                                 flush_len=self.settings.gate_flush_len,
                                 chop_on_question=getattr(self.settings, "ask_hold", False))

        self._writer_task = asyncio.create_task(self._writer())
        self._liveness_task: asyncio.Task | None = None
        if getattr(self.settings, "stt_rx_zombie_ms", 0) > 0:
            self._liveness_task = asyncio.create_task(
                self._stt_liveness_watchdog())
        # iter59 T5: adopt a prewarmed STT socket when the pool has one
        # (get() POPS the slot so the maintainer respawns a fresh idle);
        # adoption failure NEVER fails the call — fall back to fresh connect.
        adopted = False
        if self.settings.call_prewarm:
            try:
                from .prewarm import get as pool_get
                pw = pool_get("stt",
                              max_age_s=getattr(self.settings,
                                                "stt_adopt_max_age_s",
                                                120.0))   # FIX 7 age gate
                if pw is not None:
                    await self.stt.connect(ws=pw)
                    adopted = True
            except Exception:
                adopted = False
        if not adopted:
            await self.stt.connect()
        # iter59 T5: same adoption for TTS (fallback: fresh connect).
        adopted_tts = False
        if self.settings.call_prewarm:
            try:
                from .prewarm import get as pool_get
                tws = pool_get("tts")
                if tws is not None:
                    await self.tts.connect(ws=tws)
                    adopted_tts = True
            except Exception:
                adopted_tts = False
        if not adopted_tts:
            await self.tts.connect()
        self._call_start = time.perf_counter()     # iter38 F3: 10-min guard origin
        # iter21: warm the RAG path during the greeting — the ~1.7s cold
        # embed never lands on the caller's first turn.
        if self.runtime is not None:
            # iter44 T2: resolve the KB store BEFORE the prewarm fires so the
            # warm path and the hot path always see the SAME store decision.
            # Bounded (2.0 s) + best-effort: a store outage never blocks the
            # call start (the resolve latch retries on the first turn).
            try:
                await asyncio.wait_for(self.runtime._resolve_kb_store(), 2.0)
            except Exception:
                pass
            asyncio.get_event_loop().create_task(self.runtime.warm_rag(
                self._turn_state,
                dict(self._initial_payload.get("dvs") or {})))
            # iter43 T3: prewarm the FULL Intake payload (tools + head + history)
            # during the greeting playback — turn 2+ runs on a warm prefix.
            # warm_prompt_cache is fire-and-forget and spawns its own task.
            self.runtime.warm_prompt_cache(
                self._turn_state,
                self._initial_payload.get("history") or [],
                dvs=dict(self._initial_payload.get("dvs") or {}))
            # iter46 T3: ALSO warm the first_turn_lite shape (general_prompt +
            # VOICE_OUTPUT_RULES only, no tools) — the turn-1 hot payload
            # byte-diverges from the full-Intake shape at byte 0, so without
            # the lite warm turn 1 paid cache_read=0 every call (audit tax #3).
            self.runtime.warm_prompt_cache(
                self._turn_state,
                self._initial_payload.get("history") or [],
                None, lite=True)
            # iter114d: warm the SPEAK arm of the initial state (Intake) —
            # the diet-view prefix on the resolved speak twin; parallel to
            # the full/lite warms, fire-and-forget.
            self.runtime.warm_speak_cache(
                self._turn_state,
                self._initial_payload.get("history") or [],
                dvs=dict(self._initial_payload.get("dvs") or {}))

        # begin_message (start_speaker: agent) — spoken immediately, no LLM.
        # V5: the greeting gets the "begin" delivery profile (warm, energetic).
        begin = (self._initial_payload.get("history") or [{}])[0].get("content", "")
        ctx = self.tts.new_context_id()
        self._tts_context = ctx
        # iter55 T0: greeting runway anchors — stream start → first audio out
        # → drain, measured on dedicated state (never the turn-0 clock).
        self._greet = {"ctx": ctx, "t_stream": time.perf_counter(),
                       "first_audio": None, "last_audio": None}
        await self.tts.speak(ctx, begin, continue_=False,
                              overrides=resolve_delivery(self.settings, "begin"))
        asyncio.get_event_loop().create_task(self._greeting_drain())
        if self.settings.metrics_enabled:
            from ..observability import metrics
            metrics.CALLS.inc()
        log.info("call %s started (sid=%s)", self.call_sid, self.stream_sid)

    async def stop(self, reason: str = "stopped"):
        if self._stopped:
            return
        self._stopped = True
        if self._liveness_task:
            self._liveness_task.cancel()
            self._liveness_task = None
        self._ladder_disarm()
        if self._turn_task:
            self._turn_task.cancel()
        if self.gate:
            await self.gate.close()
        if self.stt:
            await self.stt.close()
        if self.tts:
            await self.tts.close()
        await self._outbox.put(None)
        if self.runtime:
            try:
                state = await self.runtime.graph.aget_state(self._thread_config)
                values = state.values or {}
                convo = [
                    {"role": m.get("role"), "content": m.get("content", "")}
                    for m in values.get("history", []) if m.get("role") in ("user", "assistant")
                ]
                booked = bool((values.get("dvs") or {}).get("booking_verified"))
                if booked and self.settings.metrics_enabled:
                    from ..observability import metrics
                    metrics.BOOKINGS.inc()
                self.tracer.finish(output={
                    "conversation": convo,
                    "tool_trace": self.runtime.executor.trace,
                    "final_state": values.get("state_name"),
                    "ended": values.get("ended", self._ended),
                    "dynamic_variables": values.get("dvs", {}),
                    "gate_rejections": self.runtime.executor.gate_rejections,
                    "turn_reports": self.turn_reports,
                    "stop_reason": reason,
                })
            except Exception:
                self.tracer.finish(output={"stop_reason": reason, "turn_reports": self.turn_reports})
            await self.runtime.aclose()
        log.info("call %s stopped (%s)", self.call_sid, reason)

    # ------------------------------------------------------------------ #
    # Twilio events
    # ------------------------------------------------------------------ #
    async def on_media(self, payload_b64: str):
        if self.stt:
            self._mic_chunks = getattr(self, "_mic_chunks", 0) + 1
            if self._mic_chunks == 1:
                log.info("mic audio FIRST chunk received (sid=%s)", self.call_sid)
            elif self._mic_chunks % 100 == 0:
                log.info("mic audio flowing: %d chunks (sid=%s)",
                         self._mic_chunks, self.call_sid)
            await self.stt.send_audio(_b64decode(payload_b64))

    async def _speak_exact(self, text: str) -> None:
        """FIX 2/iter82 (PORT 2): Vapi say.exact - deterministic text spoken
        straight to TTS on a fresh context. Zero LLM rounds, zero graph
        state, zero history writes. Same pattern as the begin-message."""
        if self._stopped or self._ended or not self.tts:
            return
        try:
            ctx = self.tts.new_context_id()
            await self.tts.speak(ctx, text, False)
        except Exception:
            log.exception("say.exact failed on call %s", self.call_sid)

    # FIX 2 (PORT 2): pause-ladder state (Vapi customer.speech.timeout)
    _ladder_task: asyncio.Task | None = None
    _ladder_step = 0

    def _ladder_disarm(self) -> None:
        if self._ladder_task is not None:
            self._ladder_task.cancel()
            self._ladder_task = None

    def _ladder_arm(self) -> None:
        """FIX 2: agent finished speaking - the silence clock starts now.
        Armed at the Twilio mark echo (playback-confirmed) or the browser
        drain. Any caller speech disarms (Vapi reset-on-speech)."""
        if getattr(self.settings, "pause_ladder_mode", "off") != "reminder":
            return
        if self._stopped or self._ended:
            return
        if self._ladder_task is not None:   # iter94: ladder already armed
            return                          # mid-stretch — an agent turn must
                                            # NOT restart the silence clock
        self._ladder_disarm()
        self._ladder_step = 0
        self._ladder_task = asyncio.get_event_loop().create_task(
            self._ladder_run())

    async def _ladder_run(self) -> None:
        nudge_ms = float(getattr(self.settings, "pause_reask_ms", 15000))
        max_n = int(getattr(self.settings, "pause_ladder_max", 1))
        close_ms = float(getattr(self.settings, "pause_close_ms", 45000))
        t0 = time.perf_counter()
        try:
            step = 0
            while step < max_n:
                await self._sleep_until(t0 + nudge_ms * (step + 1) / 1000.0)
                if self._stopped or self._ended:
                    return
                text = (self.settings.pause_reminder_text if step == 0
                        else self.settings.pause_reminder_text_2)
                await self._speak_exact(text)
                step += 1
                self._ladder_step = step
                if self.tracer:
                    self.tracer.span("pause_ladder:nudge",
                                     metadata={"step": step})
                log.info("pause ladder rung %d spoken (reminder)", step)
            await self._sleep_until(t0 + close_ms / 1000.0)
            if self._stopped or self._ended:
                return
            await self._ladder_close()
        except asyncio.CancelledError:
            return

    async def _sleep_until(self, due: float) -> None:
        remaining = due - time.perf_counter()
        if remaining > 0:
            await asyncio.sleep(remaining)

    async def _ladder_close(self) -> None:
        """Close rung: honest goodbye + hangup (deterministic)."""
        if self._stopped or self._ended:
            return
        log.warning("pause ladder close on call %s (caller absent, nudges=%d)",
                    self.call_sid, self._ladder_step)
        if self.tracer:
            self.tracer.span("pause_ladder:close",
                             metadata={"nudges": self._ladder_step})
        await self._speak_exact(self.settings.pause_close_text)
        await asyncio.sleep(PAUSE_LADDER_CLOSE_GRACE_S)
        try:
            await self.ws.close(code=1000)
        except Exception:
            pass
        await self.stop(reason="pause_ladder_close")

    async def on_twilio_mark(self, msg: dict):
        """Playback-confirmed hangup: end_call audio has finished PLAYING."""
        name = ((msg.get("mark") or {}).get("name") or "")
        if self._pending_end_mark and name == self._pending_end_mark:
            self._pending_end_mark = None
            await self.ws.close(code=1000)
            await self.stop(reason="end_call_played")
        elif name.startswith("turn-") and name.endswith("-done"):
            self._ladder_arm()               # FIX 2: audio finished playing

    async def on_twilio_warning(self, msg: dict):
        """Twilio 31931 etc. — media discarded downstream (buffer overflow)."""
        log.warning("twilio stream warning: %s", json.dumps(msg)[:300])
        self.tracer.span("twilio:warning", output=msg)

    # ------------------------------------------------------------------ #
    # STT callbacks
    # ------------------------------------------------------------------ #
    async def _on_eot(self, transcript: str):
        if self._stopped or self._ended:
            return
        # owner order 2026-10-12: EVERY caller EOT resets the pause ladder —
        # a fresh silence stretch starts only after the caller's answer; the
        # nudge can never carry over from a previous exchange (live call
        # CA17fc16…: three rung-1 asks inside one real conversation).
        self._ladder_disarm()
        self._ladder_step = 0
        if self._cap_hit():
            await self._graceful_cap_close()    # iter38 F3 (PT-34)
            return
        task = self._turn_task
        if task and not task.done():
            # iter38 F1 (PT-33) + iter93 T2 (FIND-35 §c Q4): ADOPT only when a
            # live EAGER turn of the CURRENT epoch confirms with the SAME
            # transcript (Deepgram's documented contract when no resume ran).
            # Anything else = growth / foreign generation — never answer the
            # prefix: kill the generation, discard, fresh run.
            if (self._clock.extra.get("eager")
                    and self._turn_task_epoch == self._epoch
                    and self._eager_transcript == transcript):
                k = self._epoch
                # iter43 T1: head-start telemetry — t_eager was stamped in
                # _on_eager_eot (mark_user_end fired there, so the live metric
                # stt_eot_to_llm_first_ms is actually EagerEOT-anchored).
                t_eager = self._clock.extra.get("t_eager")
                if t_eager is not None:
                    self._clock.extra["t_final"] = time.perf_counter()
                    self._clock.extra["head_start_ms"] = round(
                        (self._clock.extra["t_final"] - t_eager) * 1000, 1)
                    self._clock.extra["eager_final_match"] = True
                self._eager_transcript = None
                self._clock.extra["resume_pending"] = False   # iter85 T3: never set; cleared for safety
                self._update_fired = False   # iter59: adopted — fires already dispatched at EagerEOT
                # iter93 T2 (FIND-35): SYNC-clear the hold BEFORE awaiting the
                # flush — then release(upto=k); the gate lifts ONLY its own
                # barrier (a stale release of an older epoch is a no-op).
                _gate = getattr(self, "gate", None)
                if self._held == k and _gate:
                    self._held = None
                    self._clock.extra["released_epoch"] = k    # iter93 telemetry
                    t_eager0 = self._clock.extra.get("t_eager")
                    if t_eager0 is not None and self.tracer:
                        self.tracer.span("eager_hold:release", metadata={
                            "hold_to_release_ms":
                                round((time.perf_counter() - t_eager0) * 1000, 1),
                            "turn": self._clock.turn_index})
                    await _gate.release(upto=k)   # audio flows NOW
                    log.info("eager turn audio released at confirmed EOT (epoch=%d)", k)
                log.info("eot adopted running eager turn (no rerun, epoch=%d)", k)
                return
            # not adoptable: kill the generation BEFORE the fresh run —
            # its remnants (buffer/parked/queued) die here, floor-guarded
            task.cancel()
            _gate0 = getattr(self, "gate", None)
            if _gate0:
                _gate0.invalidate(self._epoch)      # SYNC: remnants die NOW
            if self._clock.extra.get("eager"):
                log.info("eot grew vs eager snapshot — discarding epoch, "
                         "fresh run (never answer the prefix)")
            # iter104 PT-1 (FIND-37): the previous run of THIS turn already
            # wrote its user variant (the eager fragment or a prior grown
            # final) into history — the fresh run's ingest must REPLACE it,
            # never append a second entry (the echo the model parrots back).
            self._supersede_fragment = self._eager_transcript or transcript
        # iter93 T2: a pending eager hold whose confirm never came (dead or
        # killed generation) dies HERE, on the rerun path — the barrier must
        # not survive into the fresh turn (a plain fresh turn touches nothing).
        k_old = self._epoch
        if self._held == k_old:
            self._held = None
            _gate = getattr(self, "gate", None)
            if _gate:
                _gate.invalidate(k_old)             # SYNC: dead eager dies NOW
        # iter38 F5: carry the barge-in flag across the clock swap —
        # _on_start_of_turn marked it on the OLD clock; a fresh TurnClock here
        # silently dropped it (all 6 real barge-ins reported barge_in:false in
        # the 2026-09-09 disaster call).
        prev = self._clock
        self._clock = TurnClock(turn_index=prev.turn_index + 1)
        if prev.barge_in:
            self._clock.barge_in = True
            self._clock.t_barge_in = prev.t_barge_in
        self._clock.mark_user_end()
        # iter59 T3: fire mode dispatch at final EOT.
        #   hybrid      — lane B fires here (single-query utterance embed,
        #                 consumed inline by the running/starting round).
        #   speech-window — when an Update already fired this turn, SKIP the
        #                 fire: spawning again would CANCEL the landed
        #                 task and re-degrade (the exact iter58 bug).
        #   round-sync  — consume retrieves INLINE; firing a "full" task
        #                 here was a wasted embed+pgvector per turn (iter60
        #                 AUD-5).
        #   eot         — iter58 surface (fire at EOT).
        mode = getattr(self.settings, "rag_fire_mode", "eot")
        if mode == "hybrid":
            self._fire_live_retrieve(transcript, lane="laneB")
        elif mode == "speech-window" and self._update_fired:
            pass
        elif mode == "round-sync":
            pass    # iter60 AUD-5: consume retrieves INLINE — firing a
                    # "full" task here was a wasted embed+pgvector per turn
        else:
            self._fire_live_retrieve(transcript)
        self._update_fired = False
        self._epoch += 1                    # iter93 T2: fresh generation per turn
        self._turn_task_epoch = self._epoch
        _gate = getattr(self, "gate", None)
        if _gate:
            _gate.advance(self._epoch)      # SYNC: stale tokens of dying gens rejected
        self._turn_task = asyncio.create_task(
            self._run_turn(transcript, epoch=self._epoch))

    def _fire_live_retrieve(self, transcript: str, lane: str = "full") -> None:
        """iter49 T3: fire the batched lane retrieval at the EARLIEST moment
        the queries exist (EagerEOT / final EOT before the graph invoke) —
        the arctic embed (~90-190ms multi-lane) runs inside the caller's
        remaining speech + the speculative LLM start, so the round's bounded
        await only pays pgvector + merge. Best-effort, NEVER raises: the
        state_node consumer spawns a fallback when nothing is in flight."""
        # iter93 T1 (PT-79 probe): every fire attempt recorded with its lane —
        # the laneA-silence question answered with data, never inferred.
        try:
            self._clock.extra.setdefault("rag_fires", []).append(lane)
            if self.tracer:
                self.tracer.span("rag:fire", metadata={
                    "lane": lane, "turn": self._clock.turn_index,
                    "mode": getattr(self.settings, "rag_fire_mode", "eot")})
        except Exception:
            pass
        try:
            runtime = getattr(self, "runtime", None)
            if runtime is None:
                return
            runtime.spawn_live_retrieve(
                getattr(self, "_turn_state", None) or "Intake",
                dict(getattr(self, "_turn_dvs", None) or {}),
                transcript or "", lane=lane)
        except Exception as exc:
            try:
                log.warning("live-retrieve fire failed on call %s: %s",
                            getattr(self, "call_sid", "?"), exc)
            except Exception:
                pass

    async def _on_stt_update(self, transcript: str):
        """iter59 T3: flux TurnInfo.Update handler (~every 0.25 s of audio,
        cumulative turn transcript). Fires ONCE per turn (boolean guard):
          speech-window — the FULL multi-lane retrieval (partial transcript
                          is lane B's input; scopes/queries already exist).
          hybrid        — lane A ONLY (refer-tag queries + dv values:
                          utterance-INDEPENDENT, lands during speech).
        On a TurnResumed the same turn keeps its fire (lane A is
        utterance-independent; the EagerEOT/EOT path re-fires lane B with
        the final transcript)."""
        # iter65 T4: speech-growth anchor — the last instant the turn
        # transcript grew (EOT silence span = EagerEOT − this).
        if len(transcript) > self._last_update_len:
            self._ladder_disarm()            # FIX 2: speech growth resets ladder
            self._t_speech_growth = time.perf_counter()
            self._last_update_len = len(transcript)
        mode = getattr(self.settings, "rag_fire_mode", "eot")
        if mode not in ("speech-window", "hybrid"):
            return
        if self._stopped or self._ended or self._update_fired:
            return
        self._update_fired = True
        if mode == "speech-window":
            self._fire_live_retrieve(transcript)          # full lanes, partial utterance
        else:
            self._fire_live_retrieve("", lane="laneA")    # hybrid lane A: engine intent only

    async def _on_eager_eot(self, transcript: str):
        """Speculative LLM start (optional): runs the turn early; EndOfTurn keeps it.
        iter93 T2 (FIND-35): the hold arms in the SYNC ZONE — before any await
        or task spawn — so a same-tick confirmed EOT always finds the barrier
        and releases (the fire-lag orphan path is closed by construction)."""
        if self._stopped or self._ended:
            return
        if self._cap_hit():
            await self._graceful_cap_close()    # iter38 F3 (PT-34)
            return
        if self._turn_task and not self._turn_task.done():
            return                      # a turn is already running
        # ---- SYNC ZONE (FIND-35 law 1): generation state before the first await ----
        self._epoch += 1
        k = self._epoch
        self._eager_transcript = transcript             # adopt-guard snapshot
        self._tts_ctx_epoch = k
        gate = getattr(self, "gate", None)
        if getattr(self.settings, "eager_hold_audio_until_confirm", False) \
                and gate:
            self._held = k
            gate.hold(k)                # sync-effective: barrier set NOW
        log.info("eager turn started (speculative, epoch=%d)", k)
        prev = self._clock
        self._clock = TurnClock(turn_index=prev.turn_index + 1, extra={"eager": True})
        if self._held == k:
            self._clock.extra["held_epoch"] = k    # iter93 telemetry (rides the report)
        if prev.barge_in:                        # iter38 F5: carry barge-in across swap
            self._clock.barge_in = True
            self._clock.t_barge_in = prev.t_barge_in
        self._clock.mark_user_end()
        # iter43 T1: stamp the eager anchor — head_start_ms = EagerEOT → EndOfTurn
        self._clock.extra["t_eager"] = time.perf_counter()
        # iter65 T4: eot_silence_wait span — EagerEOT minus the last instant
        # the transcript GREW (the caller's trailing silence the EOT waited
        # through). Observability-only.
        if self._t_speech_growth is not None and self.tracer:
            self.tracer.span("eot_silence_wait", metadata={
                "ms": round((time.perf_counter() - self._t_speech_growth) * 1000, 1),
                "turn": self._clock.turn_index})
        # iter85 measure (owner 350/600 experiment): silence actually waited
        # BEFORE the eager fire, at the CURRENT threshold (0.35 on the lane)
        # — a lower threshold fires sooner on the same audio; this span +
        # eager_hold:false_start count = the speed-vs-stability read.
        if self.tracer:
            self.tracer.span("eager_hold:fire", metadata={
                "silence_ms": round((time.perf_counter() - self._t_speech_growth)
                                    * 1000, 1)
                if self._t_speech_growth is not None else None,
                "eager_threshold": getattr(
                    self.settings, "deepgram_eager_eot_threshold", None),
                "epoch": k,
                "turn": self._clock.turn_index})
        # iter59 T3: EagerEOT fire dispatch (the round STARTS here — the
        # consumer is about to run):
        #   hybrid      — lane B fires NOW (the eager transcript IS the final
        #                 transcript per Deepgram contract when no resume);
        #                 an EOT adopt reuses it (the adopt return below must
        #                 not double-fire).
        #   speech-window — skip when an Update landed (landed task beats a
        #                 superseding spawn).
        #   round-sync  — consume retrieves INLINE; firing a "full" task
        #                 here was a wasted embed+pgvector per turn (iter60
        #                 AUD-5).
        #   eot         — iter58 surface.
        mode = getattr(self.settings, "rag_fire_mode", "eot")
        if mode == "hybrid":
            self._fire_live_retrieve(transcript, lane="laneB")
        elif mode == "speech-window" and self._update_fired:
            pass
        elif mode == "round-sync":
            pass    # iter60 AUD-5: consume retrieves INLINE — firing a
                    # "full" task here was a wasted embed+pgvector per turn
        else:
            self._fire_live_retrieve(transcript)
        self._update_fired = False
        self._turn_task_epoch = k
        self._turn_task = asyncio.create_task(self._run_turn(transcript, epoch=k))

    async def _on_turn_resumed(self):
        """User kept talking: the speculative turn is cancelled.
        iter93 T2 (FIND-35): the generation is invalidated in the SYNC ZONE
        (before any await) — the deferred-write clobber that swallowed the
        next generation's hold (the 48.94 trace) is impossible by
        construction; the slow cleanup is epoch-tagged (law 2)."""
        # iter43 T1: false-start counter (pre-registered EOT winner criterion)
        self._resumed_count += 1
        task = self._turn_task
        task_live = bool(task and not task.done())
        clock_eager = self._clock.extra.get("eager")
        # iter93 T2: act on a LIVE eager turn OR a finished-but-still-held
        # one (the LLM completed before the resume — orphan path 2: the
        # parked final + barrier must die here, not strand the gate).
        if not ((task_live and clock_eager)
                or (clock_eager and self._held is not None)):
            return
        k = self._epoch
        held_this_gen = self._held == k
        # ---- SYNC ZONE ----
        if held_this_gen:
            self._held = None              # never clobber a NEWER hold
        if task_live:
            task, self._turn_task = self._turn_task, None
            task.cancel()
        self._clock.extra["eager"] = False
        self._clock.extra["resume_pending"] = False
        # ---- async zone: epoch-tagged cleanup (touches only <= k artifacts) ----
        _gate = getattr(self, "gate", None)
        if _gate:
            _gate.invalidate(k)            # SYNC: buffer/parked/queued gen-k chunks die NOW
        if not held_this_gen:
            # nothing was held: audio may be flowing — cancel + clear (today's shape)
            if self.tts and self._tts_context and self._tts_ctx_epoch <= k:
                await self.tts.cancel(self._tts_context)
            self._send({"event": "clear", "streamSid": self.stream_sid})
        if self.tracer:
            t_eager1 = self._clock.extra.get("t_eager")
            self.tracer.span("eager_hold:false_start", metadata={
                "held_ms": round((time.perf_counter() - t_eager1) * 1000, 1)
                if t_eager1 is not None else None,
                "turn": self._clock.turn_index})
        log.info("eager turn discarded (user resumed, epoch=%d, silent=%s)",
                 k, held_this_gen)

    async def _on_stt_state(self, msg: dict):
        """iter43 T1: raw STT state telemetry (Flux TurnInfo events etc.).
        Telemetry-only — never mutates turn flow."""
        try:
            mtype = msg.get("type")
            if mtype == "TurnInfo":
                event = msg.get("event")
                if event in ("StartOfTurn", "TurnResumed"):
                    log.info("stt state: %s (turn %s)", event, msg.get("turn_index"))
                # Update/EagerEndOfTurn/EndOfTurn are already handled elsewhere —
                # keep this quiet to avoid double-logging every transcript.
        except Exception:
            pass

    async def _on_start_of_turn(self):
        if self._stopped:
            return
        self._ladder_disarm()                # FIX 2: user speech resets ladder
        # ---- SYNC ZONE (FIND-35): bump epoch + clear hold FIRST, cleanup second ----
        self._epoch += 1                     # everything older is now stale
        self._held = None
        task, self._turn_task = self._turn_task, None
        interrupted = bool(task and not task.done())
        if interrupted:
            task.cancel()
            self._clock.barge_in = True
            self._clock.t_barge_in = time.perf_counter()
            if self.settings.metrics_enabled:
                from ..observability import metrics
                metrics.BARGEINS.inc()
            # FIX 5 (PORT 5): stamp the post-interrupt backoff window here —
            # this is TODAY's real-barge confirm point (the ack-filter branch
            # of _barge_resolve is a later-wave primitive, not in this tree).
            backoff = getattr(self.settings, "post_interrupt_backoff_ms", 0)
            if backoff > 0:
                self._backoff_until = time.perf_counter() + backoff / 1000.0
        # ---- async zone: epoch-tagged teardown (touches only <= epoch-1) ----
        _gate = getattr(self, "gate", None)
        if _gate:
            _gate.invalidate(self._epoch - 1)   # SYNC teardown of pre-barge artifacts
        if self.tts and self._tts_context and self._tts_ctx_epoch <= self._epoch - 1:
            await self.tts.cancel(self._tts_context)
        self._send({"event": "clear", "streamSid": self.stream_sid})
        # iter59 T3: a new speech turn begins — re-arm the Update fire guard.
        # (C8: boolean, cleared here AND at EOT/EagerEOT; never turn-index keyed.)
        self._update_fired = False
        # iter65 T4: reset the mic-span anchors for the new turn.
        self._t_speech_growth = None
        self._last_update_len = 0
        if interrupted:
            log.info("barge-in on call %s (epoch bumped to %d)",
                     self.call_sid, self._epoch)

    async def _stt_liveness_watchdog(self):
        """FIX 7 (PORT 7): audio is flowing but the STT socket is silent —
        the adopted-zombie class (39s piped, zero messages). Fires the
        EXISTING transparent reconnect (_on_stt_disconnect); the call
        continues. Deliberately long horizon: flux emits nothing on
        silence, so a quiet caller is indistinguishable at the message
        level — the false-positive cost is one transparent reconnect."""
        zombie_ms0 = getattr(self.settings, "stt_rx_zombie_ms", 0)
        if zombie_ms0 <= 0:
            return                      # knob off: no watchdog at all
        while not getattr(self, "_stopped", False):
            await asyncio.sleep(1.0)
            try:
                if getattr(self, "stt", None) is None                         or getattr(self, "_mic_chunks", 0) == 0:
                    continue
                zombie_ms = zombie_ms0
                last = self.stt.last_rx or 0.0
                silent_for = time.monotonic() - last
                if silent_for > zombie_ms / 1000.0:
                    log.warning("stt zombie suspected: no frames for %.1fs "
                                "(mic chunks=%d) - forcing reconnect",
                                silent_for, self._mic_chunks)
                    if self.tracer:
                        self.tracer.span("stt:zombie_reconnect", metadata={
                            "silent_s": round(silent_for, 1),
                            "mic_chunks": self._mic_chunks})
                    await self._on_stt_disconnect()
                    return                     # the reconnect path rebuilds
            except Exception:
                log.exception("stt liveness watchdog tick failed")

    async def _on_stt_disconnect(self):
        """V2: transparent STT reconnect (audio gap, call continues)."""
        if self._stopped or not self.settings.deepgram_reconnect:
            return
        log.warning("deepgram dropped mid-call; reconnecting")
        self.tracer.span("stt:reconnect", output={"call_sid": self.call_sid})
        try:
            await self.stt.close()
        except Exception:
            pass
        eager = self.settings.deepgram_eager
        self.stt = DeepgramSTT(
            self.settings,
            on_eot=self._on_eot,
            on_start_of_turn=self._on_start_of_turn,
            on_disconnect=self._on_stt_disconnect,
            on_state=self._on_stt_state,          # iter43 T1: STT state telemetry
            on_eager=self._on_eager_eot if eager else None,
            on_turn_resumed=self._on_turn_resumed if eager else None,
            on_update=self._on_stt_update,        # iter59 T3: survive reconnect
            transport=self.transport,
        )
        try:
            await self.stt.connect()
        except Exception as exc:
            log.error("stt reconnect failed: %s", exc)

    # ------------------------------------------------------------------ #
    # The brain
    # ------------------------------------------------------------------ #
    async def _run_turn(self, transcript: str, epoch: int = 0):
        """One graph execution: LLM token stream -> gate -> TTS.
        iter93 T2: epoch-threaded — every gate.add carries the producing
        generation, and the task's own cleanup is epoch-tagged (law 2)."""
        clock = self._clock
        self._speak_clock = clock                   # TTS marks route to THIS turn
        self._real_turns += 1                       # iter38 F3: real graph executions
        config = self._thread_config
        self._tts_context = self.tts.new_context_id() if self.tts else None
        self._tts_ctx_epoch = epoch                 # stale tts.cancel guard epoch
        try:
            payload: dict[str, Any] = {"user_text": transcript}
            supersede = getattr(self, "_supersede_fragment", None)
            if supersede is not None:
                # iter104 PT-1: the confirmed final replaces the previous
                # variant of this utterance (flag consumed HERE — a cancelled
                # rerun can never leak it into a later turn).
                payload["user_text_supersedes"] = True
                self._supersede_fragment = None
            if self._first_turn:
                payload.update(self._initial_payload or {})
                self._first_turn = False
            async for mode, data in self.runtime.graph.astream(
                payload, config=config, stream_mode=["custom", "updates"]
            ):
                if mode == "custom" and isinstance(data, dict):
                    if "tts_token" in data:
                        if clock.t_llm_first is None:
                            clock.mark_llm_first()
                        self.gate.add(data["tts_token"], epoch)
                    else:
                        # iter112 clock truth (iter97 port, first-only/turn)
                        _stamp_clock_event(clock, data)
        except asyncio.CancelledError:
            # iter93 T2 (FIND-35 law 2): cleanup is epoch-tagged + SYNC —
            # touches only artifacts of generations <= epoch; a newer
            # barrier/floor survives untouched.
            self.gate.invalidate(epoch)
            # iter112: this generation may have executed tools whose dvs
            # patches were dropped with the cancellation (eager discard /
            # barge-in rerun). Clear the executor's repeat guard so the retry
            # turn's legitimate re-executions are not strangled as
            # already_captured (E3 booking-killer: empty slot_options).
            try:
                self.runtime.executor.reset_repeat_guard()
            except Exception:      # pragma: no cover — never mask the cancel
                pass
            return
        except Exception as exc:
            log.exception("turn failed on call %s: %s", self.call_sid, exc)
            return

        await self.gate.end_of_turn()
        clock.mark_turn_done()
        report = clock.report()
        self.turn_reports.append(report)
        self.tracer.span(f"turn:{clock.turn_index}", output=report)
        log.info("turn %d report: %s", clock.turn_index, json.dumps(report))
        # iter21: TTS audio often arrives AFTER the graph turn completes
        # (sentence gate flush -> Cartesia TTFB) — the early report above
        # structurally misses tts_first/audio_out. A late follow-up span
        # captures the COMPLETE budget once audio has flowed (or timed out);
        # metrics are emitted once, on the complete report.
        asyncio.get_event_loop().create_task(self._late_turn_report(clock))

    async def _late_turn_report(self, clock: TurnClock):
        for _ in range(20):                          # up to ~3s of drain wait
            await asyncio.sleep(0.15)
            if clock.t_audio_out is not None:
                break
        final = clock.report()
        # iter43 T1: false-start counter rides on the final report (clock.extra
        # would drop it across the resume swap — it is call-scoped, not turn-scoped)
        final["resumed_count"] = self._resumed_count
        # iter93 T1 (FIND-35 sensor): armed the hold, completed, never
        # released, never spoke = the swallow disease, flagged with data.
        if (final.get("held_epoch") is not None
                and final.get("eager_final_match") is True
                and final.get("released_epoch") is None
                and clock.t_tts_first is None):
            final["held_swallow"] = True
            log.warning("held_swallow anomaly: turn %d armed hold epoch %s, "
                        "completed with no release and no audio",
                        clock.turn_index, final.get("held_epoch"))
        # iter93 T1 (PT-79): last consume provenance on the turn report.
        consume = getattr(getattr(self, "runtime", None), "_last_consume", None)
        if consume:
            final["rag_consume"] = consume
        # iter103 PT-8: pre-LLM gap stopwatch (observability-only) — the
        # round entry → LLM dispatch breakdown rides the final report.
        pre_llm = getattr(getattr(self, "runtime", None),
                          "_last_round_pre_llm", None)
        if pre_llm:
            final["pre_llm_ms"] = pre_llm
        self.tracer.span(f"turn:{clock.turn_index}:final", output=final)
        if self.settings.metrics_enabled:
            from ..observability import metrics
            metrics.observe_turn_report(final)
        log.info("turn %d final report: %s", clock.turn_index, json.dumps(final))

        # mark frame: Twilio echoes it back once the audio finished PLAYING
        self._turn_mark_seq += 1
        self._send({"event": "mark", "streamSid": self.stream_sid,
                    "mark": {"name": f"turn-{clock.turn_index}-done"}})
        if self.transport == "browser":
            self._ladder_arm()               # FIX 2 (browser fallback arm)

        try:
            snap = await self.runtime.graph.aget_state(self._thread_config)
            vals = snap.values or {}
            if vals.get("ended"):
                self._ended = True
                # mark-based hangup needs Twilio's mark echo; the browser ws
                # has no echo, so the timer drain applies there.
                if self.settings.hangup_mode == "mark" and self.transport == "twilio":
                    self._pending_end_mark = f"turn-{clock.turn_index}-done"
                else:
                    asyncio.get_event_loop().create_task(self._hangup_after_drain())
            # V5: the post-transition state becomes the delivery profile for
            # the NEXT turn — turn N's speech was generated by turn N's
            # starting state's prompt, so that is whose profile voiced it.
            if vals.get("state_name"):
                self._turn_state = vals["state_name"]
            # iter49 T3: keep the last-known dvs for the eager-EOT fire.
            self._turn_dvs = dict(vals.get("dvs") or {})
        except Exception:
            # iter38 F2: NEVER silent — the 2026-09-09 disaster call lost the
            # post-goodbye hangup to a swallowed NameError here.
            log.exception("post-turn state check failed on call %s", self.call_sid)

    async def _greeting_drain(self):
        """iter55 T0 (F-06): one greeting summary span after the drain window
        (mirrors the `_late_turn_report` follow-up-task pattern). Dedicated
        anchors only — never touches the turn-0 clock."""
        try:
            await asyncio.sleep(7.0)
            greet = self._greet
            if greet is None or greet.get("first_audio") is None:
                return
            self.tracer.span("greeting", metadata={
                "stream_to_first_ms":
                    round((greet["first_audio"] - greet["t_stream"]) * 1000),
                "audio_drain_ms":
                    round((greet["last_audio"] - greet["first_audio"]) * 1000),
                "total_ms":
                    round((greet["last_audio"] - greet["t_stream"]) * 1000),
            })
        except Exception:
            pass

    async def _hangup_after_drain(self):
        await asyncio.sleep(HANGUP_GRACE_S)
        try:
            await self.ws.close(code=1000)
        except Exception:
            pass
        await self.stop(reason="end_call")

    def _cap_hit(self) -> bool:
        """iter38 F3: turn cap (64) OR wall-clock cap (600s) — Julio-tuned
        2026-09-09 after the 24-cap murdered a healthy 5-minute call."""
        if self._real_turns >= self.settings.max_call_turns:
            return True
        limit = getattr(self.settings, "max_call_seconds", 0)
        if limit and self._call_start and \
                (time.perf_counter() - self._call_start) >= limit:
            return True
        return False

    async def _graceful_cap_close(self):
        """iter38 F3 (PT-34): the call blew past the turn/time cap — say so
        honestly and hang up. Deterministic (no LLM): the same failure mode as
        the 2026-09-09 disaster call (97 turns, 11 minutes, no closure) can
        never repeat. Fires BETWEEN turns only — a mid-flight booking chain
        always finishes first."""
        if self._cap_closed:
            return
        self._cap_closed = True
        elapsed = round(time.perf_counter() - self._call_start, 1) if self._call_start else None
        log.warning("call cap reached on call %s (turns=%d, elapsed_s=%s) — graceful close",
                    self.call_sid, self._real_turns, elapsed)
        self.tracer.span("session:turn_cap_close", output={"real_turns": self._real_turns})
        line = ("We're having some temporary technical issues on my end — I'll register "
                "everything we discussed and Jay will call you back. Have a great day!")
        # FIX 1 (PORT 1, declared adaptation): clear any hold from a turn that
        # errored while held — a held gate would park the cap-close line's
        # final marker and the caller would never hear the close.
        if self._held is not None:
            self._held = None
            self.gate.invalidate(self._epoch)
        self._tts_context = self.tts.new_context_id() if self.tts else None
        if self.gate:
            self.gate.add(line, self._epoch)
            await self.gate.end_of_turn()
        await asyncio.sleep(HANGUP_GRACE_S)
        try:
            await self.ws.close(code=1000)
        except Exception:
            pass
        await self.stop(reason="turn_cap")

    # ------------------------------------------------------------------ #
    # TTS plumbing
    # ------------------------------------------------------------------ #
    async def _speak_chunk(self, text: str, continue_: bool):
        # iter65 T4: first TTS request of the turn — stamp the request anchor
        # (tts_first_byte span = first audio byte minus this). getattr guards:
        # some tests build CallSession objects without __init__.
        clock = getattr(self, "_speak_clock", None) or getattr(self, "_clock", None)
        if clock is not None and clock.extra.get("t_tts_req") is None:
            # FIX 5 (PORT 5): one-shot post-interrupt backoff — read + clear the
            # window, sleep out the remainder so the caller's answer lands first.
            backoff_until = getattr(self, "_backoff_until", None)
            if backoff_until is not None:
                self._backoff_until = None
                wait = backoff_until - time.perf_counter()
                if wait > 0:
                    await asyncio.sleep(wait)
            clock.extra["t_tts_req"] = time.perf_counter()
        if self.tts and self._tts_context:
            # V3 speech normalization: what Retell did in its platform layer,
            # now deterministic and self-hosted. Runs per flushed sentence —
            # µs of regex, never touches history/dvs/prompts.
            if self.settings.tts_normalize and text:
                text = normalize_for_tts(text, phone_style=self.settings.tts_phone_style)
            # iter21 naturalness: per-sentence inline SSML variance (Cartesia).
            # Tags sit at the START of the request (never mid-tag), so the
            # streaming path never speaks them — verified by A/B duration.
            if text and self.settings.tts_provider == "cartesia" \
                    and getattr(self.settings, "tts_delivery_jitter", False):
                text = _ssml_jitter(self.settings) + text
            # V5 delivery profile: dict lookup on the turn's state — the
            # state machine, not the LLM, decides how it sounds.
            await self.tts.speak(
                self._tts_context, text, continue_,
                overrides=resolve_delivery(self.settings, self._turn_state),
            )

    async def _on_tts_audio(self, b64_payload: str, context_id: str):
        # iter55 T0 (F-06): dedicated greeting anchors — stamped BEFORE the
        # turn-0 clock logic; the greeting span uses these, never the clock.
        greet = self._greet
        if greet is not None and context_id == greet.get("ctx"):
            now = time.perf_counter()
            if greet.get("first_audio") is None:
                greet["first_audio"] = now
                if getattr(self.settings, "warm_observability", True) \
                        and self.tracer:
                    self.tracer.span("greeting:first_audio", metadata={
                        "stream_to_first_ms":
                            round((now - greet["t_stream"]) * 1000)})
            greet["last_audio"] = now
        clock = self._speak_clock or self._clock    # iter21: the TURN being spoken, not the newest
        if clock.t_tts_first is None:
            clock.mark_tts_first()
            # iter65 T4: tts_first_byte span — first audio byte minus the
            # turn's first TTS request (tracer None-guard; once per turn).
            t_req = clock.extra.get("t_tts_req")
            if t_req is not None and self.tracer:
                self.tracer.span("tts_first_byte", metadata={
                    "ms": round((clock.t_tts_first - t_req) * 1000, 1),
                    "turn": clock.turn_index})
        if self.transport == "browser":
            self._send_raw(_b64decode(b64_payload))    # raw PCM s16le -> /mic ws
        else:
            self._send({"event": "media", "streamSid": self.stream_sid,
                        "media": {"payload": b64_payload}})
        # iter38 F6: t_audio_out is stamped by the WRITER after the bytes are
        # actually written to the socket (was stamped here — same instant as
        # tts_first — which made tts_first_to_audio_out_ms a structural ~0.0).

    # ------------------------------------------------------------------ #
    # outbound writer (single sender for the Twilio/browser WS)
    # ------------------------------------------------------------------ #
    def _send(self, msg: dict):
        try:
            self._outbox.put_nowait(msg)
        except Exception:
            pass

    def _send_raw(self, audio: bytes):
        try:
            self._outbox.put_nowait(audio)
        except Exception:
            pass

    async def _writer(self):
        try:
            while True:
                msg = await self._outbox.get()
                if msg is None:
                    break
                try:
                    if isinstance(msg, (bytes, bytearray)):
                        await self.ws.send_bytes(bytes(msg))
                        # iter38 F6: first real socket write of this speak clock =
                        # audio OUT (caller-audible boundary).
                        clock = self._speak_clock or self._clock
                        if clock.t_audio_out is None:
                            clock.mark_audio_out()
                    else:
                        await self.ws.send_text(json.dumps(msg))
                except RuntimeError:
                    # iter38: ws already closed (e.g. the cap-close path) — the
                    # 2026-09-09 :8007 test died on a send-after-close warning.
                    break
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.warning("session writer stopped: %s", exc)


def _b64decode(s: str) -> bytes:
    import base64
    return base64.b64decode(s)
