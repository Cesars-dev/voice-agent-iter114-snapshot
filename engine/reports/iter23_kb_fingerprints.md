# iter23 — Sales-KB output fingerprints (v1.8-iter20 snapshot KBs)

Verbatim signature lines → regex. A hit in an AGENT output line proves sales-craft content
surfaced in speech; it does NOT by itself prove a KB pull (prompt content overlaps KB content).
Ground truth for pulls: deployed = knowledge_base_retrieved_contents_url; v5 = Langfuse rag spans.

## sales-language-kb
source: `/home/julio/projects/Retell_AI_MCP_connection/Dialux_SDR/v5-snapshots/v1.8-iter20-20260906/agent/knowledge_bases/sales-language-kb.md`

- **SL-price-defer** (§Pricing defer) — regex: `pricing gets covered on the call|based on your (?:specific )?setup|exact numbers for your situation|pricing is custom`
- **SL-price-697** (§Pricing third-ask sanctioned release) — regex: `starts at \$?697`
- **SL-value-anchor** (§Value anchor) — regex: `2,500 to 3,500|front-desk salary|picks up every call`
- **SL-think-free** (Objection: think about it) — regex: `thinking about it is free|every day with [^.]{3,80} isn't`
- **SL-isolate** (Objection isolate) — regex: `[Ww]hat specifically\??$|what specifically concerns you`
- **SL-competitor** (Objection: competitor) — regex: `what are you comparing it to|not generic AI|full customization for`
- **SL-specialist** (Callback path) — regex: `specialist reach out|have our specialist`
- **SL-acknowledge** (Positive phrasing acks) — regex: `^(?:Got it|Makes sense|I hear you|Fair enough)\b`
- **SL-heresthething** (Power phrase) — regex: `here's the thing`
- **SL-20min** (20-minute frame) — regex: `\b20 minutes\b|20-minute|twenty minutes`

## sales-psychology-kb
source: `/home/julio/projects/Retell_AI_MCP_connection/Dialux_SDR/v5-snapshots/v1.8-iter20-20260906/agent/knowledge_bases/sales-psychology-kb.md`

- **SP-cost-ladder** (Pain amplification ladder) — regex: `what's that costing you|costing you in lost revenue|affect your ability to scale|stress level when`
- **SP-urgency-math** (Cost of inaction / loss playback) — regex: `walking away|walking out every|picture the month|want it back`
- **SP-disqualify** (Disqualification script) — regex: `not the right fit right now|reach out if things change`
- **SP-binary-close** (Commitment gradient binary) — regex: `this week or a callback|see it in action before deciding|this or that`
- **SP-industry-urgency** (Industry triggers) — regex: `competitor books|first to respond wins|empty chairs|slip through the cracks|returning voicemails`
- **SP-confident** (Confidence sells) — regex: `here's what we'll do|exactly what we built for`

## pain-points-kb
source: `/home/julio/projects/Retell_AI_MCP_connection/Dialux_SDR/v5-snapshots/v1.8-iter20-20260906/agent/knowledge_bases/pain-points-kb.md`

- **PP-quantify** (Quantification) — regex: `how many calls do you (?:think you )?miss|average value of|how much (?:time|does that cost)`
- **PP-projection** (Projection) — regex: `where will you be in|if this continues|how long can you sustain`
- **PP-comparison** (Comparison) — regex: `your competitors (?:are )?already|most businesses in your industry`
- **PP-consequence** (Consequence mapping) — regex: `affect your reputation|work-life balance|team's morale`
- **PP-emotional** (Emotional anchoring) — regex: `incredibly frustrating|hear how stressful|exhausting when`
- **PP-probe** (Probing questions) — regex: `what have you tried so far|most frustrating part|what would happen if`
- **PP-personal** (Personal questions) — regex: `keeps you up at night|affecting you personally`

