# MIC-CALL ANALYSIS SOP — live browser-mic call audit

> Audit a LIVE mic call (browser bridge, `/voiceNN/mic`) the same way the 4-SOP
> FULL CALL ANALYSIS audits harness runs — **table first, then summary, then
> relevant info**. Mic calls are a different data plane from llm2llm harness
> calls (see §1), so the sources, SDKs and mechanicals differ; the judgment
> SOPs (CALL / SALES / HUMANIZED) still apply to the transcript, and LATENCY
> comes from the per-turn reports the session already logs.
>
> Companion: `full_call_analysys.md` (harness/battery runs). This file: live
> mic sessions over the public Caddy route.

## 0. Chat call vs mic call — know which plane you are on

| | **Harness call (llm2llm)** | **Mic call (browser bridge)** |
|---|---|---|
| Trigger | `tests/llm2llm/harness.py` persona | Human speaks, `/mic` page + `/mic/ws` |
| Transport | in-process fake WS, no audio | real WS audio: linear16 PCM 16 kHz in, TTS PCM out |
| STT | none (text injected) | Deepgram **v2 flux** (adopted prewarm socket) |
| TTS | none (text captured) | Cartesia sonic-3.6 (adopted prewarm socket) |
| Trace name | `llm2llm-graph-<persona>` | `diallux-call` (+ `micbridge-<sid>` spans) |
| Call id | trace id | `sid` = 12-hex (`351691ef421a`), logged at start |
| json_log | `tests/llm2llm/json_logs/*.json` | **NONE** — the uvicorn log IS the log |
| Latency plane | `rounds`/`rag` tables (ttft_ms) | per-turn `turn N report` json in the log |
| Failure modes | prompt/graph bugs | + socket staleness (AUD-11/12), token gate 4401, reconnect deafness (AUD-13), barge-in |
| Analysis entry | `call.py` / `live_sql.py import` | `mic_events.py` + this SOP |

Rule of thumb: if there is no `sid` and no `mic bridge session ... started`
line, it is not a mic call — route it through `full_call_analysys.md`.

## 1. Data sources (mic plane)

1. **uvicorn voice-server log** — the whole socket lifecycle: page open, WS
   gate (4401), prewarm adoptions, STT/TTS errors + drops, reconnects,
   barge-ins, per-turn reports, `mic page stats` (client timeline), session
   start/stop. Default: newest `/tmp/opencode/voice_server_*.log`.
2. **`scripts/mic_events.py`** — the mechanical puller (timeline classifier:
   PAGE / WS-OK / WS-GATE / START / STOP / ADOPT / ERR / DROP / WARM /
   BARGE / TURN), optional Langfuse span pull for the same window.
3. **Langfuse** (`http://localhost:3001`, traces `diallux-call`, env in the
   engine worktree `.env`) — rag/llm/tool spans per call.
4. **SQLite ledger** (`research/surgeon/iter48-rag-truth/ledger.db`, NO
   sqlite3 CLI — use `<venv>/bin/python -c "import sqlite3; …"`) — findings
   register (FIND-N) + `sops` rows when a judgment audit is due.

## 2. SDKs (paths + one-liners)

| SDK | Path | Use |
|---|---|---|
| mic event puller | engine worktree `scripts/mic_events.py` (committed on `engine/iter60-audit-fixes` @ `c7f8d2d`) | `mic_events.py` (newest log, last 400 lines) · `--sid <12hex>` · `--window "HH:MM-HH:MM"` · `--tag ERR,DROP` · `--langfuse` |
| Langfuse SDK | engine worktree `scripts/lf.py` | health · traces · `lf_quick.py runs\|bugs` |
| ledger SDK | main repo `/home/julio/projects/clean_diallux_SDR/scripts/live_sql.py` (stdlib `python3`) | `sessions` · `sops --run <r>` · `gates --a A --b B` |
| transcript/latency | engine worktree `scripts/call.py`, `scripts/latency_pull.py` | harness plane only — NOT mic calls |

**Langfuse write path for mic runs** (same as harness, window = call window):
```bash
cd <engine-worktree> && set -a && . ./.env && set +a
.venv/bin/python scripts/live_sql.py \
    --db /home/julio/projects/clean_diallux_SDR/research/surgeon/iter48-rag-truth/ledger.db \
    import --window "HH:MM-HH:MM" --run mic-<yyyymmdd-HHMM> --branch <branch> --commit <sha>
```

**Server ops (the ONLY restart surface is :8021):**
- pid file `/tmp/opencode/voice_8021.pid` — **VERIFY it matches reality**
  (`ss -ltnp | grep 8021` → `readlink /proc/<pid>/cwd` must be the worktree
  you think you restarted). The setsid/nohup launch forks: `$!` can be a
  wrapper pid that exits immediately → stale pid file → silent failed restart
  ("address already in use") → you test the OLD code. This burned iter61.
- kill by pid file ONLY; never `pkill uvicorn` (would catch :8020).
- Health: `curl -sf http://127.0.0.1:8021/health` → check `prewarm_pool`
  stt/tts ready + `python_venv` path shows the worktree you expect.

## 3. Workflow

### Step 0 — identify the call
```bash
grep "mic bridge session .* started" /tmp/opencode/voice_server_8021.log | tail -3
```
Note the `sid`, the start timestamp, and the commit the server runs
(`git -C <worktree> log --oneline -1`).

### Step 1 — mechanical timeline (mic_events.py)
```bash
cd <engine-worktree>/engine
.venv/bin/python scripts/mic_events.py --sid <sid>            # full timeline
.venv/bin/python scripts/mic_events.py --sid <sid> --langfuse # + rag/llm spans
.venv/bin/python scripts/mic_events.py --window "05:55-06:05" --tag ERR,DROP
```

### Step 2 — latency plane (per-turn reports, in-log)
Extract `turn N final report` json for the sid:
```bash
grep "final report" /tmp/opencode/voice_server_8021.log | grep -o '{.*}' \
  | .venv/bin/python -c "import sys,json; [print(json.dumps(json.loads(l))) for l in sys.stdin]"
```
Report: `e2e_response_ms` p50/p90, `stt_eot_to_llm_first_ms` p50/p90,
`llm_first_to_tts_first_ms` p50/p90, barge-in count, `eager_final_match` rate,
`resumed_count` total. (Client-side addendum: `mic page stats` line —
`page_open_to_first_audio_ms`.)

### Step 3 — health gates (ALL must be green, else autopsy)
| Gate | Evidence |
|---|---|
| Greeting played in full | no TTS-ERR/DEAD before first turn; owner confirms audio |
| Zero STT/TTS drops | no `STT-ERR`/`STT-DEAD`/`recv loop ended`/`idle timeout` lines in window |
| Reconnect recovers (if a drop happened) | `deepgram connected` logged after the drop AND turns continued |
| No poison frames | no `UNPARSABLE_CLIENT_MESSAGE` in window |
| Pool fresh | `respawned (ttl 240s)` lines present in long-lived server, NOT during a live adoption |
| Rag flowing | `--langfuse` shows rag/llm spans; no `degraded=True` storm |
| Token gate | any 4401 lines are only pre-token handshake failures |

### Step 4 — judgment audit (transcript)
The mic session transcript lives in the log (`turn N report` context) and in
Langfuse `diallux-call` traces. For CALL/SALES/HUMANIZED judgments, pull the
turn-level transcript from Langfuse (`scripts/lf.py`, trace `micbridge-<sid>`
/ `diallux-call`), then grade per `CALL-ANALYSIS-SOP.md`, `SALES-ANALYSIS-SOP.md`,
`HUMANIZED-ANALYSIS-SOP.md`. Mic-specific CALL checks:
- barge-in behaviour sane (agent stops on interruption, no double-audio)
- EOT too eager / too slow (owner interruption pattern vs `eager_final_match`)
- duplicates/echo (log lines printing twice is a LOG artifact — verify in
  audio before flagging the agent)

### Step 5 — deliver + persist (same discipline as full_call_analysys)
1. TABLE first (gates × result, latency p50/p90).
2. Summary (the story).
3. Relevant info (quotes, worst turns, fix suggestions).
4. Save to `research/surgeon/iterNN-<slug>/NN_mic_call_report.md`.
5. Persist: `live_sql.py import` for the latency plane + `sop-import` rows for
   any judgment audit (same `sops` table, `session = "<run>@<commit>"`), and
   the `findings` table for any NEW bug (`FIND-N` + `plans/PENDING_TASKS.md` PT row).

## 4. Report conventions (mic)
- Identify the call by `sid + server commit + window`, never by "the last call".
- A mic call is only PASS if the OWNER heard audio (log absence of errors is
  necessary, not sufficient — the iter60 autopsy proved a "clean" log can
  still be a mute call: the failure was socket adoption, not an exception).
- Known log artifact: uvicorn duplicate log lines (every line ×2). Count
  EVENTS deduped; file a finding before "fixing" anything mid-call.
- Every number traces to a log line, Langfuse span or SQL row. Nothing from memory.
