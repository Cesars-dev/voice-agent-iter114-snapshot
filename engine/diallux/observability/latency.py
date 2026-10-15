"""Per-turn latency measurement (the <1200ms budget, instrumented).

Timestamps captured by the media session:
  t_user_end        Deepgram EndOfTurn received (user finished speaking)
  t_llm_dispatch    graph dispatched the round to the LLM (iter97 port)
  t_llm_first_event first SDK event of ANY type (token or tool_call_start)
  t_llm_first       first LLM content token
  t_tts_first       first Cartesia audio chunk received
  t_audio_out       first audio frame handed to Twilio
  t_turn_done       graph invocation finished
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class TurnClock:
    turn_index: int = 0
    t_user_end: float | None = None
    t_llm_dispatch: float | None = None    # iter112 (iter97 port): graph dispatched the round to the LLM
    t_llm_first_event: float | None = None # iter112 (iter97 port): first SDK event of ANY type (token or tool_call_start)
    t_llm_first: float | None = None
    t_tts_first: float | None = None
    t_audio_out: float | None = None
    t_turn_done: float | None = None
    t_barge_in: float | None = None
    barge_in: bool = False
    extra: dict = field(default_factory=dict)

    def mark_user_end(self):        self.t_user_end = time.perf_counter()
    def mark_llm_dispatch(self):    self.t_llm_dispatch = time.perf_counter()
    def mark_llm_first_event(self): self.t_llm_first_event = time.perf_counter()
    def mark_llm_first(self):       self.t_llm_first = time.perf_counter()
    def mark_tts_first(self):       self.t_tts_first = time.perf_counter()
    def mark_audio_out(self):       self.t_audio_out = time.perf_counter()
    def mark_turn_done(self):       self.t_turn_done = time.perf_counter()

    def report(self) -> dict:
        def ms(a, b):
            if a is None or b is None or b < a:
                return None
            return round((b - a) * 1000, 1)

        base = self.t_user_end
        return {
            "turn": self.turn_index,
            "stt_eot_to_llm_first_ms": ms(base, self.t_llm_first),
            # iter112 (iter97 port): EOT -> dispatch -> first SDK event -> first spoken token
            "stt_eot_to_llm_dispatch_ms": ms(base, self.t_llm_dispatch),
            "llm_dispatch_to_first_event_ms": ms(self.t_llm_dispatch, self.t_llm_first_event),
            "llm_first_event_to_llm_first_ms": ms(self.t_llm_first_event, self.t_llm_first),
            "llm_first_to_tts_first_ms": ms(self.t_llm_first, self.t_tts_first),
            "tts_first_to_audio_out_ms": ms(self.t_tts_first, self.t_audio_out),
            "e2e_response_ms": ms(base, self.t_audio_out),      # user stopped -> caller hears reply
            "e2e_turn_ms": ms(base, self.t_turn_done),          # -> full turn processed
            "barge_in": self.barge_in,
            **({"barge_in_at_ms": ms(base, self.t_barge_in)} if self.barge_in else {}),
            **self.extra,
        }
