# Dialux SDR — LangGraph Production Build (V5)

The verbatim agent (V1) with every fragile seam your own repo flagged,
hardened. Same prompts (one honest reword), same tools, same live webhooks —
plus hard gates, deterministic math, typed state, barge-in metrics,
playback-confirmed hangup, retries, Prometheus, and optional Postgres
checkpointing. V3 adds: pgvector RAG knowledge bases (top-3 per turn, exactly
like the deployed Retell kb_config), deterministic speech normalization
before TTS, a switchable TTS provider (Cartesia default / ElevenLabs Chloe),
Cartesia generation_config voice tuning, VOICE OUTPUT RULES, temperature 0.3
default, and an accuracy eval harness that tests the agent BEFORE voice is
wired. V4 adds the **LLM-to-LLM fake-call harness** (your SOP): GPT-4o plays
adaptive caller personas against the brain, plus a Retell transport for a
migration A/B with the same personas. **V5 adds per-state delivery profiles**
(`diallux/media/delivery.py`): the state machine controls emotion/speed —
the LLM writes the words, the graph decides how they sound; `TTS_DELIVERY_PROFILES=false`
restores exact V4 behavior.

**Read `ITERATIONS.md` first** — every change vs the verbatim build, with the
problem it kills. `DECISIONS.md`/`TELEPHONY.md` in the V1 package cover the
shared research (stack choices, pricing, latency budget); this build adds:

| New | Where |
|---|---|
| Typed dvs (41, Pydantic) + server-owned protection | `diallux/schema.py` |
| Hard transition gates + topology enforcement | `diallux/graph/tools.py` |
| Deterministic node (leak math, /today prefetch) | `diallux/graph/builder.py` |
| Mark-based hangup, STT reconnect, eager EOT | `diallux/media/session.py` |
| Prometheus `/metrics` (latency histograms) | `diallux/observability/metrics.py` |
| Optional Postgres checkpointer | `diallux/graph/builder.py: build_checkpointer` |
| Prompts as editable source-of-truth | `diallux/prompts/*.md` |
| **V3** pgvector RAG (top-3/turn, inline fallback) | `diallux/rag.py` + `scripts/rag_ingest.py` |
| **V3** Speech normalization (numbers/codes/markdown) | `diallux/media/normalize.py` |
| **V3** TTS provider switch (Cartesia / ElevenLabs) | `diallux/media/tts_factory.py`, `elevenlabs_tts.py` |
| **V3** Accuracy eval before voice wiring | `scripts/eval_accuracy.py` + `eval/scenarios/` |
| **V4** LLM-to-LLM fake calls (13 personas, caller GPT-4o, Retell A/B transport) | `tests/llm2llm/` |
| **V5** Per-state delivery profiles (emotion/speed from the graph state) | `diallux/media/delivery.py` + `tests/test_delivery.py` |

## Quickstart

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # same keys as V1

.venv/bin/python -m pytest tests/ -q          # 66 tests: gates, determinism, RAG, TTS, happy path, harness units
.venv/bin/python scripts/eval_accuracy.py    # 5 eval scenarios (offline, hermetic)
.venv/bin/python scripts/eval_accuracy.py --live   # real model: agent accuracy + latency
.venv/bin/python tests/llm2llm/harness.py --offline       # hermetic fake-call smoke (no keys)
.venv/bin/python tests/llm2llm/harness.py --personas happy  # LLM-to-LLM: happy path first
.venv/bin/python tests/llm2llm/harness.py --personas all     # full battery (mock slots, no real bookings)
.venv/bin/python tests/llm2llm/harness.py --transport retell --personas happy  # A/B vs live Retell
.venv/bin/python scripts/rag_ingest.py       # build the pgvector KB index (~$0.02, once)
.venv/bin/python scripts/chat_repl.py --fake # brain dry-run
.venv/bin/uvicorn diallux.app:app --port 8000
curl localhost:8000/health && curl localhost:8000/metrics | head -20
```

## Going live (the "doesn't go down" path)

```bash
docker compose up -d --build        # app + postgres (checkpointing) + healthchecks
sudo cp deploy/Caddyfile /etc/caddy/ && sudo systemctl reload caddy
.venv/bin/python scripts/provision_twilio_number.py --area-or-code 312 --host calls.your-domain.com
```

Full hardening guide: `DEPLOYMENT.md`. Ops: `RUNBOOK.md`.

## The V2 graph

```
START → ingest → [9 state nodes] ⇄ deterministic → ... → finalize → END
                     │  (tool rounds loop back through the deterministic layer;
                     │   transitions are HARD-GATED by the executor)
```

- One graph invocation = one user utterance (unchanged)
- `transition_to_X` only swaps state when the edge exists AND every `required`
  param is truthy in dvs (server-written gates can't be forged)
- The deterministic node computes the leak math + fetches /today so the model
  never has to remember to

## Watch the 1200ms budget live

```
curl -s localhost:8000/metrics | grep diallux_turn_e2e
# diallux_turn_e2e_response_ms_bucket{le="1200"} 41
# diallux_turn_e2e_response_ms_count 47
```

Every turn also lands in Langfuse with the full stage breakdown (same trace
layout as V1 + `deterministic:*` spans and `gate_rejections` on the root).
