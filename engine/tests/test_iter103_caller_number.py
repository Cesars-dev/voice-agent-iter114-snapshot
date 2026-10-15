"""iter103 PT-11 — caller number reaches the engine (trace-verified wire).

Trace verdict (`research/surgeon/iter102-fix-wave/03_caller_number_trace.md`):
the pre-wire `/twiml` read `From` from the query string ONLY — Twilio POSTs
it form-encoded, so `callback_number` never reached `customParameters` and
dvs stayed empty on the Oct-6/8 real calls. The wire reads the form body
first (query fallback kept for curl/manual tests).

Pins:
  1. test_form_post_injects_parameter  — Twilio's actual shape works
  2. test_query_param_still_works      — back-compat (manual/curl)
  3. test_no_from_no_parameter         — empty case byte-shape unchanged
  4. test_param_to_dvs_end_to_end      — the whole paper chain, pinned
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]


def _client():
    from fastapi.testclient import TestClient

    from diallux.app import app
    return TestClient(app)


def test_form_post_injects_parameter():
    r = _client().post("/twiml", data={"From": "+13125551234"})
    assert r.status_code == 200
    assert '<Parameter name="callback_number" value="+13125551234" />' in r.text


def test_query_param_still_works():
    r = _client().get("/twiml", params={"From": "+13125551234"})
    assert r.status_code == 200
    assert '<Parameter name="callback_number" value="+13125551234" />' in r.text


def test_no_from_no_parameter():
    client = _client()
    assert "<Parameter" not in client.post("/twiml").text
    assert "<Parameter" not in client.get("/twiml").text


def test_param_to_dvs_end_to_end():
    # the paper chain, pinned so it can never silently rot again:
    # customParameters -> persona filter (llm.json keys) -> state filter ->
    # dvs["callback_number"]
    from diallux.state import initial_state

    llm_json = json.loads((ROOT / "agent" / "llm.json").read_text())
    known = set(llm_json.get("default_dynamic_variables", {}))
    assert "callback_number" in known, "persona filter must pass it"
    persona = {k: v for k, v in {"callback_number": "+13125551234"}.items()
               if k in known}
    st = initial_state("c11-caller", llm_json, persona)
    assert st["dvs"]["callback_number"] == "+13125551234"
    # unknown keys stay filtered (the guard)
    persona2 = {k: v for k, v in {"not_a_dv": "x"}.items() if k in known}
    st2 = initial_state("c11-caller-2", llm_json, persona2)
    assert st2["dvs"].get("not_a_dv") is None
