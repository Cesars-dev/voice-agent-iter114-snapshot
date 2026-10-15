"""iter61 pins — prewarm socket staleness fixes (AUD-11/12/13).

The 2026-09-21 live mic test (call 351691ef421a, autopsy
research/surgeon/iter59-kb-everywhere/06_autopsy_mic_live_test_20260921.md)
proved three blockers:
  AUD-11  prewarm sent a v1-listen KeepAlive frame on the v2 flux socket —
          Deepgram queued an UNPARSABLE error that killed the first adoption.
  AUD-12  the idle TTS pool socket had no liveness — Cartesia idle-timed-out
          it server-side after ~20 min; the next call adopted a corpse.
  AUD-13  _recv_loop AWAITS on_disconnect() from inside the recv task; the
          hook's close() cancels the very task executing the reconnect
          (CancelledError is not an Exception) — silent permanent deafness.

Hermetic: fake websockets only, no network.
"""
from __future__ import annotations

import asyncio
import inspect
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.media import prewarm
from diallux.media.deepgram_stt import DeepgramSTT
from tests.test_iter49_rag_parity import _t4_settings


# --------------------------------------------------------------------------- #
# AUD-11
# --------------------------------------------------------------------------- #
def test_prewarm_sends_no_v1_keepalive():
    """The v1 poison frame is GONE from the prewarm module (v2 flux has no
    client KeepAlive variant — sending it queues an UNPARSABLE error that
    kills the first adoption)."""
    assert "KeepAlive" not in inspect.getsource(prewarm)


# --------------------------------------------------------------------------- #
# AUD-12
# --------------------------------------------------------------------------- #
class _FakeWS:
    def __init__(self):
        self.closed = False
        self.close_calls = 0

    async def close(self):
        self.closed = True
        self.close_calls += 1


class _SpawnCounter:
    def __init__(self):
        self.calls = 0

    async def __call__(self, settings):
        self.calls += 1
        kind = "stt" if self.name == "stt" else "tts"
        ws = _FakeWS()
        prewarm.pool[kind] = ws
        prewarm._spawn_ts[kind] = time.monotonic()
        return ws

    def __init__(self, name):
        self.name = name
        self.calls = 0


def _setup_pool(monkeypatch, ages: dict):
    """Fake pool per kind with the given spawn ages; spawn helpers counted."""
    stt_spawn = _SpawnCounter("stt")
    tts_spawn = _SpawnCounter("tts")
    monkeypatch.setattr(prewarm, "_spawn_idle_stt", stt_spawn)
    monkeypatch.setattr(prewarm, "_spawn_idle_tts", tts_spawn)
    monkeypatch.setattr(prewarm, "pool", {})
    monkeypatch.setattr(prewarm, "_spawn_ts", {})
    monkeypatch.setattr(prewarm, "_companion_tasks", {})
    old = {}
    for kind, age in ages.items():
        ws = _FakeWS()
        prewarm.pool[kind] = ws
        prewarm._spawn_ts[kind] = time.monotonic() - age
        old[kind] = ws
    return old, stt_spawn, tts_spawn


def test_pool_socket_older_than_ttl_respawned(monkeypatch):
    """A pool socket aged past PREWARM_SOCKET_TTL is closed + respawned by
    _maintain_once; _spawn_ts refreshes (the corpse-adopt fix)."""
    old, stt_spawn, tts_spawn = _setup_pool(
        monkeypatch, {"stt": prewarm.PREWARM_SOCKET_TTL + 999,
                      "tts": prewarm.PREWARM_SOCKET_TTL + 999})
    _run(prewarm._maintain_once(_t4_settings()))
    assert old["stt"].closed and old["tts"].closed
    assert stt_spawn.calls == 1 and tts_spawn.calls == 1
    assert prewarm.pool["stt"] is not old["stt"]
    assert prewarm.pool["tts"] is not old["tts"]
    assert prewarm._spawn_ts["tts"] <= time.monotonic()


def test_fresh_pool_socket_not_respawned(monkeypatch):
    """A fresh pool socket is left alone — no close, no respawn."""
    old, stt_spawn, tts_spawn = _setup_pool(
        monkeypatch, {"stt": 1.0, "tts": 1.0})
    _run(prewarm._maintain_once(_t4_settings()))
    assert not old["stt"].closed and not old["tts"].closed
    assert stt_spawn.calls == 0 and tts_spawn.calls == 0
    assert prewarm.pool["stt"] is old["stt"]
    assert prewarm.pool["tts"] is old["tts"]


def test_get_drops_ttl_tracking(monkeypatch):
    """Adoption pops _spawn_ts — the maintainer must not age-track an
    adopted socket."""
    monkeypatch.setattr(prewarm, "pool", {})
    monkeypatch.setattr(prewarm, "_spawn_ts", {})
    monkeypatch.setattr(prewarm, "_companion_tasks", {})
    prewarm.pool["tts"] = ws = _FakeWS()
    prewarm._spawn_ts["tts"] = time.monotonic()
    got = prewarm.get("tts")
    assert got is ws
    assert "tts" not in prewarm._spawn_ts
    assert "tts" not in prewarm.pool


# --------------------------------------------------------------------------- #
# AUD-13
# --------------------------------------------------------------------------- #
class _ConnClosed(Exception):
    pass


class _RecvOnceWS:
    """recv() raises once (connection drop); send records (audio path)."""

    def __init__(self):
        self.sent: list[bytes] = []
        self.raised = False

    async def recv(self):
        if not self.raised:
            self.raised = True
            raise _ConnClosed("simulated drop")
        await asyncio.sleep(3600)

    async def send(self, data):
        self.sent.append(data)

    async def close(self):
        pass


async def _reconnect_recorder(fired: list):
    fired.append("fired")


def test_recv_loop_death_schedules_reconnect_not_awaits():
    """A recv-loop death fires on_disconnect as a DEFERRED task (the hook
    closes/cancels this recv task — awaiting it self-cancelled the
    reconnect); the recv task itself COMPLETES, not cancelled."""
    fired: list = []
    stt = DeepgramSTT.__new__(DeepgramSTT)
    stt.settings = type("S", (), {"deepgram_mode": "flux"})()
    stt._closed = False
    stt._ws = _RecvOnceWS()
    stt.on_state = None
    stt.on_disconnect = lambda: _reconnect_recorder(fired)

    async def go():
        stt._recv_task = asyncio.create_task(stt._recv_loop())
        await asyncio.wait_for(stt._recv_task, timeout=5.0)
        # let the deferred on_disconnect task run
        await asyncio.sleep(0.05)
        assert stt._recv_task.cancelled() is False
        assert stt._recv_task.exception() is None
        assert fired == ["fired"]

    asyncio.run(go())


def test_close_never_cancels_current_task():
    """close() running FROM the recv task (the reconnect path) must not
    cancel itself — the coroutine completes without CancelledError."""
    stt = DeepgramSTT.__new__(DeepgramSTT)
    stt.settings = type("S", (), {"deepgram_mode": "flux"})()
    stt._closed = False
    stt._ws = None
    stt._keepalive_task = None

    async def go():
        stt._recv_task = asyncio.current_task()
        await stt.close()          # must NOT raise CancelledError
        assert stt._closed is True
        assert stt._recv_task.cancelled() is False

    asyncio.run(go())


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _run(coro):
    return asyncio.run(coro)
