#!/usr/bin/env bash
# iter57: the ONLY legal voice-test launcher (127.0.0.1:8020).
#
# History: `.venv/bin/uvicorn` carried a stale shebang into the OLD project
# venv — the iter56 voice call ran RAG-dead all call (ADDENDUM 2). This
# wrapper makes the correct launch the default: loads .env, refuses the
# production ports, refuses a busy port, starts the server detached
# (setsid — survives this script), polls /health, and runs the preflight
# RAG gate. Preflight red => server killed, exit 1.
#
# Usage: scripts/serve_voice.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PORT=8020
case "$PORT" in 8000|8001|8002|8003) echo "REFUSED: production port"; exit 1;; esac
if ss -tln | grep -q "127.0.0.1:$PORT "; then
    echo "REFUSED: :$PORT already busy — pid file: /tmp/opencode/voice_${PORT}.pid"
    exit 1
fi
set -a; . ./.env; set +a
LOG=/tmp/opencode/voice_server_${PORT}.log
echo "=== launch $(date -u +%FT%TZ) ===" >> "$LOG"
setsid nohup .venv/bin/python -m uvicorn diallux.app:app --host 127.0.0.1 --port $PORT >> "$LOG" 2>&1 &
PID=$!
echo "$PID" > /tmp/opencode/voice_${PORT}.pid
for _ in $(seq 1 30); do
    curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
    sleep 1
done
if .venv/bin/python scripts/voice_preflight.py --url "http://127.0.0.1:$PORT"; then
    echo "VOICE SERVER READY (pid $PID, log $LOG) — RAG PROVEN"
else
    kill "$PID" 2>/dev/null || true
    echo "PREFLIGHT FAILED — server killed. Log: $LOG"
    exit 1
fi
