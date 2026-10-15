# git_tree.md — living ledger (iters × branches × commits)

> Updated at EVERY task. One row per iter/branch. "voice" flag = iter is part of the deployed voice lineage.
> Snapshots live in `Dialux_SDR/v5-snapshots/` (folders, never git). Reports in `reports/`.

| iter | branch | commit | voice-deployed? | status |
|---|---|---|---|---|
| iter20 | main (snapshot `v1.8-iter20-20260906`) | 2dc13ff (baseline marker) | **YES — deployed voice path (v1.8)** | done |
| iter21 | main (WIP on main, no branch) | 2dc13ff (WIP) | voice-path (browser-mic bridge, `state_machine_voice` lineage) | **IN PROGRESS** — `v1.9` snapshot never cut |
| iter23 | main (no branch; artifacts committed at baseline) | f65e63d | no (chat-track eval) | done — toe-to-toe, `reports/iter23_*` |
| iter24 | main (no branch; artifacts committed at baseline) | f65e63d | no (audit) | done — deviation audit, `reports/iter24_*` |
| iter25 baseline | main | **f65e63d** (T0 pin) | no (chat-only experiment) | pinned 2026-09-06 |
| iter25-a | `iter25-a` (fork of f65e63d) | — | no | BUILD (T1) |
| iter25-b | `iter25-b` (fork of f65e63d + 942c180) | c1cc944 (common tags+gate via cherry-pick 29c93a2) | no | BUILT (T2 done, suite 93 green incl. 8 tag tests, no battery) |
| iter26 | on `iter25-b` @ c1cc944 (no new commit to prompts/code) | e04d644+iter26 ledger | no | **first-13 ladder DONE 2026-09-06** — 12/13 PASS, expect-match 8/9 (Pedro over-book ×6), curve 4/4 sane, books SALES avg 13.1/14, LLM p50 1.30s; report `tasks/surgeon/v5-pgvector-gpt52-argfix/18_iter26_b_first13_deepdive.md`; NO MERGE |
| iter27 | `iter27-human-slots` (fork of `iter25-b` @ 11f4d93) | e69f512 | no | **LIVE-slots first-13 battery DONE 2026-09-07** — 12/13 true PASS, expect-match 8/9 (Pedro over-book ×7, masked by cal_400), spoken ISO-leak 0, 7 real Cal.com bookings created + ALL cancelled; **BLOCKER: validator :8003 rejects human selected_time (ISO-only) → Maria loop ×15** (OTEL-confirmed); report `tasks/surgeon/v5-pgvector-gpt52-argfix/19_iter27_live_slots_battery.md`; NO MERGE |
| iter25-c | `iter25-c` (fork of f65e63d) | — | no | BUILD (T3, Amendment 1) |
| iter25-d | `iter25-d` (fork of f65e63d, pointer only, NO commit) | — (942c180) | no | **BLOCKED** — `gpt-5.2-mini` does not exist in OpenAI models list (verified 2026-09-06); skipped in T5; awaiting Julio pick (lighter than gpt-5.2) |
| iter28 | `iter28-server-time-payload` (fork of `iter27-human-slots` @ 8c0a0bb) | 62c9a81 | no | **first-13 LIVE battery DONE 2026-09-07** — 13/13 PASS, ISO-leak 0, containment gate live-verified, 7 bookings + all cancelled, calendar clean; smoke + battery report `tasks/surgeon/iter28-server-time-payload/20_iter28_smoke.md`; **merged to `main` 2026-09-07 (Julio order)** |
| iter29 | `iter29-winner` @ 8272a68 | 8272a68 | no | one-trip-per-turn (A1–A5), suite 109 |
| iter30 | `iter30-fast` @ 6c2d1cf | 6c2d1cf (C2 end_call goodbye memory + history window 16 + cache surfacing; iter31 C3 @ 4acd69b, C3b @ 48d6ad3) | no | latency battery 1 — NO MERGE |
| iter31 | `iter31-c3b/c3c/c3d` (forks of 6c2d1cf) | dampener stack @ 5d32c60 | no | C3/C3b cache architecture + REJECTED dampener — NO MERGE |
| iter32 | `iter32-silent-round-gate` (fork of `48d6ad3`) | 5ac026b | no | silent-round payload gate + strict end_call — battery 2026-09-08: 12/13, ended 13/13, machine-gun DEAD (Frank 13t/Ray 6t), silent% 66→54; NO MERGE |
| iter33 | `engine/iter33-phaseb-engine-chain` (fork of `596f1bb`) | 9542a73+fixes | no | Phase B engine-owned booking chain + OTEL census (tail_complete in json_logs) — gpt-5.2 battery 12/13, 13/13 ended, gens 480→366, LLM p50 1.52s, SOP 4-hire/0-FAIL; suite 147; NO MERGE |
| iter36 | autopilot audit on `engine/iter33-phaseb-engine-chain` (no new branch — analysis only) | f70b856 | no | SOP-autopilot re-audit gpt-5.2 from wt-iter35 SDKs: confirms §23 (12/13, CALL 11/1/1, SALES 4 hire, HUM 10/3/0); report 08_sops_autopilot_gpt52.md + ITERATIONS §25; NO MERGE |
