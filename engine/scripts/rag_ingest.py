#!/usr/bin/env python3
"""(Re)build the pgvector RAG index from agent/knowledge_bases/*.md.

Run ONCE at deploy time (and after any KB edit):

    python scripts/rag_ingest.py                     # uses .env DATABASE_URL
    python scripts/rag_ingest.py --db postgresql://diallux:diallux@localhost:5432/diallux

Requires: postgres with the `vector` extension available (the official
pgvector/pgvector:pg16 docker image has it), OPENAI_API_KEY for embeddings
(text-embedding-3-small, ~$0.02 for the whole corpus of ~63k chars).

After ingest, RAG_MODE=auto switches every call to retrieval (restart the
app or wait for a new call — the store is resolved per call runtime).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import get_settings          # noqa: E402
from diallux.rag import KBStore, chunk_markdown  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser(description="Build the pgvector KB index")
    ap.add_argument("--db", default="", help="postgres URL (default: DATABASE_URL from .env)")
    ap.add_argument("--kb-dir", default="", help="KB directory (default: agent/knowledge_bases)")
    ap.add_argument("--dry-run", action="store_true", help="chunk + count only, no DB writes")
    args = ap.parse_args()

    s = get_settings()
    db = args.db or s.database_url
    kb_dir = args.kb_dir or s.knowledge_base_dir
    if not db:
        print("error: no DATABASE_URL (set it in .env or pass --db)")
        return 2

    if args.dry_run:
        files = sorted(Path(kb_dir).glob("*.md"))
        total = 0
        for f in files:
            n = len(chunk_markdown(f.read_text(encoding="utf-8")))
            total += n
            print(f"  {f.name:32s} {n:3d} chunks")
        print(f"dry run: {len(files)} KBs, {total} chunks, ~${total * 0.02 / 63:.4f} embedding cost")
        return 0

    store = KBStore(
        database_url=db,
        kb_dir=kb_dir,
        embedding_model=s.rag_embedding_model,
        api_key=s.openai_api_key or None,
    )
    stats = await store.ingest(rebuild=True)
    await store.close()
    print(f"indexed: {stats['kbs']} knowledge bases -> {stats['chunks']} chunks "
          f"(model: {stats['embedding_model']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
