# VoiceAI Project Memory

## Active Working Agent (iterate this one)
- **Agent ID:** `agent_db07429de33791f69c7818a746`
- **Name:** Dialux AI (clone)
- **LLM ID:** `llm_f919ac2c012382ac983a7a3b56ae`

## Original Production Agent (do not touch)
- **Agent ID:** `agent_4b577c7e169b069deae991b11d`
- **Name:** Dialux AI
- **LLM ID:** `llm_187271736e1a805fa07737692b84`
- **Phone:** +1 (213) 600-1723 — connected inbound + outbound

## Other Agents
- **Dialux Español** — `agent_188fc925668ae9da42ca86526e` / LLM `llm_834489d997c676fda0c7d3ca35ee`
- **Dialux AI (copy)** — `agent_550cd8f232fafb0552ec13a22a` / LLM `llm_4c9e0c6d1dfdd9d2178b143f47d0` (ar-SA, unused)

## Retell API Key
- Stored in `retellai-mcp-server/.env` — never hardcode

## MCP Setup
- Server: `retellai-mcp-server/build/index.js`
- Config: `.mcp.json` in project root → calls `start.sh` which loads `.env`

## LLM Architecture — What's Fixed in Retell
- **Model is LLM-level only** — cannot set different model per state
- **KBs are LLM-level only** — `knowledge_base_ids` on LLM object, no per-state scoping
- **kb_config** exists: `{ top_k: 3, filter_score: 0.6 }` — tunable at LLM level
- States only have: `name`, `state_prompt`, `tools`, `edges`

## Latency Issues (1500–2000ms)
Root causes identified:
1. **3 KBs × top_k 3 = up to 9 KB chunks injected per turn** — fires even on 7-second calls
2. **Massive prompt context** — general_prompt ~3k tokens + long state prompts
3. **llm_token_surcharge** appears on longer calls = context bloat mid-call
4. **GPT-4.1 high priority** — powerful but heavier than mini
- Pending fix: tune `kb_config` (drop top_k, raise filter_score)

## Agent Prompt — State Machine (4 states)
Flow: Discovery → Closer → contact_details → Booking

### Changes Made This Session (clone LLM only)
- **Discovery** fully rewritten: SPIN psychology (problem Q → implication Q → trigger event), broader `interest_signal` triggers, call volume not a dealbreaker/gate
- **Closer** - Quantify Loss: now guides prospect to name loss TYPE first (missed lead / appt / deal) before asking for a number
- **Closer + Discovery disqualification**: removed `<5 calls/day` as a qualifier — volume is context only
- Do NOT advertise being AI in Discovery — no meta-moment reveal

### Prompt Files on Disk
- `retellai-mcp-server/discovery_prompt.txt` — current Discovery state prompt (source of truth for edits)

### Key Prompt Rules (never violate)
- Never say "pain point" to prospects — use "challenge" or "situation"
- Never mention AI/voice agents unless directly asked
- ONE question at a time, always wait for response
- `{{pain_points}}` is append-only, never replace
- Transition gate: `interest_signal=true AND industry exists AND pain_points exists`

## How to Update LLM Prompts
Always use direct Retell REST API (not MCP tool — schema too limited):
```
fetch LLM → modify states array → PATCH /update-retell-llm/{llm_id}
```
Write prompt to a .txt file first, read with `fs.readFileSync` — avoids shell escaping issues with em-dashes and special chars.
