"""Self-hosted RAG knowledge bases — Postgres + pgvector + OpenAI embeddings.

WHY (the hard reasoning, see DECISIONS.md §RAG):
  Your deployed Retell agent attaches its 9 knowledge bases with
  kb_config = {filter_score: 0.6, top_k: 3} — i.e. Retell was ALREADY doing
  server-side RAG: ~3 relevant chunks per turn, not the whole corpus. The
  lab engine tried to replicate KB access with `##slug-kb##` file expansion
  and (a) would inline the ENTIRE KB when it worked, and (b) actually never
  worked — its lookup searched `<slug>.md` while the shipped files are
  `<slug>-kb.md`, so every marker resolved to `[KB x MISSING]` (fixed in
  graph/subst.py). This module gives you the deployed behavior, self-hosted:

    user utterance -> ONE embedding call -> cosine top-k in pgvector (local)
                   -> ~1.6k chars of the most relevant KB excerpts
                   -> appended to a lean system prompt

  Defaults mirror the deployed kb_config: top_k=3 (RAG_TOP_K), score
  filtering approximated by cosine ranking. Chunking follows the KBs'
  heading structure so an excerpt is one complete tactic/objection.

  Cost model per turn:
    + 1 embedding call   ~60-120ms  (text-embedding-3-small)
    + pgvector query     ~5-15ms    (local, HNSW index)
    - a ~4k-token system prompt (state prompt + top-3 excerpts) instead of
      the ~16k-token everything-inline prompt the lab semantics imply
    Net: faster per round and better grounded (no attention dilution).

  Fallback: if Postgres/pgvector or the OpenAI key is unavailable, or the
  index is empty (ingest not run yet), retrieval returns nothing and the
  builder falls back to full inline KB expansion (now actually working).
  Nothing breaks; the system degrades to the lab-engine semantics.

Schema (created automatically on connect):
    CREATE EXTENSION IF NOT EXISTS vector;
    CREATE TABLE kb_chunks (
      id BIGSERIAL PRIMARY KEY, kb TEXT NOT NULL, content TEXT NOT NULL,
      embedding VECTOR(<dims>) NOT NULL);
    CREATE INDEX ... USING hnsw (embedding vector_cosine_ops);

Chunking: markdown KBs are split on headings, then packed to ~target_chars
with a small overlap so a retrieved chunk is self-contained.

Hermetic tests: `embed_fn` and the pool are injectable — see tests/test_rag.py.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

log = logging.getLogger("diallux.rag")

EmbedFn = Callable[[list[str]], Awaitable[list[list[float]]]]

DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMS = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    # iter49 T2: local arctic-m — pre-flight pinned pack
    # (research/surgeon/iter49p-sharp-rag/02_replay_report.md)
    "snowflake/snowflake-arctic-embed-m": 768,
}

# iter49 T2: models served by the LOCAL in-process fastembed embedder; any
# other rag_embedding_model keeps the OpenAI API path (exact iter48 behavior).
LOCAL_EMBED_MODELS = {"snowflake/snowflake-arctic-embed-m"}

# arctic-m query prefix (Snowflake spec; measured in the pre-flight: real-50
# junk-served 15 -> 1 at threshold 0.24 WITH the prefix)
ARCTIC_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

_CHUNK_TARGET = 700       # chars per chunk (≈170 tokens)
_CHUNK_OVERLAP = 90       # context carry-over
_HEADING_RE = re.compile(r"^#{1,3}\s+.+$", re.MULTILINE)


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #
def chunk_markdown(text: str, target: int = _CHUNK_TARGET, overlap: int = _CHUNK_OVERLAP) -> list[str]:
    """Heading-aware chunker: split on ##/### headings, then pack to `target`.

    Sales KBs are heading-structured; splitting there keeps each chunk about
    ONE tactic/objection, which is exactly the granularity the model needs.
    """
    sections: list[str] = []
    parts = re.split(r"(?m)^(#{1,3} .+)$", text)
    # re.split with a capturing group alternates [pre, heading, body, heading, body...]
    if len(parts) == 1:
        sections = [text]
    else:
        sections = [parts[0]]
        for i in range(1, len(parts) - 1, 2):
            sections.append(parts[i] + "\n" + parts[i + 1])
    chunks: list[str] = []
    for sec in sections:
        sec = sec.strip()
        if not sec:
            continue
        if len(sec) <= target:
            chunks.append(sec)
            continue
        # pack paragraphs
        paras, buf = re.split(r"\n\s*\n", sec), ""
        for p in paras:
            p = p.strip()
            if not p:
                continue
            if len(buf) + len(p) + 2 <= target:
                buf = (buf + "\n\n" + p) if buf else p
            else:
                if buf:
                    chunks.append(buf)
                # single paragraph longer than target: hard split with overlap
                while len(p) > target:
                    chunks.append(p[:target])
                    p = p[target - overlap:]
                buf = p
        if buf:
            chunks.append(buf)
    return [c for c in (c.strip() for c in chunks) if c]


# --------------------------------------------------------------------------- #
# OpenAI embeddings (injectable for hermetic tests)
# --------------------------------------------------------------------------- #
async def openai_embed_fn(model: str, api_key: str | None) -> EmbedFn:
    # iter21: ONE client per process — keep-alive connection reuse. The old
    # code built a fresh AsyncOpenAI (new TCP+TLS handshake) on EVERY embed
    # call: 190-900ms per turn vs ~60-120ms warm (Retell parity).
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=api_key)

    async def _embed(texts: list[str]) -> list[list[float]]:
        resp = await client.embeddings.create(model=model, input=texts)
        return [d.embedding for d in resp.data]
    return _embed


# --------------------------------------------------------------------------- #
# Local embeddings — fastembed in-process (iter49 T2, arctic-m 768-d)
# --------------------------------------------------------------------------- #
async def local_embed_fn(model: str, cache_dir: str, threads: int = 4,
                         prefix: str = ARCTIC_QUERY_PREFIX) -> EmbedFn:
    """Query embeddings on the local arctic-m model (768-d, $0, ~20-35ms).

    onnxruntime is synchronous AND the TextEmbedding constructor loads the
    ONNX session (~1.1s cold) — BOTH park on a worker thread via
    asyncio.to_thread so the event loop never blocks (iter49 T3: a loop-block
    stalls the bounded await's timer + TTS/STT — the "never a stall" rule;
    the pre-flight's async-off-hot-path directive; the AWAITED hot-path
    portion is pgvector only, ~3.4ms measured). QUERY semantics: every input
    text gets the arctic query prefix. DOCUMENT (re)embedding must NOT use
    this fn (documents take no prefix) — that is scripts/kb_reembed.py's job
    (iter49 T3).
    """
    from fastembed import TextEmbedding

    def _load() -> TextEmbedding:
        return TextEmbedding(model_name=model, cache_dir=cache_dir,
                             threads=threads)

    te = await asyncio.to_thread(_load)     # the ONNX session load parks too

    def _embed_sync(texts: list[str]) -> list[list[float]]:
        return [[float(x) for x in v] for v in te.embed([prefix + t for t in texts])]

    await asyncio.to_thread(_embed_sync, ["warm"])   # load the ONNX session once
    async def _embed(texts: list[str]) -> list[list[float]]:
        return await asyncio.to_thread(_embed_sync, texts)
    return _embed


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #
class KBStore:
    """pgvector-backed retrieval over the 9 Retell knowledge bases.

    One store is shared process-wide ( pools are event-loop bound, uvicorn
    runs one loop). Created lazily by `get_kb_store()`.
    """

    def __init__(
        self,
        database_url: str,
        kb_dir: str | Path,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
        api_key: str | None = None,
        local_embed_cache_dir: str = "/home/julio/fastembed/models",
        local_embed_threads: int = 4,
        embed_fn: EmbedFn | None = None,
        top_k: int = 4,
        filter_score: float = 0.40,
        char_budget: int = 1600,
        connect_timeout_s: float = 2.0,
        table_name: str = "kb_chunks",
    ):
        # iter49 T3: the table this store reads (rag_table_name knob; default
        # keeps iter48's kb_chunks exactly). Identifier-validated because it
        # reaches SQL text and is env-settable (RAG_TABLE_NAME).
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,63}", table_name):
            raise ValueError(f"rag: invalid table_name {table_name!r}")
        self.table_name = table_name
        self.database_url = database_url
        self.kb_dir = Path(kb_dir)
        self.embedding_model = embedding_model
        self.dims = EMBEDDING_DIMS.get(embedding_model, 1536)
        self.api_key = api_key
        self.local_embed_cache_dir = local_embed_cache_dir
        self.local_embed_threads = local_embed_threads
        self.embed_fn = embed_fn
        self.top_k = top_k
        self.filter_score = filter_score
        self.char_budget = char_budget
        self.connect_timeout_s = connect_timeout_s
        self._pool = None
        self._checked_indexed = False
        self._indexed = False
        self._failed_at = 0.0          # retry a dead DB at most once a minute
        self.stats = {"queries": 0, "chunks_returned": 0, "total_ms": 0.0}

    # ------------------------------------------------------------------ #
    async def connect(self) -> bool:
        """Lazy pool creation. Returns True when usable. Never raises."""
        if self._pool is not None:
            return True
        if time.monotonic() - self._failed_at < 60:
            return False
        try:
            import asyncpg
            self._pool = await asyncpg.create_pool(
                self.database_url, min_size=1, max_size=4,
                timeout=self.connect_timeout_s, command_timeout=5.0,
            )
            await self._ensure_schema()
            return True
        except Exception as exc:
            self._pool = None
            self._failed_at = time.monotonic()
            log.warning("rag: pgvector unavailable (%s); inline KB fallback", exc)
            return False

    async def close(self):
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def ready(self) -> bool:
        return self._pool is not None

    async def _ensure_schema(self):
        assert self._pool is not None
        t = self.table_name
        async with self._pool.acquire() as conn:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            await conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {t} (
                    id BIGSERIAL PRIMARY KEY,
                    kb TEXT NOT NULL,
                    content TEXT NOT NULL,
                    embedding VECTOR({self.dims}) NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT now()
                )""")
            await conn.execute(
                f"CREATE INDEX IF NOT EXISTS {t}_kb_idx ON {t} (kb)")
            try:
                await conn.execute(
                    f"CREATE INDEX IF NOT EXISTS {t}_emb_idx "
                    f"ON {t} USING hnsw (embedding vector_cosine_ops)")
            except Exception:
                pass    # pgvector < 0.5: ivfflat only; sequential scan still fine at 9 KBs
            n = await conn.fetchval(f"SELECT count(*) FROM {t}")
            self._indexed = bool(n)
            self._checked_indexed = True

    async def indexed(self) -> bool:
        if not self._checked_indexed:
            if not await self.connect():
                return False
        return self._indexed

    async def _ensure_embed_fn(self) -> EmbedFn:
        """iter49 T2: embedder selection by model — arctic-m runs the local
        fastembed path (768-d, in-process, query-prefixed); anything else
        keeps the OpenAI API path (exact iter48 behavior). One fn per store,
        built on first use and reused."""
        if self.embed_fn is None:
            if self.embedding_model in LOCAL_EMBED_MODELS:
                self.embed_fn = await local_embed_fn(
                    self.embedding_model, self.local_embed_cache_dir,
                    self.local_embed_threads)
            else:
                self.embed_fn = await openai_embed_fn(self.embedding_model, self.api_key)
        return self.embed_fn

    @staticmethod
    def _chunk_out(r) -> dict[str, Any]:
        """Row -> chunk dict. iter49 T4: carries the chunk PK (`id`) — dedupe
        keys on the DB id, never content bytes (sliding chunk windows break
        byte dedupe). Fakes/hermetic rows without `id` get None (callers
        treat None as no-PK and fall back to (kb, content) keys)."""
        keys = r.keys() if hasattr(r, "keys") else ()
        rid = r["id"] if "id" in keys else None
        return {"id": rid, "kb": r["kb"], "content": r["content"],
                "score": round(float(r["score"]), 4)}

    # ------------------------------------------------------------------ #
    async def retrieve(self, query_text: str, kb_slugs: list[str],
                       vec: list[float] | None = None) -> list[dict[str, Any]]:
        """Top-k chunks across `kb_slugs`, cosine distance, char-budgeted.

        `vec` (iter44 T6c): a precomputed query vector (from embed_query on a
        drift check) — skips the re-embed so the re-retrieval costs pgvector
        time only (~15 ms).

        Returns [{"id", "kb", "content", "score"}] best-first. Empty list on
        any failure (caller falls back to inline KBs).
        """
        if not kb_slugs or not query_text.strip():
            return []
        t0 = time.perf_counter()
        try:
            if not await self.connect():
                return []
            # iter44 T6a: the embed client is built ONCE (first use) and
            # reused — a fresh AsyncOpenAI per retrieve paid a new TCP+TLS
            # handshake (190-900 ms measured vs ~60-120 ms warm).
            await self._ensure_embed_fn()
            if vec is None:
                vectors = await self.embed_fn([query_text.strip()[:1000]])
                vec = "[" + ",".join(f"{x:.6f}" for x in vectors[0]) + "]"
            else:
                vec = "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
            assert self._pool is not None
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    f"""
                    SELECT id, kb, content, 1 - (embedding <=> $1::vector) AS score
                    FROM {self.table_name}
                    WHERE kb = ANY($2::text[]) AND 1 - (embedding <=> $1::vector) >= $4
                    ORDER BY embedding <=> $1::vector
                    LIMIT $3
                    """,
                    vec, kb_slugs, self.top_k, self.filter_score,
                )
            out, seen, budget = [], set(), self.char_budget
            for r in rows:
                c = self._chunk_out(r)
                key = (c["kb"], c["content"][:80])
                if key in seen:
                    continue
                seen.add(key)
                if budget - len(c["content"]) < 0 and out:
                    break
                out.append(c)
                budget -= len(c["content"])
            self.stats["queries"] += 1
            self.stats["chunks_returned"] += len(out)
            self.stats["total_ms"] += (time.perf_counter() - t0) * 1000
            return out
        except Exception as exc:
            self._failed_at = time.monotonic()
            log.warning("rag retrieve failed (%s); inline fallback this turn", exc)
            return []

    # ------------------------------------------------------------------ #
    async def retrieve_lanes(
            self, lanes: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        """iter49 T4: multi-lane retrieval — ONE batched embed call carrying
        ALL lane query texts (the local embedder batches natively; a batch of
        n costs ~one sequential embed), then the pgvector queries run
        CONCURRENTLY on the pool. NO sequential per-lane embeds (the
        ≤+60ms awaited-hot-path guard depends on this shape).

        lanes: [{"query": str, "scope": [kb slugs]}]. Returns one chunk-list
        per lane (retrieve() shape, ids included); empty lists on failure —
        the merge treats empties as no-evidence, never raises."""
        empty: list[list[dict[str, Any]]] = [[] for _ in lanes]
        if not lanes:
            return empty
        t0 = time.perf_counter()
        try:
            if not await self.connect():
                return empty
            await self._ensure_embed_fn()
            vecs = await self.embed_fn(
                [str(l["query"]).strip()[:1000] for l in lanes])

            async def _one(vec: list[float], scope: list[str]) -> list[dict[str, Any]]:
                vec_s = "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
                async with self._pool.acquire() as conn:   # type: ignore[union-attr]
                    rows = await conn.fetch(
                        f"""
                        SELECT id, kb, content, 1 - (embedding <=> $1::vector) AS score
                        FROM {self.table_name}
                        WHERE kb = ANY($2::text[]) AND 1 - (embedding <=> $1::vector) >= $4
                        ORDER BY embedding <=> $1::vector
                        LIMIT $3
                        """,
                        vec_s, scope, self.top_k, self.filter_score,
                    )
                return [self._chunk_out(r) for r in rows]

            out = list(await asyncio.gather(
                *(_one(v, l["scope"]) for v, l in zip(vecs, lanes))))
            self.stats["queries"] += 1
            self.stats["total_ms"] += (time.perf_counter() - t0) * 1000
            return out
        except Exception as exc:
            self._failed_at = time.monotonic()
            log.warning("rag retrieve_lanes failed (%s); no chunks this turn", exc)
            return empty

    # ------------------------------------------------------------------ #
    async def embed_query(self, text: str) -> list[float]:
        """One query vector via the reused embed client (iter44 T6a/T6c).
        Used by drift checks and RAG staging to dedupe embeds."""
        await self._ensure_embed_fn()
        return (await self.embed_fn([text.strip()[:1000]]))[0]

    # ------------------------------------------------------------------ #
    # iter52 T4: pinned industry vertical — METADATA lookup only (NO embed,
    # NO <=>). The vertical is a per-call constant once known; a vector
    # search would re-compute a constant. Fresh/hermetic tables lack the
    # vertical column (KBStore._ensure_schema doesn't create it) — ANY
    # failure returns [] and the caller degrades to today's vector lane.
    async def pinned_by_tag(self, tag: str) -> list[dict[str, Any]]:
        """ALL chunks of one industry vertical by metadata, id-ordered.

        Returns [{"id", "kb", "content", "score": None, "vertical": tag}]
        best-never (no ranking — the vertical is a constant set). Empty
        list on ANY failure (missing column, DB down, empty tag)."""
        if not tag or not tag.strip():
            return []
        try:
            if not await self.connect():
                return []
            assert self._pool is not None
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT id, kb, content FROM {self.table_name} "
                    "WHERE kb='industry' AND vertical=$1 ORDER BY id", tag)
            out = [{"id": r["id"], "kb": r["kb"], "content": r["content"],
                    "score": None, "vertical": tag} for r in rows]
            self.stats["queries"] += 1
            return out
        except Exception as exc:
            log.warning("rag pinned_by_tag(%r) failed (%s); no pin this "
                        "turn", tag, exc)
            return []

    async def vertical_of(self, chunk_id) -> str | None:
        """The vertical tag of one chunk id (embed-fallback step 4), or
        None on missing column / DB down / unknown id. NEVER raises."""
        if chunk_id is None:
            return None
        try:
            if not await self.connect():
                return None
            assert self._pool is not None
            async with self._pool.acquire() as conn:
                return await conn.fetchval(
                    f"SELECT vertical FROM {self.table_name} WHERE id=$1",
                    chunk_id)
        except Exception as exc:
            log.warning("rag vertical_of(%r) failed (%s)", chunk_id, exc)
            return None

    # ------------------------------------------------------------------ #
    async def ingest(self, rebuild: bool = True) -> dict:
        """(Re)build the index from the KB markdown files. Run via
        scripts/rag_ingest.py at deploy time — NOT on the hot path."""
        # iter49 T2: local-model document embedding is scripts/kb_reembed.py
        # (T3) — documents take NO query prefix, so this OpenAI-path ingester
        # must refuse rather than silently prefix-poison a 768-d corpus.
        if self.embedding_model in LOCAL_EMBED_MODELS:
            raise RuntimeError(
                "rag: document (re)embedding for the local embedder runs via "
                "scripts/kb_reembed.py (documents take no query prefix); "
                "KBStore.ingest is the OpenAI-path ingester")
        # iter49 T3: this ingester owns kb_chunks ONLY — kb_chunks_v2 (or any
        # other rag_table_name) is scripts/kb_reembed.py's corpus; a rebuild
        # here must never TRUNCATE a table it doesn't own.
        if self.table_name != "kb_chunks":
            raise RuntimeError(
                f"rag: KBStore.ingest owns kb_chunks only (rag_table_name="
                f"{self.table_name!r}); that table is built by "
                "scripts/kb_reembed.py")
        if not await self.connect():
            raise RuntimeError("rag: postgres/pgvector unavailable; cannot ingest")
        # iter44 T6a: same once-and-reuse client policy as retrieve()
        await self._ensure_embed_fn()
        files = sorted(self.kb_dir.glob("*.md"))
        if not files:
            raise RuntimeError(f"rag: no .md files under {self.kb_dir}")
        rows: list[tuple[str, str]] = []
        for f in files:
            slug = f.stem.removesuffix("-kb")
            for chunk in chunk_markdown(f.read_text(encoding="utf-8")):
                rows.append((slug, chunk))
        # embed in batches (API limit safety)
        vectors: list[list[float]] = []
        B = 64
        for i in range(0, len(rows), B):
            vectors.extend(await self.embed_fn([c for _, c in rows[i:i + B]]))
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            if rebuild:
                await conn.execute(f"TRUNCATE {self.table_name}")
            await conn.executemany(
                f"INSERT INTO {self.table_name} (kb, content, embedding) VALUES ($1, $2, $3::vector)",
                [(kb, c, "[" + ",".join(f"{x:.6f}" for x in v) + "]")
                 for (kb, c), v in zip(rows, vectors)],
            )
        self._indexed = True
        self._checked_indexed = True
        return {"kbs": len(files), "chunks": len(rows), "embedding_model": self.embedding_model}


# --------------------------------------------------------------------------- #
# Process-wide singleton (one pool, one event loop)
# --------------------------------------------------------------------------- #
_SINGLETON: dict[str, KBStore] = {}


async def get_kb_store(settings) -> KBStore | None:
    """Returns the shared KBStore, or None when RAG is disabled/unavailable.

    RAG_MODE: "auto" (default; use pgvector when reachable+indexed, else inline)
              "rag"    (require pgvector; still falls back per-turn on errors)
              "inline" (never touch Postgres; exact V1 KB expansion)
    """
    mode = getattr(settings, "rag_mode", "auto")
    if mode == "inline":
        return None
    db = getattr(settings, "database_url", "") or ""
    if not db:
        return None
    key = db
    store = _SINGLETON.get(key)
    if store is None:
        store = KBStore(
            database_url=db,
            kb_dir=settings.knowledge_base_dir,
            embedding_model=settings.rag_embedding_model,
            api_key=settings.openai_api_key or None,
            local_embed_cache_dir=getattr(
                settings, "local_embed_cache_dir", "/home/julio/fastembed/models"),
            local_embed_threads=getattr(settings, "local_embed_threads", 4),
            top_k=settings.rag_top_k,
            filter_score=settings.rag_filter_score,
            char_budget=settings.rag_char_budget,
            table_name=getattr(settings, "rag_table_name", "kb_chunks"),
        )
        _SINGLETON[key] = store
    if not await store.connect():
        # iter44 T4: name the degraded mode loudly — with fallback_inline_kb
        # False the session runs WITHOUT KB excerpts (never the inline blob).
        if not getattr(settings, "fallback_inline_kb", False):
            log.warning("RAG fallback inline DISABLED (fallback_inline_kb=False) "
                        "— running without KB excerpts")
        return None
    if not await store.indexed():
        log.warning("rag: index empty (run scripts/rag_ingest.py); inline fallback")
        if not getattr(settings, "fallback_inline_kb", False):
            log.warning("RAG fallback inline DISABLED (fallback_inline_kb=False) "
                        "— running without KB excerpts")
        return None
    return store


def reset_singleton_for_tests():
    _SINGLETON.clear()


# --------------------------------------------------------------------------- #
# System-prompt assembly helpers (used by graph/builder.py)
# --------------------------------------------------------------------------- #
_KB_MARKER_RE = re.compile(r"##([\w-]+)-kb##")

# iter25-b: Julio's #[refer <kb>-kb for <what> — {{dv}} ...] tags. The tag is
# model-facing prose (branch A) AND machine-readable (branch B): the KB slug
# sets retrieval scope, <what> + the dv VALUES enrich the per-turn query.
REFER_TAG_RE = re.compile(r"#\[refer ([\w-]+?)(?:-kb)? for ([^—\]]+?) — ([^\]]+)\]")
_DV_TOKEN_RE = re.compile(r"\{\{(\w+)\}\}")


def parse_refer_tags(text: str) -> list[dict[str, Any]]:
    """Parse `#[refer <kb> for <what> — {{dv}} ...]` tags out of a prompt.

    Returns [{"kb": slug (no -kb suffix), "what": str, "dvs": [names]}] in
    document order. Tags with malformed bodies simply don't match — prose
    fallback is the prompt text itself.
    """
    out: list[dict[str, Any]] = []
    for m in REFER_TAG_RE.finditer(text):
        kb = m.group(1).removesuffix("-kb")
        out.append({"kb": kb, "what": m.group(2).strip(),
                    "dvs": _DV_TOKEN_RE.findall(m.group(3))})
    return out


def kb_slugs_in(text: str) -> list[str]:
    """Ordered unique KB slugs referenced by ##slug-kb## markers."""
    out: list[str] = []
    for m in _KB_MARKER_RE.finditer(text):
        if m.group(1) not in out:
            out.append(m.group(1))
    return out


def strip_kb_markers(text: str) -> str:
    """Remove ##slug-kb## markers (their content is replaced by retrieval)."""
    return _KB_MARKER_RE.sub("", text)


def merge_lane_chunks(
        lane_results: list[list[dict[str, Any]]],
        lane_owned_kbs: list[set[str]],
        top_k: int = 3,
        char_budget: int = 1600) -> list[dict[str, Any]]:
    """iter49 T4 merge: one working set out of N lanes' chunk lists.

    - sort ALL candidates by score (best-first, lane order breaks ties)
    - dedupe by chunk id (DB PK) — the SAME chunk from two lanes renders once;
      chunks without a PK fall back to the (kb, content-prefix) key
    - per-KB quota: <=1 chunk per KB per working set UNLESS the lane owns
      that KB (a refer-tag lane owns its [tag.kb]; the Closing anchor owns
      call-closing + sales-language) — no big KB (industry) monopolizes top-3
    - top_k total, char budget (stop filling once the budget can't fit more)
    """
    flat: list[tuple[float, int, dict[str, Any]]] = []
    for li, chunks in enumerate(lane_results):
        for c in chunks:
            flat.append((float(c.get("score", 0.0)), li, c))
    flat.sort(key=lambda t: (-t[0], t[1]))
    out: list[dict[str, Any]] = []
    seen: set = set()           # ids and (kb, content) fallback keys share it
    per_kb: dict[str, int] = {}
    budget = char_budget
    for _score, li, c in flat:
        if len(out) >= top_k:
            break
        cid = c.get("id")
        key: Any = cid if cid is not None else (c.get("kb"), str(c.get("content", ""))[:80])
        if key in seen:
            continue
        kb = str(c.get("kb", ""))
        owned = kb in lane_owned_kbs[li] if li < len(lane_owned_kbs) else False
        n = per_kb.get(kb, 0)
        if n >= 1 and not owned:          # quota: non-owned KB contributes <=1
            continue
        if n >= top_k:
            continue
        if budget - len(str(c.get("content", ""))) < 0 and out:
            break
        seen.add(key)
        out.append(c)
        per_kb[kb] = n + 1
        budget -= len(str(c.get("content", "")))
    return out


def render_knowledge_section(chunks: list[dict[str, Any]]) -> str:
    """The retrieved-excerpt block appended to the (now lean) system prompt."""
    if not chunks:
        return ("\n\n## KNOWLEDGE\n(No knowledge-base excerpt matched this turn; "
                "answer from the conversation.)")
    parts = ["\n\n## KNOWLEDGE (most relevant excerpts, retrieved for THIS turn)"]
    for c in chunks:
        parts.append(f"### from {c['kb']}-kb\n{c['content']}")
    return "\n\n".join(parts)


def render_pinned_section(chunks: list[dict[str, Any]], tag: str,
                          cap: int = 1800) -> str:
    """iter52 T4: the PINNED industry vertical block, prepended to the
    per-turn KNOWLEDGE in the post-history delta. Whole-chunk drops only
    (never mid-chunk truncation); measured max vertical total is 1709 chars
    so the 1800 cap never bites in practice. Empty chunks -> "" (no pin
    block; the caller keeps industry in the vector lane)."""
    if not chunks:
        return ""
    parts = [f"\n\n## INDUSTRY CONTEXT ({tag} — pinned for this call)"]
    budget = int(cap)
    for c in chunks:
        content = str(c.get("content", ""))
        if len(content) > budget:
            continue          # whole-chunk drop only
        parts.append(f"### from {c.get('kb', 'industry')}-kb\n{content}")
        budget -= len(content)
    return "\n\n".join(parts) if len(parts) > 1 else ""
