"""Telegram HITL ping — iter55 owner-gate messenger (fail-safe, log-and-continue).

Usage:
    .venv/bin/python scripts/hitl_ping.py "message text"

Reads TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from the environment (engine/.env is
loaded by callers via `set -a; . ./.env; set +a`, or this script loads it itself
if present). Creds on file at /home/julio/projects/video_strategy/.env — copy the
values into engine/.env (gitignored); NEVER commit them.

Contract (same as the tracer): a ping failure NEVER blocks engine work — this
script logs and exits 0 regardless.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> None:
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
            os.environ.setdefault(key, value)
    except Exception as exc:  # noqa: BLE001
        print(f"hitl_ping: .env load failed (non-fatal): {exc}", file=sys.stderr)


def hitl_ping(text: str) -> bool:
    """Send `text` to the owner's Telegram chat. Returns True on success."""
    _load_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("hitl_ping: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not configured — skipping", file=sys.stderr)
        return False
    try:
        import requests

        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        if resp.status_code != 200:
            print(f"hitl_ping: telegram HTTP {resp.status_code} — non-fatal", file=sys.stderr)
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"hitl_ping: telegram error (non-fatal): {exc}", file=sys.stderr)
        return False


if __name__ == "__main__":
    message = " ".join(sys.argv[1:]) or "(empty ping)"
    ok = hitl_ping(message)
    print("hitl_ping: sent" if ok else "hitl_ping: skipped/failed (non-fatal)")
    sys.exit(0)
