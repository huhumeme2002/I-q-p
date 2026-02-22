import os
import sys
import json
import uuid
import time
import asyncio
import hashlib
import base64
import copy
from collections import OrderedDict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
import logging
import httpx
import store
import iflow_auth
import qwen_auth

# ============================================================
# LOGGING CONFIGURATION
# ============================================================
def _setup_logging():
    """Configure logging with a StreamHandler if no handlers are set."""
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        root.addHandler(handler)
        root.setLevel(logging.INFO)

_setup_logging()

logger = logging.getLogger("proxy")

# ============================================================
# IMAGE DESCRIPTION CACHE (in-memory LRU, max 200 entries)
# ============================================================
_vision_cache: OrderedDict[str, str] = OrderedDict()
_VISION_CACHE_MAX = 200

def _cache_key(image_source: dict) -> str | None:
    """Generate a cache key from image source data."""
    src_type = image_source.get("type", "")
    if src_type == "base64":
        data = image_source.get("data", "")
        if data:
            return hashlib.sha256(data.encode()).hexdigest()
    elif src_type == "url":
        url = image_source.get("url", "")
        if url and not url.startswith("data:"):
            return hashlib.md5(url.encode()).hexdigest()
    return None

store.init_data()

# ============================================================
# PERSISTENT HTTP CLIENT
# ============================================================
_http_client: httpx.AsyncClient | None = None

# Per-proxy persistent clients (SOCKS5 proxies don't support HTTP/2)
# Key: proxy URL string, Value: AsyncClient
_proxy_clients: dict[str, httpx.AsyncClient] = {}

_auto_refresh: iflow_auth.IFlowAutoRefresh | None = None
_qwen_auto_refresh: qwen_auth.QwenAutoRefresh | None = None
_proxy_checker_task: asyncio.Task | None = None

# In-memory proxy status cache: proxy_id -> bool (True=alive, False=dead)
_proxy_status: dict[str, bool] = {}

# In-memory admin session tokens: token -> expiry_timestamp
# Tokens are random UUIDs, NOT the password itself
_admin_sessions: dict[str, float] = {}
_SESSION_TTL = 86400.0  # 24 hours


def _purge_expired_sessions() -> None:
    """Remove expired session tokens from _admin_sessions."""
    now = time.time()
    expired = [t for t, exp in _admin_sessions.items() if exp <= now]
    for t in expired:
        del _admin_sessions[t]

# CSRF tokens: deque of (token, expiry) tuples, insertion order for oldest-first eviction;
# _csrf_tokens_set provides O(1) membership checks.
_CSRF_TTL = 86400.0  # 24 hours
_csrf_tokens: deque[tuple[str, float]] = deque()
_csrf_tokens_set: set[str] = set()


# Reliable endpoints for proxy health checks (tried in order)
_PROXY_CHECK_URLS = [
    "https://1.1.1.1",           # Cloudflare — fast, reliable
    "https://www.google.com",    # fallback
]


async def _check_proxy_alive(proxy_str: str) -> bool:
    """Test if a proxy is reachable by connecting to a reliable endpoint."""
    for url in _PROXY_CHECK_URLS:
        try:
            async with httpx.AsyncClient(
                proxy=proxy_str,
                timeout=httpx.Timeout(connect=8.0, read=10.0, write=5.0, pool=5.0),
                http2=False,
            ) as client:
                resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                return resp.status_code < 500
        except Exception:
            continue
    return False


async def _proxy_health_loop(interval: int = 600):
    """Background task: check all pool proxies every `interval` seconds, reassign if dead."""
    health_logger = logging.getLogger("proxy.health")
    while True:
        await asyncio.sleep(interval)
        pool = store.get_proxy_pool()
        if not pool:
            continue
        health_logger.info(f"[ProxyHealth] Checking {len(pool)} proxies...")
        for p in pool:
            proxy_str = store._build_proxy_str(p)
            alive = await _check_proxy_alive(proxy_str)
            _proxy_status[p["id"]] = alive
            if not alive:
                health_logger.warning(f"[ProxyHealth] Dead proxy {p['host']}:{p['port']} — reassigning accounts")
                accounts = store.get_accounts()
                for acc in accounts:
                    if acc.get("proxy", "").strip() == proxy_str:
                        store.reassign_account_proxy(acc["id"])
        health_logger.info("[ProxyHealth] Done.")


def _is_proxy_error(exc: Exception) -> bool:
    """Return True if the exception looks like a proxy connectivity failure."""
    return isinstance(exc, (
        httpx.ProxyError,
        httpx.ConnectError,
        httpx.ConnectTimeout,
    ))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http_client, _auto_refresh, _qwen_auto_refresh, _proxy_checker_task

    # Get proxy settings
    settings = store.get_settings()
    proxy_url = settings.get("proxy_url")

    _http_client = httpx.AsyncClient(
        proxy=proxy_url if proxy_url else None,
        timeout=httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0),
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20, keepalive_expiry=30.0),
        http2=True,
    )

    # Auto-import from ~/.iflow/oauth_creds.json if no accounts exist
    if not store.get_accounts():
        try:
            creds = iflow_auth.read_creds()
            if creds.get("apiKey"):
                store.add_account(
                    name=creds.get("userName", "iFlow Account"),
                    api_key=creds["apiKey"],
                    provider="iflow",
                )
                print(f"  ✅ Auto-imported iFlow account: {creds.get('userName')} ({creds.get('email')})")
        except Exception as e:
            print(f"  ⚠️  Could not auto-import iFlow creds: {e}")

    # Auto-import Qwen creds from ~/.cli-proxy-api/qwen-*.json
    existing_qwen_emails = {a.get("qwen_email") for a in store.get_accounts() if a.get("provider") == "qwen"}
    for qcreds in qwen_auth.list_qwen_creds():
        email = qcreds.get("email", "")
        if email and email not in existing_qwen_emails:
            token = qcreds.get("access_token", "")
            resource_url = qcreds.get("resource_url", "")
            upstream = f"https://{resource_url}/v1/chat/completions" if resource_url else ""
            store.add_account(
                name=f"Qwen ({email})",
                api_key=token,
                provider="qwen",
                qwen_email=email,
                upstream_url=upstream,
                resource_url=resource_url,
            )
            print(f"  ✅ Auto-imported Qwen account: {email}")

    # Start background auto-refresh tasks
    _auto_refresh = iflow_auth.IFlowAutoRefresh(store, interval_seconds=300)
    _auto_refresh.start()

    _qwen_auto_refresh = qwen_auth.QwenAutoRefresh(store, interval_seconds=300)
    _qwen_auto_refresh.start()

    _proxy_checker_task = asyncio.create_task(_proxy_health_loop(interval=600))

    # Reset stale reg status from previous run
    if store.get_reg_status().get("running"):
        store.set_reg_status(False)
        logger.info("Reset stale reg status from previous run")

    yield

    _auto_refresh.stop()
    _qwen_auto_refresh.stop()
    _proxy_checker_task.cancel()
    await _http_client.aclose()
    for pc in _proxy_clients.values():
        await pc.aclose()


app = FastAPI(title="iFlow Proxy for Claude Code", version="1.0.0", lifespan=lifespan)

STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

PROXY_PORT = store.get_settings().get("port", 8083)


# ============================================================
# ADMIN AUTH MIDDLEWARE
# ============================================================

_ADMIN_PATHS = ("/admin", "/api/", "/debug/")

@app.middleware("http")
async def admin_auth_middleware(request: Request, call_next):
    path = request.url.path
    # Only protect admin and API routes
    if not any(path == p.rstrip("/") or path.startswith(p) for p in _ADMIN_PATHS):
        return await call_next(request)

    pw = store.get_admin_password()
    if not pw:
        # No password set — still enforce CSRF for mutating API requests
        if _should_check_csrf(request):
            if not _csrf_valid(request):
                return JSONResponse({"detail": "CSRF token missing or invalid"}, status_code=403)
        return await call_next(request)

    # Check Authorization header (Basic auth) — hash and compare
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth[6:]).decode("utf-8")
            _, _, provided = decoded.partition(":")
            if store.verify_admin_password(provided):
                if _should_check_csrf(request) and not _csrf_valid(request):
                    return JSONResponse({"detail": "CSRF token missing or invalid"}, status_code=403)
                return await call_next(request)
        except Exception:
            pass

    # Check session cookie — validate token against in-memory store
    _purge_expired_sessions()
    token = request.cookies.get("admin_session", "")
    if token:
        expiry = _admin_sessions.get(token, 0)
        if expiry > time.time():
            if _should_check_csrf(request) and not _csrf_valid(request):
                return JSONResponse({"detail": "CSRF token missing or invalid"}, status_code=403)
            return await call_next(request)
        elif token in _admin_sessions:
            del _admin_sessions[token]  # expired, clean up

    # Return 401 with WWW-Authenticate to trigger browser login dialog
    return Response(
        content="Unauthorized",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="iFlow Admin"'},
    )


def _should_check_csrf(request: Request) -> bool:
    """Return True for mutating API requests that need CSRF protection."""
    return (
        request.method in ("POST", "PUT", "DELETE", "PATCH")
        and request.url.path.startswith("/api/")
    )


def _csrf_valid(request: Request) -> bool:
    """Return True if the request carries a valid, non-expired CSRF token."""
    token = request.headers.get("X-CSRF-Token", "")
    return bool(token) and token in _csrf_tokens_set


# ============================================================
# HELPERS
# ============================================================

def extract_text_from_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            btype = block.get("type", "")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "image":
                parts.append("[Image]")
        return "\n".join(p for p in parts if p)
    return str(content) if content else ""


# ============================================================
# VISION FALLBACK: describe images via a vision model
# ============================================================

async def describe_image_async(image_source: dict, vision_cfg: dict) -> str:
    """Call a vision model to get a text description of an image.

    Handles both base64 and URL image sources. For URL images, downloads
    locally first (since the vision API server may not access external URLs).
    Uses in-memory cache to avoid re-describing the same image.
    """
    import base64 as _b64

    # Check cache first (LRU: move to end on hit)
    ck = _cache_key(image_source)
    if ck and ck in _vision_cache:
        _vision_cache.move_to_end(ck)
        return _vision_cache[ck]

    # Resolve API key and upstream
    if vision_cfg.get("use_qwen_pool"):
        qwen_acc = store.pick_qwen_account_for_vision()
        if qwen_acc:
            api_key = qwen_acc.get("api_key", "")
            upstream = store.get_qwen_upstream(qwen_acc)
            model = vision_cfg.get("qwen_vision_model", "vision-model")
        else:
            return "[Image: no qwen accounts available for vision]"
    else:
        api_key = vision_cfg.get("api_key", "").strip()
        if not api_key:
            accounts = store.get_accounts()
            if accounts:
                api_key = accounts[0].get("api_key", "")
        upstream = vision_cfg.get("upstream_url", "").strip() or store.get_upstream_url()
        model = vision_cfg.get("model", "gpt-5.1")

    raw_key = api_key.removeprefix("Bearer ").strip()
    if not raw_key:
        return "[Image: no API key configured for vision service]"

    prompt = vision_cfg.get("prompt", "Describe this image concisely: list visible text, key objects, layout.")

    # Build image_url from source
    src_type = image_source.get("type", "")
    image_url = None

    if src_type == "base64":
        media_type = image_source.get("media_type", "image/jpeg")
        data = image_source.get("data", "")
        image_url = f"data:{media_type};base64,{data}"

    elif src_type == "url":
        raw_url = image_source.get("url", "")
        if not raw_url:
            return "[Image: empty URL]"

        if raw_url.startswith("data:"):
            image_url = raw_url
        else:
            # Download locally → base64 (vision API server may block external URLs)
            try:
                dl_resp = await _http_client.get(
                    raw_url,
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=15.0,
                    follow_redirects=True,
                )
                if dl_resp.status_code == 200:
                    content_type = dl_resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
                    if not content_type.startswith("image/"):
                        content_type = "image/jpeg"
                    img_b64 = _b64.b64encode(dl_resp.content).decode()
                    image_url = f"data:{content_type};base64,{img_b64}"
                else:
                    return f"[Image: failed to download URL (HTTP {dl_resp.status_code})]"
            except Exception as dl_exc:
                return f"[Image: download error - {str(dl_exc)[:80]}]"
    else:
        return "[Image: unsupported source type]"

    if not image_url:
        return "[Image: empty image data]"

    headers = {
        "accept": "*/*",
        "authorization": f"Bearer {raw_key}",
        "content-type": "application/json",
        "user-agent": "iFlow-Cli",
    }
    if vision_cfg.get("use_qwen_pool"):
        headers["X-Dashscope-Authtype"] = "qwen-oauth"
        headers["User-Agent"] = "QwenCode/0.10.3 (darwin; arm64)"
        headers["X-Dashscope-Useragent"] = "QwenCode/0.10.3 (darwin; arm64)"
    body = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {"type": "text", "text": prompt},
                ],
            }
        ],
        "max_tokens": 300,  # Reduced for faster response
    }

    try:
        resp = await _http_client.post(upstream, headers=headers, json=body, timeout=60.0)
        if resp.status_code == 200:
            rdata = resp.json()
            text = (rdata.get("choices") or [{}])[0].get("message", {}).get("content", "")
            result = text.strip() or "[Image: no description returned]"
        else:
            result = f"[Image: vision service returned HTTP {resp.status_code}: {resp.text[:80]}]"
    except Exception as exc:
        result = f"[Image: vision error - {str(exc)[:80]}]"

    # Store in cache (LRU eviction: popitem(last=False) removes least recently used)
    if ck and not result.startswith("[Image: vision error"):
        if len(_vision_cache) >= _VISION_CACHE_MAX:
            _vision_cache.popitem(last=False)  # remove LRU item
        _vision_cache[ck] = result

    return result


async def process_vision_in_messages(body: dict) -> dict:
    """Replace image content blocks with text descriptions (parallel processing)."""
    vision_cfg = store.get_vision_settings()
    if not vision_cfg.get("enabled"):
        return body

    # First pass: scan for images WITHOUT copying (fast path for text-only requests)
    image_tasks: list[tuple[int, int, dict]] = []
    for mi, msg in enumerate(body.get("messages", [])):
        content = msg.get("content", "")
        if not isinstance(content, list):
            continue
        for bi, block in enumerate(content):
            if block.get("type") == "image":
                image_tasks.append((mi, bi, block.get("source", {})))

    # No images → return immediately, no deepcopy overhead
    if not image_tasks:
        return body

    # Only deepcopy when we actually need to mutate the body
    body = copy.deepcopy(body)

    # Describe all images in parallel — use return_exceptions=True so one failure
    # doesn't abort the entire request
    raw_results = await asyncio.gather(
        *[describe_image_async(source, vision_cfg) for _, _, source in image_tasks],
        return_exceptions=True,
    )
    descriptions = [
        r if isinstance(r, str) else f"[Image: description failed — {str(r)[:60]}]"
        for r in raw_results
    ]

    # Replace image blocks with descriptions (in reverse order to preserve indices)
    desc_map: dict[tuple[int, int], str] = {
        (mi, bi): desc for (mi, bi, _), desc in zip(image_tasks, descriptions)
    }

    for mi, msg in enumerate(body.get("messages", [])):
        content = msg.get("content", "")
        if not isinstance(content, list):
            continue
        new_content = []
        for bi, block in enumerate(content):
            if block.get("type") == "image" and (mi, bi) in desc_map:
                new_content.append({
                    "type": "text",
                    "text": f"[Image description: {desc_map[(mi, bi)]}]",
                })
            else:
                new_content.append(block)
        msg["content"] = new_content

    return body


# ============================================================
# CONVERT: Anthropic Tools → OpenAI Tools
# ============================================================

def anthropic_tools_to_openai(tools: list) -> list:
    result = []
    for tool in tools:
        result.append({
            "type": "function",
            "function": {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {
                    "type": "object",
                    "properties": {},
                }),
            },
        })
    return result


def anthropic_tool_choice_to_openai(tool_choice) -> str | dict:
    if isinstance(tool_choice, str):
        return tool_choice
    if isinstance(tool_choice, dict):
        tc_type = tool_choice.get("type", "auto")
        if tc_type == "auto":
            return "auto"
        elif tc_type == "any":
            return "required"
        elif tc_type == "tool":
            return {"type": "function", "function": {"name": tool_choice.get("name", "")}}
    return "auto"


# ============================================================
# CONVERT: Anthropic Messages → OpenAI Messages
# ============================================================

def apply_global_system_prompt(original: str) -> str:
    global_prompt, mode = store.get_system_prompt()
    if not global_prompt.strip():
        return original
    if mode == "replace":
        return global_prompt
    elif mode == "append":
        return f"{original}\n\n{global_prompt}".strip() if original.strip() else global_prompt
    else:  # prepend
        return f"{global_prompt}\n\n{original}".strip() if original.strip() else global_prompt


def anthropic_messages_to_openai(body: dict) -> list:
    messages = []

    original_system = extract_text_from_content(body.get("system", "")) if "system" in body else ""
    final_system = apply_global_system_prompt(original_system)
    if final_system.strip():
        messages.append({"role": "system", "content": final_system})

    for msg in body.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if isinstance(content, list):
            # User message with tool_result blocks
            tool_results = [b for b in content if b.get("type") == "tool_result"]
            if tool_results and role == "user":
                for tr in tool_results:
                    tool_use_id = tr.get("tool_use_id", f"call_{uuid.uuid4().hex[:8]}")
                    result_content = tr.get("content", "")
                    if isinstance(result_content, list):
                        result_content = "\n".join(
                            b.get("text", "") for b in result_content
                            if b.get("type") == "text"
                        )
                    elif not isinstance(result_content, str):
                        result_content = str(result_content)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_use_id,
                        "content": result_content,
                    })
                text_blocks = [b for b in content if b.get("type") == "text"]
                if text_blocks:
                    text = "\n".join(b.get("text", "") for b in text_blocks)
                    if text.strip():
                        messages.insert(len(messages) - len(tool_results), {
                            "role": "user", "content": text
                        })
                continue

            # Assistant message with tool_use blocks
            tool_uses = [b for b in content if b.get("type") == "tool_use"]
            if tool_uses and role == "assistant":
                text_blocks = [b for b in content if b.get("type") == "text"]
                text_content = "\n".join(b.get("text", "") for b in text_blocks) or None

                tool_calls = []
                for tu in tool_uses:
                    tool_calls.append({
                        "id": tu.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                        "type": "function",
                        "function": {
                            "name": tu.get("name", ""),
                            "arguments": json.dumps(tu.get("input", {}), ensure_ascii=False),
                        },
                    })

                oai_msg = {"role": "assistant", "tool_calls": tool_calls}
                if text_content:
                    oai_msg["content"] = text_content
                messages.append(oai_msg)
                continue

            # Regular content list
            text = extract_text_from_content(content)
            messages.append({"role": role, "content": text})
        else:
            messages.append({"role": role, "content": content or ""})

    return messages


_QWEN_DUMMY_TOOL = {
    "type": "function",
    "function": {
        "name": "do_not_call_me",
        "description": "Do not call this tool under any circumstances.",
        "parameters": {"type": "object", "properties": {}},
    },
}


def anthropic_to_openai(body: dict, model: str, provider: str = "iflow") -> dict:
    """Convert Anthropic request → OpenAI-compatible request, branching by provider."""
    messages = anthropic_messages_to_openai(body)
    is_stream = body.get("stream", False)
    has_tools = bool(body.get("tools"))

    out: dict = {
        "model": model,
        "messages": messages,
        "stream": is_stream,
        "temperature": body.get("temperature", 1),
        "top_p": body.get("top_p", 0.95),
    }

    if provider == "qwen":
        out["max_tokens"] = body.get("max_tokens", 32000)
        if is_stream:
            out["stream_options"] = {"include_usage": True}
        # Qwen3 streaming fix: inject dummy tool when no tools present
        if is_stream and not has_tools:
            out["tools"] = [_QWEN_DUMMY_TOOL]
            out["tool_choice"] = "none"
        elif has_tools:
            out["tools"] = anthropic_tools_to_openai(body["tools"])
            if "tool_choice" in body:
                out["tool_choice"] = anthropic_tool_choice_to_openai(body["tool_choice"])
    else:
        # iFlow path
        out["chat_template_kwargs"] = {
            "enable_thinking": store.get_enable_thinking(),
        }
        if "max_tokens" in body:
            out["max_new_tokens"] = body["max_tokens"]
        else:
            out["max_new_tokens"] = 32000
        if has_tools:
            out["tools"] = anthropic_tools_to_openai(body["tools"])
        if "tool_choice" in body:
            out["tool_choice"] = anthropic_tool_choice_to_openai(body["tool_choice"])

    return out


# Backward compat alias
anthropic_to_iflow = anthropic_to_openai


# ============================================================
# CONVERT: OpenAI Response → Anthropic Response
# ============================================================

def openai_to_anthropic(openai_resp: dict, model: str) -> dict:
    choice = openai_resp.get("choices", [{}])[0]
    message = choice.get("message", {})
    finish_reason = choice.get("finish_reason", "stop")
    usage = openai_resp.get("usage", {})

    content = []

    # Some models return reasoning_content instead of (or alongside) content
    reasoning = message.get("reasoning_content") or ""
    text = message.get("content") or ""
    combined = text or reasoning
    if combined:
        content.append({"type": "text", "text": combined})

    tool_calls = message.get("tool_calls") or []
    for tc in tool_calls:
        func = tc.get("function", {})
        try:
            input_data = json.loads(func.get("arguments", "{}"))
        except (json.JSONDecodeError, TypeError):
            input_data = {"_raw": func.get("arguments", "")}

        content.append({
            "type": "tool_use",
            "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:24]}"),
            "name": func.get("name", ""),
            "input": input_data,
        })

    if finish_reason == "tool_calls":
        stop_reason = "tool_use"
    else:
        stop_reason = "end_turn"

    if not content:
        content = [{"type": "text", "text": ""}]

    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": model,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


# ============================================================
# STREAMING: OpenAI SSE → Anthropic SSE
# ============================================================

async def stream_anthropic_sse(
    openai_lines,
    model: str,
    msg_id: str,
    usage_out: dict | None = None,
) -> AsyncGenerator[str, None]:

    def sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    yield sse("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id, "type": "message", "role": "assistant",
            "content": [], "model": model,
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 1},
        },
    })

    text_block_started = False
    text_block_index = 0
    output_tokens = 0
    stop_reason = "end_turn"

    tool_blocks: dict[int, dict] = {}
    tool_block_anthropic_index: dict[int, int] = {}
    next_anthropic_index = 0

    async for raw_line in openai_lines:
        line = raw_line.strip()
        if not line or not line.startswith("data:"):
            continue
        # Handle both "data: " (OpenAI) and "data:" (iFlow) formats
        data_str = line[5:].lstrip()
        if data_str == "[DONE]":
            break

        try:
            chunk = json.loads(data_str)
        except json.JSONDecodeError:
            continue

        # Capture usage from final chunk (when stream_options.include_usage is true)
        chunk_usage = chunk.get("usage")
        if chunk_usage and usage_out is not None:
            usage_out["prompt_tokens"] = chunk_usage.get("prompt_tokens", 0)
            usage_out["completion_tokens"] = chunk_usage.get("completion_tokens", 0)

        choice = chunk.get("choices", [{}])[0] if chunk.get("choices") else {}
        delta = choice.get("delta", {})
        finish = choice.get("finish_reason")

        if finish == "tool_calls":
            stop_reason = "tool_use"
        elif finish == "stop":
            stop_reason = "end_turn"

        # Text content (some models use reasoning_content instead of content)
        text_content = delta.get("content") or delta.get("reasoning_content") or ""
        if text_content:
            if not text_block_started:
                text_block_started = True
                text_block_index = next_anthropic_index
                next_anthropic_index += 1
                yield sse("content_block_start", {
                    "type": "content_block_start",
                    "index": text_block_index,
                    "content_block": {"type": "text", "text": ""},
                })
                yield sse("ping", {"type": "ping"})

            output_tokens += 1
            yield sse("content_block_delta", {
                "type": "content_block_delta",
                "index": text_block_index,
                "delta": {"type": "text_delta", "text": text_content},
            })

        # Tool calls
        for tc_delta in (delta.get("tool_calls") or []):
            tc_index = tc_delta.get("index", 0)

            if tc_index not in tool_blocks:
                if text_block_started:
                    yield sse("content_block_stop", {
                        "type": "content_block_stop",
                        "index": text_block_index,
                    })
                    text_block_started = False

                anthropic_idx = next_anthropic_index
                next_anthropic_index += 1
                tool_block_anthropic_index[tc_index] = anthropic_idx

                tc_id = tc_delta.get("id", f"toolu_{uuid.uuid4().hex[:24]}")
                tc_name = (tc_delta.get("function") or {}).get("name", "")

                tool_blocks[tc_index] = {"id": tc_id, "name": tc_name, "args": ""}

                yield sse("content_block_start", {
                    "type": "content_block_start",
                    "index": anthropic_idx,
                    "content_block": {
                        "type": "tool_use",
                        "id": tc_id,
                        "name": tc_name,
                        "input": {},
                    },
                })

            args_chunk = (tc_delta.get("function") or {}).get("arguments", "")
            if args_chunk:
                tool_blocks[tc_index]["args"] += args_chunk
                anthropic_idx = tool_block_anthropic_index[tc_index]
                yield sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": anthropic_idx,
                    "delta": {"type": "input_json_delta", "partial_json": args_chunk},
                })

    # Close all open blocks
    if text_block_started:
        yield sse("content_block_stop", {
            "type": "content_block_stop",
            "index": text_block_index,
        })

    for tc_index, anthropic_idx in tool_block_anthropic_index.items():
        yield sse("content_block_stop", {
            "type": "content_block_stop",
            "index": anthropic_idx,
        })

    if not text_block_started and not tool_block_anthropic_index:
        yield sse("content_block_start", {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        })
        yield sse("ping", {"type": "ping"})
        yield sse("content_block_stop", {"type": "content_block_stop", "index": 0})

    yield sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": max(output_tokens, 1)},
    })

    yield sse("message_stop", {"type": "message_stop"})


# ============================================================
# ROUTES
# ============================================================

@app.get("/")
async def root():
    settings = store.get_settings()
    return {
        "service": "iFlow Proxy for Claude Code",
        "version": "1.0.0",
        "admin": "/admin",
        "upstream": settings.get("upstream_url"),
        "model": store.get_default_model(),
        "accounts": len(store.get_accounts()),
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "upstream": store.get_upstream_url(),
        "accounts": len(store.get_accounts()),
    }


def _estimate_tokens(body: dict) -> int:
    """Estimate token count for an Anthropic-format request body.

    Heuristics:
    - Text content: ~3.5 chars/token (better than 4 for English/code)
    - Tool definitions: name + description + parameters JSON
    - base64 image blocks: ~1600 tokens (fixed cost)
    - URL image blocks: ~800 tokens (fixed cost)
    """
    CHARS_PER_TOKEN = 3.5
    total = 0

    def _text_tokens(text: str) -> int:
        return max(1, int(len(text) / CHARS_PER_TOKEN))

    def _content_tokens(content) -> int:
        if isinstance(content, str):
            return _text_tokens(content)
        if isinstance(content, list):
            t = 0
            for block in content:
                btype = block.get("type", "")
                if btype == "text":
                    t += _text_tokens(block.get("text", ""))
                elif btype == "image":
                    src = block.get("source", {})
                    if src.get("type") == "base64":
                        t += 1600
                    else:
                        t += 800
                elif btype == "tool_result":
                    t += _content_tokens(block.get("content", ""))
                elif btype == "tool_use":
                    t += _text_tokens(block.get("name", ""))
                    t += _text_tokens(json.dumps(block.get("input", {})))
            return t
        return _text_tokens(str(content)) if content else 0

    # System prompt
    if "system" in body:
        total += _content_tokens(body["system"])

    # Messages
    for msg in body.get("messages", []):
        total += _content_tokens(msg.get("content", ""))

    # Tool definitions
    for tool in body.get("tools", []):
        total += _text_tokens(tool.get("name", ""))
        total += _text_tokens(tool.get("description", ""))
        params = tool.get("input_schema") or tool.get("parameters", {})
        total += _text_tokens(json.dumps(params))

    return max(1, total)


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    return JSONResponse({"input_tokens": _estimate_tokens(body)})


@app.get("/v1/models")
async def list_models():
    now = int(time.time())
    md = store.get_models()
    return {"object": "list", "data": [
        {"id": m, "object": "model", "created": now, "owned_by": "iflow"}
        for m in md["models"]
    ]}


MAX_RETRIES = 3  # max upstream attempts (initial + retries)


def _is_retryable(status: int) -> bool:
    """5xx and 429 are worth retrying; 4xx auth/client errors are not."""
    return status == 429 or status >= 500


async def _try_refresh_qwen(acc: dict) -> dict | None:
    """Force-refresh Qwen token on 401, return updated headers or None.
    Auto-disables account if refresh fails."""
    email = acc.get("qwen_email", "")
    if not email:
        store.disable_account(acc["id"])
        logger.warning(f"Auto-disabled account '{acc['name']}': no email for refresh")
        return None
    try:
        creds = qwen_auth.read_qwen_creds(email)
        rt = creds.get("refresh_token")
        if not rt:
            store.disable_account(acc["id"])
            logger.warning(f"Auto-disabled account '{acc['name']}': no refresh_token")
            return None
        new_tok = await qwen_auth.refresh_qwen_token(rt)
        new_token = new_tok["access_token"]
        creds["access_token"] = new_token
        creds["refresh_token"] = new_tok.get("refresh_token", rt)
        creds["expiry_date"] = int(time.time() * 1000) + new_tok.get("expires_in", 21600) * 1000
        if "resource_url" in new_tok:
            creds["resource_url"] = new_tok["resource_url"]
        qwen_auth.write_qwen_creds(email, creds)
        store.update_account(acc["id"], api_key=new_token)
        acc["api_key"] = new_token
        logger.info(f"Force-refreshed Qwen token for {email} after 401")
        return store.build_headers(acc)
    except Exception as e:
        logger.error(f"Force-refresh failed for {email}: {e}")
        store.disable_account(acc["id"])
        logger.warning(f"Auto-disabled account '{acc['name']}': refresh failed")
        return None


def _build_openai_request(body: dict, model: str, provider: str) -> dict:
    """Build the OpenAI-format request body for the given account provider."""
    return anthropic_to_openai(body, model, provider=provider)


def _parse_openai_response(data: dict, model: str) -> dict:
    """Convert an OpenAI-format response dict to Anthropic format."""
    return openai_to_anthropic(data, model)


@app.post("/v1/messages")
async def messages(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    is_stream = body.get("stream", False)
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"

    account = store.pick_account()
    if not account:
        raise HTTPException(
            status_code=503,
            detail="No active accounts. Add an API key at /admin"
        )

    # Vision fallback: replace image blocks with text descriptions
    body = await process_vision_in_messages(body)

    t0 = time.time()
    preview = ""
    for m in body.get("messages", []):
        c = m.get("content", "")
        if isinstance(c, str) and c.strip():
            preview = c[:80]
            break

    if is_stream:
        async def gen():
            nonlocal account
            _usage = {}
            attempt = 0
            while attempt < MAX_RETRIES:
                attempt += 1
                cur_account = account if attempt == 1 else store.pick_account()
                if not cur_account:
                    yield f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'type': 'api_error', 'message': 'No active accounts'}})}\n\n"
                    return
                cur_provider = cur_account.get("provider", "iflow")
                cur_upstream = store.get_qwen_upstream(cur_account) if cur_provider == "qwen" else (cur_account.get("upstream_url") or store.get_upstream_url())
                cur_headers = store.build_headers(cur_account)
                cur_id = cur_account["id"]
                cur_name = cur_account["name"]
                cur_proxy = cur_account.get("proxy", "").strip()
                cur_model = (cur_account.get("qwen_model") or store.get_qwen_default_model()) if cur_provider == "qwen" else store.get_default_model()
                cur_body = _build_openai_request(body, cur_model, cur_provider)

                if cur_proxy and cur_proxy not in _proxy_clients:
                    _proxy_clients[cur_proxy] = httpx.AsyncClient(
                        proxy=cur_proxy,
                        timeout=httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0),
                        http2=False,
                    )
                http = _proxy_clients[cur_proxy] if cur_proxy else _http_client
                try:
                    async with http.stream("POST", cur_upstream, headers=cur_headers, json=cur_body) as resp:
                        store.inc_account_request(cur_id)
                        if resp.status_code == 401 and cur_provider == "qwen":
                            err = (await resp.aread()).decode(errors="replace")
                            new_headers = await _try_refresh_qwen(cur_account)
                            if new_headers:
                                async with http.stream("POST", cur_upstream, headers=new_headers, json=cur_body) as resp2:
                                    if resp2.status_code != 200:
                                        err2 = (await resp2.aread()).decode(errors="replace")
                                        store.finalize_request(cur_id, cur_name, cur_model, "error", f"HTTP {resp2.status_code}: {err2[:80]}", int((time.time() - t0) * 1000), is_error=True)
                                        yield f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'type': 'api_error', 'message': err2}})}\n\n"
                                        return
                                    async for chunk in stream_anthropic_sse(resp2.aiter_lines(), cur_model, msg_id, _usage):
                                        yield chunk
                                store.finalize_request(cur_id, cur_name, cur_model, "success", preview, int((time.time() - t0) * 1000), _usage.get("prompt_tokens", 0), _usage.get("completion_tokens", 0))
                                return
                            store.finalize_request(cur_id, cur_name, cur_model, "error", f"HTTP 401: {err[:80]}", int((time.time() - t0) * 1000), is_error=True)
                            yield f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'type': 'api_error', 'message': err}})}\n\n"
                            return
                        if resp.status_code == 401 and cur_provider != "qwen":
                            err = (await resp.aread()).decode(errors="replace")
                            store.disable_account(cur_id)
                            store.finalize_request(cur_id, cur_name, cur_model, "error", f"HTTP 401 (auto-disabled): {err[:60]}", int((time.time() - t0) * 1000), is_error=True)
                            yield f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'type': 'api_error', 'message': err}})}\n\n"
                            return
                        if resp.status_code != 200:
                            err = (await resp.aread()).decode(errors="replace")
                            store.finalize_request(cur_id, cur_name, cur_model, "error", f"HTTP {resp.status_code}: {err[:80]}", int((time.time() - t0) * 1000), is_error=True)
                            if _is_retryable(resp.status_code) and attempt < MAX_RETRIES:
                                logger.warning(f"Retrying after HTTP {resp.status_code} (attempt {attempt}/{MAX_RETRIES})")
                                await asyncio.sleep(1)
                                continue
                            yield f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'type': 'api_error', 'message': err}})}\n\n"
                            return
                        async for chunk in stream_anthropic_sse(resp.aiter_lines(), cur_model, msg_id, _usage):
                            yield chunk
                    store.finalize_request(cur_id, cur_name, cur_model, "success", preview, int((time.time() - t0) * 1000), _usage.get("prompt_tokens", 0), _usage.get("completion_tokens", 0))
                    return
                except httpx.ReadTimeout:
                    store.finalize_request(cur_id, cur_name, cur_model, "error", "Timeout", int((time.time() - t0) * 1000), is_error=True)
                    if attempt < MAX_RETRIES:
                        logger.warning(f"Retrying after timeout (attempt {attempt}/{MAX_RETRIES})")
                        await asyncio.sleep(1)
                        continue
                    yield f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'type': 'timeout', 'message': 'Upstream timeout'}})}\n\n"
                    return
                except Exception as exc:
                    if _is_proxy_error(exc) and cur_proxy:
                        store.reassign_account_proxy(cur_id)
                        store.finalize_request(cur_id, cur_name, cur_model, "error", f"Proxy dead, reassigned: {str(exc)[:60]}", int((time.time() - t0) * 1000), is_error=True)
                    else:
                        store.finalize_request(cur_id, cur_name, cur_model, "error", str(exc)[:80], int((time.time() - t0) * 1000), is_error=True)
                    if attempt < MAX_RETRIES:
                        logger.warning(f"Retrying after error: {exc} (attempt {attempt}/{MAX_RETRIES})")
                        await asyncio.sleep(1)
                        continue
                    yield f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'type': 'proxy_error', 'message': str(exc)}})}\n\n"
                    return

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    # Non-streaming — retry loop
    for attempt in range(1, MAX_RETRIES + 1):
        cur_account = account if attempt == 1 else store.pick_account()
        if not cur_account:
            raise HTTPException(status_code=503, detail="No active accounts")
        cur_provider = cur_account.get("provider", "iflow")
        cur_upstream = store.get_qwen_upstream(cur_account) if cur_provider == "qwen" else (cur_account.get("upstream_url") or store.get_upstream_url())
        cur_headers = store.build_headers(cur_account)
        cur_id = cur_account["id"]
        cur_name = cur_account["name"]
        cur_proxy = cur_account.get("proxy", "").strip()
        cur_model = (cur_account.get("qwen_model") or store.get_qwen_default_model()) if cur_provider == "qwen" else store.get_default_model()
        cur_body = _build_openai_request(body, cur_model, cur_provider)

        if cur_proxy and cur_proxy not in _proxy_clients:
            _proxy_clients[cur_proxy] = httpx.AsyncClient(
                proxy=cur_proxy,
                timeout=httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0),
                http2=False,
            )
        http = _proxy_clients[cur_proxy] if cur_proxy else _http_client
        try:
            resp = await http.post(cur_upstream, headers=cur_headers, json=cur_body)
            store.inc_account_request(cur_id)
            if resp.status_code == 401:
                if cur_provider == "qwen":
                    new_headers = await _try_refresh_qwen(cur_account)
                    if new_headers:
                        resp = await http.post(cur_upstream, headers=new_headers, json=cur_body)
                    else:
                        err_body = resp.text
                        store.finalize_request(cur_id, cur_name, cur_model, "error", f"HTTP 401: token refresh failed", int((time.time() - t0) * 1000), is_error=True)
                        raise HTTPException(status_code=401, detail=f"Qwen token refresh failed: {err_body}")
                else:
                    err_body = resp.text
                    store.disable_account(cur_id)
                    store.finalize_request(cur_id, cur_name, cur_model, "error", f"HTTP 401 (auto-disabled): {err_body[:60]}", int((time.time() - t0) * 1000), is_error=True)
                    raise HTTPException(status_code=401, detail=f"Unauthorized: {err_body}")
        except httpx.ReadTimeout:
            store.finalize_request(cur_id, cur_name, cur_model, "error", "Timeout", int((time.time() - t0) * 1000), is_error=True)
            if attempt < MAX_RETRIES:
                logger.warning(f"Retrying after timeout (attempt {attempt}/{MAX_RETRIES})")
                await asyncio.sleep(1)
                continue
            raise HTTPException(status_code=504, detail="Upstream timeout")
        except Exception as exc:
            if _is_proxy_error(exc) and cur_proxy:
                store.reassign_account_proxy(cur_id)
                store.finalize_request(cur_id, cur_name, cur_model, "error", f"Proxy dead, reassigned: {str(exc)[:60]}", int((time.time() - t0) * 1000), is_error=True)
            else:
                store.finalize_request(cur_id, cur_name, cur_model, "error", str(exc)[:80], int((time.time() - t0) * 1000), is_error=True)
            if attempt < MAX_RETRIES:
                logger.warning(f"Retrying after error: {exc} (attempt {attempt}/{MAX_RETRIES})")
                await asyncio.sleep(1)
                continue
            raise HTTPException(status_code=502, detail=f"Proxy error: {exc}")

        if resp.status_code != 200:
            store.finalize_request(cur_id, cur_name, cur_model, "error", f"HTTP {resp.status_code}", int((time.time() - t0) * 1000), is_error=True)
            if _is_retryable(resp.status_code) and attempt < MAX_RETRIES:
                logger.warning(f"Retrying after HTTP {resp.status_code} (attempt {attempt}/{MAX_RETRIES})")
                await asyncio.sleep(1)
                continue
            raise HTTPException(status_code=resp.status_code, detail=f"iFlow error: {resp.text}")

        try:
            openai_resp = resp.json()
            anthropic_resp = _parse_openai_response(openai_resp, cur_model)
            usage = openai_resp.get("usage", {})
            store.finalize_request(cur_id, cur_name, cur_model, "success", preview, int((time.time() - t0) * 1000), usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
            return JSONResponse(content=anthropic_resp)
        except Exception as exc:
            store.finalize_request(cur_id, cur_name, cur_model, "error", f"Parse error: {str(exc)[:60]}", int((time.time() - t0) * 1000), is_error=True)
            raise HTTPException(status_code=502, detail=f"Parse error: {exc}")

    raise HTTPException(status_code=502, detail="All retry attempts failed")


# ============================================================
# DEBUG
# ============================================================

@app.post("/debug/raw")
async def debug_raw(request: Request):
    """Call iFlow directly and return raw response for debugging."""
    body = await request.json()
    account = store.pick_account()
    if not account:
        raise HTTPException(503, "No accounts")
    headers = store.build_headers(account)
    upstream_url = account.get("upstream_url") or store.get_upstream_url()
    resp = await _http_client.post(upstream_url, headers=headers, json=body)
    return JSONResponse({"status": resp.status_code, "headers": dict(resp.headers), "body": resp.json()})


# ============================================================
# ADMIN PAGE
# ============================================================

@app.get("/admin")
async def admin_page():
    html_path = STATIC_DIR / "admin.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Admin page not found</h1>", status_code=404)


# ============================================================
# ADMIN API

@app.get("/api/csrf-token")
async def api_csrf_token():
    """Issue a CSRF token for the current session. Clients must include this
    as X-CSRF-Token header on all POST/PUT/DELETE requests to /api/*."""
    now = time.time()
    token = uuid.uuid4().hex
    _csrf_tokens.append((token, now + _CSRF_TTL))
    _csrf_tokens_set.add(token)
    # Purge expired tokens from the front (oldest first)
    while _csrf_tokens and _csrf_tokens[0][1] <= now:
        expired_tok, _ = _csrf_tokens.popleft()
        _csrf_tokens_set.discard(expired_tok)
    # Also cap by count to bound memory
    while len(_csrf_tokens) > 1000:
        oldest_tok, _ = _csrf_tokens.popleft()
        _csrf_tokens_set.discard(oldest_tok)
    return JSONResponse({"csrf_token": token})
# ============================================================

@app.get("/api/accounts")
async def api_get_accounts():
    return JSONResponse(store.get_accounts())


@app.post("/api/accounts")
async def api_add_account(request: Request):
    d = await request.json()
    if not d.get("api_key"):
        raise HTTPException(400, "api_key required")
    acc = store.add_account(
        name=d.get("name", "Account"),
        api_key=d["api_key"],
        upstream_url=d.get("upstream_url", ""),
        proxy=d.get("proxy", ""),
        provider=d.get("provider", "iflow"),
        qwen_email=d.get("qwen_email", ""),
    )
    return JSONResponse(acc, status_code=201)


@app.put("/api/accounts/{aid}")
async def api_update_account(aid: str, request: Request):
    d = await request.json()
    acc = store.update_account(aid, **d)
    if not acc:
        raise HTTPException(404, "Account not found")
    return JSONResponse(acc)


@app.delete("/api/accounts/{aid}")
async def api_delete_account(aid: str):
    if store.delete_account(aid):
        return JSONResponse({"ok": True})
    raise HTTPException(404, "Account not found")


@app.post("/api/accounts/{aid}/toggle")
async def api_toggle_account(aid: str):
    acc = store.toggle_account(aid)
    if not acc:
        raise HTTPException(404, "Account not found")
    return JSONResponse(acc)


@app.post("/api/accounts/{aid}/check")
async def api_check_account(aid: str):
    acc = store.get_account(aid)
    if not acc:
        raise HTTPException(404, "Account not found")

    provider = acc.get("provider", "iflow")
    headers = store.build_headers(acc)

    if provider == "qwen":
        acc_upstream = store.get_qwen_upstream(acc)
        payload = {
            "model": store.get_qwen_default_model(),
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 5,
            "stream": False,
        }
    else:
        acc_upstream = acc.get("upstream_url") or store.get_upstream_url()
        payload = {
            "model": store.get_default_model(),
            "messages": [{"role": "user", "content": "hi"}],
            "max_new_tokens": 5,
            "chat_template_kwargs": {"enable_thinking": False},
        }

    acc_proxy = acc.get("proxy", "").strip()
    client = None
    try:
        if acc_proxy:
            client = httpx.AsyncClient(
                proxy=acc_proxy,
                timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
                http2=False,
            )
        http = client if client else _http_client
        resp = await http.post(acc_upstream, headers=headers, json=payload)
        if resp.status_code == 200:
            return {"status": "ok", "message": f"Account working (HTTP {resp.status_code})"}
        return {"status": "error", "message": f"HTTP {resp.status_code}: {resp.text[:100]}"}
    except Exception as e:
        return {"status": "error", "message": str(e)[:100]}
    finally:
        if client:
            await client.aclose()


@app.get("/api/models")
async def api_get_models():
    return JSONResponse(store.get_models())


@app.post("/api/models")
async def api_add_model(request: Request):
    d = await request.json()
    m = d.get("model", "").strip()
    if not m:
        raise HTTPException(400, "model required")
    if store.add_model(m):
        return JSONResponse({"ok": True}, status_code=201)
    raise HTTPException(409, "Model already exists")


@app.delete("/api/models/{model:path}")
async def api_delete_model(model: str):
    if store.delete_model(model):
        return JSONResponse({"ok": True})
    raise HTTPException(404, "Model not found")


@app.put("/api/models/default")
async def api_set_default_model(request: Request):
    d = await request.json()
    if store.set_default_model(d.get("model", "")):
        return JSONResponse({"ok": True})
    raise HTTPException(404, "Model not in list")


@app.get("/api/qwen-models")
async def api_get_qwen_models():
    return JSONResponse({"models": store.get_qwen_models(), "default": store.get_qwen_default_model()})


@app.post("/api/qwen-models")
async def api_add_qwen_model(request: Request):
    d = await request.json()
    m = d.get("model", "").strip()
    if not m:
        raise HTTPException(400, "model required")
    if store.add_qwen_model(m):
        return JSONResponse({"ok": True}, status_code=201)
    raise HTTPException(409, "Model already exists")


@app.delete("/api/qwen-models/{model:path}")
async def api_delete_qwen_model(model: str):
    if store.delete_qwen_model(model):
        return JSONResponse({"ok": True})
    raise HTTPException(404, "Model not found")


@app.get("/api/provider")
async def api_get_provider():
    return JSONResponse({
        "active_provider": store.get_active_provider(),
        "qwen_default_model": store.get_qwen_default_model(),
    })


@app.put("/api/provider")
async def api_set_provider(request: Request):
    d = await request.json()
    prov = d.get("active_provider", "").strip()
    if prov and prov in ("iflow", "qwen"):
        store.set_active_provider(prov)
    qm = d.get("qwen_default_model", "").strip()
    if qm:
        store.set_qwen_default_model(qm)
    return JSONResponse({"ok": True})


@app.get("/api/stats")
async def api_stats():
    return JSONResponse(store.get_stats())


@app.post("/api/accounts/{aid}/reset-tokens")
async def api_reset_account_tokens(aid: str):
    if store.reset_account_tokens(aid):
        return JSONResponse({"ok": True})
    raise HTTPException(404, "Account not found")


@app.post("/api/accounts/{aid}/refresh-token")
async def api_refresh_account_token(aid: str):
    """Force-refresh iFlow OAuth token for a specific account."""
    acc = store.get_account(aid)
    if not acc:
        raise HTTPException(404, "Account not found")
    if acc.get("provider", "iflow") != "iflow":
        raise HTTPException(400, "Only iFlow accounts support token refresh")
    rt = acc.get("refresh_token", "")
    if not rt:
        raise HTTPException(400, "No refresh_token stored for this account")
    try:
        new_tok = await iflow_auth.refresh_access_token(rt)
        new_access = new_tok["access_token"]
        new_refresh = new_tok.get("refresh_token", rt)
        new_expiry = int(time.time() * 1000) + new_tok.get("expires_in", 172800) * 1000
        user_info = await iflow_auth.get_user_info(new_access)
        new_api_key = user_info.get("apiKey") or acc.get("api_key")
        store.update_account(aid, api_key=new_api_key, access_token=new_access,
                             refresh_token=new_refresh, expiry_date=new_expiry)
        return JSONResponse({"ok": True, "expires_in": new_tok.get("expires_in")})
    except Exception as e:
        raise HTTPException(502, f"Token refresh failed: {e}")


@app.post("/api/tokens/reset")
async def api_reset_all_tokens():
    store.reset_all_tokens()
    return JSONResponse({"ok": True})


@app.get("/api/logs")
async def api_logs():
    return JSONResponse(store.get_logs())


@app.delete("/api/logs")
async def api_clear_logs():
    store.clear_logs()
    return JSONResponse({"ok": True})


@app.get("/api/settings")
async def api_get_settings():
    return JSONResponse(store.get_settings())


@app.put("/api/settings")
async def api_update_settings(request: Request):
    d = await request.json()
    s = store.update_settings(**d)
    return JSONResponse(s)


@app.get("/api/admin/password")
async def api_get_admin_password():
    pw = store.get_admin_password()
    return JSONResponse({"has_password": bool(pw)})


@app.put("/api/admin/password")
async def api_set_admin_password(request: Request):
    d = await request.json()
    new_pw = d.get("password", "").strip()
    store.set_admin_password(new_pw)
    resp = JSONResponse({"ok": True})
    if new_pw:
        # Issue a random session token — never store the password in the cookie
        token = uuid.uuid4().hex
        _admin_sessions[token] = time.time() + _SESSION_TTL
        _purge_expired_sessions()
        resp.set_cookie("admin_session", token, httponly=True, samesite="strict", max_age=int(_SESSION_TTL))
    else:
        # Clear all sessions when password is removed
        _admin_sessions.clear()
        resp.delete_cookie("admin_session")
    return resp


# ============================================================
# AUTH API (iFlow OAuth auto-refresh)
# ============================================================

@app.get("/api/auth/status")
async def api_auth_status():
    """Show current iFlow OAuth credentials status."""
    import time as _time
    creds = iflow_auth.read_creds()
    if not creds:
        return JSONResponse({"status": "no_creds", "message": "~/.iflow/oauth_creds.json not found"})
    expiry = creds.get("expiry_date", 0)
    remaining_s = (expiry - int(_time.time() * 1000)) / 1000
    return JSONResponse({
        "status": "ok",
        "userId": creds.get("userId"),
        "userName": creds.get("userName"),
        "email": creds.get("email"),
        "apiKey": creds.get("apiKey", "")[:12] + "..." if creds.get("apiKey") else None,
        "expiry_date": expiry,
        "expires_in_seconds": int(remaining_s),
        "is_expiring": iflow_auth.is_token_expiring(creds),
    })


@app.post("/api/auth/refresh")
async def api_auth_refresh():
    """Manually trigger token refresh and update all accounts."""
    try:
        api_key = await iflow_auth.ensure_valid_api_key()
        if not api_key:
            raise HTTPException(400, "No credentials found. Run 'iflow auth login' first.")

        # Update all accounts
        accounts = store.get_accounts()
        updated = 0
        for acc in accounts:
            if acc.get("api_key") != api_key:
                store.update_account(acc["id"], api_key=api_key)
                updated += 1

        return JSONResponse({
            "ok": True,
            "apiKey": api_key[:12] + "...",
            "accounts_updated": updated,
        })
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/auth/import")
async def api_auth_import():
    """Import account from ~/.iflow/oauth_creds.json."""
    creds = iflow_auth.read_creds()
    if not creds or not creds.get("apiKey"):
        raise HTTPException(400, "No valid credentials in ~/.iflow/oauth_creds.json")

    # Check if account already exists with same apiKey
    existing = [a for a in store.get_accounts() if a.get("api_key") == creds["apiKey"]]
    if existing:
        return JSONResponse({"ok": True, "message": "Account already exists", "account": existing[0]})

    acc = store.add_account(
        name=creds.get("userName", "iFlow Account"),
        api_key=creds["apiKey"],
    )
    return JSONResponse({"ok": True, "message": "Account imported", "account": acc}, status_code=201)


# ============================================================
# QWEN AUTH API
# ============================================================

_qwen_device_flows: dict[str, dict] = {}


@app.post("/api/auth/qwen/device")
async def api_qwen_device():
    """Initiate Qwen OAuth device flow. Returns user_code and verification_uri."""
    try:
        flow = await qwen_auth.start_device_flow()
        session_id = uuid.uuid4().hex
        _qwen_device_flows[session_id] = {
            "device_code": flow["device_code"],
            "verifier": flow["verifier"],
            "interval": flow.get("interval", 5),
            "created_at": time.time(),
            "expires_in": flow.get("expires_in", 300),
        }
        # Cleanup expired sessions
        now = time.time()
        expired = [k for k, v in _qwen_device_flows.items()
                   if now - v["created_at"] > v.get("expires_in", 300) + 60]
        for k in expired:
            del _qwen_device_flows[k]
        return JSONResponse({
            "session_id": session_id,
            "user_code": flow.get("user_code", ""),
            "verification_uri": flow.get("verification_uri_complete") or flow.get("verification_uri", "https://chat.qwen.ai"),
            "expires_in": flow.get("expires_in", 300),
        })
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/auth/qwen/poll")
async def api_qwen_poll(request: Request):
    """Poll for token after user completes device authorization."""
    d = await request.json()
    session_id = d.get("session_id", "")
    email = d.get("email", "").strip()

    flow = _qwen_device_flows.get(session_id)
    if not flow:
        raise HTTPException(400, "Unknown session_id")
    if not email:
        raise HTTPException(400, "email required")

    try:
        token = await qwen_auth.poll_device_token(
            flow["device_code"], flow["verifier"], flow["interval"],
        )
        expires_in = token.get("expires_in", 3600)
        token["expiry_date"] = int(time.time() * 1000) + expires_in * 1000
        token["email"] = email
        qwen_auth.write_qwen_creds(email, token)

        access_token = token["access_token"]
        resource_url = token.get("resource_url", "")
        upstream = f"https://{resource_url}/v1/chat/completions" if resource_url else ""

        # Update existing or create new account
        existing = [a for a in store.get_accounts()
                    if a.get("provider") == "qwen" and a.get("qwen_email") == email]
        if existing:
            store.update_account(existing[0]["id"], api_key=access_token, upstream_url=upstream)
            acc = store.get_account(existing[0]["id"])
        else:
            acc = store.add_account(
                name=f"Qwen ({email})",
                api_key=access_token,
                provider="qwen",
                qwen_email=email,
                upstream_url=upstream,
            )

        del _qwen_device_flows[session_id]
        return JSONResponse({"ok": True, "account": acc})
    except TimeoutError:
        raise HTTPException(408, "Device flow timed out — user did not authorize in time")
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/auth/qwen/status")
async def api_qwen_status():
    """List all Qwen accounts with token expiry info."""
    accounts = [a for a in store.get_accounts() if a.get("provider") == "qwen"]
    result = []
    for acc in accounts:
        email = acc.get("qwen_email", "")
        creds = qwen_auth.read_qwen_creds(email) if email else {}
        expiry = creds.get("expiry_date", 0)
        remaining_s = (expiry - int(time.time() * 1000)) / 1000 if expiry else 0
        result.append({
            "id": acc["id"],
            "name": acc["name"],
            "email": email,
            "expires_in_seconds": int(remaining_s),
            "is_expiring": qwen_auth.is_qwen_token_expiring(creds) if creds else True,
        })
    return JSONResponse(result)


# ============================================================
# REGISTRATION API
# ============================================================

import subprocess as _subprocess

_reg_process: _subprocess.Popen | None = None
_REG_LOG_FILE = Path(__file__).parent / "reg.log"


@app.get("/api/reg/accounts")
async def api_reg_accounts():
    return JSONResponse(store.get_reg_accounts())


@app.post("/api/reg/accounts")
async def api_add_reg_accounts(request: Request):
    d = await request.json()
    text = d.get("text", "")
    if not text.strip():
        raise HTTPException(400, "No accounts provided")
    added = store.add_reg_accounts(text)
    return JSONResponse({"ok": True, "added": added})


@app.delete("/api/reg/accounts")
async def api_clear_reg_accounts():
    store.clear_reg_accounts()
    return JSONResponse({"ok": True})


@app.get("/api/reg/results")
async def api_reg_results():
    return JSONResponse(store.get_reg_results())


@app.delete("/api/reg/results")
async def api_clear_reg_results():
    store.clear_reg_results()
    return JSONResponse({"ok": True})


@app.get("/api/reg/status")
async def api_reg_status():
    global _reg_process
    status = store.get_reg_status()
    # Check if process is still running
    if status.get("running") and _reg_process is not None:
        if _reg_process.poll() is not None:
            store.set_reg_status(False)
            _reg_process = None
            status["running"] = False
    return JSONResponse(status)


@app.post("/api/reg/start")
async def api_reg_start(request: Request):
    global _reg_process
    d = await request.json()
    use_proxy = d.get("use_proxy", False)
    workers = d.get("workers", 1)
    # headless: use value from request if explicitly provided, else fall back to stored setting
    headless = d.get("headless", store.get_reg_status().get("headless", True))

    # Check if already running
    status = store.get_reg_status()
    if status.get("running") and _reg_process and _reg_process.poll() is None:
        raise HTTPException(409, "Registration already running")

    accounts = store.get_reg_accounts()
    if not accounts:
        raise HTTPException(400, "No accounts to register")

    # Write accounts to file for reg_iflow.py
    acc_file = Path(__file__).parent / "accounts.txt"
    acc_file.write_text(
        "\n".join(f"{a['email']}|{a['password']}" for a in accounts),
        encoding="utf-8",
    )

    # Write proxy config to file for reg_iflow.py
    proxies = store.get_reg_proxies() if use_proxy else []
    proxy_file = Path(__file__).parent / "reg_proxies.json"
    proxy_file.write_text(json.dumps(proxies, ensure_ascii=False), encoding="utf-8")

    # Launch reg_iflow.py as subprocess
    cmd = [sys.executable, str(Path(__file__).parent / "reg_iflow.py")]
    if use_proxy and proxies:
        cmd.append("--proxy")
    if headless:
        cmd.append("--headless")
    cmd.extend(["--workers", str(workers)])

    # Clear previous log
    try:
        _REG_LOG_FILE.write_text("", encoding="utf-8")
    except Exception:
        pass
    _reg_log_fh = open(_REG_LOG_FILE, "w", encoding="utf-8", buffering=1)
    _reg_process = _subprocess.Popen(
        cmd,
        cwd=str(Path(__file__).parent),
        stdout=_reg_log_fh,
        stderr=_subprocess.STDOUT,
    )

    store.set_reg_status(True, use_proxy, headless)
    return JSONResponse({"ok": True, "pid": _reg_process.pid})


@app.post("/api/reg/stop")
async def api_reg_stop():
    global _reg_process
    if _reg_process and _reg_process.poll() is None:
        _reg_process.terminate()
        _reg_process = None
    store.set_reg_status(False)
    return JSONResponse({"ok": True})


@app.put("/api/reg/settings")
async def api_reg_settings(request: Request):
    """Persist registration settings (e.g. headless) to data.json."""
    d = await request.json()
    headless = d.get("headless")
    if headless is not None:
        store.set_reg_status(
            running=store.get_reg_status().get("running", False),
            use_proxy=store.get_reg_status().get("use_proxy", False),
            headless=bool(headless),
        )
    return JSONResponse({"ok": True})


@app.get("/api/reg/log-stream")
async def api_reg_log_stream():
    """SSE stream of registration subprocess stdout."""
    async def generate():
        while True:
            proc = _reg_process
            if proc is None or proc.stdout is None:
                yield "data: [no process running]\n\n"
                return
            try:
                line = await asyncio.get_running_loop().run_in_executor(
                    None, proc.stdout.readline
                )
                if line:
                    text = line.decode("utf-8", errors="replace").rstrip()
                    yield f"data: {text}\n\n"
                else:
                    # Process ended
                    yield "data: [process finished]\n\n"
                    return
            except Exception as e:
                yield f"data: [error: {e}]\n\n"
                return

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/reg/log-poll")
async def api_reg_log_poll(offset: int = 0):
    """Return reg log lines from offset, reading from reg.log file."""
    try:
        lines = _REG_LOG_FILE.read_text(encoding="utf-8").splitlines() if _REG_LOG_FILE.exists() else []
    except Exception:
        lines = []
    return JSONResponse({"lines": lines[offset:], "total": len(lines)})


# ── Registration Proxy Management ──

@app.get("/api/reg/proxies")
async def api_reg_proxies():
    return JSONResponse(store.get_reg_proxies())


@app.post("/api/reg/proxies")
async def api_add_reg_proxy(request: Request):
    d = await request.json()
    host = d.get("host", "").strip()
    port = d.get("port", "").strip()
    username = d.get("username", "").strip()
    password = d.get("password", "").strip()
    rotate_url = d.get("rotate_url", "").strip()
    if not host or not port:
        raise HTTPException(400, "host and port required")
    proxy = store.add_reg_proxy(host, port, username, password, rotate_url)
    return JSONResponse(proxy, status_code=201)


@app.post("/api/reg/proxies/import")
async def api_import_reg_proxies(request: Request):
    """Import proxies from text: ip:port:user:pass|rotate_url one per line."""
    d = await request.json()
    text = d.get("text", "").strip()
    if not text:
        raise HTTPException(400, "No proxy text provided")
    added = 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Split rotate_url by pipe delimiter
        rotate_url = ""
        if "|" in line:
            line, rotate_url = line.rsplit("|", 1)
            rotate_url = rotate_url.strip()
        parts = line.split(":")
        if len(parts) >= 4:
            store.add_reg_proxy(parts[0], parts[1], parts[2], ":".join(parts[3:]), rotate_url)
            added += 1
        elif len(parts) == 2:
            store.add_reg_proxy(parts[0], parts[1], "", "", rotate_url)
            added += 1
    return JSONResponse({"ok": True, "added": added})


@app.delete("/api/reg/proxies/{proxy_id}")
async def api_delete_reg_proxy(proxy_id: str):
    if store.delete_reg_proxy(proxy_id):
        return JSONResponse({"ok": True})
    raise HTTPException(404, "Proxy not found")


@app.delete("/api/reg/proxies")
async def api_clear_reg_proxies():
    store.clear_reg_proxies()
    return JSONResponse({"ok": True})


# ── SOCKS5 Proxy Pool (for accounts) ──

@app.get("/api/pool/proxies")
async def api_pool_proxies():
    return JSONResponse(store.get_proxy_pool())


@app.get("/api/pool/settings")
async def api_pool_settings():
    return JSONResponse(store.get_proxy_pool_settings())


@app.put("/api/pool/settings")
async def api_pool_settings_update(request: Request):
    d = await request.json()
    store.set_proxy_pool_max(int(d.get("max_per_proxy", 1)))
    return JSONResponse(store.get_proxy_pool_settings())


@app.post("/api/pool/proxies/import")
async def api_pool_import(request: Request):
    d = await request.json()
    text = d.get("text", "").strip()
    if not text:
        raise HTTPException(400, "No proxy text provided")
    added = store.add_pool_proxies(text)
    return JSONResponse({"ok": True, "added": added})


@app.delete("/api/pool/proxies/{proxy_id}")
async def api_pool_delete(proxy_id: str):
    if store.delete_pool_proxy(proxy_id):
        return JSONResponse({"ok": True})
    raise HTTPException(404, "Proxy not found")


@app.delete("/api/pool/proxies")
async def api_pool_clear():
    store.clear_proxy_pool()
    return JSONResponse({"ok": True})


@app.post("/api/pool/proxies/{proxy_id}/check")
async def api_pool_check_proxy(proxy_id: str):
    """Check if a proxy is alive. If dead, auto-reassign affected accounts to another proxy."""
    pool = store.get_proxy_pool()
    p = next((x for x in pool if x["id"] == proxy_id), None)
    if not p:
        raise HTTPException(404, "Proxy not found")

    proxy_str = store._build_proxy_str(p)
    alive = await _check_proxy_alive(proxy_str)

    reassigned = 0
    if not alive:
        # Find all accounts using this proxy and reassign them
        accounts = store.get_accounts()
        for acc in accounts:
            if acc.get("proxy", "").strip() == proxy_str:
                updated = store.reassign_account_proxy(acc["id"])
                if updated:
                    reassigned += 1

    return JSONResponse({"alive": alive, "proxy_id": proxy_id, "reassigned": reassigned})


@app.post("/api/accounts/{aid}/reassign-proxy")
async def api_reassign_proxy(aid: str, request: Request):
    """Reassign proxy for a single account. Optionally pass {proxy: "socks5://..."}."""
    d = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    proxy_str = d.get("proxy", "")
    acc = store.reassign_account_proxy(aid, proxy_str)
    if not acc:
        raise HTTPException(404, "Account not found")
    return JSONResponse(acc)


@app.post("/api/pool/reassign-all")
async def api_reassign_all():
    """Reassign proxies from pool to all accounts."""
    updated = store.reassign_all_proxies()
    return JSONResponse({"ok": True, "updated": updated})

if __name__ == "__main__":
    import uvicorn

    settings = store.get_settings()
    port = settings.get("port", 8083)

    print("=" * 55)
    print("  iFlow Proxy for Claude Code v1.0")
    print("=" * 55)
    print(f"  Proxy    : http://localhost:{port}")
    print(f"  Upstream : {store.get_upstream_url()}")
    print(f"  Model    : {store.get_default_model()}")
    print(f"  Accounts : {len(store.get_accounts())}")
    print(f"  Admin    : http://localhost:{port}/admin")
    print("=" * 55)
    print()
    print("  Claude Code setup (PowerShell):")
    print(f'  $env:ANTHROPIC_BASE_URL="http://localhost:{port}"')
    print(f'  $env:ANTHROPIC_API_KEY="dummy"')
    print("  claude")
    print("=" * 55)

    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
