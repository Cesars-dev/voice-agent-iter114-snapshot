r"""{{dynamic_variable}} + ##knowledge-base## substitution — verbatim port of
`lab/engine.py: SDREngine.subst()` with one BUG FIX.

Rules kept exactly:
  - ##slug-kb##  -> the entire markdown file from agent/knowledge_bases/
  - {{var}}      -> str(dvs.get(var)) or the literal token when unset

BUG FIX (inherited from engine.py, fixed here): the marker regex
`##([\w-]+)-kb##` on `##industry-kb##` captures slug "industry", but the
shipped files are named `industry-kb.md`. engine.py looked up
`industry.md` -> `[KB industry MISSING]` for EVERY marker — the lab replica
ran with zero KB content. We try both `<slug>.md` and `<slug>-kb.md`.

Context for the bigger picture: the DEPLOYED Retell agent attaches its 9
KBs with kb_config {filter_score: 0.6, top_k: 3} — server-side RAG. Retell
retrieved ~3 chunks per turn; it did NOT inline 63k chars. Our v2 RAG layer
restores exactly that behavior (see rag.py); this inline expansion is the
offline/fallback path and the lab-engine semantics.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path

_KB_RE = re.compile(r"##([\w-]+)-kb##")
_DV_RE = re.compile(r"\{\{(\w+)\}\}")


class Substitutor:
    def __init__(self, kb_dir: str | Path):
        self.kb_dir = Path(kb_dir)
        self._cache: dict[str, str] = {}
        self._lock = threading.Lock()

    def _kb(self, slug: str) -> str:
        with self._lock:
            if slug not in self._cache:
                candidates = (self.kb_dir / f"{slug}.md", self.kb_dir / f"{slug}-kb.md")
                for p in candidates:
                    if p.exists():
                        self._cache[slug] = p.read_text(encoding="utf-8")
                        break
                else:
                    self._cache[slug] = f"[KB {slug} MISSING]"
            return self._cache[slug]

    def subst(self, text: str, dvs: dict, kb: bool = True) -> str:
        """Substitute ##kb## markers (when kb=True) then {{dv}} tokens.

        kb=False leaves ##kb## markers in place — the RAG path uses it so
        strip_kb_markers() can remove them and retrieval replaces them with
        top-k excerpts instead of full-file inline expansion."""
        if kb:
            text = _KB_RE.sub(lambda m: self._kb(m.group(1)), text)
        return _DV_RE.sub(lambda m: str(dvs.get(m.group(1), m.group(0))), text)
