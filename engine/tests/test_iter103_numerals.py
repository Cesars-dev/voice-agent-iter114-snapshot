"""iter103 PT-6 — numeral expansion pins (owner ear picks 2026-10-10).

Money/date/time expand to owner-worded text in `normalize_for_tts` BEFORE
the phone/digit arms; everything else falls through byte-untouched. Speech-
only layer: never to history, never to dvs, never to the prompt.

Owner picks:
  "$1,250 a week" -> "twelve hundred fifty dollars a week"  (1,100-1,999)
  "$2,100"        -> "two thousand one hundred dollars"      (>= $2,000)
  "$19.99"        -> "nineteen ninety-nine"
  "04/20/2026"    -> "April twentieth twenty twenty-six"
  "2:45 PM"       -> "two forty-five PM"; "14:30" -> "two thirty PM"
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.media.normalize import normalize_for_tts


def test_money_expansion():
    # the Oct-6 leak-math line shape: "$1,250 a week"
    assert normalize_for_tts("$1,250 a week") == \
        "twelve hundred fifty dollars a week."
    # owner band rule: hundreds form ONLY in 1,100-1,999
    assert normalize_for_tts("$1,000 upfront") == "one thousand dollars upfront."
    assert normalize_for_tts("$1,100 flat") == "eleven hundred dollars flat."
    assert normalize_for_tts("$1,999 flat") == "nineteen hundred ninety-nine dollars flat."
    # >= $2,000 -> formal decomposition
    assert normalize_for_tts("$2,100 once") == "two thousand one hundred dollars once."
    assert normalize_for_tts("$60,000 a week") == "sixty thousand dollars a week."
    # cents: colloquial under $100
    assert normalize_for_tts("$19.99") == "nineteen ninety-nine."
    # cents: formal at/above $100
    assert normalize_for_tts("$250.50") == \
        "two hundred fifty dollars and fifty cents."


def test_date_expansion():
    assert normalize_for_tts("04/20/2026") == "April twentieth twenty twenty-six."
    assert normalize_for_tts("We can do 4/20/26.") == \
        "We can do April twentieth twenty twenty-six."
    assert normalize_for_tts("12/25/2027!") == \
        "December twenty-fifth twenty twenty-seven!"


def test_time_expansion():
    assert normalize_for_tts("2:45 PM") == "two forty-five PM."
    assert normalize_for_tts("14:30") == "two thirty PM."
    assert normalize_for_tts("at 3:00pm") == "at three PM."
    assert normalize_for_tts("2:05") == "two oh five."


def test_order_stable_deterministic():
    sentence = "Pay $500 then $1,250 by 04/20/2026 at 2:45 PM."
    first = normalize_for_tts(sentence)
    for _ in range(100):
        assert normalize_for_tts(sentence) == first
    assert "five hundred dollars" in first
    assert "twelve hundred fifty dollars" in first
    assert first.index("five hundred dollars") < \
        first.index("twelve hundred fifty dollars")
    assert first.index("April twentieth") < first.index("two forty-five")


def test_terminal_punctuation_preserved():
    out = normalize_for_tts("The offer is $1,250.")
    assert out.endswith("dollars.") and out.count(".") == 1
    out = normalize_for_tts("Set for 2:45 PM?")
    assert out.endswith("PM?")


def test_fallthrough_parity():
    # inputs with NO money/date/time tokens: byte-identical to the pre-PT-6
    # arms (the regression gate) — exact literals from test_normalize.py
    assert normalize_for_tts("my code is 665657.") == "my code is 6, 6, 5, 6, 5, 7."
    assert normalize_for_tts("call me back at 312-400-1234.") == \
        "call me back at 3, 1, 2, 4, 0, 0, 1, 2, 3, 4."
    assert normalize_for_tts("In 2026 we grew.") == "In 2026 we grew."
    assert normalize_for_tts("Job value 650.") == "Job value 650."
    assert normalize_for_tts("**Sure**, it is _3124001234_.") == \
        "Sure, it is 3, 1, 2, 4, 0, 0, 1, 2, 3, 4."


def test_history_untouched_guard():
    # the speech-only contract: normalize_for_tts is referenced ONLY from the
    # session's speak path — never from the graph/history/dvs writers.
    import diallux

    root = Path(diallux.__file__).resolve().parent
    hits = []
    for p in root.rglob("*.py"):
        if p.name == "normalize.py":
            continue
        if "normalize_for_tts" in p.read_text(encoding="utf-8"):
            hits.append(str(p.relative_to(root)))
    assert hits == ["media/session.py"], hits
