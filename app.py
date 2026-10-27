#!/usr/bin/env python3
"""
gamebrain — a generic, game-agnostic voice + text intel assistant.

The engine knows NOTHING about any particular game. All game knowledge
lives in a *pack*: a directory with a ``pack.json`` manifest and a
``kb.json`` knowledge base. The manifest declares the tools, the prompt,
the reference tabs and the UI copy; the engine executes the tools with a
generic query layer.

Swap the pack and the whole assistant becomes a different game.

Endpoints:
  GET  /health              -> status, pack info, engine info
  GET  /pack                -> the loaded pack manifest (public)
  GET  /tools               -> tool manifest (what the LLM can call)
  POST /chat                -> {"message": str} -> JSON answer
  POST /chat/stream         -> SSE: start / delta / tool / answer / error
  GET  /kb/<section>        -> raw KB section (debug)
  GET  /ref/<kind>          -> pre-shaped reference data for the UI
  GET  /tts/voices          -> available voices
  POST /tts                 -> WAV/MP3 audio bytes
  POST /tts/stream          -> streamed WAV (first chunk plays early)

Optional integrations (off unless configured):
  - Hermes-compatible chat API, used as an escalation target for
    questions the local model cannot answer. Set HERMES_URL/HERMES_KEY.
  - Any LLM endpoint: Ollama's native API (LLM_API=ollama) or any
    OpenAI-compatible API (LLM_API=openai) - OpenRouter, OpenAI, vLLM,
    LM Studio, llama.cpp, LiteLLM, Groq, Together.

Design notes:
- The pack KB is loaded ONCE at startup into RAM.
- Tool calls run through the LLM's native tool-calling interface.
- The pack is hot-reloadable via POST /pack/reload - no restart needed.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import struct
import time
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (HTMLResponse, JSONResponse, Response,
                               StreamingResponse)
from pydantic import BaseModel

try:
    from engine_pack import Pack, QueryEngine, norm_name, slugify, toks
except ImportError:  # allow "python app.py" from the same directory
    from .engine_pack import Pack, QueryEngine, norm_name, slugify, toks

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------
PACK_DIR = os.environ.get("PACK_DIR", "./packs/default")
# ---- LLM ----
# Two wire formats are supported. Set LLM_API to pick one.
#
#   ollama  (default)  POST {LLM_URL}/api/chat
#                      Ollama's native format.
#   openai             POST {LLM_URL}/chat/completions
#                      The OpenAI-compatible format - OpenRouter, OpenAI,
#                      vLLM, LM Studio, llama.cpp, LiteLLM, Groq, Together,
#                      and Ollama's own /v1 endpoint all speak it.
#
# The tool schema is identical in both, so the same pack works either way.
LLM_API = os.environ.get("LLM_API", "ollama").strip().lower()
if LLM_API not in ("ollama", "openai"):
    LLM_API = "ollama"
# LLM_URL replaces OLLAMA_URL (the old name still works, for compatibility).
LLM_URL = (os.environ.get("LLM_URL")
           or os.environ.get("OLLAMA_URL")
           or "http://127.0.0.1:11434").rstrip("/")
LLM_MODEL = (os.environ.get("LLM_MODEL")
             or os.environ.get("OLLAMA_MODEL")
             or "qwen3:8b")
# API key for the LLM endpoint. Sent as "Authorization: Bearer <key>".
# Leave empty for a local model that needs no auth.
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
# Some providers use a different header name (e.g. "x-api-key"). If set,
# this header is used INSTEAD of Authorization.
LLM_API_KEY_HEADER = os.environ.get("LLM_API_KEY_HEADER", "")
LLM_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.3"))
LLM_NUM_CTX = int(os.environ.get("LLM_NUM_CTX", "8192"))
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "0"))
# Optional escalation target (any OpenAI-compatible /v1/chat/completions API)
HERMES_URL = os.environ.get("HERMES_URL", "").rstrip("/")
HERMES_KEY = os.environ.get("HERMES_KEY", "")
PORT = int(os.environ.get("PORT", "8095"))
UI_BUILD = os.environ.get("UI_BUILD", "dev")

# TTS - any OpenAI-compatible /v1/audio/speech endpoint (speaches, etc.)
TTS_URL = os.environ.get("TTS_URL", "").rstrip("/")
TTS_MODEL = os.environ.get("TTS_MODEL", "speaches-ai/Kokoro-82M-v1.0-ONNX")
TTS_VOICE = os.environ.get("TTS_VOICE", "af_bella")
TTS_SPEED = float(os.environ.get("TTS_SPEED", "1.0"))
TTS_VOICES: List[str] = []

# Serving the UI
UI_PATH = os.environ.get("UI_PATH", os.path.join(os.path.dirname(
    os.path.abspath(__file__)), "ui", "index.html"))

app = FastAPI(title="gamebrain", version="1.0.0")

PACK: Optional[Pack] = None
ENGINE: Optional[QueryEngine] = None
CLIENT: Optional[httpx.AsyncClient] = None
TTS_CACHE_TS = 0.0


# --------------------------------------------------------------------------
# STARTUP / SHUTDOWN
# --------------------------------------------------------------------------
@app.on_event("startup")
async def startup():
    global PACK, ENGINE, CLIENT
    CLIENT = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0,
                                                      read=300.0, write=60.0))
    load_pack(PACK_DIR)

    # Pre-load the LLM so the first question does not pay a cold start.
    try:
        t_warm = time.time()
        r = await CLIENT.post(
            _llm_endpoint(),
            json=call_ollama([{"role": "user", "content": "ok"}]),
            headers=_llm_headers(),
            timeout=180.0,
        )
        r.raise_for_status()
        print(f"[gamebrain] LLM warm in {time.time() - t_warm:.1f}s", flush=True)
    except Exception as e:
        print(f"[gamebrain] LLM warmup failed: {e}", flush=True)


@app.on_event("shutdown")
async def shutdown():
    if CLIENT:
        await CLIENT.aclose()


def load_pack(path: str) -> Pack:
    """Load (or reload) a pack and rebuild the query engine."""
    global PACK, ENGINE
    p = Pack.load(path)
    PACK = p
    ENGINE = QueryEngine(p)
    for e in p.errors:
        print(f"[gamebrain] PACK WARNING: {e}", flush=True)
    kb_bytes = 0
    kb_path = os.path.join(p.root, p.manifest.get("kb", "kb.json"))
    if os.path.exists(kb_path):
        kb_bytes = os.path.getsize(kb_path)
    print(f"[gamebrain] pack '{p.id}' loaded: {len(p.tools)} tools, "
          f"{len(p.kb)} KB sections, {kb_bytes} bytes", flush=True)
    return p


# --------------------------------------------------------------------------
# TOOLS  (built from the pack manifest)
# --------------------------------------------------------------------------
def tool_specs() -> List[Dict[str, Any]]:
    """Convert the pack's tool definitions into Ollama tool specs."""
    if not PACK:
        return []
    specs = []
    for t in PACK.tools:
        specs.append({
            "type": "function",
            "function": {
                "name": t.get("name"),
                "description": t.get("description", ""),
                "parameters": t.get("parameters", {"type": "object",
                                                   "properties": {}}),
            },
        })
    # optional escalation tool - only offered when it is actually usable
    if HERMES_URL and HERMES_KEY:
        specs.append({
            "type": "function",
            "function": {
                "name": "ask_agent",
                "description": "Ask a more capable general-purpose agent a "
                               "question it can look up (the web, your own "
                               "files, current facts). Use this only when "
                               "you genuinely cannot answer from the "
                               "knowledge base or general knowledge.",
                "parameters": {
                    "type": "object",
                    "properties": {"question": {"type": "string"}},
                    "required": ["question"],
                },
            },
        })
    return specs


async def exec_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    if ENGINE and PACK:
        for t in PACK.tools:
            if t.get("name") == name:
                return ENGINE.run(name, args)
    if name == "ask_agent":
        return await _ask_agent(args)
    return {"error": f"unknown tool {name}"}


async def _ask_agent(args) -> Dict[str, Any]:
    """Escalate to a general-purpose agent (optional integration)."""
    q = args.get("question") or ""
    if not HERMES_URL or not HERMES_KEY:
        return {"error": "escalation agent not configured",
                "question": q}
    try:
        r = await CLIENT.post(
            f"{HERMES_URL}/v1/chat/completions",
            headers={"Authorization": f"Bearer {HERMES_KEY}"},
            json={"model": "full", "messages": [
                {"role": "system", "content":
                    "You are answering a question asked aloud by the user, "
                    "relayed by a voice assistant. Reply with the ANSWER ONLY. "
                    "No preamble, no explanation of your process, no mention "
                    "of injected context, no tool narration. If you need to "
                    "check something, do it silently and then give the plain "
                    "answer. Max 3 sentences unless detail is asked for."},
                {"role": "user", "content": q}]},
            timeout=180.0,
        )
        r.raise_for_status()
        answer = (r.json()["choices"][0]["message"]["content"] or "").strip()
        return {"answer": answer, "source": "agent"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "question": q}


# --------------------------------------------------------------------------
# SYSTEM PROMPT  (engine rules + the pack's fragment)
# --------------------------------------------------------------------------
ENGINE_PROMPT = """You are a voice and text assistant for a game intel
service. You answer questions about the game the user is playing, using the
knowledge-base tools.

TOOL SELECTION - follow this strictly:
* Questions about the game's map, entities, items, locations, missions, or
  strategy -> use the matching tool. NEVER guess names, locations, rewards or
  drop tables from memory - always call a tool and read the result.
* ALWAYS include video links when the tool result provides them. If a result
  has a "videos" array with id/title/author/url, you MUST end your answer with
  that link rendered as markdown: [Title](url).
* Ordinary general knowledge (capital cities, authors, definitions, science,
  history) -> answer DIRECTLY and immediately. Do not call a tool for it.
* Anything you cannot know from general knowledge or the knowledge base ->
  use ask_agent if it is available. If it is not available, say plainly that
  you do not have that information.

Other rules:
- Be concise and direct: give the answer, the location and the risk. Skip
  preamble and filler.
- If a tool returns found:false, say you do not have it and offer what you do
  have. Never invent an answer.
- Copy any quoted field (damage rules, drop tables, stat blocks) VERBATIM.
  A paraphrase can invert the advice and be dangerous.
- Use British English.
"""


def build_system_prompt() -> str:
    parts = [ENGINE_PROMPT]
    if PACK:
        frag = PACK.system_prompt()
        if frag:
            parts.append(frag.strip())
        facts = PACK.facts()
        if facts:
            parts.append("Facts you can state without a tool call:\n" +
                         "\n".join(f"- {f}" for f in facts))
    return "\n\n".join(parts)


# --------------------------------------------------------------------------
# TOOL LOOP
# --------------------------------------------------------------------------
def call_ollama(messages, tools=None, stream=False) -> Dict[str, Any]:
    """Build the request payload for the configured LLM API.

    Two wire formats are supported, selected by LLM_API:

      ollama  (default) - POST {LLM_URL}/api/chat
                          Ollama's native format. No auth header unless
                          LLM_API_KEY is set.

      openai            - POST {LLM_URL}/chat/completions
                          The OpenAI-compatible format, understood by
                          OpenRouter, OpenAI, vLLM, LM Studio, llama.cpp,
                          LiteLLM, Groq, Together, Ollama's /v1 endpoint
                          and most hosted providers.

                          Tool calling uses the OpenAI "tools" schema
                          (identical to Ollama's), and the response is
                          normalised back into the same shape the tool
                          loop expects, so the loop is identical for both.
    """
    payload: Dict[str, Any] = {
        "model": LLM_MODEL,
        "think": False,
        "stream": stream,
        "keep_alive": "24h",
        "options": {"temperature": LLM_TEMPERATURE, "num_ctx": LLM_NUM_CTX},
        "messages": messages,
    }
    if tools:
        payload["tools"] = tools
    if LLM_API == "openai":
        # the OpenAI schema has no "think" / "keep_alive" / "options"
        payload.pop("think", None)
        payload.pop("keep_alive", None)
        opts = payload.pop("options", {})
        payload["temperature"] = opts.get("temperature", LLM_TEMPERATURE)
        if LLM_MAX_TOKENS:
            payload["max_tokens"] = LLM_MAX_TOKENS
    return payload


def _llm_headers() -> Dict[str, str]:
    """Auth headers for the LLM endpoint (empty when not configured)."""
    h = {"Content-Type": "application/json"}
    if LLM_API_KEY:
        h["Authorization"] = f"Bearer {LLM_API_KEY}"
    elif LLM_API_KEY_HEADER:
        h[LLM_API_KEY_HEADER] = LLM_API_KEY
    return h


def _llm_endpoint() -> str:
    """The full URL to POST the chat request to."""
    if LLM_API == "openai":
        base = LLM_URL.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"
    return f"{LLM_URL.rstrip('/')}/api/chat"


def _normalise_openai_chunk(chunk: Dict[str, Any]) -> Dict[str, Any]:
    """Convert an OpenAI streaming chunk into the engine's internal shape.

    Both formats end up as {"message": {"content": str, "tool_calls": [...]}}
    so the tool loop below does not need to know which API is in use.
    """
    choices = chunk.get("choices") or []
    if not choices:
        return {"message": {}}
    ch = choices[0] or {}
    delta = ch.get("delta") or {}
    msg: Dict[str, Any] = {"message": {"content": delta.get("content") or ""}}
    tcs = delta.get("tool_calls")
    if tcs:
        calls = []
        for tc in tcs:
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    pass
            calls.append({
                # keep the provider's call id - the OpenAI schema needs it
                # on the tool RESULT message (tool_call_id).
                "id": tc.get("id") or "",
                "function": {
                    "name": fn.get("name"),
                    "arguments": args if isinstance(args, dict) else {},
                }
            })
        msg["message"]["tool_calls"] = calls
    return msg


async def run_tool_loop(message: str, session_id: str = "default",
                        max_iter: int = 6, on_delta=None) -> Dict[str, Any]:
    """Run the tool loop.

    Streams the final answer token-by-token via ``on_delta`` so the UI can
    render text and start speaking the first sentence while the rest is
    still being generated.
    """
    messages = [{"role": "system", "content": build_system_prompt()},
                {"role": "user", "content": message}]
    tool_log: List[Dict[str, Any]] = []
    timings: List[Dict[str, Any]] = []
    specs = tool_specs()

    for i in range(max_iter):
        payload = call_ollama(messages, tools=specs, stream=True)
        t_iter = time.time()
        content_parts: List[str] = []
        calls: List[Dict[str, Any]] = []
        msg: Dict[str, Any] = {"role": "assistant", "content": ""}
        async with CLIENT.stream("POST", _llm_endpoint(),
                                 json=payload,
                                 headers=_llm_headers()) as resp:
            if resp.status_code >= 400:
                # read the body BEFORE raising - httpx discards it
                err = await resp.aread()
                raise RuntimeError(
                    f"LLM {resp.status_code} from {_llm_endpoint()}: "
                    f"{err.decode('utf-8', 'replace')[:800]}")
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line or not line.strip():
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                    if line == "[DONE]":
                        break
                try:
                    chunk = json.loads(line)
                except Exception:
                    continue
                if LLM_API == "openai":
                    chunk = _normalise_openai_chunk(chunk)
                cm = chunk.get("message") or {}
                piece = cm.get("content") or ""
                if piece:
                    content_parts.append(piece)
                    if on_delta:
                        await on_delta(piece)
                tc = cm.get("tool_calls")
                if tc:
                    calls = tc
        msg["content"] = "".join(content_parts)
        if calls:
            # The assistant message we append to the conversation must match
            # the API we are talking to.
            #
            # Ollama accepts `arguments` as an OBJECT, but the OpenAI
            # schema requires a JSON STRING. Sending the parsed dict back
            # makes /chat/completions reject the whole request with
            # "cannot unmarshal object into ... arguments of type string".
            if LLM_API == "openai":
                msg["tool_calls"] = [
                    {
                        "id": c.get("id") or f"call_{i}_{j}",
                        "type": "function",
                        "function": {
                            "name": (c.get("function") or {}).get("name"),
                            "arguments": json.dumps(
                                (c.get("function") or {}).get("arguments")
                                or {}, ensure_ascii=False),
                        },
                    }
                    for j, c in enumerate(calls)
                ]
            else:
                msg["tool_calls"] = calls
        timings.append({"iter": i + 1,
                        "ms": int((time.time() - t_iter) * 1000),
                        "tool": (calls[0].get("function") or {}).get("name")
                        if calls else None})
        messages.append(msg)
        if not calls:
            return {"answer": (msg.get("content") or "").strip(),
                    "tools_used": tool_log, "iterations": i + 1,
                    "timings": timings}
        for k, c in enumerate(calls):
            fn = c.get("function") or {}
            tname = fn.get("name")
            raw_args = fn.get("arguments")
            # Ollama returns `arguments` as a dict; some backends send JSON.
            if isinstance(raw_args, dict):
                targs = raw_args
            elif isinstance(raw_args, str) and raw_args.strip():
                try:
                    targs = json.loads(raw_args)
                except Exception:
                    targs = {"raw": raw_args}
            else:
                targs = {}
            if not isinstance(targs, dict):
                targs = {"raw": targs}
            res = await exec_tool(tname, targs)
            tool_log.append({"tool": tname, "args": targs, "result": res})
            if LLM_API == "openai":
                messages.append({
                    "role": "tool",
                    "tool_call_id": (calls[k].get("id")
                                     or f"call_{i}_{k}") if k < len(calls)
                                    else f"call_{i}_{k}",
                    "name": tname,
                    "content": json.dumps(res, ensure_ascii=False)[:4000],
                })
            else:
                messages.append({"role": "tool", "name": tname,
                                 "content": json.dumps(res, ensure_ascii=False)[:4000]})
            # A tool may already have produced a complete spoken answer.
            if isinstance(res, dict) and res.get("answer"):
                return {"answer": str(res["answer"]).strip(),
                        "tools_used": tool_log, "iterations": i + 1,
                        "timings": timings, "short_circuit": tname}
    return {"answer": "Reached tool-call limit.", "tools_used": tool_log,
            "iterations": max_iter, "timings": timings}


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
class ChatReq(BaseModel):
    message: str
    session_id: Optional[str] = None


@app.get("/health")
async def health():
    return {
        "ok": True,
        "pack": PACK.id if PACK else None,
        "pack_name": PACK.name if PACK else None,
        "pack_errors": PACK.errors if PACK else ["no pack loaded"],
        "kb_loaded": bool(PACK and PACK.kb),
        "kb_sections": len(PACK.kb) if PACK else 0,
        "tools": len(PACK.tools) if PACK else 0,
        "model": LLM_MODEL,
        "llm": LLM_URL,
        "llm_api": LLM_API,
        "llm_auth": bool(LLM_API_KEY),
        "tts": bool(TTS_URL),
        "escalation": bool(HERMES_URL and HERMES_KEY),
        "build": UI_BUILD,
    }


@app.get("/pack")
async def pack_manifest():
    """The public pack manifest - the UI reads its copy and tabs from here."""
    if not PACK:
        raise HTTPException(503, "no pack loaded")
    return {
        "id": PACK.id,
        "name": PACK.name,
        "subtitle": PACK.subtitle,
        "ui": PACK.ui(),
        "quick_prompts": PACK.quick_prompts(),
        "ref_tabs": PACK.ref_tabs,
        "known_facts": PACK.facts(),
    }


@app.get("/tools")
async def tools():
    return {"tools": tool_specs()}


@app.post("/pack/reload")
async def pack_reload():
    """Hot-reload the pack from disk - edit a kb.json and it is live."""
    try:
        p = load_pack(PACK_DIR)
        return {"ok": True, "pack": p.id, "errors": p.errors}
    except Exception as e:
        raise HTTPException(400, f"{type(e).__name__}: {e}")


@app.post("/chat")
async def chat(req: ChatReq):
    t0 = time.time()
    out = await run_tool_loop(req.message, req.session_id or "default")
    out["elapsed_ms"] = int((time.time() - t0) * 1000)
    return out


@app.post("/chat/stream")
async def chat_stream(req: ChatReq):
    async def gen():
        t0 = time.time()
        yield f"data: {json.dumps({'type': 'start', 'model': LLM_MODEL})}\n\n"

        # The tool loop can take 10-15s with NO bytes on the wire. A silent
        # gap that long is enough for Cloudflare / the mobile NAT to drop
        # the connection, and Chrome then rejects the pending reader.read()
        # with "network error" - even though the server logged a 200 OK.
        # Send an SSE comment heartbeat every 2s so the socket never idles.
        q: asyncio.Queue = asyncio.Queue()

        async def producer():
            try:
                async def on_delta(piece: str):
                    await q.put(f"data: {json.dumps({'type': 'delta', 'content': piece}, ensure_ascii=False)}\n\n")

                out = await run_tool_loop(req.message, req.session_id or "default",
                                          on_delta=on_delta)
                for tl in out.get("tools_used", []):
                    await q.put(f"data: {json.dumps({'type': 'tool', **tl}, ensure_ascii=False)}\n\n")
                await q.put(f"data: {json.dumps({'type': 'answer', 'content': out.get('answer', ''), 'elapsed_ms': int((time.time() - t0) * 1000), 'timings': out.get('timings')})}\n\n")
            except Exception as e:
                await q.put(f"data: {json.dumps({'type': 'error', 'error': f'{type(e).__name__}: {e}'})}\n\n")
            await q.put("data: [DONE]\n\n")

        task = asyncio.create_task(producer())
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=2.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield msg
                if msg.rstrip().endswith("[DONE]"):
                    break
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store",
                                      "X-Accel-Buffering": "no"})


@app.get("/kb/{section}")
async def kb_section(section: str):
    if not PACK or section not in PACK.kb:
        raise HTTPException(404, f"no section {section}")
    return PACK.kb[section]


# --------------------------------------------------------------------------
# REFERENCE DATA FOR THE UI
# --------------------------------------------------------------------------
@app.get("/ref/{kind}")
async def ref_data(kind: str):
    """Pre-built reference datasets for the UI.

    The kinds and their shape come from the pack manifest, so a new game
    gets its reference tabs for free.
    """
    if not PACK:
        raise HTTPException(503, "no pack loaded")
    kb = PACK.kb

    # A pack may define custom reference sections.
    #
    # The browser requests /ref/<tab.id>, so a tab is matched on its id
    # OR its kind - a pack that spells them differently still resolves.
    for tab in PACK.ref_tabs:
        if kind not in (tab.get("id"), tab.get("kind")):
            continue
        custom = tab.get("custom")
        if custom:
            return _custom_ref(kb, custom)
        collection = tab.get("collection")
        if collection:
            return {"items": _as_items(kb.get(collection)), "count": len(_as_items(kb.get(collection)))}
        break

    if kind == "grid":
        gi = kb.get("grid_index", {}) or {}
        return {"grid": {k: [x.get("name") for x in (v if isinstance(v, list) else [])]
                          for k, v in gi.items()}}

    if kind == "list":
        coll = kb.get(tab_collection(PACK, kind), []) or []
        return {"items": coll, "count": len(coll)}

    raise HTTPException(404, f"unknown kind {kind}")


def _as_items(data: Any) -> List[Any]:
    """Normalise a KB section into a list of dicts for the reference pane.

    A dict section (e.g. key_lookup) becomes its values, with the dict key
    injected as "name" when the record does not already carry one - without
    it the UI renders a blank row.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        out = []
        for k, v in data.items():
            if isinstance(v, dict):
                v = dict(v)
                v.setdefault("name", k)
            out.append(v)
        return out
    return []


def tab_collection(pack: Pack, kind: str) -> str:
    for t in pack.ref_tabs:
        if kind in (t.get("id"), t.get("kind")):
            return t.get("collection", "")
    return ""


def _custom_ref(kb: Dict[str, Any], spec: Dict[str, Any]) -> Dict[str, Any]:
    """Build a reference list from a small declarative spec.

    ``spec`` = {"collection": "bosses.commanders", "fields": ["name", "grid"],
    "filter": {"field": "kind", "in": ["Commander"]}}
    """
    path = spec.get("collection", "")
    data: Any = kb
    for part in path.split("."):
        if isinstance(data, dict) and part in data:
            data = data[part]
        else:
            data = []
            break
    items = data if isinstance(data, list) else []
    filt = spec.get("filter")
    if filt:
        f = filt.get("field")
        allowed = [str(x).lower() for x in (filt.get("in") or [])]
        if f and allowed:
            items = [i for i in items
                     if str(i.get(f, "")).lower() in allowed]
    fields = spec.get("fields")
    out = []
    for i in items:
        if not isinstance(i, dict):
            continue
        if fields:
            out.append({f: i.get(f) for f in fields})
        else:
            out.append(i)
    return {"items": out, "count": len(out)}


# --------------------------------------------------------------------------
# TTS
# --------------------------------------------------------------------------
async def _tts_voices() -> List[str]:
    global TTS_VOICES
    if TTS_VOICES:
        return TTS_VOICES
    if TTS_URL:
        try:
            r = await CLIENT.get(f"{TTS_URL}/v1/models", timeout=15.0)
            r.raise_for_status()
            for m in r.json().get("data", []):
                for v in (m.get("voices") or []):
                    n = v.get("name")
                    if n and n not in TTS_VOICES:
                        TTS_VOICES.append(n)
        except Exception:
            pass
    if not TTS_VOICES:
        TTS_VOICES = ["af_bella", "af_heart", "af_nicole", "am_michael", "bf_emma"]
    return TTS_VOICES


class TTSReq(BaseModel):
    text: str
    voice: Optional[str] = None
    speed: Optional[float] = None
    fmt: str = "wav"


@app.get("/tts/voices")
async def tts_voices():
    return {"voices": await _tts_voices(), "default": TTS_VOICE,
            "model": TTS_MODEL, "enabled": bool(TTS_URL)}


@app.post("/tts")
async def tts(req: TTSReq):
    """Text-to-speech. Returns audio bytes (wav by default)."""
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(400, "text required")
    if not TTS_URL:
        raise HTTPException(503, "TTS not configured (set TTS_URL)")
    if len(text) > 2000:
        text = text[:2000]
    voice = req.voice or TTS_VOICE
    speed = req.speed if req.speed else TTS_SPEED
    fmt = (req.fmt or "wav").lower()
    payload = {"model": TTS_MODEL, "input": text, "voice": voice,
               "response_format": fmt, "speed": speed}
    try:
        r = await CLIENT.post(f"{TTS_URL}/v1/audio/speech", json=payload,
                              timeout=120.0)
        if r.status_code >= 400:
            return JSONResponse({"error": f"TTS {r.status_code}: {r.text[:200]}"},
                                status_code=502)
        media = {"wav": "audio/wav", "mp3": "audio/mpeg",
                 "opus": "audio/opus", "flac": "audio/flac"}.get(fmt, "audio/wav")
        return Response(content=r.content, media_type=media,
                        headers={"Cache-Control": "no-store"})
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"},
                            status_code=502)


@app.get("/tts")
async def tts_get(text: str, voice: Optional[str] = None, fmt: str = "wav"):
    return await tts(TTSReq(text=text, voice=voice, fmt=fmt))


def _split_for_speech(text: str, max_chars: int = 180) -> List[str]:
    """Split into speakable chunks on sentence boundaries.

    Markdown is stripped first so the synthesiser does not read '**' aloud.
    """
    t = text or ""
    t = re.sub(r"```.*?```", " ", t, flags=re.S)
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)
    t = re.sub(r"\*(.+?)\*", r"\1", t)
    t = re.sub(r"`([^`]+)`", r"\1", t)
    t = re.sub(r"\[(.+?)\]\((.+?)\)", r"\1", t)
    t = re.sub(r"^\s*[-*•]\s*", "", t, flags=re.M)
    t = t.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return []
    parts = re.split(r"(?<=[.!?])\s+", t)
    chunks: List[str] = []
    buf = ""
    for p in parts:
        if not p:
            continue
        if len(buf) + len(p) + 1 <= max_chars:
            buf = (buf + " " + p).strip()
        else:
            if buf:
                chunks.append(buf)
            while len(p) > max_chars:
                cut = p.rfind(" ", 0, max_chars)
                if cut <= 0:
                    cut = max_chars
                chunks.append(p[:cut].strip())
                p = p[cut:].strip()
            buf = p
    if buf:
        chunks.append(buf)
    return chunks


def _pcm_from_wav(raw: bytes) -> bytes:
    """Strip the RIFF container from a complete WAV, returning raw PCM."""
    if raw[:4] != b"RIFF" or len(raw) < 44:
        return raw
    idx = 12
    while idx < len(raw) - 8:
        cid = raw[idx:idx + 4]
        sz = struct.unpack("<I", raw[idx + 4:idx + 8])[0]
        if cid == b"data":
            start = idx + 8
            return raw[start:start + sz]
        idx += 8 + sz + (sz & 1)
    return raw[44:]


def _streaming_wav_header(sample_rate: int = 24000, channels: int = 1,
                          bits: int = 16) -> bytes:
    """RIFF header for a streamed WAV (0xFFFFFFFF sizes = unknown length)."""
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    return (
        b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate,
                                byte_rate, block_align, bits)
        + b"data" + struct.pack("<I", 0xFFFFFFFF)
    )


@app.post("/tts/stream")
async def tts_stream(req: TTSReq):
    """Stream synthesised audio chunk by chunk.

    The browser starts playing the first chunk while the rest is still being
    generated, so the wait is one chunk instead of the whole answer.
    """
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(400, "text required")
    if not TTS_URL:
        raise HTTPException(503, "TTS not configured (set TTS_URL)")
    voice = req.voice or TTS_VOICE
    chunks = _split_for_speech(text[:4000])
    if not chunks:
        chunks = [text[:4000]]

    async def gen():
        yield _streaming_wav_header(24000, 1, 16)
        for ch in chunks:
            payload = {"model": TTS_MODEL, "input": ch, "voice": voice,
                       "response_format": "wav",
                       "speed": req.speed if req.speed else TTS_SPEED}
            try:
                r = await CLIENT.post(f"{TTS_URL}/v1/audio/speech",
                                      json=payload, timeout=180.0)
                if r.status_code < 400:
                    pcm = _pcm_from_wav(r.content)
                    if pcm:
                        yield pcm
            except Exception:
                continue

    return StreamingResponse(gen(), media_type="audio/wav",
                             headers={"Cache-Control": "no-store",
                                      "X-Chunks": str(len(chunks))})


@app.get("/tts/stream")
async def tts_stream_get(text: str, voice: Optional[str] = None):
    return await tts_stream(TTSReq(text=text, voice=voice))


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
def _ui_headers() -> dict:
    return {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache", "Expires": "0",
        "ETag": f'"{UI_BUILD}"', "X-UI-Build": UI_BUILD,
    }


@app.get("/build")
async def build_stamp():
    return {"build": UI_BUILD, "pack": PACK.id if PACK else None}


@app.get("/")
async def ui_index():
    if not os.path.exists(UI_PATH):
        raise HTTPException(500, f"UI not found at {UI_PATH}")
    with open(UI_PATH, encoding="utf-8") as f:
        body = f.read()
    return HTMLResponse(body, headers=_ui_headers())


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
