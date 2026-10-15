"""iter107 (PT-10) — VerifyLead dv write-back pins.

The wedge class (found 2026-10-11): values recovered at the VerifyLead gate
(prospect_timezone / selected_time / company_name) are validated by the
server but were never persisted — `verify_lead_data.response_variables`
mapped only data_verified/callback_number/first_name/last_name, so the
Phase-B chain gate could stay blocked after a green gate.

Pins:
  a. response_variables carries the three added mappings (schema level)
  b. the shared applier (_patch_from_body) persists the ok-payload
     `**injected` values for those keys (behavior level)
  c. an `incomplete` payload (no injected keys) writes NOTHING (no blank
     writes)
  d. the un-capped "KEEP CALLING" imperative is gone from BOTH mirrors
     (llm.json state_prompt + prompts/VerifyLead.md)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]


def _verify_tool() -> dict:
    d = json.loads((ROOT / "agent" / "llm.json").read_text())
    for s in d["states"]:
        if s["name"] == "VerifyLead":
            for t in s["tools"]:
                if t["name"] == "verify_lead_data":
                    return t
    raise AssertionError("verify_lead_data not found in llm.json")


def test_iter107_response_variables_map_complete():
    rv = _verify_tool().get("response_variables") or {}
    for key in ("company_name", "prospect_timezone", "selected_time"):
        assert rv.get(key) == key, f"missing write-back mapping: {key}"


def test_iter107_ok_payload_persists_injected_values():
    from diallux.graph.tools import ToolExecutor

    t = _verify_tool()
    body = {
        "status": "ok",
        "data_verified": True,
        "problems": [],
        "actions": [],
        "first_name": "Maria",
        "last_name": "Gonzales",
        "company_name": "Bright Smile Dental",
        "callback_number": "+13125551234",
        "prospect_timezone": "America/Chicago",
        "selected_time": "2026-10-12T13:00:00-05:00",
    }
    patch = ToolExecutor._patch_from_body(object(), t, body)
    for key in ("company_name", "prospect_timezone", "selected_time",
                "first_name", "last_name", "callback_number", "data_verified"):
        assert patch.get(key) == body[key], f"not persisted: {key}"


def test_iter107_incomplete_payload_writes_nothing():
    from diallux.graph.tools import ToolExecutor

    t = _verify_tool()
    body = {
        "status": "incomplete",
        "data_verified": False,
        "problems": [{"field": "callback_number", "issue": "missing"}],
        "actions": ["Say: 'What's the best number to reach you?'"],
    }
    patch = ToolExecutor._patch_from_body(object(), t, body)
    # no phantom field writes and no blank values (data_verified=False is the
    # server verdict and is allowed to ride — the field keys must not)
    for key in ("company_name", "prospect_timezone", "selected_time",
                "first_name", "last_name", "callback_number"):
        assert key not in patch, f"incomplete payload wrote {key}: {patch}"
    assert all(v not in (None, "") for v in patch.values()), patch


def test_iter107_prompt_keep_calling_removed():
    d = json.loads((ROOT / "agent" / "llm.json").read_text())
    state = [s for s in d["states"] if s["name"] == "VerifyLead"][0]
    assert "KEEP CALLING" not in state["state_prompt"]
    md = (ROOT / "diallux" / "prompts" / "VerifyLead.md").read_text()
    assert "KEEP CALLING" not in md
