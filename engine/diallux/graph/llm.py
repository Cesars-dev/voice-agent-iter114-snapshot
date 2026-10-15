"""LLM access layer: streaming ChatOpenAI + Retell->OpenAI tool schema conversion.

Verbatim semantics from lab/engine.py:
  - `_openai_schema()` converts Retell `type:"enum" + choices` to OpenAI
    `type:"string" + enum` (recursively).
  - Toolset for a round = state tools + general_tools + `transition_to_<State>`
    functions built from the state's edges.
  - Model config: gpt-5.4 with reasoning off (MODEL LAW 2026-09-22: gpt-5.2
    deprecated). V2: temperature defaults to
    0.3 (your ask — mild variance for liveliness) with a GUARD:
    gpt-5.x reasoning models only accept the default temperature (passing
    anything else 400s on chat-completions), so for gpt-5* we omit it. Set
    OPENAI_MODEL=gpt-4.1 / gpt-4o to make 0.3 actually bite.

Streaming: content deltas are yielded as they arrive (for TTS pipelining);
tool_call fragments are accumulated into complete calls before the executor
runs. `stream_usage=True` gives token counts on the final chunk.
"""
from __future__ import annotations

import json
import re
from typing import Any, AsyncIterator

from langchain_openai import ChatOpenAI

from ..config import Settings

# reasoning-family models: temperature is fixed by the API; sending 0.3 400s
_REASONING_MODEL_RE = re.compile(r"^(gpt-5|o[134](-|$))", re.IGNORECASE)

# iter65 T1: ONE shared httpx pool for every ChatOpenAI the process builds —
# sessions/agents construct a fresh StreamingLLM per call; without sharing,
# each call pays its own TCP/TLS cold start (turn-1 TTFT 1,026-2,956 ms with
# cache_read 1,664 = the smoking gun). Client is never closed (process exit
# owns it); hermetic tests never send real traffic through it.
_SHARED_HTTP: "httpx.AsyncClient | None" = None


def shared_llm_http_client():
    global _SHARED_HTTP
    if _SHARED_HTTP is None or _SHARED_HTTP.is_closed:
        import httpx
        _SHARED_HTTP = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=600.0, write=600.0, pool=600.0),
            limits=httpx.Limits(keepalive_expiry=600.0))
    return _SHARED_HTTP


async def llm_boot_prewarm(settings) -> float:
    """iter65 T1: one throwaway completion through the SHARED client at process
    start — TCP/TLS + server-side model spin-up happen once, output discarded.
    Bounded (15 s), best-effort, never raises. Returns ms."""
    import asyncio
    import time
    kwargs: dict = {}
    if settings.openai_base_url:
        kwargs["base_url"] = settings.openai_base_url
    if _REASONING_MODEL_RE.match(settings.openai_model):
        mk: dict = {}
        if settings.openai_reasoning_effort:
            mk["reasoning_effort"] = settings.openai_reasoning_effort
        if settings.openai_verbosity:
            mk["verbosity"] = settings.openai_verbosity
        if mk:
            kwargs["model_kwargs"] = mk
    llm = ChatOpenAI(model=settings.openai_model, api_key=settings.openai_api_key,
                     max_completion_tokens=16, stream_usage=False,
                     http_async_client=shared_llm_http_client(), **kwargs)
    t0 = time.perf_counter()
    try:
        await asyncio.wait_for(
            llm.ainvoke([{"role": "user", "content": "HI"}]), 15.0)
    except Exception:
        pass
    return round((time.perf_counter() - t0) * 1000, 1)

# iter48b (FIND-5, cURL+OTEL-proven): gpt-5.4 prompt-cache engages ONLY on
# requests that carry tools — a no-tools request NEVER reports cache_read>0
# (9/9 raw cURL attempts, back-to-back up to +15s). The first-turn lite
# payload therefore carries this ONE do-nothing tool so the shape is
# cache-eligible and the lite warm seeds the same bytes. The model must not
# call it — and the executor no-ops it even if it does.
LITE_NOOP_TOOL = {
    "type": "function",
    "function": {
        "name": "memory_note",
        "description": "Internal pacing tool. NEVER call this — always reply "
                       "in text only.",
        "parameters": {"type": "object", "properties": {},
                       "required": [], "additionalProperties": False},
    },
}


# --------------------------------------------------------------------------- #
# Retell -> OpenAI schema conversion (engine.py verbatim)
# --------------------------------------------------------------------------- #
def retell_to_openai_schema(params: Any) -> Any:
    """Recursively convert Retell schemas (type 'enum' + 'choices')."""
    if isinstance(params, list):
        return [retell_to_openai_schema(p) for p in params]
    if not isinstance(params, dict):
        return params
    out: dict = {}
    if params.get("type") == "enum" and params.get("choices"):
        out["type"] = "string"
        out["enum"] = params["choices"]
    else:
        for k, v in params.items():
            if k == "choices":
                continue
            out[k] = retell_to_openai_schema(v)
    return out


def _strictify(params: dict) -> dict:
    """Make a JSON schema OpenAI Structured Outputs compliant (strict:true).

    Rules enforced by the API: every property listed in `required`,
    additionalProperties:false at every object level, optional fields become
    nullable unions, nested objects/arrays recurse. This is DECODE-TIME
    enforcement — the model cannot emit malformed tool args (Julio's
    'non-roulette' requirement; no instructor needed)."""
    if not isinstance(params, dict):
        return params
    ptype = params.get("type")
    if ptype == "object" or "properties" in params:
        props = params.get("properties") or {}
        required = params.get("required") or []
        strict_props = {}
        for name, spec in props.items():
            spec = _strictify(spec) if isinstance(spec, dict) else spec
            if name not in required and isinstance(spec, dict) and "type" in spec:
                t = spec["type"]
                spec = {**spec, "type": [t, "null"] if isinstance(t, str) else t}
            strict_props[name] = spec
        return {"type": "object", "properties": strict_props,
                "required": list(props.keys()), "additionalProperties": False}
    if ptype == "array" and isinstance(params.get("items"), dict):
        return {**params, "items": _strictify(params["items"])}
    return params


# iter114 T2: compute-only tools live in the ENGINE, never in the model's
# toolset — "the brain shouldn't be asked to do arithmetic" (the E5 class:
# the tools stream simply never called calculate_monthly_leak; derivation-
# on-write replaced model discretion). The definition stays in llm.json
# (JS parity source-of-truth for the port); it is filtered from every schema.
ENGINE_ONLY_TOOLS = {"calculate_monthly_leak"}


def build_tool_schemas(
    state_cfg: dict,
    general_tools: list[dict],
    subst_fn,
    dvs: dict,
) -> list[dict]:
    """OpenAI tool schemas for one LLM round in the current state (engine.py verbatim)."""
    out: list[dict] = []
    for t in list(state_cfg.get("tools", [])) + list(general_tools):
        if t.get("name") in ENGINE_ONLY_TOOLS:
            continue                    # iter114 T2: engine-derived, not model-invoked
        ttype = t.get("type")
        if ttype == "extract_dynamic_variable":
            props: dict = {}
            req: list = []
            for v in t.get("variables", []):
                prop: dict = {"type": v.get("type", "string"), "description": subst_fn(v.get("description", ""))}
                if v.get("choices"):
                    prop["type"] = "string"
                    prop["enum"] = v["choices"]
                props[v["name"]] = prop
                req.append(v["name"])
            out.append({"type": "function", "function": {
                "name": t["name"],
                "description": subst_fn(t.get("description", "")),
                "parameters": _strictify({"type": "object", "properties": props, "required": req}),
                "strict": True,
            }})
        elif ttype in ("custom", "book_appointment_cal", "code"):
            params = t.get("parameters") or {"type": "object", "properties": {}}
            if "properties" in params:
                params = retell_to_openai_schema(json.loads(subst_fn(json.dumps(params))))
            out.append({"type": "function", "function": {
                "name": t["name"],
                "description": subst_fn(t.get("description", "")),
                "parameters": _strictify(params),
                "strict": True,
            }})
        elif ttype == "end_call":
            # Retell exposes the built-in end_call general tool to the model.
            # (lab/engine.py skipped it in _tools_for_state — a lab gap, not
            # deployed-agent behavior; the Closing prompt explicitly uses it.)
            out.append({"type": "function", "function": {
                "name": t["name"],
                "description": subst_fn(t.get("description", "Ends the call gracefully.")),
                "parameters": _strictify({"type": "object", "properties": {}}),
                "strict": True,
            }})
    # edges -> transition_to_<State> functions
    for e in state_cfg.get("edges", []):
        out.append({"type": "function", "function": {
            "name": f"transition_to_{e['destination_state_name']}",
            "description": subst_fn(e.get("description", "")),
            "parameters": _strictify(e.get("parameters", {"type": "object", "properties": {}})),
            "strict": True,
        }})
    return out


# --------------------------------------------------------------------------- #
# Streaming LLM
# --------------------------------------------------------------------------- #
class LLMEvent(dict):
    """{'type': 'token'|'final', ...}"""

    @property
    def type(self) -> str:
        return self["type"]


class StreamingLLM:
    """Thin async wrapper around ChatOpenAI with tool-call accumulation."""

    def __init__(self, settings: Settings):
        self.settings = settings
        kwargs: dict[str, Any] = {}
        if settings.openai_temperature is not None and not _REASONING_MODEL_RE.match(settings.openai_model):
            kwargs["temperature"] = settings.openai_temperature
        elif settings.openai_temperature is not None:
            # gpt-5*: the API rejects non-default temperature; silently use
            # the platform default (logged once so it's never a surprise)
            import logging
            logging.getLogger("diallux.llm").info(
                "temperature=%.2f ignored for reasoning model %s "
                "(API-fixed; use gpt-4.1/gpt-4o to apply it)",
                settings.openai_temperature, settings.openai_model)
        if settings.openai_base_url:
            kwargs["base_url"] = settings.openai_base_url
        # iter98 B1: shared model_kwargs hoisted for ALL models. Reasoning
        # knobs stay conditional; service_tier lands here only when the
        # Settings knob is non-empty ("" = default tier). Attachment stays
        # "only when non-empty" so flag-off serialization is byte-identical
        # to the pre-iter98 build (non-reasoning models never carried
        # model_kwargs — that property is pinned).
        mk: dict[str, Any] = {}
        if _REASONING_MODEL_RE.match(settings.openai_model):
            if settings.openai_reasoning_effort:
                mk["reasoning_effort"] = settings.openai_reasoning_effort
            if settings.openai_verbosity:
                mk["verbosity"] = settings.openai_verbosity
        if settings.openai_service_tier:
            mk["service_tier"] = settings.openai_service_tier
        if mk:
            kwargs["model_kwargs"] = mk
        # iter65 T1: when the boot-prewarm knob is on, all ChatOpenAI twins
        # share ONE httpx pool (the warmed one). Flag off = per-instance
        # clients = iter64 exact.
        if getattr(settings, "llm_boot_prewarm", True):
            kwargs["http_async_client"] = shared_llm_http_client()
        self._llm = ChatOpenAI(
            model=settings.openai_model,
            api_key=settings.openai_api_key,
            stream_usage=True,
            **kwargs,
        )
        # iter46 D5: per-state verbosity twins — lazily-built ChatOpenAI per
        # verbosity level, chosen per ROUND by the state node (late-flow
        # ask/capture states run medium; the rest ride the "low" default).
        self._verbosity_llms: dict[str, ChatOpenAI] = {"low": self._llm}
        # iter43: prewarm twin — identical prefix bytes, minimal completion
        # (max_completion_tokens cap), output discarded. Same kwargs incl.
        # model_kwargs so the cached prefix matches the hot path exactly.
        self._warm_llm = ChatOpenAI(
            model=settings.openai_model,
            api_key=settings.openai_api_key,
            max_completion_tokens=settings.prewarm_max_completion_tokens,
            stream_usage=False,
            **kwargs,
        )
        # iter112 E2 (MODEL LAW exception, owner 2026-10-13): lazy speak
        # twins — built ONLY for models the SPEAK stream actually routes to;
        # NO verbosity kwargs at all (gpt-4.1 rejects gpt-5.x `verbosity`).
        # iter114c: ONE TWIN PER MODEL (`speak_model_states` routing — luna
        # for the fast states, openai_model for the tool-heavy states); the
        # cache is keyed by model so both mouths stay further-warmable.
        self._speak_llms: dict[str, ChatOpenAI] = {}

    def speak_llm(self, model: str | None = None) -> ChatOpenAI:
        """iter112: the dedicated SPEAK-stream twin (default
        settings.speak_model; iter114c: any model the per-state routing
        resolves to, e.g. openai_model for tool-heavy states).
        Bare construction — model + api_key + stream_usage (+ base_url /
        shared httpx pool when configured). No verbosity/service_tier kwargs
        (gpt-4.1 does not accept `verbosity`; owner order keeps the tier off
        these twins). Reasoning-model speak twins (gpt-5.x, e.g. luna) DO get
        `reasoning_effort` = settings.openai_reasoning_effort (default
        "none") — without it gpt-5.6-luna reasons by default and the speech
        stream starves on hidden reasoning (probe 2026-10-13: 60 completion
        tokens of reasoning, zero visible text)."""
        m = model or self.settings.speak_model
        inst = self._speak_llms.get(m)
        if inst is None:
            kwargs: dict[str, Any] = dict(
                model=m,
                api_key=self.settings.openai_api_key,
                stream_usage=True,
            )
            if self.settings.openai_base_url:
                kwargs["base_url"] = self.settings.openai_base_url
            if getattr(self.settings, "llm_boot_prewarm", True):
                kwargs["http_async_client"] = shared_llm_http_client()
            if _REASONING_MODEL_RE.match(m):
                kwargs["model_kwargs"] = {
                    "reasoning_effort": self.settings.openai_reasoning_effort or "none"}
            inst = ChatOpenAI(**kwargs)
            self._speak_llms[m] = inst
        return inst

    def llm_for_verbosity(self, verbosity: str) -> ChatOpenAI:
        """iter46 D5: the hot-path LLM for a verbosity level (lazy twin)."""
        if verbosity == "low" or verbosity == self.settings.openai_verbosity:
            return self._llm
        llm = self._verbosity_llms.get(verbosity)
        if llm is None:
            kwargs = dict(model=self.settings.openai_model,
                          api_key=self.settings.openai_api_key,
                          stream_usage=True)
            if self.settings.openai_base_url:
                kwargs["base_url"] = self.settings.openai_base_url
            if getattr(self.settings, "llm_boot_prewarm", True):
                kwargs["http_async_client"] = shared_llm_http_client()
            if _REASONING_MODEL_RE.match(self.settings.openai_model):
                twin_mk: dict[str, Any] = {
                    **(self.settings.openai_reasoning_effort and
                       {"reasoning_effort": self.settings.openai_reasoning_effort} or {}),
                    "verbosity": verbosity,
                }
                if self.settings.openai_service_tier:
                    twin_mk["service_tier"] = self.settings.openai_service_tier
                kwargs["model_kwargs"] = twin_mk
            llm = ChatOpenAI(**kwargs)
            self._verbosity_llms[verbosity] = llm
        return llm

    async def warm(self, messages: list[dict], tools: list[dict]) -> None:
        """iter43: cache-warm request — identical prefix bytes, minimal output,
        discarded. iter55 T0 (F-08): NO internal try/except — failures reach
        the caller's own except path (builder `_warm` diag record + lite
        retry + honest warm-span outcome). All callers are inside their own
        try/except in `_warm`."""
        bound = self._warm_llm.bind_tools(tools) if tools else self._warm_llm
        await bound.ainvoke(messages)

    async def astream(
        self, messages: list[dict], tools: list[dict], verbosity: str = "",
        speak: bool = False, speak_model: str = "",
    ) -> AsyncIterator[LLMEvent]:
        """Yield token deltas then one 'final' event with the complete message dict.

        final message dict: {"content": str, "tool_calls": [{"id","name","arguments"}],
        "usage": {...}|None}
        iter46 D5: `verbosity` ("" = the default level) routes the round to
        the matching hot-path LLM (per-state override support).
        iter112: `speak=True` marks the SPEAK stream of the speak-first
        pipeline — when a speak model resolves it rides the dedicated
        speak twin (no verbosity param possible); "" keeps the same LLM both
        streams (E1). Bare `tools=[]` was always supported.
        iter114c: `speak_model` = the per-state resolved speak model
        (builder `_speak_model_for`: speak_model for the fast states,
        openai_model for the tool-heavy ones); "" falls back to
        settings.speak_model (legacy all-luna mode).
        """
        spk = speak_model or self.settings.speak_model
        if speak and spk:
            hot = self.speak_llm(spk)
        elif verbosity and verbosity != self.settings.openai_verbosity:
            hot = self.llm_for_verbosity(verbosity)
        else:
            hot = self._llm
        bound = hot.bind_tools(tools) if tools else hot
        content_parts: list[str] = []
        tool_acc: dict[int, dict] = {}
        usage: dict | None = None
        # iter46 T4a: an early `tool_call_start` event the FIRST time a given
        # index's name is complete — the builder fires the transition prewarm
        # at name detection (tool name encodes the destination), banking the
        # remaining speaking round + TTS drain as warm-prefill head start.
        # OpenAI streams the tool name BEFORE its arguments, so args arrival
        # proves the name finished; a no-args call flushes at stream end.
        started: set[int] = set()
        async for chunk in bound.astream(messages):
            text = _chunk_text(chunk)
            if text:
                content_parts.append(text)
                yield LLMEvent({"type": "token", "text": text})
            for tc in getattr(chunk, "tool_call_chunks", None) or []:
                idx = tc.get("index") or 0
                acc = tool_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                if tc.get("id"):
                    acc["id"] = tc["id"]
                if tc.get("name"):
                    acc["name"] = acc["name"] + tc["name"] if acc["name"] and not tc.get("id") else tc["name"]
                if tc.get("args"):
                    acc["arguments"] += tc["args"]
                if acc["name"] and tc.get("args") and idx not in started:
                    started.add(idx)
                    yield LLMEvent({"type": "tool_call_start", "name": acc["name"]})
            if getattr(chunk, "usage_metadata", None):
                usage = dict(chunk.usage_metadata)
        for idx, v in sorted(tool_acc.items()):
            if v["name"] and idx not in started:
                started.add(idx)
                yield LLMEvent({"type": "tool_call_start", "name": v["name"]})
        tool_calls = [
            {"id": v["id"] or f"call_{i}", "name": v["name"], "arguments": v["arguments"] or "{}"}
            for i, v in sorted(tool_acc.items())
        ]
        yield LLMEvent({
            "type": "final",
            "content": "".join(content_parts),
            "tool_calls": tool_calls,
            "usage": usage,
        })


def _chunk_text(chunk) -> str:
    content = getattr(chunk, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # content blocks
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""
