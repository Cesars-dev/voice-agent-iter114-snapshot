"""iter28 server-time-payload contracts.

The 2026-09-05 smoke shipped a silent ConfirmSlots drift (embedded state_prompt
in llm.json vs diallux/prompts/ConfirmSlots.md). These tests pin the iter28
human-time contract across engine, prompts, and mock so it cannot drift again.
"""
import json
import re
from pathlib import Path

from tests.mock_webhooks import _contained, _human_pick, canonical

ROOT = Path(__file__).resolve().parents[1]
LLM = json.loads((ROOT / "agent" / "llm.json").read_text(encoding="utf-8"))


def _state(name):
    return next(s for s in LLM["states"] if s["name"] == name)


def _tool(state_name, tool_name):
    return next(t for t in _state(name=state_name)["tools"] if t["name"] == tool_name)


# --------------------------------------------------------------------------- #
def test_confirmslots_state_prompt_no_drift():
    """Embedded ConfirmSlots state_prompt must EQUAL the live prompt file —
    builder.py:159 falls back to the embedded copy when the file is absent."""
    embedded = _state("ConfirmSlots")["state_prompt"]
    ondisk = (ROOT / "diallux" / "prompts" / "ConfirmSlots.md").read_text(encoding="utf-8")
    assert embedded == ondisk


def test_selected_time_stays_in_payloads_and_stays_human():
    """Prime directive: selected_time remains in validate_lead AND
    verify_lead_data, gated against slot_options — never ISO-coerced."""
    for state_name, tool_name in [("ConfirmSlots", "validate_lead"),
                                  ("VerifyLead", "verify_lead_data")]:
        props = _tool(state_name, tool_name)["parameters"]["properties"]
        assert "selected_time" in props, (state_name, tool_name)
        assert "slot_options" in props, (state_name, tool_name)
        assert "{{requested_slot}}" in props["slot_options"]["description"]
        assert "offered options" in props["slot_options"]["description"]
        assert "gates it against the offered options" in props["selected_time"]["description"]


def test_extract_confirm_details_plain_english_contract():
    var = next(v for v in _tool("ConfirmSlots", "extract_confirm_details")["variables"]
               if v.get("name") == "selected_time")
    assert "verbatim" in var["description"]
    assert "Never convert, never compute" in var["description"]


def test_create_livecall_booking_booked_human_and_time_desc():
    tool = _tool("Booking", "create_livecall_booking")
    assert tool["response_variables"].get("booked_human") == "booked_human"
    assert "the server resolves it against the held slots" in \
        tool["parameters"]["properties"]["time"]["description"]
    assert "never a UTC iso" not in json.dumps(LLM)


def test_prompt_contract_markers():
    booking = (ROOT / "diallux" / "prompts" / "Booking.md").read_text(encoding="utf-8")
    assert "{{booked_human}}" in booking
    assert "[DAY, DATE] at [TIME]" not in booking
    assert "never a UTC" not in booking
    verifylead = (ROOT / "diallux" / "prompts" / "VerifyLead.md").read_text(encoding="utf-8")
    assert "slot_options" in verifylead and "{{requested_slot}}" in verifylead
    confirm = (ROOT / "diallux" / "prompts" / "ConfirmSlots.md").read_text(encoding="utf-8")
    assert "the server resolves and freezes it" in confirm


# --------------------------------------------------------------------------- #
# canonicalizer / containment gate (mirror of v1 + validator — PM rule pinned)
# --------------------------------------------------------------------------- #
def test_canonical_bare_hour_pm_rule():
    """Generator only offers bare hours for afternoon/evening — bare <=6 is PM."""
    assert canonical("tomorrow at 5") == "tomorrow 5:00 pm"
    assert canonical("tomorrow, 5 pm") == "tomorrow 5:00 pm"
    assert canonical("today at 11") == "today 11:00 am"


def test_canonical_variant_collapse():
    for variant in ["tomorrow at noon", "tomorrow, 12 pm", "tomorrow around noon",
                    "tomorrow, 9/8 at noon"]:
        assert canonical(variant) == "tomorrow 12:00 pm", variant


def test_containment_gate():
    opts = "tomorrow, 9/8 at noon or 5:30 pm"
    for pick in ["tomorrow at noon", "noon", "12 pm", "5:30 pm", "tomorrow, 9/8 at noon"]:
        assert _contained(pick, opts), pick
    for pick in ["tomorrow at 6:30 pm", "friday at noon", "midnight", "9 pm"]:
        assert not _contained(pick, opts), pick
    short = "today, 9/4 at 1 pm or 2:30 pm"
    assert _contained("today at 2:30 pm", short)   # same-day short form, day-strip
    assert not _contained("friday at 2:30 pm", short)  # unoffered day never strips


def test_mock_booked_human_present_and_natural():
    assert _human_pick("2026-09-04T14:30:00") == "today at 2:30 pm"
    assert _human_pick("2026-09-04T12:00:00") == "today at noon"
    assert _human_pick("2026-09-05T17:30:00") == "tomorrow at 5:30 pm"
    assert _human_pick("garbage") == ""
    assert not re.search(r"\d{4}-\d{2}-\d{2}T", _human_pick("2026-09-04T14:30:00"))
