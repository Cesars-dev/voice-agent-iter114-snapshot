"""Hermetic units for the LLM-to-LLM harness (no keys, no network).

Validates: persona battery integrity (schema, dynvars keys, run-order rules)
and the scorer (booking evidence — the SOP "non-empty, never fixed
timestamp" rule). The full loop is exercised hermetically by:
    python tests/llm2llm/harness.py --offline
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.llm2llm.harness import (            # noqa: E402
    BOOKING_TOOL,
    booking_evidence,
    retell_booking_evidence,
    score_outcome,
)
from tests.llm2llm.personas import PERSONAS, persona_groups, select  # noqa: E402

LLM_JSON = json.loads((ROOT / "agent" / "llm.json").read_text())
DVS_KEYS = set((LLM_JSON.get("default_dynamic_variables") or {}).keys())


# ------------------------------------------------------------------ personas
def test_battery_schema():
    for p in PERSONAS:
        assert {"name", "type", "system", "opener", "expect"} <= set(p), p["name"]
        assert p["expect"] in ("book", "no-book"), p["name"]
        assert p["system"].strip() and p["opener"].strip(), p["name"]


def test_dynvars_are_real_dynamic_variables():
    """Seeded dynvars must exist in the agent's default_dynamic_variables —
    otherwise initial_state silently drops them and the persona lies."""
    for p in PERSONAS:
        for k in p.get("dynvars", {}):
            assert k in DVS_KEYS, f"{p['name']}: dynvar {k!r} not in llm.json dvs"


def test_happy_path_present_and_first():
    groups = persona_groups()
    assert groups["happy"], "SOP: at least one happy path (smoke test) is REQUIRED"
    assert all(p["type"] == "happy-path" for p in PERSONAS[:len(groups["happy"])]), \
        "SOP run order: happy paths must come first in the battery"


def test_groups_cover_everything():
    covered = {p["name"] for g in ("happy", "stress", "curve", "gatekeepers", "breakers")
               for p in persona_groups()[g]}
    assert covered == {p["name"] for p in PERSONAS}


def test_select_substring_and_error():
    assert len(select("Maria")) == 1
    assert select("happy") == persona_groups()["happy"]
    try:
        select("does-not-exist")
    except SystemExit:
        pass
    else:
        raise AssertionError("select() should SystemExit on no match")


# ------------------------------------------------------------------ scoring
def _trace(resp=None, tool=BOOKING_TOOL):
    return [{"tool": tool, "args": {}, "resp": resp or {}}]


def test_booked_via_tool_response_nonempty_uid():
    ev = booking_evidence(
        _trace({"ok": True, "status": "booked", "booking_uid": "qeTqHuZ1EDzH8bxEdhPQ6H"}),
        {})
    assert ev["booked"] and ev["uid"] == "qeTqHuZ1EDzH8bxEdhPQ6H"


def test_not_booked_when_uid_empty():
    """SOP: assert by NON-EMPTY time/uid — an ok-but-empty booking is not a book."""
    ev = booking_evidence(_trace({"ok": True, "status": "booked", "booking_uid": ""}), {})
    assert not ev["booked"] and ev["fired"]


def test_reschedule_and_cancel_do_not_count():
    for status in ("reschedule_options", "released"):
        ev = booking_evidence(_trace({"ok": True, "status": status, "booking_uid": "x"}), {})
        assert not ev["booked"], status


def test_failed_booking_not_counted():
    ev = booking_evidence(_trace({"ok": False, "error": "invalid_number"}), {})
    assert not ev["booked"] and ev["fired"]


def test_booked_via_dvs_server_confirmation():
    ev = booking_evidence([], {"booking_uid": "abc", "booking_verified": True})
    assert ev["booked"] and ev["status"] == "dvs_confirmed"


def test_score_outcome_expect_matching():
    book = score_outcome({"booked": True}, True, "book")
    assert book["pass"] and book["outcome"] == "book"
    no = score_outcome({"booked": False}, False, "no-book")
    assert no["pass"] and no["outcome"] == "no-book"
    bad = score_outcome({"booked": False}, True, "book")
    assert not bad["pass"]


def test_retell_evidence_needs_nonempty_slot():
    ev = retell_booking_evidence([BOOKING_TOOL], [{"time": "2026-09-04T13:00:00"}])
    assert ev["booked"] and ev["uid"] == "2026-09-04T13:00:00"
    ev2 = retell_booking_evidence([BOOKING_TOOL], [{"time": ""}])
    assert not ev2["booked"] and ev2["status"] == "no_slot"
    ev3 = retell_booking_evidence(["end_call"], [])
    assert not ev3["fired"]


def test_booking_tool_name_matches_agent():
    """The scorer must watch the tool the deployed agent actually books with."""
    names = {t["name"] for s in LLM_JSON["states"] for t in s.get("tools", [])}
    assert BOOKING_TOOL in names
