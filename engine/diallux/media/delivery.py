"""Per-state delivery profiles — the state machine controls the mood.

V5 feature. The principle: the LLM writes WHAT to say, the graph decides HOW
it sounds. Delivery is a function of WHERE the call is — one deterministic
dict lookup per turn, no extra LLM call, no extra tokens, zero added latency
(two JSON fields on requests we already send).

  - Cartesia  -> generation_config (speed / volume / emotion), which the
                 adapter sends per request anyway; profile values override
                 the .env globals for that state.
  - ElevenLabs-> voice_settings (stability / style / speed), attached to the
                 first message of the turn's generation (the documented slot).

Semantics (deliberate, so tuning never fights itself):
  - TTS_DELIVERY_PROFILES=false  -> .env globals only (exact V4 behavior).
  - profile field = None         -> fall back to the .env global for that
                                    field (.env stays the base tuning; the
                                    table is per-state DIRECTION on top).
  - state not in the table       -> DEFAULT profile (all None -> pure .env).
  - explicit value               -> the table wins for that state+field.

Tuning lives HERE — editable source of truth, same philosophy as
diallux/prompts/*.md. Values are clamped to the documented ranges:
  Cartesia   speed 0.6-1.5, volume 0.5-2.0 (emotion: English-only beta)
  ElevenLabs stability/style 0-1, speed 0.7-1.2

Why these numbers (sales-cadence rationale):
  - iter43 (2026-09-10, Julio): ALL Cartesia speeds +0.07 over the iter40
    table — owner verdict after the iter42 live calls was "a touch faster
    everywhere" (pace sells, dedupe already guards double-speak).
  - Opener / Intake / Booking brisk (1.12) = energy without rushing; happy
    emotion only where warmth sells (greeting, confirmation).
  - Closing / Closer near-base (1.02) = patience and empathy when the caller
    objects or says goodbye — still faster than the old 0.95 drag.
  - contact_details near-base (0.99, calm) = accuracy while reading back
    phone numbers and codes; clarity beats charisma on data collection.
  - Offer is the one expressive state: fastest spoken state (1.15) + EL style
    up / stability down (0.45 / 0.35) for pitch energy.
  - Emotion is used like salt (Cartesia docs: guidance, not a strict
    adjustment): 5 states total, never stacked, never mid-call jarring.

The profile applies for the state whose PROMPT generated the turn's speech:
CallSession snapshots state_name at the END of each turn, so turn N speaks
with the profile of the state at the START of turn N (i.e. where the LLM was
answering from). Transitions land at the end of the round and take effect
from the next turn — matching Retell's per-state prompt semantics.
"""
from __future__ import annotations

# documented API ranges — clamped so a bad table value can never 400 a call
_CLAMPS: dict[tuple[str, str], tuple[float, float]] = {
    ("cartesia", "speed"): (0.6, 1.5),
    ("cartesia", "volume"): (0.5, 2.0),
    ("elevenlabs", "stability"): (0.0, 1.0),
    ("elevenlabs", "style"): (0.0, 1.0),
    ("elevenlabs", "speed"): (0.7, 1.2),
}

DEFAULT_PROFILE: dict = {
    # None everywhere: fall back to the .env globals (pure V4 behavior)
    "cartesia": {"speed": None, "volume": None, "emotion": ""},
    "elevenlabs": {"stability": None, "style": None, "speed": None},
    "note": "unlisted state — .env base tuning applies",
}

# keys: the 9 deployed Retell states + "begin" (the begin_message greeting)
DELIVERY_PROFILES: dict[str, dict] = {
    # --- opener ------------------------------------------------------------ #
    "begin": {
        "cartesia": {"speed": 1.12, "emotion": "happy"},
        "elevenlabs": {"stability": 0.50, "style": 0.30, "speed": 1.02},
        "note": "greeting: warm, energetic, smile in the voice",
    },
    "Intake": {
        "cartesia": {"speed": 1.12, "emotion": "happy"},
        "elevenlabs": {"stability": 0.50, "style": 0.30, "speed": 1.02},
        "note": "intake: same welcoming energy as the opener",
    },
    # --- qualification ----------------------------------------------------- #
    "Discovery": {
        "cartesia": {"speed": 1.07, "emotion": ""},
        "elevenlabs": {"stability": 0.60, "style": 0.15, "speed": 1.0},
        "note": "discovery: genuinely interested, unhurried questioning",
    },
    "VerifyLead": {
        "cartesia": {"speed": 1.07, "emotion": ""},
        "elevenlabs": {"stability": 0.65, "style": 0.10, "speed": 1.0},
        "note": "verify: precise, matter-of-fact, zero sales pressure",
    },
    # --- data collection: slow + calm beats charisma ----------------------- #
    "contact_details": {
        "cartesia": {"speed": 0.99, "emotion": "calm"},
        "elevenlabs": {"stability": 0.70, "style": 0.05, "speed": 0.95},
        "note": "phone numbers / codes read-back: slow, calm, maximally clear",
    },
    "ConfirmSlots": {
        "cartesia": {"speed": 1.07, "emotion": ""},
        "elevenlabs": {"stability": 0.60, "style": 0.10, "speed": 1.0},
        "note": "slots list: helpful clarity, no rush, no drag",
    },
    # --- commitment: energy ------------------------------------------------ #
    "Booking": {
        "cartesia": {"speed": 1.12, "emotion": "happy"},
        "elevenlabs": {"stability": 0.50, "style": 0.25, "speed": 1.02},
        "note": "confirmation: confident, upbeat — the caller just said yes",
    },
    # --- the pitch: the one expressive state -------------------------------- #
    "Offer": {
        "cartesia": {"speed": 1.15, "emotion": ""},
        "elevenlabs": {"stability": 0.45, "style": 0.35, "speed": 1.05},
        "note": "offer: expressive pitch energy (EL style up, stability down)",
    },
    # --- objections & goodbye: patience ------------------------------------- #
    "Closing": {
        "cartesia": {"speed": 1.02, "emotion": "calm"},
        "elevenlabs": {"stability": 0.65, "style": 0.10, "speed": 0.97},
        "note": "objections: patient, empathetic, never pushy",
    },
    "Closer": {
        "cartesia": {"speed": 1.02, "emotion": ""},
        "elevenlabs": {"stability": 0.60, "style": 0.10, "speed": 0.95},
        "note": "goodbye: warm, unhurried, leaves them smiling",
    },
}


def profile_for(state_name: str | None) -> dict:
    """Profile for a state; unknown/None -> DEFAULT (pure .env behavior)."""
    if not state_name:
        return DEFAULT_PROFILE
    return DELIVERY_PROFILES.get(state_name, DEFAULT_PROFILE)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))


def provider_overrides(profile: dict, provider: str) -> dict:
    """Only the fields the profile actually sets (others fall back to .env).

    Values are clamped to the documented ranges so a miscalibrated table
    entry can never make a provider reject the request.
    """
    out: dict = {}
    for field, value in (profile.get(provider) or {}).items():
        if value is None or value == "":
            continue
        bounds = _CLAMPS.get((provider, field))
        out[field] = _clamp(value, *bounds) if bounds else value
    return out


def resolve_delivery(settings, state_name: str | None) -> dict | None:
    """Overrides dict for the LIVE provider, or None when profiles are off.

    CallSession passes the result to tts.speak(..., overrides=...):
      - None  -> provider uses its .env globals (exact V4 behavior)
      - {}    -> same as None (state maps to all-None fields)
      - {...} -> these fields win over the .env globals for this turn
    """
    if not getattr(settings, "tts_delivery_profiles", False):
        return None
    provider = getattr(settings, "tts_provider", "cartesia")
    return provider_overrides(profile_for(state_name), provider)
