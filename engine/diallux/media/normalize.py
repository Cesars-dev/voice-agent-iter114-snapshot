"""Speech normalization for TTS — the Retell-native feature, self-hosted.

Retell normalizes numbers/addresses before speaking (its platform layer does
it for you). Raw LangGraph has no such layer, so an LLM that writes "my
number is 3124001234" would be read at full speed by Cartesia/ElevenLabs —
the "665657 ramble" problem. This module is the deterministic fix, applied
to every sentence right before it hits the TTS socket (never to history,
never to dvs, never to the prompt).

Provider research that shaped the rules (docs.cartesia.ai, 2026-08-14):
  - Sonic reads CONVENTIONAL written forms naturally: "(415) 555-1212",
    "$19.99", "04/20/2025", "user@example.com". Pre-normalization is only
    recommended for edge cases.
  - For digit-by-digit read-out Sonic 3.5 wants space/comma delimiters:
    "A B C, 1 2 3" (NOT the Sonic-3 "A. B. C." style), or <spell> tags.
  - ElevenLabs has no <spell>; comma delimiters work on both providers.
  - Both providers read markdown/emoji literally — strip them.

Our policy (tts_phone_style):
  "digits"  (default) phone-like and long digit runs -> "3, 1, 2, 4, 0, 0, 1, 2, 3, 4"
            slow, unambiguous, provider-agnostic — what an SDR confirming a
            number wants.
  "natural" leave phone-shaped strings as written ("(312) 400-1234" is read
            correctly by both engines); still split bare 7+ digit runs.
  "spell"   Cartesia-only: wrap runs in <spell> tags (works on one sentence
            chunk at a time; our gate never splits a sentence).

iter103 PT-6 adds a numeral arm BEFORE the phone/digit arms: $ amounts,
dates, times expand to owner-picked words ("$1,250" -> "twelve hundred
fifty dollars", "04/20/2026" -> "April twentieth twenty twenty-six",
"2:45 PM" -> "two forty-five PM") — raw digits were read too fast
(FIND-40). Everything else falls through byte-untouched.

Everything here is pure regex — zero latency (µs), zero API calls.
"""
from __future__ import annotations

import re

# markdown symbols that TTS engines would read aloud or stutter on
_MD_CHARS = re.compile(r"[*_`~>#|]+")
# emoji & symbol ranges (keep Latin-1 accents for names like José)
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\U0000FE0F\U0000200D\U00002190-\U000021FF\U00002B00-\U00002BFF]+")
# phone-shaped candidate: digits + separators, filtered by digit count >= 10
_PHONE_CAND_RE = re.compile(r"\+?\(?[\d][\d\s().-]{8,}\d\b(?![\w])")
# bare digit runs (665657, booking numbers, readouts)
_DIGIT_RUN_RE = re.compile(r"\d{5,}")
# mixed alnum tokens (qeTqHuZ1EDzH8bxEdhPQ6H) read best char-by-char
_MIXED_TOKEN_RE = re.compile(r"\b(?=\w*\d)(?=\w*[A-Za-z])\w{8,}\b")
_TERMINAL = ".!?…"


def _digits_out(digit_str: str) -> str:
    return ", ".join(digit_str)


def _spell(digits: str) -> str:
    return f"<spell>{digits}</spell>"


# --- iter103 PT-6: numeral expansion (deterministic; speech-only layer) -----
# Owner ear picks 2026-10-10:
#   "$1,250" -> "twelve hundred fifty dollars"    (hundreds form: $1,100-1,999)
#   "$2,100" -> "two thousand one hundred dollars" (formal: >= $2,000)
#   "$19.99" -> "nineteen ninety-nine"
#   "04/20/2026" -> "April twentieth twenty twenty-six"
#   "2:45 PM" -> "two forty-five PM"; "14:30" -> "two thirty PM"
_WORDING = {
    "hundreds_band": (1100, 1999),   # hundreds form inside this band only
    "formal_from": 2000,             # >= this: formal decomposition
    "cents_colloquial_below": 100,   # < $100 with cents: "nineteen ninety-nine"
}
_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven",
         "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen",
         "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
         "eighty", "ninety"]
_ORDINAL_UNIT = {1: "first", 2: "second", 3: "third", 4: "fourth",
                 5: "fifth", 6: "sixth", 7: "seventh", 8: "eighth",
                 9: "ninth"}
_ORDINAL_TEEN = {10: "tenth", 11: "eleventh", 12: "twelfth",
                 13: "thirteenth", 14: "fourteenth", 15: "fifteenth",
                 16: "sixteenth", 17: "seventeenth", 18: "eighteenth",
                 19: "nineteenth"}
_MONTHS = ["", "January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December"]
_MONEY_RE = re.compile(r"\$\s?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{2})?")
_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b")
_TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})(?:\s?(am|pm|a\.m\.|p\.m\.))?\b",
                      re.I)


def _under_100(n: int) -> str:
    if n < 20:
        return _ONES[n]
    t, o = divmod(n, 10)
    return _TENS[t] + (f"-{_ONES[o]}" if o else "")


def _number_to_words(n: int) -> str:
    """0 .. 999,999,999 — standard decomposition (no 'and')."""
    if n == 0:
        return "zero"
    parts: list[str] = []
    for div, name in ((1_000_000, "million"), (1_000, "thousand")):
        if n >= div:
            q, n = divmod(n, div)
            parts.append(f"{_number_to_words(q)} {name}")
    if n >= 100:
        q, n = divmod(n, 100)
        parts.append(f"{_under_100(q)} hundred")
    if n:
        parts.append(_under_100(n))
    return " ".join(parts)


def _hundreds_words(n: int) -> str:
    """1100-1999 owner form: 'twelve hundred fifty'."""
    h, r = divmod(n, 100)
    return f"{_ONES[h]} hundred" + (f" {_under_100(r)}" if r else "")


def _cents_words(c: int) -> str:
    if c < 10:
        return f"oh {_ONES[c]}"
    return _under_100(c)


def _money_sub(m: re.Match) -> str:
    raw = m.group(0).replace("$", "").replace(",", "").replace(" ", "")
    whole, _, frac = raw.partition(".")
    n = int(whole) if whole else 0
    c = int(frac) if frac else 0
    lo, hi = _WORDING["hundreds_band"]
    dollars = _hundreds_words(n) if lo <= n <= hi else _number_to_words(n)
    if not c:
        return f"{dollars} dollars"
    if n < _WORDING["cents_colloquial_below"]:
        return f"{dollars} {_cents_words(c)}"
    return f"{dollars} dollars and {_cents_words(c)} cents"


def _ordinal_words(d: int) -> str:
    if d <= 9:
        return _ORDINAL_UNIT[d]
    if d <= 19:
        return _ORDINAL_TEEN[d]
    if d % 10 == 0:
        return _TENS[d // 10][:-1] + "ieth"
    return f"{_TENS[d // 10]}-{_ORDINAL_UNIT[d % 10]}"


def _year_words(y: int) -> str:
    if 2000 <= y <= 2099:
        return "two thousand" if y == 2000 else f"twenty {_under_100(y - 2000)}"
    if 1900 <= y <= 1999:
        return (f"nineteen {_under_100(y - 1900)}" if y > 1900
                else "nineteen hundred")
    return _number_to_words(y)


def _date_sub(m: re.Match) -> str:
    month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if year < 100:
        year += 2000
    if not (1 <= month <= 12) or not (1 <= day <= 31):
        return m.group(0)             # not a date — fall through untouched
    return f"{_MONTHS[month]} {_ordinal_words(day)} {_year_words(year)}"


def _time_sub(m: re.Match) -> str:
    hour, minute = int(m.group(1)), int(m.group(2))
    marker = (m.group(3) or "").lower().replace(".", "")
    if hour > 23 or minute > 59:
        return m.group(0)             # not a time — fall through untouched
    if marker:
        h12 = hour % 12 or 12
        suffix = "PM" if marker.startswith("p") else "AM"
    elif hour == 0:
        h12, suffix = 12, "AM"
    elif hour <= 12:
        h12, suffix = hour, ("PM" if hour == 12 else "")
    else:
        h12, suffix = hour - 12, "PM"
    if minute == 0:
        words = _ONES[h12]
    elif minute < 10:
        words = f"{_ONES[h12]} {_cents_words(minute)}"
    else:
        words = f"{_ONES[h12]} {_under_100(minute)}"
    return f"{words} {suffix}".strip()


def normalize_for_tts(
    text: str,
    phone_style: str = "digits",
    strip_markdown: bool = True,
) -> str:
    """Normalize one TTS-bound sentence chunk. Deterministic, order-stable.

    Steps:
      1. strip markdown symbols + emoji, collapse whitespace
      2. phone-shaped strings + bare 5+ digit runs -> comma-separated digits
         (or <spell> when phone_style="spell", or untouched when "natural")
      3. long mixed alnum tokens (booking UIDs) -> char-separated
      4. guarantee terminal punctuation (both engines pace on it)
    """
    if not text or not text.strip():
        return text
    out = text
    if strip_markdown:
        out = _MD_CHARS.sub("", out)
        out = _EMOJI_RE.sub("", out)
        out = re.sub(r"\s+", " ", out).strip()

    # iter103 PT-6: numerals BEFORE the phone/digit arms — matched
    # money/date/time tokens are consumed whole; everything else falls
    # through byte-untouched to the existing arms below.
    out = _MONEY_RE.sub(_money_sub, out)
    out = _DATE_RE.sub(_date_sub, out)
    out = _TIME_RE.sub(_time_sub, out)

    if phone_style == "spell":
        out = _PHONE_CAND_RE.sub(lambda m: _spell(re.sub(r"\D", "", m.group(0))), out)
        out = _DIGIT_RUN_RE.sub(lambda m: _spell(m.group(0)), out)
    elif phone_style == "natural":
        # phone-shaped strings stay as written; long BARE runs still split
        out = _DIGIT_RUN_RE.sub(lambda m: m.group(0) if len(m.group(0)) < 7
                                else _digits_out(m.group(0)), out)
    else:  # "digits" — the SDR default
        def _phone_sub(m: re.Match) -> str:
            if ", " in m.group(0):
                return m.group(0)                 # already normalized
            digits = re.sub(r"\D", "", m.group(0))
            if len(digits) < 10:
                return m.group(0)                 # not a phone; leave alone
            if len(digits) == 11 and digits.startswith("1"):
                digits = digits[1:]               # drop US country code
            return _digits_out(digits)
        out = _PHONE_CAND_RE.sub(_phone_sub, out)
        out = _DIGIT_RUN_RE.sub(lambda m: _digits_out(m.group(0)), out)

    out = _MIXED_TOKEN_RE.sub(lambda m: ", ".join(m.group(0)), out)
    if out and out[-1] not in _TERMINAL:
        out += "."
    return out
