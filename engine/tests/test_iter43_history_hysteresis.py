"""iter43 — hysteretic history window: the cached prefix stays byte-stable.

The old sliding window (history[-n:]) shifts the FRONT of history every turn
once len > n, which invalidates the OpenAI prefix cache at the first history
element every round. The hysteretic trim pins the window START for `step`
entries: len 17..24 (n=16, step=8) all send history[8:] (9..16 entries,
append-only); len 25 jumps the start to 16 — one re-prefill per jump.

Pins:
  a. sawtooth: start 8 for len 17 AND len 24; start 16 at len 25
  b. leading orphan tool response dropped (OpenAI 400 guard, unchanged)
  c. len <= n → full history; window size bounded (never exceeds n)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.graph.builder import CallRuntime as CR


def _hist(n: int) -> list[dict]:
    return [{"role": "user", "content": f"m{i}"} for i in range(n)]


# --------------------------------------------------------------------------- #
def test_sawtooth_start_pinned_at_8_for_len_17_to_24():
    """len 17..24 with n=16, step=8: start pinned at 8 — first element is
    history[8], window grows append-only, no re-prefill."""
    for length in (17, 20, 24):
        history = _hist(length)
        window = CR._history_window(history, 16, 8)
        assert window[0] is history[8], f"len {length}: start must be 8"
        assert len(window) == length - 8


def test_start_jumps_to_16_at_len_25():
    """len 25 crosses the step boundary: start jumps 8 -> 16 (one re-prefill)."""
    history = _hist(25)
    window = CR._history_window(history, 16, 8)
    assert window[0] is history[16]
    assert len(window) == 9


def test_full_history_when_len_le_n():
    """len <= n: everything is sent, unchanged (iter30 behavior preserved)."""
    for length in (0, 1, 15, 16):
        history = _hist(length)
        assert CR._history_window(history, 16, 8) == history


def test_window_never_exceeds_n():
    """Hysteresis bounds the window: len - start <= n for every length."""
    for length in range(17, 40):
        window = CR._history_window(_hist(length), 16, 8)
        assert len(window) <= 16, f"len {length}: window {len(window)} > n"
        assert 0 < len(window) <= length


def test_leading_orphan_tool_response_dropped():
    """The start-jump may land on a tool response — it must be dropped (an
    OpenAI 400: a tool response must follow its assistant tool_calls msg)."""
    history = [{"role": "user", "content": "m0"}]
    # a tool pair at the trim boundary: assistant(tool_calls) + tool response
    history.append({"role": "assistant", "content": "",
                    "tool_calls": [{"id": "c1", "type": "function",
                                    "function": {"name": "t", "arguments": "{}"}}]})
    history.append({"role": "tool", "tool_call_id": "c1", "content": "1"})
    for i in range(1, 30):
        history.append({"role": "user", "content": f"m{i}"})
    window = CR._history_window(history, 16, 8)
    assert window[0].get("role") != "tool"


def test_step_one_restores_sliding_window():
    """Revert path: step=1 == the old iter30 sliding window (history[-n:])."""
    history = _hist(30)
    assert CR._history_window(history, 16, 1) == history[-16:]
