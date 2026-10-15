# ENV-SWITCHES — the engine behavior-switch register + audit protocol

> **Why this file exists (the 2026-10-02 incident):** iter87's deterministic hold
> ("never miss the prewarm race") shipped as `warm_await_dest_ms` with **code
> default 0** and nothing anywhere turned it on. The battery that PASSED (Sep 30)
> ran the design active; every fresh boot after ran it OFF. Three weeks of tests
> were measured against an engine state that wasn't running, and the diffs got
> interpreted as regressions. Root cause class: **load-bearing switches live in a
> gitignored .env that does not travel with worktrees and is invisible to git**
> — so configuration drift is silent, has no diff, and no alarm.
>
> This file is the register of every behavior switch, what it does, what it is
> TODAY on each env source, and the one-query audit every run must perform.

---

## 1. The rules (non-negotiable)

1. Every behavior switch is a pydantic **Settings field in `diallux/config.py`**.
   The **code default is the shipped behavior**; `engine/.env` **silently
   overrides** it. If a design is load-bearing, its CODE DEFAULT must equal the
   owner-tested value (default-off for a tested-on design was the iter87 bug).
2. `.env` is **gitignored**: no diff, no history, doesn't follow worktree/branch
   cuts. Therefore EVERY run/battery/live call must snapshot the resolved switch
   set into its evidence (§2) — the snapshot is the only durable config record.
3. **A behavioral-switch diff between two runs voids the comparison** until the
   verdict explains it (full_call_analysys.md Step 0a).
4. `.env` edits are **owner order only** (LAW 0). Agents propose, never touch.
5. Secrets (API keys, tokens, DB URLs) live in .env and are NEVER documented
   here — this register covers behavior switches only.
6. When adding a new Settings field, ADD A ROW HERE in the same commit. A switch
   not in this register is a future mystery regression.

---

## 2. The snapshot command (run INSIDE the engine checkout you're testing)

```bash
cd <engine_checkout>            # e.g. /tmp/opencode/wt-iter89/engine
set -a; . ./.env; set +a        # .env supplies keys + overrides
.venv/bin/python - <<'PY' > /tmp/opencode/<run>_env_snapshot.txt
import json
from diallux.config import Settings
s = Settings()
GROUPS = {
  "warm_hold": ["warm_await_dest_ms","prewarm_entry_wait_ms","prewarm_entry_wait_eot_ms",
                "prewarm_byte_exact","prewarm_max_completion_tokens","prompt_prewarm"],
  "rag": ["rag_mode","rag_fire_mode","rag_live_retrieve","rag_live_await_ms","rag_top_k",
          "rag_char_budget","rag_filter_score","rag_min_query_chars","rag_multiquery",
          "rag_append_delta","rag_prefetch_on_transition","rag_drift_requery",
          "rag_drift_async","rag_drift_threshold","rag_pin_industry","rag_keep_markers",
          "rag_embedding_model","rag_table_name"],
  "stt": ["deepgram_mode","deepgram_model","deepgram_eager","deepgram_eager_eot",
          "deepgram_eager_eot_threshold","deepgram_eot_threshold",
          "deepgram_endpointing_ms","deepgram_eot_timeout_ms","deepgram_nova_interim"],
  "tts": ["tts_provider","tts_gate_fast_first_flush","tts_gate_fast_first_flush_chars",
          "tts_dedupe_semantic","tts_dedupe_sentences","tts_dedupe_window",
          "tts_dedupe_stem_window","tts_dedupe_cos_threshold","tts_phrase_cache",
          "tts_normalize","tts_sanitize_tokens","tts_delivery_profiles"],
  "llm": ["openai_model","openai_reasoning_effort","openai_verbosity",
          "verbosity_states_medium","history_window","history_trim_step",
          "state_in_delta","delta_stage_static",
          "max_tool_rounds","max_call_turns","max_call_seconds"],
  "preload": ["call_prewarm","llm_boot_prewarm"],
  "infra": ["audio_transport","http_port","langfuse_enabled","langgraph_checkpoint",
            "metrics_enabled","voice_test_token_present"],
}
snap = {}
for g, keys in GROUPS.items():
    snap[g] = {}
    for k in keys:
        k2 = k if k != "voice_test_token_present" else None
        if k2 is None:
            snap[g][k] = "set" if s.voice_test_token else "unset"
        else:
            snap[g][k] = getattr(s, k2, "<missing-field>")
print(json.dumps({"settings_snapshot": snap,
                  "model_version": "run Env-Switch audit"}, indent=2))
PY
# then: cat /tmp/opencode/<run>_env_snapshot.txt   → REVIEW THE DIFF vs baseline
```

Save next to the run evidence (`research/surgeon/<iter>/run/…` or `/tmp/opencode/`
kept life of session) and cite it in the report header.

---

## 3. The register (ground truth 2026-10-02, branch head `30405ad`, Oregon)

Resolved = code default + env override, as the engine actually runs it.

### 3a. Warm / hold plane (THE trip-wire that created this file)

| Switch (ENV) | field | code default | resolved today | What it owns | Trip-wire |
|---|---|---|---|---|---|
| `WARM_AWAIT_DEST_MS` | `warm_await_dest_ms` | **0** | **0** (absent in ALL envs) | iter87 deterministic hold: one-ahead warms of FSM successors **fire at state entry** + entry-lite round **holds** until the destination warm lands (+10ms settle). `0` = "byte-exact iter85" = BOTH OFF | ⚠️ iter87's PASS battery ran the design ON; all later boots run OFF. Owner sets `>0` to restore. Turning OFF = turn 8-class stalls (warm tail paid in-path, 1.1s+) |
| `PREWARM_ENTRY_WAIT_MS` | 100 | 100 | 100 | mid-turn state-entry await cap for an in-flight warm | cap only, safe |
| `PREWARM_ENTRY_WAIT_EOT_MS` | 500 | 500 | 500 | EOT-class entry await cap | cap only |
| `PREWARM_BYTE_EXACT` | True | True | True | warm prefill must be byte-identical to the hot path (cache-floor law) | flipping = cache re-prefills everywhere |
| `PREWARM_MAX_COMPLETION_TOKENS` | 64 | 64 | 64 | warm's max_tokens (gpt-5.4 needs `max_completion_tokens`; FIND-5: request must carry a tool to cache) | must stay ≥64 (400 rejections seen at 16) |
| `PROMPT_PREWARM` | True | True | True | master switch for prompt-cache warms | off = cold everything |

### 3b. RAG plane (lanes A/B — the design: laneA at SOT, laneB at EOT)

| Switch | field | code default | resolved today | What it owns | Trip-wire |
|---|---|---|---|---|---|
| `RAG_FIRE_MODE` | `rag_fire_mode` | **hybrid** | hybrid | `hybrid` = laneA (utterance-independent queries) at FIRST STT Update (~0.25s into speech) + laneB (final transcript) at EOT. Other modes: `speech-window`, `round-sync`, `eot` | set to `eot` = the iter58/63-era degraded draft; the KB-less Intake G1 was measured under old modes |
| `RAG_LIVE_RETRIEVE` | True | True | True | live in-flight retrieval tasks the round consumes (LANES EXIST only when True) | False = iter48 async-only path (merged-era "great" e2e ran with live-retrieve OFF on some lanes — cross-run comparisons MUST record this) |
| `RAG_LIVE_AWAIT_MS` | 60 | 60 | 60 | bounded wait for an in-flight lane at consume | inline fallback fires past the cap (embed+pgvector paid inline ~120-190ms) |
| `RAG_MODE` | `rag_mode` | `auto` | `rag` | kb-store on/off gate (`rag`) | |
| `RAG_TOP_K` / `RAG_CHAR_BUDGET` / `RAG_FILTER_SCORE` / `RAG_MIN_QUERY_CHARS` | 3 / 1600 / 0.24 / 12 | 3/1600/0.24/12 | 3/1600/0.24/12 | retrieval yield + query gating | `min_query_chars` gates short caller turns (content-free queries → 0-chunk family, FIND-4) |
| `RAG_APPEND_DELTA` | True | True | True | chunks ride the post-history delta (never cache-poison) | |
| `RAG_PREFETCH_ON_TRANSITION` | True | True | True | bg RAG staging for the destination on every transition | the staging the successor-warm consumes |
| `RAG_DRIFT_*` | requery True / async True / thresh 0.85 | same | same | drift re-query machinery | |
| `RAG_PIN_INDUSTRY` | True | True | True | industry dv → pinned KB tag (iter90 trace: landscaping pin hit 3.2ms) | |

### 3c. STT / EOT plane

| Switch | field | code default | resolved today | What it owns | Trip-wire |
|---|---|---|---|---|---|
| `DEEPGRAM_MODE` | `deepgram_mode` | flux | flux | flux v2 lane (TurnInfo: StartOfTurn/Update/EagerEndOfTurn/EndOfTurn) | Update events = laneA ONLY here |
| `DEEPGRAM_EAGER_EOT` | False | **True** | **True** (env: true) | OUR engine flag (V2 speculative LLM start on EagerEndOfTurn) — NOT a Deepgram param; default question lives in FLAG 6 | env-on overrides conservative default — the running behavior YES could differ from default without env; record in snapshot |
| `DEEPGRAM_EAGER_EOT_THRESHOLD` | None→api default | **0.7** | 0.7 | native-DG param (pass-through; range 0.3–0.9, default None = eager OFF; must be ≤ eot_threshold or API rejects) | lower = earlier EOT = faster e2e, more barge risk |
| `DEEPGRAM_EOT_THRESHOLD` / `ENDPOINTING_MS` / `EOT_TIMEOUT_MS` | 0.7 / 150 / 2500 | same | same | native-DG params (pass-through; eot_threshold 0.5–1.0 def 0.7; eot_timeout_ms 500–60000 def 5000) | hysteretic trim re-bill class lives here |
| `DEEPGRAM_RECONNECT` | True | True | True | mid-call socket respawn | fixed in iter61 keepalives |

### 3d. TTS gate / dedupe plane (PT-71 class — muted-speech lessons)

| Switch | field | code default | resolved today | What it owns | Trip-wire |
|---|---|---|---|---|---|
| `TTS_GATE_FAST_FIRST_FLUSH` | True | **False** (env: `TTS_GATE_FAST_FIRST_FLUSH=false`) | False repo envs / **ABSENT in /opt prod** | flush first chunk early | ⚠️ owner PT-71 order: FALSE stays (chopped clauses). /opt prod missing the line → any /opt-only boot runs True |
| `GATE_FLUSH_LEN` | `gate_flush_len` | **500** (iter103 PT-4, ON BRANCH engine/103-wave-stack until merge) | 500 (nobody sets env) | SentenceGate hard cap = runaway valve ONLY ("a cap, not a rule" — owner verdict on FIND-38); sentence boundaries + 150 ms idle flush FIRST | 140 restores legacy (mid-clause cuts); a behavioral-switch diff between two runs VOIDS the comparison (SWITCH LAW #3) |
| `TTS_DEDUPE_SEMANTIC` | True | **False** (env: false) | False repo envs / **ABSENT in /opt prod** | semantic cos dedupe | ⚠️ PT-71: False stays (muted real speech). Same absences as above |
| `TTS_DEDUPE_SENTENCES` + window/stem/cos | True / 6 / 12 / 0.90 | same | same | sentence dedupe machinery (iter68-era; stem-window embeds run in-stream) | changing affects both latency and fidelity |
| `TTS_PHRASE_CACHE` / `TTS_NORMALIZE` / `TTS_SANITIZE_TOKENS` | True ×3 | same | same | per-state delivery support (VOICE LAW: part of the sound) | do not "simplify" |
| `TTS_DELIVERY_PROFILES` | True (code) | lane iter91: **False** | True (ABSENT in /opt) | 2026-10-05 owner verdict: per-state EL rows in delivery.py = HISTORICAL CARTESIA TUNING (iter43 era), NOT owner-tuned for EL; EL voice = pure prod globals (AGENTS VOICE LAW). Cartesia rows stay real if provider flips. | EL per-state tuning = own future engine branch; add TTS_DELIVERY_PROFILES=false to /opt env at cutover |
| `ASK_HOLD` | `ask_hold` | **True** (iter105, ON BRANCH engine/105-ask-hold until proven) | nobody sets env → True | ONE knob, one semantic "ask → wait → pull": (T1) chop a round's spoken tail after a flushed `?` + spoken-only history + next-round NOTE; (T2) defer gate — `query_livecall_slots`/`create_livecall_booking`/`calculate_monthly_leak` never execute on an open question or a ConfirmSlots-entry round before the caller named a day; (T3) engine news/results held until new caller input; (T4) single filler producer + pull-first prompt lines deleted | `ASK_HOLD=false` = byte-exact pre-branch behavior (rollback = one env line); behavioral-switch diff between runs VOIDS the comparison (SWITCH LAW #3) |

### 3e. LLM plane

| Switch | field | code default | resolved today | What it owns | Trip-wire |
|---|---|---|---|---|---|
| `OPENAI_MODEL` | gpt-5.4 | gpt-5.4 | gpt-5.4 | MODEL LAW. `llm.json` "model" field is dead display metadata — **Langfuse `observations[].model` = ground truth** | |
| `OPENAI_REASONING_EFFORT` | none | none | none | hidden-reasoning budget (TTFT-to-first-delta inflation on long outputs!) | iter90: two 1.7-2.0s TTFTs were long-output reasoning rounds |
| `OPENAI_VERBOSITY` | medium | medium | medium + `verbosity_states_medium=Closer,Offer,contact_details,ConfirmSlots` | per-state reply length | SOP pitfall #4: verbosity is a config variable — only compare same-verbosity runs |
| `OPENAI_SERVICE_TIER` | "" (iter98 B1; cherry-picked onto engine/107-dv-recovery for the 2026-10-12 line test — merge = owner call) | "" = default tier | "" default; OR line 2026-10-12 = "fast" (line test) | iter98 request-tier knob ("fast" = ~2x token price; measured 2026-10-08 from OREGON: TTFT 1.18–1.94 s → 0.72–0.97 s uncached, 0.78 s vs 1.18–1.81 s on a 5,888-token cached prefix, cross-tier cache-read preserved). Rides model_kwargs in hot llm + _warm_llm + verbosity twins; `llm_boot_prewarm` deliberately NOT tiered; flag-off "" = byte-identical request bytes (pinned, `test_iter98_service_tier.py`). B3 keep gate PASSED: same-lane p50 −378.5 ms (n=42+42, OR :8066) | A behavioral-switch diff between two runs VOIDS the comparison (SWITCH LAW #3) — both sides of any A/B must record this field / health `service_tier` |
| `HISTORY_WINDOW` / `TRIM_STEP` | 0 / 8 | same | same | hysteretic history window (0 = unbounded, trim every 8) | window trim re-bill = periodic TTFT spikes |
| `STATE_DELTA_STAGE_STATIC` | `delta_stage_static` | **True** | True | iter96 stage-static delta split: the per-state frame (state block + RAG stage frame) rides the CACHED prefix as its own system message ([tools][head][stage][history]); the trailing delta shrinks to the true per-round change (# LIVE VALUES (changed) block). Gated by `state_in_delta` | False = byte-exact iter56 layout (state+RAG re-shipped trailing every round, ~1k tok re-prefill/round). Flipping = payload-layout change — A/B comparisons must record it |
| `MAX_TOOL_ROUNDS` / `MAX_CALL_TURNS` / `MAX_CALL_SECONDS` | 12 / 64 / 900 | same | same | chat caps | |

### 3f. Preload plane (three DIFFERENT switches — don't confuse them)

| Switch | field | code default | resolved today | What it owns |
|---|---|---|---|---|
| `CALL_PREWARM` | False | False | **false in all 3 envs** | per-call TTS/STT socket pool prewarm |
| `LLM_BOOT_PREWARM` | True | True | True | harness boot throwaway completion (iter65). NOTE: `CALL_PREWARM=false` does NOT turn this off — different field |
| (kb store prefetch) | `rag_prefetch_on_transition` see 3b | | | |

### 3g. Infra / identity

| Switch | field | code default | resolved today | Trip-wire |
|---|---|---|---|---|
| `AUDIO_TRANSPORT` | twilio | **browser** | browser | |
| `TTS_PROVIDER` | `tts_provider` | elevenlabs | elevenlabs | VOICE LAW: EL default; Cartesia only HIPAA/low-quality |
| `ELEVENLABS_VOICE_ID` + speed/stability/style/boost | — | Sarah(test) 1.06/0.5/1.0/0.75 in wt-iter89 + /opt; **Hannah(prod) 1.0/0.7 in main-checkout .env** | ⚠️ see flags |
| `CARTESIA_MODEL_ID` | `sonic-3.6` rolling | **sonic-3.6-2026-08-27** (both repo envs pin it; /opt prod env ABSENT) | ⚠️ paid lesson 2026-09-23: any env edit touching TTS keys must preserve the dated pin |
| `VOICE_TEST_TOKEN` | — | set | mic WS token gate (public-endpoint-token-gate; 4401 fail-closed) |
| `LANGFUSE_HOST` | localhost:3000 | http://<vps-ip>:3001 | flag if a run vanishes from traces |

---

## 4. FLAGS from the 2026-10-02 audit → OWNER RESOLUTIONS (2026-10-02, same day)

1. `WARM_AWAIT_DEST_MS=0` everywhere — **RE-RESOLVED (2026-10-03, iter90 verdict
   memo 94):** owner "turn it on" was executed as `WARM_AWAIT_DEST_MS=250` — that
   value was a TRANSCRIPTION ERROR (prior-session, on record). The ratified value
   is **2500** (iter87 battery spec: worst warm measured 2059 ms, zero timeouts,
   fence never bound). The wrong 250 sits in wt-iter89/.env:56 + Oregon main
   .env:54 + /opt prod and is scrubbed by the pack-promotion commit (FIND-37).
   No A/B re-run needed — values are pinned; 0-vs-250 told us nothing new.
2. **Voice drift:** /opt prod ran Sarah vs main-checkout Hannah — **RESOLVED:**
   owner standard = **Hannah, prod** (`ZSNL4hPqCnqoMPaI4jGX`, 1.0/0.6, ~1.08–1.15
   via the py playback accelerator, not env-speed). /opt prod now carries Hannah.
   wt-iter89 worktree keeps Sarah (VOICE LAW: test voice).
3. PT-71 pair absent in /opt prod — **RESOLVED:** the pair WAS a half-landed
   iter68-era measure the owner reversed (muted/chopped real speech) → **the
   correct state is OFF**: `TTS_GATE_FAST_FIRST_FLUSH=false` +
   `TTS_DEDUPE_SEMANTIC=false` now explicit in /opt prod too.
4. `CARTESIA_MODEL_ID` missing in /opt prod — **RESOLVED:** dated pin
   `sonic-3.6-2026-08-27` added (2026-09-23 paid lesson honored).
5. `deepgram_eager_eot` code default False vs env True — **CLOSED as confusion
   (2026-10-03, flag-5 verdict, memo
   `research/surgeon/iter89-prompt-enhancements/93_deepgram_param_nativeness.md`):
   the Deepgram params (`eot_threshold`, `eager_eot_threshold`, `eot_timeout_ms`,
   `keyterms`) are NATIVE flux `/v2/listen` query params — our
   `deepgram_stt.py:74-90` is a urlencoded pass-through. The only invented part is
   the settings field `deepgram_eager_eot` (engine V2 flag) → its default-vs-env
   question is FLAG 6 material, not a param-nativeness flag.

**NEW FLAG 6 (the remaining gap to "everything in order"):** three code defaults
still ship the WRONG-shipped value vs owner standards (SWITCH LAW rule 4):
`warm_await_dest_ms` default 0 (owner standard: ON at **2500** — the env's 250 was
the transcription error above),
`tts_gate_fast_first_flush` + `tts_dedupe_semantic` default True (owner standard: False),
`deepgram_eager_eot` default False (owner standard: True).
**One next-engine-iter commit flips these defaults + promotes the FULL iter87
round-B switch pack to defaults** (EAGER_HOLD_AUDIO_UNTIL_CONFIRM=true,
DEEPGRAM_EAGER_EOT_THRESHOLD=0.35, EOT_THRESHOLD=0.65, EOT_TIMEOUT_MS=3000,
POST_INTERRUPT_BACKOFF_MS=**1000** — owner 2026-10-03: the 500 the iter84 lane
ran added more problems than it solved; production standard is 1000
(FIND-38: 1000 was the MD default, never run — 500 proven zero-cost only
PRE-eager-hold; the post-hold interaction was never re-measured),
PAUSE_LADDER reminder/8000/1) — after that, even a
fresh worktree with a bare .env behaves like the tested design, and .env drift
can no longer mis-shape an iteration. Until then: the snapshot audit is NOT
optional — it is what stands between us and a silent re-run of this incident.
Owner decision 2026-10-03: promote the pack (no A/B re-run; verify with ONE GK
stress persona). Ledger: FIND-37 (rowid 37). Verdict memo:
`research/surgeon/iter89-prompt-enhancements/94_eager_drop_design_truth.md`.

**DRIVER LAW (owner 2026-10-03, revised same day):** mic batteries run the
ORIGINAL `engine/scripts/llm_mic_caller.py` — it drives calls fine (the iter90
A/B's 12-turn full-FSM calls were vanilla-driven). b52 is ONLY the barge-loop
stress variant (greeting barge + mid-turn barges; owner-ordered to measure
barge-ins) — not needed for the pack-promotion verification. Evidence-capture
rules stand (the A/B's real lessons): DISTINCT `--out` per arm/run (one shared
name destroyed the B0 raw json 2026-10-03), `journal_lines` is never cited
(stale/empty by design when `--journal-log` points at the server log), and
ground truth for any verdict = server log + Langfuse. b52 stays on its
`engine/iter82-pause-nudge` branch — no need to bring it to main.

**Knob verdict note (2026-10-03, iter90 Boris A/B):** `WARM_AWAIT_DEST_MS=250`
ran head-to-head vs knob=0 (same commit `30405ad`, single-variable A/B) — verdict
+ per-turn tables in
`research/surgeon/iter89-prompt-enhancements/92_boris_ab_hold_verdict.md`
(OREGON evidence copy). Key facts: the EOT-class bounded await is free
(14/14 + 17/18 already_done waited_ms=0); the dest-entry hold was PAID once and
delivered nothing (B250 t8r2 Discovery timeout 250ms, warm landed after the llm
call started); the multi-round/re-round cache-drop tax persisted in both runs.
iter91 shape = owner call (decouple vs keep knob); FLAG 6 flip is in scope either
way.

## 5. Who does what

- **Owner:** flips/writes .env values (LAW 0). Requests register rows for new switches.
- **Agents:** snapshot every run (§2), diff, and REPORT flag mismatches here (§4)
  with date + evidence. Never edit .env. New Settings field in a code change ⇒ row
  added here in the same branch, before the ask.
- **All audits:** cite the snapshot in the report header; verdicts between runs with
  unexplained switch diffs are void (full_call_analysys.md Step 0a).
