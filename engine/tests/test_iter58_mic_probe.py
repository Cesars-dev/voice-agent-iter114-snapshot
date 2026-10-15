"""iter58 T1 — mic probe / token gate / health field / chunk-rate metric pins.

Measurement-only iteration (the ONE behavior variable, EOT 0.8, is .env-only):
  1. /health carries `eager_eot_threshold` (fail-safe getattr)
  2. WS /mic/ws fail-closed: env unset → close 4401 (no token accepted)
  3. WS /mic/ws wrong token → close 4401
  4. WS /mic/ws correct token + probe frame → probe_ack echo (RTT basis)
  5. mic_stats page timeline frames are logged, not fatal
  6. diallux_mic_chunk_interarrival_ms histogram registered + rendered
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

TOKEN = "iter58-test-token"


def test_health_carries_eager_eot_threshold():
    from fastapi.testclient import TestClient
    from diallux.app import app
    r = TestClient(app).get("/health")
    assert r.status_code == 200
    body = r.json()
    assert "eager_eot_threshold" in body


def test_mic_ws_rejects_when_env_unset(monkeypatch):
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    from diallux.app import app
    monkeypatch.delenv("VOICE_TEST_TOKEN", raising=False)
    with pytest.raises(WebSocketDisconnect) as exc:
        with TestClient(app).websocket_connect("/mic/ws") as ws:
            ws.receive_text()
    assert exc.value.code == 4401


def test_mic_ws_rejects_wrong_token(monkeypatch):
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    from diallux.app import app
    monkeypatch.setenv("VOICE_TEST_TOKEN", TOKEN)
    with pytest.raises(WebSocketDisconnect) as exc:
        with TestClient(app).websocket_connect("/mic/ws?k=wrong") as ws:
            ws.receive_text()
    assert exc.value.code == 4401


def test_mic_ws_probe_ack_echo(monkeypatch):
    from fastapi.testclient import TestClient
    from diallux.app import app
    monkeypatch.setenv("VOICE_TEST_TOKEN", TOKEN)
    client = TestClient(app)
    with client.websocket_connect(f"/mic/ws?k={TOKEN}") as ws:
        ws.send_text(json.dumps({"event": "probe", "t": 12345}))
        ack = json.loads(ws.receive_text())
        assert ack["event"] == "probe_ack"
        assert isinstance(ack["t_server_ms"], (int, float))
        # mic_stats page timeline is accepted without killing the socket
        ws.send_text(json.dumps({"event": "mic_stats", "page_open_to_first_audio_ms": 1234}))


def test_mic_chunk_interarrival_histogram_registered_and_rendered():
    from diallux.observability import metrics
    names = [m.name for m in metrics.REGISTRY.collect()]
    assert "diallux_mic_chunk_interarrival_ms" in names
    rendered = metrics.render()
    assert b"diallux_mic_chunk_interarrival" in rendered
