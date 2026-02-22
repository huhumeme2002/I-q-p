# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

iFlow Proxy — a Python/FastAPI reverse proxy that translates Anthropic's Messages API format to iFlow's and Qwen's OpenAI-compatible API formats, enabling Claude Code to use iFlow-hosted models (GLM, DeepSeek, etc.) or Qwen models as a backend.

## Running

```bash
# Windows launcher (installs deps on first run, creates .deps_installed marker)
start.bat

# Or directly
python proxy.py
```

Starts on port 8083 (configurable in data.json). Admin panel at `http://localhost:8083/admin`.

```powershell
# Point Claude Code at the proxy
$env:ANTHROPIC_BASE_URL="http://localhost:8083"
$env:ANTHROPIC_API_KEY="dummy"
claude
```

## Dependencies

```bash
pip install -r requirements.txt
```

Core deps: FastAPI, uvicorn, httpx (with HTTP/2), python-dotenv. Python 3.10+ required (uses `X | Y` union types and `str.removeprefix`).

## Testing

No test framework. Manual test scripts run against a live proxy instance:

```bash
python test_proxy.py     # Tests SOCKS5 proxies + models against iFlow API directly
python test_vision.py    # Tests vision fallback with base64 image
python test_vision2.py   # Tests vision fallback with URL image
python setup_vision.py   # Configures vision settings via admin API, runs a test, then disables
```

Debug endpoint for raw iFlow responses: `POST /debug/raw` (sends body directly to iFlow, returns raw response).

## Architecture

Five Python modules, one static HTML file:

**proxy.py** — FastAPI app, the main entry point. Handles:
- `POST /v1/messages` — core proxy route: receives Anthropic-format requests, converts to OpenAI format via `anthropic_to_openai()` (alias: `anthropic_to_iflow`), forwards to upstream, converts response back via `openai_to_anthropic()` (non-streaming) or `stream_anthropic_sse()` (streaming SSE)
- `GET /v1/models`, `POST /v1/messages/count_tokens` — compatibility endpoints. Token counting uses `_estimate_tokens()`: 3.5 chars/token for text, 1600 tokens for base64 images, 800 for URL images, separate accounting for tool definitions.
- `/admin` + `/api/*` — admin REST API for managing accounts, models, settings, logs
- `/api/auth/*` — iFlow OAuth credential management (status, refresh, import)
- `/api/auth/qwen/*` — Qwen OAuth device flow (initiate, poll)
- `/api/reg/*` — bulk registration management: accounts, proxies, start/stop subprocess, results, settings
- `/api/pool/*` — SOCKS5 proxy pool for account-level proxying (import, delete, settings)
- `GET /api/csrf-token` — issues CSRF tokens; all mutating `/api/*` requests require `X-CSRF-Token` header
- Vision fallback: `process_vision_in_messages()` replaces image blocks with text descriptions from a vision model before forwarding. Uses in-memory LRU cache (`_vision_cache`, max 200). Images processed in parallel via `asyncio.gather`.
- Per-account SOCKS5 proxy support via separate httpx clients (HTTP/2 disabled for SOCKS5)
- Module-level helpers extracted from `messages()`: `_try_refresh_qwen()`, `_build_openai_request()`, `_parse_openai_response()`

**store.py** — Thread-safe JSON file store backed by `data.json`. Manages:
- Multiple accounts with load balancing (round-robin via module-level `_rr_index`, least-requests, random). Accounts have a `pair_id` field linking iflow+qwen accounts sharing the same email — used by `delete_account()` to correctly release proxy pool slots.
- Model list and default model selection (separate lists for iflow and qwen models)
- Settings (upstream URL, port, system prompt injection with prepend/append/replace modes, vision config, thinking toggle)
- Request/error stats and capped request logs (200 entries). `finalize_request()` batches token update + error count + log in one read+write.
- `build_headers()` generates HMAC-SHA256 signatures (`x-iflow-signature`, `x-iflow-timestamp`) for iFlow API auth; Qwen accounts get static `X-Dashscope-*` headers instead.
- Registration subsystem: manages bulk registration accounts, results, proxies, and status (including `headless` setting persisted via `set_reg_status()`)
- SOCKS5 proxy pool: `pick_pool_proxy()` assigns proxies to new accounts (respects `max_per_proxy`), `auto_add_account_with_proxy()` combines account creation with proxy assignment and `pair_id` generation

**iflow_auth.py** — iFlow OAuth token lifecycle. Reads `~/.iflow/oauth_creds.json`, auto-refreshes access tokens via iFlow's OAuth endpoint (`https://iflow.cn/oauth/token`), fetches new apiKey via `getUserInfo`, and runs a background task (`IFlowAutoRefresh`) every 5 minutes to keep all accounts updated.

**qwen_auth.py** — Qwen OAuth Device Flow (RFC 8628 + PKCE). Credentials stored per-email at `~/.cli-proxy-api/qwen-{email}.json`. Provides `start_device_flow()`, `poll_device_token()`, `refresh_qwen_token()`, and `QwenAutoRefresh` background task (5-minute interval). Qwen accounts use `resource_url` from the token response to build the upstream URL.

**reg_iflow.py** — Playwright-based bulk account registration. Automates Google Sign-In → iFlow → Create API Key. Reads `accounts.txt` (email|password per line), supports concurrent workers (`--workers N`) and rotating HTTP proxies (`--proxy` with `reg_proxies.json`). Successfully registered accounts are auto-added via `store.auto_add_account_with_proxy()` with SOCKS5 proxies from the pool. Launched from admin UI via `/api/reg/start` (runs as a subprocess).

**static/admin.html** — Single-page admin UI served at `/admin`. All API calls go through `apiFetch()` wrapper which auto-injects `X-CSRF-Token`. CSRF token is fetched at page load via `_initCsrf()`.

## Key Data Flow

1. Claude Code sends Anthropic Messages API request → `POST /v1/messages`
2. `store.pick_account()` selects an account via load balancing (respects `active_provider`: iflow or qwen)
3. Vision fallback processes any image blocks in parallel (if enabled)
4. `_build_openai_request()` → `anthropic_to_openai()` converts request format (tools, messages, system prompt injection, provider-specific params)
5. Request forwarded to upstream with signed headers; on Qwen 401, `_try_refresh_qwen()` force-refreshes the token and retries
6. Response converted back: `_parse_openai_response()` → `openai_to_anthropic()` for non-streaming, `stream_anthropic_sse()` for SSE streaming
7. `store.finalize_request()` records stats and logs in one atomic write

## Format Translation Notes

- Anthropic `tool_use`/`tool_result` blocks ↔ OpenAI `tool_calls`/`tool` messages
- Anthropic `tool_choice.type: "any"` → OpenAI `"required"`
- iFlow: `max_tokens` → `max_new_tokens`; `chat_template_kwargs.enable_thinking` controls thinking mode
- Qwen streaming: a dummy tool is injected when no tools are present (Qwen3 streaming fix)
- `reasoning_content` from models (e.g., DeepSeek) is treated as regular content in the response
- System prompt injection applies `prepend`/`append`/`replace` modes on top of the original system message

## Configuration

All runtime config lives in `data.json` (auto-created on first run with defaults from `store.DEFAULT_DATA`). The `DATA_FILE` env var can override its location. Settings are managed through the admin API or admin UI.

Two levels of proxy support:
- **Global proxy** (`settings.proxy_url`): used by the main httpx client for all outbound requests
- **Per-account SOCKS5 proxy** (`account.proxy`): creates a separate httpx client per request (HTTP/2 disabled for SOCKS5)
- **Proxy pool** (`proxy_pool`): SOCKS5 proxies auto-assigned to new accounts during registration, controlled by `max_per_proxy`

## Security Notes

- Admin auth: session token in `admin_session` cookie (random UUID, never the password). Sessions stored in `_admin_sessions: dict[str, float]` (in-memory, 24h TTL). Password stored as SHA-256 hash in `data.json`; env var `ADMIN_PASSWORD` takes precedence as plain text.
- CSRF: `_csrf_tokens: set[str]` (in-memory). `GET /api/csrf-token` issues tokens; middleware enforces `X-CSRF-Token` on all `POST/PUT/DELETE` to `/api/*`.
- Account `pair_id`: shared between iflow+qwen accounts with the same email, used to correctly track proxy pool slot usage on deletion.
