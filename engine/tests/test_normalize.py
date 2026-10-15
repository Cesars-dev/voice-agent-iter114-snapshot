"""TTS speech normalization — the Retell-native layer, unit-tested.

The "665657 ramble" problem: bare digit runs must come out comma-separated;
phone-shaped strings digit-by-digit; markdown/emoji stripped; money/dates/
times expanded to owner-picked words (iter103 PT-6 — digits alone were read
too fast, FIND-40).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.media.normalize import normalize_for_tts


# --------------------------------------------------------------------------- #
def test_bare_digit_run_reads_slow():
    assert normalize_for_tts("my code is 665657.") == "my code is 6, 6, 5, 6, 5, 7."
    assert normalize_for_tts("code 665657") == "code 6, 6, 5, 6, 5, 7."


def test_phone_shapes_go_digit_by_digit():
    out = normalize_for_tts("call me back at 312-400-1234.")
    assert out == "call me back at 3, 1, 2, 4, 0, 0, 1, 2, 3, 4."
    out = normalize_for_tts("(312) 400-1234 works")
    assert out == "3, 1, 2, 4, 0, 0, 1, 2, 3, 4 works."
    out = normalize_for_tts("+1 312 400 1234 is best")
    assert out == "3, 1, 2, 4, 0, 0, 1, 2, 3, 4 is best."
    out = normalize_for_tts("+13124001234 ok")
    assert out == "3, 1, 2, 4, 0, 0, 1, 2, 3, 4 ok."     # country code dropped


def test_money_dates_times_expanded():
    # iter103 PT-6: owner-picked numeral expansion (was: left as written —
    # the fast-number disease, FIND-40: the caller asked "can you slow down?")
    assert normalize_for_tts("That is about $6,500 a week.") == \
        "That is about six thousand five hundred dollars a week."
    assert normalize_for_tts("See you 09/04/2026.") == \
        "See you September fourth twenty twenty-six."
    assert normalize_for_tts("It costs $19.99.") == \
        "It costs nineteen ninety-nine."
    assert normalize_for_tts("Call at 2:45 PM.") == "Call at two forty-five PM."


def test_years_and_short_numbers_untouched():
    assert normalize_for_tts("In 2026 we grew.") == "In 2026 we grew."
    assert normalize_for_tts("Job value 650.") == "Job value 650."
    assert normalize_for_tts("close rate of 50%.") == "close rate of 50%."


def test_markdown_and_emoji_stripped():
    out = normalize_for_tts("**Sure**, it is _3124001234_.")
    assert "*" not in out and "_" not in out
    assert "3, 1, 2, 4, 0, 0, 1, 2, 3, 4" in out
    out = normalize_for_tts("Great! 🎉 Let's talk.")
    assert "🎉" not in out
    assert out == "Great! Let's talk."


def test_booking_uid_char_by_char():
    out = normalize_for_tts("Your booking is qeTqHuZ1EDzH8bxEdhPQ6H.")
    assert ", " in out and "qeTqHuZ1EDzH8bxEdhPQ6H" not in out


def test_terminal_punctuation_added():
    assert normalize_for_tts("One moment").endswith(".")
    assert normalize_for_tts("One moment.").endswith(".")
    assert normalize_for_tts("Right?") == "Right?"


def test_spell_mode_wraps_cartesia_tags():
    out = normalize_for_tts("code 665657.", phone_style="spell")
    assert "<spell>665657</spell>" in out


def test_natural_mode_leaves_phone_written():
    out = normalize_for_tts("call me at (312) 400-1234.", phone_style="natural")
    assert "(312) 400-1234" in out
    # long bare runs still split
    out = normalize_for_tts("id 66565723.", phone_style="natural")
    assert "6, 6, 5, 6, 5, 7, 2, 3" in out


def test_idempotent_on_already_normalized():
    once = normalize_for_tts("code 665657")
    twice = normalize_for_tts(once)
    assert once == twice


def test_empty_and_whitespace_safe():
    assert normalize_for_tts("") == ""
    assert normalize_for_tts("   ") == "   "
