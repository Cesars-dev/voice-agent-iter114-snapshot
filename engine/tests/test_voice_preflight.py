"""iter57: /health venv+embedder fields + voice_preflight contract pins.

Pins the boot gate that closes the launcher trap (ADDENDUM 2: `.venv/bin/uvicorn`
stale shebang → old venv → `import fastembed` namespace-stub false positive →
RAG dead all call). /health must report the interpreter (`python_venv`) and the
REAL embedder proof (`embedder == "ok"` requires `TextEmbedding`, never a bare
`import fastembed`).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path


def test_health_has_venv_and_embedder_fields():
    from fastapi.testclient import TestClient
    from diallux.app import app

    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["embedder"] == "ok"          # main venv has fastembed 0.8.0
    assert data["python_venv"]
    assert "Retell_AI_MCP_connection" not in data["python_venv"]


def test_voice_preflight_module_import_safe():
    """scripts/ is not a package — load by path; the module must import
    cleanly (side effects only under __main__) and expose the two checks."""
    ROOT = Path(__file__).resolve().parent.parent
    script = ROOT / "scripts" / "voice_preflight.py"
    spec = importlib.util.spec_from_file_location("voice_preflight", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert callable(mod.check_health)
    assert callable(mod.check_rag)
    assert callable(mod.main)
