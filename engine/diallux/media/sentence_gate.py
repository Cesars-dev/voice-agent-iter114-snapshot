"""Sentence gate: LLM token stream -> TTS-ready text chunks, strictly ordered.

Custom buffering mode (Cartesia docs recommendation):
  - accumulate streamed tokens; flush on sentence-ending punctuation once a
    minimum length is reached (first sentence leaves ASAP);
  - hard cut at the runaway cap (Settings.gate_flush_len, default 500 —
    iter103 PT-4: sentence boundaries flush FIRST; the old 140 constant
    chopped mid-clause, FIND-38) so a rambling turn still speaks early;
  - idle timeout flush (150ms without a token) so half-sentences still voice;
  - every flush uses continue=true EXCEPT the end-of-turn marker (continue=false
    minimizes latency, per Cartesia context docs; an empty final chunk is the
    documented way to close a context when the text is already flushed).

Ordering guarantee: all on_sentence calls go through one internal asyncio.Queue
with a single sender task — chunks can never reorder, even when end_of_turn's
final marker races a just-flushed sentence (the naive fire-and-forget version
could emit the closing empty chunk BEFORE the sentence, closing the Cartesia
context prematurely — caught by the test suite).

Managed mode: tokens are forwarded immediately and Cartesia's own buffering
(max_buffer_delay_ms) decides when to start speaking.

iter93 T2 (FIND-35 epoch discipline): the gate owns the held-state —
  _barrier: the epoch whose audio is held (None = open). NO side-channel
  boolean. Every queued item carries its producing epoch; the sender PARKS
  items with epoch >= barrier before on_sentence (still unfinished, so
  _drain/join accounting stays truthful). release(upto) lifts ONLY its own
  barrier (a stale release of an older epoch is a no-op — it can never leak
  a newer generation's unconfirmed audio). invalidate(upto) — SYNC, zero
  awaits — drops parked/queued chunks with epoch <= upto ONLY and clears
  the buffer only when no newer generation has spawned (_floor). hold(k)
  sweeps EVERYTHING older synchronously (buffer/parked/queue all predate
  gen k by construction) — a stale draft can never leak into a newer
  generation's release, and two generations can never mix text in one
  buffer. advance(k) bumps the speakable floor in the spawner's sync zone;
  add() rejects tokens below it (dying streams' stale tokens).
  reset() survives as CALL TEARDOWN ONLY.
"""
from __future__ import annotations

import asyncio
import re
from typing import Awaitable, Callable

OnSentence = Callable[[str, bool], Awaitable[None]]   # (text, continue_)

_SENTENCE_END = re.compile(r"[.!?](\s|$)")
_FLUSH_LEN = 140          # fallback only — direct constructions (tests) keep
                          # the legacy default; production value =
                          # Settings.gate_flush_len (SWITCH LAW, iter103 PT-4)
_MIN_LEN = 25
_IDLE_FLUSH_MS = 150


class SentenceGate:
    def __init__(self, on_sentence: OnSentence, managed: bool = False,
                 flush_len: int | None = None,
                 chop_on_question: bool = False):
        self.on_sentence = on_sentence
        self.managed = managed
        self._flush_len = flush_len if flush_len and flush_len > 0 else _FLUSH_LEN
        # iter105 T1: ask-hold chop — off by default (byte-exact for direct
        # constructions); the session passes Settings.ask_hold.
        self._chop_on_question = bool(chop_on_question)
        self._buffer = ""
        self._timer_task: asyncio.Task | None = None
        self._closed = False
        self._last_continue_sent = False
        self._queue: asyncio.Queue[tuple[int, str, bool] | None] = asyncio.Queue()
        self._sender_task: asyncio.Task | None = None
        # iter93 T2 (FIND-35 epoch discipline): held-state lives HERE.
        self._barrier: int | None = None          # held epoch (None = open)
        self._parked: list[tuple[int, str, bool]] = []   # parked (unfinished) held items
        self._cur_epoch: int = 0                  # epoch of the most recent add
        self._floor: int = 0                      # min speakable generation
        self._held_final = False
        # iter105 T1 (ask-hold chop, belt to the builder's suspenders): a
        # flushed SPOKEN chunk ending "?" stops this round's stream — every
        # later add() of the round dies here, unbuffered. Round-scoped (NOT
        # epoch-based): cleared by round_reset() on a new round, end_of_turn
        # (turn over), a live-generation invalidate() (barge/replace), and
        # reset()/close() (teardown).
        self._chop_round = False
        self._open_evt = asyncio.Event()

    # ------------------------------------------------------------------ #
    def hold(self, epoch: int) -> None:
        """iter93: stop flushing — buffer tokens until release(upto)/
        invalidate(upto) for THIS epoch. SYNC-effective (no await inside):
        the barrier is set in the caller's sync zone.
        THE SWEEP: everything currently buffered/parked/queued predates gen
        `epoch` by construction (gen-epoch tokens can only arrive AFTER this
        hold arms) — a new generation's hold synchronously invalidates it
        all, so a stale draft can never leak into a newer release."""
        self._barrier = epoch
        if epoch > self._floor:
            self._floor = epoch
        self._cancel_timer()
        self._held_final = False
        self._buffer = ""
        self._last_continue_sent = False
        self._drop_through(epoch - 1)

    def advance(self, epoch: int) -> None:
        """iter93: a fresh generation `epoch` opened — SYNC floor bump in the
        spawner's sync zone. Any add() below the floor afterwards is a dying
        stream's stale token and is rejected: two generations can never mix
        text in one buffer (the cross-gen corruption class)."""
        if epoch > self._floor:
            self._floor = epoch

    def round_reset(self) -> None:
        """iter105 T1: a new ROUND opened — lift the ask-hold chop so the
        round's own stream flows again. SYNC (no awaits)."""
        self._chop_round = False

    async def release(self, upto: int) -> None:
        """iter93: confirmation for epoch `upto` arrived — flush everything
        buffered while held, at normal sentence boundaries. Lifts ONLY the
        barrier of epoch `upto`: a stale/foreign release (older epoch, or
        after a barge already opened the gate) is a no-op — it can never
        leak a newer generation's unconfirmed audio. If the LLM stream
        already parked its final marker, the closing flush (continue=false)
        runs exactly as the normal end_of_turn path would have."""
        if self._barrier != upto:
            return
        self._barrier = None
        self._open_evt.set()
        if self._closed:
            self._held_final = False
            return
        if not self.managed:
            if len(self._buffer) >= _MIN_LEN:
                m = _SENTENCE_END.search(self._buffer)
                if m:
                    self._flush_upto(m.end(), upto)
            if len(self._buffer) >= self._flush_len:
                self._flush_upto(len(self._buffer), upto)
        final = self._held_final
        self._held_final = False
        if final:
            await self.end_of_turn()          # normal final flush + drain
        elif self._buffer.strip():
            self._schedule_idle_flush()

    def invalidate(self, upto: int) -> None:
        """iter93: everything of generations <= `upto` is dead — drop it,
        wherever it sits (buffer, parked shelf, queue). SYNC-effective (NO
        await inside — law 1: atomic w.r.t. every handler and the sender).
        Epoch-tagged: a stale invalidate (a NEWER generation already
        holding/writing) is harmless by construction:
          - lifts the barrier ONLY if it is <= upto (never a newer hold);
            on lift, the buffer is gen-upto's by construction (no newer gen
            exists while OUR barrier was armed) → cleared;
          - parked/queued chunks with epoch <= upto dropped ONLY (task_done
            accounting so a concurrent _drain never hangs);
          - a newer generation's text buffer is never touched (its hold's
            sweep already removed anything of ours).
        Sender task and its in-flight on_sentence are left alone."""
        self._open_evt.set()                 # wake the sender; predicate re-checks
        if self._barrier is not None and self._barrier <= upto:
            self._barrier = None
            self._held_final = False
            self._cancel_timer()
            self._buffer = ""
            self._last_continue_sent = False
            self._chop_round = False          # iter105 T1: dying generation's chop dies with it
        elif upto >= self._floor:
            # barrier open or foreign: clear only if no newer generation has
            # spawned (floor advanced SYNC in the spawner — no window)
            self._cancel_timer()
            self._buffer = ""
            self._last_continue_sent = False
            self._chop_round = False          # iter105 T1: same — a barge/replace opens fresh
        self._drop_through(upto)

    async def discard(self, upto: int) -> None:
        """Async alias of invalidate (API compat for tests)."""
        self.invalidate(upto)

    def _drop_through(self, upto: int) -> None:
        """Drop parked + queued items with epoch <= upto (accounting kept:
        one task_done per dropped item — join() never hangs on the dead)."""
        kept: list[tuple[int, str, bool]] = []
        while self._parked:
            item = self._parked.pop()
            if item[0] <= upto:
                self._queue.task_done()
            else:
                kept.append(item)
        kept.reverse()
        self._parked = kept
        requeue: list[tuple[int, str, bool] | None] = []
        while not self._queue.empty():
            item = self._queue.get_nowait()
            if item is None or item[0] > upto:
                requeue.append(item)
            else:
                self._queue.task_done()
        for item in requeue:
            self._queue.put_nowait(item)

    # ------------------------------------------------------------------ #
    def add(self, token: str, epoch: int = 0) -> None:
        """Called for every LLM content delta (sync — cheap, no awaits).
        epoch = the producing generation (iter93): while the barrier is
        armed, tokens buffer only; queued items carry this epoch so the
        sender can park/discard them by valence. Tokens below the floor are
        a dying stream's stale tail — rejected. iter105 T1: after a flushed
        chunk ended "?" the round is chopped — later tokens die here."""
        if self._closed or not token:
            return
        if self._chop_round:                  # iter105 T1: tail dies unbuffered
            return
        if epoch < self._floor:
            return
        if epoch > self._floor:
            self._floor = epoch
        self._cur_epoch = epoch
        self._ensure_sender()
        self._buffer += token
        if self._barrier is not None:         # held: buffering only, no flush
            return
        if not self.managed:
            if len(self._buffer) >= _MIN_LEN:
                m = _SENTENCE_END.search(self._buffer)
                if m:
                    self._flush_upto(m.end(), epoch)
            if len(self._buffer) >= self._flush_len:
                self._flush_upto(len(self._buffer), epoch)
        self._schedule_idle_flush()

    async def end_of_turn(self) -> None:
        """Turn finished: mark the context done; drains all queued chunks."""
        self._cancel_timer()
        self._chop_round = False               # iter105 T1: turn over — next turn unchopped
        if self._closed:
            return
        if self._barrier is not None:
            # iter93: stream ended while audio is held — park the final
            # flush; release(upto) performs it when confirmation arrives.
            self._held_final = True
            return
        text = self._buffer.strip()
        self._buffer = ""
        if text:
            self._put(text, False, self._cur_epoch)
        elif self._last_continue_sent:
            # closing marker: empty transcript + continue=false (Cartesia docs)
            self._put("", False, self._cur_epoch)
        self._last_continue_sent = False
        await self._drain()

    async def reset(self) -> None:
        """iter93: CALL TEARDOWN ONLY (hard path). Turn-level invalidation
        uses the epoch-filtered invalidate(upto) — reset() drops EVERYTHING
        unspoken (buffer + parked + queued) and is reserved for teardown."""
        self._cancel_timer()
        self._barrier = None
        self._held_final = False
        self._buffer = ""
        self._last_continue_sent = False
        self._chop_round = False
        self._parked = []                    # parked items were unfinished; the queue swap drops the count
        self._open_evt.set()
        self._drop_queue()

    async def close(self) -> None:
        self._closed = True
        self._cancel_timer()
        self._barrier = None
        self._chop_round = False
        self._parked = []
        self._open_evt.set()
        self._drop_queue()

    # ------------------------------------------------------------------ #
    def _ensure_sender(self):
        if self._sender_task is None or self._sender_task.done():
            self._sender_task = asyncio.create_task(self._sender())

    def _put(self, text: str, continue_: bool, epoch: int = 0):
        self._queue.put_nowait((epoch, text, continue_))

    async def _drain(self):
        """Wait until every queued (and parked) chunk was sent or discarded."""
        if self._sender_task is None:
            return
        await self._queue.join()

    async def _sender(self):
        try:
            while True:
                # serve a parked item once its epoch is no longer held
                # (predicate re-checked every wake — a re-armed newer barrier
                # can never unpark a stale item, and a confirmed item parked
                # under an old barrier flows even if a newer hold re-armed)
                if self._parked and (self._barrier is None
                                     or self._parked[0][0] < self._barrier):
                    item = self._parked.pop(0)     # parked items flow first (order kept)
                else:
                    item = await self._queue.get()
                    if item is None:
                        return
                epoch, text, continue_ = item
                if self._barrier is not None and epoch >= self._barrier:
                    # iter93: park BEFORE on_sentence; the item stays
                    # unfinished (task_done only on real send / discard)
                    # so _drain accounting stays truthful.
                    self._parked.append(item)
                    while (self._barrier is not None and self._parked
                           and self._parked[0][0] >= self._barrier):
                        self._open_evt.clear()
                        await self._open_evt.wait()
                    continue
                try:
                    await self.on_sentence(text, continue_)
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            raise

    def _drop_queue(self):
        if self._sender_task:
            self._sender_task.cancel()
            self._sender_task = None
        # fresh queue: anything not yet sent is discarded
        self._queue = asyncio.Queue()
        self._last_continue_sent = False

    def _flush_upto(self, idx: int, epoch: int = 0) -> None:
        text = self._buffer[:idx].strip()
        self._buffer = self._buffer[idx:]
        if text:
            # iter105 T1: a flushed SPOKEN chunk ending "?" chops the rest of
            # the round — the tail after a question never voices.
            if self._chop_on_question and text.endswith("?"):
                self._chop_round = True
            self._last_continue_sent = True
            self._put(text, True, epoch)

    def _schedule_idle_flush(self) -> None:
        self._cancel_timer()
        if self._closed:
            return

        async def _idle():
            await asyncio.sleep(_IDLE_FLUSH_MS / 1000)
            if self._buffer.strip() and self._barrier is None:
                self._flush_upto(len(self._buffer), self._cur_epoch)

        self._timer_task = asyncio.create_task(_idle())

    def _cancel_timer(self) -> None:
        if self._timer_task:
            self._timer_task.cancel()
            self._timer_task = None
