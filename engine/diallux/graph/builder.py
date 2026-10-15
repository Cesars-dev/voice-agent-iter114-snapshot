"""LangGraph builder — V3 production graph.

V1 translated the Retell runtime 1:1. V2 kept the same 9 state nodes and
loop semantics, plus:

  1. Prompts are read from `diallux/prompts/*.md` (editable source of truth,
     same content as the deployed llm.json — verified identical at build time).
  2. A `deterministic` node runs after EVERY state round — your "fix queued in
     the deterministic layer" open items, finally real:
       - leak math: when the three inputs exist and weekly_leak is empty,
         compute it server-side (the Closer can no longer stochastically skip
         the monetization pitch — open item #1 in your AGENTS.md)
       - today prefetch: entering ConfirmSlots with an empty today_date fetches
         /today directly (time never depends on the model remembering the tool)
  3. dvs flow through schema.DynamicVariables (typed; "" defaults; server-owned
     keys protected — see graph/tools.py).
  4. Transitions are hard-gated by the executor (edge `required` params must be
     truthy); the graph structure itself is unchanged because the executor is
     the single door for state swaps.
  5. Optional Postgres checkpointer (LANGGRAPH_CHECKPOINT=postgres) for
     multi-worker durability; MemorySaver fallback per call.

V3 adds (see ITERATIONS.md):
  6. RAG knowledge bases: pgvector top-3 retrieval per turn (the DEPLOYED
     Retell kb_config was {filter_score: 0.6, top_k: 3} — server-side RAG,
     now self-hosted). Inline full-KB fallback when Postgres is unavailable.
  7. VOICE OUTPUT RULES appended to the system prompt (Cartesia's voice-agent
     starter rules) so the model writes text the TTS reads well; the
     deterministic normalizer (media/normalize.py) backs it up per sentence.

One graph invocation == one user utterance (unchanged).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from langgraph.checkpoint.memory import MemorySaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from ..config import Settings
from .. import rag as ragmod
from ..schema import DynamicVariables
from ..state import GraphState, initial_state
from .llm import LITE_NOOP_TOOL, StreamingLLM, build_tool_schemas
from .subst import Substitutor
from .tools import ToolExecutor, _GOODBYE_RE, calculate_monthly_leak, leak_inputs_present

log = logging.getLogger("diallux.graph")   # iter43 T1: round-usage telemetry

# V3: TTS-friendly output rules — adapted from Cartesia's voice-agent starter
# prompt (docs.cartesia.ai, prompting tips). ON in production: the LLM itself
# writes speakable text; media/normalize.py catches what it misses.
VOICE_OUTPUT_RULES = """

## VOICE OUTPUT RULES (your text is spoken by a real-time TTS engine)
- Plain conversational prose. No markdown, bullet points, emoji, or special characters — they get read aloud.
- Always finish sentences with . ? or !
- Phone numbers, verification codes, and any run of 5+ digits: one character at a time, comma-separated, like 3, 1, 2, 4, 0, 0, 1, 2, 3, 4.
- Dollar amounts, dates, times: write them normally ($6,500, September 4th, 2:30 PM) — the engine reads them naturally.
- Tool arguments are never spoken; only your reply text is.
"""

# iter32: mechanical = bookkeeping the model may fire silently ONCE. After a
# silent mechanical round the tool channel closes (forced-speech round).
# end_call included: a blocked silent end_call must be answered with speech
# (the goodbye), never another silent end_call. Same prefix-anchor style as
# FIRE_AND_FORGET (extract_ prefix-match); NO $ anchor — it would break
# extract_* matching (the most common mechanical silent round).
_MECHANICAL_RE = re.compile(
    r"^(extract_|record_reach_details|set_callback_number|intake_completed|"
    r"discovery_completed|closer_completed|offer_completed|"
    r"contact_details_completed|record_booking_outcome|end_call)")

# iter41 T1: sentence boundaries for the TTS dedupe buffer. A sentence flushes
# at . ? ! followed by whitespace (or end of buffer) or at a newline (the
# model's doubles often join with "\n", e.g. "Got it — after-hours…\nGot it…").
_SENTENCE_SPLIT_RE = re.compile(r"[.?!]+(?=\s|$)|\n+")

# iter56 (C2.1): literal {{var}} tokens in the static head — rewritten to the
# stable anchor "CURRENT CALL STATE" when head_strip_vars is on. Matches the
# same token shape the substituter owns (lowercase snake_case names).
_VAR_TOKEN_RE = re.compile(r"\{\{\s*[a-z_][a-z0-9_]*\s*\}\}")

# ---- iter49 T4: live multi-lane retrieval ----------------------------------
# Closing.md has ZERO refer tags (lane A would be empty) and raw "ok thanks
# bye" refuses at arctic 0.24 — the goodbye KB only surfaces via a hand-written
# anchor query (pre-flight: call-closing 4/4). E3: after a successful booking
# the close is a RECAP, not a rescue — swap the anchor.
_CLOSING_ANCHOR = "warm goodbye wrap-up next steps"
_CLOSING_ANCHOR_BOOKED = "recap booking SMS confirmation next steps"
# Pinned pack #6: leak dv values render $-formatted WITH units in lane-A
# queries — raw numerals ("60,000") score below 0.24 and refused the Closer
# Loss-Playback lane in the pre-flight. calculate_monthly_leak stores
# comma-formatted strings ("60,000"); the $ + unit make it "$60,000 a week".
_LEAK_UNITS = {"weekly_leak": "a week", "monthly_leak": "a month"}
# Lane budget (owner 2026-09-13): <=3 engine lanes + <=3 caller lanes per turn.
_MAX_LANES_A = 3
_MAX_LANES_B = 3

# ---- iter52: pinned industry vertical ---------------------------------------
# Resolver stoplist (plan T3): values that NEVER pin — including the literal
# 'header' tag so the header row is unreachable by resolution (defense in
# depth: stoplist + embed-fallback skip + no alias targets it).
_PIN_STOPLIST = {"", "unknown", "n/a", "none", "not sure", "unsure", "tbd",
                 "various", "multiple", "header"}
# Alias map: word-boundary substring, every alias -> exactly ONE tag,
# sorted longest-first. >=2 DISTINCT tag hits = ambiguous -> embed fallback
# (never guess). NO bare "contractor" (a generic "contractor" must not beat
# "roofing contractor"). plumbing/plumber -> the 20th vertical (owner v3).
_PIN_ALIASES: tuple[tuple[str, str], ...] = tuple(sorted((
    ("dental", "Dental Practices"),
    ("dentist", "Dental Practices"),
    ("law firm", "Law Firms (Personal Injury, Family, Immigration)"),
    ("personal injury", "Law Firms (Personal Injury, Family, Immigration)"),
    ("attorney", "Law Firms (Personal Injury, Family, Immigration)"),
    ("auto repair", "Auto Repair & Body Shops"),
    ("auto body", "Auto Repair & Body Shops"),
    ("mechanic", "Auto Repair & Body Shops"),
    ("landscap", "Landscaping, Maintenance & Tree Services"),
    ("lawn", "Landscaping, Maintenance & Tree Services"),
    ("tree service", "Landscaping, Maintenance & Tree Services"),
    ("remodel", "Home Remodeling & Small General Contractors"),
    ("renovation", "Home Remodeling & Small General Contractors"),
    ("general contractor", "Home Remodeling & Small General Contractors"),
    ("roofing", "Residential Roofing & Solar Installers"),
    ("solar", "Residential Roofing & Solar Installers"),   # iter62: dv
    # "solar company" fell to embed-fallback < 0.24 and cached a per-dv
    # miss for the whole call (pinned="" on all 67 spans, cc22e0a0185e)
    ("hvac", "HVAC & Home Services"),
    ("heating", "HVAC & Home Services"),
    ("air conditioning", "HVAC & Home Services"),
    ("med spa", "Medical Spas & Aesthetic Clinics"),
    ("medical spa", "Medical Spas & Aesthetic Clinics"),
    ("aesthetic", "Medical Spas & Aesthetic Clinics"),
    ("vet", "Veterinary Clinics (General Practice, Small Animal)"),
    ("veterinary", "Veterinary Clinics (General Practice, Small Animal)"),
    ("moving", "Local & Regional Moving Companies"),
    ("maid", "Residential Home Cleaning & Maid Services"),
    ("house cleaning", "Residential Home Cleaning & Maid Services"),
    ("cleaning service", "Residential Home Cleaning & Maid Services"),
    ("property management", "Residential Property Management"),
    ("pest", "Pest Control (Residential & Commercial)"),
    ("garage door", "Garage Door & Locksmith Services"),
    ("locksmith", "Garage Door & Locksmith Services"),
    ("appliance repair", "Appliance Repair & Handyman Services"),
    ("handyman", "Appliance Repair & Handyman Services"),
    ("security system", "Residential/Commercial Security & Smart Home"),
    ("smart home", "Residential/Commercial Security & Smart Home"),
    ("alarm", "Residential/Commercial Security & Smart Home"),
    ("msp", "IT Managed Service Providers (MSPs)"),
    ("it services", "IT Managed Service Providers (MSPs)"),
    ("it support", "IT Managed Service Providers (MSPs)"),
    ("managed service", "IT Managed Service Providers (MSPs)"),
    ("urgent care", "Private Clinics (Urgent Care, Physical Therapy, Chiropractic)"),
    ("physical therapy", "Private Clinics (Urgent Care, Physical Therapy, Chiropractic)"),
    ("chiropract", "Private Clinics (Urgent Care, Physical Therapy, Chiropractic)"),
    ("dermatology", "Specialty Clinics (Dermatology & Ophthalmology)"),
    ("ophthalmology", "Specialty Clinics (Dermatology & Ophthalmology)"),
    ("plumbing", "Plumbing"),
    ("plumber", "Plumbing"),
), key=lambda t: -len(t[0])))
# iter52 T5: deep-dive scope triggers — normalized {{industry}} containing
# the keyword adds the KB to that round's lane-B scope (INDEPENDENT of the
# pin; similarity decides; the deep-dive KB is NEVER pinned). Generalizable
# industry-keyword -> extra-KB map. Gated by the same rag_pin_industry
# kill switch (False = byte-exact iter49 live behavior).
_INDUSTRY_KB_TRIGGERS: dict[str, tuple[str, ...]] = {"plumb": ("plumbing",)}


def _fmt_leak_value(name: str, value) -> str:
    """"$60,000 a week" from the tool's raw "60,000" — passthrough when the
    value is already $-formatted or empty."""
    s = str(value or "").strip()
    if not s or "$" in s:
        return s
    unit = _LEAK_UNITS.get(name)
    return f"${s} {unit}" if unit else s


def _fast_flush_cut(text: str, settings) -> int:
    """iter46 T5: fast first-flush boundary for the sentence gate — the first
    "," that follows ≥12 chars, or the whole buffer once it reaches
    tts_gate_fast_first_flush_chars (28 default). 0 = no flush.
    iter114: a comma flanked by digits is a thousands separator inside a
    number ("...about $3,375 a week") — never a clause boundary; skip it."""
    comma = text.find(",")
    while comma >= 12:
        if 0 < comma < len(text) - 1 \
                and text[comma - 1].isdigit() and text[comma + 1].isdigit():
            comma = text.find(",", comma + 1)
            continue
        return comma + 1
    limit = getattr(settings, "tts_gate_fast_first_flush_chars", 28)
    if limit > 0 and len(text) >= limit:
        return len(text)
    return 0


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity, pure python (1536 dims is trivial work)."""
    num = da = db = 0.0
    for x, y in zip(a, b):
        num += x * y
        da += x * x
        db += y * y
    if not da or not db:
        return 0.0
    return num / ((da ** 0.5) * (db ** 0.5))


def _norm_sentence(s: str) -> str:
    """Dedupe key: case-insensitive, whitespace-collapsed sentence text."""
    return " ".join(s.split()).casefold()


# --------------------------------------------------------------------------- #
# iter114 speech authorization (render guard): the mouth may only SPEAK
# figures/times/status claims the ENGINE has authorized — the WIT card, tool
# results, or the caller's own words. Sentence-level, regex-only, fail-closed;
# a cut sentence never reaches TTS. Tier 1a = numbers/times (cross-kind digit
# authorization); Tier 1b = status lexicon gated on engine-known facts.
_SPK_MONEY_RE = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)")
_SPK_PCT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s?%")
_SPK_TIME_RE = re.compile(r"\b(\d{1,2}(?::\d{2})?)\s?(am|pm)\b", re.IGNORECASE)
_SPK_BIGNUM_RE = re.compile(r"\b(\d{1,3}(?:,\d{3})+|\d{3,})\b")
_SPK_PHONE_RUN_RE = re.compile(r"\b\d(?:[,\s-]+\d){6,}\b")
_SPK_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
# Tier 1b — status lexicon (each class gated on a card fact).
_SPK_AVAIL_RE = re.compile(
    r"\b(is|are|was|were) available\b|\bavailable slots?\b|\bslot is open\b",
    re.IGNORECASE)
_SPK_CHOSE_RE = re.compile(
    r"\byou (chose|picked|selected)\b|\b(slot|time) you (chose|picked|selected)\b",
    re.IGNORECASE)
_SPK_FILLED_RE = re.compile(
    r"\b(filled up|got taken|someone (took|grabbed|booked))\b", re.IGNORECASE)
_SPK_COMMIT_EXTRA_RE = re.compile(
    r"\b(?:have|got) you down for\b|\b(slot|time) (?:is|was) set\b|"
    r"\bregistered for you\b", re.IGNORECASE)
_GUARD_RECOVERY_NUM = "Let me run those numbers real quick — one moment."
_GUARD_RECOVERY_STATUS = "Let me check on that — one moment."


def _spk_digits(text: str) -> str:
    return re.sub(r"\D", "", text or "")


def _spk_tokens(text: str) -> set[str]:
    """Normalized numeric tokens in a text chunk:
    `$<digits>` money, `%<digits>` percent, `t<digits><am|pm>` time,
    `#<3+ digits>` bare big numbers, `p<digits>` phone-format runs.
    Bare 1–2 digit integers are NOT tokens (small talk stays allowed)."""
    toks: set[str] = set()
    if not text:
        return toks
    for m in _SPK_MONEY_RE.finditer(text):
        d = _spk_digits(m.group(1).split(".")[0])
        if d:
            toks.add("$" + d)
    for m in _SPK_PCT_RE.finditer(text):
        d = _spk_digits(m.group(1).split(".")[0])
        if d:
            toks.add("%" + d)
    for m in _SPK_TIME_RE.finditer(text):
        toks.add("t" + _spk_digits(m.group(1)) + m.group(2).lower())
    for m in _SPK_BIGNUM_RE.finditer(text):
        d = m.group(1).replace(",", "")
        if len(d) >= 3:
            toks.add("#" + d)
    for m in _SPK_PHONE_RUN_RE.finditer(text):
        toks.add("p" + _spk_digits(m.group(0)))
    return toks


def _spk_auth_numbers(text: str) -> set[str]:
    """Every digit run in the text (commas pre-stripped) — cross-kind
    authorization: a value stored as "3,375" authorizes a spoken "$3,375"."""
    nums: set[str] = set()
    if not text:
        return nums
    for m in re.finditer(r"\d+(?:\.\d+)?", text.replace(",", "")):
        nums.add(m.group(0))
    return nums


# iter114 v1.1 (E7 live evidence): the caller speaks WORDS, the mouth
# renders DIGITS — "four to five thousand" -> "$4,000 to $5,000", "one pm"
# -> "1 PM". Those mirrors must stay authorized or the guard false-cuts
# faithful speech (E7: 3/3 cuts were word-sourced mirrors).
_SPK_WNUM = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}
_SPK_WNUM_RE = re.compile(
    r"\b(?:(twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)"
    r"[ -]?(one|two|three|four|five|six|seven|eight|nine)?"
    r"|(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen)"
    r"|(dozen))\b", re.IGNORECASE)
_SPK_WTIME_RE = re.compile(
    r"\b(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
    r"(?:\s+(five|ten|fifteen|twenty|twenty[ -]five|thirty|"
    r"thirty[ -]five|forty|forty[ -]five))?\s*(am|pm)\b", re.IGNORECASE)


_SPK_TENS = {"twenty", "thirty", "forty", "fifty", "sixty", "seventy",
             "eighty", "ninety"}
_SPK_UNITS = {"one", "two", "three", "four", "five", "six", "seven",
              "eight", "nine"}


def _spk_word_amounts(text: str) -> set[str]:
    """Digit strings for word-number amounts ≥100 in the text:
    'four hundred fifty' -> {450}; 'forty eight hundred' -> {4800};
    'four to five thousand' -> {4000, 5000}. Only ≥3-digit values emitted."""
    nums: set[str] = set()
    if not text:
        return nums
    low = text.lower()
    toks = list(re.finditer(r"[a-z-]+", low))
    n = len(toks)
    idx = 0
    while idx < n:
        total = 0
        current = 0
        consumed = False
        j = idx
        while j < n:
            w = toks[j].group(0)
            if w in _SPK_WNUM:
                if (w in _SPK_TENS and j + 1 < n
                        and toks[j + 1].group(0) in _SPK_UNITS
                        and toks[j + 1].start() - toks[j].end() <= 2):
                    current += _SPK_WNUM[w] + _SPK_WNUM[toks[j + 1].group(0)]
                    j += 2
                else:
                    current += _SPK_WNUM[w]
                    j += 1
                consumed = True
                continue
            if w == "hundred" and consumed:
                current = max(1, current) * 100
                j += 1
                continue
            if w in ("thousand", "grand") and consumed:
                total += max(1, current) * 1000
                current = 0
                j += 1
                continue
            if w == "and" and consumed:
                j += 1
                continue
            break
        if consumed:
            val = total + current
            if val >= 100:
                nums.add(str(val))
            idx = max(j, idx + 1)
        else:
            idx += 1
    # range-aware ×1000: each number word with 'thousand' in its immediate
    # tail counts ×1000 ("four TO five thousand" -> 4000 AND 5000).
    for m in _SPK_WNUM_RE.finditer(low):
        if m.group(1):
            val = _SPK_WNUM[m.group(1)] + _SPK_WNUM.get(m.group(2) or "", 0)
        elif m.group(3):
            val = _SPK_WNUM[m.group(3)]
        else:
            val = 12
        tail = low[m.end():m.end() + 24]
        if re.search(r"\b(thousand|grand|k)\b", tail):
            val *= 1000
        elif re.search(r"\bhundred\b", tail):
            val *= 100
        if val >= 100:
            nums.add(str(val))
    return nums


_SPK_DIGIT_WORDS = _SPK_UNITS | {"zero", "oh"}
_SPK_DIGIT_VAL = {**{w: str(i) for i, w in enumerate(
    ("zero", "one", "two", "three", "four", "five", "six", "seven",
     "eight", "nine"), 0)}, "oh": "0"}


def _spk_word_digits(text: str) -> set[str]:
    """iter114d (FIND-114C, owner: 'if we see spoken numbers we convert
    into digits'): digit strings for word-spoken DIGIT RUNS — the caller
    says 'five one two, three one two, zero zero zero one', the mouth
    renders '5, 1, 2, 3, 1, 2, 0, 0, 0, 1' (phone-run token). Runs of
    >=3 consecutive digit-words (zero/oh/one..nine) are greedily consumed
    and emitted as the concatenated digit string; 1-2 word runs are NOT
    emitted (small-talk safe, same law as _spk_tokens' bare ints)."""
    nums: set[str] = set()
    if not text:
        return nums
    toks = list(re.finditer(r"[a-z-]+", text.lower()))
    n = len(toks)
    i = 0
    while i < n:
        if toks[i].group(0) in _SPK_DIGIT_WORDS:
            run: list[str] = []
            j = i
            while j < n and toks[j].group(0) in _SPK_DIGIT_WORDS:
                run.append(_SPK_DIGIT_VAL[toks[j].group(0)])
                j += 1
            if len(run) >= 3:
                nums.add("".join(run))
            i = j
        else:
            i += 1
    return nums


def _spk_word_times(text: str) -> set[str]:
    """`t<digits><am|pm>` tokens for word times: 'one pm' -> t1pm,
    'one thirty pm' -> t130pm."""
    toks: set[str] = set()
    if not text:
        return toks
    for m in _SPK_WTIME_RE.finditer(text):
        hh = _SPK_WNUM[m.group(1).lower()]
        mm = ""
        if m.group(2):
            mm = str(_SPK_WNUM.get(m.group(2).lower().replace("-", " "), 0))
            if m.group(2).lower() in ("twenty five", "thirty five", "forty five"):
                mm = str(_SPK_WNUM[m.group(2).lower().split()[0]] + 5)
        toks.add(f"t{hh}{mm}{m.group(3).lower()}")
    return toks


def _speak_guard_cut(sent: str, auth_tokens: set[str], auth_nums: set[str],
                     dvs: dict, tool_text: str) -> str | None:
    """Return a cut reason when the sentence asserts something unauthorized,
    else None. Runs BEFORE the dedupe window and `round_asked` bookkeeping."""
    toks = _spk_tokens(sent)
    for tok in toks:
        if tok in auth_tokens:
            continue
        if tok.startswith("#") and _SPK_YEAR_RE.match(tok[1:]):
            continue                        # plain years are not claims
        if _spk_digits(tok) in auth_nums:   # cross-kind digit equality
            continue
        return "number:" + tok
    booking_fact = bool(str(dvs.get("booking_uid") or "").strip()) \
        or bool(dvs.get("booking_verified"))
    if not booking_fact and (_BOOKED_CLAIM_RE.search(sent)
                             or _SPK_COMMIT_EXTRA_RE.search(sent)):
        return "booking-claim-without-booking"
    if not booking_fact and _SPK_AVAIL_RE.search(sent) \
            and "not_offered" in tool_text:
        return "available-claim-over-not_offered"
    if _SPK_CHOSE_RE.search(sent):
        sel = str(dvs.get("selected_time") or "").strip()
        if not sel:
            return "chose-claim-without-selection"
        sel_toks = {t for t in _spk_tokens(sel) if t.startswith("t")}
        sent_toks = {t for t in toks if t.startswith("t")}
        if sent_toks and sel_toks and sent_toks != sel_toks:
            return "chose-claim-time-mismatch"
    if _SPK_FILLED_RE.search(sent) and "preferred_unavailable" not in tool_text:
        return "unavailability-narrative-no-event"
    return None


# iter65 T2: spoken-sentence sanitizer — runs BEFORE the dedupe window on
# every flushed sentence (mid-stream cut + stream-end pending). Model
# output/history/final content are NEVER touched; only the spoken stream.
_FUNC_CALLS_RE = re.compile(r"\bfunction_calls\b", re.IGNORECASE)
_CODEISH_CHARS = set("{}[]=>")
# iter114 T6: CJK/fullwidth punctuation (e.g. "】【。") is glyph garbage in the
# TTS stream — drop the sentence like other code artifacts. Ranges: CJK
# symbols/punctuation (U+3000–303F) + fullwidth forms (U+FF00–FFEF). Latin
# accents (Spanish callers) are untouched.
_CJK_PUNCT_RE = re.compile(r"[\u3000-\u303f\uff00-\uffef]")


def _sanitize_spoken(text: str, settings) -> str | None:
    """None = drop the sentence from speech (code/JSON artifact).
    1) strip a verbatim `function_calls` protocol token;
    2) drop if any structural code char { } [ ] => OR the JSON-ish pair `":`
       (single ':' = natural times "10:30"; single '"' = quotes — kept)."""
    if not getattr(settings, "tts_sanitize_tokens", True):
        return text
    t = _FUNC_CALLS_RE.sub("", text).strip()
    if not t:
        return None
    if any(c in t for c in _CODEISH_CHARS) or '":' in t \
            or _CJK_PUNCT_RE.search(t):
        return None
    return t


async def _embed_stem(runtime, sentence: str) -> list[float] | None:
    """iter59 T4: ONE query embedding of a spoken question stem via the
    store's reused embed client (rag.py embed_query). None on store-absent /
    failure — the caller keeps the exact-only fallback and never blocks
    speech. Best-effort, never raises."""
    try:
        store = await runtime._resolve_kb_store()
        if store is None:
            return None
        return await store.embed_query(sentence)
    except Exception:
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    da = sum(x * x for x in a)
    db = sum(y * y for y in b)
    if not da or not db:
        return 0.0
    return num / ((da ** 0.5) * (db ** 0.5))


async def _semantic_dup(runtime, sentence: str,
                        stem_window: list[dict]) -> bool:
    """iter59 T4: True when the question's stem is a near-verbatim re-ask
    of a question already spoken this call (cosine >= threshold). ONE
    embed; embed failure -> False (exact-only fallback, never block)."""
    try:
        vec = await _embed_stem(runtime, sentence)
        if vec is None:
            return False
        thr = float(runtime.settings.tts_dedupe_cos_threshold)
        for entry in stem_window:
            prev = entry.get("vec")
            if not prev:
                continue
            if _cosine(vec, prev) >= thr:
                return True
        return False
    except Exception:
        return False


def _spoken_entry_has(stem_window: list[dict], key: str) -> bool:
    """iter59 T4: the stem key already in the window (avoid double-embed of
    the same question text across rounds of a turn)."""
    return any(e.get("text") == key for e in stem_window)

# iter105 T1 (ask-hold): ratified verbatim 2026-10-10 — the engine tells the
# model its post-question tail was chopped so it does not repeat the question.
_CHOP_NOTE = ("NOTE: your previous reply was cut off after your question — "
              "the caller did NOT hear the rest. Do not repeat what you already "
              "asked; wait for their answer.")

# iter105 T2 (ask-hold defer gate): the caller-visible, answer-dependent
# tools never execute on an open question — the model must wait for the
# caller's answer, then re-call with what they said. iter114 T2:
# calculate_monthly_leak left this set — it is pure compute, no caller
# answer needed, and no longer model-invoked (ENGINE_ONLY_TOOLS).
_DEFER_SET = {"query_livecall_slots", "create_livecall_booking"}

# iter105 T2 (S2 day-guard): day/time expressions that prove the caller has
# already chosen a day — those pulls execute immediately (answer-first flows
# never cost an extra exchange). KEEP IN SYNC with the T5 replay classifier
# (engine/scripts/s3_replay.py DAY_RE — one constant, two readers).
_DAY_RE = re.compile(
    r"\b(tomorrow|today|tonight|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday|afternoon|morning|evening|soonest|earliest|next day|"
    r"asap|as soon as possible)\b"
    r"|\b\d{1,2}:\d{2}\b|\b\d{1,2}\s*(am|pm)\b", re.I)

# iter105 T2: synthetic tool-result messages for a deferred call.
_DEFER_S1_MSG = ("You asked the caller a question in this same response. "
                 "Wait for their answer, then call {tool} again with what "
                 "they said.")
_DEFER_S2_MSG = ("Slot pull deferred: the caller has not chosen a day yet. "
                 "Ask 'Would you prefer later today or tomorrow for the demo?', "
                 "wait for their answer, then call query_livecall_slots with "
                 "their chosen day as slot_target_date.")

# iter33 Phase B: engine-owned interstitial fillers. Spoken via tts_token —
# never an LLM call. Anti-repeat within call (mirrors general_prompt's rule).
_ENGINE_FILLERS = [
    "One sec — finishing that up.",
    "Alright, getting that locked in for you.",
    "Just confirming those details now.",
    "Great — booking that for you as we speak.",
    "Almost there, locking in your time.",
    "Perfect, just a moment while I finish this.",
]
_PHASEB_ABORT = {"gate_failed", "incomplete", "unfixable_format", "book_failed",
                 "error", "invalid_uid", "uid_not_found", "request_failed"}
_PHASEB_SLOW = {"verify_lead_data", "create_livecall_booking", "record_booking_uid"}

# iter112 speak-first: divergence monitor — the SPEAK stream claiming a
# booking outcome while the background TOOLS stream called nothing (the
# iter110 spoke-outran-the-tools failure class, generalized).
_BOOKED_CLAIM_RE = re.compile(
    r"(all booked|you'?re all set|all set for|you'?re booked|i'?ve booked|"
    r"booked (you|it|that)|confirmation (text|email|message)|"
    r"confirmed (your|the) (slot|time|appointment)|scheduled you|"
    r"reserved (your|the|it)|you'?re confirmed)", re.I)


@dataclass
class _PendingTools:
    """iter112: a background TOOLS task awaiting its merge point.
    M1 adopts it in-round when already done; M2 awaits it at the next
    turn's ingest (await-on-arrival, no caps); M3 cancels at close."""
    task: asyncio.Task
    state_name: str          # the state whose round fired it (executor context)
    turn_index: int
    dispatched_at: float
    spoken_text: str = ""    # the SPEAK text of that round (divergence checks)


def _chain_args(step: str, dvs: dict, booking_uid: str = "") -> dict:
    """Deterministic chain args — byte-equal to the model's templates today."""
    if step == "validate_lead":
        return {"first_name": dvs.get("first_name", ""), "last_name": dvs.get("last_name", ""),
                "company_name": dvs.get("company_name", ""),
                "callback_number": dvs.get("callback_number", ""),
                "prospect_timezone": dvs.get("prospect_timezone", ""),
                "selected_time": dvs.get("selected_time", ""),
                "missed_calls_per_week": dvs.get("missed_calls_weekly", ""),
                "avg_job_value": dvs.get("avg_job_value", ""),
                "close_rate_pct": dvs.get("close_rate_pct", "")}
    if step == "verify_lead_data":
        return {"first_name": dvs.get("first_name", ""), "last_name": dvs.get("last_name", ""),
                "company_name": dvs.get("company_name", ""),
                "callback_number": dvs.get("callback_number", ""),
                "prospect_timezone": dvs.get("prospect_timezone", ""),
                "selected_time": dvs.get("selected_time", ""),
                "slot_options": dvs.get("requested_slot", "")}
    if step == "create_livecall_booking":
        name = f"{dvs.get('first_name', '')} {dvs.get('last_name', '')}".strip()
        return {"account_id": "diallux_live", "timezone": dvs.get("prospect_timezone", ""),
                "time": dvs.get("selected_time", ""), "name": name or dvs.get("company_name", ""),
                "email": "jaydiallux@gmail.com", "attendeePhoneNumber": dvs.get("callback_number", ""),
                "title": f"Dialux Live call for {dvs.get('company_name', '')}",
                "notes": f"Industry: {dvs.get('industry', '')} | Pain points: {dvs.get('pain_points', '')}",
                "slot_reservation_uids": dvs.get("slot_reservation_uids", ""),
                "booking_intent": dvs.get("booking_intent", ""), "preferred_time": ""}
    if step == "record_booking_uid":
        return {"booking_uid": booking_uid}
    return {}


class CallRuntime:
    """Everything scoped to ONE phone call: graph, llm, executor, tracer."""

    def __init__(
        self,
        settings: Settings,
        llm_json: dict,
        tracer=None,
        llm: StreamingLLM | None = None,
        http_client=None,
        checkpointer=None,
        kb_store=...,   # ellipsis = resolve lazily; None = force off; object = injected (tests/eval)
    ):
        self.settings = settings
        self.llm_json = llm_json
        self.states: dict[str, dict] = {s["name"]: s for s in llm_json.get("states", [])}
        # iter87 VERSION B fire law: FSM successor map derived from the SAME
        # edges that generate the transition_to_* tool schemas (llm.py
        # build_tool_schemas) — one source of truth, no hand-written table.
        self._state_successors: dict[str, list[str]] = {
            name: [e["destination_state_name"] for e in cfg.get("edges", [])]
            for name, cfg in self.states.items()}
        self.subst = Substitutor(settings.knowledge_base_dir)
        self.tracer = tracer
        self.llm = llm or StreamingLLM(settings)
        # iter40 L2: shared warm store for the ASAP availability payload.
        # {"payload": <webhook body>, "ts": time.monotonic(), "tz": <IANA>}
        # Written by _warm_slots (bg task), read by ToolExecutor prefetch.
        self._slots_warm: dict = {}   # {date: {"payload","ts","tz","task"}} (iter40 L2)
        self._slots_warm_task: asyncio.Task | None = None
        self.executor = ToolExecutor(settings, llm_json, tracer=tracer, http_client=http_client,
                                     slots_warm=self._slots_warm)
        self.prompts = self._load_prompts()
        self._kb_store = kb_store
        self._kb_store_resolved = kb_store is not ...
        self._kb_stats = {"rag_turns": 0, "rag_ms_total": 0.0, "rag_chars": 0}
        self._rag_cache: dict = {}          # iter29: per-(state, query) retrieval cache
        # iter31 C3: byte-stable prefix — static head + tool schemas cached per
        # (state, expand_kb); RAG chunks frozen per state-visit.
        self._head_cache: dict = {}
        self._tools_cache: dict = {}
        self._frozen_state: str | None = None
        self._frozen_chunks: list = []
        # iter44 T6c: drift-requery stash — vector + text of the query that
        # produced _frozen_chunks (vec may be None until lazily embedded).
        self._frozen_query_vec: list | None = None
        self._frozen_query_text: str = ""
        # iter44 T6b: transition RAG prefetch — staged chunks consumed by the
        # state_node rag lane (pop), in-flight task registry + per-state latch.
        self._rag_staging: dict[str, dict] = {}
        self._rag_staging_tasks: dict[str, asyncio.Task] = {}
        self._frozen_rag_tail: str = ""          # iter44 append-only: byte-stable knowledge tail
        self._rag_delta_chunks: list[dict] = []  # chunks appended AFTER history on drift
        # iter44 T4: loud no-KB warning is logged once per call, not per round.
        self._inline_guard_logged = False
        # iter44 T6c: drift-embed failure backoff (60 s, monotonic)
        self._drift_failed_at = 0.0
        # iter46 T2: async drift check — one serialized task per state,
        # cancel-superseded when a newer caller message lands.
        self._drift_tasks: dict[str, asyncio.Task] = {}
        # iter43 T3: async prompt-cache prewarm — latched per state, tasks
        # cancelled in aclose()
        self._prewarmed: set[str] = set()
        self._warm_tasks: set[asyncio.Task] = set()
        # iter46 T4c: newest warm task per state — the bounded entry await
        # targets this one (the fire-early warm may be superseded by the
        # post-execute warm with corrected dvs bytes).
        self._latest_warm: dict[str, asyncio.Task] = {}
        # iter112 speak-first: the background TOOLS task of the last heavy
        # round, awaiting its merge point (M1 in-round adopt / M2 next-turn
        # ingest await / M3 cancel at close). None when flag off or no
        # pending round. Survives the state_node walk by design.
        self._pending_tools: _PendingTools | None = None
        # iter55 T0: per-call warm failure diag ({key: {error, retried_lite}})
        # — read by the warm done-callback to mark the span degraded/failed.
        self._warm_diag: dict[str, dict] = {}
        # iter49 T3: live-retrieve bg tasks (key = state_name) + the previous
        # turn's landed fresh set per state (the degrade source). The eager
        # EOT fires the task; the round bounded-awaits it (rag_live_await_ms)
        # and degrades to _live_prev — never a stall. Cancelled in aclose().
        self._live_tasks: dict[str, asyncio.Task] = {}
        self._live_prev: dict[str, dict] = {}
        self._live_msgs: dict[str, str] = {}
        # iter59 T3: turn-keyed supersede bookkeeping. Keys are the composite
        # "<state>|<lane>" ("full" | "laneA" | "laneB"):
        #   _live_seq        monotonically increasing fire seq per state
        #   _live_task_seq   the seq of the task currently stored per key
        #   _live_consumed   highest seq CONSUMED (fresh) per state
        #   _live_consumed_res the last consumed FRESH raw/merged pieces per
        #                    state — the timeout fallback degrades to THIS,
        #                    never to _live_prev's unconsumed overwrite (C4)
        self._live_seq: dict[str, int] = {}
        self._live_task_seq: dict[str, int] = {}
        self._live_consumed: dict[str, int] = {}
        self._live_consumed_res: dict[str, dict] = {}
        # iter103 PT-5: per-call byte-identical query-set memo for RAG fires
        # ({normalized key -> _retrieve_raw result tuple}). A canned query
        # that already fired this call never fires again (skip embed+search,
        # reuse the stored pieces). Calls never cross — the dict dies with
        # the call (no sqlite, no cross-call layer).
        self._rag_query_memo: dict[str, dict] = {}
        # iter103 PT-5 (observability): did the last _retrieve_raw fire hit
        # the memo? read by the live rag span (`memo` field).
        self._last_retrieve_memo: bool = False
        # iter60 AUD-3: per-turn merged-set stash — rounds 2+ of the SAME
        # turn reuse round-1's merged set (the seq floor rightly rejects
        # stale laneA but also rejected THIS turn's laneA on tool rounds).
        self._live_merged_turn: dict[str, dict] = {}
        # iter59 T3: consume-mode label for the node's rag span
        # ("live-async" | "live-sync" | "hybrid").
        self._live_mode_label: str = "live-async"
        # iter103 PT-8 (observability-only): the last round's pre-LLM gap
        # breakdown + the per-round warm-await scratch — read by the
        # session's late turn report as `pre_llm_ms`.
        self._last_round_pre_llm: dict | None = None
        self._last_warm_wait: int = 0
        # iter52 T5: the pinned industry vertical — {"dv", "tag", "chunks"}
        # fetched ONCE per call (per dv value) inside the bg _live_retrieve
        # task; re-resolved only when the dv value changes; reset per call
        # in initial_state(). The pin renders OUTSIDE merge_lane_chunks
        # (own block, own budget) in the post-history delta.
        self._pinned_industry: dict = {}
        # iter49 T6 (T4b): the state whose head the LAST round rendered —
        # the state-entry-lite trigger compares against it. None until a
        # non-turn-1-lite round runs (the turn-1 lite round renders no state
        # head, so the turn-2 entry into the initial state still counts as
        # an entry). Reset per call in initial_state().
        self._prev_round_state: str | None = None
        self.graph = self._build(checkpointer)

    async def aclose(self):
        # iter112 M3: no orphan background TOOLS tasks at close — a still-
        # pending future is cancelled (its results were never merged).
        self._split_close_pending("aclose")
        for task in list(self._warm_tasks):
            task.cancel()
        for task in self._drift_tasks.values():
            task.cancel()
        for task in self._live_tasks.values():
            task.cancel()
        if self._warm_tasks:
            await asyncio.gather(*self._warm_tasks, return_exceptions=True)
        if self._drift_tasks:
            await asyncio.gather(*self._drift_tasks.values(), return_exceptions=True)
        if self._live_tasks:
            await asyncio.gather(*self._live_tasks.values(),
                                 return_exceptions=True)
        self._warm_tasks.clear()
        self._drift_tasks.clear()
        self._live_tasks.clear()
        await self.executor.aclose()

    # ------------------------------------------------------------------ #
    # iter43 T3: async prompt-cache prewarm. The EXACT next-round payload
    # shape (tools FIRST + stripped static head + hysteretic history window)
    # is sent with a 16-token cap; OpenAI caches the prefix bytes, the caller
    # never waits, output is discarded. Cold-vs-cached TTFT gap (~800 ms at
    # iter21) closes without touching the hot path.
    def warm_prompt_cache(self, state_name: str, history: list[dict] | None = None,
                          dvs: dict | None = None, lite: bool = False,
                          force: bool = False) -> None:
        """Fire-and-forget prefix warm for `state_name`'s FULL payload shape.
        Latched once per state per call; no-op outside a running loop.
        iter46 T3: `lite=True` warms the first_turn_lite shape instead (no
        tools, no tail). iter46 T4a: `force=True` re-fires a warm for an
        already-latched state — the post-execute transition warm supersedes
        the tool-detection warm with the post-patch dvs bytes.
        `_latest_warm[<key>]` always points at the newest task (T4c bounded
        await awaits THAT one at state entry; the non-lite key is the bare
        state name, the lite warm registers under `lite:<state>`)."""
        if not self.settings.prompt_prewarm:
            return
        key = f"lite:{state_name}" if lite else state_name
        if key in self._prewarmed and not force:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._prewarmed.add(key)
        fired = time.time()                # iter55 T0: warm fire timestamp
        task = asyncio.get_running_loop().create_task(
            self._warm(state_name, history or [], dvs, lite=lite))
        self._warm_tasks.add(task)
        # iter55 T0: done-callback emits the `warm:{key}` span (fire→complete/
        # fail per shape) — fail-safe, never raises into the event loop.
        task.add_done_callback(self._make_warm_done_cb(key, state_name, lite,
                                                       fired))
        # iter48 Commit B (M12): lite warms are awaitable too — registered
        # under `lite:<state>` so the turn-1 (lite) round can bounded-await
        # them at entry (audit A5: the lite task was previously invisible to
        # _latest_warm → live turn-1 cache_read=0). Collides with nothing.
        if lite:
            self._latest_warm[key] = task
        else:
            self._latest_warm[state_name] = task

    def _make_warm_done_cb(self, key: str, state_name: str, lite: bool,
                           fired: float):
        """iter55 T0: done-callback factory for warm tasks — emits the
        `warm:{key}` span (shape, state, fired_at, ms, outcome, degraded,
        error) + an info log. Fail-safe: its own try/except, same contract
        as the tracer."""
        def _cb(task: asyncio.Task) -> None:
            try:
                ms = round((time.time() - fired) * 1000)
                if task.cancelled():
                    outcome = "cancelled"
                elif task.exception() is not None:
                    outcome = "failed"
                else:
                    outcome = "ok"
                diag = self._warm_diag.get(key)
                degraded = bool(diag and diag.get("retried_lite"))
                error = diag.get("error") if diag else None
                if outcome == "failed" and error is None:
                    error = str(task.exception())
                log.info("warm done key=%s shape=%s outcome=%s degraded=%s ms=%s",
                         key, "lite" if lite else "full", outcome, degraded, ms)
                if getattr(self.settings, "warm_observability", True) \
                        and self.tracer:
                    self.tracer.span(f"warm:{key}", metadata={
                        "shape": "lite" if lite else "full",
                        "state": state_name,
                        "fired_at": fired,
                        "ms": ms,
                        "outcome": outcome,
                        "degraded": degraded,
                        "error": error,
                    })
            except Exception:
                pass
        return _cb

    def warm_speak_cache(self, state_name: str,
                         history: list[dict] | None = None,
                         dvs: dict | None = None) -> None:
        """iter114d (owner: 4 main states only — Intake/Discovery/Closer/
        Offer; heavy states stay unwarmed). Fire-and-forget SPEAK-ARM
        prefix warm: the diet-view bytes on the RESOLVED speak twin,
        minimal output, discarded. Gates: pipeline+diet ON, prompt_prewarm
        ON, a dedicated speak twin resolves (model != openai_model), and
        the state is listed in speak_model_states (legacy "" = no warm —
        pinned intended no-op). Latched `speak:<state>` in _prewarmed;
        task rides _warm_tasks (aclose cancels); NEVER registered in
        _latest_warm (that is _await_warm's full-warm domain, iter113).
        No force re-fire: dvs only moves the speak DELTA (trailing,
        post-history) — the [head][history] prefix is dvs-invariant."""
        if not (self.settings.speak_first_pipeline
                and self.settings.speak_diet):
            return
        if not self.settings.prompt_prewarm:
            return
        model = self._speak_model_for(state_name)
        if not model or model == self.settings.openai_model:
            return
        raw = str(getattr(self.settings, "speak_model_states", "") or "")
        states = {s.strip() for s in raw.split(",") if s.strip()}
        if not states or state_name not in states:
            return
        key = f"speak:{state_name}"
        if key in self._prewarmed:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._prewarmed.add(key)
        fired = time.time()
        task = asyncio.get_running_loop().create_task(
            self._speak_warm(state_name, history or [], dvs, model))
        self._warm_tasks.add(task)
        task.add_done_callback(
            self._make_speak_warm_done_cb(key, state_name, fired))

    async def _speak_warm(self, state_name: str, history: list[dict],
                          dvs: dict | None, model: str) -> None:
        """Byte-mirrors the HOT speak construction (state_node: _head_for
        -> _speak_delta -> _speak_view) — NOT _build_messages (the speak
        payload is [head][speak-view history][delta tail]). Tail/card
        divergence vs the eventual hot round is ACCEPTABLE — the cache
        reads the shared [head][history] prefix only. Failures logged and
        swallowed (a failed warm = today's cold round, never surfaces)."""
        try:
            dvs = DynamicVariables.from_flat(dvs or {}).to_flat()
            store = await self._resolve_kb_store()
            system, _ = self._head_for(state_name, store)
            delta = self._speak_delta(state_name, dvs, history, "")
            messages = self._speak_view(system, history, delta)
            twin = self.llm.speak_llm(model)
            bound = twin.bind(
                max_completion_tokens=self.settings.prewarm_max_completion_tokens)
            await bound.ainvoke(messages)
        except Exception as exc:
            log.warning("speak prewarm failed for %s: %s", state_name, exc)

    def _make_speak_warm_done_cb(self, key: str, state_name: str,
                                 fired: float):
        """Dedicated done-callback: _make_warm_done_cb hardcodes
        shape=full|lite; E10 greps shape=speak."""
        def _cb(task: asyncio.Task) -> None:
            try:
                ms = round((time.time() - fired) * 1000)
                outcome = ("cancelled" if task.cancelled()
                           else "failed" if task.exception() is not None
                           else "ok")
                log.info("warm done key=%s shape=speak outcome=%s ms=%s",
                         key, outcome, ms)
                if getattr(self.settings, "warm_observability", True) \
                        and self.tracer:
                    self.tracer.span(f"warm:{key}", metadata={
                        "shape": "speak", "state": state_name,
                        "fired_at": fired, "ms": ms, "outcome": outcome})
            except Exception:
                pass
        return _cb

    async def _warm(self, state_name: str, history: list[dict],
                    dvs: dict | None = None, lite: bool = False) -> None:
        """Warm request — BYTE-IDENTICAL to the state_node path (iter44 T1:
        composed through the SAME _build_messages as the hot path — same
        static head, same full tool array, same hysteretic history window,
        same STATE-BLOCK tail and RAG section)."""
        try:
            # iter46 T4b: normalize the dvs through the SAME schema path as
            # ingest's frozen STATE-BLOCK (to_flat field order) — a raw
            # dvs+patch merge appends NEW keys at the END, diverging from the
            # entry round's tail bytes and capping the transition cache at
            # tools+head (the audit's tax #4).
            dvs = DynamicVariables.from_flat(dvs or {}).to_flat()
            if lite:
                # iter46 T3: turn-1 hot payload = [lite head][greeting history
                # message][user msg] — the warm covers the byte-prefix BEFORE
                # the user message exists (it fires during the greeting).
                # iter48b: binds LITE_NOOP_TOOL — warm shape == hot shape, and
                # gpt-5.4 only caches tool-bearing requests (FIND-5).
                messages = [{"role": "system", "content": self._lite_head()}] + \
                    self._history_window(history, self.settings.history_window,
                                         self.settings.history_trim_step)
                await self.llm.warm(messages, [LITE_NOOP_TOOL])
                return
            store = await self._resolve_kb_store()
            system, expand_kb = self._head_for(state_name, store)
            tools = self._tools_cache.get((state_name, expand_kb))
            if tools is None:
                tools = build_tool_schemas(
                    self.states[state_name],
                    self.llm_json.get("general_tools", []),
                    lambda text: self.subst.subst(text, {}, kb=expand_kb),
                    {},
                )
                self._tools_cache[(state_name, expand_kb)] = tools
            tail = ""
            # iter56 (C2.5): under state_in_delta the hot path's pre-history
            # prefix carries NO state-block (it rides the post-history delta,
            # which never rides cache) — the warm prefills EXACTLY
            # [tools][head][history], the new append-only prefix. Warming a
            # state-block here would warm the WRONG shape and the latch
            # would poison the round. The staged-RAG branch (revert mode
            # only) stays unchanged behind the flag.
            if self.settings.prewarm_byte_exact \
                    and not getattr(self.settings, "state_in_delta", False):
                tail = self._state_block(dvs)
                # iter49 T4: under rag_live_retrieve the hot path's pre-history
                # prefix carries NO chunks (fresh chunks render post-history)
                # — the staged-RAG tail would make warm bytes != entry bytes
                # at EVERY transition (cold re-prefill). The warm now prefills
                # EXACTLY [tools][head][state-block][history].
                if not getattr(self.settings, "rag_live_retrieve", False) \
                        and store is not None and self.kb_slugs_for(state_name):
                    # staged chunks (this prefetch or a pending transition one)
                    # make the warm tail byte-equal to the next state entry's.
                    staged = await self._await_staging(state_name)
                    if staged is None:
                        staged = await self._stage_rag(state_name, dvs or {})
                    if staged is None:
                        staged = self._rag_staging.get(state_name)
                    tail += ragmod.render_knowledge_section(
                        staged["chunks"] if staged else [])
            messages = self._build_messages(state_name, history, tail, system)
            await self.llm.warm(messages, tools)
        except Exception as exc:
            # iter48 Commit B (M13): warm failures are no longer swallowed at
            # info level — a full-shape warm that fails gets ONE lite-shape
            # retry ([lite head][history], no tools/tail) so the prefix still
            # warms partially; a lite retry that also fails stays silent.
            log.warning("prewarm failed for %s: %s — one lite-shape retry",
                        state_name, exc)
            # iter55 T0 (F-08): record the failure for the warm done-callback
            # — a full-fail→lite-retry is reported degraded in the span.
            self._warm_diag[f"lite:{state_name}" if lite else state_name] = {
                "error": str(exc), "retried_lite": not lite}
            try:
                lite_messages = [{"role": "system",
                                  "content": self._lite_head()}] + \
                    self._history_window(history, self.settings.history_window,
                                         self.settings.history_trim_step)
                await self.llm.warm(lite_messages, [LITE_NOOP_TOOL])
            except Exception:
                pass

    async def _await_warm(self, state_name: str,
                          wait_ms: int | None = None) -> None:
        """iter46 T4c: state-entry decision on the in-flight warm for this
        state. iter113 (owner 2026-10-13: "we should cancel, and await
        nothing… it either landed or didn't"): the await NEVER waits in-path
        — a DONE warm is adopted (already_done), a PENDING warm is cancelled
        IMMEDIATELY (aborted, waited_ms=0). Waiting gains nothing: default-
        tier warms run ~1-2.3 s, so the old iter95 cap (400 ms) almost never
        caught a landing — it was pure dead time in the S→L window plus a
        forfeited deposit on default tier (probe4). The iter95/46 knobs
        (`wait_ms`, `prewarm_abort_cap_ms`) stay in signatures/env for
        continuity but are NO-OPs for the await decision. Outcomes:
        no_task / aborted / already_done (`landed`/`timeout`/`task_error`
        retire — nothing can land or fail mid-await without a wait).
        Historical: iter48 Commit B (M10) `wait_ms` override; iter55 T0
        outcome+waited_ms logging/spanning; iter95 cancel-not-wait cap
        (superseded here)."""
        if wait_ms is None:
            wait_ms = getattr(self.settings, "prewarm_entry_wait_ms", 0)
        if wait_ms <= 0:
            return
        task = self._latest_warm.get(state_name)
        waited = 0
        if task is None:                            # F-09: warm never fired
            outcome = "no_task"
        elif task.cancelled():                      # iter95: previously
            outcome = "aborted"                     # aborted — nothing to adopt
        elif task.done():
            outcome = "already_done"
        else:
            # iter113: adopt-if-done else cancel-instantly. No timed wait,
            # no guarded task, no cap — a pending warm is cancelled NOW
            # (waited_ms=0 always); its prefix-cache deposit is forfeit
            # (accepted: the cross-round prefix cache covers the bulk).
            task.cancel()
            outcome = "aborted"
        # iter103 PT-8 (observability-only): surface the waited ms on the
        # runtime scratch so the round's pre-LLM breakdown can carry it.
        self._last_warm_wait = waited
        log.info("await_warm key=%s outcome=%s waited_ms=%s cap_ms=%s",
                 state_name, outcome, waited, 0)
        if getattr(self.settings, "warm_observability", True) and self.tracer:
            self.tracer.span("await_warm", metadata={
                "key": state_name, "outcome": outcome,
                "waited_ms": waited, "cap_ms": 0})

    # ------------------------------------------------------------------ #
    # iter112 speak-first staggered pipeline helpers.
    def _fire_split_tools(self, messages: list[dict], tools: list[dict],
                          hist_snapshot: list[dict], dvs_snapshot: dict,
                          state_name: str, turn_index: int) -> asyncio.Task:
        """Fire the background TOOLS stream of a speak-first round.

        Same messages as the foreground SPEAK call; token events DISCARDED
        (the tools stream is the only tool source); the iter46 T4a
        transition prewarm re-fires from ITS `tool_call_start` (once) so
        the destination warm banks during the speak/gap. Returns the task;
        also stored on `self._pending_tools` for M1/M2/M3."""
        runtime = self
        t_dispatch = time.perf_counter()

        async def _consume() -> dict:
            _prewarm_fired = False
            _tool_calls: list[dict] = []
            _usage: dict | None = None
            _first_event: float | None = None
            async for tev in runtime.llm.astream(
                    messages, tools,
                    verbosity=runtime._verbosity_for(state_name)):
                if _first_event is None:
                    # First SDK event of the tools stream — informational
                    # only; the TURN clock never stamps from this stream.
                    _first_event = time.perf_counter()
                if tev["type"] == "tool_call_start":
                    name = tev.get("name") or ""
                    dest = name[len("transition_to_"):] \
                        if name.startswith("transition_to_") else ""
                    if dest in runtime.states and not _prewarm_fired:
                        _prewarm_fired = True
                        runtime.warm_prompt_cache(dest, list(hist_snapshot),
                                                  dvs=dict(dvs_snapshot))
                        runtime.warm_speak_cache(dest, list(hist_snapshot),
                                                 dvs=dict(dvs_snapshot))
                elif tev["type"] == "final":
                    _tool_calls = tev["tool_calls"]
                    _usage = tev.get("usage")
            itd = (_usage or {}).get("input_token_details") or {}
            log.info("split stream usage: stream=tools turn=%s state=%s "
                     "input=%s cache_read=%s output=%s ttft_event_ms=%s "
                     "tool_calls=%s",
                     turn_index, state_name,
                     (_usage or {}).get("input_tokens"),
                     itd.get("cache_read", 0) or itd.get("priority_cache_read", 0) or 0,
                     (_usage or {}).get("output_tokens"),
                     round((_first_event - t_dispatch) * 1000)
                     if _first_event is not None else None,
                     [tc["name"] for tc in _tool_calls])
            return {"tool_calls": _tool_calls, "usage": _usage}

        task = asyncio.get_running_loop().create_task(_consume())
        if self._pending_tools is not None and not self._pending_tools.task.done():
            # Defensive: per the round/turn contract at most ONE heavy round
            # defers per turn (a deferred round executes no tools -> the
            # turn finalizes). A live overwrite would orphan a task.
            log.warning("split pending overwrite: cancelling unmerged task "
                        "state=%s", self._pending_tools.state_name)
            self._pending_tools.task.cancel()
        self._pending_tools = _PendingTools(task=task, state_name=state_name,
                                            turn_index=turn_index,
                                            dispatched_at=t_dispatch)
        return task

    async def _execute_deferred_tools(self, tool_calls: list[dict],
                                      dvs: dict, state_name: str,
                                      state: GraphState):
        """M2 execution: run the deferred TOOLS round's calls at the next
        turn's ingest — EXACTLY the executor contract the round loop uses
        (minus the in-round filler/defer machinery: the caller's answer has
        already arrived; there is no turn left to defer into). Returns
        (new_history_msgs, dvs_patch, new_state, ended)."""
        new_msgs: list[dict] = []
        dvs_patch: dict[str, Any] = {}
        new_state: str | None = None
        ended = False
        taken: set[str] = set()
        tool_calls = [tc for tc in tool_calls
                      if not (tc["name"] in taken or taken.add(tc["name"]))]
        if not tool_calls:
            return new_msgs, dvs_patch, new_state, ended
        new_msgs.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": tc["id"], "type": "function",
                 "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                for tc in tool_calls
            ],
        })
        for tc in tool_calls:
            try:
                args = json.loads(tc["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            try:
                outcome = await self.executor.execute(
                    tc["name"], args, dvs, state_name,
                    spoken_text="", last_spoken=state.get("last_spoken", ""))
            except Exception as exc:      # pragma: no cover - defensive
                log.warning("split M2 execute failed name=%s: %s", tc["name"], exc)
                continue
            dvs_patch.update(outcome.dvs_patch)
            dvs.update(outcome.dvs_patch)
            if outcome.new_state:
                new_state = outcome.new_state
            ended = ended or outcome.ended
            new_msgs.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": json.dumps(outcome.response)[:4000],
            })
        return new_msgs, dvs_patch, new_state, ended

    def _split_close_pending(self, reason: str) -> None:
        """M3: no orphan TOOLS tasks at close — cancel a still-pending
        future (its results were never merged) and log the finding."""
        pend = self._pending_tools
        if pend is None:
            return
        self._pending_tools = None
        if not pend.task.done():
            pend.task.cancel()
            log.info("split M3: %s — pending tools task cancelled unmerged "
                     "state=%s age_ms=%s", reason, pend.state_name,
                     round((time.perf_counter() - pend.dispatched_at) * 1000))

    # ------------------------------------------------------------------ #
    # iter40 L2: slots prefetch — bg-warm the availability payload.
    # FIRES on ConfirmSlots entry + on an ok transition INTO ConfirmSlots.
    # WARM CALL = byte-identical to the model's first slots call (frozen OTEL
    # t40 evidence: {"account_id", "timezone", "slot_target_date",
    # "current_reservation_uids": ""} — freeze mode, holds frozen). The plan's
    # no-date pin was corrected 2026-09-09: the LIVE endpoint REQUIRES
    # slot_target_date (no-date → no_start_time; proven by signed probe), so
    # the warm covers the two days the ConfirmSlots flow ever offers —
    # today + tomorrow. Serve only on exact arg-match (same date, empty
    # current_reservation_uids, no preferred_time) — any other call goes live.
    # Warm holds linger until Cal.com auto-expiry when not served (noted in
    # 02_latency_fixes.md). Fresh (same tz+date, ok, < ttl) → no-op; in-flight
    # warm → no-op; never sets engine_fired.
    def warm_slots_async(self, dvs: dict) -> None:
        if not self.settings.slots_prefetch:
            return
        tz = str(dvs.get("prospect_timezone") or "").strip()
        if not tz:
            return
        today = self._slots_warm_today(dvs, tz)
        if today is None:
            return
        ttl = float(self.settings.slots_prefetch_ttl_s)
        now = time.monotonic()
        wanted = [today.isoformat(), (today + timedelta(days=1)).isoformat()]
        pending = False
        for date in wanted:
            warm = self._slots_warm.get(date) or {}
            if (warm.get("tz") == tz
                    and isinstance(warm.get("payload"), dict)
                    and warm["payload"].get("ok")
                    and (now - warm.get("ts", 0.0)) < ttl):
                continue
            task = self._slots_warm.get(date, {}).get("task")
            if task is not None and not task.done():
                pending = True
                continue
            try:
                self._slots_warm[date] = {
                    "task": asyncio.get_running_loop().create_task(
                        self._warm_slots(tz, date))}
            except RuntimeError:
                return
        self._slots_warm_task = None   # tasks now live per-date in the store

    def _slots_warm_today(self, dvs: dict, tz: str):
        """Today in the prospect's tz: dvs today_date first, zoneinfo fallback."""
        raw = str(dvs.get("today_date") or "").strip()
        if raw:
            try:
                return datetime.strptime(raw[:10], "%Y-%m-%d").date()
            except ValueError:
                pass
        try:
            return datetime.now(ZoneInfo(tz)).date()
        except Exception:
            return None

    async def _warm_slots(self, tz: str, date: str) -> None:
        tool = self.executor._tool_def("query_livecall_slots")
        if not tool or not tool.get("url"):
            return
        payload = {"args": {"account_id": "diallux_live", "timezone": tz,
                            "slot_target_date": date,
                            "current_reservation_uids": ""}}  # freeze-mode (model parity)
        raw = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json",
                   "X-Retell-Signature": self.executor._sign(raw, tool["url"])}
        t0 = time.perf_counter()
        try:
            resp = await self.executor.http.post(tool["url"], content=raw, headers=headers)
            try:
                body = resp.json()
            except ValueError:
                body = {}
        except Exception as exc:
            body = {"ok": False, "error": "prefetch_failed", "message": str(exc)[:200]}
        # mutate IN PLACE — the executor reads this same dict object
        self._slots_warm[date] = {"payload": body, "ts": time.monotonic(), "tz": tz}
        if self.tracer:
            self.tracer.span("slots_prefetch", output={
                "ok": bool(isinstance(body, dict) and body.get("ok")),
                "slots": len((body or {}).get("slots") or []),
                "ms": round((time.perf_counter() - t0) * 1000, 1), "tz": tz,
                "date": date})

    async def warm_rag(self, state_name: str | None = None, dvs: dict | None = None):
        """iter21: warm the RAG path BEFORE turn 1 (embedding client + pgvector
        pool). Fired by CallSession.start() during the greeting — kills the
        ~1.7s cold retrieval that otherwise lands on the caller's first turn.
        iter44: the chunks are STAGED (not discarded) so the byte-exact
        prewarm and the first state entry reuse the same retrieval.
        iter49 T4: under rag_live_retrieve nothing consumes staging — the
        cold-start value left is the EMBEDDER session (arctic ONNX ~833ms)
        + the pgvector pool; warm those, skip the dead retrieval."""
        try:
            state_name = state_name or self.llm_json.get("starting_state", "Intake")
            if getattr(self.settings, "rag_live_retrieve", False):
                store = await self._resolve_kb_store()
                if store is not None:
                    embed_q = getattr(store, "embed_query", None)
                    if embed_q is not None:
                        await embed_q("warm")
                return
            task = self._spawn_stage_rag(state_name, dvs or {})
            if task is not None:
                await task       # registry-registered: the prewarm's
                                 # _await_staging reuses THIS retrieval
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    async def _resolve_kb_store(self):
        """Lazily attach the process-wide KBStore (pgvector) once.
        iter44 T2: the resolve latch is set only AFTER a successful attach —
        one transient failure must never latch None (inline-heavy mode) for
        the whole session; the next caller retries."""
        if self._kb_store_resolved:
            return self._kb_store
        try:
            store = await ragmod.get_kb_store(self.settings)
        except Exception as exc:
            log.warning("kb store resolve failed (will retry): %s", exc)
            return None
        self._kb_store_resolved = True
        self._kb_store = store
        return store

    # ------------------------------------------------------------------ #
    # iter44 T4: expansion decision — a REAL store means retrieval (markers
    # stripped). No store: inline expansion ONLY when the operator asked for
    # it (fallback_inline_kb=True, or the explicit rag_mode="inline" V1
    # mode). The accidental pgvector-outage fallback to the ~16k blob is dead.
    def _expand_kb(self, kb_store) -> bool:
        if kb_store is not None:
            return False
        return self.settings.fallback_inline_kb or \
            getattr(self.settings, "rag_mode", "auto") == "inline"

    def _head_for(self, state_name: str, kb_store) -> tuple[str, bool]:
        """System head + expand flag shared by the hot path and the warm path
        (byte-identical bytes for the same (state, store) decision).
        iter49 T4 rag_keep_markers: True keeps the ##slug-kb## references in
        the cached head (A/B whether the model grounds better with them);
        default False = stripped, iter48 exact."""
        expand_kb = self._expand_kb(kb_store)
        system = self.static_head(state_name, expand_kb)
        # iter49c T4 fix: the old `or not expand_kb` made rag_keep_markers
        # INERT whenever a store is present (store => expand False => always
        # strip — the A/B could never keep markers). Strip when a store is
        # present and the flag is off; with no store + inline the markers
        # were already substituted; with no store + no expansion strip so
        # literal markers never leak to the model.
        strip = (kb_store is not None
                 and not getattr(self.settings, "rag_keep_markers", False)) \
            or (kb_store is None and not expand_kb)
        if strip:
            system = ragmod.strip_kb_markers(system)
        return system, expand_kb

    # ------------------------------------------------------------------ #
    def _entry_lite_off_states(self) -> set[str]:
        """iter49 T6: per-state opt-out parsed from state_entry_lite_off
        (comma list) — states that must tool-call on entry."""
        return {s.strip() for s in
                str(getattr(self.settings, "state_entry_lite_off", "") or "").split(",")
                if s.strip()}

    def _entry_lite_head(self, state_name: str, kb_store) -> str:
        """iter49 T6: the state-entry ack head — the state's kb=False head.
        Strip decision mirrors _head_for so the bytes are IDENTICAL to the
        heavy round's head when a store is present (markers kept only under
        rag_keep_markers + store); with no store the ack stays light (kb
        forced False — never the inline blob, even in the degraded mode)."""
        system = self.static_head(state_name, expand_kb=False)
        keep = bool(getattr(self.settings, "rag_keep_markers", False))
        if not (keep and kb_store is not None):
            system = ragmod.strip_kb_markers(system)
        return system

    # ------------------------------------------------------------------ #
    # iter44 T1: the ONE composer for both the hot path and the warm path —
    # identical bytes for identical (system, tail, history) inputs.
    # iter44 T3 layout (tail_before_history): [head][tail][history] so the
    # cached prefix (tools + head + tail) grows round-to-round; the history
    # window (16/8 hysteretic) bounds the ceiling (~5k tokens). False =
    # legacy [head][history][tail].
    # iter56: with state_in_delta the hot path passes tail="" (no pre-history
    # state-block) and the delta carries state → RAG → directive; the
    # history window defaults 0 = FULL append-only (cache climbs with it).
    def _build_messages(self, state_name: str, history: list[dict],
                        tail: str, system: str, delta: str = "") -> list[dict]:
        hist = self._history_window(history, self.settings.history_window,
                                    self.settings.history_trim_step)
        messages = [{"role": "system", "content": system}]
        has_tail = bool(tail.strip())
        if has_tail and self.settings.tail_before_history:
            messages.append({"role": "system", "content": tail})
        messages.extend(hist)
        if has_tail and not self.settings.tail_before_history:
            messages.append({"role": "system", "content": tail})
        # iter44 append-only: drift-refetched NEW knowledge goes AFTER the
        # history as its own system block — the [head][tail][history] prefix
        # stays byte-stable, so the cached prefix keeps climbing with history
        # while the delta (small) re-bills per round. Never mutate mid-prompt.
        if delta.strip():
            messages.append({"role": "system", "content": delta})
        return messages

    # ------------------------------------------------------------------ #
    # iter44 T6b: transition RAG prefetch. An ok transition_to_X fires a bg
    # retrieval for X's scope; the chunks land in _rag_staging and the
    # state_node rag lane consumes them at state entry (~0 ms critical path).
    # Prefetch failure degrades to today's sync fetch (never raises).
    def warm_rag_async(self, state_name: str, dvs: dict | None = None) -> None:
        """Fire-and-forget RAG staging for `state_name`. iter44: NO per-state
        latch — every ok transition into a state (re)stages its scope, so a
        revisited state's entry is prefetched too. The in-flight task dedupe
        in _spawn_stage_rag prevents duplicate concurrent embeds."""
        if not self.settings.rag_prefetch_on_transition:
            return
        # iter49 T4: transition prefetch is part of the iter48 diagnosis
        # (stale caller text staged ahead) and dead work under live-retrieve
        # (every round retrieves fresh) — inert, like the drift lane.
        if getattr(self.settings, "rag_live_retrieve", False):
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._spawn_stage_rag(state_name, dvs or {})

    def _spawn_stage_rag(self, state_name: str, dvs: dict):
        """One in-flight staging task per state (a pending task is reused,
        not duplicated). Returns the task, or None outside a loop."""
        pending = self._rag_staging_tasks.get(state_name)
        if pending is not None and not pending.done():
            return pending
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return None
        task = asyncio.get_running_loop().create_task(
            self._stage_rag(state_name, dvs))
        self._rag_staging_tasks[state_name] = task
        self._warm_tasks.add(task)      # cancelled in aclose()
        return task

    async def _await_staging(self, state_name: str) -> dict | None:
        """Await a pending staging task for `state_name`, if any."""
        task = self._rag_staging_tasks.get(state_name)
        if task is None or task.done():
            return self._rag_staging.get(state_name)
        try:
            return await task
        except Exception:
            return None

    async def _stage_rag(self, state_name: str, dvs: dict,
                         user_msg: str = "") -> dict | None:
        """Retrieve `state_name`'s scope and stage the chunks. Returns the
        staged dict, or None on any failure (caller falls back to the sync
        fetch — never worse than today)."""
        store = await self._resolve_kb_store()
        if store is None:
            return None
        scope = self.kb_slugs_for(state_name)
        if not scope:
            return None
        query, _dv_vals = self.refer_query(state_name, dvs, user_msg)
        if not query.strip():
            # iter48 M2: no anchor at all — never embed the empty string;
            # return None so the caller falls back to the sync fetch.
            return None
        vec = None
        embed_q = getattr(store, "embed_query", None)
        if embed_q is not None:
            try:
                vec = await embed_q(query)
            except Exception:
                vec = None
        try:
            # real stores reuse the staged vec; fakes without embed_query
            # still stage (retrieve does its own embed).
            kwargs = {"vec": vec} if vec is not None else {}
            chunks = await store.retrieve(query, scope, **kwargs)
        except Exception as exc:
            log.warning("rag prefetch failed for %s (%s); sync fallback", state_name, exc)
            return None
        staged = {"query": query, "chunks": chunks, "vec": vec}
        self._rag_staging.setdefault(state_name, staged)
        return staged

    # ------------------------------------------------------------------ #
    # iter46 T2: async drift check. Serialized per state (one task handle,
    # cancel-supersede on a newer caller message — H2b's race guard); the
    # `_drift_failed_at` backoff applies inside the task. Never raises.
    def _spawn_drift_check(self, state_name: str, dvs: dict, user_msg: str,
                           scope: list[str], kb_store) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        pending = self._drift_tasks.get(state_name)
        if pending is not None and not pending.done():
            pending.cancel()      # superseded by the newer query
        task = asyncio.get_running_loop().create_task(
            self._drift_check(state_name, dvs, user_msg, scope, kb_store))
        self._drift_tasks[state_name] = task

    async def _drift_check(self, state_name: str, dvs: dict, user_msg: str,
                           scope: list[str], kb_store) -> None:
        """BG drift lane: embed base (lazily) + round query, cosine, on drift
        re-retrieve and append NEW chunks to `_rag_delta_chunks` (rendered
        next round via rag_append_delta)."""
        try:
            embed_q = getattr(kb_store, "embed_query", None)
            if embed_q is None or (time.monotonic() - self._drift_failed_at < 60):
                return
            query, _dv_vals = self.refer_query(state_name, dvs, user_msg)
            base = self._frozen_query_vec
            t0 = time.perf_counter()
            if base is None and self._frozen_query_text:
                base = await embed_q(self._frozen_query_text)
                self._frozen_query_vec = base
            round_vec = await embed_q(query)
            # iter48 M3 (R3): base-None is NO LONGER a dead end — an empty
            # freeze has NO valid baseline, so the round FORCES a re-retrieve
            # and adopts the round vector as the new baseline (the rescue
            # happens in the existing drift path below).
            force_drift = base is None
            cos = 0.0 if force_drift else _cosine(round_vec, base)
            rag_ms = 0.0
            new_chunks: list = []
            if cos >= self.settings.rag_drift_threshold and not force_drift:
                chunks = self._frozen_chunks
            else:
                try:
                    chunks = await kb_store.retrieve(query, scope, vec=round_vec)
                except Exception:
                    return
                self._frozen_query_vec = round_vec
                self._frozen_query_text = query
                if self.settings.rag_append_delta:
                    seen = {(c.get("kb"), c.get("content")) for c in self._frozen_chunks} \
                        | {(c.get("kb"), c.get("content")) for c in self._rag_delta_chunks}
                    self._rag_delta_chunks += [
                        c for c in chunks
                        if (c.get("kb"), c.get("content")) not in seen]
                    chunks = self._frozen_chunks
                else:
                    self._frozen_chunks = chunks
                self._kb_stats["rag_turns"] += 1
                self._kb_stats["rag_chars"] += sum(len(c["content"]) for c in chunks)
            rag_ms = round((time.perf_counter() - t0) * 1000, 1)
            if self.tracer:
                self.tracer.span("rag:drift", output={
                    "state": state_name, "cosine": round(cos, 4), "ms": rag_ms,
                    "async": True, "chunks": len(chunks),
                    "force_drift": force_drift,
                    "delta_chunks": len(self._rag_delta_chunks)
                        if self.settings.rag_append_delta else 0,
                    "query": query[:200]})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._drift_failed_at = time.monotonic()
            log.warning("async drift check failed for %s (%s)", state_name, exc)

    # iter25 retrieval gate: mechanical states pull nothing (iter24: 431 wasted
    # sales-psychology pulls in Closing etc.); Closing allow-lists call-closing;
    # sales states keep general+state scope as today.
    _RETRIEVAL_ALLOW: dict[str, list[str]] = {"Closing": ["call-closing"]}
    # iter59 T4 (owner): KB EVERYWHERE — every state retrieves all call long
    # (Retell-parity: retrieval on EVERY response from the global union).
    # The four mechanical states were blind since iter25; freed states
    # retrieve via lane B over the 9-KB general union (kb_slugs_for fallback).
    # The attribute STAYS (referenced at the round gate / tests) — empty set
    # keeps the gate code alive and the revert is a one-line restore.
    _RETRIEVAL_OFF: set[str] = set()

    # iter49 T4: REAL per-state KB attachment. The iter48 union (general ∪
    # state markers) gave Intake/Discovery/Closer/Offer the SAME 9-10-KB scope
    # — per-state attachment was effectively fake (FIND-9). This map is the
    # lane-B scope and the parity target ("same KBs visible" per state).
    # Pinned from the iter49 plan T4; validated by the pre-flight replay
    # (research/surgeon/iter49p-sharp-rag/02_replay_report.md pack #9).
    _STATE_KBS: dict[str, list[str]] = {
        "Intake": ["pain-points", "call-context", "sales-language",
                   "voice-ai-capabilities", "are-you-ai", "sales-psychology",
                   "industry", "hipaa", "call-closing"],
        "Discovery": ["pain-points", "call-context", "sales-language",
                      "voice-ai-capabilities", "are-you-ai", "sales-psychology",
                      "industry", "hipaa", "call-closing", "discovery-bridge"],
        "Closer": ["sales-psychology", "industry", "sales-language",
                   "call-context", "pain-points"],
        "Offer": ["sales-psychology", "sales-language", "industry",
                  "voice-ai-capabilities"],
        "Closing": ["call-closing", "sales-language"],
    }

    def kb_slugs_for(self, state_name: str) -> list[str]:
        """iter49 T4: per-state KB attachment (_STATE_KBS). _RETRIEVAL_OFF
        keeps the mechanical-states gate (zero pulls); unmapped states keep
        the iter48 general∪state union (revert-safe fallback)."""
        if state_name in self._RETRIEVAL_OFF:
            return []
        if state_name in self._STATE_KBS:
            return list(self._STATE_KBS[state_name])
        if state_name in self._RETRIEVAL_ALLOW:
            return list(self._RETRIEVAL_ALLOW[state_name])
        general = Path(self.settings.prompts_dir) / "general_prompt.md"
        general_text = general.read_text(encoding="utf-8") if general.exists() \
            else self.llm_json.get("general_prompt", "")
        slugs = ragmod.kb_slugs_in(general_text)
        state_text = self.prompts.get(state_name, "")
        slugs += [s for s in ragmod.kb_slugs_in(state_text) if s not in slugs]
        for t in ragmod.parse_refer_tags(state_text):     # iter25-b: tag slugs join the scope
            if t["kb"] not in slugs:
                slugs.append(t["kb"])
        return slugs

    def refer_query(self, state_name: str, dvs: dict, user_msg: str) -> tuple[str, list[str]]:
        """iter48: truth-anchored retrieval query.

        iter25-b built "[state: X] whats: dv-values user msg" — the state tag
        and the empty-dv join polluted the embedding (the are-you-ai beat the
        pitch for "what do you guys do") and the empty-user branch returned a
        content-free placeholder, freezing every prefetch 0-chunk (iter40 C3).
        iter48 R1: the query is the refer whats (KB anchors, KEPT) + the
        non-empty dv values + the caller text — NO state tag, NO label join,
        NO empty-dv join. Empty user_msg = state-anchor mode (prefetch):
        whats + dv values only. Returns (query, values_used).
        """
        tags = ragmod.parse_refer_tags(self.prompts.get(state_name, ""))
        parts: list[str] = []
        values: list[str] = []
        if tags:
            parts.append("; ".join(t["what"] for t in tags))
            for t in tags:
                for dv in t["dvs"]:
                    v = str(dvs.get(dv) or "").strip()
                    if v:
                        values.append(v)
            if values:
                parts.append(" / ".join(values))
        if user_msg:
            parts.append(user_msg)
        query = " ".join(p.strip() for p in parts if p.strip())
        if not query:
            # iter48 M2 contract: no anchor at all (no tags AND no user msg)
            # → empty query; _stage_rag guards on this (caller falls back to
            # the sync fetch, never worse).
            return "", []
        return query, values

    # ------------------------------------------------------------------ #
    # iter49 T4: live multi-lane retrieval (rag_live_retrieve=True).
    def build_lanes(self, state_name: str, dvs: dict,
                    user_msg: str, pinned_tag: str | None = None
                    ) -> tuple[list[dict], list[set]]:
        """The lane plan for this round. Lane A (engine intent) = one lane per
        refer-tag: query = the tag's `what` + its non-empty dv values (leak
        values $-formatted with units), scope = [tag.kb] ONLY — the tag's KB
        can no longer be crowded out. Closing has zero tags → the hand-written
        anchor instead (booked calls recap). Lane B (user intent) = the
        caller utterance (>= rag_min_query_chars) + the industry anchor when
        extracted (right-vertical 0.589->0.788, beats Retell's 0.637), scoped
        the state's _STATE_KBS. <=3 lanes per side. Returns (lanes,
        owned_kbs_per_lane) — owned KBs are quota-exempt in the merge.

        iter52: `pinned_tag` (default None = byte-exact iter49) removes
        `industry` from the scopes — the pinned KB leaves the vector lanes
        and re-enters via render_pinned_section. The deep-dive scope trigger
        (normalized industry dv contains a trigger keyword, e.g. 'plumb')
        appends the extra KB (e.g. `plumbing`) to the lane-B scope — INDEPENDENT
        of the pin, gated by rag_pin_industry."""
        lanes: list[dict] = []
        owned: list[set] = []
        scope = self.kb_slugs_for(state_name)
        if not scope:
            return lanes, owned
        if pinned_tag:
            scope = [s for s in scope if s != "industry"]
        if getattr(self.settings, "rag_pin_industry", False):
            # iter64: pre-pin gate — the shared `industry` KB leaves the
            # scopes until the vertical is KNOWN (pin resolved or the
            # industry dv extracted); generic pre-pin turns cross-matched
            # every vertical's pain/ROI prose (iter63 battery: Susan t3/t4,
            # Marcus t3/t5, Maria t4, Danny t6). The pin block re-introduces
            # the vertical the moment it resolves.
            if not pinned_tag \
                    and not str((dvs or {}).get("industry") or "").strip():
                scope = [s for s in scope if s != "industry"]
            industry_norm = str((dvs or {}).get("industry") or "").strip().lower()
            if industry_norm:
                for kw, extra_kbs in _INDUSTRY_KB_TRIGGERS.items():
                    if kw in industry_norm:
                        for kb in extra_kbs:
                            if kb not in scope:
                                scope.append(kb)
        # ---- lane A: engine intent (refer-tags; Closing anchor) ----------
        if not self.settings.rag_multiquery:
            # middle mode: ONE combined query (iter48's refer_query shape),
            # scoped to the state — live-per-turn without the lane split.
            q, _vals = self.refer_query(state_name, dvs, user_msg)
            if q.strip():
                lanes.append({"query": q, "scope": scope})
                owned.append(set())
        elif state_name == "Closing":
            booked = bool(dvs.get("booking_verified")
                          or dvs.get("booking_confirmed"))
            lanes.append({
                "query": _CLOSING_ANCHOR_BOOKED if booked else _CLOSING_ANCHOR,
                "scope": scope})
            owned.append({"call-closing", "sales-language"})
        else:
            tags = ragmod.parse_refer_tags(self.prompts.get(state_name, ""))[:_MAX_LANES_A]
            for t in tags:
                vals = [_fmt_leak_value(dv, str(dvs.get(dv) or "").strip())
                        for dv in t["dvs"]
                        if str(dvs.get(dv) or "").strip()]
                q = t["what"] if not vals else f"{t['what']}: {', '.join(vals)}"
                lanes.append({"query": q, "scope": [t["kb"]]})
                owned.append({t["kb"]})
        # ---- lane B: user intent (the caller utterance) ------------------
        msg = (user_msg or "").strip()
        if len(msg) >= int(getattr(self.settings, "rag_min_query_chars", 0)):
            industry = str(dvs.get("industry") or "").strip()
            q = f"{msg} (their industry: {industry})" if industry else msg
            lanes.append({"query": q, "scope": scope})
            owned.append(set())       # lane B owns nothing: quota applies
        return lanes[:_MAX_LANES_A + _MAX_LANES_B], owned

    async def _retrieve_raw(self, state_name: str, dvs: dict, user_msg: str,
                            kb_store, lane: str = "full"
                            ) -> tuple[list, list, float, list[str], str, dict]:
        """iter59 T3: the retrieval pieces WITHOUT the merge/render — so the
        hybrid consume can merge lane A's landed set and lane B's awaited set
        through the SAME merge_lane_chunks (top_k=3, 1600 chars, per-KB quota
        — lane A candidates keep their owned-KB exemption).

        lane:
          "full"  — build_lanes as today (all lanes, utterance included)
          "laneA" — engine intent ONLY: build_lanes with an EMPTY utterance
                    (refer-tag lanes + dv values; utterance-independent).
                    Hybrid lane A fires at the first flux Update.
          "laneB" — the caller utterance as a SINGLE query (one embed),
                    scoped the state's KB slugs; no owned KBs (quota applies).
                    Hybrid lane B fires at EOT/EagerEOT.

        Returns (lane_results, owned, ms, lane_queries, pinned_tag, pin).
        The pinned industry vertical is ensured FIRST (unchanged iter52)."""
        t0 = time.perf_counter()
        await self._ensure_pinned_industry(dvs, kb_store)
        pin = self._pinned_industry or {}
        pinned_tag = str(pin.get("tag") or "")
        if pinned_tag and not pin.get("chunks"):
            pinned_tag = ""     # resolve hit, fetch empty -> degrade to the vector lane
        if lane == "laneB":
            msg = (user_msg or "").strip()
            lanes: list[dict] = []
            owned: list[set] = []
            if len(msg) >= int(getattr(self.settings, "rag_min_query_chars", 0)):
                industry = str(dvs.get("industry") or "").strip()
                q = f"{msg} (their industry: {industry})" if industry else msg
                scope = [s for s in self.kb_slugs_for(state_name)
                         if not (pinned_tag and s == "industry")]
                # iter64: pre-pin gate — same rule as build_lanes; pinned
                # and dv-known rounds are byte-identical to iter63.
                if getattr(self.settings, "rag_pin_industry", False) \
                        and not pinned_tag and not industry:
                    scope = [s for s in scope if s != "industry"]
                lanes.append({"query": q, "scope": scope})
                owned.append(set())       # lane B owns nothing: quota applies
        elif lane == "laneA":
            lanes, owned = self.build_lanes(state_name, dvs, "",
                                            pinned_tag=pinned_tag or None)
            # build_lanes with an empty utterance keeps ONLY lane A (the
            # utterance branch is gated by rag_min_query_chars) — exactly
            # the engine-intent half of hybrid.
        else:
            lanes, owned = self.build_lanes(state_name, dvs, user_msg,
                                            pinned_tag=pinned_tag or None)
        lane_qs = [l["query"] for l in lanes]
        # iter103 PT-5: byte-identical query-set memo — the Oct-8 canned shape
        # (same lanes, same queries, round after round) fired the same embed
        # 25x with the same top-3. A hit reuses the stored raw pieces and
        # skips the embed+search entirely; a miss fires and stores. The key
        # normalizes whitespace + case ONLY (byte-identical semantics, no
        # cosine, no fuzzy — owner-ratified). A hit does NOT fake a seq: the
        # consumer's freshness bookkeeping stays exactly as today.
        memo_key = ""
        if lanes:
            memo_key = f"{state_name}|{lane}|" + " || ".join(
                " ".join(str(q or "").split()).lower() for q in lane_qs)
            hit = self._rag_query_memo.get(memo_key)
            if hit is not None:
                self._last_retrieve_memo = True
                return hit
            self._last_retrieve_memo = False
        lane_results = await kb_store.retrieve_lanes(lanes) if lanes else []
        ms = round((time.perf_counter() - t0) * 1000, 1)
        result = (lane_results, owned, ms, lane_qs, pinned_tag, pin)
        if lanes:
            if len(self._rag_query_memo) > 128:   # crude cap, _rag_cache style
                self._rag_query_memo.clear()
            self._rag_query_memo[memo_key] = result
        return result

    def _render_from_raw(self, lane_results: list, owned: list, merged: list,
                         ms: float, lane_qs: list[str], pinned_tag: str,
                         pin: dict) -> tuple[str, list[str]]:
        """iter59 T3: merge/render shared by every consume path (single
        merge_lane_chunks + render_knowledge_section + optional pin block).
        iter60 AUD-10: _kb_stats increments live in the CALLERS (_live_retrieve
        and _consume_hybrid) — a shared counter here double-counted hybrid
        (bg-task land + consume each called this)."""
        knowledge = ragmod.render_knowledge_section(merged)
        if pinned_tag:
            pin_delta = ragmod.render_pinned_section(
                pin.get("chunks") or [], pinned_tag,
                cap=int(getattr(self.settings, "rag_pin_char_budget", 1800)))
            delta = f"{pin_delta}\n\n{knowledge}" if pin_delta else knowledge
            kbs = sorted({str(c.get("kb", "")) for c in merged} | {"industry"})
        else:
            delta = knowledge
            kbs = sorted({str(c.get("kb", "")) for c in merged})
        return delta, kbs

    async def _live_retrieve(self, state_name: str, dvs: dict, user_msg: str,
                             kb_store, lane: str = "full"
                             ) -> tuple[str, list[str], float, list, list[str]]:
        """Fresh multi-lane retrieval for THIS round: build_lanes → ONE
        batched embed (all lane texts in ONE call, off-hot-path embedder) +
        CONCURRENT pgvector → merge with the per-KB quota + chunk-id dedupe.
        Returns (delta_str, kbs, ms, merged_chunks, lane_queries).

        iter52: the pinned industry vertical is ensured FIRST (once per dv
        value, inside this bg task — off the hot path); when pinned, industry
        leaves the lane scopes and the pin renders as its OWN block (own
        budget, whole-chunks) PREPENDED to the per-turn KNOWLEDGE."""
        t0 = time.perf_counter()
        lane_results, owned, _ms, lane_qs, pinned_tag, pin = \
            await self._retrieve_raw(state_name, dvs, user_msg, kb_store, lane=lane)
        merged = ragmod.merge_lane_chunks(
            lane_results, owned,
            top_k=int(self.settings.rag_top_k),
            char_budget=int(self.settings.rag_char_budget))
        ms = round((time.perf_counter() - t0) * 1000, 1)
        # iter60 AUD-10: count here (bg task land / round-sync inline) —
        # preserves iter58 counting semantics for eot/speech-window/round-sync.
        self._kb_stats["rag_turns"] += 1
        self._kb_stats["rag_ms_total"] += ms
        self._kb_stats["rag_chars"] += sum(len(str(c.get("content", ""))) for c in merged)
        delta, kbs = self._render_from_raw(lane_results, owned, merged, ms,
                                           lane_qs, pinned_tag, pin)
        return (delta, kbs, ms, merged, lane_qs)

    async def _resolve_industry_tag(self, dv: str, kb_store) -> str | None:
        """iter52 T3: the 5-step resolver ladder — normalize → stoplist →
        alias map (single tag; ≥2 distinct hits = ambiguous) → embed fallback
        (ONE store.retrieve over ['industry'], top-1 >= rag_filter_score,
        header skipped) → None (caller keeps today's vector lane). Never
        guesses, never blocks."""
        norm = " ".join(str(dv or "").split()).lower()
        if norm in _PIN_STOPLIST:
            return None
        hits = {tag for alias, tag in _PIN_ALIASES
                if re.search(rf"\b{re.escape(alias)}", norm)}
        if len(hits) == 1:
            return next(iter(hits))
        # ambiguous (>=2 distinct tags) or no alias hit -> embed fallback
        filter_score = float(getattr(self.settings, "rag_filter_score", 0.24))
        try:
            chunks = await kb_store.retrieve(norm or dv, ["industry"])
        except Exception:
            return None
        for c in chunks:                     # best-first (retrieve order)
            score = c.get("score")
            if score is None or float(score) < filter_score:
                break
            vertical = await kb_store.vertical_of(c.get("id"))
            if vertical and vertical != "header":
                return vertical
        return None

    async def _ensure_pinned_industry(self, dvs: dict, kb_store) -> None:
        """iter52 T5: resolve + fetch the pinned vertical ONCE per call (per
        dv value), lazily, inside the bg _live_retrieve task. The result is
        cached on the dv VALUE — re-resolve only when it changes; a resolved
        miss (no vertical / fetch failure) is cached too so a pinless call
        never re-embeds every round. Kill switch off -> no-op (byte-exact
        iter49). Never raises."""
        if not getattr(self.settings, "rag_pin_industry", False):
            return
        dv = str((dvs or {}).get("industry") or "").strip()
        if not dv:
            return
        pin = self._pinned_industry
        if pin and pin.get("dv") == dv:
            return                    # cached (hit or resolved miss)
        try:
            t0 = time.perf_counter()
            tag = await self._resolve_industry_tag(dv, kb_store)
            chunks = await kb_store.pinned_by_tag(tag) if tag else []
            self._pinned_industry = {"dv": dv, "tag": tag or "",
                                     "chunks": chunks}
            if self.tracer:
                self.tracer.span("rag:pin", output={
                    "dv": dv[:80], "tag": tag or "",
                    "chunks": len(chunks),
                    "ms": round((time.perf_counter() - t0) * 1000, 1),
                    "resolved": bool(tag and chunks)})
        except Exception as exc:
            log.warning("pin industry ensure failed: %s", exc)
            self._pinned_industry = {"dv": dv, "tag": "", "chunks": []}

    # ------------------------------------------------------------------ #
    # iter49 T3: async off the hot path. The eager EOT fires spawn_live_
    # retrieve the moment the lane queries exist (the batched arctic embed,
    # ~90-190ms multi-lane, runs inside the caller's remaining speech + the
    # speculative LLM start); the round's live branch bounded-awaits the
    # in-flight task (rag_live_await_ms) — ready: THIS turn's fresh set
    # (awaited = pgvector + merge only); not ready: the PREVIOUS turn's
    # fresh set for this state (first turn of a call: empty) — never a
    # stall; the shielded task lands for the next round.
    def spawn_live_retrieve(self, state_name: str, dvs: dict,
                            user_msg: str, lane: str = "full") -> None:
        """Fire-and-forget live retrieval for (state, dvs, user_msg). NEVER
        raises. An in-flight task for the same state+lane is superseded
        (cancelled) by the newer query, like _spawn_drift_check. No-op when
        rag_live_retrieve is off (the iter48 path never consumes these).

        iter59 T3: tasks are keyed "<state>|<lane>" and every fire bumps a
        per-state SEQUENCE number (_live_seq). The consumer matches seq (not
        msg text — fixes the same-text-rerun edge): a task from an older seq
        can never be consumed as fresh."""
        try:
            if not getattr(self.settings, "rag_live_retrieve", False):
                return
            asyncio.get_running_loop()
        except RuntimeError:
            return
        try:
            key = f"{state_name}|{lane}"
            pending = self._live_tasks.get(key)
            if pending is not None and not pending.done():
                pending.cancel()      # superseded by the newer query
            seq = self._live_seq.get(state_name, 0) + 1
            self._live_seq[state_name] = seq
            task = asyncio.get_running_loop().create_task(
                self._live_task(state_name, dict(dvs or {}), user_msg or "",
                                lane=lane, seq=seq))
            self._live_tasks[key] = task
            self._live_msgs[key] = user_msg or ""
            self._live_task_seq[key] = seq
        except Exception as exc:
            log.warning("spawn_live_retrieve(%s, %s) failed: %s",
                        state_name, lane, exc)

    async def _live_task(self, state_name: str, dvs: dict,
                         user_msg: str, lane: str = "full",
                         seq: int = 0) -> dict | None:
        """The bg body: resolve the store, run the retrieval, stash the
        result into _live_prev[state_name] and RETURN it. Exceptions are
        logged, never raised — the round degrades to the previous set.

        iter59 T3: LANE tasks (laneA/laneB) additionally stash the RAW
        piece sets (lane_results / owned / pinned pieces) so the hybrid
        consume can re-merge both lanes through ONE merge_lane_chunks."""
        try:
            store = await self._resolve_kb_store()
            if store is None:
                return None
            if lane in ("laneA", "laneB"):
                lane_results, owned, ms, lane_qs, pinned_tag, pin = \
                    await self._retrieve_raw(state_name, dvs, user_msg, store,
                                             lane=lane)
                merged = ragmod.merge_lane_chunks(
                    lane_results, owned,
                    top_k=int(self.settings.rag_top_k),
                    char_budget=int(self.settings.rag_char_budget))
                delta, kbs = self._render_from_raw(
                    lane_results, owned, merged, ms, lane_qs,
                    pinned_tag, pin)
                result = {"delta": delta, "kbs": kbs, "chunks": merged,
                          "lane_qs": lane_qs, "ms": ms, "seq": seq,
                          "lane": lane, "lane_results": lane_results,
                          "owned": owned, "pinned_tag": pinned_tag, "pin": pin}
            else:
                delta, kbs, ms, merged, lane_qs = await self._live_retrieve(
                    state_name, dvs, user_msg, store, lane=lane)
                result = {"delta": delta, "kbs": kbs, "chunks": merged,
                          "lane_qs": lane_qs, "ms": ms, "seq": seq,
                          "lane": lane}
            self._live_prev[state_name] = result
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("live retrieve task for %s failed: %s",
                        state_name, exc)
            return None

    async def _consume_live_retrieve(self, state_name: str, dvs: dict,
                                     user_msg: str, kb_store):
        """The round-path consumer, per rag_fire_mode (iter59 T3):

        "eot" (default) — iter58 surface EXACTLY: task keyed "<state>|full",
            msg-text match, bounded await, degrade to _live_prev.
        "speech-window" — the task was fired at the FIRST flux Update (the
            EOT/EagerEOT fire was SKIPPED so the landed task survives).
            Freshness matches by SEQUENCE (not msg text — the fire ran on a
            PARTIAL utterance): a fire not yet consumed = fresh for this
            round; multi-round same-turn reuse allowed when the msg matches.
            Timeout fallback degrades to _live_consumed_res (the last
            CONSUMED fresh set — never _live_prev's unconsumed overwrite).
        "round-sync" — no task at all: ONE inline retrieval on the round
            path (span label live-sync). Degraded 0% by construction.
        "hybrid" — lane A (fired at first Update, refer-tags + dvs) + lane B
            (fired at EOT/EagerEOT, single utterance query). Lane A's landed
            pieces and lane B's awaited pieces merge through the SAME
            merge_lane_chunks (lane A keeps its owned-KB exemption).
            Degraded = NEITHER lane landed fresh.

        NEVER raises into the node; only a true node cancellation propagates.
        Returns (delta, kbs, ms, chunks, lane_qs, degraded, await_ms)."""
        t_wait = time.perf_counter()
        msg = user_msg or ""
        mode = getattr(self.settings, "rag_fire_mode", "eot")

        if mode == "round-sync":
            self._live_mode_label = "live-sync"
            store = kb_store
            if store is None:
                store = await self._resolve_kb_store()
            if store is None:
                return ("", [], 0.0, [], [], True,
                        round((time.perf_counter() - t_wait) * 1000, 1))
            delta, kbs, ms, merged, lane_qs = await self._live_retrieve(
                state_name, dvs, msg, store, lane="full")
            await_ms = round((time.perf_counter() - t_wait) * 1000, 1)
            return (delta, kbs, ms, merged, lane_qs, False, await_ms)

        if mode == "hybrid":
            return await self._consume_hybrid(state_name, dvs, msg, kb_store,
                                              t_wait)

        # ---- "eot" (iter58 exact) and "speech-window" share the task path
        key = f"{state_name}|full"
        task = self._live_tasks.get(key)
        if mode == "eot":
            stale = task is None or task.cancelled() \
                or self._live_msgs.get(key) != msg
        else:
            # speech-window: fresh = a fire seq not yet consumed for this
            # state (the EOT rerun of an identical text spawns a NEW seq,
            # so cross-turn staleness is impossible); an already-consumed
            # task may still be REUSED when its msg matches (multi-round).
            tseq = self._live_task_seq.get(key, -1)
            cseq = self._live_consumed.get(state_name, -1)
            fresh = tseq > cseq
            stale = task is None or task.cancelled() or (
                not fresh and self._live_msgs.get(key) != msg)
        if stale:
            self.spawn_live_retrieve(state_name, dvs, msg, lane="full")
            task = self._live_tasks.get(key)
        res = None
        if task is not None and not task.cancelled() and task.done():
            res = task.result() or None          # landed (fresh or failure)
        elif task is not None and not task.cancelled():
            cap_ms = int(getattr(self.settings, "rag_live_await_ms", 60))
            if cap_ms > 0:
                try:
                    await asyncio.wait_for(asyncio.shield(task), cap_ms / 1000)
                except asyncio.CancelledError:
                    if not task.cancelled():
                        raise    # the NODE itself was cancelled — propagate
                    # the inner task was cancelled (superseded) — degrade
                except Exception:
                    pass        # timeout or task failure — degrade below
            if task.done() and not task.cancelled():
                res = task.result() or None
        await_ms = round((time.perf_counter() - t_wait) * 1000, 1)
        if res:
            self._live_consumed[state_name] = self._live_task_seq.get(key, 0)
            self._live_consumed_res[state_name] = res   # last consumed fresh set (C4)
            # iter62: same empty-set rule — 0 chunks is never "healthy".
            return (res["delta"], res["kbs"], res["ms"], res["chunks"],
                    res["lane_qs"], not res["chunks"], await_ms)
        # degrade: the last CONSUMED fresh set for this state (C4) — a fire
        # that never landed must NOT overwrite the degrade source
        # (_live_prev is written by the bg task at completion time, consumed
        # or not). First turn of a call: empty.
        fallback = self._live_consumed_res.get(state_name) \
            if mode != "eot" else None
        if fallback is None:
            fallback = self._live_prev.get(state_name) or {}
        return (fallback.get("delta", ""), fallback.get("kbs", []),
                fallback.get("ms", 0.0), fallback.get("chunks", []),
                fallback.get("lane_qs", []), True, await_ms)

    async def _consume_hybrid(self, state_name: str, dvs: dict, msg: str,
                              kb_store, t_wait: float):
        """iter59 T3: hybrid consume — lane A (landed during speech: refer-tag
        queries + dv values, utterance-independent) + lane B (fired at
        EOT/EagerEOT: the caller utterance as a SINGLE query, awaited within
        the cap). Both lanes' RAW piece sets merge through the SAME
        merge_lane_chunks (lane A candidates keep their owned-KB exemption;
        the pin block re-renders once). Degraded = NEITHER lane landed fresh
        this round; fallback = the last consumed fresh set (C4), else the
        previous merged set."""
        self._live_mode_label = "hybrid"
        key_a, key_b = f"{state_name}|laneA", f"{state_name}|laneB"
        # iter60 AUD-3: rounds 2+ of the SAME turn reuse the round-1 merged
        # set (the seq floor rightly rejects stale laneA, but it also
        # rejected THIS turn's laneA — knowledge silently shrank to
        # laneB-only on every tool round). Stash validity = same msg AND
        # the laneB task unchanged (a new fire bumps _live_task_seq).
        stash = self._live_merged_turn.get(state_name)
        if stash and stash.get("msg") == msg \
                and self._live_task_seq.get(key_b, -1) == stash.get("b_seq"):
            r = stash["res"]
            # iter62: same empty-set honesty on same-turn reuse rounds.
            return (r["delta"], r["kbs"], r["ms"], r["chunks"],
                    r["lane_qs"], not r["chunks"], 0.0)
        task_a = self._live_tasks.get(key_a)
        task_b = self._live_tasks.get(key_b)
        # lane A: use only if it LANDED (never await — it had the whole
        # speech window) and its fire seq was not already consumed.
        raw_a = None
        if task_a is not None and not task_a.cancelled() and task_a.done():
            ra = task_a.result() or None
            if ra and ra.get("seq", 0) > self._live_consumed.get(state_name, -1):
                raw_a = ra
        # lane B: bounded await on the in-flight single-query embed.
        # iter62: NO respawn — the fire lives in the session layer
        # (Update/EagerEOT/EOT). Missing, cancelled, or fired for a
        # DIFFERENT utterance (iter60 AUD-4 mismatch) = CLEAN FAIL this
        # round; the next fire supersedes (owner: no late-restart injection).
        res_b = None
        if task_b is None or task_b.cancelled() \
                or self._live_msgs.get(key_b) != msg:
            task_b = None
        if task_b is not None and not task_b.cancelled():
            if not task_b.done():
                cap_ms = int(getattr(self.settings, "rag_live_await_ms", 60))
                if cap_ms > 0:
                    try:
                        await asyncio.wait_for(asyncio.shield(task_b),
                                               cap_ms / 1000)
                    except asyncio.CancelledError:
                        if not task_b.cancelled():
                            raise   # the NODE itself was cancelled — propagate
                    except Exception:
                        pass        # timeout or failure — degrade below
            if task_b.done() and not task_b.cancelled():
                res_b = task_b.result() or None
        await_ms = round((time.perf_counter() - t_wait) * 1000, 1)
        # merge the RAW piece sets that landed
        lane_results: list = []
        owned: list = []
        lane_qs: list[str] = []
        ms = 0.0
        pinned_tag, pin = "", {}
        landed = False
        for r in (raw_a, res_b):
            if not r:
                continue
            lr = r.get("lane_results")
            if lr is None:            # a merged (full-shape) result — skip
                continue
            lane_results.extend(lr)
            owned.extend(r.get("owned") or [])
            lane_qs.extend(r.get("lane_qs") or [])
            ms = max(ms, float(r.get("ms") or 0.0))
            if not pinned_tag and r.get("pinned_tag"):
                pinned_tag = r["pinned_tag"]
                pin = r.get("pin") or {}
            landed = True
        if not landed:
            # iter62 — owner decision: a late/failed retrieval is a CLEAN
            # FAIL; stale-prev injection feeds the model chunks that make
            # no sense at the wrong moment. Next fire supersedes.
            return ("", [], 0.0, [], [], True, await_ms)
        merged = ragmod.merge_lane_chunks(
            lane_results, owned,
            top_k=int(self.settings.rag_top_k),
            char_budget=int(self.settings.rag_char_budget))
        delta, kbs = self._render_from_raw(lane_results, owned, merged, ms,
                                           lane_qs, pinned_tag, pin)
        # iter60 AUD-10: hybrid counts ONCE per consumed round (the bg land
        # path no longer increments inside _render_from_raw).
        self._kb_stats["rag_turns"] += 1
        self._kb_stats["rag_ms_total"] += ms
        self._kb_stats["rag_chars"] += sum(len(str(c.get("content", ""))) for c in merged)
        # record consumption: lane A's seq becomes the consumed floor
        if raw_a:
            self._live_consumed[state_name] = max(
                self._live_consumed.get(state_name, -1), raw_a.get("seq", 0))
        fresh = {"delta": delta, "kbs": kbs, "chunks": merged,
                 "lane_qs": lane_qs, "ms": ms}
        self._live_consumed_res[state_name] = fresh   # C4 fallback source
        # iter60 AUD-3: stash the merged set for same-turn rounds (rounds 2+
        # short-circuit above; a NEW turn bumps b_seq or changes msg → stale).
        self._live_merged_turn[state_name] = {
            "msg": msg, "b_seq": self._live_task_seq.get(key_b, -1),
            "res": fresh}
        # iter62: chunks=0 is an HONEST degraded round (was hard-coded False;
        # 38/67 spans hid behind that on the iter61 call).
        return (delta, kbs, ms, merged, lane_qs, not merged, await_ms)

    # ------------------------------------------------------------------ #
    def _load_prompts(self) -> dict[str, str]:
        """V2: prompts from diallux/prompts (source of truth, editable).
        Falls back to llm.json's embedded state_prompt when a file is absent."""
        prompts_dir = Path(self.settings.prompts_dir)
        out: dict[str, str] = {}
        for name, cfg in self.states.items():
            p = prompts_dir / f"{name}.md"
            out[name] = p.read_text(encoding="utf-8") if p.exists() else cfg.get("state_prompt", "")
        return out

    # ------------------------------------------------------------------ #
    def static_head(self, state_name: str, expand_kb: bool = True) -> str:
        """iter31 C3: byte-stable system head — general_prompt + state prompt
        with dvs NOT substituted ({{var}} left literal via dvs={}), cached per
        (state, expand_kb). Identical bytes across rounds AND calls, so OpenAI's
        automatic prompt cache holds the whole prefix. VOICE_OUTPUT_RULES fold
        in here (static bytes belong in the cached head, not behind the tail).
        iter56: under head_strip_vars the literal {{var}} tokens are rewritten
        to the stable anchor "CURRENT CALL STATE" AT BUILD TIME — still cached
        per (state, expand_kb), still byte-stable across rounds AND calls (no
        dvs involved). The anchor points at the delta block that ALWAYS carries
        the live values (state_in_delta) — kills the {{dv}} blindness (PT-53)."""
        key = (state_name, expand_kb)
        head = self._head_cache.get(key)
        if head is None:
            general = Path(self.settings.prompts_dir) / "general_prompt.md"
            general_text = general.read_text(encoding="utf-8") if general.exists() \
                else self.llm_json["general_prompt"]
            head = self.subst.subst(general_text + "\n\n" + self.prompts.get(state_name, ""),
                                    {}, kb=expand_kb)
            if self.settings.tts_voice_rules:
                head += VOICE_OUTPUT_RULES
            # iter56 (C2.1): head strip — dead {{var}} anchors become a stable
            # pointer to the live-values delta. Applied at head-BUILD time so
            # the result stays byte-stable (cached per key, no dvs involved).
            if getattr(self.settings, "head_strip_vars", False):
                head = _VAR_TOKEN_RE.sub("CURRENT CALL STATE", head)
            self._head_cache[key] = head
        return head

    def _lite_head(self) -> str:
        """iter43 first-turn-lite: turn-1 system = general_prompt +
        VOICE_OUTPUT_RULES only (no state prompt, no RAG) — same read path as
        static_head, no KB markers."""
        general = Path(self.settings.prompts_dir) / "general_prompt.md"
        general_text = general.read_text(encoding="utf-8") if general.exists() \
            else self.llm_json.get("general_prompt", "")
        # iter43 FIX: kb=False — do NOT inline the KBs the general prompt
        # references. kb=True ballooned the "light" turn-1 head to ~16.5k
        # tokens (every ##slug-kb## inline-expanded) — the opposite of light.
        head = self.subst.subst(general_text, {}, kb=False)
        if self.settings.tts_voice_rules:
            head += VOICE_OUTPUT_RULES
        # iter56: the strip is a head-BUILD pass — the lite head strips too
        # (same dead-{{var}} blindness on the turn-1 general prompt).
        if getattr(self.settings, "head_strip_vars", False):
            head = _VAR_TOKEN_RE.sub("CURRENT CALL STATE", head)
        return head

    def _state_block(self, dvs: dict) -> str:
        """iter31 C3: dynamic tail — every non-empty dv as `name: value`. The
        model reads live values HERE; the tail changing never invalidates the
        cached head. Schema field order (to_flat) keeps line order stable."""
        if not self.settings.state_block:
            return ""
        lines = [f"{k}: {v}" for k, v in dvs.items()
                 if v is not None and str(v).strip()]
        if not lines:
            return ""
        # iter32: recency co-emit directive — this block is the LAST message the
        # model reads; head-position rules failed 5 batteries at 60-69% silent.
        return ("\n\n# CURRENT CALL STATE (live values)\n"
                "SPEAK FIRST: every response starts with your spoken sentence, "
                "THEN fires its tools — one response, never tool calls without "
                "speech.\n" + "\n".join(lines))

    # ------------------------------------------------------------------ #
    # iter114 speech authorization — WIT card / speak view / derive.
    _WIT_TASKS = {
        "Intake": "learn why they called and the missed-call problem",
        "Discovery": "understand the pain; capture their numbers when volunteered",
        "Closer": "quantify the revenue leak with their own numbers",
        "Offer": "offer the live walkthrough",
        "contact_details": "collect name, business, callback number, timezone",
        "ConfirmSlots": "agree on a slot and confirm it",
        "VerifyLead": "verify the details",
        "Booking": "book the agreed slot",
        "Closing": "wrap up warmly",
    }
    _WIT_AVAIL_LINE = ("the time they asked for is no longer available — "
                       "offer the open times above, verbatim")

    def _wit_card(self, state_name: str, dvs: dict,
                  history: list[dict]) -> str:
        """iter114 T3: the engine-authored render target for the mouth —
        every fact it may speak plus the status flags its claims are gated
        on. Facts only; no prose, no KB text."""
        if not getattr(self.settings, "speak_card", False):
            return ""
        g = lambda k: str(dvs.get(k) or "").strip()  # noqa: E731
        lines = ["# WHAT IS TRUE RIGHT NOW (speak only from this card)"]
        who = ", ".join(x for x in (g("first_name"), g("company_name")) if x)
        if who:
            lines.append(f"- caller: {who}")
        if g("callback_number"):
            lines.append(f"- callback number: {g('callback_number')}")
        if g("prospect_timezone"):
            lines.append(f"- timezone: {g('prospect_timezone')}")
        if g("industry"):
            lines.append(f"- industry: {g('industry')}")
        own: list[str] = []
        if g("missed_calls_weekly"):
            own.append(f"{g('missed_calls_weekly')} missed calls/week")
        if g("missed_calls_daily"):
            own.append(f"{g('missed_calls_daily')} missed calls/day")
        if g("call_volume"):
            own.append(f"volume: {g('call_volume')}")
        if g("close_rate_pct"):
            own.append(f"{g('close_rate_pct')}% would book")
        if g("avg_job_value"):
            own.append(f"avg ticket ${g('avg_job_value')}")
        if own:
            lines.append("- their numbers: " + " · ".join(own))
        if g("weekly_leak") or g("monthly_leak"):
            lines.append("- leak (computed by the engine): weekly "
                         f"${g('weekly_leak')} · monthly ${g('monthly_leak')}")
        else:
            lines.append("- leak: NOT COMPUTED YET — never estimate or "
                         "calculate it yourself")
        lines.append(f"- slots offered: {g('requested_slot') or 'none yet'}")
        if g("selected_time"):
            status = "verified" if dvs.get("slot_verified") \
                else "pending verification"
            lines.append(f"- caller's chosen time: {g('selected_time')} "
                         f"({status})")
        else:
            lines.append("- caller's chosen time: none yet")
        lines.append("- booking: CONFIRMED"
                     if g("booking_uid") else "- booking: NOT CREATED yet")
        tool_text = "\n".join(str(m.get("content") or "")
                              for m in (history or [])[-6:]
                              if m.get("role") == "tool")
        if "preferred_unavailable" in tool_text:
            lines.append(f"- sanctioned line: {self._WIT_AVAIL_LINE}")
        # iter114 v1.1 (E6 live evidence): the LATEST tool outcome — errors/
        # instructions the mouth must be able to render ("No free slots in
        # that window. Offer the next available day."). Tool text never
        # reaches the speak view; without this the mouth re-asks blindly.
        last_note = ""
        for m in reversed((history or [])[-10:]):
            if m.get("role") == "tool" and (m.get("content") or "").strip():
                try:
                    d = json.loads(str(m["content"]))
                except Exception:
                    break
                if isinstance(d, dict):
                    note = str(d.get("message") or d.get("error") or "")
                    if note:
                        last_note = note
                break
        if last_note:
            lines.append(f"- last tool result: {last_note[:160]}")
        task = self._WIT_TASKS.get(state_name)
        if task:
            lines.append(f"- current task: {task}")
        return "\n".join(lines)

    def _speak_delta(self, state_name: str, dvs: dict, history: list[dict],
                     notes_delta: str) -> str:
        """iter114 T4: the speak view's trailing delta — WIT card + ENGINE
        notes only (no RAG, no full state block — the mouth renders facts)."""
        parts: list[str] = []
        card = self._wit_card(state_name, dvs, history)
        if card:
            parts.append(card)
        if notes_delta:
            parts.append(notes_delta)
        return "\n\n".join(parts)

    def _speak_view(self, system: str, history: list[dict],
                    delta: str) -> list[dict]:
        """iter114 T4 (speak diet): the mouth's payload — system head +
        conversation (user messages; assistant TEXT with tool_calls frames
        stripped) + the card delta. Raw tool JSON never reaches the mouth —
        it can no longer argue with machine verdicts (the E4c-t36 class)."""
        hist = self._history_window(history, self.settings.history_window,
                                    self.settings.history_trim_step)
        messages = [{"role": "system", "content": system}]
        for m in hist:
            role = m.get("role")
            if role == "user":
                messages.append({"role": "user",
                                 "content": m.get("content") or ""})
            elif role == "assistant" and (m.get("content") or "").strip():
                messages.append({"role": "assistant",
                                 "content": m.get("content")})
        if delta.strip():
            messages.append({"role": "system", "content": delta})
        return messages

    def _speak_auth_sets(self, dvs: dict,
                         history: list[dict]) -> tuple[str, set, set]:
        """iter114 T5: authorized-number fact set for the render guard.
        Sources: dvs values + the caller's own words + tool results (last 40
        messages). NEVER the static prompt, RAG chunks, or assistant history —
        fabrications/echoes must not re-authorize themselves."""
        texts: list[str] = [str(v) for v in dvs.values()
                            if v is not None and str(v).strip()]
        tool_parts: list[str] = []
        for m in (history or [])[-40:]:
            role = m.get("role")
            if role in ("user", "tool"):
                c = m.get("content")
                if c:
                    texts.append(str(c))
                    if role == "tool":
                        tool_parts.append(str(c))
        toks: set[str] = set()
        nums: set[str] = set()
        for t in texts:
            toks |= _spk_tokens(t)
            toks |= _spk_word_times(t)
            nums |= _spk_auth_numbers(t)
            nums |= _spk_word_amounts(t)
            nums |= _spk_word_digits(t)      # iter114d FIND-114C
        return "\n".join(tool_parts), toks, nums

    def _derive(self, dvs: dict,
                span_name: str = "derive:leak_math") -> tuple[dict, bool]:
        """iter114 T1: pure deterministic derivation on a dvs snapshot —
        runs on EVERY write path (M2 ingest, in-round tools, deterministic
        node) so the fact always exists BEFORE the next payload build.
        No LLM, no I/O, sub-ms."""
        patch: dict[str, Any] = {}
        fired = False
        _norm = bool(getattr(self.settings, "leak_normalize", True))
        if leak_inputs_present(dvs, normalize=_norm) and not dvs.get("weekly_leak"):
            result, leak_patch = calculate_monthly_leak(dvs, normalize=_norm)
            if result.get("ok"):
                patch.update(leak_patch)
                fired = True
                if self.tracer:
                    self.tracer.span(span_name, output=result)
        return patch, fired

    # ------------------------------------------------------------------ #
    @staticmethod
    def _history_window(history: list[dict], n: int, step: int = 8) -> list[dict]:
        """iter43: hysteretic trim — the window START only advances every `step`
        entries past n, so the cached prefix stays byte-stable between jumps.
        len 17..24 (n=16, step=8): start pinned at 8 (window 9..16 entries,
        append-only); len 25: start jumps to 16. One re-prefill per jump
        instead of per turn. NEVER starting on an orphan tool message (an
        OpenAI 400: a tool response must follow its assistant tool_calls
        message). step=1 restores the old sliding-window behavior."""
        if n <= 0 or len(history) <= n:
            return list(history)
        start = step * ((len(history) - n + step - 1) // step)
        tail = history[start:] if start < len(history) else []
        while tail and tail[0].get("role") == "tool":
            tail = tail[1:]
        return tail

    # ------------------------------------------------------------------ #
    # iter46 D5 (H1 fix-1): per-state verbosity — states in
    # verbosity_states_medium run "medium" (late-flow ask/capture states
    # where low stalls contact_details completion); everything else rides
    # the "low" default (the 700 ms TTFT recipe). Env-able list.
    def _verbosity_for(self, state_name: str) -> str:
        if not state_name:
            return ""
        medium = {s.strip() for s in
                  str(getattr(self.settings, "verbosity_states_medium", "")).split(",")
                  if s.strip()}
        return "medium" if state_name in medium else ""

    # ------------------------------------------------------------------ #
    # iter114c (owner design call 2026-10-13): per-state SPEAK model —
    # states listed in `speak_model_states` speak via `speak_model` (luna,
    # fast); every other state's speak stream rides `openai_model` (tool
    # accuracy). "" = legacy all-luna (every state speaks `speak_model`);
    # `speak_model` unset = "" here too (same model both streams, E1).
    def _speak_model_for(self, state_name: str) -> str:
        fast = getattr(self.settings, "speak_model", "") or ""
        if not fast:
            return ""
        if not state_name:
            return self.settings.openai_model
        raw = str(getattr(self.settings, "speak_model_states", "") or "")
        if not raw.strip():
            return fast                     # legacy all-luna (E7 mode)
        states = {s.strip() for s in raw.split(",") if s.strip()}
        return fast if state_name in states else self.settings.openai_model

    # ------------------------------------------------------------------ #
    def _make_state_node(self, state_name: str) -> Callable[[GraphState], dict]:
        runtime = self

        async def state_node(state: GraphState) -> dict:
            t_round_entry = time.perf_counter()   # iter103 PT-8 stopwatch origin
            writer = get_stream_writer()
            dvs = DynamicVariables.from_flat(state.get("dvs", {})).to_flat()
            history = state.get("history", [])

            # ---- V3 RAG: retrieval instead of full-KB inline expansion ----
            # iter31 C3b: STATIC head (dvs never substituted, cached bytes) is
            # messages[0]; the dynamic tail (STATE BLOCK + frozen RAG section)
            # is a TRAILING system message AFTER the history. The OpenAI prompt
            # cache holds tools + head + full append-only history; only the
            # small tail re-bills each round.
            # iter56: the tail is EMPTY under state_in_delta (default) — the
            # STATE BLOCK rides the post-history DELTA (fresh every round,
            # re-billed anyway) and the prefix [tools][head][history] is
            # truly append-only, so cache_read climbs with history. The
            # head's {{var}} tokens are stripped to the "CURRENT CALL STATE"
            # anchor (head_strip_vars) pointing at that delta.
            rag_ms = 0.0
            rag_kbs: list[str] = []
            # iter103 PT-8 (observability-only): per-round pre-LLM stopwatch
            # scratch — the live branch sets rag_await_ms; the warm scratch
            # resets here and is written by _await_warm below.
            rag_await_ms = 0.0
            runtime._last_warm_wait = 0
            # iter43 first-turn-lite: turn 1 = general_prompt + VOICE_OUTPUT_RULES
            # only — no state prompt, no tools, no RAG, no tail. Guard is == 1
            # EXACTLY (never <=1 — a default-0 state would go silent forever).
            # Default OFF; live enables via .env FIRST_TURN_LITE=true.
            lite = bool(runtime.settings.first_turn_lite) \
                and state.get("turn_index", 0) == 1
            # iter49 T6 (T4b): state-entry lite — the FIRST round in a state
            # whose name differs from the previous round's rendered state.
            # Covers the post-transition_to_X ack rounds AND the turn-2 entry
            # into the initial state (the turn-1 lite round renders no state
            # head, so it never counts as a visit). Turn 1 itself keeps the
            # EXISTING first_turn_lite path; per-state opt-out via
            # state_entry_lite_off. The ack can only SPEAK — extraction and
            # slots fire on the later heavy rounds.
            entry_lite = (
                not lite
                and bool(getattr(runtime.settings, "state_entry_lite", False))
                and state.get("turn_index", 0) > 1
                and state_name != runtime._prev_round_state
                and state_name not in runtime._entry_lite_off_states())
            kb_store = None if lite else await runtime._resolve_kb_store()
            if lite:
                system = runtime._lite_head()
                expand_kb = False
            elif entry_lite:
                # iter49 T6: [state head (kb=False)] — head bytes identical
                # to the heavy round's (same strip decision), never inline-
                # expanded. FULL tools (FIND-5 port — see the tools branch),
                # NO RAG lanes, NO warm await.
                system = runtime._entry_lite_head(state_name, kb_store)
                expand_kb = False
            else:
                system, expand_kb = runtime._head_for(state_name, kb_store)
                # iter44 T4: degraded (no store, inline guard OFF) — say so
                # ONCE per call instead of silently losing the KB lane.
                if kb_store is None and not expand_kb \
                        and not runtime._inline_guard_logged:
                    runtime._inline_guard_logged = True
                    log.warning("RAG fallback inline DISABLED "
                                "(fallback_inline_kb=False) — running without KB excerpts")
            # iter46 T4c: bounded await of an in-flight prewarm for this state
            # — a warm that finished in time rides cache_read at entry; on
            # timeout the warm keeps running (shield) for the next round.
            # iter48 Commit B (M11): EOT-boundary split — the FIRST round of
            # a turn (rounds_left == max_tool_rounds; turn 1 IS an EOT
            # boundary; sole writers: ingest sets max, deterministic decrements)
            # awaits with prewarm_entry_wait_eot_ms (500); mid-turn rounds keep
            # prewarm_entry_wait_ms (100). M12: the lite round awaits its own
            # `lite:<state>` warm with the EOT cap (turn-1 cache coverage).
            if runtime.settings.prewarm_entry_wait_eot_ms > 0 or \
                    runtime.settings.prewarm_entry_wait_ms > 0:
                eot = state.get("rounds_left", 0) == \
                    runtime.settings.max_tool_rounds
                if lite:
                    if eot and runtime.settings.prewarm_entry_wait_eot_ms > 0:
                        await runtime._await_warm(f"lite:{state_name}",
                                                  wait_ms=runtime.settings.prewarm_entry_wait_eot_ms)
                elif entry_lite:
                    # iter87 VERSION B hold law (supersedes the iter49 T6
                    # "NEVER awaits" rule): ordering, not timing — the ack's
                    # payload is NEVER sent before the destination warm has
                    # landed. We await a TASK, not a timer: success, error or
                    # HTTP timeout all complete it and release (shielded);
                    # 0-200 ms typical because the fire law scheduled the
                    # warm during think time / webhook idle. The cap is a
                    # fence, not the mechanism: a dead-provider day degrades
                    # to a cold round, never a frozen call. 0 = byte-exact
                    # iter85 (pass).
                    if runtime.settings.warm_await_dest_ms > 0:
                        await runtime._await_warm(
                            state_name,
                            wait_ms=runtime.settings.warm_await_dest_ms)
                        # iter87 (owner 2026-09-30): 10 ms settle after the
                        # warm lands — never race an incomplete prefix.
                        await asyncio.sleep(0.01)
                elif eot and runtime.settings.prewarm_entry_wait_eot_ms > 0:
                    await runtime._await_warm(
                        state_name, wait_ms=runtime.settings.prewarm_entry_wait_eot_ms)
                elif runtime.settings.prewarm_entry_wait_ms > 0:
                    await runtime._await_warm(state_name)
            # iter40 C3: the tail bytes were frozen at turn start (ingest) —
            # MODEL tool writes between rounds no longer mutate the prefix
            # (their results reach the model via the history tool messages).
            # EXCEPTION: a round following an engine-fired deterministic pass
            # (iter33 chain-abort surface, leak math) MUST re-render — the
            # engine's news has no other channel. Correctness > cache here.
            # iter56 (C2.2): under state_in_delta the tail is EMPTY in live
            # mode — the state-block rides the POST-HISTORY delta instead
            # (state_delta_block, joined with the RAG delta at final
            # assembly). The prefix [tools][head][history] stays append-only
            # and cache_read climbs with history. False = iter55 layout
            # (frozen/re-rendered tail pre-history — the revert path).
            state_delta_block = ""
            if lite:
                tail = ""
            elif runtime.settings.state_in_delta:
                tail = ""
                # fresh EVERY round — the delta re-bills anyway; the model
                # must read live values at the position that always mutates
                state_delta_block = runtime._state_block(dvs)
            else:
                tail = (state.get("frozen_state_block")
                        if runtime.settings.frozen_state_block
                        and state.get("frozen_state_block") is not None
                        and not state.get("engine_fired")
                        else runtime._state_block(dvs))
            # iter49 T4: live mode is decided ONCE here, BEFORE the kb_store
            # gate — rounds without a store (hermetic tests, KB disabled)
            # still need `live`/`live_delta` bound (UnboundLocalError bug,
            # fixed in iter49c T1: the bindings used to live inside the
            # kb_store branch).
            live = bool(getattr(runtime.settings, "rag_live_retrieve", False))
            live_delta = ""
            # iter49 T6: the state-entry ack round runs NO retrieval lanes —
            # the heavy rounds retrieve fresh (the eager-EOT fire supersedes).
            if kb_store is not None and not entry_lite:
                refer_tags = ragmod.parse_refer_tags(runtime.prompts.get(state_name, ""))
                scope = runtime.kb_slugs_for(state_name)
                # iter59 T4: LIVE branch gate = scope only (KB-everywhere:
                # tag-less mechanical states retrieve via lane B — Retell
                # tier). Non-live path below keeps the iter25 gate
                # (tags or allow-list) exactly.
                do_retrieve = bool(scope) if live else \
                    bool(scope) and (bool(refer_tags)
                                     or state_name in runtime._RETRIEVAL_ALLOW)
                # iter49 T4: fresh multi-lane retrieval EVERY non-lite round.
                # The merged working set REPLACES the post-history delta each
                # turn; the pre-history prefix [tools][head][history] is
                # NEVER rewritten (cache = placement, not byte-compare;
                # iter56: the state-block rides the delta too — state_in_delta).
                # Freeze/drift/prefetch are INERT under this
                # flag — the elif below is the exact iter48 path (revert
                # switch: rag_live_retrieve=False).
                if live and do_retrieve:
                    # iter62 — the session-provided transcript is the single
                    # source of truth; history re-extraction diverged under
                    # eager/barge-in and triggered empty respawns (25/67
                    # blind rounds on call cc22e0a0185e).
                    user_msg = (state.get("user_text") or "").strip()
                    # iter49 T3: bounded-await the in-flight task (fired at
                    # eager EOT); degrade = previous turn's fresh set, never
                    # a stall. The awaited portion is pgvector + merge only
                    # when the embed already ran in the speech window.
                    live_delta, rag_kbs, rag_ms, merged, lane_qs, degraded, await_ms = \
                        await runtime._consume_live_retrieve(
                            state_name, dvs, user_msg, kb_store)
                    rag_await_ms = float(await_ms or 0.0)   # iter103 PT-8
                    # iter93 T1 (PT-79): last consume provenance for the
                    # session's turn report (laneA/laneB/fallback + await).
                    runtime._last_consume = {
                        "state": state_name,
                        "mode": getattr(runtime, "_live_mode_label",
                                        "live-async"),
                        "degraded": degraded, "await_ms": await_ms,
                        "ms": rag_ms, "chunks": len(merged)}
                    if runtime.tracer:
                        runtime.tracer.span("rag", output={
                            "state": state_name, "ms": rag_ms, "kbs": rag_kbs,
                            "chunks": len(merged),
                            "query": "; ".join(lane_qs)[:200],
                            "mode": getattr(runtime, "_live_mode_label",
                                            "live-async"),
                            "lanes": len(lane_qs),
                            "pinned": (getattr(runtime, "_pinned_industry", None)
                                       or {}).get("tag") or "",
                            "degraded": degraded,
                            "zero_hit": (len(merged) == 0),
                            "await_ms": await_ms,
                            "prefetched": False, "frozen_reuse": False,
                            "drifted": False,
                            "memo": bool(getattr(runtime,
                                                 "_last_retrieve_memo",
                                                 False))})
                if do_retrieve and not live:
                    # iter31 C3: chunks frozen per state-visit — one retrieval on
                    # state entry, held until the state changes. Same-state rounds
                    # reuse the frozen bytes (no embed, no tail mutation).
                    # iter21: skip the embedding round trip on filler turns
                    # ("hello?", "yes") — zero chunks came back anyway, but the
                    # 200-1900ms embed call sat on the hot path before the LLM.
                    user_msg = next((m["content"] for m in reversed(history)
                                     if m.get("role") == "user"), "")
                    query = ""          # frozen-reuse rounds never enter a branch
                    frozen_reuse = runtime._frozen_state == state_name
                    drifted = False
                    drift_embed_ms = 0.0
                    if len(user_msg.strip()) < getattr(runtime.settings,
                                                      "rag_min_query_chars", 0):
                        # iter21: skip the embedding round trip on filler turns
                        # ("hello?", "yes") — zero chunks came back anyway, but
                        # the 200-1900ms embed sat on the hot path before the LLM.
                        query = ""
                        chunks = []
                        rag_ms = 0.0
                        runtime._frozen_state = state_name
                        runtime._frozen_chunks = chunks
                        # iter48 M4: a filler freeze has NO baseline — reset
                        # any PREVIOUS state's stale query text/vec so the
                        # next real message force-drifts instead of comparing
                        # against the wrong state's vector.
                        runtime._frozen_query_text = ""
                        runtime._frozen_query_vec = None
                    elif not frozen_reuse:
                        # iter44 T6b: consume this state's PREFETCHED chunks when
                        # the transition prewarm staged them — ~0 ms retrieval on
                        # the state-entry critical path; missing/failed staging
                        # falls back to the sync fetch (today's behavior).
                        staged = runtime._rag_staging.pop(state_name, None)
                        if staged is not None:
                            query = staged["query"]
                            chunks = staged["chunks"]
                            rag_ms = 0.0
                            runtime._frozen_state = state_name
                            runtime._frozen_chunks = chunks
                            runtime._frozen_query_text = query
                            runtime._frozen_query_vec = staged.get("vec")
                        else:
                            query, _dv_vals = runtime.refer_query(state_name, dvs, user_msg)
                            cache_key = (state_name, query)
                            chunks = runtime._rag_cache.get(cache_key)
                            cached = chunks is not None
                            if not cached:
                                t_rag = time.perf_counter()
                                chunks = await kb_store.retrieve(query, scope)
                                if len(runtime._rag_cache) > 64:
                                    runtime._rag_cache.clear()
                                runtime._rag_cache[cache_key] = chunks
                                rag_ms = round((time.perf_counter() - t_rag) * 1000, 1)
                            else:
                                rag_ms = 0.2
                            runtime._frozen_state = state_name
                            runtime._frozen_chunks = chunks
                            runtime._frozen_query_text = query
                            runtime._frozen_query_vec = None   # drift baseline embeds lazily
                            if not cached:
                                runtime._kb_stats["rag_turns"] += 1
                                runtime._kb_stats["rag_ms_total"] += rag_ms
                                runtime._kb_stats["rag_chars"] += sum(len(c["content"]) for c in chunks)
                    elif runtime.settings.rag_drift_requery:
                        # iter44 T6c: drift-triggered re-retrieval — a NEW caller
                        # message in the SAME state may need DIFFERENT knowledge
                        # (the iter43 miss: "trucking" never re-anchored the
                        # pain_points query).
                        # iter46 T2 (audit P2): the embed round-trip (234-272 ms
                        # measured) leaves the hot path. The round uses the
                        # frozen chunks with ZERO awaits; a bg task embeds
                        # base+query, computes cosine, and on drift re-retrieves
                        # and appends NEW chunks via the rag_append_delta path —
                        # they render next round (correctness lag ≤ 1 round,
                        # strictly better than pre-T6c which never re-anchored).
                        # Flag OFF → the old synchronous requery, exactly.
                        chunks = runtime._frozen_chunks
                        if runtime.settings.rag_drift_async:
                            runtime._spawn_drift_check(state_name, dvs, user_msg,
                                                       scope, kb_store)
                        else:
                            embed_q = getattr(kb_store, "embed_query", None)
                            if embed_q is None or (
                                    time.monotonic() - runtime._drift_failed_at < 60):
                                chunks = runtime._frozen_chunks   # can't drift-check now
                            else:
                                try:
                                    query, _dv_vals = runtime.refer_query(state_name, dvs, user_msg)
                                    t_e = time.perf_counter()
                                    round_vec = await embed_q(query)
                                    drift_embed_ms = round((time.perf_counter() - t_e) * 1000, 1)
                                    base = runtime._frozen_query_vec
                                    if base is None and runtime._frozen_query_text:
                                        base = await embed_q(runtime._frozen_query_text)
                                        runtime._frozen_query_vec = base
                                except Exception:
                                    runtime._drift_failed_at = time.monotonic()
                                    query = ""
                                    chunks = runtime._frozen_chunks
                                else:
                                    # iter48 M5 (R6): sync-lane parity with
                                    # the async forced-drift semantics —
                                    # base-None ⇒ forced re-retrieve, never a
                                    # silent keep-frozen.
                                    cos = 0.0 if base is None else _cosine(round_vec, base)
                                    if cos >= runtime.settings.rag_drift_threshold \
                                            and base is not None:
                                        chunks = runtime._frozen_chunks
                                    else:
                                        drifted = True
                                        try:
                                            t_rag = time.perf_counter()
                                            chunks = await kb_store.retrieve(query, scope, vec=round_vec)
                                            rag_ms = round((time.perf_counter() - t_rag) * 1000, 1)
                                        except Exception:
                                            chunks = runtime._frozen_chunks
                                        else:
                                            runtime._frozen_query_vec = round_vec
                                            runtime._frozen_query_text = query
                                            if runtime.settings.rag_append_delta:
                                                # append-only: keep the entry tail
                                                # byte-stable; only the chunks NOT
                                                # already rendered become a delta
                                                # system block after the history.
                                                seen = {
                                                    (c.get("kb"), c.get("content"))
                                                    for c in runtime._frozen_chunks
                                                } | {
                                                    (c.get("kb"), c.get("content"))
                                                    for c in runtime._rag_delta_chunks
                                                }
                                                runtime._rag_delta_chunks += [
                                                    c for c in chunks
                                                    if (c.get("kb"), c.get("content")) not in seen
                                                ]
                                                chunks = runtime._frozen_chunks
                                            else:
                                                runtime._frozen_chunks = chunks
                                            runtime._kb_stats["rag_turns"] += 1
                                            runtime._kb_stats["rag_ms_total"] += rag_ms
                                            runtime._kb_stats["rag_chars"] += sum(
                                                len(c["content"]) for c in chunks)
                                            if runtime.tracer:
                                                runtime.tracer.span("rag:drift", output={
                                                    "state": state_name,
                                                    "cosine": round(cos, 4),
                                                    "embed_ms": drift_embed_ms, "ms": rag_ms,
                                                    "chunks": len(chunks), "query": query[:200]})
                    chunks = runtime._frozen_chunks
                    # iter44 append-only: the KNOWLEDGE tail is byte-stable for
                    # the whole state visit (frozen at entry); drift re-fetches
                    # land as a DELTA system block AFTER the history — never a
                    # mid-prompt re-render, so the cached prefix keeps climbing.
                    if runtime.settings.rag_append_delta:
                        if not frozen_reuse:
                            runtime._frozen_rag_tail = ragmod.render_knowledge_section(chunks)
                            runtime._rag_delta_chunks = []
                        elif not runtime._frozen_rag_tail:
                            runtime._frozen_rag_tail = ragmod.render_knowledge_section(chunks)
                        tail += runtime._frozen_rag_tail
                        rag_kbs = sorted({c["kb"] for c in chunks}
                                         | {c["kb"] for c in runtime._rag_delta_chunks})
                    else:
                        rag_kbs = sorted({c["kb"] for c in chunks})
                        tail += ragmod.render_knowledge_section(chunks)
                    if runtime.tracer:
                        runtime.tracer.span("rag", output={
                            "state": state_name, "ms": rag_ms, "kbs": rag_kbs,
                            "chunks": len(chunks), "query": query[:200],
                            "zero_hit": (len(chunks) == 0),
                            "delta_chunks": len(runtime._rag_delta_chunks)
                                if runtime.settings.rag_append_delta else 0,
                            "cached": not frozen_reuse and rag_ms <= 0.2,
                            "frozen_reuse": frozen_reuse, "drifted": drifted,
                            "drift_async": runtime.settings.rag_drift_async,
                            "prefetched": (not frozen_reuse and rag_ms == 0.0
                                           and query != "")})

            # iter31 C3: tool descriptions/params are dvs-FREE ({{var}} literal,
            # static bytes) — the tools block is the FIRST bytes of the request;
            # cached per (state, expand_kb) too.
            # iter32 payload gate: read the PREVIOUS round's payload.
            # silent + all-mechanical → this round gets NO tool channel (must speak).
            prev_tools = [t.get("name") or "" for t in (state.get("last_round_tool_calls") or [])]
            force_speech = (not state.get("round_spoke", True)) and bool(prev_tools) \
                and all(_MECHANICAL_RE.match(n) for n in prev_tools)
            if force_speech and not entry_lite:
                # iter43: keep the FULL tool array — tools render FIRST in the
                # OpenAI cached prefix, so tools=[] on a forced round busts the
                # whole prefix (Langfuse cache_read=0 on the iter32 forced
                # rounds). The SPEECH-ONLY directive in the tail asks the model
                # to hold its fire; the executor still runs anything it calls
                # (no worse than pre-iter32) and round_spoke re-evaluates.
                # iter49 T6: entry_lite is exempt — the ack is speech-only by
                # design and holds its fire via its OWN post-history
                # directive (appending the tail HERE would mutate the
                # pre-history prefix bytes; iter56: the state lives in the
                # delta now, the prefix is [tools][head][history]).
                if runtime.settings.force_speech_keep_tools:
                    tools = runtime._tools_cache.get((state_name, expand_kb))
                    if tools is None:
                        tools = build_tool_schemas(
                            runtime.states[state_name],
                            runtime.llm_json.get("general_tools", []),
                            lambda text: runtime.subst.subst(text, {}, kb=expand_kb),
                            {},
                        )
                        runtime._tools_cache[(state_name, expand_kb)] = tools
                    tail += ("\n\nTHIS ROUND: SPEECH ONLY — do not call any tool. "
                             "Reply to the caller in one or two short sentences, "
                             "then stop.")
                else:
                    tools = []          # iter32 (revertible): channel closed — speech is the only output
            elif lite:
                # iter48b (FIND-5): the lite round carries ONE do-nothing tool —
                # gpt-5.4 only prompt-caches requests WITH tools (raw cURL,
                # OTEL-proven); without it turn 1 was structurally cache-0.
                tools = [LITE_NOOP_TOOL]
            else:
                # Heavy rounds AND the iter49 T6 state-entry ack (FIND-5 port,
                # owner 2026-09-16 "cached tokens are the speed"): the ack
                # carries the FULL tool array, byte-identical to the heavy
                # round's — same (state, expand_kb) cache key (the ack's
                # expand is False; with a store present the heavy round's is
                # False too, i.e. every live round). tools=[] is structurally
                # uncacheable on gpt-5.4 (iter48b FIND-5) and busts the whole
                # cached prefix (the iter43 force-speech lesson), so the ack
                # would re-prefill ~3k fresh tokens at EVERY state change.
                # With the full array the ack's [tools][head] prefix IS the
                # heavy/warm prefix (iter56: no pre-history state-block — it
                # rides the delta) — it rides the already-landed
                # transition/greeting heavy warm at the same cache floor and
                # EXTENDS the cache for the heavy
                # round that follows. Speech-only rides the post-history
                # directive (see the delta below); a state whose entry must
                # run lanes/tools immediately opts out via
                # state_entry_lite_off.
                tools = runtime._tools_cache.get((state_name, expand_kb))
                if tools is None:
                    tools = build_tool_schemas(
                        runtime.states[state_name],
                        runtime.llm_json.get("general_tools", []),
                        lambda text: runtime.subst.subst(text, {}, kb=expand_kb),
                        {},
                    )
                    runtime._tools_cache[(state_name, expand_kb)] = tools
            # C3b layout: [static head] + history + [dynamic tail as trailing
            # system message]. iter44 T3: with tail_before_history the tail
            # sits BEFORE the history ([head][tail][history]) so the cached
            # prefix grows round-to-round. No tail when there is nothing to
            # say (empty dvs, no RAG lane) — never an empty system message.
            # iter44 append-only: drift delta rides AFTER the history.
            # iter56 (C2.2): under state_in_delta the DELTA composition order
            # is pinned state-block → RAG/pinned → ack directive (CR-2/P2):
            # the directive is imperative = FINAL; the state anchors first,
            # adjacent to the head's "CURRENT CALL STATE" pointer. Under the
            # revert flag the state rides the tail pre-history and the delta
            # stays RAG/directive-only (the iter55 layout, byte-exact).
            if live:
                # iter49 T4: the fresh working set REPLACES the post-history
                # delta each turn (placement, never a prefix rewrite).
                rag_delta = live_delta
            else:
                # iter49 T6: entry_lite already leaves live_delta/rag lanes
                # untouched — the revert-mode drift delta stays off the ack too.
                rag_delta = ragmod.render_knowledge_section(runtime._rag_delta_chunks) \
                    if (kb_store is not None and not entry_lite
                        and runtime.settings.rag_append_delta
                        and runtime._rag_delta_chunks) else ""
            if entry_lite:
                # iter49 T6 (FIND-5 port): the ack's speech-only directive
                # rides the POST-HISTORY delta slot — fresh bytes every
                # round, zero cached-prefix cost (the iter43 tail placement
                # would bust the state-block out of the prefix). Same text
                # as the force-speech directive (proven live, iter43); the
                # executor still runs anything the model calls despite it
                # (no worse than pre-iter32, and state_entry_lite_off is
                # the per-state escape hatch).
                rag_delta = ("THIS ROUND: SPEECH ONLY — do not call any "
                             "tool. Reply to the caller in one or two "
                             "short sentences, then stop.")
                if runtime.settings.state_in_delta and not lite:
                    # iter56 (F-09): the ack delta carries the LIVE state
                    # too — the model must see current values while it
                    # acknowledges. Directive stays FINAL (it's imperative).
                    # iter114 T3: the WIT card REPLACES the raw state block
                    # in the ack delta (facts + flags for the ack's 1-2
                    # sentences; the heavy round that follows restores full
                    # fidelity). Keeps the ack payload ≤ the heavy payload
                    # (the FIND-5 byte-budget pin).
                    state_block = runtime._state_block(dvs)
                    if runtime.settings.speak_card:
                        card = runtime._wit_card(state_name, dvs, history)
                        if card:
                            state_block = card
                    if state_block:
                        rag_delta = state_block + "\n\n" + rag_delta
            elif runtime.settings.state_in_delta and not lite:
                # iter56 (C2.2): state FIRST, RAG after — the delta IS the
                # last message the model reads; the state block anchors the
                # live values before the knowledge section tails it.
                if state_delta_block:
                    rag_delta = state_delta_block + rag_delta
            notes_delta = ""
            if runtime.settings.ask_hold:
                notes: list[str] = []
                if state.get("unspoken_tail"):
                    # iter105 T1 (ratified NOTE, verbatim): the previous round
                    # was chopped at its question — the tail never reached the
                    # caller. Consumed by this round (updates clear the tail).
                    notes.append(_CHOP_NOTE)
                if state.get("held_facts"):
                    # iter105 T3: engine news/results parked while the caller
                    # owed an answer — deliver them now, then continue.
                    facts = "; ".join(str(f) for f in state["held_facts"])
                    notes.append(f"ENGINE: {facts}. Deliver these now, then "
                                 f"continue the flow.")
                if notes:
                    notes_delta = "\n\n".join(notes)
                    rag_delta = notes_delta + ("\n\n" + rag_delta if rag_delta else "")
            messages = self._build_messages(state_name, history, tail, system,
                                            delta=rag_delta)

            # iter114 speech authorization: authorized number/status facts for
            # the render guard, computed ONCE per round (dvs values + the
            # caller's own words + tool results — never the static prompt, RAG
            # or assistant history). Flag-gated; no-op when off.
            if runtime.settings.speak_fact_guard:
                _guard_tooltext, _guard_auth, _guard_nums = \
                    runtime._speak_auth_sets(dvs, history)
            else:
                _guard_tooltext, _guard_auth, _guard_nums = "", set(), set()

            # iter103 PT-8 (observability-only): pre-LLM gap breakdown — the
            # window from round entry to LLM dispatch, decomposed per round.
            # Additive report data only; no behavior change.
            t0 = time.perf_counter()
            entry_ms = round((t0 - t_round_entry) * 1000.0, 1)
            warm_ms = float(getattr(runtime, "_last_warm_wait", 0) or 0)
            runtime._last_round_pre_llm = {
                "state": state_name,
                "turn": state.get("turn_index", 0),
                "entry_to_dispatch_ms": entry_ms,
                "warm_await_ms": warm_ms,
                "rag_ms": float(rag_ms or 0.0),
                "rag_await_ms": round(rag_await_ms, 1),
                "other_ms": max(0.0, round(entry_ms - warm_ms - rag_await_ms, 1)),
            }
            # iter112 clock truth (iter97 port): the round dispatches to the
            # LLM NOW — session stamps the turn clock on this custom event
            # (first-only per turn). Under speak-first this sits before the
            # SPEAK dispatch (the user-perceived stream).
            writer({"llm_dispatch": True})
            # iter114 T7 (telemetry truth): decide the split BEFORE the tracer
            # starts so the generation label names the model that ACTUALLY
            # serves this stream (the lie: every gen labelled openai_model
            # while the speak twin ran 4.1/luna). The delta rides the trace
            # input too — the value the mouth saw is what autopsies need.
            speak_first_arm = bool(
                getattr(runtime.settings, "speak_first_pipeline", False)
                and tools and not lite and not entry_lite)
            # iter114c: the STATE-resolved speak model (luna for the fast
            # states, openai_model for the tool-heavy ones; "" when the
            # pipeline isn't armed or speak_model is unset = E1).
            _spk = runtime._speak_model_for(state_name) if speak_first_arm else ""
            _trace_model = _spk or runtime.settings.openai_model
            _trace_input = history[-6:] + \
                ([{"role": "system", "content": rag_delta}] if rag_delta else [])
            gen_obs = runtime.tracer.start_llm(state_name, _trace_model,
                                               _trace_input) if runtime.tracer else None
            content = ""
            tool_calls: list[dict] = []
            usage = None
            ttft_s: float | None = None   # iter44: time to FIRST spoken token (TTS-visible)
            separated = False   # iter16: one space between state texts in a multi-state walk
            # iter41 T1 / iter42: sentence-level TTS dedupe — buffer tokens per
            # sentence (flush on . ? ! / newline, or at stream end); an exact
            # duplicate (case-insensitive, whitespace-collapsed) of ANY of the
            # last `tts_dedupe_window` flushed sentences of this turn is dropped
            # from the SPOKEN stream only (model output/history/final content
            # untouched). Kept sentences are written with their ORIGINAL token
            # boundaries. The window carries across ROUNDS of the same turn (a
            # state_node call is one round; gpt-5.x repeats whole sentence
            # blocks — the Sofia/Carlos/Jorge A,B,A,B shape). Reset at ingest.
            # Silence guard: a round whose every sentence is a window hit still
            # speaks if nothing has been spoken yet this turn.
            pending_text = ""
            pending_tokens: list[str] = []
            window: list[str] = list(state.get("tts_sentence_window") or []) \
                if runtime.settings.tts_dedupe_sentences else []
            # iter59 T4: cross-turn question stem window — round-scoped copy
            # of the state key (plain key, no reducer; survives the call),
            # mutated by the token loop and written back in `updates`.
            stem_window: list[dict] = list(state.get("question_stem_window")
                                           or []) \
                if runtime.settings.tts_dedupe_semantic else []
            spoken_any = bool(state.get("turn_spoke"))
            # iter105 T1 (ask-hold chop): exact spoken bytes of this round and
            # the "the round asked a question" flag — the first SPOKEN chunk
            # ending "?" chops the round's tail from TTS; history/updates then
            # carry only the spoken prefix (rule 3: history == heard).
            spoken_parts: list[str] = []
            round_asked = False
            # iter114 T5 (render guard): per-round cut bookkeeping — one
            # recovery line max, tracer span + log per cut sentence.
            _guard_cuts = 0
            _guard_recovery = False
            # iter112 speak-first staggered pipeline: every tool-eligible
            # heavy round fires a BARE speech-only stream first (tools=[],
            # "we don't need tools to stream audio") while a gpt-5.4 TOOLS
            # stream runs concurrently as a background task (text discarded;
            # its final's tool_calls are the only tool source). Lite, entry-
            # lite and channel-closed (tools=[]) rounds stay byte-exact
            # single-call — they are already speech-only. Flag OFF = the
            # ORIGINAL call site byte-exact.
            # (speak_first_arm computed above with the tracer — T7.)
            _sf_task: asyncio.Task | None = None
            if speak_first_arm:
                _sf_task = runtime._fire_split_tools(
                    messages, tools, history, dvs, state_name,
                    state.get("turn_index", 0))
                # iter114 T4 (speak diet): the mouth renders from conversation
                # + the WIT card — no raw tool JSON, no RAG chunks. The TOOLS
                # stream above keeps the full-fidelity payload byte-unchanged.
                if runtime.settings.speak_diet:
                    _speak_delta = runtime._speak_delta(state_name, dvs,
                                                        history, notes_delta)
                    speak_messages = runtime._speak_view(system, history,
                                                         _speak_delta)
                else:
                    speak_messages = messages
                ev_source = runtime.llm.astream(
                    speak_messages, [],
                    verbosity=runtime._verbosity_for(state_name), speak=True,
                    speak_model=_spk)
            else:
                ev_source = runtime.llm.astream(
                    messages, tools,
                    verbosity=runtime._verbosity_for(state_name))
            async for ev in ev_source:
                # iter87 T1.4: ttft anchors on the FIRST delta of ANY type
                # (token or tool_call_start) — tool-only rounds previously
                # logged ttft=None and hid the LLM time-to-first-event.
                if ttft_s is None:
                    ttft_s = time.perf_counter() - t0
                    # iter112 clock truth (iter97 port): first SDK event of
                    # ANY type — session stamps the turn clock (first-only).
                    # Under speak-first this stream IS the SPEAK stream; the
                    # background TOOLS task never stamps.
                    writer({"llm_first_event": True})
                if ev["type"] == "token":
                    if runtime.settings.ask_hold and round_asked:
                        continue              # iter105 T1: chopped — tail dies here
                    if state.get("turn_spoke") and not separated:
                        writer({"tts_token": " "})
                        separated = True
                    if runtime.settings.tts_dedupe_sentences:
                        pending_text += ev["text"]
                        pending_tokens.append(ev["text"])
                        while True:
                            if runtime.settings.ask_hold and round_asked:
                                pending_tokens, pending_text = [], ""
                                break         # iter105 T1: chopped mid-round
                            cut = 0
                            m = _SENTENCE_SPLIT_RE.search(pending_text)
                            if m:
                                cut = m.end()
                            elif runtime.settings.tts_gate_fast_first_flush \
                                    and not spoken_any:
                                # iter46 T5: nothing spoken this turn yet —
                                # flush the FIRST clause early (first "," after
                                # ≥12 chars, or the char threshold) so the
                                # caller hears the reply one clause sooner.
                                # The fragment joins the dedupe window exactly
                                # like a full sentence (dedupe integrity holds:
                                # a later FULL sentence can never equal an
                                # earlier fragment's bytes).
                                cut = _fast_flush_cut(pending_text, runtime.settings)
                            if not cut:
                                break
                            head_tokens: list[str] = []
                            acc = 0
                            while pending_tokens:
                                tok = pending_tokens[0]
                                if acc + len(tok) <= cut:
                                    head_tokens.append(tok)
                                    acc += len(tok)
                                    pending_tokens.pop(0)
                                else:               # token straddles the boundary
                                    k = cut - acc
                                    head_tokens.append(tok[:k])
                                    pending_tokens[0] = tok[k:]
                                    break
                            pending_text = pending_text[cut:]
                            # iter65 T2: sanitize BEFORE the dedupe window —
                            # a code/JSON artifact sentence is dropped from
                            # speech entirely (never joins the window, never
                            # sets spoken_any); a stripped sentence re-keys
                            # so the window matches what was actually spoken.
                            sent_raw = "".join(head_tokens)
                            sent = _sanitize_spoken(sent_raw, runtime.settings)
                            if sent is None:
                                continue
                            # iter114 T5: render guard — an unauthorized
                            # figure/time/status sentence never reaches TTS.
                            if runtime.settings.speak_fact_guard:
                                _reason = _speak_guard_cut(
                                    sent, _guard_auth, _guard_nums, dvs,
                                    _guard_tooltext)
                                if _reason:
                                    _guard_cuts += 1
                                    if not _guard_recovery:
                                        _guard_recovery = True
                                        _rec = _GUARD_RECOVERY_NUM \
                                            if _reason.startswith("number:") \
                                            else _GUARD_RECOVERY_STATUS
                                        writer({"tts_token": _rec + " "})
                                    if runtime.tracer:
                                        runtime.tracer.span(
                                            "speak_guard_cut",
                                            output={"reason": _reason,
                                                    "sentence": sent[:160]})
                                    log.info("speak guard cut: reason=%s "
                                             "sentence=%r", _reason,
                                             sent[:160])
                                    continue
                            if sent != sent_raw:
                                head_tokens = [sent]
                            key = _norm_sentence(sent)
                            if key and key in window and spoken_any:
                                continue            # in-turn window double — never spoken
                            # iter59 T4: semantic cross-turn dedupe — a QUESTION
                            # whose stem is a near-verbatim re-ask of a question
                            # already spoken THIS CALL is dropped from the
                            # spoken stream (same drop path as the exact dup:
                            # model output/history untouched). ONE embed per
                            # question sentence (vec reused for the window
                            # append); store None / embed failure -> exact-only
                            # fallback, never block speech. Statements and
                            # fast-flush fragments NEVER embed (fragments join
                            # the exact window as today — G5).
                            # iter60 AUD-6: no spoken_any gate — a turn-OPENING
                            # near-verbatim re-ask must be checked too; the
                            # non-empty stem_window itself proves a question
                            # was already spoken this call.
                            qvec = None
                            is_q = runtime.settings.tts_dedupe_semantic \
                                and "?" in sent
                            if is_q and stem_window:
                                qvec = await _embed_stem(runtime, sent)
                                if qvec is not None:
                                    thr = float(runtime.settings.tts_dedupe_cos_threshold)
                                    dup = False
                                    for entry in stem_window:
                                        prev = entry.get("vec")
                                        if prev and _cosine(qvec, prev) >= thr:
                                            dup = True
                                            break
                                    if dup:
                                        continue    # cross-turn near-verbatim re-ask — never spoken
                            for tok in head_tokens:
                                writer({"tts_token": tok})
                            if runtime.settings.ask_hold:
                                spoken_parts.append("".join(head_tokens))
                                if not round_asked and sent.rstrip().endswith("?"):
                                    round_asked = True   # T1: tail chopped from here
                            if key:
                                window.append(key)
                                spoken_any = True
                                max_w = runtime.settings.tts_dedupe_window
                                if max_w > 0 and len(window) > max_w:
                                    del window[:len(window) - max_w]
                                # iter59 T4: spoken questions join the cross-turn
                                # stem window ({"text","vec"}; capped FIFO).
                                if is_q:
                                    if not _spoken_entry_has(stem_window, key):
                                        if qvec is None:
                                            qvec = await _embed_stem(runtime, sent)
                                        if qvec is not None:
                                            stem_window.append({"text": key,
                                                                "vec": qvec})
                                        max_sw = runtime.settings.tts_dedupe_stem_window
                                        if max_sw > 0 and len(stem_window) > max_sw:
                                            del stem_window[:len(stem_window) - max_sw]
                    else:
                        writer({"tts_token": ev["text"]})
                elif ev["type"] == "tool_call_start":
                    # iter46 T4a: fire the destination-state prewarm the
                    # moment a transition tool NAME completes in the stream
                    # (the tool name encodes the destination) — NOT after
                    # executor.execute() returns. Banks the rest of the
                    # speaking round + TTS drain (~0.5-1.5 s) as warm-prefill
                    # head start. The post-execute warm re-fires with the
                    # post-patch dvs (force=True supersedes in _latest_warm).
                    name = ev.get("name") or ""
                    dest = name[len("transition_to_"):] if name.startswith("transition_to_") else ""
                    if dest in runtime.states:
                        runtime.warm_prompt_cache(dest, list(history), dvs=dict(dvs))
                elif ev["type"] == "final":
                    content = ev["content"]
                    tool_calls = ev["tool_calls"]
                    usage = ev.get("usage")
            # iter43 T1: per-round token/cache telemetry — cache_read is the
            # OpenAI prefix-cache hit; the T2/T3 goal is it growing past the
            # tools+static-head floor across turns.
            # iter56 (C2.7): the round usage line carries state_in_delta +
            # delta_tokens (the post-history delta's char size) so the
            # G1/G2 gates and the report can attribute the layout per round.
            if usage:
                itd = usage.get("input_token_details") or {}
                # iter108 fix (2026-10-12): fast-tier (priority) serves can report
                # the cached count as `priority_cache_read` — read either field.
                _cache_read = itd.get("cache_read", 0) or itd.get("priority_cache_read", 0) or 0
                log.info("round usage: turn=%s state=%s input=%s cache_read=%s "
                         "output=%s ttft=%sms state_in_delta=%s delta_tokens=%s",
                         state.get("turn_index"), state_name,
                         usage.get("input_tokens"), _cache_read,
                         usage.get("output_tokens"),
                         round(ttft_s * 1000) if ttft_s is not None else None,
                         bool(runtime.settings.state_in_delta),
                         len(rag_delta) if rag_delta else 0)
                if speak_first_arm:
                    # iter112: per-stream telemetry — the speak stream's own
                    # line (the tools line is logged by the task consumer).
                    log.info("split stream usage: stream=speak turn=%s "
                             "state=%s input=%s cache_read=%s output=%s "
                             "ttft_ms=%s",
                             state.get("turn_index"), state_name,
                             usage.get("input_tokens"), _cache_read,
                             usage.get("output_tokens"),
                             round(ttft_s * 1000) if ttft_s is not None else None)
            if runtime.settings.tts_dedupe_sentences and pending_tokens \
                    and not (runtime.settings.ask_hold and round_asked):
                # iter65 T2: sanitize the stream-end flush FIRST — a code/
                # JSON artifact sentence is dropped entirely (pending
                # cleared; never spoken, never joins the window).
                pending_sent = _sanitize_spoken(pending_text, runtime.settings)
                if pending_sent is not None and runtime.settings.speak_fact_guard:
                    # iter114 T5: render guard on the stream-end flush too.
                    _reason = _speak_guard_cut(pending_sent, _guard_auth,
                                               _guard_nums, dvs,
                                               _guard_tooltext)
                    if _reason:
                        _guard_cuts += 1
                        if not _guard_recovery:
                            _guard_recovery = True
                            _rec = _GUARD_RECOVERY_NUM \
                                if _reason.startswith("number:") \
                                else _GUARD_RECOVERY_STATUS
                            writer({"tts_token": _rec + " "})
                        if runtime.tracer:
                            runtime.tracer.span(
                                "speak_guard_cut",
                                output={"reason": _reason,
                                        "sentence": pending_sent[:160]})
                        log.info("speak guard cut: reason=%s sentence=%r",
                                 _reason, pending_sent[:160])
                        pending_sent = None
                if pending_sent is None:
                    pending_tokens, pending_text = [], ""
                else:
                    if pending_sent != pending_text:
                        pending_tokens = [pending_sent]
                    pending_text = pending_sent
                    pending_key = _norm_sentence(pending_text)
                    if not (pending_key and spoken_any and pending_key in window):
                        # iter59 T4: the stream-end flush is a FULL sentence — the
                        # semantic question check applies (fragments never embed).
                        # ONE embed for check + window append.
                        # iter60 AUD-6: no spoken_any gate (turn-OPENING re-asks
                        # are checked too); non-empty stem_window = never-silent.
                        pending_qvec = None
                        pending_is_q = runtime.settings.tts_dedupe_semantic \
                            and "?" in pending_text
                        if pending_is_q and stem_window:
                            pending_qvec = await _embed_stem(runtime, pending_text)
                            if pending_qvec is not None:
                                thr = float(runtime.settings.tts_dedupe_cos_threshold)
                                dup = any(e.get("vec") and
                                          _cosine(pending_qvec, e["vec"]) >= thr
                                          for e in stem_window)
                                if dup:
                                    pending_tokens, pending_text = [], ""
                                    pending_key = None
                                    pending_is_q = False
                        if pending_key and pending_tokens:
                            for tok in pending_tokens:
                                writer({"tts_token": tok})
                            if runtime.settings.ask_hold:
                                spoken_parts.append("".join(pending_tokens))
                                if not round_asked and \
                                        pending_text.rstrip().endswith("?"):
                                    round_asked = True   # T1: final chunk asked
                            window.append(pending_key)
                            spoken_any = True
                            max_w = runtime.settings.tts_dedupe_window
                            if max_w > 0 and len(window) > max_w:
                                del window[:len(window) - max_w]
                            if pending_is_q and not _spoken_entry_has(stem_window,
                                                                      pending_key):
                                if pending_qvec is None:
                                    pending_qvec = await _embed_stem(runtime, pending_text)
                                if pending_qvec is not None:
                                    stem_window.append({"text": pending_key,
                                                        "vec": pending_qvec})
                                max_sw = runtime.settings.tts_dedupe_stem_window
                                if max_sw > 0 and len(stem_window) > max_sw:
                                    del stem_window[:len(stem_window) - max_sw]
                    pending_tokens, pending_text = [], ""
            llm_s = time.perf_counter() - t0
            # iter112 M1: adopt the background TOOLS result if it landed while
            # the turn is still open (await-on-arrival: a done future is
            # taken NOW; a pending one is NEVER waited for here — it rides to
            # M2 at the next caller EOT). NO caps anywhere on this path.
            if speak_first_arm and _sf_task is not None:
                if _sf_task.cancelled():
                    runtime._pending_tools = None
                    tool_calls = []
                    log.info("split M1: tools task cancelled turn=%s state=%s",
                             state.get("turn_index"), state_name)
                elif _sf_task.done():
                    _sf_res: dict | None = None
                    try:
                        _sf_res = _sf_task.result()
                    except Exception as exc:
                        log.warning("split tools task failed: %s", exc)
                    runtime._pending_tools = None
                    if _sf_res:
                        tool_calls = _sf_res["tool_calls"] or []
                    # divergence: silent mouth + tools present (iter110 class)
                    if tool_calls and not content.strip():
                        log.warning("split_divergence: mouth-empty "
                                    "speak_chars=0 tools=%s",
                                    [tc["name"] for tc in tool_calls])
                    # divergence: booking-claim speech with zero tool calls
                    if not tool_calls and _BOOKED_CLAIM_RE.search(content or ""):
                        log.warning("split_divergence: booked-claim-no-tools "
                                    "speech=%r", (content or "")[:160])
                    if tool_calls:
                        log.info("split followup: turn=%s state=%s tools=%s",
                                 state.get("turn_index"), state_name,
                                 [tc["name"] for tc in tool_calls])
                    else:
                        log.info("split followup: none turn=%s state=%s "
                                 "(speak-only round)",
                                 state.get("turn_index"), state_name)
                else:
                    # Not landed yet — this turn closes as a pure-speech round
                    # (`executed` empty -> finalize); the future is awaited at
                    # the NEXT turn's ingest (M2).
                    tool_calls = []
                    log.info("split M1: tools pending turn=%s state=%s — "
                             "deferring to M2", state.get("turn_index"),
                             state_name)
            if runtime.tracer:
                runtime.tracer.finish_llm(
                    gen_obs, output=content, latency_s=llm_s, usage=usage,
                    tool_calls=[{"name": tc["name"], "args": tc["arguments"]} for tc in tool_calls],
                    ttft_s=ttft_s,
                    extra_meta={"state_in_delta": bool(runtime.settings.state_in_delta),
                                "delta_tokens": len(rag_delta) if rag_delta else 0},
                )

            # iter105 T1 (ask-hold): the spoken prefix is the round's record
            # when the tail was chopped; the chopped bytes = unspoken_tail
            # (diagnostic + the next round's NOTE trigger). ASK_HOLD=false
            # leaves both no-ops (round_asked stays False).
            chopped = bool(runtime.settings.ask_hold) and round_asked
            spoken_prefix = "".join(spoken_parts)
            assistant_text_out = spoken_prefix if chopped else content
            updates: dict[str, Any] = {"assistant_text": assistant_text_out,
                                       "turn_spoke": bool(content) or bool(state.get("turn_spoke")),
                                       "round_spoke": bool(content),   # iter29: this round's speech flag
                                       # iter41 T1/iter42: carry the last-N SPOKEN sentence
                                       # keys to the next round of this turn (cross-round dedupe)
                                       "tts_sentence_window": window,
                                       # iter59 T4: cross-turn question stems ride
                                       # the round updates (plain key — last
                                       # write wins; NOT reset at ingest).
                                       "question_stem_window": stem_window,
                                       # iter105 T1: did THIS round ask (chop fired);
                                       # unspoken_tail always written — "" consumes
                                       # the previous round's tail/NOTE trigger.
                                       "round_asked": bool(round_asked),
                                       "unspoken_tail": content[len(spoken_prefix):] if chopped else "",
                                       # iter105 T3: always written — [] consumes
                                       # parked engine facts (delivered in the
                                       # delta note above).
                                       "held_facts": [],
                                       # iter30: last non-empty spoken text (end_call goodbye memory)
                                       "last_spoken": assistant_text_out if assistant_text_out.strip()
                                       else state.get("last_spoken", "")}
            new_history: list[dict] = []
            if tool_calls:
                # iter29: dedupe identical tool calls within one round — keep first by name
                seen: set[str] = set()
                tool_calls = [tc for tc in tool_calls
                              if not (tc["name"] in seen or seen.add(tc["name"]))]
                new_history.append({
                    "role": "assistant",
                    "content": assistant_text_out or "",
                    "tool_calls": [
                        {"id": tc["id"], "type": "function",
                         "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                        for tc in tool_calls
                    ],
                })
                dvs_patch: dict[str, Any] = {}
                new_state: str | None = None
                ended = False
                executed: list[dict] = []
                t1 = time.perf_counter()
                # iter40 L3: dead-air killer — an empty-text round whose pending
                # tool is the slots query speaks the ConfirmSlots.md Start line
                # (already in the prompt, no new content) via TTS while the
                # webhook runs. Anti-repeat per call via fillers_said.
                # iter105 T4: ask_hold=True replaces this with the >=400 ms
                # in-flight timer below (single filler producer). The legacy
                # arm survives ONLY on the rollback path (ASK_HOLD=false).
                slots_filler = ("One moment while I check availability.")
                emit_filler = (not runtime.settings.ask_hold
                               and runtime.settings.one_trip and not content.strip()
                               and not state.get("turn_spoke")
                               and slots_filler not in (state.get("fillers_said") or [])
                               and any(tc["name"] == "query_livecall_slots"
                                       for tc in tool_calls))
                if emit_filler:
                    writer({"tts_token": slots_filler + " "})
                    fillers = list(state.get("fillers_said") or []) + [slots_filler]
                    updates["fillers_said"] = fillers
                    updates["turn_spoke"] = True        # round 2 gets its separator
                for tc in tool_calls:
                    # iter105 T2 (ask-hold defer gate): a DEFER_SET call never
                    # executes before the caller's answer — S1: the round's
                    # spoken text ended on a question (chop fired); S2: the
                    # round entered ConfirmSlots and the caller has not named
                    # a day/time yet (day-guard executes answer-first pulls).
                    # Deferred != executed: a synthetic tool message tells the
                    # model to wait/re-call; the tool runs next round with the
                    # caller's real answer.
                    if runtime.settings.ask_hold and tc["name"] in _DEFER_SET:
                        s1 = bool(round_asked)
                        s2 = (tc["name"] == "query_livecall_slots"
                              and any(t["name"] == "transition_to_ConfirmSlots"
                                      for t in tool_calls)
                              and not (_DAY_RE.search(state.get("user_text") or "")
                                       or _DAY_RE.search(str(dvs.get("requested_slot") or ""))))
                        if s1 or s2:
                            deferred = {"status": "deferred",
                                        "message": (_DEFER_S1_MSG.format(tool=tc["name"])
                                                    if s1 else _DEFER_S2_MSG)}
                            executed.append({"name": tc["name"], "ok": False,
                                             "status": "deferred"})
                            new_history.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "content": json.dumps(deferred),
                            })
                            if runtime.tracer:
                                runtime.tracer.span("ask_hold:defer", output={
                                    "tool": tc["name"], "arm": "S1" if s1 else "S2",
                                    "state": state_name})
                            continue
                    try:
                        args = json.loads(tc["arguments"] or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    exec_coro = runtime.executor.execute(
                        tc["name"], args, dvs, state_name,
                        spoken_text=assistant_text_out,
                        last_spoken=state.get("last_spoken", ""))
                    # iter105 T4 (single filler producer): the ONLY hold-line
                    # emission — a slots webhook GENUINELY in flight >= 400 ms
                    # speaks "One moment while I check availability" exactly
                    # once (never when the round/turn already spoke; a warm
                    # prefetch hit or fast result completes in time => never
                    # fires). ask_hold=False keeps the legacy dead-air killer.
                    if (runtime.settings.ask_hold
                            and tc["name"] == "query_livecall_slots"
                            and not state.get("turn_spoke")
                            and not spoken_parts
                            and slots_filler not in (state.get("fillers_said") or [])):
                        _task = asyncio.create_task(exec_coro)
                        _done, _ = await asyncio.wait({_task}, timeout=0.4)
                        if not _done:
                            writer({"tts_token": slots_filler + " "})
                            updates["fillers_said"] = \
                                list(state.get("fillers_said") or []) + [slots_filler]
                            updates["turn_spoke"] = True   # round 2 gets its separator
                        outcome = await _task
                    else:
                        outcome = await exec_coro
                    executed.append({"name": tc["name"],
                                     "ok": bool(outcome.response.get("ok", True)),
                                     "status": outcome.response.get("status")})
                    dvs_patch.update(outcome.dvs_patch)
                    dvs.update(outcome.dvs_patch)   # same-round visibility (engine parity)
                    if outcome.new_state:
                        new_state = outcome.new_state
                    ended = ended or outcome.ended
                    new_history.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": json.dumps(outcome.response)[:4000],
                    })
                # iter109 (transition-hold): iter105 T4 left transition rounds
                # fully silent — the model's pull-first prose was deleted and
                # the legacy chain fillers are gated off under ask_hold — so
                # on live calls the caller heard nothing from their EOT until
                # the destination state spoke (3-4.6 s on transitions). A hop
                # round that SUCCEEDED and spoke nothing speaks ONE engine
                # hold line here, while the destination prewarm + entry round
                # run. Anti-repeat per call; never when the round/turn spoke;
                # ask_hold=False keeps the legacy byte-exact path.
                if (new_state
                        and runtime.settings.ask_hold
                        and not content.strip()
                        and not state.get("turn_spoke")
                        and not updates.get("turn_spoke")):
                    said = list(updates.get("fillers_said")
                                or state.get("fillers_said") or [])
                    for line in _ENGINE_FILLERS:
                        if line not in said:
                            writer({"tts_token": line + " "})
                            updates["fillers_said"] = said + [line]
                            updates["turn_spoke"] = True
                            break
                if new_state:
                    updates["state_name"] = new_state
                    # iter40 L2: an ok transition INTO ConfirmSlots bg-warms
                    # the ASAP payload so the model's first slots call hits cache
                    if new_state == "ConfirmSlots":
                        runtime.warm_slots_async(dvs)
                    # iter43 T3 / iter44 T1: prewarm the DESTINATION state's
                    # full payload (tools + stripped head + tail + history
                    # snapshot) while the transition round still speaks.
                    # history + new_history is the exact next-round history
                    # (only ever appends). iter46 T4a: force=True — this warm
                    # re-fires after the tool-detection warm with the
                    # POST-PATCH dvs bytes (tail-byte parity with the entry
                    # round) and becomes the _latest_warm entry await target.
                    runtime.warm_prompt_cache(new_state,
                                              list(history) + list(new_history),
                                              dvs=dict(dvs), force=True)
                    # iter87 VERSION B fire law (one-ahead): entering X also
                    # warms X's FSM successors — lands during the caller's
                    # think time (real p50 17 s). Non-force: the latch
                    # dedupes; the eventual real transition re-fires with
                    # force=True (above) which supersedes _latest_warm.
                    # GATED on the hold knob (knob law): 0 = byte-exact
                    # iter85 — successor warms stage RAG, so they must not
                    # fire at default.
                    if runtime.settings.warm_await_dest_ms > 0:
                        for succ in runtime._state_successors.get(new_state, ()):
                            runtime.warm_prompt_cache(
                                succ, list(history) + list(new_history),
                                dvs=dict(dvs))
                    # iter44 T6b: bg RAG prefetch for the destination scope —
                    # staged chunks are consumed at the next state entry.
                    runtime.warm_rag_async(new_state, dvs=dict(dvs))
                if ended:
                    updates["ended"] = True
                # iter114 T1: derive-on-write — the moment this round's tool
                # writes land, every derivable fact (leak math) exists BEFORE
                # the next payload build. Pure compute, no LLM.
                if runtime.settings.derive_on_write and dvs_patch:
                    _derived, _ = runtime._derive(dvs)
                    if _derived:
                        dvs_patch.update(_derived)
                        dvs.update(_derived)
                updates["dvs"] = dvs_patch
                updates["history"] = new_history
                updates["last_round_tool_calls"] = executed
                # iter33: after a chain abort, the model's repair speech clears
                # the abort surface (the STATE BLOCK tail stops showing it).
                if content.strip() and dvs.get("chain_aborted_step"):
                    dvs_patch.update({"chain_aborted_step": "", "chain_aborted_msg": ""})
                updates["rounds_left"] = state.get("rounds_left", 0) - 1
                updates["metrics"] = {
                    "llm_s": round(llm_s, 3),
                    "tools_s": round(time.perf_counter() - t1, 3),
                    "calls": [tc["name"] for tc in tool_calls],
                    "rag_ms": rag_ms,
                    "rag_kbs": rag_kbs,
                }
            else:
                new_history.append({"role": "assistant", "content": assistant_text_out or ""})
                updates["history"] = new_history
                updates["last_round_tool_calls"] = []
                # iter33: pure-speech repair round after a chain abort — clear
                # the abort surface once the model actually spoke.
                if assistant_text_out.strip() and state.get("dvs", {}).get("chain_aborted_step"):
                    updates["dvs"] = {"chain_aborted_step": "", "chain_aborted_msg": ""}
                updates["metrics"] = {"llm_s": round(llm_s, 3), "tools_s": 0.0,
                                      "calls": ["<speak>"], "rag_ms": rag_ms,
                                      "rag_kbs": rag_kbs}
                # iter30: the goodbye IS the end of the call — deterministic.
                # The goodbye round is a pure-speech round (20+ rounds of live
                # evidence: gpt-5.2-class models never co-emit end_call with text), so the
                # "SPOKEN goodbye = call over" rule is engine-enforced here:
                # ended=True, no end_call call required, no hammer loop.
                # GATED by the SAME booking-claim truth gate as the end_call
                # tool (iter8): a livecall-agreed caller with no verified slot/
                # booking can NOT be hung up on — no bail-out, no fake booking.
                # iter105 T1: a goodbye the caller did NOT hear (chopped tail)
                # never ends the call — the check runs on the spoken bytes.
                if assistant_text_out.strip() and _GOODBYE_RE.search(assistant_text_out) and not (
                        dvs.get("livecall_agreed")
                        and not (dvs.get("slot_verified") or dvs.get("booking_confirmed"))):
                    updates["ended"] = True
            # iter49 T6: remember the state this round RENDERED. The turn-1
            # lite round is exempt — it renders no state head, so the turn-2
            # entry into the initial state still counts as a state entry.
            if not lite:
                runtime._prev_round_state = state_name
            return updates

        state_node.__name__ = f"state_{state_name}"
        return state_node

    # ------------------------------------------------------------------ #
    async def _deterministic_node(self, state: GraphState) -> dict:
        """Runs after every state round. Deterministic, no LLM, no speech.

        - leak math auto-compute (open item #1: the pitch can't be skipped)
        - /today prefetch when entering ConfirmSlots without a date (open item #3)
        """
        dvs = DynamicVariables.from_flat(state.get("dvs", {})).to_flat()
        state_name = state.get("state_name", "")
        patch: dict[str, Any] = {}
        fired = False                        # iter29: engine acted this pass
        fired_leak = False                   # iter105 T3
        fired_date = False                   # iter105 T3

        # iter114 T1: shared derivation (byte-equal to the old inline block —
        # same conditions, same span name for the existing gates/reports).
        _leak_patch, fired_leak = self._derive(
            dvs, span_name="deterministic:leak_math")
        if _leak_patch:
            patch.update(_leak_patch)
            fired = True

        # iter40 L2: every ConfirmSlots pass keeps the ASAP payload warm (fresh
        # → no-op). Background warm only — never counts as engine_fired.
        if state_name == "ConfirmSlots":
            self.warm_slots_async(dvs)

        if state_name == "ConfirmSlots" and not dvs.get("today_date"):
            outcome = await self.executor.execute("check_current_date", {}, dvs, "ConfirmSlots")
            if outcome.dvs_patch.get("today_date"):
                patch["today_date"] = outcome.dvs_patch["today_date"]
                fired = True
                fired_date = True
                if self.tracer:
                    self.tracer.span("deterministic:today_prefetch", output=outcome.dvs_patch)

        # iter105 T3 (ask-hold hold-release): the trigger round asked a
        # question — engine news (leak math / date prefetch) and ok executed
        # result tools must NOT speak this turn. Park them as held_facts;
        # route_after_deterministic finalizes; the next round's delta carries
        # the ENGINE note. Non-question rounds are byte-exact today.
        held_out: list[str] = []
        chain_ready = (self.settings.phaseb_chain
                       and state_name in ("ConfirmSlots", "VerifyLead", "Booking")
                       and not dvs.get("chain_done") and not dvs.get("booking_verified")
                       and not dvs.get("booking_intent")
                       and dvs.get("slot_verified") and dvs.get("selected_time")
                       and dvs.get("prospect_timezone") and dvs.get("callback_number")
                       and dvs.get("first_name"))
        if (self.settings.ask_hold and state.get("round_asked")
                and not chain_ready):
            held: list[str] = list(state.get("held_facts") or [])
            for t in (state.get("last_round_tool_calls") or []):
                n = t.get("name") or ""
                if (t.get("status") == "ok" and not _MECHANICAL_RE.match(n)
                        and not n.startswith("transition_to_")):
                    held.append(f"the {n} result is ready")
            if fired_leak:
                held.append("the leak math result is ready")
            if fired_date:
                held.append("today's date is available")
            if held:
                held_out = held
                if self.tracer:
                    self.tracer.span("ask_hold:hold", output={"held": held,
                                                              "state": state_name})

        # iter33 Phase B: engine-owned booking chain. The model confirmed the
        # slot in conversation; everything past slot_verified is fixed-order
        # plumbing. One deterministic pass, resumable (each step skips if its
        # flag is set), fillers between the slow webhooks, abort -> model repair.
        if chain_ready:
            try:
                writer = get_stream_writer()
            except Exception:
                writer = None
            fillers: list[str] = list(state.get("fillers_said") or [])
            executed: list[dict] = []
            uid = dvs.get("booking_uid", "")
            plan = [("validate_lead", "ConfirmSlots", not dvs.get("slot_verified")),
                    ("transition_to_VerifyLead", "ConfirmSlots", state_name == "ConfirmSlots"),
                    ("verify_lead_data", "VerifyLead", not dvs.get("data_verified")),
                    ("transition_to_Booking", "VerifyLead", state_name in ("ConfirmSlots", "VerifyLead")),
                    ("create_livecall_booking", "Booking", not uid),
                    ("record_booking_uid", "Booking", not dvs.get("booking_verified")),
                    ("record_booking_outcome", "Booking", not dvs.get("booking_verified")),
                    ("transition_to_Closing", "Booking", True)]
            # iter87 VERSION B fire law (chain): every transition_to_*
            # destination in the plan warms at chain start — the warm lands
            # inside the webhook idle (1.5-4.5 s) the caller already
            # tolerates. ALL destinations (not just Closing) so an abort's
            # repair round in a mid-chain state holds on a landed warm too
            # (owner-ratified 2026-09-30). state["history"] is byte-exact
            # for the post-chain ack (the chain appends no history).
            if self.settings.warm_await_dest_ms > 0:
                for _step, _home, _needs in plan:
                    if _step.startswith("transition_to_"):
                        _dest = _step[len("transition_to_"):]
                        if _dest in self.states:
                            self.warm_prompt_cache(
                                _dest, list(state.get("history") or []),
                                dvs=dict(dvs))
            cur_state = state_name
            patch["chain_done"] = True        # idempotency latch (popped on abort)
            emitted_filler = False            # iter41 T2: max ONE filler per pass
            for step, home, needs in plan:
                if not needs:
                    continue
                if step == "validate_lead" and dvs.get("slot_verified"):
                    continue                  # model already validated this pass
                if (writer and step in _PHASEB_SLOW and not emitted_filler
                        and not self.settings.ask_hold):
                    # iter105 T4: ask_hold=True — the chain emits NO filler
                    # (single filler producer lives in the slots round only);
                    # ASK_HOLD=false keeps the legacy iter41 T2 arm.
                    for line in _ENGINE_FILLERS:
                        if line not in fillers:
                            writer({"tts_token": line + " "})
                            fillers.append(line)
                            emitted_filler = True
                            break
                args = _chain_args(step, dvs, uid)
                if step == "record_booking_outcome":
                    args = {"booking_verified": True}   # engine asserts only on this path
                outcome = await self.executor.execute(step, args, dvs, home)
                status = outcome.response.get("status", "ok" if outcome.response.get("ok") else "error")
                executed.append({"name": step, "ok": status == "ok"})
                patch.update(outcome.dvs_patch)
                dvs.update(outcome.dvs_patch)   # same-round visibility
                if outcome.new_state:
                    cur_state = outcome.new_state
                if step == "create_livecall_booking" and outcome.response.get("booking_uid"):
                    uid = outcome.response["booking_uid"]
                if status in _PHASEB_ABORT:
                    patch.pop("chain_done", None)
                    patch["chain_aborted_step"] = step
                    patch["chain_aborted_msg"] = json.dumps(outcome.response)[:600]
                    if self.tracer:
                        self.tracer.span("phaseb:chain_abort", output={"step": step, "resp": outcome.response})
                    return {"dvs": patch, "engine_fired": True, "state_name": cur_state,
                            "fillers_said": fillers, "last_round_tool_calls": executed}
            # iter87 VERSION B: re-fire the final destination with
            # POST-PATCH dvs bytes (force=True supersedes the chain-start
            # warm in _latest_warm — the Closing ack's hold target).
            # GATED on the hold knob (knob law: 0 = byte-exact iter85).
            if self.settings.warm_await_dest_ms > 0:
                self.warm_prompt_cache(
                    "Closing", list(state.get("history") or []),
                    dvs=dict(dvs), force=True)
            if self.tracer:
                self.tracer.span("phaseb:chain_ok", output={"steps": [e["name"] for e in executed]})
            return {"dvs": patch, "engine_fired": True, "state_name": "Closing",
                    "fillers_said": fillers, "last_round_tool_calls": executed,
                    "metrics": {"phaseb_chain": [e["name"] for e in executed]}}

        # iter29: engine_fired MUST be reset every pass (a stale True from an
        # earlier pass would mask fire-and-forget one-trip finalization)
        if not patch and not fired and not held_out:
            return {"engine_fired": False}
        out: dict[str, Any] = {"dvs": patch, "engine_fired": fired}
        if held_out:
            out["held_facts"] = held_out
        return out

    # ------------------------------------------------------------------ #
    def _build(self, checkpointer=None):
        async def ingest(state: GraphState) -> dict:
            # iter38 F4 (defense-in-depth): idempotent user-append. Any
            # cancel/rerun path in the session layer must never duplicate the
            # user utterance in history again (2026-09-09 disaster call: ~40
            # duplicated user turns). Bookkeeping (turn_index etc.) still
            # advances so orchestration reruns stay visible in metrics.
            patch: dict[str, Any] = {
                "turn_active": True,
                "turn_spoke": False,
                "round_spoke": False,
                "engine_fired": False,
                "rounds_left": self.settings.max_tool_rounds,
                "last_round_tool_calls": [],
                # iter62: keep the turn transcript — state_node consumes
                # state["user_text"]; the next turn's payload overwrites it.
                "user_text": state.get("user_text", ""),
                # iter41 T1: turn-scoped reset — the cross-round dedupe
                # window never leaks across turns.
                "tts_sentence_window": [],
                "turn_index": state.get("turn_index", 0) + 1,
            }
            # iter112 M2: the caller spoke first — AWAIT the previous round's
            # background TOOLS future BEFORE this turn's build (await-on-
            # arrival, NO caps: a required dependency; gap 1-15 s vs tools
            # ~1.5-2.5 s -> usually alread_done). Its calls execute HERE so
            # the tool results ride the same payload as the caller's
            # utterance ("speak fast, act smart"). Tool messages append
            # BEFORE the new user entry (assistant-tool_calls adjacency for
            # the API); a deferred transition applies dvs/state_name/warms
            # exactly like the round loop would have.
            _deferred_msgs: list[dict] = []
            pend = self._pending_tools
            if pend is not None:
                self._pending_tools = None
                _res: dict | None = None
                _m2_t0 = time.perf_counter()
                try:
                    if pend.task.done() and not pend.task.cancelled():
                        _res = pend.task.result()
                        _m2_how = "already_done"
                    elif pend.task.cancelled():
                        _m2_how = "cancelled"
                    else:
                        _res = await pend.task
                        _m2_how = "awaited"
                except Exception as exc:
                    log.warning("split tools task failed (M2): %s", exc)
                    _res, _m2_how = None, "error"
                if _res and _res.get("tool_calls"):
                    _dvs2 = DynamicVariables.from_flat(state.get("dvs", {})).to_flat()
                    _deferred_msgs, _dvs_patch, _new_state, _ended = \
                        await self._execute_deferred_tools(
                            _res["tool_calls"], _dvs2, pend.state_name, state)
                    log.info("split M2: turn=%s state=%s how=%s waited_ms=%s "
                             "tools=%s new_state=%s",
                             state.get("turn_index", 0) + 1, pend.state_name,
                             _m2_how,
                             round((time.perf_counter() - _m2_t0) * 1000),
                             [tc["name"] for tc in _res["tool_calls"]],
                             _new_state)
                    # iter114 T1: derive-on-write — M2-written inputs never
                    # reach the deterministic node (ingest routes straight to
                    # the state node); derive them HERE, before the next
                    # round's payload builds (closes the E4c t9→t11 window).
                    if self.settings.derive_on_write:
                        _merged = {**state.get("dvs", {}), **_dvs_patch}
                        _derived, _ = self._derive(
                            _merged, span_name="derive:leak_math")
                        if _derived:
                            _dvs_patch = {**_dvs_patch, **_derived}
                    if _dvs_patch:
                        patch["dvs"] = _dvs_patch
                    if _new_state:
                        patch["state_name"] = _new_state
                        if _new_state == "ConfirmSlots":
                            self.warm_slots_async(_dvs2)
                        self.warm_prompt_cache(
                            _new_state,
                            list(state.get("history", [])) + _deferred_msgs,
                            dvs=dict(_dvs2), force=True)
                        if self.settings.warm_await_dest_ms > 0:
                            for succ in self._state_successors.get(_new_state,
                                                                   ()):
                                self.warm_prompt_cache(
                                    succ,
                                    list(state.get("history", [])) + _deferred_msgs,
                                    dvs=dict(_dvs2))
                        self.warm_rag_async(_new_state, dvs=dict(_dvs2))
                    if _ended:
                        patch["ended"] = True
                else:
                    log.info("split M2: turn=%s how=%s — pending future "
                             "consumed with no tool calls",
                             state.get("turn_index", 0) + 1, _m2_how)
            text = state.get("user_text", "")
            last_user = next((m for m in reversed(state.get("history", []))
                              if m.get("role") == "user"), None)
            identical = (text and last_user is not None
                         and last_user.get("content", "") == text)
            if not identical:
                if state.get("user_text_supersedes") and last_user is not None:
                    # iter104 PT-1 (FIND-37): the confirmed final REPLACES the
                    # previous variant of THIS utterance (the eager fragment
                    # or a prior grown final) — one user entry per utterance.
                    # The merge_history marker pops the trailing user entry
                    # before appending this one.
                    patch["history"] = _deferred_msgs + [
                        {"role": "user", "content": text, "supersedes": True}]
                else:
                    patch["history"] = _deferred_msgs + [
                        {"role": "user", "content": text}]
            elif _deferred_msgs:
                patch["history"] = _deferred_msgs
            # iter104 PT-1: the flag is turn-scoped — consumed HERE so no
            # later turn can inherit a stale supersede.
            patch["user_text_supersedes"] = False
            # iter40 C3: freeze the state-block bytes for THIS turn — every
            # round of the turn reuses them (prefix shared across rounds →
            # prompt-cache hits). Refresh happens at the NEXT turn's ingest.
            # iter56 (C2.4): INERT under state_in_delta — the pre-history
            # tail is empty in the new layout and nothing reads the frozen
            # bytes (the state rides the live delta every round). Kept for
            # the revert path (STATE_IN_DELTA=false); gated to skip the
            # dead write.
            if self.settings.frozen_state_block \
                    and not getattr(self.settings, "state_in_delta", False):
                dvs_flat = DynamicVariables.from_flat(state.get("dvs", {})).to_flat()
                patch["frozen_state_block"] = self._state_block(dvs_flat)
            return patch

        async def finalize(state: GraphState) -> dict:
            return {"turn_active": False}

        def route_state(state: GraphState) -> str:
            if state.get("ended"):
                return "finalize"
            name = state.get("state_name", self.llm_json.get("starting_state", "Intake"))
            return name if name in self.states else "finalize"

        def after_state(state: GraphState) -> str:
            if state.get("ended"):
                return "finalize"
            executed = state.get("last_round_tool_calls") or []
            if not executed:
                return "finalize"                    # pure speech round -> turn done
            if state.get("rounds_left", 0) <= 0:
                return "finalize"
            return "deterministic"                   # iter29: tool rounds ALWAYS pass the engine

        # iter29 ONE-TRIP routing: speech + all fire-and-forget bookkeeping tools
        # finalizes the turn in a single LLM trip; result-dependent tools and the
        # silence guard loop back into the state node.
        FIRE_AND_FORGET = re.compile(
            r"^(extract_|record_reach_details|set_callback_number|intake_completed|"
            r"discovery_completed|closer_completed|offer_completed|"
            r"contact_details_completed|record_booking_outcome|memory_note)")

        def route_after_deterministic(state: GraphState) -> str:
            if state.get("ended"):
                return "finalize"
            executed = state.get("last_round_tool_calls") or []
            # iter105 T2 (ask-hold defer gate): a round carrying deferred
            # calls never produces continuation speech — if it SPOKE, the
            # turn waits for the caller (finalize); if it was SILENT, loop
            # ONCE so the model reads the deferred message and speaks the
            # ask. Bounded by rounds_left exactly like the result loops.
            if self.settings.ask_hold and any(
                    t.get("status") == "deferred" for t in executed):
                return "finalize" if state.get("round_spoke") else route_state(state)
            # iter105 T3 (ask-hold hold-release): the deterministic pass just
            # parked engine news / result tools because the trigger round asked
            # a question — finalize; the ENGINE note fires at the next round.
            if self.settings.ask_hold and state.get("held_facts"):
                return "finalize"
            if state.get("engine_fired"):
                return route_state(state)            # engine has news -> model speaks it
            # iter40 L3: an ok transition_to_* counts as terminal bookkeeping —
            # a spoken round that ONLY books/transitions finalizes in one trip.
            # gate_failed keeps the repair loop (status != "ok" → loops below).
            if self.settings.one_trip and executed and all(
                    FIRE_AND_FORGET.match(t["name"])
                    or (t["name"].startswith("transition_to_")
                        and t.get("status") == "ok")
                    for t in executed):
                if state.get("round_spoke"):
                    return "finalize"                # ONE TRIP: speech + bookkeeping/ok-transition
            if state.get("round_spoke") and executed and \
                    all(FIRE_AND_FORGET.match(t["name"]) for t in executed):
                return "finalize"                    # ONE TRIP: speech + bookkeeping, done
            return route_state(state)                # silence guard / result-dependent loop

        builder = StateGraph(GraphState)
        builder.add_node("ingest", ingest)
        for name in self.states:
            builder.add_node(name, self._make_state_node(name))
        builder.add_node("deterministic", self._deterministic_node)
        builder.add_node("finalize", finalize)

        builder.add_edge(START, "ingest")
        builder.add_conditional_edges("ingest", route_state,
                                      {**{n: n for n in self.states}, "finalize": "finalize"})
        for name in self.states:
            builder.add_conditional_edges(name, after_state,
                                          {**{n: n for n in self.states},
                                           "deterministic": "deterministic",
                                           "finalize": "finalize"})
        # deterministic layer routes back into the (possibly new) state node
        # (iter29: route_after_deterministic owns the one-trip decision)
        builder.add_conditional_edges("deterministic", route_after_deterministic,
                                      {**{n: n for n in self.states}, "finalize": "finalize"})
        builder.add_edge("finalize", END)
        return builder.compile(checkpointer=checkpointer or MemorySaver())

    # ------------------------------------------------------------------ #
    def initial_state(self, call_id: str, persona_dvs: dict | None = None) -> GraphState:
        self._rag_cache.clear()             # iter29: cache is per call, never across calls
        # iter31 C3: the RAG freeze is per call too (a fresh call re-retrieves
        # on its first state visit)
        self._frozen_state = None
        self._frozen_chunks = []
        # iter44: drift stash + staging are per call as well
        self._frozen_query_vec = None
        self._frozen_query_text = ""
        self._rag_staging.clear()
        self._rag_staging_tasks.clear()
        self._latest_warm.clear()          # iter46: warm tasks are per call
        self._warm_diag.clear()            # iter55 T0: warm failure diag is per call
        self._drift_tasks.clear()
        # iter49 T6: state-entry trigger baseline is per call
        self._prev_round_state = None
        # iter52: the pinned industry vertical is per call too
        self._pinned_industry = {}
        # iter103 PT-5: the rag query memo dies with the call
        self._rag_query_memo = {}
        return initial_state(call_id, self.llm_json, persona_dvs)


def build_checkpointer(settings: Settings):
    """Optional Postgres checkpointer (multi-worker / restart durability).
    Set LANGGRAPH_CHECKPOINT=postgres and DATABASE_URL=postgresql://...
    Falls back to per-call MemorySaver when the package or DB is unavailable."""
    if settings.checkpoint_backend != "postgres":
        return None
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        async def _make():
            saver = AsyncPostgresSaver.from_conn_string(settings.database_url)
            await saver.setup()                      # one-time migrations
            return saver
        return _make()
    except Exception as exc:
        print(f"[checkpoint] postgres unavailable ({exc}); using MemorySaver")
        return None
