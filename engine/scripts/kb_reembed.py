#!/usr/bin/env python3
"""iter49 T3 — one-time re-INGEST of the 193 v2+faq corpus into `kb_chunks_v2`
(arctic-m 768-d) + replay sanity against the new table.

WHY a new table (not a per-row vector update): the v2+faq corpus content
DIFFERS from kb_chunks' 233 rows — T3 is a re-ingest (new rows), and additive
= zero-downtime + revertible (`kb_chunks` stays one release):

    kb_chunks       233 rows, 1536-d OpenAI space — UNTOUCHED (iter48 live)
    kb_chunks_v2    193 rows, 768-d arctic-m      — built here; selected via
                    RAG_TABLE_NAME at the T4 cutover (flip together with
                    RAG_EMBEDDING_MODEL + RAG_FILTER_SCORE=0.24)

Documents take NO arctic query prefix (queries only — that asymmetry is why
KBStore.ingest refuses local models). Idempotent: re-runs skip rows already
present (kb + sha256(content)); kb_chunks is only ever SELECT-counted.

Run from the engine worktree (cwd matters: Settings loads .env from cwd, and
~/fastembed/ shadows the fastembed package when cwd=~):

    .venv/bin/python scripts/kb_reembed.py                  # ingest + sanity
    .venv/bin/python scripts/kb_reembed.py --replay-only    # sanity table only
    .venv/bin/python scripts/kb_reembed.py --db postgresql://…

Corpus: /home/julio/projects/diallux_kb_lab/corpi/it7_faq.json
(list of 193 {"kb", "content"}; v2 chunker + faq-kb).
Pinned pack: research/surgeon/iter49p-sharp-rag/02_replay_report.md
(arctic-m 768-d, threads>=4, threshold 0.24, query prefix for QUERIES only,
lane-B industry anchor, leak values $-formatted).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import get_settings              # noqa: E402
from diallux.rag import KBStore                      # noqa: E402

ARCTIC = "snowflake/snowflake-arctic-embed-m"    # org slash — bare id raises ValueError
CACHE_DIR = "/home/julio/fastembed/models"
THREADS = 4
DIMS = 768
BATCH = 256
CORPUS = "/home/julio/projects/diallux_kb_lab/corpi/it8_plumbing.json"
TABLE = "kb_chunks_v2"
FILTER = 0.24          # pinned pack: arctic cosine (proper-10 10/10, junk 15->1)
TOP_K = 3

# iter52 T2: vertical tagging. Only industry rows carry a vertical, parsed
# from the content heading's FIRST line (deterministic, no LLM/classifier).
# Header row (bare KB heading on line 1) -> literal 'header' (never pinned,
# never resolved-to). All non-industry rows (incl. the plumbing deep-dive
# KB) keep vertical = NULL.
_KB_HEADER = "[industry | Industry-Specific Sales Knowledge Base]"
_VERT_RE = re.compile(
    r"^\[industry \| Industry-Specific Sales Knowledge Base > (.+?) > [^\]]+\]$")


def parse_vertical(kb: str, content: str) -> str | None:
    """iter52 T2: vertical tag from the chunk heading. Pure function.

    Validated live at plan time: 56/56 content rows + 1 header parsed
    (0 unparsed). The header row is tagged 'header' — the pin resolver
    stoplists it and the embed fallback skips a header top-1."""
    if kb != "industry":
        return None
    first = content.split("\n", 1)[0].strip()
    if first == _KB_HEADER:
        return "header"
    m = _VERT_RE.match(first)
    return m.group(1).strip() if m else None

# _STATE_KBS draft (iter49 plan T4) — lane-B scopes for the replay sanity
INTAKE_KBS = ["pain-points", "call-context", "sales-language",
              "voice-ai-capabilities", "are-you-ai", "sales-psychology",
              "industry", "hipaa", "call-closing"]
DISCOVERY_KBS = INTAKE_KBS + ["discovery-bridge"]

# The 7 replay queries — the iter48 live-replay diagnosis (4 booker calls)
# replayed under iter49 lane semantics. Q1-Q7 texts are grounded: tag texts
# are the REAL refer-tag `what`s from diallux/prompts/*.md (lane-A format
# "tag.what" / "tag.what: dv values" as the pre-flight ran them), lane-B
# texts are the gold-chat caller utterances (diallux_kb_lab results.db,
# run_id=replay); leak values $-formatted (pinned pack #6 — raw numerals
# refuse at 0.24). hard=True rows are the plan's asserts: mirror->
# pain-points, price-push deferral->sales-language, closing->call-closing.
#
# Q8 (soft, honest finding for the report/T4): the deferral LANE-A query
# "deferral escalation: {pain_frame}" tops out at 0.2117 < 0.24 against
# sales-language — the tag lane alone cannot surface the deferral KB; the
# price-push rides lane B (Q4, 0.3045 measured). T4 may want pricing
# vocabulary in the deferral lane query ("deferral escalation: what does
# this cost" = 0.2889 passes).
SANITY_QUERIES = [
    ("Intake mirror (lane A)",
     "the empathic mirror missed calls piling up while the front desk "
     "is on the other line",
     ["pain-points"], "pain-points", True),
    ("Intake caller (lane B, industry anchor)",
     "we're a dental practice and we keep missing new-patient calls "
     "(their industry: dental)",
     INTAKE_KBS, "industry", False),
    ("Discovery how-it-works (lane B, gold text)",
     "I saw your ad on Facebook about answering missed calls. I keep "
     "losing patients after hours, so I'm curious how it works.",
     DISCOVERY_KBS, "discovery-bridge", False),
    ("Discovery price-push (lane B, deferral)",
     "okay but what's your pricing? what does this cost every month?",
     DISCOVERY_KBS, "sales-language", True),
    ("Discovery deferral (lane A probe — known miss)",
     "deferral escalation: losing patients after hours",
     ["sales-language"], "sales-language", False),
    ("Offer re-anchor (lane A)",
     "re-anchoring missed revenue $259800 a month",
     ["sales-psychology"], "sales-psychology", False),
    ("Closer loss playback (lane A)",
     "loss playback $60000 a week / $259800 a month",
     ["sales-psychology"], "sales-psychology", False),
    ("Closing anchor (hand-written)",
     "warm goodbye wrap-up next steps",
     ["call-closing", "sales-language"], "call-closing", True),
]


def load_corpus() -> list[dict]:
    items = json.loads(Path(CORPUS).read_text(encoding="utf-8"))
    assert isinstance(items, list) and items, "corpus must be a non-empty list"
    for it in items:
        assert set(it) >= {"kb", "content"} and it["kb"] and it["content"], it
    return items


def _sha(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def embed_documents(texts: list[str]) -> list[list[float]]:
    """DOCUMENT embeddings via fastembed directly — NO arctic query prefix
    (queries only; that asymmetry is the whole point of this script)."""
    from fastembed import TextEmbedding
    te = TextEmbedding(model_name=ARCTIC, cache_dir=CACHE_DIR, threads=THREADS)
    vecs: list[list[float]] = []
    for i in range(0, len(texts), BATCH):
        vecs.extend([[float(x) for x in v] for v in te.embed(texts[i:i + BATCH])])
    assert len(vecs) == len(texts), "fastembed returned wrong count"
    assert all(len(v) == DIMS for v in vecs), "fastembed returned wrong dims"
    return vecs


async def ingest(dsn: str, corpus: list[dict]) -> bool:
    import asyncpg
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
                id BIGSERIAL PRIMARY KEY,
                kb TEXT NOT NULL,
                content TEXT NOT NULL,
                embedding VECTOR({DIMS}) NOT NULL,
                created_at TIMESTAMPTZ DEFAULT now()
            )""")
        await conn.execute(
            f"CREATE INDEX IF NOT EXISTS {TABLE}_kb_idx ON {TABLE} (kb)")
        # iter52 T2: additive vertical column (owner-directed migration).
        # NO new index — the 57 industry rows sit behind the existing kb_idx.
        await conn.execute(
            f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS vertical TEXT")
        n_prod_before = await conn.fetchval("SELECT count(*) FROM kb_chunks")
        existing = {(r["kb"], _sha(r["content"])) for r in
                    await conn.fetch(f"SELECT kb, content FROM {TABLE}")}
        pending = [it for it in corpus
                   if (it["kb"], _sha(it["content"])) not in existing]
        print(f"ingest: corpus={len(corpus)} existing={len(existing)} "
              f"pending={len(pending)} (idempotent skip on kb+sha256)")
        if pending:
            t0 = time.perf_counter()
            vectors = embed_documents([it["content"] for it in pending])
            emb_ms = (time.perf_counter() - t0) * 1000
            t0 = time.perf_counter()
            await conn.executemany(
                f"INSERT INTO {TABLE} (kb, content, embedding, vertical) "
                "VALUES ($1, $2, $3::vector, $4)",
                [(it["kb"], it["content"],
                  "[" + ",".join(f"{x:.6f}" for x in v) + "]",
                  parse_vertical(it["kb"], it["content"]))
                 for it, v in zip(pending, vectors)])
            print(f"ingest: embedded+inserted {len(pending)} rows "
                  f"(embed {emb_ms:.0f}ms, insert "
                  f"{(time.perf_counter() - t0) * 1000:.0f}ms, no query prefix)")
        # iter52 T2 backfill: tag EVERY existing industry row by heading
        # parse (unconditional UPDATE — same-value re-runs are no-ops; the
        # v2 trap: plain re-embed idempotence skips rows, so a plain re-run
        # tags NOTHING without this backfill).
        rows = await conn.fetch(
            f"SELECT id, content FROM {TABLE} WHERE kb='industry'")
        for r in rows:
            await conn.execute(
                f"UPDATE {TABLE} SET vertical=$1 WHERE id=$2",
                parse_vertical("industry", r["content"]), r["id"])
        print(f"vertical backfill: {len(rows)} industry rows re-tagged "
              "(unconditional; identical values are no-ops)")
        # HNSW AFTER the inserts (full-corpus build, not incremental)
        await conn.execute(
            f"CREATE INDEX IF NOT EXISTS {TABLE}_emb_idx "
            f"ON {TABLE} USING hnsw (embedding vector_cosine_ops)")
        n_v2 = await conn.fetchval(f"SELECT count(*) FROM {TABLE}")
        dims = {r[0] for r in await conn.fetch(
            f"SELECT DISTINCT vector_dims(embedding) FROM {TABLE}")}
        n_prod_after = await conn.fetchval("SELECT count(*) FROM kb_chunks")
        print(f"verify: {TABLE}={n_v2} rows dims={dims}; "
              f"kb_chunks {n_prod_before}->{n_prod_after} (must be equal)")
        ok = n_v2 == len(corpus) and dims == {DIMS} \
            and n_prod_before == n_prod_after
        print(f"ingest: {'OK' if ok else 'FAILED'}")
        return ok
    finally:
        await conn.close()


async def replay(dsn: str) -> int:
    """The 7 replay queries against kb_chunks_v2 via the REAL plumbing
    (KBStore + rag_table_name + local_embed_fn — queries carry the arctic
    prefix automatically). Prints the sanity table; hard asserts exit 1."""
    store = KBStore(
        database_url=dsn,
        kb_dir=get_settings().knowledge_base_dir,
        embedding_model=ARCTIC,
        local_embed_cache_dir=CACHE_DIR,
        local_embed_threads=THREADS,
        top_k=TOP_K,
        filter_score=FILTER,
        char_budget=1600,
        table_name=TABLE,
    )
    failures: list[str] = []
    print(f"\nreplay sanity vs {TABLE} (filter {FILTER}, top_k {TOP_K}, "
          "arctic query prefix ON)")
    print(f"{'query':38s} {'ms':>5s}  top-3 (kb:score)")
    print("-" * 100)
    for label, query, scope, expected, hard in SANITY_QUERIES:
        t0 = time.perf_counter()
        chunks = await store.retrieve(query, scope)
        ms = (time.perf_counter() - t0) * 1000
        tops = ", ".join(f"{c['kb']}:{c['score']}" for c in chunks[:3]) or "-"
        hit = any(c["kb"] == expected for c in chunks[:3])
        verdict = ("PASS" if hit else ("FAIL *" if hard else "miss"))
        print(f"{label:38s} {ms:5.0f}  {tops}   [{verdict} -> {expected}]")
        if hard and not hit:
            failures.append(f"{label}: expected {expected} in top-3, got [{tops}]")
    await store.close()
    if failures:
        print("\nHARD-ASSERT FAILURES:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nreplay sanity: all hard asserts PASS "
          "(mirror->pain-points, deferral->sales-language, closing->call-closing)")
    return 0


async def main() -> int:
    ap = argparse.ArgumentParser(
        description="iter49 T3: re-ingest the 193 v2+faq corpus into "
                    f"{TABLE} (arctic-m {DIMS}-d) + replay sanity")
    ap.add_argument("--db", default="",
                    help="postgres URL (default: DATABASE_URL from .env)")
    ap.add_argument("--replay-only", action="store_true",
                    help="skip the ingest; print the sanity table only")
    args = ap.parse_args()

    dsn = args.db or get_settings().database_url
    if not dsn:
        print("error: no DATABASE_URL (set it in .env or pass --db)")
        return 2
    corpus = load_corpus()
    print(f"corpus: {CORPUS} -> {len(corpus)} chunks "
          f"({len({it['kb'] for it in corpus})} KBs)")
    if not args.replay_only:
        if not await ingest(dsn, corpus):
            return 1
    return await replay(dsn)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
