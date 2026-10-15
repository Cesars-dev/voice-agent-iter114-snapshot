"""Typed dynamic variables (V2 production iteration #1).

The Retell artifact stores 41 dynamic variables as a flat stringly dict — the
root cause of the null-dv bug class you built a whole verification tool for.
In V2 every variable is a typed field on a Pydantic model:

  - "" / None defaults instead of Retell's JSON null clobbering
  - booleans are real booleans (server-written gates can't be "null")
  - int fields coerce digit-strings ("20" -> 20) the way the leak tool needs
  - to_flat() feeds the verbatim {{dv}} substitution, so prompts don't change

SERVER_OWNED_DVS: keys that only webhook responses (or deterministic nodes)
may write. If the LLM tries to fill one via an extract tool, the write is
rejected — the model can no longer fabricate progress (slot_verified,
data_verified, phone_confirmed, booking_verified ...). This is what lets the
graph route on gate booleans safely.
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

# Keys written ONLY by the server side (webhook response_variables /
# deterministic nodes). Extract-tool writes to these are dropped.
SERVER_OWNED_DVS: frozenset[str] = frozenset({
    "slot_verified", "data_verified", "phone_confirmed", "booking_verified",
    "booking_failed", "booking_uid", "weekly_leak", "monthly_leak",
    "today_date", "slot_reservation_uids", "requested_slot",
})


_BIZ_NAME_RE = re.compile(
    r"\b(llc|inc|ltd|llp|co|corp|association|associates|group|company|services|"
    r"solutions|partners|partnership|legal|plumbing|roofing|hvac|heating|cooling|"
    r"dental|dentistry|law|firm|brothers|sons|homes|builders|construction|"
    r"remodeling|electric|electrical)\b|&", re.IGNORECASE)


class DynamicVariables(BaseModel):
    """The 41 dvs, typed. Field names == Retell dynamic variable names."""

    # identity/contact
    first_name: str = ""
    last_name: str = ""
    company_name: str = ""
    industry: str = ""
    callback_number: str = ""
    prospect_timezone: str = ""
    is_calling_best_number: bool | None = None   # None = question not asked yet (non-null gate); false is a valid answer
    # discovery
    pain_points: str = ""
    pain_frame: str = ""
    pain_urgency: str = ""
    call_volume: str = ""
    interest_topic: str = ""
    inbound_channel: str = ""
    interest_signal: bool = False
    pattern_matched: bool = False
    # gate flags (completion booleans are model-written, verification booleans server-owned)
    intake_completed: bool = False
    discovery_completed: bool = False
    closer_completed: bool = False
    offer_completed: bool = False
    contact_details_completed: bool = False
    phone_confirmed: bool = False        # server-owned (record-reach-details / set-callback-number)
    slot_verified: bool = False          # server-owned (validate_lead)
    data_verified: bool = False          # server-owned (verify-lead-data)
    booking_completed: bool = False
    # slots
    selected_time: str = ""
    requested_slot: str = ""             # server-owned (query_livecall_slots)
    slot_reservation_uids: str = ""      # server-owned
    today_date: str = ""                 # server-owned (check_current_date)
    # economics
    missed_calls_weekly: int | str = ""
    missed_calls_daily: int | str = ""   # iter65 T3: model-written; leak math resolves daily×5
    close_rate_pct: int | str = ""
    avg_job_value: int | str = ""
    weekly_leak: str = ""                # server-owned (leak math)
    monthly_leak: str = ""               # server-owned
    # booking
    booking_uid: str = ""                # server-owned (record_booking_uid)
    booking_verified: bool = False       # server-owned
    booking_failed: bool = False         # server-owned policy node
    booking_intent: str = ""
    # misc
    objection_type: str = ""
    interest_level: str = ""
    livecall_agreed: bool = False
    booking_confirmed: bool = False
    chain_done: bool = False            # iter33: engine chain ran (engine-write only; not in any extract tool)
    chain_aborted_step: str = ""        # iter33: abort surfacing (engine-write, tail-rendered)
    chain_aborted_msg: str = ""         # iter33: abort response summary (tail-rendered)

    @field_validator("missed_calls_weekly", "missed_calls_daily", "close_rate_pct", "avg_job_value", mode="before")
    @classmethod
    def _coerce_numeric(cls, v: Any) -> Any:
        """Digit strings become ints ('about 20' stays a string; the leak node
        parses it with the same firstNum() JS the Retell code tool used)."""
        if isinstance(v, str):
            s = v.replace(",", "").strip()
            if s.isdigit():
                return int(s)
        return v

    @field_validator("first_name", "last_name", mode="before")
    @classmethod
    def _person_name_only(cls, v: Any) -> Any:
        """ENFORCED (pydantic, not prompt): identity fields hold a PERSON's
        name only. Live-observed gpt-5.x slip: company name extracted into
        first_name ('Bell & Associates Legal'), then persisted by the
        verify-lead-data echo. Null-doctrine: store nothing rather than
        garbage — the contact prompt re-asks on empty, final verification
        re-confirms. Idempotent: '' passes, clean names pass."""
        if v is None:
            return ""
        s = str(v).strip()
        if s and (_BIZ_NAME_RE.search(s) or len(s) > 40):
            return ""
        return s

    # ---- flat dict views (for {{dv}} substitution + Langfuse parity) ---- #
    def to_flat(self) -> dict[str, Any]:
        return self.model_dump()

    @classmethod
    def from_flat(cls, flat: dict | None) -> "DynamicVariables":
        clean = {k: v for k, v in (flat or {}).items() if k in cls.model_fields}
        try:
            return cls(**clean)
        except Exception:
            # unknown/invalid keys are dropped, never crash a live call
            safe = {}
            for k, v in clean.items():
                try:
                    cls(**{k: v})
                    safe[k] = v
                except Exception:
                    continue
            return cls(**safe)
