#!/usr/bin/env python3
"""iter47 T1 — call ledger generator.

Builds a SQLite ledger of EVERY iteration/branch/commit/run/call from:
  git branches + git log (first-parent per engine/iter* branch)
  engine/ITERATIONS.md (one-line narratives)
  research/surgeon/ folder inventory
  tests/llm2llm/json_logs/*.json (mtime = run end time; local == UTC on this box)
  Langfuse REST (per-trace TTFT / cache_read; list endpoint ignores fromTimestamp
  on this build -> paginate ?limit=100&page=N and filter client-side)

Usage:
  .venv/bin/python scripts/call_ledger.py build \
      --db /home/julio/projects/clean_diallux_SDR/research/surgeon/iter47-call-ledger/ledger.db

iter46's 9 runs are PINNED VERBATIM from plans/plan_iter47_call_ledger.md (mtime
windows + verdicts + latency trios); json files are bucketed into them by mtime.
Files outside every pinned window bucket by mtime day -> run kind=day-bucket,
attribution-confidence=low.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sqlite3
import statistics
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAIN_ENGINE = Path("/home/julio/projects/clean_diallux_SDR/engine")
PROJECT_ROOT = ROOT.parent
# main-checkout copy is authoritative (docs go straight to main; worktree may lag)
ITERATIONS_MD = MAIN_ENGINE / "ITERATIONS.md" if (MAIN_ENGINE / "ITERATIONS.md").exists() \
    else ROOT / "ITERATIONS.md"
SURGEON_DIR = MAIN_ENGINE.parent / "research" / "surgeon"
JSON_LOGS = ROOT / "tests" / "llm2llm" / "json_logs"

DB_DAY = "2026-09-11"
RUNS_PINNED = [
    # (run_id, start, end, kind, config_flags, score, verdict_line, e2e_p50, ttft_p50, turn1, cache_pct, branch)
    ("iter44-battery", "05:50", "06:15", "full-battery",
     "first-13 low tier (gpt-5.4)", "12/13 (Pedro persona mismatch)",
     "iter44 T8 battery — THE working baseline (suite 240, e2e 1,332-2,265); "
     "Pedro over-book = pre-existing persona variance. NOTE: window holds 15 jsons = "
     "13 battery calls + 2 spot-checks (Sofia d63bdbd8 FAIL @06:03 then retry "
     "1bc365de PASS @06:07; Pedro acf3cf13 @06:02) — score counts Sofia's retry",
     None, None, None, None, "engine/iter44-cache-floor"),
    ("battery-a", "13:40", "13:50", "cli-failure",
     "—", None, "CLI persona-flag failure (harness takes ONE group/substring, not comma list)",
     None, None, None, None, "engine/iter46-latency-floor"),
    ("battery-b", "13:57", "14:10", "full-battery",
     "iter46 flags + verbosity LOW", "12/13 (Pedro only)",
     "first-13 12/13, Pedro marginal as in baseline",
     2121, 849, 799, 86, "engine/iter46-latency-floor"),
    ("ladder-a", "14:12", "14:16", "ladder",
     "iter46 flags + verbosity LOW", "happy 3/4 -> SOP STOP",
     "Maria 15t OK; Danny/Susan/Marcus 28t stalls at low verbosity",
     None, None, None, None, "engine/iter46-latency-floor"),
    ("bisect-1", "14:22", "14:27", "bisect",
     "flush OFF", "happy 2/4 -> T5 NOT the cause",
     "flush fast-first-flush OFF; Maria spike 54,941 ms",
     None, None, None, None, "engine/iter46-latency-floor"),
    ("bisect-2", "14:30", "14:34", "bisect",
     "verbosity MEDIUM", "happy 4/4 -> T1 verbosity=low WAS the cause",
     "medium restores happy ladder",
     2500, None, None, None, "engine/iter46-latency-floor"),
    ("d5-ladder", "14:45", "14:49", "ladder",
     "D5 medium on late states", "happy 3/4 -> SOP STOP",
     "stalls persist in Intake/Discovery at low",
     None, None, None, None, "engine/iter46-latency-floor"),
    ("ladder-post-revert", "15:01", "15:25", "ladder",
     "shipped (medium)", "20/25, first-13 12/13",
     "post-revert full ladder = baseline level",
     1498, 892, 848, 93, "engine/iter46-latency-floor"),
    ("battery-c", "15:36", "15:42", "corrupt",
     "—", None, "CORRUPT — concurrent-write junk, EXCLUDED from scoring",
     None, None, None, None, "engine/iter46-latency-floor"),
    ("battery-d", "15:51", "16:07", "full-battery",
     "shipped", "11/13 (Pedro + Sofia 48t)",
     "Pedro marginal + Sofia 48t slot loop (the bug); latency floor held",
     2177, 934, 981, 91, "engine/iter46-latency-floor"),
]
RUNS_BRANCH = "engine/iter46-latency-floor"
WINDOW_TOL = timedelta(seconds=180)

SCHEMA = """
CREATE TABLE IF NOT EXISTS branches (
    name TEXT PRIMARY KEY, kind TEXT, parent TEXT, head_commit TEXT,
    first_iter INTEGER, last_iter INTEGER, status TEXT, summary_line TEXT
);
CREATE TABLE IF NOT EXISTS commits (
    branch TEXT, sha TEXT, date TEXT, subject TEXT,
    files_touched INTEGER, summary_line TEXT, is_origin INTEGER,
    PRIMARY KEY (branch, sha)
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, window_start TEXT, window_end TEXT, kind TEXT,
    branch TEXT, head_commit TEXT, config_flags TEXT, score TEXT,
    e2e_p50 INTEGER, ttft_p50 INTEGER, turn1_ttft_p50 INTEGER, cache_pct INTEGER,
    verdict_line TEXT, evidence_paths TEXT
);
CREATE TABLE IF NOT EXISTS calls (
    call_id TEXT PRIMARY KEY, run_id TEXT, json_path TEXT, persona TEXT, slug TEXT,
    outcome TEXT, expect TEXT, pass INTEGER, turns INTEGER, ended INTEGER,
    gate_rejections INTEGER, tools INTEGER, blocked_transitions INTEGER,
    e2e_p50 INTEGER, ttft_p50 INTEGER, cache_pct INTEGER, rounds INTEGER,
    langfuse_trace_id TEXT, notes TEXT
);
"""


def git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fmt_dt(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- branches

def classify_branch(name: str) -> str:
    if name.startswith("origin/"):
        return "remote"
    if name.startswith("engine/iter"):
        return "engine-iter"
    if name.startswith("engine/snap-"):
        return "snap"
    if name.startswith("history/"):
        return "history"
    if name in ("engine/main", "main"):
        return "main"
    return "other"


def iter_num(name: str) -> int | None:
    m = re.search(r"iter(\d+)", name)
    return int(m.group(1)) if m else None


def iterations_lines() -> list[str]:
    if not ITERATIONS_MD.exists():
        return []
    return ITERATIONS_MD.read_text().splitlines()


def summary_for_branch(lines: list[str], name: str) -> str | None:
    base = name.removeprefix("engine/")
    n = iter_num(name)
    # exact branch-name mention wins
    for ln in lines:
        if base in ln:
            return ln.strip()[:200]
    # else heading mentioning the iter number
    if n is not None:
        pat = re.compile(rf"##.*\biter{n}\b")
        for ln in lines:
            if pat.search(ln):
                return ln.strip("# ").strip()[:200]
    return None


def collect_branches() -> list[dict]:
    refs: dict[str, dict] = {}
    raw = git("branch", "-a").splitlines()
    for ln in raw:
        ln = ln.strip().lstrip("*+ ").strip()
        if ln.startswith("remotes/"):
            ln = "origin/" + ln.removeprefix("remotes/origin/")
        if not ln or ln.startswith("("):
            continue
        refs.setdefault(ln, {})
    heads = git("for-each-ref", "refs/heads",
                "--format=%(refname:short) %(objectname:short)").splitlines()
    head_map = {p.split()[0]: p.split()[1] for p in heads}
    all_heads = {}
    for p in git("for-each-ref", "refs/heads", "refs/remotes",
                 "--format=%(refname:short) %(objectname:short)").splitlines():
        name = p.split()[0]
        if name.startswith("origin/"):
            pass  # keep origin/ prefix as the dedupe identity
        all_heads.setdefault(name, p.split()[1])
    lines = iterations_lines()
    out = []
    for name in sorted(refs):
        head = head_map.get(name) or all_heads.get(name)
        kind = classify_branch(name)
        status = None
        if head and kind != "remote" and refs_exists("engine/main"):
            try:
                anc = subprocess.run(
                    ["git", "-C", str(ROOT), "merge-base", "--is-ancestor", head,
                     "engine/main"], capture_output=True)
                status = "merged" if anc.returncode == 0 else "open"
            except Exception:
                status = None
        out.append({
            "name": name, "kind": kind, "head_commit": head,
            "first_iter": iter_num(name), "last_iter": iter_num(name),
            "status": status,
            "summary_line": summary_for_branch(lines, name) or fallback_summary(kind, name),
        })
    return out


def fallback_summary(kind: str, name: str) -> str:
    if kind == "snap":
        return "photocopy era, read-only archaeology (snap chain)"
    if kind == "remote":
        return f"remote-tracking mirror (origin/{name.removeprefix('origin/')})"
    if kind == "history":
        return "history/* nested-repo ref (frozen archaeology)"
    if kind == "main":
        return "engine mainline (merged iteration state)"
    if kind == "other":
        return "work-in-progress ref (stash-era)"
    if kind == "engine-iter":
        return "no ITERATIONS.md narrative line (unmerged or pre-ledger era)"
    return None


def refs_exists(name: str) -> bool:
    r = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--verify", name],
                       capture_output=True)
    return r.returncode == 0


def slug_of_callid(call_id: str | None) -> str | None:
    if not call_id:
        return None
    cid = call_id.split("l2l-")[-1]
    return cid.rsplit("-", 1)[0] or cid


def branch_topology(branches: list[dict]) -> dict[str, dict]:
    """For each engine/iter* branch: own first-parent commits + parent branch.

    parent = the first commit of another branch's HEAD met while walking the
    branch's first-parent history (exclusive of the branch's own tip). Branches
    sharing the same head sha: the LATER-created one becomes a child of the
    earlier one (own commits = []), the earlier one walks through normally.
    """
    heads = {b["name"]: b["head_commit"] for b in branches if b["head_commit"]}
    sha_owner: dict[str, list[tuple[str, str]]] = {}
    for name, sha in heads.items():
        sha_owner.setdefault(sha, []).append((name, branch_creation_ts(name)))
    topo: dict[str, dict] = {}
    for name, head in heads.items():
        peers = [(p, ts) for p, ts in sha_owner.get(head, []) if p != name]
        earlier = [(p, ts) for p, ts in peers if ts < branch_creation_ts(name)]
        if earlier:
            topo[name] = {"parent": min(earlier, key=lambda x: x[1])[0],
                          "own": []}
            continue
        chain = git("rev-list", "--first-parent", head).splitlines()
        parent, own = None, []
        for i, sha in enumerate(chain):
            if i == 0:
                continue
            if sha in sha_owner and any(p != name for p, _ in sha_owner[sha]):
                parent = min((p for p, _ in sha_owner[sha] if p != name),
                             key=lambda p: branch_creation_ts(p))
                break
            own.append(sha)
        topo[name] = {"parent": parent, "own": own}
    return topo


def branch_creation_ts(name: str) -> str:
    return git("for-each-ref", f"refs/heads/{name}",
               "--format=%(committerdate:iso8601)") or "1970-01-01"


# ---------------------------------------------------------------- commits

def collect_commits(branches: list[dict]) -> list[dict]:
    topo = branch_topology(branches)
    order = {b["name"]: branch_creation_ts(b["name"]) for b in branches}
    rows: dict[tuple[str, str], dict] = {}
    for br in sorted(topo):
        info = topo[br]
        if not info["own"]:
            continue
        head = next(b["head_commit"] for b in branches if b["name"] == br)
        rng = f"{info['parent']}..{head}" if info["parent"] else head
        log = git("log", "--first-parent", "--format=%H%x00%ct%x00%s", rng)
        for ln in log.splitlines():
            if not ln:
                continue
            sha, ct, subject = ln.split("\x00", 2)
            key = (br, sha[:40])
            if key in rows:
                continue
            try:
                nf = len(git("show", "--name-only", "--format=", sha).splitlines())
            except Exception:
                nf = None
            rows[key] = {
                "branch": br, "sha": sha[:40], "date": datetime.fromtimestamp(
                    int(ct), tz=timezone.utc).isoformat(),
                "subject": subject, "files_touched": nf, "summary_line": subject,
            }
    # is_origin = the earliest-created branch whose range contains the sha
    first: dict[str, str] = {}
    for (br, sha), r in rows.items():
        if sha not in first or order.get(br, "9999") < order.get(first[sha], "9999"):
            first[sha] = br
    for (br, sha), r in rows.items():
        r["is_origin"] = 1 if first[sha] == br else 0
    return list(rows.values())


def commit_at(branch: str, when: datetime) -> str | None:
    """Latest commit on branch with commit-time <= when."""
    log = git("log", "--first-parent", "--format=%H %ct", branch)
    ts = int(when.timestamp())
    for ln in log.splitlines():
        sha, ct = ln.split()
        if int(ct) <= ts:
            return sha[:40]
    return None


# ---------------------------------------------------------------- runs + calls

def pinned_windows() -> list[tuple[str, datetime, datetime]]:
    out = []
    for rid, s, e, *_ in RUNS_PINNED:
        out.append((rid, parse_utc(f"{DB_DAY}T{s}:00Z"), parse_utc(f"{DB_DAY}T{e}:00Z")))
    return out


def bucket_mtime(mtime: datetime, windows) -> tuple[str, str]:
    """(run_id, confidence) for one json file."""
    for rid, ws, we in windows:
        if ws - WINDOW_TOL <= mtime <= we + WINDOW_TOL:
            return rid, "high"
    return f"day-{mtime.strftime('%Y%m%d')}", "low"


def load_calls(windows) -> list[dict]:
    rows = []
    for p in sorted(JSON_LOGS.glob("*.json")):
        mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
        try:
            d = json.loads(p.read_text())
        except Exception:
            run_id, conf = bucket_mtime(mtime, windows)
            rows.append({"call_id": f"corrupt-{p.stem}", "json_path": str(p),
                         "mtime": mtime, "run_id": run_id, "confidence": conf,
                         "notes": "unparseable json"})
            continue
        run_id, conf = bucket_mtime(mtime, windows)
        slug = slug_of_callid(d.get("call_id")) or slug_of_callid(
            p.stem.split("_l2l_")[-1])
        rows.append({
            "call_id": d.get("call_id") or p.stem,
            "json_path": str(p), "mtime": mtime,
            "persona": d.get("persona"), "slug": slug,
            "outcome": d.get("outcome"), "expect": d.get("expect"),
            "pass": 1 if d.get("pass") else 0,
            "turns": d.get("turns"), "ended": 1 if d.get("ended") else 0,
            "gate_rejections": d.get("gate_rejections"),
            "tools": len(d.get("tools") or []) if isinstance(d.get("tools"), list) else None,
            "blocked_transitions": d.get("blocked_transitions"),
            "e2e_p50": int(d["p50_ms"]) if d.get("p50_ms") else None,
            "wall_s": d.get("wall_s"), "run_id": run_id, "confidence": conf,
        })
    return rows


# ---------------------------------------------------------------- langfuse

def lf_env() -> tuple[str, str, str]:
    env = {}
    envfile = ROOT / ".env"
    if envfile.exists():
        for line in envfile.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env.setdefault(k.strip(), v.strip())
    return (env.get("LANGFUSE_HOST", "http://localhost:3001").rstrip("/"),
            env.get("LANGFUSE_PUBLIC_KEY", ""), env.get("LANGFUSE_SECRET_KEY", ""))


def lf_get(host: str, auth: str, path: str) -> dict:
    req = urllib.request.Request(
        f"{host}{path}", headers={"Authorization": "Basic " + auth})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def lf_traces() -> list[dict]:
    host, pk, sk = lf_env()
    if not pk:
        return []
    auth = base64.b64encode(f"{pk}:{sk}".encode()).decode()
    out, page = [], 1
    while page <= 30:
        try:
            d = lf_get(host, auth, f"/api/public/traces?limit=100&page={page}")
        except Exception:
            break
        data = d.get("data", [])
        out.extend(data)
        if len(data) < 100:
            break
        page += 1
    return [t for t in out if (t.get("name") or "").startswith("llm2llm-graph-")]


def trace_slug(t: dict) -> str:
    return (t.get("name") or "").split("llm2llm-graph-")[-1].split("@")[0]


def match_trace(traces: list[dict], call: dict) -> dict | None:
    slug = call.get("slug")
    if not slug:
        return None
    wall = timedelta(seconds=float(call.get("wall_s") or 60))
    target = call["mtime"] - wall  # approx trace start
    cands = [t for t in traces if trace_slug(t) == slug
             and abs(parse_utc(t["timestamp"]) - target) < timedelta(minutes=10)]
    if not cands:
        return None
    return min(cands, key=lambda t: abs(parse_utc(t["timestamp"]) - target))


def lf_trace_stats(trace: dict) -> dict | None:
    host, pk, sk = lf_env()
    if not pk:
        return None
    auth = base64.b64encode(f"{pk}:{sk}".encode()).decode()
    try:
        d = lf_get(host, auth, f"/api/public/traces/{trace['id']}")
    except Exception:
        return None
    gens = sorted(
        (o for o in d.get("observations", []) if o.get("type") == "GENERATION"),
        key=lambda o: o.get("startTime") or "")
    if not gens:
        return None
    ttfts, cache_hits = [], 0
    for g in gens:
        ud = g.get("usageDetails") or {}
        ttft = ud.get("ttft_ms")
        if ttft:
            ttfts.append(float(ttft))
        if (ud.get("cache_read") or 0) > 0:
            cache_hits += 1
    return {
        "ttft_p50": int(statistics.median(ttfts)) if ttfts else None,
        "cache_pct": int(100 * cache_hits / len(gens)),
        "rounds": len(gens),
    }


# ---------------------------------------------------------------- build

def build(db_path: Path, use_langfuse: bool = True) -> dict:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)

    branches = collect_branches()
    topo = branch_topology(branches)
    conn.executemany(
        "INSERT INTO branches(name,kind,parent,head_commit,first_iter,last_iter,"
        "status,summary_line) VALUES (:name,:kind,:parent,:head_commit,:first_iter,"
        ":last_iter,:status,:summary_line)",
        [{**b, "parent": topo.get(b["name"], {}).get("parent")} for b in branches])

    commits = collect_commits(branches)
    conn.executemany(
        "INSERT INTO commits(branch,sha,date,subject,files_touched,summary_line,"
        "is_origin) VALUES (:branch,:sha,:date,:subject,:files_touched,"
        ":summary_line,:is_origin)", commits)

    windows = pinned_windows()
    conn.executemany(
        "INSERT INTO runs(run_id,window_start,window_end,kind,branch,head_commit,"
        "config_flags,score,e2e_p50,ttft_p50,turn1_ttft_p50,cache_pct,verdict_line,"
        "evidence_paths) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(rid, parse_utc(f"{DB_DAY}T{s}:00Z").isoformat(),
          parse_utc(f"{DB_DAY}T{e}:00Z").isoformat(), kind, br,
          commit_at(br, parse_utc(f"{DB_DAY}T{e}:00Z")) if refs_exists(br) else None,
          cfg, score, e2e, ttft, t1, cache, verdict,
          f"research/surgeon/{'iter44-cache-floor' if br.endswith('iter44-cache-floor') else 'iter46-latency-floor'}/01_report.md")
         for rid, s, e, kind, cfg, score, verdict, e2e, ttft, t1, cache, br
         in RUNS_PINNED])

    calls = load_calls(windows)
    # day-bucket runs
    for rid in sorted({c["run_id"] for c in calls if c["run_id"].startswith("day-")}):
        day_calls = [c for c in calls if c["run_id"] == rid]
        mtimes = [c["mtime"] for c in day_calls]
        conn.execute(
            "INSERT INTO runs(run_id,window_start,window_end,kind,config_flags,"
            "verdict_line,evidence_paths) VALUES (?,?,?,?,?,?,?)",
            (rid, min(mtimes).isoformat(), max(mtimes).isoformat(), "day-bucket",
             None,
             "older-era files bucketed by mtime day; attribution-confidence=LOW "
             "(many logs overwritten across runs)",
             "tests/llm2llm/json_logs/"))

    traces = lf_traces() if use_langfuse else []
    for c in calls:
        tr = match_trace(traces, c) if traces else None
        stats, tid, note = None, None, f"attribution-confidence={c.get('confidence','low')}"
        if tr:
            tid = tr["id"]
            stats = lf_trace_stats(tr)
        elif use_langfuse:
            note += "; no matching langfuse trace"
        conn.execute(
            "INSERT INTO calls(call_id,run_id,json_path,persona,slug,outcome,expect,"
            "pass,turns,ended,gate_rejections,tools,blocked_transitions,e2e_p50,"
            "ttft_p50,cache_pct,rounds,langfuse_trace_id,notes) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (c["call_id"], c.get("run_id"), c.get("json_path"), c.get("persona"),
             c.get("slug"), c.get("outcome"), c.get("expect"), c.get("pass"),
             c.get("turns"), c.get("ended"), c.get("gate_rejections"),
             c.get("tools"), c.get("blocked_transitions"), c.get("e2e_p50"),
             (stats or {}).get("ttft_p50"), (stats or {}).get("cache_pct"),
             (stats or {}).get("rounds"), tid, note))
    conn.commit()
    summary = {
        "branches": conn.execute("SELECT count(*) FROM branches").fetchone()[0],
        "commits": conn.execute("SELECT count(*) FROM commits").fetchone()[0],
        "runs": conn.execute("SELECT count(*) FROM runs").fetchone()[0],
        "calls": conn.execute("SELECT count(*) FROM calls").fetchone()[0],
    }
    conn.close()
    summary["db"] = str(db_path)
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--db", type=Path,
                   default=SURGEON_DIR / "iter47-call-ledger" / "ledger.db")
    b.add_argument("--no-langfuse", action="store_true",
                   help="skip Langfuse enrichment (hermetic)")
    a = ap.parse_args()
    print(json.dumps(build(a.db, use_langfuse=not a.no_langfuse), indent=1))


if __name__ == "__main__":
    main()
