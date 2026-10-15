# Retell Dynamic Values — Injection & Testing Reference

> Dedicated reference for Retell dynamic variables (`{{variable}}`): what system values exist, how to inject them, and how to drive a test LLM/agent with made-up values. Source: crawled `docs.retellai.com/build/dynamic-variables`, `features/inbound-call-webhook`, `api-references/create-chat` — 2026-08-02.
> The webhook-specific injection route (`call_inbound`/`chat_inbound`) lives in [`WEBHOOKS.md`](WEBHOOKS.md).

---

## 1. Overview

`{{variable_name}}` placeholders inject per-call/per-chat data. Works in prompts, begin message, tool configs (custom function URLs, tool descriptions, property descriptions), voicemail, transfer numbers, and webhook URLs.

**Important:** all injected values must be **strings** — numbers/booleans are not supported. Spaces around a variable name are trimmed.

---

## 2. Time / Date system variables (the ones you actually get)

There is **NO `{{current_date}}` or `{{now}}`** — Retell has no bare "date only" system variable. The real ones:

| Variable | What it gives | Example |
|---|---|---|
| `{{current_time}}` | full datetime, `America/Los_Angeles` | `Sunday, August 2, 2026 at 11:46 PM PDT` |
| `{{current_time_Europe/London}}` | full datetime, Europe/London | `Sunday, August 2, 2026 at 07:00 AM BST` |
| `{{current_hour}}` / `{{current_hour_Europe/London}}` | hour as a fraction | `7.0` |
| `{{current_calendar}}` / `{{current_calendar_Europe/London}}` | **14-day calendar listing** (NOT a single date) | `Sunday, August 2, 2026 BST (Today)` + 13 more lines |

**Consequences:**
- `{{current_calendar}}` is a 14-day listing, not `2026-08-02`. Use it only when you want the whole week context.
- To hand the LLM "today's date", reference **`{{current_time_Europe/London}}`** and tell it to use the date embedded in that string.
- For a clean exact date, inject it yourself (see §4) rather than deriving it.

### Other useful system variables
`{{current_agent_state}}`, `{{previous_agent_state}}`, `{{current_node}}`, `{{previous_node}}`, `{{session_type}}`, `{{session_duration}}`, `{{session_duration_ms}}`, `{{direction}}`, `{{user_number}}`, `{{agent_number}}`, `{{call_id}}`, `{{call_type}}`, `{{chat_id}}`.

---

## 3. Injection routes

| Scenario | Where to set | Field |
|---|---|---|
| **Outbound call / chat via API** | `create-phone-call` / `create-chat` | `retell_llm_dynamic_variables` |
| **Inbound call / SMS** | number's Inbound Webhook response | `call_inbound.dynamic_variables` (chat: `chat_inbound.dynamic_variables`) |
| **Live call override** | `PATCH /v2/update-live-call/{call_id}` | `fields_to_override.override_dynamic_variables` |
| **Agent defaults (fallback)** | agent settings | default dynamic variables |

---

## 4. Injecting made-up values into a TEST LLM / agent

The clean way to test a single state (e.g. `slot_selection`) with pre-populated values is to pass them directly in **`POST /create-chat`** — no webhook required:

```json
POST /create-chat
{
  "agent_id": "agent_...",
  "retell_llm_dynamic_variables": {
    "customer_name": "John Smith",
    "address_house_number": "14",
    "address_street": "Victoria Terrace",
    "address_city": "Newcastle",
    "address_postcode": "NE4 5AB",
    "problem_category": "boiler_breakdown",
    "symptom_brief": "no heating, pressure gauge reading 0",
    "boiler_make": "Worcester",
    "error_code": "F22"
  }
}
```

`create-chat` echoes them back in the response under `retell_llm_dynamic_variables`, and extract-tool results appear under `collected_dynamic_variables`.

### Disposable test agents (this project)

| Purpose | LLM | Chat agent |
|---|---|---|
| Custom `check_availability` webhook + `book_calendar` (happy/error paths) | `llm_545f2f1d0e7ddab0007407c38b31` | `agent_7863d7b9542fd36a91e45d18af` |
| Native-name-as-custom check (`check_availability`, custom webhook) | `llm_c4b7e24f4c250266807128a6c63d` | `agent_b25624bbf1686ffb79d3bc7ec7` |

To test the `slot_selection` state in isolation, point `create-chat` at a fresh chat agent built from the **`V3.1/prompts/calendar/`** prompt copies (`multiprompt_mvp_slot_selection_v3.md` + `multiprompt_mvp_confirmation_v3.md` — these now pass `slot_target_date` to the custom webhook) and inject the made-up variables via `retell_llm_dynamic_variables` as above.

---

## 5. Gotchas
- All dynamic-variable values must be **strings**.
- No `{{current_date}}` / `{{now}}` exists — use `{{current_time_[tz]}}` and let the LLM extract the date, or inject the exact date yourself.
- `{{current_calendar}}` is a 14-day listing, not a single date.
- To override a value mid-call (not chat), use `PATCH /v2/update-live-call/{call_id}` with `fields_to_override.override_dynamic_variables`.
