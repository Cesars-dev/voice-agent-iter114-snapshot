# Retell Webhooks & Dynamic-Variable Injection (Corrected & Expanded)

> Source: crawled `docs.retellai.com` (features/inbound-call-webhook, features/webhook-overview, build/dynamic-variables, api-references/create-chat, api-references/create-chat-agent) — 2026-08-02.
> Supersedes the partial webhook notes in `RETELL_API_REFERENCE.md` / `WEBHOOKS_AND_ANALYSIS.md`. Key correction: **`call_started` does NOT inject variables.**

---

## Two distinct webhook types (do not confuse them)

| | **Event webhook** (agent-level) | **Inbound webhook** (per phone number) |
|---|---|---|
| Fires | `call_started` / `call_ended` / `call_analyzed`<br>`chat_started` / `chat_ended` / `chat_analyzed` | `call_inbound` (voice) / `chat_inbound` (chat / SMS) |
| Configured on | agent `webhook_url` + `webhook_events` | number's **Inbound Webhook URL** |
| Direction | **one-way** notification | **returns** data to inject |
| Can inject variables? | **NO** | **YES** (`dynamic_variables`) |

---

## 1. Event webhooks (agent-level, one-way)

Set via agent `webhook_url` + `webhook_events` (+ optional `webhook_timeout_ms`).

- **Voice defaults:** `call_started`, `call_ended`, `call_analyzed`
- **Chat defaults:** `chat_started`, `chat_ended`, `chat_analyzed`
- `call_started` fires only if the call connects (not on `dial_failed` / `dial_no_answer` / `dial_busy`).
- Retell POSTs with a **10s timeout**, retries **up to 3x** on non-2xx. Events fire in order but are **not blocking** — a failed `call_started` doesn't block `call_ended`.
- `webhook_url` supports **dynamic variables**, e.g. `https://example.com/webhook?customer={{customer_name}}` — resolved per call before delivery.

Request payload for voice (from `webhook-overview`):
```json
{ "event": "call_started", "call": { "call_id": "...", "agent_id": "...", "from_number": "+1...", "to_number": "+1..." } }
```
Chat: `{ "event": "chat_started", "chat": { ... } }`.

---

## 2. Inbound webhook (per number) — the variable injector

Configured **per phone number** (not account/agent level) in the number's settings → **Inbound Webhook URL**. Applies to inbound phone calls and inbound SMS on that number. NOT for dial-to-SIP.

Use cases: filter/reject inbound, add per-call context (`dynamic_variables`, `metadata`), override agent id/version, delay pickup, internal records.

**Request payload** (call / SMS not yet connected — no call/chat id exists yet):
```json
{
  "event": "call_inbound",
  "event_timestamp": 1780012672105,
  "call_inbound": {
    "agent_id": "agent_12345",
    "agent_version": 1,
    "from_number": "+12137771234",
    "to_number": "+12137771235",
    "custom_sip_headers": { "x-my-header": "my-value", "user-to-user": "616263;encoding=hex" }
  }
}
```
Chat/SMS form uses `event: "chat_inbound"` and `chat_inbound: { agent_id, agent_version, from_number, to_number }`.

**Response** — 2xx JSON, fields grouped under `call_inbound` (voice) or `chat_inbound` (chat/SMS). All optional:
```json
{
  "call_inbound": {
    "reject": false,
    "override_agent_id": "agent_...",
    "override_agent_version": 1,
    "dynamic_variables": {
      "customer_name": "John",
      "account_balance": "2400"
    },
    "metadata": {},
    "agent_override": { /* partial Agent settings */ }
  }
}
```
For chat use the same shape under `chat_inbound`.

---

## 3. Dynamic variables — pointer

Dynamic variables, system time/date variables, injection routes, and how to inject made-up values into a test LLM/agent are documented in **[`DYNAMIC_VALUES.md`](DYNAMIC_VALUES.md)**.

Key webhook-specific point (kept here): the **Inbound Webhook** injects variables by returning `dynamic_variables` under `call_inbound` (voice) / `chat_inbound` (chat/SMS) — see §2 above. All values must be strings.

---

## Reference: agent webhook config (create-chat-agent)
```json
{
  "response_engine": { "type": "retell-llm", "llm_id": "llm_...", "version": 0 },
  "agent_name": "Jarvis",
  "webhook_url": "https://webhook-url-here",
  "webhook_events": ["chat_started", "chat_ended"],
  "webhook_timeout_ms": 10000,
  "post_chat_analysis_data": [
    { "type": "string", "name": "customer_name", "description": "The name of the customer." }
  ],
  "post_chat_analysis_model": "gpt-4.1-mini",
  "timezone": "America/New_York"
}
```

---

## Notes / gotchas
- `call_started` is **not** a variable-injection hook — only the per-number **inbound webhook** is.
- Inbound webhook runs before a call/chat object exists (no id in payload).
- Use `retell_test_webhook` (MCP) or `POST /test-webhook` to send a synthetic event to verify your endpoint.
- Verify inbound/event webhook authenticity with your Retell API key / webhook signing secret (`features/secure-webhook`).
