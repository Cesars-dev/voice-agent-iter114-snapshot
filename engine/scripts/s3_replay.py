#!/usr/bin/env python3
"""iter105 T5 — S3 replay classifier. Committed on branch engine/105-ask-hold
as engine/scripts/s3_replay.py (owner order 2026-10-10); this file is the source
of record.

Classifies every `query_livecall_slots` pull turn in the archived harness corpus
turn-level json_logs:
  - legit pull   : caller text carries a day/time/soonest expression (answer-first)
  - bad pull     : no day/time in caller text (pulled without an answer)
  - S2-shaped    : same turn also ran transition_to_ConfirmSlots
  - S1-shaped    : agent text carries the day-question pattern
  - SURVIVOR     : bad pull that is neither S1- nor S2-shaped (what S3 must catch)
  - COLLATERAL   : legit pull that is S2-shaped (would defer once -> extra exchange)
No models / embeddings / APIs — pure regex over json_logs.
"""
import json, glob, re, os, sys

DAY_RE = re.compile(
    r"\b(tomorrow|today|tonight|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"afternoon|morning|evening|soonest|earliest|next day|asap|as soon as possible)\b"
    r"|\b\d{1,2}:\d{2}\b|\b\d{1,2}\s*(am|pm)\b", re.I)

Q_RE = re.compile(
    r"(later today or tomorrow|which works|which one works|prefer\b.*\bday|"
    r"what day|which day|next day|earlier or later|prefer earlier|prefer later)", re.I)

def corpus():
    files = sorted(set(
        glob.glob('/tmp/opencode/wt-10*/engine/tests/llm2llm/json_logs/*.json')
        + glob.glob('/home/julio/projects/clean_diallux_SDR/engine/tests/llm2llm/json_logs/*.json')))
    return files

def main():
    tot = legit = bad = bad_s2 = bad_s1 = survivors = collat_s2 = requery = 0
    surv_rows, collat_rows = [], []
    for f in corpus():
        try:
            d = json.load(open(f))
        except Exception:
            continue
        call_day_prior = False   # any day word in a PRIOR caller turn of THIS call
        for t in d.get('transcript', []):
            tools = t.get('tools') or []
            caller = t.get('caller', '') or ''
            agent = t.get('agent', '') or ''
            has_day = bool(DAY_RE.search(caller))
            if 'query_livecall_slots' not in tools:
                call_day_prior = call_day_prior or has_day
                continue
            tot += 1
            is_s2 = 'transition_to_ConfirmSlots' in tools
            is_s1 = bool(Q_RE.search(agent))
            if has_day:
                legit += 1
                if is_s2:
                    collat_s2 += 1
                    collat_rows.append((os.path.basename(f), t.get('turn'),
                                        caller[:100], agent[:100]))
            elif call_day_prior:
                # day chosen earlier in the call -> re-query/preferred_time pull, not the disease
                requery += 1
            else:
                bad += 1
                if is_s2:
                    bad_s2 += 1
                elif is_s1:
                    bad_s1 += 1
                else:
                    survivors += 1
                    surv_rows.append((os.path.basename(f), t.get('turn'), t.get('state'),
                                      caller[:130], agent[:170]))
            call_day_prior = call_day_prior or has_day

    print("=== iter105 T5 — S3 replay baseline (turn-level json_logs) ===")
    print(f"corpus files scanned      : {len(corpus())}")
    print(f"pull turns (total)        : {tot}")
    print(f"  legit (caller named day): {legit}")
    print(f"    collateral (S2-shaped): {collat_s2}  <- defer costs one extra exchange (killed by the S2 day-guard)")
    print(f"  re-query (day prior)    : {requery}")
    print(f"  bad (no day, ever)      : {bad}")
    print(f"    fixed by S2 (co-fire) : {bad_s2}")
    print(f"    fixed by S1 (question): {bad_s1}")
    print(f"    SURVIVORS             : {survivors}  <- decides the optional S3 arm")
    print()
    print("--- SURVIVORS (bad pulls dodging S1+S2) ---")
    for r in surv_rows:
        print(f"  {r[0]} T{r[1]} [{r[2]}]")
        print(f"    caller: {r[3]}")
        print(f"    agent : {r[4]}")
    if not surv_rows:
        print("  (none)")
    print()
    print("--- COLLATERAL sample (legit pulls that are S2-shaped) ---")
    for r in collat_rows[:12]:
        print(f"  {r[0]} T{r[1]}  caller: {r[2]}")
    return 0

if __name__ == '__main__':
    sys.exit(main())
