"""Langfuse observability — port of the team's `lab/langfuse_bridge.py` (v3/v4-compatible).

One trace per call (session id `diallux-call-<hex>`), containing:
  - a generation per LLM round      (state, model, messages preview, output,
                                     tool_calls, token usage, exact latency)
  - a tool span per tool call       (args, full JSON response, latency, state)
  - media spans per turn (added by the session): stt_eot, llm_first_token,
    tts_first_byte, e2e_turn — the latency budget, measured
  - on finish(): the full transcript + final dvs + final state on the root trace

Fail-safe: if Langfuse is unreachable, tracing disables itself and the call
continues (same contract as the original bridge).
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.request
import uuid


class Tracer:
    def __init__(self, session_name: str = "diallux-call", metadata: dict | None = None, enabled: bool = True):
        self.enabled = enabled
        self.root = None
        self.lf = None
        self.trace_id = None                     # set at init; used by score()
        self.session_id = f"{session_name}-{uuid.uuid4().hex[:8]}"
        self._meta = metadata or {}
        self._started = 0      # local census of observations created (root + children)
        self.tail_ok: bool | None = None  # set by wait_for_tail: True=complete, False=short, None=skipped
        self.tail_seen: int = -1  # last server-side count observed by wait_for_tail
        if not enabled:
            return
        try:
            from langfuse import Langfuse, propagate_attributes

            self.lf = Langfuse(
                public_key=os.environ.get("LANGFUSE_PUBLIC_KEY", ""),
                secret_key=os.environ.get("LANGFUSE_SECRET_KEY", ""),
                host=os.environ.get("LANGFUSE_HOST", "http://localhost:3000"),
            )
            with propagate_attributes(session_id=self.session_id, trace_name=session_name):
                self.root = self.lf.start_observation(
                    name=session_name,
                    as_type="agent",
                    metadata={"session_id": self.session_id, **self._meta},
                )
                self.trace_id = getattr(self.root, "trace_id", None)
                self._started = 1  # the root agent observation itself
        except Exception as exc:
            print(f"[langfuse] disabled ({type(exc).__name__}: {exc})")
            self.enabled = False

    def score(self, name: str, value: float, comment: str | None = None):
        """Attach a NUMERIC score to this call's trace (e.g. harness_result). No-op when disabled."""
        if not (self.lf and self.trace_id):
            return
        try:
            self.lf.create_score(trace_id=self.trace_id, name=name, value=value,
                                 data_type="NUMERIC", comment=comment)
        except Exception as exc:
            print(f"[langfuse] score skipped: {type(exc).__name__}: {exc}")

    # ---------------- LLM generation ---------------- #
    def start_llm(self, state: str, model: str, messages: list):
        if not self.root:
            return None
        try:
            obs = self.root.start_observation(
                name=f"llm:{state}",
                as_type="generation",
                model=model,
                input=messages,
                metadata={"state": state},
            )
            self._started += 1
            return obs
        except Exception as exc:
            print(f"[langfuse] gen open skipped: {type(exc).__name__}: {exc}")
            return None

    def finish_llm(self, obs, *, output: str, latency_s: float, usage: dict | None = None,
                   tool_calls=None, ttft_s: float | None = None,
                   extra_meta: dict | None = None):
        if not obs:
            return
        try:
            usage_details = None
            if usage:
                # iter30: prompt-cache visibility — OpenAI reports cached input
                # tokens under input_token_details.cache_read (langchain)
                # iter108 fix (2026-10-12): priority-served responses
                # (service_tier=fast) can report the counter as
                # `priority_cache_read` instead (schema varies per response) —
                # the old cache_read-only read logged 0 on every fast round.
                # cache_read = effective (either field); raw priority kept too.
                itd = usage.get("input_token_details") or usage.get("input_token_details_extra") or {}
                _cache_read = itd.get("cache_read", 0) or itd.get("priority_cache_read", 0) or 0
                usage_details = {
                    "input": usage.get("input_tokens", usage.get("input", 0)),
                    "output": usage.get("output_tokens", usage.get("output", 0)),
                    "total": usage.get("total_tokens", usage.get("total", 0)),
                    "cache_read": _cache_read,
                }
                if itd.get("priority_cache_read"):
                    usage_details["priority_cache_read"] = itd.get("priority_cache_read")
                # iter44: TTFT on the SAME generation row as cache_read —
                # the whole point is correlating cache hits with first-token
                # latency (head+RAG-tail+history must enter the cached prefix)
                if ttft_s is not None:
                    usage_details["ttft_ms"] = round(ttft_s * 1000)
            obs.update(
                output={"text": output, "tool_calls": tool_calls or []},
                metadata={"latency_s": round(latency_s, 3),
                          **({"ttft_s": round(ttft_s, 3)} if ttft_s is not None else {}),
                          # iter56 (C2.7): payload-layout attribution per round
                          **(extra_meta or {})},
                usage_details=usage_details,
            )
            obs.end()
        except Exception as exc:
            print(f"[langfuse] gen close skipped: {type(exc).__name__}: {exc}")

    # ---------------- tool span ---------------- #
    def start_tool(self, name: str, input_data, state: str):
        if not self.root:
            return None
        try:
            obs = self.root.start_observation(
                name=f"tool:{name}",
                as_type="tool",
                input=input_data,
                metadata={"state": state},
            )
            self._started += 1
            return obs
        except Exception as exc:
            print(f"[langfuse] tool open skipped: {type(exc).__name__}: {exc}")
            return None

    def finish_tool(self, obs, output_data, latency_s: float):
        if not obs:
            return
        try:
            obs.update(output=output_data, metadata={"latency_s": round(latency_s, 3)})
            obs.end()
        except Exception as exc:
            print(f"[langfuse] tool close skipped: {type(exc).__name__}: {exc}")

    # ---------------- media spans (new: the latency budget) ---------------- #
    def span(self, name: str, *, input=None, output=None, metadata: dict | None = None):
        """Fire-and-forget span under the call trace (stt_eot, tts_first_byte, e2e_turn...)."""
        if not self.root:
            return
        try:
            obs = self.root.start_observation(name=name, as_type="span", input=input)
            obs.update(output=output, metadata=metadata or {})
            obs.end()
            self._started += 1
        except Exception as exc:
            print(f"[langfuse] span skipped ({name}): {type(exc).__name__}: {exc}")

    def flush(self):
        if not self.lf:
            return
        try:
            self.lf.flush()
        except Exception:
            pass

    # ---------------- tail confirm (iter33 OTEL fix) ---------------- #
    def _tail_observation_count(self) -> int:
        """Server-side observation count for this trace (-1 = unknown/skip).

        Read-only GET against the Langfuse public API with the same creds the
        SDK exports with. Raises on transport/API errors (caller decides).
        """
        host = os.environ.get("LANGFUSE_HOST", "http://localhost:3000").rstrip("/")
        pk = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
        sk = os.environ.get("LANGFUSE_SECRET_KEY", "")
        if not self.trace_id or not pk or not sk:
            return -1
        token = base64.b64encode(f"{pk}:{sk}".encode()).decode()
        url = f"{host}/api/public/observations?traceId={self.trace_id}&limit=1"
        req = urllib.request.Request(url, headers={"Authorization": f"Basic {token}"})
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 (pinned localhost/self-host)
            body = json.loads(resp.read().decode())
        meta = body.get("meta") or {}
        total = meta.get("totalItems", len(body.get("data", [])))
        return int(total)

    def wait_for_tail(self, timeout_s: float = 60.0, poll_s: float = 5.0) -> int:
        """Blocking server-side confirm that the trace tail landed.

        Compares the server-side observation count against the LOCAL census
        (every observation this tracer started, root included): quiescence
        alone cannot distinguish "complete" from "truncated", the census can.
        Exits complete on 2 consecutive equal polls at/above census; gives up
        (LOUD warning, tail_ok=False) after 4 equal polls or the timeout cap.
        Fail-open: returns -1 / tail_ok=None when disabled/unknown/unreachable
        (never raises, never blocks past the cap). Returns the last seen count.
        """
        self.tail_ok = None
        if not (self.enabled and self.lf and self.trace_id):
            return -1
        expected = getattr(self, "_started", 0) or 0
        deadline = time.monotonic() + max(0.0, timeout_s)
        last = -2
        stable_rounds = 0
        seen = -1
        try:
            while True:
                try:
                    seen = self._tail_observation_count()
                except Exception as exc:
                    print(f"[langfuse] tail-confirm skipped ({type(exc).__name__}: {exc})")
                    return seen if isinstance(seen, int) else -1
                if seen < 0:
                    return -1
                if seen == last:
                    stable_rounds += 1
                else:
                    stable_rounds = 0
                    last = seen
                if seen >= expected and stable_rounds >= 1:
                    self.tail_ok = True
                    return seen
                if stable_rounds >= 3 or time.monotonic() + poll_s > deadline:
                    self.tail_ok = seen >= expected
                    if not self.tail_ok:
                        print(f"[langfuse] TAIL SHORTFALL trace_id={self.trace_id} "
                              f"server={seen} local={expected} "
                              f"(~{expected - seen} observations never landed)")
                    return seen
                time.sleep(poll_s)
        finally:
            self.tail_seen = seen

    def finish(self, output=None, wait: bool = True, timeout_s: float = 60.0):
        """Close the root observation with the FULL transcript (bridge contract).

        wait=True (default) blocks on wait_for_tail() so the OTEL batch
        exporter's in-flight tail lands server-side before the process exits.
        Pass wait=False only for hermetic/offline paths that never exported.
        """
        if not self.root:
            return
        try:
            if output is not None:
                self.root.update(
                    input={"session_id": self.session_id, **(self._meta or {})},
                    output=output,
                )
            self.root.end()
            self.flush()
        except Exception as exc:
            print(f"[langfuse] finish skipped: {type(exc).__name__}: {exc}")
            return
        if wait:
            self.wait_for_tail(timeout_s=timeout_s)
