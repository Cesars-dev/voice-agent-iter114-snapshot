# DEPLOYMENT.md — self-hosted and it doesn't go down

Target: one VPS (2 vCPU / 2 GB is plenty for tens of concurrent calls), your
domain, Docker. Everything restarts itself; every failure is visible.

## Topology

```
Internet ──► Caddy (443, auto-TLS, wss+https)
                │  calls.your-domain.com
                ▼
        docker: diallux-app (uvicorn, port 8000, restart=unless-stopped)
                │ optional: LANGGRAPH_CHECKPOINT=postgres
                ▼
        docker: diallux-db (postgres 16, volume-backed, restart=unless-stopped)
                │
                ├──► Deepgram  (wss STT)
                ├──► Cartesia  (wss TTS)
                ├──► OpenAI    (LLM)
                ├──► slots.diallux-ai.site  (your LIVE webhook services)
                └──► Langfuse  (your existing self-hosted stack)
```

## 1. Provision

```bash
# DNS: A record calls.your-domain.com -> VPS IP
git clone <this-repo> /opt/diallux && cd /opt/diallux
cp .env.example .env  # fill everything; set PUBLIC_BASE_URL=wss://calls.your-domain.com

# Postgres checkpointing (optional but recommended):
#   LANGGRAPH_CHECKPOINT=postgres
#   DATABASE_URL=postgresql://diallux:<pw>@diallux-db:5432/diallux

docker compose up -d --build
docker compose ps           # both healthy?
curl -s https://calls.your-domain.com/health
```

Caddy on the host (or as a second compose service):

```
# /etc/caddy/Caddyfile  (deploy/Caddyfile has the exact file)
calls.your-domain.com {
    reverse_proxy 127.0.0.1:8000
}
```

`sudo apt install caddy && sudo systemctl reload caddy` — certificates are
automatic; **wss requires valid TLS**, never skip this.

## 2. The number

```bash
docker compose exec app python scripts/provision_twilio_number.py \
    --area-or-code 312 --host calls.your-domain.com
```

$1.15/mo + $0.0085/min inbound. Twilio retries webhooks on failure and
failures page no one at 3am — the `/twiml` handler is idempotent.

## 3. Why it stays up

| Mechanism | Effect |
|---|---|
| `restart: unless-stopped` | container crashes / VPS reboots -> auto-restart |
| Docker healthcheck (`/health`) | hung process -> marked unhealthy -> restarted |
| Webhook retries + fail-closed dvs | VPS hiccup on slots services -> retry, then honest failure (never a fake booking) |
| STT auto-reconnect | Deepgram socket death mid-call -> reconnect, call continues |
| Langfuse fail-safe tracer | Langfuse down -> tracing off, calls unaffected |
| Per-call graph + checkpointer | one bad call can't poison another; state survives restarts (postgres mode) |
| Log rotation (json-file, 5×50MB) | disks never fill from logs |

Add uptime monitoring on `https://calls.your-domain.com/health` (any
free monitor; 30s interval) and scrape `/metrics` with a Prometheus/Grafana
if you want the latency dashboards. Alert on:
`diallux_turn_e2e_response_ms p95 > 2000` and `diallux_gate_rejections_total`
growing fast (model fighting the gates — usually a prompt regression).

## 4. Scaling

- The app is stateless across calls (MemorySaver per call) → run N replicas
  behind Caddy (`reverse_proxy app:8000 app:8001 ...`) or
  `docker compose up -d --scale app=3` with the port map removed.
- With Postgres checkpointing, any worker can resume any call's state.
- Concurrency per replica: each call = 1 Twilio WS + 1 Deepgram WS + 1 Cartesia
  WS + ~2 outbound HTTP — asyncio handles hundreds on one vCPU.

## 5. Secrets

Everything lives in `.env` on the box (never in git): OpenAI, Deepgram,
Cartesia, RETELL_API_KEY (the HMAC key your VPS accepts), Twilio (only for
the provisioning script), Langfuse keys. Rotate by editing `.env` +
`docker compose up -d --force-recreate app`.

---

# V3 additions

## pgvector (RAG) — now REQUIRED for the db service

The `db` compose service is `pgvector/pgvector:pg16` (plain postgres:16 lacks
the `vector` extension). One-time index build after first `up`:

```bash
docker compose exec app python scripts/rag_ingest.py
```

~$0.02 of embeddings (whole 63k-char corpus, once). After ingest, every call
retrieves top-3 chunks per turn (`RAG_MODE=auto`); before ingest or if the DB
is down, calls fall back to full inline KB expansion — the app never hard
fails on Postgres. Re-run the SAME command after any `agent/knowledge_bases/*.md`
edit (idempotent: truncates + re-embeds).

## Voice wiring order (test the brain first)

1. `scripts/eval_accuracy.py` — offline machine checks (transitions, gates,
   tools, dvs, KB wiring, TTS normalization). Zero keys needed.
2. `scripts/eval_accuracy.py --live` — real model, real latency numbers,
   same scenarios (this is your "check accuracy before wiring TTS-STT").
3. `scripts/chat_repl.py` — interactive text chat against the real graph.
4. `scripts/fake_twilio_call.py --tts-live` — full audio loop on localhost
   before a number is bought.
5. Provision the number (`scripts/provision_twilio_number.py`) and go live.

## TTS provider switch (Cartesia -> ElevenLabs)

```bash
# .env
TTS_PROVIDER=elevenlabs
ELEVENLABS_API_KEY=xi-...
ELEVENLABS_VOICE_ID=<your Chloe voice id>
ELEVENLABS_MODEL_ID=eleven_flash_v2_5      # latency; eleven_multilingual_v2 = best Chloe
ELEVENLABS_STABILITY=0.55                  # your Chloe settings
ELEVENLABS_SIMILARITY_BOOST=0.8
ELEVENLABS_STYLE=0.15
```
Restart the app. Same gate, same barge-in, same metrics, same Langfuse spans
(`tts: elevenlabs/...` in session metadata). PAYG demo costs in DECISIONS.md.
