# Retell AI MCP Server — README

> Brief capability summary for any agent (or developer) connecting to Retell AI via the Model Context Protocol.
>
> For the full technical spec (every tool, every endpoint, every schema field), see **`RETELL_AI_MCP_TECHNICAL_SPECIFICATION.md`** in this same folder.


---

## Quick Reference — Connection

| Property | Value |
|---|---|
| **MCP server URL** | `https://mcp.retellai.com` |
| **Transport** | Streamable HTTP (MCP 2025-06-18) |
| **Auth header** | `Authorization: Bearer <RETELL_API_KEY>` |
| **REST equivalent** | `https://api.retellai.com` |
| **Get an API key** | Retell Dashboard → API Keys tab |
| **Tool count** | ~60 tools across 14 resource domains |

### Cursor / Claude Desktop

```json
{
  "mcpServers": {
    "retell": {
      "url": "https://mcp.retellai.com",
      "headers": { "Authorization": "Bearer <RETELL_API_KEY>" }
    }
  }
}
```

### Claude Code

```bash
claude mcp add --transport http retell https://mcp.retellai.com \
  --header "Authorization: Bearer <RETELL_API_KEY>"
```

### Codex (`~/.codex/config.toml`)

```toml
[mcp_servers.retell]
url = "https://mcp.retellai.com"
bearer_token_env_var = "RETELL_API_KEY"
```

---

## Your Three Questions, Answered Directly

### Q1 — Can the MCP connection create functions (native and custom)?

**Yes — both.** Functions (Retell calls them "tools") live on the **Retell LLM response engine**, not on the agent directly. The MCP server exposes full CRUD on Retell LLMs:

- `retell_create_llm`
- `retell_update_llm`
- `retell_get_llm` / `retell_list_llms`
- `retell_delete_llm`

When you create or update an LLM via MCP, you pass a `tools` array. Each entry is one of:

**A. Pre-built / native functions** (Retell ships these — you just enable them):

| Native tool | What it does |
|---|---|
| `end_call` | Gracefully terminate the call |
| `transfer_call` | Route to a human / phone number / SIP endpoint |
| `send_sms` | Send an SMS mid-conversation |
| `press_digit` | Send DTMF tones (IVR navigation) |
| `check_calendar_availability` | Query a calendar (Google, Calendly, etc.) |
| `book_calendar` | Create a calendar event |
| `extract_dynamic_variables` | Pull structured fields from the conversation |
| `agent_transfer` | Hand off to a different Retell agent |
| `code_tool` | Run a sandboxed JS/Python snippet |
| `mcp_tools` | Let the agent itself call OTHER MCP servers mid-call |

To enable a native function, you just add its definition to the LLM's `tools` array via `retell_create_llm` or `retell_update_llm`.

**B. Custom functions** (your own webhook-backed tools):

You define a custom function with:
- `name` (unique, snake_case — e.g. `get_user_details`)
- `description` (LLM uses this to decide when to call it)
- `http_method` (`GET` / `POST` / `PATCH` / `PUT` / `DELETE`)
- `url` (your endpoint — Retell POSTs to this when the LLM invokes the function)
- `headers` (static or dynamic-variable values)
- `query_params` (each can be `description`-resolved-by-LLM or `const_value`)
- `parameters` (JSON Schema — only for POST/PATCH/PUT)
- `payload_args_only` flag (flat body vs. `{name, call, args}` envelope)
- Optional `secret` for HMAC request signing (so your endpoint can verify Retell sent it)

When the LLM decides to call your custom function, Retell sends an HTTP request to your URL, waits for your JSON response, and feeds that response back into the conversation. **All of this is configurable via MCP** because the entire LLM object — including its `tools` array — is exposed through `retell_create_llm` / `retell_update_llm`.

**So:** yes, the MCP connection can create, edit, and delete both native and custom functions, end-to-end, without touching the dashboard.

---

### Q2 — Can it customize the agent's settings?

**Yes — every single setting.** The `retell_create_agent` and `retell_update_agent` tools expose the full agent schema. Settings you can customize via MCP include (non-exhaustive):

**Identity & response engine**
- `agent_name`, `version_title`, `version_description`
- `response_engine` (Retell LLM or Conversation Flow)
- `language` (BCP-47, e.g. `en-US`, `es-MX`)
- `timezone` (IANA, e.g. `America/Mexico_City`)

**Voice & TTS**
- `voice_id`, `fallback_voice_ids`
- `voice_temperature`, `voice_speed`, `volume`
- `voice_emotion` (calm, cheerful, etc.)
- `enable_expressive_mode`, `expressive_emotion_tags`, `expressive_mode_prompt`
- `pronunciation_dictionary` (IPA/NIMFA phonemes per word)
- `ambient_sound_volume`
- `enable_backchannel`, `backchannel_frequency`, `backchannel_words`

**Conversation behavior**
- `responsiveness`, `interruption_sensitivity`
- `reminder_trigger_ms`, `reminder_max_count`
- `begin_message_delay_ms`
- `end_call_after_silence_ms`, `max_call_duration_ms`
- `ring_duration_ms`
- `stt_mode` (`fast` / `accurate`)
- `vocab_specialization` (`general` / `medical` / `finance`)
- `boosted_keywords`
- `denoising_mode`
- `custom_stt_config.endpointing_ms`

**DTMF / IVR**
- `allow_user_dtmf`, `allow_dtmf_interruption`
- `user_dtmf_options` (digit_limit, termination_key, timeout_ms)
- `ivr_option` (what to do when IVR is detected)
- `voicemail_option` (static text / dynamic text / hangup)

**Outbound call screening**
- `call_screening_option.agent_identity`
- `call_screening_option.call_purpose`

**Post-call analysis**
- `post_call_analysis_data` (custom fields to extract: name, type, description, examples, required, conditional_prompt)
- `post_call_analysis_model` (e.g. `gpt-4.1-mini`)

**Webhooks**
- `webhook_url`, `webhook_events`, `webhook_timeout_ms`

**Data storage & privacy**
- `data_storage_setting` (`everything` / `essential` / `none`)
- `data_storage_retention_days`
- `opt_in_signed_url`, `signed_url_expiration_ms`

**Guardrails**
- `guardrail_config.input_topics` (e.g. `platform_integrity_jailbreaking`)
- `guardrail_config.output_topics`

**Handbook (behavioral toggles)**
- `default_personality`, `conversational_personality`, `natural_filler_words`
- `high_empathy`, `echo_verification`, `nato_phonetic_alphabet`
- `speech_normalization`, `smart_matching`, `ai_disclosure`, `scope_boundaries`

**Per-call overrides** — even after an agent is published, you can override ANY of these settings for a single call by passing `agent_override` to `retell_create_phone_call` / `retell_create_web_call` / `retell_register_phone_call`. No need to create a new agent for A/B tests or per-customer personalization.

**Publishing** — changes to an agent create a new draft version. Use `retell_publish_agent` to promote draft → production. Production phone numbers always call the latest published version unless you specify otherwise.

---

### Q3 — Can it create an agent from scratch or follow a specific set of rules?

**Yes, both.**

**From scratch** — minimal agent creation flow via MCP:

```text
1. retell_create_llm({
     model: "gpt-4.1",
     general_prompt: "<system prompt / persona / rules>",
     begin_message: "Hi, thanks for calling Acme...",
     tools: [/* native + custom function defs */],
     knowledge_base_ids: [/* optional */]
   })
   → returns llm_id

2. retell_create_agent({
     agent_name: "Acme Intake",
     response_engine: { type: "retell-llm", llm_id, version: 0 },
     voice_id: "retell-Cimo",
     language: "en-US",
     timezone: "America/Mexico_City",
     /* ...any other settings from Q2... */
   })
   → returns agent_id (status: "draft")

3. retell_publish_agent({ agent_id })
   → status: "published"

4. retell_list_phone_numbers({})
   → find a number you own

5. retell_update_phone_number({
       phone_number: "+14157774444",
       inbound_agents: [{ agent_id: agent_id, weight: 1 }]
   })
   → number now routes inbound calls to the new agent
```

That's a complete from-scratch agent in 5 tool calls.

**Following a specific set of rules** — yes, multiple mechanisms:

1. **System prompt** — the `general_prompt` field on the Retell LLM is the agent's "rulebook." Write whatever constraints, persona, do/don't lists, escalation rules, etc. you want. The LLM obeys it on every turn.

2. **Conversation Flows** — for stricter, node-graph behavior, use `retell_create_conversation_flow` to build a deterministic state machine with conditional transitions. Each node has its own prompt and tools. Best for compliance-heavy workflows (healthcare intake, KYC, etc.) where you cannot let the LLM free-form.

3. **Guardrails** — `guardrail_config.input_topics` and `output_topics` enforce topic boundaries (block jailbreaks, off-topic requests, sensitive content). Retell runs these as a separate model pass on every turn.

4. **Handbook config** — `handbook_config` toggles (e.g. `ai_disclosure: true` forces the agent to disclose it's an AI; `scope_boundaries: true` keeps it on-topic; `nato_phonetic_alphabet: true` for serial numbers / emails).

5. **Tool gating** — only give the LLM the tools it needs for that scope. A sales-qualification agent doesn't need `book_calendar`. Least-privilege tool surface = tighter behavior.

6. **Versioning & tags** — `retell_create_draft_agent_version` lets you stage rule changes without touching production. Tag versions (`v1-ruleset-a`, `v1-ruleset-b`) for A/B testing different rule sets.

7. **Post-call analysis as a feedback loop** — `post_call_analysis_data` can extract "Did the agent follow rule X?" as a structured field. Use this to monitor rule adherence across calls.

So: from scratch ✅, from a rule set ✅, from a compliance flow ✅, A/B tested ✅.

---

## Q4 — What do I need to do manually (besides the obvious SIP config etc.)?

The MCP server can do almost everything programmatically. Below is the **complete list of things you MUST do outside MCP**, either in the Retell dashboard, in your own infrastructure, or in code.

### 🔧 Things only the dashboard can do

| Task | Why MCP can't |
|---|---|
| **Create / rotate API keys** | Security: keys create keys = privilege escalation risk |
| **Create / rename / delete workspaces** | Org-admin scope |
| **Invite users & set roles** (Access Control) | Identity is dashboard-scoped |
| **Complete KYC verification** (for phone number purchasing in some regions) | Legal requirement — needs document upload |
| **Add payment method / change billing plan / view invoices** | Payments are dashboard-only |
| **Configure Salesforce / HubSpot OAuth** | OAuth flow requires browser redirect |
| **Install the Retell Website Widget** on your site | It's a JS snippet, not an API config |
| **Conductor (Retell's own AI assistant)** setup | Dashboard-only feature |
| **View the analytics dashboard / play call recordings inline** | MCP returns the data; the visualization is dashboard UI |
| **File support tickets / view status page** | Use status.retellai.com / support@retellai.com |

### 🌐 Things you must host yourself

| Task | Notes |
|---|---|
| **Custom function webhook endpoints** | When you define a custom function via MCP, you give it a `url`. **You** must run that endpoint (your own server, Vercel, Lambda, etc.) to receive Retell's POSTs and reply with JSON. MCP defines the function; your server implements it. |
| **Webhook receiver for call/chat events** | Same — `webhook_url` on the agent points at your server. You write the handler that ingests `call_started`, `call_ended`, `call_analyzed`, etc. |
| **Custom LLM WebSocket server** (only if you bring your own LLM) | If you don't use Retell's built-in LLMs and instead route to your own model, you stand up an OpenAI-compatible WebSocket server that Retell dials into. Not an MCP concern. |
| **Audio WebSocket client** (for web calls) | The browser-side WebRTC client that connects to `wss://api.retellai.com/audio-websocket/{call_id}`. Use Retell's Web SDK or build your own. MCP only creates the call; it does not stream audio. |
| **SIP trunk / carrier config** (custom telephony) | If you bring your own carrier, configure SIP on your side. Then use `retell_import_phone_number` (MCP) to register the number with Retell. |
| **Knowledge base source files** | If your KB source is a file (PDF, .docx), you must host it at a URL Retell can fetch, or upload it via dashboard. MCP `add_knowledge_base_sources` accepts URLs and text directly; raw file upload via API requires a presigned URL flow. |
| **Cron / scheduler for batch calls** | `retell_create_batch_call` schedules a batch — but if you want recurring batches (every Monday at 9am), you schedule that on your side and call the tool each time. Retell doesn't have a "schedule" primitive for batches. |

### 🛠 Things that are technically optional but you should set up once

| Task | Why |
|---|---|
| **Initial concurrency capacity purchase** | New accounts come with `base_concurrency`. If you need more concurrent calls, buy `purchased_concurrency` in the dashboard. MCP can *inspect* limits (`retell_get_concurrency`) but cannot *buy* more. |
| **Default `data_storage_setting` for the workspace** | Set a workspace-wide default so new agents inherit a sane privacy posture. |
| **Workspace timezone & locale** | Affects dashboard displays; agents have their own `timezone` field regardless. |
| **Webhook signing secret** | Generate one in the dashboard and use it to verify Retell webhook signatures on your server. |
| **Test phone numbers** | Retell provides `+1` test ranges — claim them in the dashboard before doing CI/CD call tests. |
| **Knowledge base source documents** | Author your PDFs, docs, FAQs once; then attach via MCP. |

### 🚫 Things MCP explicitly cannot do (recap from main spec)

- Stream audio (use the Audio WebSocket directly)
- Run the Custom LLM WebSocket (you host it; Retell dials in)
- Manage API keys / workspaces / users / billing
- Configure Salesforce/HubSpot OAuth connections
- Install the website widget
- Subscribe to live call events (use webhooks for that)
- Download raw audio bytes (MCP returns a signed `recording_url`; you HTTP GET it)
- Cross-workspace operations (one key = one workspace)
- Raise rate limits (plan-dependent)

### ✅ Everything else is MCP-able

If it's not in the lists above, the MCP server can do it: creating agents, LLMs, conversation flows, phone numbers, calls, chats, KBs, voices, tests, batch tests, alerts, webhooks (definitions, not receivers), QA reruns, exports, etc.

---

## ✅ What CAN be done via MCP (full list)

### 🤖 Voice agents — full CRUD + lifecycle
Create, update, publish, list, get, delete, version history, promote specific versions.

### 💬 Chat agents — full CRUD + lifecycle
Same as voice agents, for text/SMS chat.

### 📞 Calls — initiate, monitor, manipulate, analyze
- Outbound PSTN (`create_phone_call`)
- Web / WebRTC (`create_web_call`)
- Custom telephony (`register_phone_call`)
- Live call control (`update_live_call`, `stop_call`)
- List/filter/get/delete calls
- Re-run post-call analysis
- Per-call `agent_override` to change any agent field for one call

### 📱 Phone numbers — telephony management
Purchase, import (custom SIP), assign agents, configure outbound, release.

### 📚 Knowledge bases — RAG
Create KBs, attach URL/text/Q&A/sitemap sources, remove sources, list/get/delete KBs.

### 🎙 Voices
List across providers, clone, search community library, add new.

### 🧠 LLMs (response engines)
Create/update/delete with model selection, prompt, tools (native + custom), knowledge bases, begin messages, dynamic variables.

### 🔀 Conversation flows
Full CRUD on flows + subflows + reusable components.

### 🧪 Testing & QA
Test case definitions, batch tests, test runs, rerun QA, submit scores.

### 📊 Account & utilities
Get concurrency, schedule batch calls, list exports, introspect MCP tools.

### 🚨 Alerts & webhooks (rolling out)
Create/list alert rules, list incidents, test webhooks.

### 🎛 Per-call overrides
Every call tool accepts `agent_override` to change any agent field for that single call — A/B testing and per-customer personalization without spawning new agents.

### 🔁 Multi-surface parity
REST, SDK, MCP are 100% equivalent. Same IDs, same data model. Mix freely.

---

## ⚠️ Tools that should always require user confirmation

| Action | Risk |
|---|---|
| `retell_delete_agent` / `retell_delete_chat_agent` | Removes the agent; in-flight calls may break |
| `retell_publish_agent` | Pushes draft → production immediately |
| `retell_delete_phone_number` | Releases the number back to the pool |
| `retell_create_phone_call` / `retell_create_batch_call` | Dials real phone numbers — costs money, calls real people |
| `retell_delete_call` | Permanently destroys transcripts / recordings |
| `retell_update_live_call` | Modifies an in-progress conversation |
| `retell_stop_call` | Ends a live call |

Enable **confirm-before-running-tools** in your MCP client and keep it on.

---

## 🔐 Security one-pager

1. **Least-privilege API keys.** Scoped keys; different keys for dev/prod.
2. **Never paste API keys into chat.** Store in client config / env vars.
3. **Read before write.** Always `get_*` or `list_*` before `update_*` or `delete_*`.
4. **Gate destructive tools.** Confirm `delete_*`, `publish_*`, `create_phone_call`, `create_batch_call` explicitly.
5. **Watch for prompt injection.** Transcripts, user messages, KB docs may contain malicious tool-call instructions.
6. **PII hygiene.** Set `data_storage_setting` appropriately; don't echo full transcripts back to the model.
7. **Test in dev first.** Sandbox workspace + test numbers before connecting production keys.

---

## 📚 Companion Document

For the full agent-facing spec — every tool's argument schema, every endpoint, every object field, end-to-end worked examples, error catalogue, troubleshooting matrix, REST↔MCP↔SDK migration map — open **`RETELL_AI_MCP_TECHNICAL_SPECIFICATION.md`** in this folder.

---

## 🆘 Support

- **Docs:** https://docs.retellai.com
- **MCP setup:** https://docs.retellai.com/get-started/mcp-server
- **Function calling:** https://docs.retellai.com/build/single-multi-prompt/function-calling
- **Custom functions:** https://docs.retellai.com/build/single-multi-prompt/custom-function
- **Email:** support@retellai.com
- **Status:** status.retellai.com
- **Dashboard:** https://dashboard.retellai.com
