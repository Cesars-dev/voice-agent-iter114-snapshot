"""Configuration for the Dialux SDR LangGraph runtime (production build).

V3 iteration (see ITERATIONS.md):
  - RAG knowledge bases on Postgres+pgvector (RAG_MODE=auto; inline fallback)
  - TTS normalization before every spoken sentence (the Retell-native feature)
  - TTS provider switch: Cartesia (default) or ElevenLabs (your Chloe settings)
  - Cartesia generation_config (speed/volume/emotion) per request
  - VOICE OUTPUT RULES appended to the system prompt (Cartesia's voice-agent
    starter rules — ON in production; the deterministic normalizer backs it)
  - openai_temperature defaults to 0.3 (your ask). NOTE: gpt-5.x reasoning
    models only accept the default temperature — the guard in graph/llm.py
    omits it for gpt-5* so the API doesn't 400; set OPENAI_MODEL=gpt-4.1 to
    make 0.3 actually apply.

V5 iteration (see ITERATIONS.md §16):
  - Per-state delivery profiles (media/delivery.py): the state machine
    controls emotion/speed — Cartesia generation_config and ElevenLabs
    voice_settings per turn, driven by the graph state, not the LLM.
    TTS_DELIVERY_PROFILES=false restores exact V4 behavior (.env globals).
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---- LLM -------------------------------------------------------------
    openai_api_key: str = ""
    openai_base_url: str | None = None          # override for Groq/OpenRouter/etc
    # MODEL LAW (owner 2026-09-22): gpt-5.4 everywhere; .env OPENAI_MODEL
    # overrides; gpt-5.2 DEPRECATED. Deployed Retell model.
    openai_model: str = "gpt-5.4"
    openai_temperature: float | None = 0.3      # gpt-5* guard lives in graph/llm.py
    openai_reasoning_effort: str = "none"        # gpt-5.4: none|low|medium|high ("low" ≈ 0.77s/call; off=default verbosity)
    openai_verbosity: str = "medium"            # gpt-5.4 output length control (T1 REVERTED: eval ladder stalls the contact_details ask at low — H1 fix-2; latency note: medium turn-1 live TTFT ~1,248 ms, D1/D2 recover it in iter47)
    # iter98 B1: request-tier knob, "" = DEFAULT tier (flag-off bytes identical
    # to the pre-iter98 build — byte-parity law). "fast" = the one PROVEN lever
    # for the dominant segment (measured from OREGON 2026-10-08: TTFT 1.18-1.94s
    # → 0.72-0.97s uncached, 0.78s vs 1.18-1.81s on a 5,888-token cached prefix,
    # cross-tier cache-read preserved; ~2x token price). Env: OPENAI_SERVICE_TIER.
    openai_service_tier: str = ""
    # iter112: speak-first staggered pipeline (owner design 2026-10-13).
    # Flag ON (heavy rounds only — lite/entry-lite/channel-closed rounds are
    # byte-exact unchanged): the round fires a BARE speech-only astream
    # (tools=[] — "we don't need tools to stream audio") while a gpt-5.4
    # TOOLS astream runs concurrently as a background task (text discarded;
    # its tool_calls are the only tool source). Merge = await-on-arrival,
    # NO caps: M1 in-round follow-up when the tools already landed, M2 the
    # next turn's ingest awaits the future before the build, M3 clean
    # close. Flag OFF = byte-exact original single-astream call site.
    # Plan: plans/plan_v5_iter112_speak_first_pipeline.md
    speak_first_pipeline: bool = False
    # iter112 E2 (MODEL LAW exception, owner order 2026-10-13): dedicated
    # SPEAK-stream model. "" = settings.openai_model (E1, same model both
    # streams). "gpt-4.1" = speak-only flip (probe: warm bare TTFT 748 vs
    # 1,085 ms). The speak twin is built WITHOUT model_kwargs — gpt-4.1
    # rejects the gpt-5.x `verbosity` param (and per the owner order carries
    # no service_tier). Tools always stay on openai_model.
    speak_model: str = ""
    # iter114c (owner design call 2026-10-13): states whose SPEAK stream rides
    # `speak_model`; every OTHER state's speak stream rides `openai_model`
    # (tool accuracy). "" = every state rides `speak_model` (legacy all-luna
    # E7 mode); `speak_model=""` = same model both streams (E1 mode).
    # Default: luna for the fast speech states, gpt-5.4 for the tool-heavy.
    # iter114d (owner GO 2026-10-14): Offer joins the luna states — it carries
    # no math tool (tools = extract_offer_details + offer_completed); the
    # leak math is Closer's and engine-owned.
    speak_model_states: str = "Intake,Discovery,Closer,Offer"
    # iter114 speech authorization layer (plan: tasks/surgeon/iter114-speech-auth/
    # 04_master_plan.md; owner GO 2026-10-13). The mouth is a RENDERER: the
    # engine derives facts, hands them a true card, curates its payload, and
    # mechanically blocks unauthorized speech. All five code-default ON
    # (switch-law #4); each is independently rollbackable.
    # T1: run the pure-derivation pass on every dvs write path (M2 ingest +
    # in-round tools; the deterministic node keeps its own call).
    derive_on_write: bool = True
    # T2: leak-input resolution — word numbers ("fifteen" -> 15) + a
    # call_volume "...week" fallback (the E5 unparseable-inputs disease).
    leak_normalize: bool = True
    # T3: the WIT card ("WHAT IS TRUE RIGHT NOW") replaces/anchors the state
    # block in the speak delta + rides the entry-lite delta.
    speak_card: bool = True
    # T4: the speak view — conversation + card only (no raw tool JSON, no
    # RAG); the tools stream keeps full fidelity byte-unchanged.
    speak_diet: bool = True
    # T5: the render guard — sentences with unauthorized figures/times/status
    # claims never reach TTS (fail-closed; one recovery line per round).
    speak_fact_guard: bool = True
    # iter46 D5 (H1 fix-1): per-state verbosity OVERRIDE — states listed here
    # run at "medium" (the late-flow ask/capture states where verbosity=low
    # stalls the contact_details completion in the eval ladder); all other
    # states ride openai_verbosity ("low" = the 700 ms TTFT recipe). Env:
    # VERBOSITY_STATES_MEDIUM="Closer,Offer,contact_details,ConfirmSlots".
    verbosity_states_medium: str = "Closer,Offer,contact_details,ConfirmSlots"
    max_tool_rounds: int = 12                   # engine.py MAX_TOOL_ROUNDS (verbatim)
    # iter38 F3 (PT-34): hard cap on REAL graph turns per call + wall-clock
    # guard (Julio 2026-09-09: 24 murdered healthy 5-min calls; disaster calls
    # were 97+). Reaching EITHER limit speaks a graceful close and stops —
    # checked BETWEEN turns only, so a mid-flight booking chain always finishes.
    max_call_turns: int = 64
    max_call_seconds: int = 900                 # iter66-T7 (owner 2026-09-23): 15-minute cap (was 600)
    phaseb_chain: bool = True                   # iter33 Phase B: engine-owned booking chain (env PHASEB_CHAIN=0 disables)
    # iter30 token diet: cap on history entries sent per LLM round. The dvs
    # state carries the facts; old conversational turns are dead weight. 0 =
    # send full history. Trims happen at turn boundaries (tool pairs intact).
    # iter56: default 0 — FULL append-only history (deployed Retell parity,
    # T2 anatomy §3d). The 16/8 hysteretic window was the second cache
    # mutator (the sliding start busted the prefix every ~3-4 turns). Revert
    # switch: HISTORY_WINDOW=16.
    history_window: int = 0
    # iter43 cache-prefix stability: hysteretic trim — the history window START
    # only advances every `history_trim_step` entries past the window size, so
    # the OpenAI prompt-cache prefix stays byte-stable between jumps instead of
    # shifting every turn. Revertible: step 1 = old sliding-window behavior.
    history_trim_step: int = 8
    # iter43 cache-prefix stability: a forced-speech round (iter32 payload gate)
    # keeps the state's FULL tool array (tools render FIRST in the cached
    # prefix; sending [] on forced rounds busted the entire prefix). A
    # SPEECH-ONLY directive in the tail asks the model to hold its fire;
    # revertible to the old tools=[] behavior. Flag False → exact old behavior.
    force_speech_keep_tools: bool = True
    # iter43 first-turn-lite: turn 1 sends ONLY general_prompt + VOICE_OUTPUT_RULES
    # (no state prompt, no tools, no RAG) — the lightest possible cold payload.
    # iter44: default ON (owner call — first TTFT 1913→976 ms proven on battery);
    # the tools channel opens at turn 2. Guard is turn_index == 1 exactly.
    first_turn_lite: bool = True
    # iter49 T4b (T6): state-entry lite — the FIRST round in a state whose
    # name differs from the previous round's rendered state (post-
    # transition_to_X ack rounds + the turn-2 entry into the initial state,
    # because the turn-1 lite round renders no state head) sends
    # [state head (kb=False)][state-block dvs][history window] (~1200 tok):
    # NO tools, NO RAG lanes, NO warm await — the ack speaks instantly while
    # the heavy payload prewarms in the background (owner 2026-09-13
    # MUST-HAVE: state changes invisible, ack TTFT ~800ms). Turn 1 keeps the
    # first_turn_lite path; the ack can only SPEAK (extraction/slots fire
    # round 2+). Per-state opt-out (comma list) for states that must
    # tool-call on entry.
    state_entry_lite: bool = True
    state_entry_lite_off: str = ""
    # iter56: state-block relocation — the volatile CURRENT CALL STATE block
    # moves from the pre-history tail (re-frozen at ingest, mutated by every
    # dv write → the prefix never climbed past [tools][head]) into the
    # POST-HISTORY delta (the last message the model reads, re-billed anyway).
    # The prefix [tools][head][history] becomes truly append-only and
    # cache_read climbs with history (deployed parity + T0 verdict).
    # False = the iter55 layout (revert switch; the tail/freeze machinery
    # stays intact behind the flag).
    state_in_delta: bool = True
    # iter56: strip literal {{var}} tokens from the static head — dead
    # placeholders ("{{callback_number}} may already hold the number") read
    # as "CURRENT CALL STATE may already hold…" — a byte-stable pointer to
    # the live values that ALWAYS ride the delta. Kills the {{dv}} blindness
    # (PT-53's verbatim-question loop) without putting dvs in the cached
    # head. False = literal {{var}} (revert switch).
    head_strip_vars: bool = True
    # iter43 async prompt-cache prewarm: fire-and-forget warm() requests that
    # replicate the EXACT next-round prefix (full tools + stripped static head +
    # history snapshot), max_completion_tokens capped, output discarded. Fired
    # at greeting (full Intake) and on every state transition. Latched once per
    # state per call; tasks cancelled in aclose().
    prompt_prewarm: bool = True
    prewarm_max_completion_tokens: int = 64
    # iter48b: was 16 (iter43); RAISED per the plan's BLOCKED item — the 400
    # "Could not finish the tool call because max_tokens was reached"
    # rejection was OBSERVED in happy-e WARNING logs (full-shape warm killed).
    # 64 completes the warm cleanly (wire-tested: 62 output tokens, no tool
    # calls). With LITE_NOOP_TOOL bound, even a noop tool call fits in 64.
    # iter65 T1: boot LLM prewarm — ONE shared httpx pool for all ChatOpenAI
    # twins + a throwaway boot completion (app lifespan AND harness start).
    # False = per-instance clients, no boot warm (iter64 exact).
    llm_boot_prewarm: bool = True
    # iter44 append-only knowledge: the RAG tail is byte-stable for the whole
    # state visit (frozen at entry); drift re-fetches land as a DELTA system
    # block AFTER the history instead of re-rendering the tail mid-prompt —
    # correctness (new chunks) AND cache (prefix never mutates) both hold.
    rag_append_delta: bool = True
    # iter31 C3: dynamic CURRENT CALL STATE block appended after the static
    # prompt head (all non-empty dvs as "name: value"). False drops the block;
    # the head stays static either way (byte-stable OpenAI prompt-cache prefix).
    state_block: bool = True
    # iter40 L2: bg-warm the ASAP availability payload on ConfirmSlots entry /
    # ok transition into it; the model's no-date query_livecall_slots call is
    # answered from the warm store when < slots_prefetch_ttl_s old (live
    # webhook fallback otherwise; slot_target_date / preferred_time calls
    # ALWAYS go live). False = today's behavior (every call hits the webhook).
    slots_prefetch: bool = True
    slots_prefetch_ttl_s: float = 120.0
    # iter40 L3: one-trip transitions — a spoken round whose only executed tools
    # are fire-and-forget bookkeeping OR an ok `transition_to_*` finalizes the
    # turn (no second LLM trip). gate_failed transitions keep looping (repair
    # round preserved). Also: an empty-text round carrying query_livecall_slots
    # speaks the prompt's own "One moment while I check availability." via TTS
    # instead of leaving dead air. False = iter29 behavior exactly.
    one_trip: bool = True
    # ---- iter105 (PT-2): engine-side ask-hold (ONE knob, one semantic) ------
    # ask → wait → pull, enforced in code:
    #   T1 chop: a flushed SPOKEN sentence ending "?" chops the rest of that
    #     round's tail from TTS; history/assistant_text = the spoken prefix;
    #     round_asked=True; unspoken_tail recorded; the next round's delta
    #     carries the engine NOTE (caller did NOT hear the rest).
    #   T2 defer gate: DEFER_SET tools (query_livecall_slots,
    #     create_livecall_booking, calculate_monthly_leak) never execute on an
    #     open question (S1) or on a ConfirmSlots-entry round before the caller
    #     named a day (S2, day-guard exempts answer-first flows).
    #   T3 hold-release: engine continuation speech (news/results) waits for
    #     new caller input (held_facts + release note at the next round).
    #   T4 filler: single producer (in-flight timer only); pull-first prompt
    #     lines deleted.
    # False = byte-exact pre-branch behavior (rollback = one env line;
    # env: ASK_HOLD).
    ask_hold: bool = True
    # iter40 C3: freeze the CURRENT CALL STATE block bytes per TURN — rendered
    # once at turn start (ingest, before round 1) and reused for every round of
    # that turn. Tool rounds no longer mutate the tail between rounds, so
    # round-2 gens share the byte-identical prefix → OpenAI prompt-cache hits
    # (kills the 5.7s t28-class full re-prefill). Block refresh at next turn
    # start; dvs written mid-turn stay visible to the NEXT turn. False = the
    # old per-round re-render.
    frozen_state_block: bool = True
    # iter41 T1: sentence-level TTS dedupe — inside ONE turn's token stream an
    # exact-duplicate consecutive sentence (case-insensitive, whitespace-
    # collapsed) is dropped from the SPOKEN stream only; model output/history/
    # final content recorded as-is. Kills the gpt-5.4 reasoning-off repetition
    # artifact (in-turn double-speak) at the audio layer. False = raw stream.
    tts_dedupe_sentences: bool = True
    # iter42: sentences of within-turn history the dedupe remembers (sliding
    # window). 6 catches every observed multi-sentence double block (max = 3)
    # with headroom; the window carries across tool-rounds of the same turn
    # and resets at ingest. 0 keeps only the guard behaviour.
    tts_dedupe_window: int = 6
    # iter65 T2: sanitizer gate on the TTS sentence buffer — strips verbatim
    # `function_calls` protocol tokens, drops code/JSON-ish flushed sentences
    # ({ } [ ] => or the `":` pair) from SPEECH only. False = raw stream (iter64).
    tts_sanitize_tokens: bool = True

    # ---- iter44: cache floor + conversation-driven RAG ----------------------
    # T1: prewarm composes its request through the SAME _build_messages as the
    # hot path (exact bytes + tools, frozen tail included). False = legacy
    # warm shape ([head][history], no tail).
    prewarm_byte_exact: bool = True
    # T3: message layout [head][tail][history] — the STATE-BLOCK tail sits
    # BEFORE the history so the cached prefix grows round-to-round (the old
    # tail-after-history position made history land where the tail was and
    # busted the prefix every turn). False = legacy layout.
    tail_before_history: bool = True
    # T6b: an ok transition_to_X fires a bg RAG prefetch for X's scope
    # (staged chunks consumed at state entry, ~0 ms critical path).
    rag_prefetch_on_transition: bool = True
    # T6c: on a same-state round with a NEW caller message, embed the round
    # query and compare against the frozen query vector; below threshold →
    # re-retrieve for what the caller needs NOW (fixes the frozen-retrieval
    # industry miss). Requires the store to expose embed_query.
    rag_drift_requery: bool = True
    rag_drift_threshold: float = 0.85   # cosine >= threshold → chunks still valid
    # T4: when pgvector is unavailable, fall back to inline-KB expansion (the
    # ~16k whole-KB blob). Default OFF: degraded = lean head with KB markers
    # stripped + loud log, never the blob. Explicit rag_mode="inline" (the
    # chosen V1 mode) still expands inline regardless of this flag.
    fallback_inline_kb: bool = False

    # ---- iter46: latency floor (plan_iter46_latency_floor) -------------------
    # T5: sentence-gate fast first flush — when nothing has been spoken this
    # turn, flush pending tokens on the first "," (≥12 chars) or at
    # tts_gate_fast_first_flush_chars total instead of waiting for a full
    # sentence boundary. PROMOTED default False (iter91 promote-pack:
    # PT-71 class — fast flush chopped clauses; False = proven prod value).
    tts_gate_fast_first_flush: bool = False
    tts_gate_fast_first_flush_chars: int = 28
    # iter103 PT-4: SentenceGate hard cap — "a cap, not a rule" (owner verdict
    # on FIND-38): sentence boundaries flush first; this is the runaway valve
    # ONLY. 140 = legacy mid-clause behavior; 500 = the tested value (SWITCH
    # LAW rule 4; env: GATE_FLUSH_LEN).
    gate_flush_len: int = 500
    # T2: drift check goes async — the round path never awaits embed_q(); a bg
    # task embeds base+query, computes cosine, and appends new chunks via the
    # rag_append_delta path for the NEXT round. Flag False = sync requery.
    rag_drift_async: bool = True
    # T4c: state entry bounded-awaits an in-flight prewarm for this state
    # (MILLISECONDS — divided by 1000 for asyncio.wait_for). 0 = off.
    prewarm_entry_wait_ms: int = 100
    # iter48 Commit B (M9): EOT-boundary entry await cap — the FIRST round of
    # a turn (rounds_left == max_tool_rounds; turn 1 IS an EOT boundary) can
    # afford a longer bounded wait because the user's reply window is the
    # warm's head start. Mid-turn rounds keep prewarm_entry_wait_ms.
    prewarm_entry_wait_eot_ms: int = 500
    # iter95 (savage piece 1): at state entry, a warm that is NOT yet done
    # gets up to this long to land; at the cap it is CANCELLED — the prefix
    # cache it wrote at prefill survives server-side (savage Proof 1) and
    # the real round reads it at hit-class TTFT (cancel, don’t wait).
    # 0 = legacy iter46 wait-shield (timeout lets the warm keep running,
    # shield semantics). Code default = RATIFIED owner value (SWITCH LAW 4).
    prewarm_abort_cap_ms: int = 400
    # iter87 VERSION B (fire + hold), knob-gated as ONE unit: >0 arms
    # (a) the one-ahead/chain destination warms and (b) the state-entry
    # ack (entry_lite) bounded-await of the DESTINATION warm TASK before
    # sending + the 10 ms settle — ordering, not timing; the cap is a
    # dead-provider fence. PROMOTED default 2500 (iter91; iter87 battery:
    # worst warm 2059 ms, zero timeouts — forensics_calls 13/14). FIND-37:
    # never boot without it. 0 = off (byte-exact iter85).
    warm_await_dest_ms: int = 2500
    # FIX 1 (PORT 1): eager-think, confirmed-speak — hold the eager turn's
    # AUDIO at the sentence gate until the confirmed EndOfTurn adopts it;
    # TurnResumed discards silently (nothing was ever spoken). Default off
    # = byte-exact today behavior; rollback = env off.
    # (iter85 T3: eager_resume_grace_ms DELETED — its guarded-mismatch branch
    # and _grace_then_cancel went with it; the hold makes the grace moot.)
    eager_hold_audio_until_confirm: bool = True

    # ---- RAG knowledge bases (pgvector) -------------------------------------
    rag_mode: str = "auto"                      # auto | rag | inline
    database_url: str = "postgresql://diallux:diallux@localhost:5432/diallux"
    # iter49 T4 CUTOVER — the three knobs below are ONE vector space, flipped
    # together (T2 wired the embedder, T3 built kb_chunks_v2; this is the
    # moment live-retrieve becomes the engine's behavior). Revert = set all
    # three back (text-embedding-3-small / kb_chunks / 0.40 = iter48 exact).
    rag_embedding_model: str = "snowflake/snowflake-arctic-embed-m"
    rag_table_name: str = "kb_chunks_v2"     # scripts/kb_reembed.py's corpus (193 v2+faq, 768-d)
    rag_filter_score: float = 0.24           # arctic-calibrated (02_replay_report.md: proper-10 10/10, junk 15->1)
    local_embed_cache_dir: str = "/home/julio/fastembed/models"  # arctic-m weights (shared with the lab venv)
    local_embed_threads: int = 4                # pre-flight: 6-lane batch p50 126ms @4 threads vs 192ms @2
    rag_top_k: int = 3                          # deployed Retell kb_config.top_k
    rag_char_budget: int = 1600                 # chars of excerpts per system prompt
    # iter21: skip the embedding round trip on filler turns ("hello?", "yes")
    # — user utterance shorter than this many chars never hits the vector DB
    rag_min_query_chars: int = 12

    # ---- iter49 T4: fresh multi-lane live retrieval -------------------------
    # True: retrieval runs fresh EVERY non-lite round (lane A = one retrieve
    # per refer-tag, scoped [tag.kb]; lane B = the caller utterance, scoped
    # the state's _STATE_KBS with the industry anchor; ONE batched embed +
    # parallel pgvector; quota merge by chunk id). The fresh set REPLACES the
    # post-history delta each turn; the pre-history prefix
    # [tools][head][state-block][history] is NEVER rewritten. Freeze/drift/
    # transition-prefetch are INERT under this flag. False = iter48 exactly.
    rag_live_retrieve: bool = True
    # lane construction under live-retrieve: True = lane A per refer-tag +
    # lane B caller text; False = ONE combined query (refer_query) per round.
    rag_multiquery: bool = True
    # A/B: keep ##slug-kb## markers in the cached head instead of stripping
    # them (their content arrives via retrieval; default False = iter48).
    rag_keep_markers: bool = False
    # iter49 T3: bounded await (ms) for the in-flight live-retrieve task on
    # the round path. The batched arctic embed (~90-190ms multi-lane) is
    # fired EARLY (eager EOT) and runs inside the caller's remaining speech;
    # the round awaits at most this long (pgvector + merge are the only
    # awaited portion when the embed already ran). 0 = never await (always
    # the previous turn's fresh set; the task lands for the next round).
    rag_live_await_ms: int = 60
    # ---- iter59 T3: RAG fire mode -------------------------------------------
    # WHEN the live-retrieve task fires relative to the turn:
    #   "eot"           iter58 exact — fire at EOT/EagerEOT (revert surface)
    #   "speech-window" fire the FULL multi-lane retrieval at the FIRST flux
    #                   TurnInfo.Update (~0.3 s into speech); EOT fire is
    #                   SKIPPED when an Update landed (it would cancel the
    #                   landed task). Live gate: degraded >=90% false,
    #                   await p50 <= 15 ms.
    #   "round-sync"    consume short-circuits to an INLINE retrieval (span
    #                   live-sync). Gate: TTFT p50 <= 980 ms, degraded 0%.
    #   "hybrid"        owner-preferred: lane A (refer-tag queries + dv values,
    #                   utterance-INDEPENDENT) fires at the first Update; lane
    #                   B (caller utterance — only exists at EOT) fires at
    #                   EOT/EagerEOT as a SINGLE-query embed consumed inline
    #                   within the await cap. Gate: await p50 <= 80 ms,
    #                   lane-A landed >= 90%, degraded < 5%, TTFT <= 900 ms.
    # PT-58 (iter65): "hybrid" is the CODE default — ".env gets lost on
    # worktree copies" (owner 2026-09-22). RAG_FIRE_MODE env still overrides.
    rag_fire_mode: str = "hybrid"

    @field_validator("rag_fire_mode")
    @classmethod
    def _rag_fire_mode_whitelist(cls, v: str) -> str:
        # iter60 AUD-8: an invalid mode silently ran eot-fires with
        # speech-window consume semantics — fail fast at Settings load.
        allowed = ("eot", "speech-window", "round-sync", "hybrid")
        if v not in allowed:
            raise ValueError(
                f"rag_fire_mode must be one of {allowed}, got {v!r}")
        return v

    # ---- iter52 T6: pinned industry vertical --------------------------------    # True: {{industry}} resolves to a vertical tag ONCE per call; that
    # vertical's chunks are fetched by METADATA (no embed, no pgvector) and
    # re-injected from the pin into the post-history delta every round.
    # Also gates the plumbing deep-dive scope trigger (dv contains 'plumb'
    # -> lane-B scope += 'plumbing'). False = iter49 live behavior exactly
    # (byte-exact; zero schema dependency — rollback without the DROP).
    rag_pin_industry: bool = True
    # ---- iter59 T4: semantic dedupe v2 + KB-everywhere ----------------------
    # KB-everywhere: _RETRIEVAL_OFF becomes EMPTY (all 9 states retrieve —
    # Retell-parity: KB on every response). The attribute stays (tests /
    # builder reference it); the live-branch gate becomes do_retrieve =
    # bool(scope). Freed states retrieve via lane B over the 9-KB union.
    tts_dedupe_semantic: bool = False          # PROMOTED iter91: False = prod value (PT-71 class: semantic dedupe muted real speech)
    # cross-turn question re-ask killer: after the exact-match check, a QUESTION
    # sentence whose stem cosine vs the last spoken question stems >= this
    # threshold is dropped from the spoken stream. 0.90 sweep-verified:
    # near-verbatim variants 0.939-1.000 caught, legit controls max 0.748.
    tts_dedupe_cos_threshold: float = 0.90
    # cross-turn question stem window (spoken QUESTION entries {"text","vec"};
    # plain state key, survives the whole call). 0 disables the window growth.
    tts_dedupe_stem_window: int = 12
    # ---- iter59 T5: transport prewarm + greeting phrase cache ---------------
    # call_prewarm: server-start pool of ONE idle Deepgram flux WS + ONE idle
    # Cartesia WS + a warm arctic embed (ONNX session load) + the greeting
    # phrase cache. Sessions ADOPT from the pool (fallback: fresh connect — a
    # call never fails on the pool). False = iter58 exact (no startup work).
    call_prewarm: bool = False
    # phrase cache: repeated EXACT phrases (the greeting) are synthesized once
    # (raw bytes matching output_format + generation_config) and spliced into
    # the stream without touching the API — faster first audio, zero credits.
    # False = synthesize every time (iter58).
    tts_phrase_cache: bool = True
    # whole-chunk char cap for the pinned block (measured max vertical
    # total 1709; whole-chunk drops only, never mid-chunk truncation).
    rag_pin_char_budget: int = 1800

    # ---- iter55 T0: warm/greeting observability ------------------------------
    # Emits Langfuse spans + info logs for prompt-cache warms (fire/complete/
    # fail per shape), _await_warm outcomes, and the voice greeting runway.
    # Pure observability — False restores the exact pre-T0 span surface.
    warm_observability: bool = True

    # ---- Deepgram STT --------------------------------------------------------
    deepgram_api_key: str = ""
    deepgram_mode: str = "flux"                 # "flux" (v2 listen) | "nova3" (v1 listen)
    deepgram_model: str = "flux-general-en"     # or nova-3 when mode=nova3
    deepgram_eot_threshold: float = 0.65         # EOT confidence (PROMOTED iter91: iter87 pack)
    deepgram_eot_timeout_ms: int = 3000         # silence backstop (PROMOTED iter91: sales cadence)
    deepgram_eager_eot_threshold: float | None = 0.35  # PROMOTED iter91 (iter87 pack: eager 0.35)
    deepgram_endpointing_ms: int = 150          # nova3 fallback endpointing
    deepgram_eager_eot: bool = True            # PROMOTED iter91 (iter87 pack: speculative on EagerEOT)
    deepgram_eager: bool = True                 # V2 master switch for speculative turn execution
                                                # (iter21: was MISSING from config — the eager path could
                                                # never run; iter21 enabled it and it AttributeError'd)
    deepgram_reconnect: bool = True             # V2: auto-reconnect the STT socket mid-call
    deepgram_raw_dump: bool = False             # iter93 T1: raw flux TurnInfo dump (FIND-35 data-first, default OFF)
    deepgram_nova_interim: bool = True
    deepgram_keyterms: list[str] | None = None  # e.g. ["Dialux", "Jay"]

    # ---- TTS (provider-agnostic; see media/tts_factory.py) -------------------
    # TTS provider switch (media/tts_factory.py): cartesia | elevenlabs.
    # iter66 CUTOVER (owner directive 2026-09-22): elevenlabs is the prod
    # default — Sarah Casual&Modern uG1JFy6xppqckhHCs2KG, eleven_turbo_v2_5,
    # speed 1.06 / style 1.0 / stability 0.5 (owner ear test, e91af1a).
    # Voice + keys ride .env (ELEVENLABS_*). Rollback: TTS_PROVIDER=cartesia.
    tts_provider: str = "elevenlabs"

    # speech normalization (the Retell-native layer, re-implemented):
    tts_normalize: bool = True
    tts_phone_style: str = "digits"             # digits | natural | spell(cartesia)

    # V5: per-state delivery profiles (media/delivery.py) — the graph state
    # decides emotion/speed per turn; false = .env globals only (V4 behavior)
    tts_delivery_profiles: bool = True

    # iter21 naturalness: per-sentence inline SSML variance on Cartesia.
    # Each flushed sentence gets <volume ratio>/<speed ratio> tags with
    # natural jitter around the state profile — the t11 voicemail recipe's
    # core finding (mechanical, reliable; the voice stops sounding robotic).
    tts_delivery_jitter: bool = True
    tts_jitter_speed: float = 0.06              # ±speed ratio around 1.0
    tts_jitter_volume: float = 0.10             # ±volume ratio around 1.0

    # ---- Cartesia ---------------------------------------------------------
    cartesia_api_key: str = ""
    cartesia_version: str = "2026-08-14"
    cartesia_voice_id: str = "a0e99841-438c-4a64-b679-ae501e7d6091"  # list voices: scripts/list_cartesia_voices.py
    cartesia_model_id: str = "sonic-3.6"
    cartesia_language: str = "en"
    # pcm_mulaw @ 8000 = Twilio Media Streams native format: zero transcoding.
    cartesia_sample_rate: int = 8000
    cartesia_buffering: str = "custom"          # "custom" (our gate, max_buffer_delay_ms=0) | "managed"
    cartesia_max_buffer_delay_ms: int = 0       # custom buffering -> 0
    # generation_config per request (docs: guidance, not strict; emotion is
    # English-only beta; best on emotive-tagged voices — DECISIONS.md)
    cartesia_speed: float | None = None         # 0.6-1.5 (1.05 = slightly brisk sales cadence)
    cartesia_volume: float | None = None        # 0.5-2.0
    cartesia_emotion: str = ""                  # e.g. "calm" / "trust" / "happy"

    # ---- ElevenLabs (optional provider) -----------------------------------
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""               # your Chloe voice id
    elevenlabs_model_id: str = "eleven_flash_v2_5"  # low latency; or eleven_multilingual_v2
    elevenlabs_inactivity_timeout: int = 180    # ws-level inactivity timeout, max 180 (docs); was elevenlabs_latency_opt (deprecated param, removed)
    elevenlabs_stability: float = 0.55          # Chloe tuning (your settings, env-able)
    elevenlabs_similarity_boost: float = 0.8
    elevenlabs_style: float = 0.15
    elevenlabs_speed: float = 1.0               # 0.7-1.2

    # ---- Telephony (Twilio Media Streams) ------------------------------------
    public_base_url: str = "wss://your-domain.example.com"   # used in TwiML
    twilio_account_sid: str = ""                # provisioning script only
    twilio_auth_token: str = ""                 # provisioning script only

    # ---- Audio transport (iter21: browser-mic bridge) -------------------------
    # "twilio" = Media Streams 8k mulaw (native); "browser" = /mic ws, 16-bit
    # PCM linear16 @ browser_sample_rate mono (Chrome AudioContext resample).
    # Same pipeline after the codec layer: STT -> graph -> gates -> TTS.
    audio_transport: str = "twilio"
    browser_sample_rate: int = 16000

    # ---- Webhook tools (verbatim Retell custom tools) ------------------------
    retell_api_key: str = ""                    # HMAC signing key for slots.diallux-ai.site
    slots_webhook_key: str = ""                 # optional override key for the cal_slots endpoints (v1 accepts a different key than the validator)
    webhook_timeout_s: float = 15.0

    # ---- Langfuse (self-hosted) ----------------------------------------------
    langfuse_enabled: bool = True
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "http://localhost:3000"

    # ---- V2 production --------------------------------------------------------
    prompts_dir: str = str(ROOT / "diallux" / "prompts")   # editable source of truth
    checkpoint_backend: str = "memory"          # "memory" | "postgres"
    database_url: str = "postgresql://diallux:diallux@localhost:5432/diallux"
    metrics_enabled: bool = True                # Prometheus /metrics
    hangup_mode: str = "mark"                   # "mark" (playback-confirmed) | "timer"

    # ---- Agent artifact ------------------------------------------------------
    agent_llm_json: str = str(ROOT / "agent" / "llm.json")
    agent_begin_message: str = str(ROOT / "agent" / "begin_message.txt")
    knowledge_base_dir: str = str(ROOT / "agent" / "knowledge_bases")

    # ---- Runtime --------------------------------------------------------------
    http_port: int = 8000
    log_level: str = "info"

    # ---- V2 production --------------------------------------------------------
    prompts_dir: str = str(ROOT / "diallux" / "prompts")   # editable source of truth
    checkpoint_backend: str = "memory"          # "memory" | "postgres"
    metrics_enabled: bool = True                # Prometheus /metrics
    hangup_mode: str = "mark"                   # "mark" (playback-confirmed) | "timer"

    # ---- V3 ---------------------------------------------------------------------
    # VOICE OUTPUT RULES appended to the system prompt (Cartesia voice-agent
    # starter rules) so the model writes TTS-friendly text — ON in production.
    tts_voice_rules: bool = True

    # ---- FIX 2 (iter82): pause ladder — Vapi customer.speech.timeout shape.
    # Default OFF (byte-exact behavior when off). Lane env opts in:
    # PAUSE_LADDER_MODE=reminder PAUSE_REASK_MS=15000 PAUSE_LADDER_MAX=1
    pause_ladder_mode: str = "reminder"       # PROMOTED iter91 (iter87 pack)
    pause_reask_ms: int = 20000                # owner order 2026-10-12 (was 8000 "PROMOTED iter91"): 8s nudge fired inside caller thinking pauses; every caller EOT resets the ladder (session _on_eot)
    pause_ladder_max: int = 1                   # rungs before the close rung (unchanged)
    pause_close_ms: int = 45000                 # silence before goodbye+hangup
    pause_reminder_text: str = "Hey, are you still there?"
    pause_reminder_text_2: str = "No rush — take your time."
    pause_close_text: str = "I'll let you go then. Have a good one — goodbye."

    # ---- FIX 7 (iter83): STT liveness — adoption age gate + recv-heartbeat
    # zombie watchdog (fires the EXISTING transparent reconnect path).
    # Default OFF-by-behavior (gate off when 0, watchdog exits when 0).
    stt_adopt_max_age_s: float = 120.0           # refuse older pooled sockets; 0 = off
    stt_rx_zombie_ms: int = 15000                # 0 = off; > eot_timeout(2500)

    # ---- FIX 5 (iter84): post-interrupt backoff — after a real barge-in,
    # block new agent audio for post_interrupt_backoff_ms so the caller's
    # answer lands before the agent speaks again. 0 = off (default).
    post_interrupt_backoff_ms: int = 1000          # PROMOTED iter91: 1000 = owner standard + MD default (FIND-38; 500 lane value added more problems than it solved)


@lru_cache
def get_settings() -> Settings:
    return Settings()
