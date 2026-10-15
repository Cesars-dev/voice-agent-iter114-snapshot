"""Voice preflight — iter57 boot self-check for the :8020 voice leg.

The iter56 voice call ran RAG-dead all call because the server was launched
via `.venv/bin/uvicorn`, whose stale shebang resolved the OLD project venv
(no fastembed — and `import fastembed` there succeeds as an EMPTY NAMESPACE
STUB, so nothing failed at boot). This gate makes that failure loud BEFORE
anyone speaks:

  Check A (server): GET /health — `embedder == "ok"` (proves TextEmbedding
  imports INSIDE the server process) and the venv is NOT the old Retell venv.
  Check B (in-process): resolve the pgvector KB store and run ONE real
  retrieve_lanes probe on the Intake scope — ≥1 chunk or the gate is red.

Exit 0 only if BOTH checks are green. Exit 1 with a named failure otherwise.
No retries: a gate that retries is not a gate.

Usage: .venv/bin/python scripts/voice_preflight.py [--url http://127.0.0.1:8020]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    """Same loader pattern as scripts/hitl_ping.py."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    try:
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            import os
            os.environ.setdefault(key, value)
    except Exception as exc:  # noqa: BLE001
        print(f"preflight: .env load failed (non-fatal): {exc}", file=sys.stderr)


def check_health(url: str) -> dict:
    """Check A — the SERVING process's interpreter and embedder, via /health."""
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"{url.rstrip('/')}/health", timeout=10) as resp:
        if resp.status != 200:
            raise RuntimeError(f"/health HTTP {resp.status}")
        data = json.loads(resp.read().decode())
    venv = str(data.get("python_venv") or "")
    embedder = str(data.get("embedder") or "")
    print(f"[A] python_venv = {venv}")
    print(f"[A] embedder    = {embedder}")
    if "Retell_AI_MCP_connection" in venv:
        raise RuntimeError(
            f"server runs the OLD Retell venv ({venv}) — launcher bug is back")
    if embedder != "ok":
        raise RuntimeError(f"server embedder not ok: {embedder}")
    return data


def check_rag() -> int:
    """Check B — ONE real retrieve_lanes probe in THIS interpreter (the same
    venv the server runs via the symlink). Scope read from the code itself."""
    sys.path.insert(0, str(ROOT))          # script runs with scripts/ on path
    from diallux.config import get_settings
    from diallux import rag as ragmod
    from diallux.graph.builder import CallRuntime

    scope = CallRuntime._STATE_KBS["Intake"]
    print(f"[B] Intake scope ({len(scope)} KBs): {scope}")
    settings = get_settings()

    async def _probe() -> int:
        store = await ragmod.get_kb_store(settings)
        if store is None:
            raise RuntimeError(
                "kb store resolve returned None (pgvector attach failed)")
        lanes = [{"query": "boiler broken no heating urgent repair",
                  "scope": scope}]
        results = await store.retrieve_lanes(lanes)
        return [len(l) for l in results]

    counts = asyncio.run(_probe())
    total = sum(counts)
    print(f"[B] retrieve_lanes chunk counts: {counts} (total {total})")
    if total < 1:
        raise RuntimeError("retrieve_lanes returned 0 chunks — RAG would be blind")
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="voice preflight RAG gate")
    parser.add_argument("--url", default="http://127.0.0.1:8020")
    args = parser.parse_args(argv)
    print("=== voice preflight ===")
    print(f"[B] sys.prefix  = {sys.prefix}")
    try:
        import fastembed
        print(f"[B] fastembed   = {fastembed.__file__}")
        from fastembed import TextEmbedding  # noqa: F401
        print("[B] TextEmbedding import OK")
    except Exception as exc:  # noqa: BLE001
        print(f"[B] fastembed import FAILED: {exc}", file=sys.stderr)
        return 1
    try:
        check_health(args.url)
    except Exception as exc:  # noqa: BLE001
        print(f"[A] FAIL: {exc}", file=sys.stderr)
        return 1
    try:
        check_rag()
    except Exception as exc:  # noqa: BLE001
        print(f"[B] FAIL: {exc}", file=sys.stderr)
        return 1
    print("=== PREFLIGHT GREEN — RAG PROVEN ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
