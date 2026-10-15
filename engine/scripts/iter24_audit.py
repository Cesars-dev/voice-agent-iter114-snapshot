#!/usr/bin/env python3
"""iter24 — READ-ONLY deviation audit of the Retell design era.

For each source (4 deployed versions + v5 prompts dir), extract per-state:
  1. KB-name mentions (count + quoted lines)
  2. dv mentions (count + quoted lines) + same-line KB/dv "joins"
  3. Refer-style patterns (refer / #[ / consult / grounded in / pull from / use the ... KB)
  4. Verbatim overlap between prompt lines and KB file lines (normalized, >=40 chars)
  5. Price lines (count only + quoted lines)
  6. Word counts per state

Stdlib only. Writes exactly one JSON to --out. Reads nothing else.
"""
import argparse
import json
import re
from pathlib import Path

KB_PATTERN = re.compile(
    r"sales-language|sales-psychology|pain-points|call-context|call-closing|"
    r"discovery-bridge|are-you-ai|voice-ai-capabilities|industry-kb|hipaa"
)
DV_PATTERN = re.compile(r"pain_points|interest_topic|weekly_leak|monthly_leak|industry")
REFER_PATTERN = re.compile(
    r"refer|#\[|consult|grounded in|pull from|use the .{0,30}KB", re.IGNORECASE
)
PRICE_PATTERN = re.compile(r"\$|price|pricing|697|ballpark|range", re.IGNORECASE)
MIN_OVERLAP_LEN = 40


def normalize(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip().lower()).rstrip(".,;:")


def load_kb_index(kb_dir: Path):
    index = {}
    for f in sorted(kb_dir.glob("*.md")):
        lines = set()
        for raw in f.read_text(encoding="utf-8", errors="replace").splitlines():
            n = normalize(raw)
            if len(n) >= MIN_OVERLAP_LEN:
                lines.add(n)
        index[f.name] = lines
    return index


def quoted_matches(pattern, text, max_quotes=6):
    hits = []
    for line in text.splitlines():
        if pattern.search(line):
            hits.append(line.strip())
    count = len(hits)
    return count, hits[:max_quotes]


def overlaps(prompt_lines, kb_index):
    out = []
    for i, raw in enumerate(prompt_lines):
        n = normalize(raw)
        if len(n) < MIN_OVERLAP_LEN:
            continue
        for kb_name, kb_lines in kb_index.items():
            if n in kb_lines:
                out.append(
                    {"prompt_line_no": i + 1, "kb_file": kb_name, "line": raw.strip()}
                )
    return out


def extract_prompt(name, text, kb_index):
    lines = text.splitlines()
    kb_count, kb_quotes = quoted_matches(KB_PATTERN, text)
    dv_count, dv_quotes = quoted_matches(DV_PATTERN, text)
    refer_count, refer_quotes = quoted_matches(REFER_PATTERN, text)
    price_count, price_quotes = quoted_matches(PRICE_PATTERN, text)

    joins = []
    for line in lines:
        if KB_PATTERN.search(line) and DV_PATTERN.search(line):
            joins.append(line.strip())

    return {
        "state": name,
        "word_count": len(text.split()),
        "kb_name_mentions": {"count": kb_count, "lines": kb_quotes},
        "dv_mentions": {"count": dv_count, "lines": dv_quotes},
        "kb_dv_same_line_joins": {"count": len(joins), "lines": joins[:6]},
        "refer_style_patterns": {"count": refer_count, "lines": refer_quotes},
        "kb_verbatim_overlap": {"count": len(overlaps(lines, kb_index)), "lines": overlaps(lines, kb_index)[:6]},
        "price_lines": {"count": price_count, "lines": price_quotes},
    }


def load_deployed(version_dir: Path):
    llm = version_dir / "llm.json"
    if not llm.exists():
        llm = version_dir / "DEPLOYED_llm_snapshot.json"
    if not llm.exists():
        return None, "UNKNOWN — source absent"
    data = json.loads(llm.read_text(encoding="utf-8"))
    states = {}
    for s in data.get("states", []):
        prompt = s.get("state_prompt") or s.get("customized_prompt") or s.get("prompt") or ""
        states[s.get("name", "?")] = prompt
    return states, str(llm)


def load_v5_prompts(prompts_dir: Path):
    states = {}
    for f in sorted(prompts_dir.glob("*.md")):
        states[f.stem] = f.read_text(encoding="utf-8")
    return states, str(prompts_dir)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", required=True, help="comma-separated dirs")
    ap.add_argument("--kbs", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    kb_index = load_kb_index(Path(args.kbs))

    inventory = {"kb_reference_files": sorted(kb_index.keys()), "sources": {}}
    for src in args.sources.split(","):
        p = Path(src)
        if (p / "llm.json").exists() or (p / "DEPLOYED_llm_snapshot.json").exists():
            states, source = load_deployed(p)
            kind = "deployed"
        elif p.is_dir() and list(p.glob("*.md")):
            states, source = load_v5_prompts(p)
            kind = "v5_prompts"
        else:
            inventory["sources"][p.name] = {"kind": "error", "source": str(p), "error": "no llm.json/snapshot/md prompts"}
            continue
        inventory["sources"][p.name] = {
            "kind": kind,
            "source": source,
            "state_count": len(states),
            "states": {name: extract_prompt(name, text, kb_index) for name, text in states.items()},
        }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(inventory, indent=2, ensure_ascii=False), encoding="utf-8")

    for name, s in inventory["sources"].items():
        if s.get("kind") == "error":
            print(f"{name}: ERROR {s['error']}")
            continue
        total_kb = sum(v["kb_name_mentions"]["count"] for v in s["states"].values())
        total_join = sum(v["kb_dv_same_line_joins"]["count"] for v in s["states"].values())
        total_overlap = sum(v["kb_verbatim_overlap"]["count"] for v in s["states"].values())
        total_refer = sum(v["refer_style_patterns"]["count"] for v in s["states"].values())
        total_price = sum(v["price_lines"]["count"] for v in s["states"].values())
        print(
            f"{name}: {s['state_count']} states | kb-mentions {total_kb} | "
            f"kb-dv joins {total_join} | refer-patterns {total_refer} | "
            f"verbatim-overlap {total_overlap} | price-lines {total_price}"
        )
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
