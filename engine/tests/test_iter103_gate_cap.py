"""iter103 PT-4 — gate cap 500 pins ("a cap, not a rule", FIND-38).

SentenceGate never trims spoken sentences mid-phrase: it already flushes at
natural sentence boundaries (MIN_LEN 25 + `[.!?](\s|$)`) and on the 150 ms
idle timer. PT-4 promotes the hard cap `_FLUSH_LEN` 140 -> 500 and makes it
a Settings field (`GATE_FLUSH_LEN`, code default 500 = the tested value;
the 140 constant stays the no-arg fallback). The Oct-8 disease: 11 of 23
turns cut at exactly char 140 mid-clause.

Pins:
  1. test_233char_single_sentence_no_mid_clause_cut — the T20 shape, cap 500
  2. test_flag_140_byte_parity                    — 140 still cuts at char 140
  3. test_runaway_valve_still_fires               — 600 no-punct -> 500 + rest
  4. test_env_override                            — GATE_FLUSH_LEN reaches Settings
  5. test_no_arg_ctor_fallback                    — SentenceGate(cb) -> 140
  6. test_release_path_respects_cap               — held release under the cap
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from diallux.media.sentence_gate import SentenceGate, _FLUSH_LEN

_BASE = ("Marcus this is exactly why those after hours calls matter because "
         "every single one of them is a job you already paid to generate "
         "and the math on your missed revenue is bigger than you think")
TEXT_233 = (_BASE + _BASE)[:232] + "."
assert len(TEXT_233) == 233
assert "." not in TEXT_233[:140], "the 140 window must contain no sentence end"


def _run(coro):
    return asyncio.run(coro)


def _mk(flush_len=None):
    chunks = []

    async def cb(text, cont):
        chunks.append((text, cont))

    kwargs = {} if flush_len is None else {"flush_len": flush_len}
    return SentenceGate(cb, **kwargs), chunks


def test_233char_single_sentence_no_mid_clause_cut():
    async def go():
        gate, chunks = _mk(flush_len=500)
        for ch in TEXT_233:
            gate.add(ch)
        await gate.end_of_turn()
        spoken = [c for c in chunks if c[0]]
        assert spoken == [(TEXT_233, True)], \
            f"must flush WHOLE once at its end, got {[len(t) for t, _ in spoken]}"
        await gate.close()

    _run(go())


def test_flag_140_byte_parity():
    async def go():
        gate, chunks = _mk(flush_len=140)
        for ch in TEXT_233:
            gate.add(ch)
        await gate.end_of_turn()
        spoken = [c for c in chunks if c[0]]
        # snapshot parity: the 140-cut still lands at exactly char 140
        assert spoken[0][0] == TEXT_233[:140].strip()
        assert spoken[1][0] == TEXT_233[140:].strip()
        await gate.close()

    _run(go())


def test_runaway_valve_still_fires():
    async def go():
        gate, chunks = _mk(flush_len=500)
        for _ in range(600):
            gate.add("a")
        await gate.end_of_turn()
        spoken = [c for c in chunks if c[0]]
        assert spoken[0] == ("a" * 500, True), "one valve flush at 500"
        assert spoken[1][0] == "a" * 100 and spoken[1][1] is False
        await gate.close()

    _run(go())


def test_env_override(monkeypatch):
    monkeypatch.setenv("GATE_FLUSH_LEN", "200")
    s = Settings(openai_api_key="test", retell_api_key="test",
                 langfuse_enabled=False)
    assert s.gate_flush_len == 200, "SWITCH LAW proof: env reaches Settings"


def test_no_arg_ctor_fallback():
    gate = SentenceGate(lambda t, c: asyncio.sleep(0))
    assert gate._flush_len == _FLUSH_LEN == 140, \
        "direct constructions keep the legacy default"


def test_release_path_respects_cap():
    async def go():
        gate, chunks = _mk(flush_len=500)
        gate.hold(1)
        for _ in range(233):
            gate.add("a", epoch=1)
        await gate.release(1)
        await asyncio.sleep(0)                   # let the sender settle
        assert [c for c in chunks if c[0]] == [], \
            "under-cap held buffer waits for idle/end — no mid-clause cut"
        await gate.end_of_turn()
        spoken = [c for c in chunks if c[0]]
        assert spoken == [("a" * 233, False)], \
            f"whole held sentence voices at end, got {[(len(t), c) for t, c in spoken]}"
        await gate.close()

        # legacy cap: 140 dumps the same held buffer whole (pre-PT-4 path)
        gate2, chunks2 = _mk(flush_len=140)
        gate2.hold(1)
        for _ in range(233):
            gate2.add("a", epoch=1)
        await gate2.release(1)
        await asyncio.sleep(0)
        assert [c for c in chunks2 if c[0]] == [("a" * 233, True)]
        await gate2.close()

    _run(go())
