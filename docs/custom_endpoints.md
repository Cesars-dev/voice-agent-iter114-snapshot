# Custom Endpoints — Retell Tool Integration Guide

Everything you need to use our two deployed FastAPI services (`time_endpoint`, `cal_slots_endpoint`)
as Retell custom-function tools, and to build/update agents that use them.

> Read order: this file → `cal_slots_endpoint/README.md` + `ARCHITECTURE.md` + `FINDINGS.md`
> → `time_endpoint/README.md` + `ARCHITECTURE.md` → `docs/BUILD-SOP.md`.

---

## 1. What we have (two services, one domain)

| | **Time Service** | **Cal Slot Service** |
|---|---|---|
| Source dir | `time_endpoint/` | `cal_slots_endpoint/` |
| Systemd (user) | `time-service.service` | `cal-slots.service` |
| Local port | 8002 | 8001 |
| Public path | `/time-function/*` | everything else |
| Purpose | compute the caller's **today** (date) | query **available slots** for a date |

Both sit behind Caddy on **`https://slots.diallux-ai.site`**. Path routing:
`/time-function/*` → 8002, all other paths → 8001.

**How they interact (the flow):**
```
1. A date is computed   → time service   → "today" = 2026-08-20
2. That date is injected→ into the agent as {{today}} (dynamic variable)
3. Agent calls          → cal slots      → check_availability(slot_target_date={{today}})
4. Slots come back      → agent offers 1–2 → caller picks → book_calendar
```
The time service is **not a tool the LLM calls** — it feeds `{{today}}` at call start.
The cal slot service **is** a tool the LLM calls (`check_availability`).

---

## 2. Time service — `time_endpoint/`

### `GET /time-function/today`  (NOT signed)
Compute today in any IANA tz.
```
GET https://slots.diallux-ai.site/time-function/today?tz=Europe/London
→ {"ok":true,"today":"2026-08-20","timezone":"Europe/London","tz_abbrev":"BST"}
```
- `tz` optional, default `Europe/London`. Any IANA name.
- Use this to get the real date to inject as `{{today}}` (in tests, via `create-chat` dynvars).

### `POST /time-function/parse-time`  (SIGNED)
Parse Retell's dynamic time string into standard formats.
```
POST https://slots.diallux-ai.site/time-function/parse-time
Header: X-Retell-Signature: v=<ms>,d=<hex>
{
  "source_timezone": "Europe/London",
  "current_time": "Sunday, August 2, 2026 at 07:00 AM BST",
  "requested_format": "today,now,timestamp,iso_format"
}
→ {"ok":true,"timezone":"Europe/London","tz_abbrev":"BST",
   "parsed_time":"Sunday, August 2, 2026 at 07:00 AM BST",
   "today":"2026-08-02","now":"2026-08-02T07:00:00",
   "timestamp":1783076400,"iso_format":"2026-08-02T07:00:00+01:00"}
```
- Formats: `today`=`YYYY-MM-DD`, `now`=`YYYY-MM-DDTHH:MM:SS`, `timestamp`=unix, `iso_format`=ISO-8601.
- Errors: `400 invalid_json / missing_time / parse_failed / invalid_timezone`, `401 unauthorized`, `429 rate_limited`.

**Rule:** for the agent date, the value that matters is `today` (YYYY-MM-DD), kept identical to
`/today` output. Docs: `time_endpoint/README.md`, `time_endpoint/ARCHITECTURE.md`.

---

## 3. Cal slot service — `cal_slots_endpoint/`

### `POST /check_availability`  (SIGNED — Retell signs it automatically)
Query future slots for a date.
```
POST https://slots.diallux-ai.site/check_availability
Header: X-Retell-Signature: v=<ms>,d=<hex>
{ "slot_target_date": "2026-08-20", "timezone": "America/Chicago" }   // account_id optional
→ {"ok":true,"slots":[
     {"day":"2026-08-20","time":"2026-08-20T09:00:00","iso":"2026-08-20T14:00:00.000Z"}, ...]}
```
**Args:**
| arg | type | required | notes |
|---|---|---|---|
| `slot_target_date` | string | **yes** | `YYYY-MM-DD`. Missing/bad → `no_start_time`/`invalid_slot_target_date`. |
| `account_id` | string | no | maps to a `CAL_ACCOUNTS` key; omitted → first entry (`diallux`). |
| `timezone` | string | no | IANA zone to return `day`/`time` in; omitted → the account's own timezone. |

- Args arrive **at root** (`args_at_root: true`) → the service reads `payload["slot_target_date"]`.
- **`time`** = bare local `YYYY-MM-DDTHH:MM:SS` in the **requested** `timezone` — **this is what you book with**.
- **`timezone`** = the SAME zone as `time` (rule: `time` and `timezone` must match). Omit it and the
  endpoint defaults to the account's timezone.
- **`iso`** = UTC reference only; never use it for booking (causes the 1-hour shift bug). The endpoint
  omits `iso` entirely for accounts with `include_iso: false` (e.g. `diallux`); it is still returned
  for accounts that reference it (e.g. `diallux_live`).

**IMPORTANT — it ALWAYS returns HTTP 200 for business errors.** The LLM MUST read the body:
| body.error | meaning / fix |
|---|---|
| `no_start_time` | no date passed → retry with a real YYYY-MM-DD |
| `invalid_slot_target_date` | bad format → retry with valid YYYY-MM-DD |
| `invalid_timezone` | `timezone` arg is not a valid IANA name → use a real zone |
| `no_availability_in_window` | no slots in window → offer the next available day |
| `upstream_unavailable` | Cal down → say you'll retry, then retry |
| (HTTP 401) | bad signature (manual calls only; Retell signs for you) |
| (HTTP 400 `unknown_account`) | `account_id` is not a `CAL_ACCOUNTS` key |

### account_id / CAL_ACCOUNTS
Accounts live in `cal_slots_endpoint/.env` → `CAL_ACCOUNTS` (JSON map label → key/event/duration).
- Omit `account_id` → uses the **first** map entry. For heating_uk that is `diallux` (event 6522694).
- Per-account `include_iso` (default true): set `"include_iso": false` to strip `iso` from the slots
  response for that account (`diallux` does this; `diallux_live` leaves it on).
- **Gotcha (fixed):** the model previously guessed `account_id` (`british-heat`, etc.) and the
  service returned `unknown_account`. We **removed `account_id` from the tool schema** so the LLM
  only sends `slot_target_date` and the service uses its `diallux` default. Keep it that way unless
  you genuinely need multiple accounts (then add the label and instruct the LLM exactly).

Docs: `cal_slots_endpoint/README.md`, `ARCHITECTURE.md`, `FINDINGS.md`, `SOP_ADD_ACCOUNT.md`.

---

## 4. Shared signature (HMAC) — how it works

`X-Retell-Signature: v=<ms>,d=<hex>` where `d = HMAC-SHA256(raw_body + str(v))`, key = `RETELL_API_KEY`
(the webhook-badge key). Replay window 5 min.

- **You never sign manually in normal operation.** Retell signs the request automatically for any
  custom-function tool (`/check_availability`, `/parse-time`).
- **Unsigned:** `GET /today` (time service) — you can call it directly with no headers.
- **Manual calls** (curl/bridge.py tests) must sign. See `cal_slots_endpoint/FINDINGS.md` #1 and
  `bridge.py` (project root) for a working HMAC helper.

---

## 5. Wiring the custom tool into the LLM (the tool schema)

Custom functions live in the LLM's tool JSON — for V3.1 that is
`agents/heating_uk/multiprompt/V3.1/JSON/tool_definitions_v3.json`, under `states.<state>.tools`.

`check_availability` example (exact shape we use):
```json
{
  "type": "custom",
  "name": "check_availability",
  "description": "Query available slots. ALWAYS returns HTTP 200 — read the body. ...success/error fields...",
  "url": "https://slots.diallux-ai.site/check_availability",
  "method": "POST",
  "parameters": {
    "type": "object",
    "properties": {
      "slot_target_date": {
        "type": "string",
        "description": "ISO date (YYYY-MM-DD) of the day the caller wants a slot for."
      },
      "timezone": {
        "type": "string",
        "description": "Optional IANA timezone to return slot times in (e.g. Europe/London). Omit to use the account default."
      }
    }
  },
  "args_at_root": true,
  "speak_after_execution": false
}
```
Rules:
- `args_at_root: true` — required, the service reads root-level args.
- Do **not** include `account_id` (see §3 gotcha). If you must, set `"default": "diallux"` and tell
  the LLM never to change it.
- Describe the response (slots + error codes) **in the tool description** — the LLM reads it.
- Booking is a **native** tool (`book_appointment_cal`, name `book_calendar`), not custom. It needs
  `time` = the slot's bare local `time` + `timezone` in the SAME zone (never `iso`).

---

## 6. Injecting `{{today}}` into the agent

Prompts reference `{{today}}` (renamed from `{{today_uk}}`; agent-agnostic). Do **not** set it in
`default_dynamic_variables` (a static date goes stale). Inject per call:
- **Test (chat):** `create-chat` with `retell_llm_dynamic_variables: {"today": "2026-08-20", ...}`,
  where the date comes from `GET /time-function/today?tz=<TZ>`.
- **Production (deferred):** a per-phone inbound webhook computes `today` from `{{current_calendar}}`
  (see `docs/api/WEBHOOKS.md`). The agent-level webhook cannot inject variables.

`{{today}}` is a **string** (Retell requires string dynvars).

---

## 7. Build a NEW agent that uses these tools

Template: `agents/heating_uk/multiprompt/V3.1/build_v3_llm.py`. It reads prompts from `prompts/`,
tools from `JSON/tool_definitions_v3.json`, edges from `JSON/transition_*.json`, then creates a
**new** LLM + **new** chat agent.

```
1. Write prompts (general + one per state). See docs/BUILD-SOP.md Phases 0–3.
2. Put your custom tools in a tool_definitions JSON (copy the V3.1 one; keep check_availability).
3. Point build script PROMPTS_DIR / JSON_DIR / prompt_files at your files.
4. Ensure account_id resolution matches a CAL_ACCOUNTS key (or rely on the diallux default).
5. Run:
     cd /home/julio/projects/Retell_AI_MCP_connection
     set -a; source .env; set +a
     python3 agents/heating_uk/multiprompt/V3.1/build_v3_llm.py
   → prints NEW llm_id + chat agent_id (Model gpt-5.1, 13 KBs).
6. Test the chat agent (LLM-to-LLM): drive create-chat + create-chat-completion,
   injecting {{today}} via retell_llm_dynamic_variables. See test_happy_path_today.py.
7. When clean: create a voice agent bound to that llm_id, publish, assign a phone number.
```

---

## 8. Update an EXISTING agent (re-deploy a new LLM)

> **NEVER `PATCH /update-retell-llm`.** Retell stores the patch but does **not** propagate it to
> runtime (caching bug). Always create a NEW LLM, then repoint the agent.

```
1. Edit the build source only (prompts/, JSON/tool_definitions_v3.json). Do NOT touch the live LLM.
2. Re-run build_v3_llm.py → creates a brand-new LLM + chat agent. Nothing existing is modified.
3. Point your chat/voice agent to the new llm_id (PATCH /update-chat-agent, or the voice agent).
4. LLM-to-LLM test the new chat agent before touching production.
5. Once validated: publish the new version, register in AGENTS.md.
```
**Deploy rules:** never restart `cal-slots.service` / `time-service.service`; never touch the
production voice agent `agent_16985b5d087e56c35141983396`; keys from `.env`, never hardcoded.

---

## 9. Quick reference — arguments & tools

| Tool | Method/Path | Auth | Key args | Returns |
|---|---|---|---|---|
| `today` | GET `/time-function/today?tz=` | none | `tz` | `{ok,today,timezone,tz_abbrev}` |
| `parse-time` | POST `/time-function/parse-time` | signed | `source_timezone`, `current_time`, `requested_format` | `today,now,timestamp,iso_format` |
| `check_availability` | POST `/check_availability` | signed | `slot_target_date` (YYYY-MM-DD), optional `account_id`, `timezone` | `{ok,slots:[{day,time(,iso if include_iso)}]}` |
| `set-callback-number` | POST `/validator-function/set-callback-number` | signed | `callback_number` (spoken digits) | `{status:ok,callback_number:"+1XXXXXXXXXX"}` (writes dv) / `{status:wrong_number,action:"review_and_recapture",instruction:"..."}` (no write) / `{status:error,...}` |
| `book_calendar` | native `book_appointment_cal` | — | `time` (bare local), `timezone` (same zone), `cal_fields` | booking_uid |

**Testing gotchas (LLM-to-LLM):** never `break` on an empty agent turn — retry/backchannel;
tool `arguments` come back as a JSON **string** — `json.loads` before reading; assert booking by a
non-empty parsed `time`, never a fixed timestamp. See `docs/Testing_guidelines/LLM-TO-LLM-TESTING.md`.

**Key files:** `SERVICES_OVERVIEW.md`, `cal_slots_endpoint/{README,ARCHITECTURE,FINDINGS,SOP_ADD_ACCOUNT}.md`,
`time_endpoint/{README,ARCHITECTURE}.md`, `bridge.py`, `docs/BUILD-SOP.md`.
