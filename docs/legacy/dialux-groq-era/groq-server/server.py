import os
import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI()
anthropic_client = AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

MODEL = "claude-sonnet-4-6"
BEGIN_MESSAGE = "Hello! You have reached Dialux, this is Linda speaking, how may I help you today?"

CAL_API_KEY              = os.environ.get("CAL_API_KEY", "")
CAL_LIVE_EVENT_TYPE_ID   = int(os.environ.get("CAL_LIVE_EVENT_TYPE_ID", "0"))
CAL_CALLBACK_EVENT_TYPE_ID = int(os.environ.get("CAL_CALLBACK_EVENT_TYPE_ID", "0"))
CAL_BASE_URL             = "https://api.cal.com/v1"

TZ_MAP = {
    "Pacific":  "America/Los_Angeles",
    "Mountain": "America/Denver",
    "Central":  "America/Chicago",
    "Eastern":  "America/New_York",
}

# ── Prompts ────────────────────────────────────────────────────────────────

def _load(name: str) -> str:
    path = os.path.join(os.path.dirname(__file__), "prompts", name)
    with open(path) as f:
        return f.read().strip()

_HEADER = _load("system_header.txt")

PROMPTS: dict[str, str] = {
    state: _HEADER + "\n\n" + _load(f"{state}.txt")
    for state in ("discovery", "closer", "contact_details", "booking")
}

# ── Tool schemas (Anthropic format) ────────────────────────────────────────

TOOLS: dict[str, list] = {
    "discovery": [
        {
            "name": "extract_discovery_details",
            "description": (
                "Call this silently whenever you learn new information from the prospect: "
                "their industry, a phone challenge they describe, their call volume, "
                "or any signal of genuine interest (asking how it works, urgency language, "
                "pricing questions, or saying 'that's exactly what we need'). "
                "Only include fields you actually have new information for."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "industry": {
                        "type": "string",
                        "description": "Prospect's industry (e.g. 'real estate', 'HVAC', 'dental')",
                    },
                    "pain_points": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Phone/business challenges mentioned by the prospect",
                    },
                    "call_volume_per_day": {
                        "type": "integer",
                        "description": "Approximate inbound calls per day",
                    },
                    "interest_signal": {
                        "type": "boolean",
                        "description": "True if prospect has shown genuine interest in the solution",
                    },
                },
                "required": [],
            },
        }
    ],
    "closer": [
        {
            "name": "signal_deal_progress",
            "description": (
                "Call this when you've gathered the prospect's loss information or "
                "received a clear decision about the demo/callback."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "loss_type": {
                        "type": "string",
                        "enum": ["missed_lead", "missed_appointment", "missed_deal", "other"],
                    },
                    "loss_amount_per_unit": {
                        "type": "string",
                        "description": "Prospect's own dollar estimate in their words",
                    },
                    "agreed_to_demo": {
                        "type": "boolean",
                        "description": "True if prospect agreed to a live demo or callback",
                    },
                    "booking_preference": {
                        "type": "string",
                        "enum": ["demo", "callback"],
                        "description": "'demo' for live walkthrough, 'callback' for Jay to call them",
                    },
                    "disqualified": {
                        "type": "boolean",
                        "description": "True if prospect is not a good fit",
                    },
                },
                "required": [],
            },
        }
    ],
    "contact_details": [
        {
            "name": "collect_contact_details",
            "description": "Call this as you collect and verify the prospect's contact information.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "first_name":           {"type": "string"},
                    "last_name":            {"type": "string"},
                    "company":              {"type": "string"},
                    "phone_confirmed":      {"type": "boolean"},
                    "timezone": {
                        "type": "string",
                        "enum": ["Pacific", "Mountain", "Central", "Eastern"],
                    },
                    "all_details_verified": {
                        "type": "boolean",
                        "description": "True only after reading back all details and prospect confirmed",
                    },
                },
                "required": [],
            },
        }
    ],
    "booking": [
        {
            "name": "query_callback_slots",
            "description": (
                "Query available callback time slots (Jay calls the prospect). "
                "Use when the prospect prefers a callback or when timing is tight."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "timezone": {
                        "type": "string",
                        "enum": ["Pacific", "Mountain", "Central", "Eastern"],
                    },
                    "days_out": {"type": "integer", "description": "Days ahead to search (default 3, max 7)"},
                },
                "required": ["timezone"],
            },
        },
        {
            "name": "book_callback",
            "description": "Book a callback appointment once the prospect confirms a slot.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "slot_iso": {"type": "string", "description": "ISO 8601 UTC datetime from query_callback_slots"},
                    "name":     {"type": "string"},
                    "phone":    {"type": "string"},
                    "notes":    {"type": "string", "description": "Industry and main challenge"},
                },
                "required": ["slot_iso", "name"],
            },
        },
        {
            "name": "query_live_meeting_slots",
            "description": (
                "Query available live demo/walkthrough slots. "
                "Use when the prospect wants to see a live product walkthrough."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "timezone": {
                        "type": "string",
                        "enum": ["Pacific", "Mountain", "Central", "Eastern"],
                    },
                    "days_out": {"type": "integer", "description": "Days ahead to search (default 3, max 7)"},
                },
                "required": ["timezone"],
            },
        },
        {
            "name": "book_live_meeting",
            "description": "Book a live demo/walkthrough once the prospect confirms a slot.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "slot_iso": {"type": "string", "description": "ISO 8601 UTC datetime from query_live_meeting_slots"},
                    "name":     {"type": "string"},
                    "phone":    {"type": "string"},
                    "notes":    {"type": "string", "description": "Industry and main challenge"},
                },
                "required": ["slot_iso", "name"],
            },
        },
        {
            "name": "end_call",
            "description": "Call this immediately after saying goodbye to end the call.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
    ],
}

# ── Cal.com API calls ──────────────────────────────────────────────────────

async def _cal_query_slots(event_type_id: int, tz_label: str, days_out: int = 3) -> dict:
    iana_tz = TZ_MAP.get(tz_label, "America/New_York")
    now   = datetime.now(timezone.utc)
    start = now.isoformat()
    end   = (now + timedelta(days=days_out)).isoformat()
    params = {
        "apiKey":      CAL_API_KEY,
        "eventTypeId": event_type_id,
        "startTime":   start,
        "endTime":     end,
        "timeZone":    iana_tz,
    }
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(f"{CAL_BASE_URL}/slots", params=params)
            resp.raise_for_status()
            data = resp.json()
            slots = []
            for day_slots in data.get("slots", {}).values():
                for s in day_slots:
                    slots.append(s["time"])
            return {"ok": True, "slots": slots[:6], "timezone": iana_tz}
    except Exception as e:
        logger.error(f"Cal.com query_slots error: {e}")
        return {"ok": False, "error": str(e)}


async def _cal_create_booking(
    event_type_id: int, slot_iso: str, name: str, phone: str, notes: str, tz_label: str,
) -> dict:
    iana_tz  = TZ_MAP.get(tz_label, "America/New_York")
    start_dt = datetime.fromisoformat(slot_iso.replace("Z", "+00:00"))
    end_dt   = start_dt + timedelta(minutes=30)
    payload  = {
        "eventTypeId": event_type_id,
        "start":       start_dt.isoformat(),
        "end":         end_dt.isoformat(),
        "responses": {
            "name":  name,
            "email": "noreply@dialux.ai",
            "phone": phone or "",
            "notes": notes or "",
        },
        "timeZone": iana_tz,
        "language": "en",
        "metadata": {},
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{CAL_BASE_URL}/bookings",
                params={"apiKey": CAL_API_KEY},
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            return {"ok": True, "booking_id": data.get("id"), "uid": data.get("uid"), "start": slot_iso}
    except Exception as e:
        logger.error(f"Cal.com create_booking error: {e}")
        return {"ok": False, "error": str(e)}


# ── Booking helpers ───────────────────────────────────────────────────────

def _fmt_slots(slots: list[str], tz_label: str) -> list[str]:
    iana_tz = TZ_MAP.get(tz_label, "America/New_York")
    out = []
    for iso in slots:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        dt_local = dt.astimezone(ZoneInfo(iana_tz))
        out.append(dt_local.strftime("%A, %B %-d at %-I:%M %p"))
    return out

def _full_name(variables: dict) -> str:
    return f"{variables.get('first_name','') or ''} {variables.get('last_name','') or ''}".strip()

def _auto_notes(variables: dict) -> str:
    return f"Industry: {variables.get('industry','')}. Challenge: {', '.join(variables.get('pain_points') or [])}"


# ── Tool dispatcher ────────────────────────────────────────────────────────

async def _handle_tool(name: str, args: dict, variables: dict, state: str) -> tuple[str, str]:
    result: dict = {"status": "ok", "updated": {}}
    next_state = state

    if name == "extract_discovery_details":
        if args.get("industry"):
            variables["industry"] = args["industry"]
            result["updated"]["industry"] = args["industry"]
        if args.get("pain_points"):
            for p in args["pain_points"]:
                if p not in variables["pain_points"]:
                    variables["pain_points"].append(p)
            result["updated"]["pain_points"] = variables["pain_points"]
        if "call_volume_per_day" in args:
            variables["call_volume"] = args["call_volume_per_day"]
        if "interest_signal" in args:
            variables["interest_signal"] = args["interest_signal"]
        if variables["interest_signal"] and variables["industry"] and variables["pain_points"]:
            next_state = "closer"
            result["transition"] = "stage_2_closer"

    elif name == "signal_deal_progress":
        if args.get("loss_type"):
            variables["loss_type"] = args["loss_type"]
        if args.get("loss_amount_per_unit"):
            variables["loss_amount"] = args["loss_amount_per_unit"]
        if args.get("booking_preference"):
            variables["booking_preference"] = args["booking_preference"]
        if args.get("agreed_to_demo"):
            variables["agreed_to_demo"] = True
            next_state = "contact_details"
            result["transition"] = "stage_3_contact_details"
        if args.get("disqualified"):
            variables["disqualified"] = True
            result["status"] = "disqualified"

    elif name == "collect_contact_details":
        for field in ("first_name", "last_name", "company", "timezone"):
            if args.get(field):
                variables[field] = args[field]
                result["updated"][field] = args[field]
        if "phone_confirmed" in args:
            variables["phone_confirmed"] = args["phone_confirmed"]
        if args.get("all_details_verified"):
            variables["all_details_verified"] = True
            next_state = "booking"
            result["transition"] = "stage_4_booking"

    elif name == "query_callback_slots":
        tz = args.get("timezone") or variables.get("timezone") or "Eastern"
        days_out = min(int(args.get("days_out", 3)), 7)
        slot_data = await _cal_query_slots(CAL_CALLBACK_EVENT_TYPE_ID, tz, days_out)
        result.update(slot_data)
        if slot_data.get("slots"):
            result["slots_formatted"] = _fmt_slots(slot_data["slots"], tz)
            result["slots_iso"] = slot_data["slots"]

    elif name == "book_callback":
        tz = variables.get("timezone") or "Eastern"
        booking_result = await _cal_create_booking(
            event_type_id=CAL_CALLBACK_EVENT_TYPE_ID,
            slot_iso=args.get("slot_iso", ""),
            name=args.get("name") or _full_name(variables),
            phone=args.get("phone") or "",
            notes=args.get("notes") or _auto_notes(variables),
            tz_label=tz,
        )
        result.update(booking_result)
        if booking_result.get("ok"):
            variables["booking_confirmed"] = True
            variables["time_slot"] = args.get("slot_iso", "")

    elif name == "query_live_meeting_slots":
        tz = args.get("timezone") or variables.get("timezone") or "Eastern"
        days_out = min(int(args.get("days_out", 3)), 7)
        slot_data = await _cal_query_slots(CAL_LIVE_EVENT_TYPE_ID, tz, days_out)
        result.update(slot_data)
        if slot_data.get("slots"):
            result["slots_formatted"] = _fmt_slots(slot_data["slots"], tz)
            result["slots_iso"] = slot_data["slots"]

    elif name == "book_live_meeting":
        tz = variables.get("timezone") or "Eastern"
        booking_result = await _cal_create_booking(
            event_type_id=CAL_LIVE_EVENT_TYPE_ID,
            slot_iso=args.get("slot_iso", ""),
            name=args.get("name") or _full_name(variables),
            phone=args.get("phone") or "",
            notes=args.get("notes") or _auto_notes(variables),
            tz_label=tz,
        )
        result.update(booking_result)
        if booking_result.get("ok"):
            variables["booking_confirmed"] = True
            variables["time_slot"] = args.get("slot_iso", "")

    elif name == "end_call":
        variables["end_call"] = True
        result["action"] = "end_call"

    active_vars = {k: v for k, v in variables.items() if v}
    logger.info(f"Tool {name}: {state}→{next_state} | {json.dumps(active_vars)}")
    return json.dumps(result), next_state


# ── WebSocket endpoint ─────────────────────────────────────────────────────

@app.websocket("/llm-websocket/{call_id}")
async def websocket_endpoint(websocket: WebSocket, call_id: str):
    await websocket.accept()
    logger.info(f"Retell connected  call_id={call_id}")

    state = "discovery"
    variables: dict = {
        "industry": None,
        "pain_points": [],
        "interest_signal": False,
        "call_volume": None,
        "loss_type": None,
        "loss_amount": None,
        "agreed_to_demo": False,
        "booking_preference": None,
        "disqualified": False,
        "first_name": None,
        "last_name": None,
        "company": None,
        "phone_confirmed": False,
        "timezone": None,
        "all_details_verified": False,
        "booking_confirmed": False,
        "time_slot": None,
        "end_call": False,
    }

    # Anthropic message history — seed with begin_message so AI knows the greeting was already played
    messages: list[dict] = []
    greeting_sent = False
    last_transcript_len = 0
    response_id = 0

    try:
        while True:
            raw = await websocket.receive_text()
            request = json.loads(raw)

            interaction_type = request.get("interaction_type")
            response_id = request.get("response_id", response_id)
            transcript: list[dict] = request.get("transcript", [])

            logger.info(f"← {interaction_type}  id={response_id}  state={state}  tx={len(transcript)}")

            if interaction_type == "call_details":
                continue

            if interaction_type == "update_only":
                continue

            # Greeting always goes first — before processing any transcript turns
            if not greeting_sent:
                await websocket.send_text(json.dumps({
                    "response_type": "response",
                    "response_id": response_id,
                    "content": BEGIN_MESSAGE,
                    "content_complete": True,
                }))
                messages.append({"role": "assistant", "content": BEGIN_MESSAGE})
                greeting_sent = True
                last_transcript_len = len(transcript)  # skip any pre-greeting transcript entries
                logger.info(f"→ greeting id={response_id}")
                continue

            # ── Sync new user turns from Retell transcript
            new_turns = transcript[last_transcript_len:]
            new_user_turns = [t for t in new_turns if t["role"] == "user"]
            for turn in new_user_turns:
                messages.append({"role": "user", "content": turn["content"]})
                logger.info(f"  USER: {turn['content'][:120]}")
            last_transcript_len = len(transcript)

            if not new_user_turns:
                await websocket.send_text(json.dumps({
                    "response_type": "response",
                    "response_id": response_id,
                    "content": "",
                    "content_complete": True,
                }))
                logger.info(f"→ skip (no user turn) id={response_id}")
                continue

            # ── Anthropic streaming call
            text_chunks: list[str] = []
            tool_uses: list[dict] = []
            current_tool: dict | None = None

            async with anthropic_client.messages.stream(
                model=MODEL,
                max_tokens=300,
                system=PROMPTS[state],
                messages=messages,
                tools=TOOLS[state],
                temperature=1.0,
            ) as stream:
                async for event in stream:
                    if event.type == "content_block_start":
                        if event.content_block.type == "tool_use":
                            current_tool = {
                                "id": event.content_block.id,
                                "name": event.content_block.name,
                                "input_json": "",
                            }
                    elif event.type == "content_block_delta":
                        if event.delta.type == "text_delta":
                            chunk = event.delta.text
                            text_chunks.append(chunk)
                            await websocket.send_text(json.dumps({
                                "response_type": "response",
                                "response_id": response_id,
                                "content": chunk,
                                "content_complete": False,
                            }))
                        elif event.delta.type == "input_json_delta" and current_tool:
                            current_tool["input_json"] += event.delta.partial_json
                    elif event.type == "content_block_stop":
                        if current_tool:
                            tool_uses.append(current_tool)
                            current_tool = None

            # ── Tool call path
            if tool_uses and not text_chunks:
                # Build assistant message with tool_use content blocks
                assistant_content = []
                for tu in tool_uses:
                    try:
                        input_obj = json.loads(tu["input_json"]) if tu["input_json"] else {}
                    except json.JSONDecodeError:
                        input_obj = {}
                    assistant_content.append({
                        "type": "tool_use",
                        "id": tu["id"],
                        "name": tu["name"],
                        "input": input_obj,
                    })
                messages.append({"role": "assistant", "content": assistant_content})

                # Process tools, collect results
                tool_result_content = []
                for tu in tool_uses:
                    try:
                        args = json.loads(tu["input_json"]) if tu["input_json"] else {}
                    except json.JSONDecodeError:
                        args = {}

                    result_str, new_state = await _handle_tool(tu["name"], args, variables, state)
                    state = new_state

                    tool_result_content.append({
                        "type": "tool_result",
                        "tool_use_id": tu["id"],
                        "content": result_str,
                    })

                    # Inform Retell for analytics
                    await websocket.send_text(json.dumps({
                        "response_type": "tool_call_invocation",
                        "response_id": response_id,
                        "tool_call_id": tu["id"],
                        "name": tu["name"],
                        "arguments": tu["input_json"],
                    }))
                    await websocket.send_text(json.dumps({
                        "response_type": "tool_call_result",
                        "response_id": response_id,
                        "tool_call_id": tu["id"],
                        "content": result_str,
                    }))

                messages.append({"role": "user", "content": tool_result_content})

                # End call if flagged
                if variables["end_call"]:
                    await websocket.send_text(json.dumps({
                        "response_type": "response",
                        "response_id": response_id,
                        "content": "",
                        "content_complete": True,
                        "end_call": True,
                    }))
                    logger.info("End call signaled")
                    continue

                # ── Second Anthropic call — verbal response (no tools)
                verbal_chunks: list[str] = []
                async with anthropic_client.messages.stream(
                    model=MODEL,
                    max_tokens=300,
                    system=PROMPTS[state],
                    messages=messages,
                    temperature=1.0,
                ) as stream2:
                    async for event in stream2:
                        if (event.type == "content_block_delta"
                                and event.delta.type == "text_delta"):
                            chunk = event.delta.text
                            verbal_chunks.append(chunk)
                            await websocket.send_text(json.dumps({
                                "response_type": "response",
                                "response_id": response_id,
                                "content": chunk,
                                "content_complete": False,
                            }))

                messages.append({"role": "assistant", "content": "".join(verbal_chunks)})
                await websocket.send_text(json.dumps({
                    "response_type": "response",
                    "response_id": response_id,
                    "content": "",
                    "content_complete": True,
                }))
                logger.info(f"→ tool→response id={response_id} state={state}")

            # ── Pure text path — already streamed
            else:
                ai_text = "".join(text_chunks)
                messages.append({"role": "assistant", "content": ai_text})
                await websocket.send_text(json.dumps({
                    "response_type": "response",
                    "response_id": response_id,
                    "content": "",
                    "content_complete": True,
                }))
                logger.info(f"→ text id={response_id}: {ai_text[:120]}")

    except WebSocketDisconnect:
        logger.info("Retell disconnected")
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        try:
            await websocket.send_text(json.dumps({
                "response_type": "response",
                "response_id": response_id,
                "content": "",
                "content_complete": True,
            }))
        except Exception:
            pass
