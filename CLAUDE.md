# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

iFlow Proxy — a Python/FastAPI reverse proxy that translates Anthropic's Messages API format to iFlow's OpenAI-compatible API format, enabling Claude Code to use iFlow-hosted models (GLM, DeepSeek, etc.) as a backend.

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

Three Python modules, one static HTML file:

**proxy.py** — FastAPI app, the main entry point. Handles:
- `POST /v1/messages` — core proxy route: receives Anthropic-format requests, converts to OpenAI format via `anthropic_to_iflow()`, forwards to iFlow upstream, converts response back via `openai_to_anthropic()` (non-streaming) or `stream_anthropic_sse()` (streaming SSE)
- `GET /v1/models`, `POST /v1/messages/count_tokens` — compatibility endpoints
- `/admin` + `/api/*` — admin REST API for managing accounts, models, settings, logs
- `/api/auth/*` — OAuth credential management (status, refresh, import)
- `/api/reg/*` — bulk registration management: accounts, proxies, start/stop subprocess, results
- `/api/pool/*` — SOCKS5 proxy pool for account-level proxying (import, delete, settings)
- Vision fallback: `process_vision_in_messages()` replaces image blocks with text descriptions from a vision model before forwarding, since iFlow models may not support vision natively. Uses in-memory LRU cache (`_vision_cache`, max 200). Images are processed in parallel via `asyncio.gather`.
- Per-account SOCKS5 proxy support via separate httpx clients (HTTP/2 disabled for SOCKS5)

**store.py** — Thread-safe JSON file store backed by `data.json`. Manages:
- Multiple accounts with load balancing (round-robin via module-level `_rr_index`, least-requests, random)
- Model list and default model selection
- Settings (upstream URL, port, system prompt injection with prepend/append/replace modes, vision config, thinking toggle)
- Request/error stats and capped request logs (200 entries)
- `build_headers()` generates HMAC-SHA256 signatures (`x-iflow-signature`, `x-iflow-timestamp`) for iFlow API auth
- Registration subsystem: manages bulk registration accounts, results, proxies, and status
- SOCKS5 proxy pool: `pick_pool_proxy()` assigns proxies to new accounts (respects `max_per_proxy`), `auto_add_account_with_proxy()` combines account creation with proxy assignment

**iflow_auth.py** — OAuth token lifecycle. Reads `~/.iflow/oauth_creds.json`, auto-refreshes access tokens via iFlow's OAuth endpoint (`https://iflow.cn/oauth/token`), fetches new apiKey via `getUserInfo`, and runs a background task (`IFlowAutoRefresh`) every 5 minutes to keep all accounts updated.

**reg_iflow.py** — Playwright-based bulk account registration. Automates Google Sign-In → iFlow → Create API Key. Reads `accounts.txt` (email|password per line), supports concurrent workers (`--workers N`) and rotating HTTP proxies (`--proxy` with `reg_proxies.json`). Successfully registered accounts are auto-added to the proxy store via `store.auto_add_account_with_proxy()` with SOCKS5 proxies from the pool. Can be launched from the admin UI via `/api/reg/start` (runs as a subprocess).

**static/admin.html** — Single-page admin UI served at `/admin`.

## Key Data Flow

1. Claude Code sends Anthropic Messages API request → `POST /v1/messages`
2. `store.pick_account()` selects an account via load balancing
3. Vision fallback processes any image blocks in parallel (if enabled)
4. `anthropic_to_iflow()` converts request format (tools, messages, system prompt injection)
5. Request forwarded to iFlow upstream with HMAC-signed headers
6. Response converted back: `openai_to_anthropic()` for non-streaming, `stream_anthropic_sse()` for SSE streaming
7. Stats and logs recorded in `data.json`

## Format Translation Notes

- Anthropic `tool_use`/`tool_result` blocks ↔ OpenAI `tool_calls`/`tool` messages
- Anthropic `tool_choice.type: "any"` → OpenAI `"required"`
- `max_tokens` → `max_new_tokens` (iFlow parameter name)
- `reasoning_content` from models (e.g., DeepSeek) is treated as regular content in the response
- System prompt injection applies `prepend`/`append`/`replace` modes on top of the original system message

## Configuration

All runtime config lives in `data.json` (auto-created on first run with defaults from `store.DEFAULT_DATA`). The `DATA_FILE` env var can override its location. Settings are managed through the admin API or admin UI.

Two levels of proxy support:
- **Global proxy** (`settings.proxy_url`): used by the main httpx client for all outbound requests
- **Per-account SOCKS5 proxy** (`account.proxy`): creates a separate httpx client per request (HTTP/2 disabled for SOCKS5)
- **Proxy pool** (`proxy_pool`): SOCKS5 proxies auto-assigned to new accounts during registration, controlled by `max_per_proxy`
