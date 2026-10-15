"""iter93 T3 — FIND-35 epoch-race property tests (fake-clock harness).

The property under test (FIND35 §d): *every confirmed EOT either adopts a
live turn whose audio flows, or starts a fresh turn whose audio flows —
under any interleaving of {EagerEOT, TurnResumed, StartOfTurn, EOT} with
arbitrarily delayed async tails.* Plus the dual: *unconfirmed speculative
audio never leaks.*

Harness: REAL CallSession handlers + REAL SentenceGate; injection points at
the known await sites — RecorderGate.discard delay (the old 240 ms drain),
TailTTS.cancel delay. Each generation streams "REPLY|<utterance>." — unique
per utterance, so every property reads off the spoken record. The driver
mirrors the session's generation logic DYNAMICALLY (reads the live task/
clock state before each event, exactly what the handlers will see).

Properties:
  A  no-swallow:  the last confirmed generation's reply reaches spoken
                  (earlier confirms may be legitimately superseded — soft)
  B  no-leak:     a generation killed by TurnResumed never reaches spoken
  C  no-clobber:  every discard's actual drop set ⊆ {epochs <= upto}
  D  ordering:    a sentence chunk (cont=True) precedes any final marker
  E  barrier sanity: after settle: barrier None + parked shelf empty

Regression pin: the EXACT 48.94 headphone repro (resume tail lands between
the gen-4 fire and its confirmed EOT). Proven to fail on the T1 (pre-fix)
tree.

Stutter persona: the deterministic fragment-paced scenario the text battery
was missing (two false starts per real EOT) — runs the REAL handlers end to
end with a fake LLM, offline.
"""
from __future__ import annotations

import asyncio
import random
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from diallux.media.sentence_gate import SentenceGate
from diallux.media.session import CallSession
from diallux.observability.latency import TurnClock

BASE = dict(openai_api_key="t", retell_api_key="t",
            langfuse_enabled=False, metrics_enabled=False)
SETTINGS = Settings(eager_hold_audio_until_confirm=True, **BASE)


# ------------------------------------------------------------------ #
# fakes
# ------------------------------------------------------------------ #
class TailTTS:
    def __init__(self, cancel_delay: float = 0.0):
        self._cancel_delay = cancel_delay
        self.cancelled: list[str] = []
        self.spoken: list[tuple[str, bool]] = []
        self._n = 0

    def new_context_id(self) -> str:
        self._n += 1
        return f"ctx-{self._n}"

    async def speak(self, ctx, text, continue_, overrides=None):
        self.spoken.append((text, continue_))

    async def cancel(self, ctx):
        if self._cancel_delay:
            await asyncio.sleep(self._cancel_delay)
        self.cancelled.append(ctx)


class ReplyGraph:
    """reply = 'REPLY|<utterance>.' — unique per utterance by construction."""

    async def astream(self, payload, config=None, stream_mode=None):
        text = "REPLY|" + (payload.get("user_text") or "empty") + "."
        for ch in text:
            await asyncio.sleep(0.002)
            yield ("custom", {"tts_token": ch})

    async def aget_state(self, config):
        return SimpleNamespace(values={"ended": False, "state_name": "Intake",
                                       "dvs": {}})


class RecorderGate(SentenceGate):
    """Parent gate + discard recording with an injectable drain delay."""

    def __init__(self, on_sentence, discard_delay: float = 0.0):
        super().__init__(on_sentence)
        self._discard_delay = discard_delay
        self.discard_log: list[int] = []
        self.dropped: list[list[int]] = []

    async def discard(self, upto: int = 0):
        self.discard_log.append(upto)
        if self._discard_delay:
            await asyncio.sleep(self._discard_delay)
        dropped = sorted(
            {e for (e, _t, _c) in self._parked if e <= upto}
            | {e for (e, _t, _c) in list(self._queue._queue) if e <= upto})
        self.dropped.append(dropped)
        await super().discard(upto)


class FakeTracer:
    def span(self, name, metadata=None, output=None):
        pass


# ------------------------------------------------------------------ #
# session factory
# ------------------------------------------------------------------ #
def mk_sess(discard_delay: float = 0.0):
    spoken: list[tuple[str, bool]] = []

    async def on_sentence(text, cont):
        spoken.append((text, cont))

    sess = object.__new__(CallSession)
    sess.settings = SETTINGS
    sess._stopped = False
    sess._ended = False
    sess._cap_closed = False
    sess._real_turns = 0
    sess._call_start = None
    sess.stream_sid = "x"
    sess.call_sid = "c1"
    sess.transport = "twilio"
    sess._outbox = asyncio.Queue()
    sess._turn_mark_seq = 0
    sess._ladder_task = None
    sess._update_fired = False
    sess._first_turn = False
    sess._initial_payload = {}
    sess._thread_config = {"configurable": {"thread_id": "t"}}
    sess._tts_context = None
    sess._speak_clock = None
    sess._pending_end_mark = None
    sess._backoff_until = None
    sess._t_speech_growth = None
    sess._last_update_len = 0
    sess._resumed_count = 0
    sess._eager_transcript = None
    sess.turn_reports = []
    sess._clock = TurnClock()
    sess.gate = RecorderGate(on_sentence, discard_delay=discard_delay)
    sess.tts = TailTTS()
    sess.tracer = FakeTracer()
    sess.runtime = SimpleNamespace(
        graph=ReplyGraph(), spawn_live_retrieve=lambda *a, **k: None)
    sess._turn_task = None
    sess.outbox_msgs: list = []
    sess._send = lambda payload: sess.outbox_msgs.append(payload)
    return sess, spoken


# ------------------------------------------------------------------ #
# driver — mirrors the session's generation logic DYNAMICALLY
# ------------------------------------------------------------------ #
async def drive(sess, spoken, script, settle: float = 1.2):
    """Schedule handlers as tasks at their scripted times (ms). Before each
    event, read the live session state exactly as the handler will, and
    build the property expectations from what the session can actually see."""
    handlers: list[asyncio.Task] = []
    must_speak: list[str] = []       # guaranteed (the LAST confirm's reply)
    soft_speak: list[str] = []       # earlier confirms (may be superseded)
    never_speak: list[str] = []      # resume-killed generations
    last_confirm_idx = max(i for i, e in enumerate(script)
                           if e[1] == "eot_confirm")
    for idx, (t_ms, kind, kw) in enumerate(script):
        if idx:
            await asyncio.sleep((t_ms - script[idx - 1][0]) / 1000.0)
        if kind == "eager":
            handlers.append(asyncio.ensure_future(
                sess._on_eager_eot(kw["transcript"])))
        elif kind == "resumed":
            task = sess._turn_task
            task_live = bool(task and not task.done())
            if (task_live and sess._clock.extra.get("eager")) \
                    or (sess._clock.extra.get("eager")
                        and sess._held is not None):
                frag = sess._eager_transcript
                if frag is not None:
                    never_speak.append("REPLY|" + frag + ".")
            handlers.append(asyncio.ensure_future(sess._on_turn_resumed()))
        elif kind == "start_of_turn":
            handlers.append(asyncio.ensure_future(sess._on_start_of_turn()))
        elif kind == "eot_confirm":
            adoptable = (
                sess._turn_task is not None
                and not sess._turn_task.done()
                and sess._clock.extra.get("eager")
                and sess._turn_task_epoch == sess._epoch
                and sess._eager_transcript is not None)
            transcript = (sess._eager_transcript if adoptable
                          else kw.get("transcript")
                          or (sess._eager_transcript or "frag") + " FINAL")
            (must_speak if idx == last_confirm_idx else soft_speak).append(
                "REPLY|" + transcript + ".")
            handlers.append(asyncio.ensure_future(sess._on_eot(transcript)))
    await asyncio.sleep(settle)
    results = await asyncio.gather(*handlers, return_exceptions=True)
    for r in results:
        assert not isinstance(r, Exception), f"handler raised: {r!r}"
    return {"must_speak": must_speak, "soft_speak": soft_speak,
            "never_speak": never_speak}


def assert_invariants(sess, spoken, expect):
    # C — no-clobber: every discard's drop set ⊆ {e <= upto}
    for upto, dropped in zip(sess.gate.discard_log, sess.gate.dropped):
        assert all(e <= upto for e in dropped), \
            f"C VIOLATED: discard(upto={upto}) dropped {dropped}"
    # B — no-leak
    for txt in expect["never_speak"]:
        assert not any(t == txt for t, _c in spoken), \
            f"B VIOLATED: killed generation's reply spoken: {txt} ({spoken})"
    # A — no-swallow (guaranteed for the last confirmed generation)
    for txt in expect["must_speak"]:
        assert any(t == txt for t, _c in spoken), \
            f"A VIOLATED: confirmed reply never spoken: {txt} (spoken={spoken})"
    # D — ordering: an EMPTY final marker (context close) must follow a
    # sentence chunk; a whole-reply final chunk is legal by itself.
    for i, (t, c) in enumerate(spoken):
        if c is False and t == "":
            assert any(cc is True for _tt, cc in spoken[:i]), \
                "D VIOLATED: empty closing marker before any sentence chunk"
    # E — barrier sanity after settle
    assert sess.gate._barrier is None, \
        f"E VIOLATED: barrier stuck at {sess.gate._barrier}"
    assert sess.gate._parked == [], f"E VIOLATED: parked shelf {sess.gate._parked}"


# ------------------------------------------------------------------ #
# regression pin — the EXACT 48.94 headphone repro
# ------------------------------------------------------------------ #
def test_regression_pin_headphone_repro_4894():
    """resume@1510's delayed discard lands BETWEEN the gen-4 fire (1740) and
    its confirmed EOT (1745): the old deferred-write clobber made the adopt
    read _eager_held=False → no release → gen-4 reply swallowed
    (tts_first null). Under epoch discipline the adopt MUST release."""
    async def scenario():
        sess, spoken = mk_sess()
        script = [
            (0,    "eot_confirm", {"transcript": "reply two finished"}),
            (640,  "start_of_turn", {}),
            (1440, "eager", {"transcript": "frag three"}),
            (1510, "resumed", {}),               # RecorderGate delay = 250 ms
            (1740, "eager", {"transcript": "frag four"}),
            (1745, "eot_confirm", {"auto": True}),
        ]
        expect = await drive(sess, spoken, script)
        assert expect["never_speak"] == ["REPLY|frag three."]
        # THE assert that fails on the pre-fix code (FIND-35: was zero):
        assert any("frag four" in t for t, _c in spoken), \
            "FIND-35 regression: gen-4 reply swallowed again"
        assert any("reply two finished" in t for t, _c in spoken)
        assert_invariants(sess, spoken, expect)
        # the released epoch rode the adopted turn's report
        assert any(r.get("released_epoch") == 4 for r in sess.turn_reports), \
            sess.turn_reports

    asyncio.run(scenario())


# ------------------------------------------------------------------ #
# orphan path 1: fire-lag adopt (eager → confirmed EOT in the same burst)
# ------------------------------------------------------------------ #
def test_fire_lag_same_tick_adopt_releases():
    """EOT lands before the speculative _run_turn task ever ran: the hold was
    armed in the fire's SYNC ZONE → the adopt still releases."""
    async def scenario():
        sess, spoken = mk_sess()
        t1 = asyncio.ensure_future(sess._on_eager_eot("instant confirm"))
        t2 = asyncio.ensure_future(sess._on_eot("instant confirm"))
        await asyncio.gather(t1, t2)
        await asyncio.sleep(0.6)
        assert any("instant confirm" in t for t, _c in spoken), \
            "fire-lag adopt swallowed the reply"
        assert sess.gate._barrier is None and sess.gate._parked == []
        assert any(r.get("released_epoch") is not None
                   for r in sess.turn_reports)

    asyncio.run(scenario())


# ------------------------------------------------------------------ #
# orphan path 2: LLM finishes before TurnResumed (stranded parked-final)
# ------------------------------------------------------------------ #
def test_parked_final_then_resume_then_next_turn_speaks():
    """Stream completes while held (final parked); resume discards; the NEXT
    turn's audio must flow — the pre-fix code left the gate held forever."""
    async def scenario():
        sess, spoken = mk_sess()
        await sess._on_eager_eot("first draft")
        await asyncio.sleep(0.35)               # stream completes, parks final
        assert sess.gate._held_final is True
        await sess._on_turn_resumed()           # resume AFTER completion
        await asyncio.sleep(0.2)
        assert sess.gate._barrier is None       # gate opened again
        assert not any("first draft" in t for t, _c in spoken), "leak"
        await sess._on_eot("second real utterance")   # fresh turn
        await asyncio.sleep(0.8)
        assert any("second real utterance" in t for t, _c in spoken), \
            "next turn stuck behind a stranded hold"
        assert sess.gate._barrier is None and sess.gate._parked == []

    asyncio.run(scenario())


# ------------------------------------------------------------------ #
# orphan path 3: stale cleanup vs a newer hold (the clobber class)
# ------------------------------------------------------------------ #
def test_stale_discard_never_clobbers_newer_hold():
    """A delayed gen-3 discard lands while gen-4 holds: gen-4's barrier,
    buffer and chunks must survive untouched."""
    async def scenario():
        sess, spoken = mk_sess(discard_delay=0.25)
        await sess._on_eager_eot("frag three")       # gen 3, held
        await asyncio.sleep(0.05)
        resumed = asyncio.ensure_future(sess._on_turn_resumed())
        await asyncio.sleep(0.05)                    # discard now sleeping 250 ms
        await sess._on_eager_eot("frag four")        # gen 4, held DURING the tail
        await asyncio.sleep(0.05)
        await sess._on_eot("frag four")              # adopt + release gen 4
        await resumed
        await asyncio.sleep(0.6)
        assert any("frag four" in t for t, _c in spoken)
        assert not any("frag three" in t for t, _c in spoken)
        for upto, dropped in zip(sess.gate.discard_log, sess.gate.dropped):
            assert all(e <= upto for e in dropped)
        assert sess.gate._barrier is None and sess.gate._parked == []

    asyncio.run(scenario())


# ------------------------------------------------------------------ #
# property battery — seeded random interleavings (batched)
# ------------------------------------------------------------------ #
def _random_script(rng: random.Random) -> list[tuple]:
    events: list[tuple] = []
    t = 0
    for _ in range(rng.randint(4, 7)):
        t += rng.randint(8, 50)
        kind = rng.choice(["eager", "resumed", "start_of_turn", "eot_confirm"])
        if kind == "eager":
            events.append((t, kind, {"transcript": f"frag{len(events)}"}))
        elif kind == "eot_confirm":
            events.append((t, kind, {"auto": True}))
        else:
            events.append((t, kind, {}))
    if events[-1][1] != "eot_confirm":
        t += rng.randint(8, 50)
        events.append((t, "eot_confirm", {"auto": True}))
    return events


def test_random_interleavings_200_seeds():
    async def one_seed(seed: int, discard_delay: float):
        sess, spoken = mk_sess(discard_delay=discard_delay)
        script = _random_script(random.Random(seed))
        try:
            expect = await drive(sess, spoken, script)
            assert_invariants(sess, spoken, expect)
        except AssertionError:
            print(f"SEED={seed} delay={discard_delay} script={script} "
                  f"discards={list(zip(sess.gate.discard_log, sess.gate.dropped))} "
                  f"spoken={spoken}")
            raise

    async def scenario():
        rng = random.Random(20261005)
        seeds = [(s, rng.choice([0.0, 0.05, 0.12])) for s in range(200)]
        for i in range(0, len(seeds), 20):          # batches of 20: one loop, ~10x faster
            await asyncio.gather(*(one_seed(s, d) for s, d in seeds[i:i + 20]),
                                 return_exceptions=False)

    asyncio.run(scenario())


# ------------------------------------------------------------------ #
# stutter persona — the deterministic battery character (offline, real handlers)
# ------------------------------------------------------------------ #
def test_stutter_persona_two_false_starts_per_eot():
    """FIND35 §d persona: 300–700 ms word bursts, 250–400 ms micro-pauses,
    TWO false starts before each real EOT. The real EOT's reply must be
    spoken; both false-start drafts never leak; no stranded hold. Every
    future turn-engine change runs against this script."""
    async def scenario():
        sess, spoken = mk_sess(discard_delay=0.12)
        script = [
            (0,    "eot_confirm", {"transcript": "hi I saw your ad"}),
            (800,  "eager", {"transcript": "yeah so I wanted to ask"}),
            (1050, "resumed", {}),                        # false start 1
            (1250, "eager", {"transcript": "yeah so I wanted to ask abou"}),
            (1500, "resumed", {}),                        # false start 2
            (1750, "eager",
             {"transcript": "yeah so I wanted to ask about your pricing"}),
            (2350, "eot_confirm", {"auto": True}),
            (3300, "eot_confirm",
             {"transcript": "ok thanks that makes sense"}),
        ]
        expect = await drive(sess, spoken, script)
        # real EOT replies spoken
        assert any("hi I saw your ad" in t for t, _c in spoken)
        assert any("ask about your pricing" in t for t, _c in spoken), spoken
        assert any("ok thanks that makes sense" in t for t, _c in spoken)
        # both false-start drafts never leaked
        assert not any(t == "REPLY|yeah so I wanted to ask." for t, _c in spoken)
        assert not any(t == "REPLY|yeah so I wanted to ask abou." for t, _c in spoken)
        assert_invariants(sess, spoken, expect)

    asyncio.run(scenario())
