"""iter43 — prompt-shape pins (Offer consent ask PROMPT-FIRST + contact_details
softening) + iter40 no-drift byte lockstep.

Pins:
  a. Offer.md carries the consent ask ("walk you through") and NO date binary
     ("later today or tomorrow" is dead — scheduling belongs to ConfirmSlots)
  b. contact_details.md carries the demo lead-in + the attendee-name fallback
     rule (never re-ask a declined name)
  c. llm.json state_prompt for Offer + contact_details is BYTE-IDENTICAL to
     their .md files (iter40 lockstep)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
OFFER = (ROOT / "diallux" / "prompts" / "Offer.md").read_text()
CONTACT = (ROOT / "diallux" / "prompts" / "contact_details.md").read_text()


def test_offer_has_consent_ask_and_no_date_binary():
    assert "walk you through" in OFFER
    # the ask lives in the prompt as spoken language, variable-friendly
    assert "Would you like me to walk you through" in OFFER
    # date binary DELETED everywhere in this state (Mission line was already
    # "No dates or times here" — step 1 contradicting it was the iter21 bug)
    assert "later today or tomorrow" not in OFFER
    assert "tomorrow?" not in OFFER
    # capture rule: extract only AFTER the explicit yes, never on the ask round
    assert "ONLY after the caller explicitly said yes" in OFFER


def test_contact_details_has_lead_in_and_attendee_name_rule():
    assert "get you set up for the live demo" in CONTACT
    assert "attendee name" in CONTACT
    assert "Never re-ask for a name, never loop the read-back" in CONTACT


def test_llm_json_lockstep_byte_identical():
    # iter89 Edit A (PT-76): full 9-state lockstep — every state_prompt must
    # byte-match its .md runtime source of truth.
    d = json.loads((ROOT / "agent" / "llm.json").read_text())
    for name in ("Intake", "Discovery", "Closer", "Offer",
                 "contact_details", "ConfirmSlots", "VerifyLead",
                 "Booking", "Closing"):
        md = (ROOT / "diallux" / "prompts" / f"{name}.md").read_text()
        state = [s for s in d["states"] if s["name"] == name][0]
        assert state["state_prompt"] == md, f"{name} lockstep drift"
