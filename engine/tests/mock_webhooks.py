"""Mock webhook server for hermetic tests — the LIVE production contract
(slots.diallux-ai.site) simulated in-process via httpx.MockTransport.

Response shapes follow ENDPOINTS.md exactly, so tests exercise the real
response_variables -> dvs paths (slot_verified, data_verified, booking_uid,
phone_confirmed ...) without touching production.
"""
from __future__ import annotations

import json
import re
from datetime import date

import httpx

BOOKING_UID = "qeTqHuZ1EDzH8bxEdhPQ6H"

# iter41 T3: mock "today" anchor — every date-aware day-word renders relative
# to this (mirrors the live endpoint anchoring _fetch_slots to the requested
# slot_target_date instead of always serving "today").
MOCK_TODAY = date(2026, 9, 4)


def _day_word(d: date) -> str:
    """Day-word logic per the live endpoint: today / tomorrow / weekday name;
    m/dd only beyond a week (same-day join drops m/dd per iter40)."""
    delta = (d - MOCK_TODAY).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if 2 <= delta <= 6:
        return d.strftime("%A").lower()
    return d.strftime("%m/%d")

# ---------------------------------------------------------------------------
# iter28 human-time contract mirror (v1 cal_slots_endpoint/main.py). The mock
# implements the SAME canonicalizer + containment gate as the live services —
# any drift here hides real regressions (R6 blind-spot class).
# ---------------------------------------------------------------------------

_CANON_SYNONYMS = [(re.compile(r"\bnoon\b"), "12:00 pm"), (re.compile(r"\bmidnight\b"), "12:00 am")]
_CANON_AT_RE = re.compile(r"\bat (\d{1,2})(?::(\d{2}))? ?(am|pm)?\b")
_CANON_STANDALONE_RE = re.compile(r"(?<![\d:])(\d{1,2}) (am|pm)\b")
_CANON_FILLER_RE = re.compile(r"\b(around|about) ")
_CANON_DATE_RE = re.compile(r"\b\d{1,2}/\d{1,2}\b")
_DAY_WORD_RE = re.compile(r"^(today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b\s*")


def canonical(value: str) -> str:
    """Mirror of v1 _norm_human / validator canonical() — keep in lockstep."""
    t = re.sub(r"\s+", " ", str(value or "").strip().lower()).replace(",", "")
    for pat, repl in _CANON_SYNONYMS:
        t = pat.sub(repl, t)
    t = _CANON_AT_RE.sub(
        lambda m: f"{m.group(1)}:{m.group(2) or '00'} {m.group(3) or ('pm' if int(m.group(1)) <= 6 else 'am')}",
        t,
    )
    t = _CANON_STANDALONE_RE.sub(lambda m: f"{m.group(1)}:00 {m.group(2)}", t)
    t = _CANON_FILLER_RE.sub("", t)
    t = _CANON_DATE_RE.sub("", t)
    return re.sub(r"\s+", " ", t).strip()


def _contained(pick: str, options: str) -> bool:
    """iter28 gate: canonical pick substring-contained in canonical options.
    A leading day word is also strippable when that SAME day word appears in
    the options (same-day short offers don't repeat the day on the 2nd time).
    Unoffered day words never strip -> invented slots still bounce."""
    pc, oc = canonical(pick), canonical(options)
    if pc and pc in oc:
        return True
    m = _DAY_WORD_RE.match(pc)
    if m and m.group(1) in oc:
        return pc[m.end():] in oc
    return False


_TIME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})")
_ISO_TIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(?::(\d{2}))?$")
_OFFERED = {"today": ("2026-09-04", {"13:00": "1 pm", "14:30": "2:30 pm",
                                     "12:00": "noon", "17:30": "5:30 pm"})}


def _human_pick(time_raw: str) -> str:
    """booked_human mirror: machine local wall time -> natural string (or '')."""
    m = _TIME_RE.match(str(time_raw or "").strip())
    if not m:
        return ""
    y, mo, dd, hh, mm = m.groups()
    day = "today" if f"{y}-{mo}-{dd}" == "2026-09-04" else "tomorrow"
    h, minute = int(hh), int(mm)
    ampm = "am" if h < 12 else "pm"
    if h == 12 and minute == 0:
        t = "noon"
    elif h == 0 and minute == 0:
        t = "midnight"
    elif minute == 0:
        t = f"{h % 12 or 12} {ampm}"
    else:
        t = f"{h % 12 or 12}:{minute:02d} {ampm}"
    return f"{day} at {t}"


def handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "/today" in url:
        return httpx.Response(200, json={
            "ok": True, "today": "2026-09-04", "timezone": "America/Mexico_City", "tz_abbrev": "CST"})
    if "/check_availability" in url:
        # iter41 T3 date-aware fixture: serve slots for the REQUESTED
        # slot_target_date (lockstep with the live endpoint, which anchors
        # _fetch_slots to that day). Legacy no-date calls → today (unchanged).
        # Post-iter40 format: same-day join with day words, NO m/dd.
        args = _args(request)
        raw = str(args.get("slot_target_date") or MOCK_TODAY.isoformat())[:10]
        try:
            target_day = date.fromisoformat(raw)
        except ValueError:
            target_day = MOCK_TODAY
        day_word = _day_word(target_day)
        d_iso = target_day.isoformat()
        # times = DATE at 13:00/14:30 local (iso = +5h, mock-tz CST parity)
        return httpx.Response(200, json={
            "ok": True,
            "slots": [
                {"day": d_iso, "time": f"{d_iso}T13:00:00",
                 "iso": f"{d_iso}T18:00:00.000Z", "reservation_uid": "2c4b75b4-546f-46fc-bb36-b3a600eb4c65",
                 "day_human": day_word, "time_human": "1 pm",
                 "human": f"{day_word} at 1 pm"},
                {"day": d_iso, "time": f"{d_iso}T14:30:00",
                 "iso": f"{d_iso}T19:30:00.000Z", "reservation_uid": "a1f76ae0-6009-4887-90d4-41794a7e949d",
                 "day_human": day_word, "time_human": "2:30 pm",
                 "human": f"{day_word} at 2:30 pm"},
            ],
            "taken": [], "reissued": False, "preferred_unavailable": False,
            "slot_reservation_uids": "2c4b75b4-546f-46fc-bb36-b3a600eb4c65,a1f76ae0-6009-4887-90d4-41794a7e949d",
            # iter40 endpoint lockstep: same-day join drops m/dd (the model read
            # "9/10" as "nine ten"); noon keeps its word (no "12 pm")
            "slots_human": f"{day_word} at 1 pm or 2:30 pm"})
    if "/validator-function/validate_lead" in url:
        args = _args(request)
        # live contract: the validator computes the leak from the SENT inputs
        # and echoes verified data — never a frozen fixture (that masked
        # per-run DVS as cross-run contamination on 2026-09-04).
        try:
            # verbatim contract from validator_endpoint/validate.py:205-212:
            # first_num extraction, clamps, int() truncation, 2-sig floor, 4.33;
            # ANY input missing -> null leak (executor skips None -> DVS keeps
            # the deterministic calculate_monthly_leak value).
            import re as _re

            def _num(v):
                if v is None or v == "":
                    return None
                m = _re.search(r"-?\d+(?:\.\d+)?", str(v).replace(",", ""))
                return float(m.group(0)) if m else None

            calls = _num(args.get("missed_calls_per_week"))
            close = _num(args.get("close_rate_percent"))
            job = _num(args.get("avg_job_value"))
            weekly_leak = monthly_leak = None
            if calls is not None and close is not None and job is not None:
                calls = max(0, min(calls, 500))
                close = max(0, min(close, 100))
                job = max(0, min(job, 100000))
                weekly = int(calls * (close / 100.0) * job)

                def _round_2sig(n: int) -> int:
                    n = int(n)
                    if n <= 0:
                        return 0
                    mag = 10 ** (len(str(n)) - 2)
                    return (n // mag) * mag

                weekly_leak = f"${_round_2sig(weekly):,}"
                monthly_leak = f"${_round_2sig(int(weekly * 4.33)):,}"
        except Exception:
            weekly_leak = monthly_leak = None
        # iter28 containment gate (mirror of validate.py): empty -> polite ask;
        # slot_options present -> pick must be contained, else apologetic Say-line.
        st = str(args.get("selected_time") or "").strip()
        problems, actions = [], []
        if not st or st.lower() in {"n/a", "na", "none", "unknown", "idk", "tbd", "-", "--", "?"}:
            problems.append({"field": "selected_time", "reason": "missing"})
            actions.append("Say: 'Which of those slots works best for you?'")
        else:
            slot_options = str(args.get("slot_options") or "").strip()
            if slot_options and not _contained(st, slot_options):
                problems.append({"field": "selected_time", "reason": f"not_offered:{st!r}"})
                actions.append("Say: 'Sorry, I lost track - which slot did you pick?'")
        data_complete = not problems
        return httpx.Response(200, json={
            "ok": True, "data_complete": data_complete, "slot_verified": data_complete,
            "problems": problems, "actions": actions,
            "weekly_leak": weekly_leak, "monthly_leak": monthly_leak, "inputs": args})
    if "/validator-function/verify-lead-data" in url:
        args = _args(request)
        # live contract (iter28): echo the lead data SENT for verification;
        # selected_time: ISO fast-path injects the fix; human pick passes ONLY
        # when contained in slot_options (inject nothing — booking resolves it);
        # anything else -> unfixable_format + apologetic Say-line.
        st = str(args.get("selected_time") or "").strip()
        selected_out: dict = {}
        problems, actions = [], []
        if not st or st.lower() in {"n/a", "na", "none", "unknown", "idk", "tbd", "-", "--", "?"}:
            problems.append({"field": "selected_time", "issue": "missing"})
            actions.append("Say: 'Which of those slots works best for you?'")
        elif _ISO_TIME_RE.match(st):
            _m = _ISO_TIME_RE.match(st)
            selected_out["selected_time"] = f"{_m.group(1)}T{_m.group(2)}:{_m.group(3) or '00'}"
        else:
            slot_options = str(args.get("slot_options") or "").strip()
            if slot_options and _contained(st, slot_options):
                pass  # contained: verify passes, no injection (booking-side resolution)
            else:
                problems.append({"field": "selected_time", "issue": "unfixable_format"})
                actions.append("Say: 'Sorry, I lost track - which slot did you pick?'")
        if problems:
            return httpx.Response(200, json={
                "ok": True, "status": "incomplete", "data_verified": False,
                "problems": problems, "actions": actions})
        return httpx.Response(200, json={
            "ok": True, "status": "ok", "data_verified": True, "problems": [], "actions": [],
            "first_name": args.get("first_name", ""),
            "callback_number": args.get("callback_number", ""),
            "prospect_timezone": args.get("prospect_timezone", ""),
            **selected_out})
    if "/book-livecall" in url:
        args = _args(request)
        if args.get("booking_intent") == "reschedule":
            return httpx.Response(200, json={
                "ok": True, "status": "reschedule_options", "reissued": True,
                "slots": [{"day": "2026-09-05", "time": "2026-09-05T13:00:00", "iso": "x", "reservation_uid": "r1",
                           "day_human": "tomorrow", "time_human": "1 pm", "human": "tomorrow at 1 pm"},
                          {"day": "2026-09-05", "time": "2026-09-05T15:00:00", "iso": "y", "reservation_uid": "r2",
                           "day_human": "tomorrow", "time_human": "3 pm", "human": "tomorrow at 3 pm"}],
                "slot_reservation_uids": "r1,r2", "slots_human": "tomorrow at 1 pm or 3 pm"})
        if args.get("booking_intent") == "cancel":
            return httpx.Response(200, json={"ok": True, "status": "released"})
        return httpx.Response(200, json={
            "ok": True, "status": "booked", "booking_uid": BOOKING_UID,
            "recovered": False, "booking_verified": True,
            "booked_human": _human_pick(args.get("time"))})
    if "/validator-function/record-booking-uid" in url:
        return httpx.Response(200, json={"ok": True, "booking_uid": BOOKING_UID})
    if "/validator-function/record-reach-details" in url:
        args = _args(request)
        # live contract (verify.py record_reach_details, iter14): a NO answer
        # NEVER writes phone_confirmed — only a YES mirrors it true.
        best = bool(args.get("is_calling_best_number"))
        body = {"status": "ok", "is_calling_best_number": best}
        if best:
            body["phone_confirmed"] = True
        return httpx.Response(200, json=body)
    if "/validator-function/set-callback-number" in url:
        return httpx.Response(200, json={"status": "ok", "callback_number": "+15123120001",
                                         "phone_confirmed": True})
    return httpx.Response(404, json={"ok": False, "error": "not_mocked", "url": url})


def _args(request: httpx.Request) -> dict:
    try:
        body = json.loads(request.content.decode() or "{}")
        return body.get("args", body)
    except Exception:
        return {}


def mock_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))
