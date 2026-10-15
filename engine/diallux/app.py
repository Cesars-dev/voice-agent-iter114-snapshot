"""FastAPI app — V2 production entrypoints.

  POST/GET /twiml    TwiML bridge (same as V1: inbound_track + From-seeded dv)
  WS       /media    bidirectional Media Streams session (media/session.py)
  GET      /health   liveness (docker healthcheck / uptime monitoring)
  GET      /metrics  Prometheus (latency histograms, counters) — V2
  GET      /mic      browser-mic bridge page (iter21)
  WS       /mic/ws   browser audio in (16-bit PCM 16 kHz mono), TTS PCM out —
                     SAME CallSession pipeline with transport="browser"
"""
from __future__ import annotations

import base64
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qsl

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from .config import get_settings
from .media.session import CallSession
from .media.tts_factory import tts_label

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("diallux.app")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """iter59 T5: server-start prewarm (call_prewarm=True only): idle
    Deepgram flux WS + idle Cartesia WS + warm arctic embed + the greeting
    phrase cache. Flag off = iter58 exact (no startup work)."""
    settings = get_settings()
    if getattr(settings, "call_prewarm", False):
        try:
            from .media.prewarm import prewarm_startup
            await prewarm_startup(settings)
        except Exception as exc:
            log.warning("prewarm startup failed (non-fatal): %s", exc)
    # iter65 T1: LLM boot prewarm — one throwaway completion through the
    # SHARED httpx pool (TCP/TLS + model spin-up paid once per process).
    # Gated on BOTH knobs: prewarming without the transport pool is dead air.
    if getattr(settings, "llm_boot_prewarm", False) \
            and getattr(settings, "call_prewarm", False):
        try:
            from .graph.llm import llm_boot_prewarm
            ms = await llm_boot_prewarm(settings)
            log.info("llm boot prewarm ok %sms", ms)
        except Exception as exc:
            log.warning("llm boot prewarm failed (non-fatal): %s", exc)
    yield


app = FastAPI(title="Dialux SDR — LangGraph production", version="2.0.0",
              lifespan=_lifespan)

_MIC_PAGE = Path(__file__).resolve().parent / "static" / "mic" / "index.html"


def _prewarm_status() -> dict:
    """iter59 T5: pool readiness for /health (monitoring field, fail-safe)."""
    try:
        from .media.prewarm import pool
        return {k: "ready" for k in pool}
    except Exception:
        return {}


def _embedder_status() -> str:
    """iter57: RAG embedder proof for /health — the launcher trap (ADDENDUM 2)
    died silently because `import fastembed` succeeds as a namespace stub on
    a wrong venv. Verify the real import (`TextEmbedding`), lazily and
    fail-safe: this is a monitoring field, not a kill switch."""
    try:
        from fastembed import TextEmbedding  # noqa: F401
        return "ok"
    except Exception as exc:  # noqa: BLE001
        return f"import-failed: {exc}"


@app.get("/health")
async def health():
    s = get_settings()
    try:
        eager_eot_threshold = getattr(s, "deepgram_eager_eot_threshold", None)
    except Exception:  # noqa: BLE001 — monitoring field, fail-safe
        eager_eot_threshold = None
    return {
        "ok": True,
        "version": "2.0.0",
        "stt_mode": s.deepgram_mode,
        "llm": s.openai_model,
        "service_tier": getattr(s, "openai_service_tier", "") or "",
        # iter112: speak-first pipeline visibility (flag + E2 speak model)
        "speak_first_pipeline": bool(getattr(s, "speak_first_pipeline", False)),
        "speak_model": getattr(s, "speak_model", "") or "",
        "rag_fire_mode": getattr(s, "rag_fire_mode", "eot"),
        "call_prewarm": bool(getattr(s, "call_prewarm", False)),
        "llm_prewarm": bool(getattr(s, "llm_boot_prewarm", False) and getattr(s, "call_prewarm", False)),
        "prewarm_pool": _prewarm_status(),
        "tts": tts_label(s),
        "rag_mode": s.rag_mode,
        "checkpoint": s.checkpoint_backend,
        "eager_eot": s.deepgram_eager_eot,
        "eager_eot_threshold": eager_eot_threshold,
        "langfuse": s.langfuse_enabled,
        "python_venv": sys.prefix,
        "embedder": _embedder_status(),
    }


@app.get("/metrics")
async def metrics_endpoint():
    s = get_settings()
    if not s.metrics_enabled:
        return PlainTextResponse("metrics disabled\n")
    from .observability import metrics
    return Response(content=metrics.render(), media_type="text/plain; version=0.0.4")


@app.api_route("/twiml", methods=["GET", "POST"])
async def twiml(request: Request):
    s = get_settings()
    base = s.public_base_url.rstrip("/")
    # iter103 PT-11: Twilio voice webhooks POST form-encoded (From rides the
    # BODY, not the query string — traced 2026-10-10: the <Parameter> never
    # fired on real calls). Parse the urlencoded body with stdlib (no
    # python-multipart dependency); keep the query-param fallback for
    # manual/curl tests.
    body = await request.body()
    form = dict(parse_qsl(body.decode("utf-8", "replace"),
                          keep_blank_values=True)) if body else {}
    from_number = (form.get("From")
                   or request.query_params.get("From") or "").strip()
    params_xml = ""
    if from_number:
        from xml.sax.saxutils import escape
        params_xml = f'          <Parameter name="callback_number" value="{escape(from_number)}" />\n'
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{base}/media" track="inbound_track">
{params_xml}    </Stream>
  </Connect>
  <Hangup/>
</Response>"""
    return PlainTextResponse(xml, media_type="application/xml")


@app.websocket("/media")
async def media(ws: WebSocket):
    await ws.accept()
    settings = get_settings()
    session: CallSession | None = None
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            event = msg.get("event")
            if event == "start":
                start = msg.get("start", {})
                session = CallSession(
                    settings=settings,
                    ws=ws,
                    stream_sid=start.get("streamSid", ""),
                    call_sid=start.get("callSid", ""),
                    custom_parameters=start.get("customParameters") or {},
                )
                await session.start()
            elif event == "media" and session:
                payload = (msg.get("media") or {}).get("payload")
                if payload:
                    await session.on_media(payload)
            elif event == "mark" and session:
                await session.on_twilio_mark(msg)          # V2: playback-confirmed hangup
            elif event == "warning" and session:
                await session.on_twilio_warning(msg)       # V2: 31931 watchdog
            elif event == "stop" and session:
                break
            elif event == "connected":
                pass
    except WebSocketDisconnect:
        log.info("twilio disconnected")
    except Exception:
        log.exception("media session crashed")
    finally:
        if session:
            await session.stop(reason="stream_closed")


# --------------------------------------------------------------------------- #
# iter21 — browser-mic bridge: same pipeline, transport="browser"
# --------------------------------------------------------------------------- #
@app.get("/mic")
async def mic_page():
    return HTMLResponse(_MIC_PAGE.read_text(encoding="utf-8"))


@app.websocket("/mic/ws")
async def mic_ws(ws: WebSocket):
    # iter58: token gate — fail-closed (env unset → reject) so the public
    # Caddy route can never serve an unauthenticated agent endpoint.
    expected = os.environ.get("VOICE_TEST_TOKEN")
    supplied = ws.query_params.get("k") or ""
    if not expected or supplied != expected:
        await ws.accept()
        await ws.close(code=4401)
        log.warning("mic ws rejected (%s)", "VOICE_TEST_TOKEN unset" if not expected else "token mismatch")
        return
    await ws.accept()
    settings = get_settings()
    from .observability.metrics import MIC_CHUNK_INTERARRIVAL_MS
    last_chunk_mono: float | None = None
    session: CallSession | None = None
    sid = uuid.uuid4().hex[:12]
    try:
        while True:
            raw = await ws.receive()
            if raw.get("type") == "websocket.disconnect":
                break
            audio = raw.get("bytes")
            if audio:
                now = time.monotonic()
                if last_chunk_mono is not None:
                    MIC_CHUNK_INTERARRIVAL_MS.observe((now - last_chunk_mono) * 1000.0)
                last_chunk_mono = now
                if session:
                    await session.on_media(base64.b64encode(audio).decode())
                continue
            text = raw.get("text")
            if not text:
                continue
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                continue
            event = msg.get("event")
            if event == "probe":
                # iter58 client RTT probe: client times send→ack round trip.
                await ws.send_json({"event": "probe_ack", "t_server_ms": time.monotonic() * 1000.0})
            elif event == "mic_stats":
                # iter58 page-side first-audio timeline (page-open → ws-open → first audio).
                log.info("mic page stats (%s): %s", sid, json.dumps(msg))
            elif event == "start" and session is None:
                session = CallSession(
                    settings=settings,
                    ws=ws,
                    stream_sid=sid,
                    call_sid=sid,
                    custom_parameters=msg.get("customParameters") or {},
                    transport="browser",
                )
                await session.start()
                log.info("mic bridge session %s started (transport=browser)", sid)
            elif event == "stop" and session:
                break
    except WebSocketDisconnect:
        log.info("mic bridge disconnected (%s)", sid)
    except Exception:
        log.exception("mic bridge session crashed (%s)", sid)
    finally:
        if session:
            await session.stop(reason="mic_stream_closed")
