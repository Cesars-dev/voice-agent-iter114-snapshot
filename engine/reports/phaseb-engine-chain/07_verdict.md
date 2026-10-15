# iter33/iter34 verdict — Phase B engine chain + model compare (gpt-5.2 vs gpt-4.1)

## Compare table (first-13 batteries, identical harness/meters/SOPs, mock slots)

| Metric | gpt-5.2 (iter33) | gpt-4.1 (iter34) | Delta |
|---|---|---|---|
| PASS | 12/13 (Pedro over-book) | 12/13 (Brenda under-drive) | equal |
| ended | 13/13 | 12/13 (Pedro no-end_call) | 5.2 |
| turns | 212 | 203 | ≈equal |
| LLM gens | ~366 | ~340 | 4.1 fewer |
| gens/turn | ~1.73 | ~1.67 | ≈equal |
| silent% (OTEL) | 54.9% | 36.8% | 4.1 (mix-assisted) |
| LLM p50 / p90 | 1.52s / 2.15s | 0.88s / 1.28s | 4.1 ~40% faster |
| agent-turn p50 | 3.02s | 1.57s | 4.1 |
| books (chain_ok) | 8/8 clean | 6/6 clean (of 7 chances) | 5.2 converts |
| SALES | 4 hire / 7 solid / 2 retrain | 0 hire / 9 solid / 4 retrain | **5.2 sells** |
| HUMANIZED | 10 PASS / 3 POLISH / 0 FAIL | 7 / 5 / 1 FAIL (Ray loop) | **5.2 sounds human** |
| CALL | 11 / 1 / 1 (Pedro) | 10 / 2 / 1 (Brenda) | 5.2 |
| cost | n/a (endpoint 400s, logs empty) | n/a | — |
| tails | 13/13 complete (re-runs) | 13/13 complete (re-runs) | equal |

Engine behavior identical by construction (`git diff` iter33→iter34 = config.py
one-liner): chain fires, fillers stream, aborts repair, zero VerifyLead/Booking LLM
gens on chain-booked calls — on BOTH models. Every divergence below is model behavior.

## Summary

Phase B works and is model-independent: the engine books invisibly on both models
(single attempts, gate_rejections ≈ 0, booking turns ≤6s). The models differ where
models always differ — revenue skill and speech polish. **gpt-5.2 sells:**
4 hire-band calls, no critical FAILs, holds the Brenda gauntlet (36t book) and
varies hostile handling (Ray). **gpt-4.1 is faster and cheaper-feeling** (0.88s p50,
fewer gens) but leaves money and polish on the table: Brenda collapse (explicit
"book it" → fake-book, 0 tools — the single worst call of either battery), Ray
verbatim loop ×7 (HUMANIZED FAIL), no hire-band call, three echo-hygiene slips
(Maria wrong-number, Marcus "filled up" fib, Pedro calc ×3), Pedro no-end_call.
Counterpoint recorded: 4.1 held AI-identity honesty ×4 on Daniel where 5.2 flipped
(t10 "a real person") — prompt-track item either way (prompts frozen).

OTEL record: the self-hosted ingest silently drops whole export batches in
transient clusters (payloads ≤3.2KB — not size; no server access to confirm).
Landed permanent value: census-based `tail_complete` (True/False/None) in EVERY
per-persona json_log + LOUD SHORTFALL + quiescence wait + 6 regression tests
(suite 147). Tails complete 13/13 on both models after same-branch re-runs —
meters now disclose instead of silently undercounting.

Efficiency gates (both models): gens ≤300 and silent% ≤30 still unmet
(366/54.9% → 340/36.8%). Residual is the per-state extract→complete→transition
sawtooth — needs tool-protocol redesign (Phase B v2 scope, NOT this plan).

## Relevant info + merge recommendation

- Pedro over-books on BOTH models (5.2: 2/2, 4.1: 1/2) → persona/craft item,
  confirmed model-independent. Still deferred.
- Brenda-4.1 and Ray-4.1 are the only model-critical FAILs; both are speech/tool-drive
  behaviors, zero engine implication.
- **Recommend (Julio decides): merge the ENGINE** (iter33 Phase B chain + OTEL census —
  model-independent, proven ×2 batteries, suite 147), **keep gpt-5.2 default**.
  gpt-4.1 stays a config-flag fallback for latency/cost-sensitive traffic AFTER
  repetition-governor + tool-drive prompt work (Phase B v2). No merge without Julio
  say-so (LAW 0); nothing pushed.
