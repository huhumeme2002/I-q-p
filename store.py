"""
Data management layer for iFlow Proxy.
Manages accounts, models, settings, stats, logs via data.json.
"""
import os
import json
import uuid
import hmac
import hashlib
import random
import logging
import threading
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)

DATA_FILE = Path(os.environ.get("DATA_FILE", str(Path(__file__).parent / "data.json")))
MAX_LOGS = 200

_lock = threading.Lock()

# ============================================================
# DEFAULT DATA
# ============================================================
DEFAULT_DATA = {
    "accounts": [],
    "models": [
        "glm-4.7",
        "glm-4-plus",
        "glm-4-air",
        "glm-4-flash",
        "qwen3-coder-plus",
        "qwen3-coder-flash",
        "coder-model",
        "vision-model",
    ],
    "default_model": "glm-4.7",
    "qwen_default_model": "qwen3-coder-plus",
    "active_provider": "iflow",
    "settings": {
        "upstream_url": "https://apis.iflow.cn/v1/chat/completions",
        "load_balance_strategy": "round_robin",
        "port": 8083,
        "system_prompt": "",
        "system_prompt_mode": "prepend",
        "enable_thinking": False,
        "vision": {
            "enabled": False,
            "upstream_url": "",
            "api_key": "",
            "model": "glm-4v-plus",
            "prompt": "Please describe this image in detail, including all visible text, objects, colors, and context. If there is any text in the image, transcribe it verbatim.",
            "use_qwen_pool": False,
            "qwen_vision_model": "vision-model",
        },
        "auto_continue": {
            "enabled": False,
            "token_threshold": 80,
            "max_retries": 3,
            "only_with_tools": True,
            "message": "You stopped without using any tools. Continue and complete the task by actually calling the appropriate tools. Do not just describe what you would do - do it.",
        },
        "context_window": {
            "enabled": False,
            "max_tokens": 60000,
            "keep_recent_messages": 20,
            "truncation_notice": "[Context truncated: {removed_count} earlier messages were removed to fit the model's context window. The conversation continues from the most recent messages below.]",
        },
    },
    "stats": {"total_requests": 0, "total_errors": 0, "start_time": None},
    "logs": [],
    "reg_accounts": [],
    "reg_results": [],
    "reg_status": {"running": False, "use_proxy": False, "headless": True},
    "reg_proxies": [],
    "proxy_pool": [],
    "proxy_pool_settings": {"max_per_proxy": 1},
}


def _read() -> dict:
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return json.loads(json.dumps(DEFAULT_DATA))
    except Exception as exc:
        logger.warning("data.json is corrupt or unreadable (%s); returning defaults", exc)
        return json.loads(json.dumps(DEFAULT_DATA))


def _write(data: dict):
    # NOTE: Callers must hold _lock before calling _write to ensure atomicity.
    # Write to a temp file then atomically replace to avoid corruption on crash.
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, DATA_FILE)


def init_data():
    """Ensure data.json exists with all required keys."""
    with _lock:
        if not DATA_FILE.exists():
            DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
            _write(DEFAULT_DATA)
        else:
            d = _read()
            changed = False
            for k, v in DEFAULT_DATA.items():
                if k not in d:
                    d[k] = v
                    changed = True
                elif k == "settings" and isinstance(v, dict):
                    for sk, sv in v.items():
                        if sk not in d["settings"]:
                            d["settings"][sk] = sv
                            changed = True
            if changed:
                _write(d)


# ============================================================
# ACCOUNTS
# ============================================================

def get_accounts() -> list:
    return _read().get("accounts", [])


def get_account(account_id: str) -> dict | None:
    for a in get_accounts():
        if a["id"] == account_id:
            return a
    return None


def add_account(name: str, api_key: str, upstream_url: str = "", proxy: str = "",
                provider: str = "iflow", qwen_email: str = "",
                pair_id: str = "", resource_url: str = "") -> dict:
    with _lock:
        d = _read()
        if not upstream_url:
            if provider == "qwen":
                upstream_url = "https://portal.qwen.ai/v1/chat/completions"
            else:
                upstream_url = d.get("settings", {}).get("upstream_url", DEFAULT_DATA["settings"]["upstream_url"])
        acc = {
            "id": uuid.uuid4().hex[:12],
            "name": name,
            "api_key": api_key,
            "provider": provider,
            "qwen_email": qwen_email,
            "pair_id": pair_id,
            "upstream_url": upstream_url,
            "resource_url": resource_url,
            "proxy": proxy.strip(),
            "enabled": True,
            "request_count": 0,
            "error_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "last_used": None,
            "created_at": datetime.utcnow().isoformat(),
        }
        d["accounts"].append(acc)
        _write(d)
        return acc


def update_account(account_id: str, **kwargs) -> dict | None:
    with _lock:
        d = _read()
        for a in d["accounts"]:
            if a["id"] == account_id:
                for k, v in kwargs.items():
                    if k in ("name", "api_key", "upstream_url", "proxy", "provider", "qwen_email", "qwen_model",
                             "refresh_token", "access_token", "expiry_date", "pair_id"):
                        a[k] = v if k != "proxy" else str(v).strip()
                _write(d)
                return a
        return None


def delete_account(account_id: str) -> bool:
    with _lock:
        d = _read()
        acc = next((a for a in d["accounts"] if a["id"] == account_id), None)
        if not acc:
            return False

        proxy = acc.get("proxy", "").strip()
        provider = acc.get("provider", "iflow")
        qwen_email = acc.get("qwen_email", "")

        d["accounts"] = [a for a in d["accounts"] if a["id"] != account_id]

        # Release proxy pool slot if applicable
        if proxy:
            acc_pair_id = acc.get("pair_id", "")
            if acc_pair_id:
                # Prefer pair_id matching (new accounts)
                paired_still_exists = any(
                    a.get("proxy") == proxy and a.get("pair_id") == acc_pair_id
                    for a in d["accounts"]
                )
            else:
                # Fallback: legacy name matching for old accounts without pair_id
                paired_still_exists = any(
                    a.get("proxy") == proxy and a.get("provider") != provider
                    and (
                        (provider == "qwen" and qwen_email and
                         a.get("name") == qwen_email.split("@")[0]) or
                        (provider == "iflow" and
                         a.get("qwen_email", "").split("@")[0] == acc.get("name", ""))
                    )
                    for a in d["accounts"]
                )
            if not paired_still_exists:
                pool = d.get("proxy_pool", [])
                for p in pool:
                    if _build_proxy_str(p) == proxy and p.get("assigned", 0) > 0:
                        p["assigned"] -= 1
                        break

        _write(d)
        return True


def toggle_account(account_id: str) -> dict | None:
    with _lock:
        d = _read()
        for a in d["accounts"]:
            if a["id"] == account_id:
                a["enabled"] = not a["enabled"]
                _write(d)
                return a
        return None


def disable_account(account_id: str) -> dict | None:
    with _lock:
        d = _read()
        for a in d["accounts"]:
            if a["id"] == account_id:
                a["enabled"] = False
                _write(d)
                return a
        return None


def inc_account_request(account_id: str):
    with _lock:
        d = _read()
        for a in d["accounts"]:
            if a["id"] == account_id:
                a["request_count"] = a.get("request_count", 0) + 1
                a["last_used"] = datetime.utcnow().isoformat()
                break
        d["stats"]["total_requests"] = d["stats"].get("total_requests", 0) + 1
        _write(d)


def inc_account_error(account_id: str):
    with _lock:
        d = _read()
        for a in d["accounts"]:
            if a["id"] == account_id:
                a["error_count"] = a.get("error_count", 0) + 1
                break
        d["stats"]["total_errors"] = d["stats"].get("total_errors", 0) + 1
        _write(d)


def inc_account_tokens(account_id: str, input_tokens: int, output_tokens: int):
    with _lock:
        d = _read()
        for a in d["accounts"]:
            if a["id"] == account_id:
                a["input_tokens"] = a.get("input_tokens", 0) + input_tokens
                a["output_tokens"] = a.get("output_tokens", 0) + output_tokens
                break
        d["stats"]["total_input_tokens"] = d["stats"].get("total_input_tokens", 0) + input_tokens
        d["stats"]["total_output_tokens"] = d["stats"].get("total_output_tokens", 0) + output_tokens
        _write(d)


def reset_account_tokens(account_id: str):
    with _lock:
        d = _read()
        for a in d["accounts"]:
            if a["id"] == account_id:
                a["input_tokens"] = 0
                a["output_tokens"] = 0
                _write(d)
                return True
        return False


def reset_all_tokens():
    with _lock:
        d = _read()
        for a in d["accounts"]:
            a["input_tokens"] = 0
            a["output_tokens"] = 0
        d["stats"]["total_input_tokens"] = 0
        d["stats"]["total_output_tokens"] = 0
        _write(d)


# ============================================================
# LOAD BALANCER
# ============================================================

_rr_index = 0


def pick_account() -> dict | None:
    global _rr_index
    with _lock:
        d = _read()
        active_prov = d.get("active_provider", "iflow")
        enabled = [a for a in d.get("accounts", [])
                   if a.get("enabled", True) and a.get("provider", "iflow") == active_prov]
        if not enabled:
            return None

        strategy = d.get("settings", {}).get("load_balance_strategy", "round_robin")

        if strategy == "round_robin":
            _rr_index = _rr_index % len(enabled)
            acc = enabled[_rr_index]
            _rr_index += 1
            return acc
        elif strategy == "least_requests":
            return min(enabled, key=lambda a: a.get("request_count", 0))
        elif strategy == "random":
            return random.choice(enabled)
        else:
            _rr_index = _rr_index % len(enabled)
            acc = enabled[_rr_index]
            _rr_index += 1
            return acc


def _generate_iflow_signature(api_key: str, session_id: str, timestamp: int) -> str | None:
    """Generate iFlow HMAC-SHA256 signature: HMAC-SHA256(apiKey, 'iFlow-Cli:{sessionId}:{timestamp}')"""
    if not api_key:
        return None
    try:
        message = f"iFlow-Cli:{session_id}:{timestamp}"
        return hmac.new(api_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    except Exception:
        return None


_QWEN_HEADERS_STATIC = {
    "User-Agent": "QwenCode/0.10.3 (darwin; arm64)",
    "X-Dashscope-Useragent": "QwenCode/0.10.3 (darwin; arm64)",
    "X-Dashscope-Authtype": "qwen-oauth",
    "X-Dashscope-Cachecontrol": "enable",
    "X-Stainless-Lang": "js",
    "X-Stainless-Arch": "arm64",
    "X-Stainless-Os": "MacOS",
    "X-Stainless-Runtime": "node",
    "X-Stainless-Runtime-Version": "v22.17.0",
    "X-Stainless-Package-Version": "5.11.0",
    "X-Stainless-Retry-Count": "0",
    "Content-Type": "application/json",
}


def build_headers(account: dict) -> dict:
    """Build API headers from account data, branching by provider."""
    provider = account.get("provider", "iflow")
    api_key = account.get("api_key", "")
    raw_key = api_key.removeprefix("Bearer ").strip()

    if provider == "qwen":
        headers = dict(_QWEN_HEADERS_STATIC)
        headers["Authorization"] = f"Bearer {raw_key}"
        return headers

    # iFlow path
    auth = f"Bearer {raw_key}" if raw_key else ""

    timestamp = int(__import__("time").time() * 1000)
    session_id = f"session-{uuid.uuid4()}"
    signature = _generate_iflow_signature(raw_key, session_id, timestamp)

    headers = {
        "accept": "*/*",
        "authorization": auth,
        "content-type": "application/json",
        "user-agent": "iFlow-Cli",
        "session-id": session_id,
        "conversation-id": str(uuid.uuid4()),
    }
    if signature:
        headers["x-iflow-signature"] = signature
        headers["x-iflow-timestamp"] = str(timestamp)
    return headers


def get_qwen_upstream(account: dict) -> str:
    """Return the correct Qwen API URL, respecting resource_url if present."""
    resource_url = account.get("resource_url", "").strip()
    if resource_url:
        return f"https://{resource_url}/v1/chat/completions"
    return account.get("upstream_url") or "https://portal.qwen.ai/v1/chat/completions"


_vision_rr_index = 0


def pick_qwen_account_for_vision() -> dict | None:
    """Pick a qwen account round-robin for vision requests."""
    global _vision_rr_index
    with _lock:
        d = _read()
        accounts = [a for a in d.get("accounts", [])
                    if a.get("enabled", True) and a.get("provider") == "qwen"]
        if not accounts:
            return None
        _vision_rr_index = _vision_rr_index % len(accounts)
        acc = accounts[_vision_rr_index]
        _vision_rr_index += 1
        return acc


# ============================================================
# MODELS
# ============================================================

def get_models() -> dict:
    d = _read()
    return {"models": d.get("models", []), "default_model": d.get("default_model", "")}


def get_default_model() -> str:
    return _read().get("default_model", "glm-4.7")


def get_qwen_default_model() -> str:
    return _read().get("qwen_default_model", "qwen3-coder-plus")


def set_qwen_default_model(model: str):
    with _lock:
        d = _read()
        d["qwen_default_model"] = model
        _write(d)


_DEFAULT_QWEN_MODELS = ["qwen3-coder-plus", "qwen3-coder-flash"]


def get_qwen_models() -> list:
    d = _read()
    return d.get("qwen_models", _DEFAULT_QWEN_MODELS)


def add_qwen_model(model: str) -> bool:
    with _lock:
        d = _read()
        if "qwen_models" not in d:
            d["qwen_models"] = list(_DEFAULT_QWEN_MODELS)
        if model not in d["qwen_models"]:
            d["qwen_models"].append(model)
            _write(d)
            return True
        return False


def delete_qwen_model(model: str) -> bool:
    with _lock:
        d = _read()
        if "qwen_models" not in d:
            return False
        if model in d["qwen_models"]:
            d["qwen_models"].remove(model)
            _write(d)
            return True
        return False


def set_active_provider(provider: str):
    with _lock:
        d = _read()
        d["active_provider"] = provider
        _write(d)


def get_active_provider() -> str:
    return _read().get("active_provider", "iflow")


def add_model(model: str) -> bool:
    with _lock:
        d = _read()
        if model not in d["models"]:
            d["models"].append(model)
            _write(d)
            return True
        return False


def delete_model(model: str) -> bool:
    with _lock:
        d = _read()
        if model in d["models"]:
            d["models"].remove(model)
            if d["default_model"] == model and d["models"]:
                d["default_model"] = d["models"][0]
            _write(d)
            return True
        return False


def set_default_model(model: str) -> bool:
    with _lock:
        d = _read()
        if model in d["models"]:
            d["default_model"] = model
            _write(d)
            return True
        return False


# ============================================================
# SETTINGS
# ============================================================

def get_settings() -> dict:
    return _read().get("settings", DEFAULT_DATA["settings"])


def update_settings(**kwargs) -> dict:
    with _lock:
        d = _read()
        allowed = ("upstream_url", "load_balance_strategy", "port",
                    "system_prompt", "system_prompt_mode", "enable_thinking")
        for k, v in kwargs.items():
            if k in allowed:
                d["settings"][k] = v
            elif k == "vision" and isinstance(v, dict):
                if "vision" not in d["settings"]:
                    d["settings"]["vision"] = {}
                d["settings"]["vision"].update(v)
            elif k == "auto_continue" and isinstance(v, dict):
                if "auto_continue" not in d["settings"]:
                    d["settings"]["auto_continue"] = {}
                d["settings"]["auto_continue"].update(v)
            elif k == "context_window" and isinstance(v, dict):
                if "context_window" not in d["settings"]:
                    d["settings"]["context_window"] = {}
                d["settings"]["context_window"].update(v)
        _write(d)
        return d["settings"]


def _hash_password(password: str) -> str:
    """Return SHA-256 hex digest of password."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def get_admin_password() -> str:
    """Return stored password hash (or empty string = no auth).
    Env var ADMIN_PASSWORD takes precedence and is used as plain text for comparison.
    """
    env_pw = os.environ.get("ADMIN_PASSWORD", "").strip()
    if env_pw:
        return env_pw  # env var: plain text (compared after hashing in verify)
    return _read().get("admin_password", "")


def _is_hashed(value: str) -> bool:
    """Return True if value looks like a SHA-256 hex digest (64 hex chars)."""
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value.lower())


def verify_admin_password(provided: str) -> bool:
    """Return True if provided password matches stored hash (or env var).

    Handles migration: if stored value is plain text (not a SHA-256 hash),
    compares directly and migrates to hash on success.
    """
    stored = get_admin_password()
    if not stored:
        return True  # no password set = open access
    env_pw = os.environ.get("ADMIN_PASSWORD", "").strip()
    if env_pw:
        # env var is plain text — use constant-time comparison
        return hmac.compare_digest(provided, env_pw)
    # data.json: check if stored is already a SHA-256 hash
    if _is_hashed(stored):
        return hmac.compare_digest(_hash_password(provided), stored)
    else:
        # Legacy plain text password — compare and migrate to hash
        if hmac.compare_digest(provided, stored):
            set_admin_password(provided)
            return True
        return False


def set_admin_password(password: str):
    with _lock:
        d = _read()
        pw = password.strip()
        # Store as SHA-256 hash (empty string = no auth)
        d["admin_password"] = _hash_password(pw) if pw else ""
        _write(d)


def get_system_prompt() -> tuple[str, str]:
    s = get_settings()
    return s.get("system_prompt", ""), s.get("system_prompt_mode", "prepend")


def get_upstream_url() -> str:
    return get_settings().get("upstream_url", DEFAULT_DATA["settings"]["upstream_url"])


def get_enable_thinking() -> bool:
    return get_settings().get("enable_thinking", False)


def get_vision_settings() -> dict:
    default = DEFAULT_DATA["settings"]["vision"].copy()
    v = get_settings().get("vision", {})
    default.update(v)
    return default


def get_auto_continue_settings() -> dict:
    default = DEFAULT_DATA["settings"]["auto_continue"].copy()
    ac = get_settings().get("auto_continue", {})
    default.update(ac)
    return default


def get_context_window_settings() -> dict:
    default = DEFAULT_DATA["settings"]["context_window"].copy()
    cw = get_settings().get("context_window", {})
    default.update(cw)
    return default


# ============================================================
# STATS & LOGS
# ============================================================

def get_stats() -> dict:
    return _read().get("stats", {"total_requests": 0, "total_errors": 0})


def get_logs() -> list:
    return _read().get("logs", [])


def add_log(account_name: str, model: str, status: str, preview: str = "", duration: int = 0):
    with _lock:
        d = _read()
        log = {
            "id": uuid.uuid4().hex[:10],
            "timestamp": datetime.utcnow().isoformat(),
            "account_name": account_name,
            "model": model,
            "status": status,
            "preview": preview[:100] if preview else "",
            "duration": duration,
        }
        d["logs"].insert(0, log)
        d["logs"] = d["logs"][:MAX_LOGS]
        _write(d)


def finalize_request(
    account_id: str,
    account_name: str,
    model: str,
    status: str,
    preview: str = "",
    duration: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    is_error: bool = False,
) -> None:
    """Batch update: tokens + error count + log in ONE read+write (replaces 3 separate calls).

    Equivalent to calling:
        inc_account_tokens(account_id, input_tokens, output_tokens)  [if tokens > 0]
        inc_account_error(account_id)                                 [if is_error]
        add_log(account_name, model, status, preview, duration)
    """
    with _lock:
        d = _read()
        # Update account-level stats
        for a in d["accounts"]:
            if a["id"] == account_id:
                if input_tokens or output_tokens:
                    a["input_tokens"] = a.get("input_tokens", 0) + input_tokens
                    a["output_tokens"] = a.get("output_tokens", 0) + output_tokens
                if is_error:
                    a["error_count"] = a.get("error_count", 0) + 1
                break
        # Update global stats
        if input_tokens or output_tokens:
            d["stats"]["total_input_tokens"] = d["stats"].get("total_input_tokens", 0) + input_tokens
            d["stats"]["total_output_tokens"] = d["stats"].get("total_output_tokens", 0) + output_tokens
        if is_error:
            d["stats"]["total_errors"] = d["stats"].get("total_errors", 0) + 1
        # Add log entry
        log = {
            "id": uuid.uuid4().hex[:10],
            "timestamp": datetime.utcnow().isoformat(),
            "account_name": account_name,
            "model": model,
            "status": status,
            "preview": preview[:100] if preview else "",
            "duration": duration,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
        d["logs"].insert(0, log)
        d["logs"] = d["logs"][:MAX_LOGS]
        _write(d)


def clear_logs():
    with _lock:
        d = _read()
        d["logs"] = []
        _write(d)


# ============================================================
# REGISTRATION
# ============================================================

def get_reg_accounts() -> list:
    return _read().get("reg_accounts", [])


def add_reg_accounts(text: str) -> int:
    """Parse email|password lines and add to reg_accounts. Returns count added."""
    with _lock:
        d = _read()
        if "reg_accounts" not in d:
            d["reg_accounts"] = []
        added = 0
        for line in text.strip().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|", 1)
            if len(parts) == 2:
                email, password = parts[0].strip(), parts[1].strip()
                if email and password:
                    d["reg_accounts"].append({"email": email, "password": password})
                    added += 1
        _write(d)
        return added


def clear_reg_accounts():
    with _lock:
        d = _read()
        d["reg_accounts"] = []
        _write(d)


def get_reg_results() -> list:
    return _read().get("reg_results", [])


def add_reg_result(result: dict):
    with _lock:
        d = _read()
        if "reg_results" not in d:
            d["reg_results"] = []
        d["reg_results"].insert(0, result)
        _write(d)


def clear_reg_results():
    with _lock:
        d = _read()
        d["reg_results"] = []
        _write(d)


def get_reg_status() -> dict:
    return _read().get("reg_status", {"running": False, "use_proxy": False, "headless": True})


def set_reg_status(running: bool, use_proxy: bool = False, headless: bool | None = None):
    with _lock:
        d = _read()
        prev = d.get("reg_status", {})
        d["reg_status"] = {
            "running": running,
            "use_proxy": use_proxy,
            "headless": headless if headless is not None else prev.get("headless", True),
        }
        _write(d)


# ============================================================
# REGISTRATION PROXIES
# ============================================================

def get_reg_proxies() -> list:
    return _read().get("reg_proxies", [])


def add_reg_proxy(host: str, port: str, username: str, password: str, rotate_url: str = "") -> dict:
    with _lock:
        d = _read()
        if "reg_proxies" not in d:
            d["reg_proxies"] = []
        proxy = {
            "id": uuid.uuid4().hex[:8],
            "host": host.strip(),
            "port": port.strip(),
            "username": username.strip(),
            "password": password.strip(),
            "rotate_url": rotate_url.strip(),
        }
        d["reg_proxies"].append(proxy)
        _write(d)
        return proxy


def delete_reg_proxy(proxy_id: str) -> bool:
    with _lock:
        d = _read()
        before = len(d.get("reg_proxies", []))
        d["reg_proxies"] = [p for p in d.get("reg_proxies", []) if p["id"] != proxy_id]
        if len(d["reg_proxies"]) < before:
            _write(d)
            return True
        return False


def clear_reg_proxies():
    with _lock:
        d = _read()
        d["reg_proxies"] = []
        _write(d)


# ============================================================
# PROXY POOL (SOCKS5 for accounts)
# ============================================================

def get_proxy_pool() -> list:
    return _read().get("proxy_pool", [])


def get_proxy_pool_settings() -> dict:
    return _read().get("proxy_pool_settings", {"max_per_proxy": 1})


def set_proxy_pool_max(max_per_proxy: int):
    with _lock:
        d = _read()
        if "proxy_pool_settings" not in d:
            d["proxy_pool_settings"] = {}
        d["proxy_pool_settings"]["max_per_proxy"] = max(1, max_per_proxy)
        _write(d)


def _build_proxy_str(p: dict) -> str:
    """Build proxy URL string from pool proxy dict. Supports http and socks5 schemes."""
    scheme = p.get("scheme", "socks5")
    if p.get("username"):
        return f"{scheme}://{p['username']}:{p['password']}@{p['host']}:{p['port']}"
    return f"{scheme}://{p['host']}:{p['port']}"


def add_pool_proxies(text: str) -> int:
    """Parse ip:port:user:pass lines and add to proxy_pool. Returns count added.
    Optionally prefix line with 'http:' or 'socks5:' scheme, e.g. 'http:ip:port:user:pass'.
    """
    with _lock:
        d = _read()
        if "proxy_pool" not in d:
            d["proxy_pool"] = []
        added = 0
        for line in text.strip().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            scheme = "socks5"
            if line.lower().startswith("http:") and not line.lower().startswith("http://"):
                scheme = "http"
                line = line[5:]
            elif line.lower().startswith("socks5:") and not line.lower().startswith("socks5://"):
                scheme = "socks5"
                line = line[7:]
            parts = line.split(":")
            if len(parts) >= 4:
                d["proxy_pool"].append({
                    "id": uuid.uuid4().hex[:8],
                    "scheme": scheme,
                    "host": parts[0],
                    "port": parts[1],
                    "username": parts[2],
                    "password": ":".join(parts[3:]),
                    "assigned": 0,
                })
                added += 1
            elif len(parts) == 2:
                d["proxy_pool"].append({
                    "id": uuid.uuid4().hex[:8],
                    "scheme": scheme,
                    "host": parts[0],
                    "port": parts[1],
                    "username": "",
                    "password": "",
                    "assigned": 0,
                })
                added += 1
        _write(d)
        return added


def delete_pool_proxy(proxy_id: str) -> bool:
    with _lock:
        d = _read()
        p = next((x for x in d.get("proxy_pool", []) if x["id"] == proxy_id), None)
        if not p:
            return False
        proxy_str = _build_proxy_str(p)
        d["proxy_pool"] = [x for x in d["proxy_pool"] if x["id"] != proxy_id]
        for a in d.get("accounts", []):
            if a.get("proxy", "").strip() == proxy_str:
                a["proxy"] = ""
        _write(d)
        return True


def clear_proxy_pool():
    with _lock:
        d = _read()
        proxy_strs = {_build_proxy_str(p) for p in d.get("proxy_pool", [])}
        d["proxy_pool"] = []
        for a in d.get("accounts", []):
            if a.get("proxy", "").strip() in proxy_strs:
                a["proxy"] = ""
        _write(d)


def pick_and_inc_pool_proxy() -> dict | None:
    """Atomically pick a proxy from pool that hasn't reached max_per_proxy and increment its
    assigned count. Returns a copy of the proxy dict (with updated assigned count) or None."""
    with _lock:
        d = _read()
        pool = d.get("proxy_pool", [])
        max_pp = d.get("proxy_pool_settings", {}).get("max_per_proxy", 1)
        for p in pool:
            if p.get("assigned", 0) < max_pp:
                p["assigned"] = p.get("assigned", 0) + 1
                _write(d)
                return dict(p)
        return None


# Keep old names as thin wrappers for any external callers.
def pick_pool_proxy() -> dict | None:
    """Deprecated: use pick_and_inc_pool_proxy() for atomic pick+increment."""
    with _lock:
        d = _read()
        pool = d.get("proxy_pool", [])
        max_pp = d.get("proxy_pool_settings", {}).get("max_per_proxy", 1)
        for p in pool:
            if p.get("assigned", 0) < max_pp:
                return dict(p)
        return None


def inc_pool_proxy_assigned(proxy_id: str):
    with _lock:
        d = _read()
        for p in d.get("proxy_pool", []):
            if p["id"] == proxy_id:
                p["assigned"] = p.get("assigned", 0) + 1
                break
        _write(d)


def auto_add_account_with_proxy(api_key: str, name: str, provider: str = "iflow",
                                qwen_email: str = "", pair_email: str = "") -> dict | None:
    """Add account to main accounts list with a proxy from pool. Returns account or None.

    If pair_email is given, reuse the proxy already assigned to the iflow account with
    that email (iflow + qwen sharing the same email count as one slot, not two).
    Accounts sharing the same email get the same pair_id for reliable pairing.
    """
    proxy_str = ""
    pair_id = ""

    if pair_email:
        # Try to reuse proxy and pair_id from the paired iflow account (same email)
        existing = [a for a in get_accounts()
                    if a.get("provider") == "iflow" and a.get("name") == pair_email.split("@")[0]
                    and a.get("proxy")]
        if existing:
            proxy_str = existing[0]["proxy"]
            pair_id = existing[0].get("pair_id", "")

    if not proxy_str:
        proxy = pick_and_inc_pool_proxy()
        if proxy:
            proxy_str = _build_proxy_str(proxy)

    # Generate a shared pair_id if this account will be paired (has pair_email)
    if pair_email and not pair_id:
        pair_id = uuid.uuid4().hex[:12]
        # Backfill pair_id on the existing iflow account so both share it
        existing_iflow = [a for a in get_accounts()
                          if a.get("provider") == "iflow"
                          and a.get("name") == pair_email.split("@")[0]]
        if existing_iflow:
            update_account(existing_iflow[0]["id"], pair_id=pair_id)

    acc = add_account(name=name, api_key=api_key, proxy=proxy_str, provider=provider,
                      qwen_email=qwen_email, pair_id=pair_id)
    return acc


# ============================================================
# PROXY REASSIGNMENT
# ============================================================

def reassign_account_proxy(account_id: str, proxy_str: str = "") -> dict | None:
    """Reassign proxy for a single account. If proxy_str is empty, pick from pool.
    Also updates the paired account (same pair_id) to share the same proxy slot.
    """
    with _lock:
        d = _read()
        acc = None
        for a in d["accounts"]:
            if a["id"] == account_id:
                acc = a
                break
        if not acc:
            return None

        old_proxy = acc.get("proxy", "").strip()
        if old_proxy:
            for p in d.get("proxy_pool", []):
                if _build_proxy_str(p) == old_proxy and p.get("assigned", 0) > 0:
                    p["assigned"] -= 1
                    break

        if not proxy_str:
            max_pp = d.get("proxy_pool_settings", {}).get("max_per_proxy", 1)
            for p in d.get("proxy_pool", []):
                if p.get("assigned", 0) < max_pp:
                    proxy_str = _build_proxy_str(p)
                    p["assigned"] = p.get("assigned", 0) + 1
                    break

        acc["proxy"] = proxy_str

        # Update paired account — match by pair_id first, fall back to email matching
        pair_id = acc.get("pair_id", "")
        acc_provider = acc.get("provider", "")
        acc_name = acc.get("name", "")
        acc_qwen_email = acc.get("qwen_email", "")

        for a in d["accounts"]:
            if a["id"] == account_id:
                continue
            matched = False
            if pair_id and a.get("pair_id") == pair_id:
                matched = True
            elif not pair_id:
                # Fallback: iflow name matches qwen_email prefix, or vice versa
                a_qwen = a.get("qwen_email", "")
                a_name = a.get("name", "")
                if acc_provider == "iflow" and a.get("provider") == "qwen":
                    if a_qwen and (acc_name == a_qwen.split("@")[0] or acc_name in a_qwen):
                        matched = True
                elif acc_provider == "qwen" and a.get("provider") == "iflow":
                    if acc_qwen_email and (a_name == acc_qwen_email.split("@")[0] or a_name in acc_qwen_email):
                        matched = True
            if matched:
                a["proxy"] = proxy_str

        _write(d)
        return acc


def reassign_all_proxies() -> int:
    """Reassign proxies from pool to all accounts. Returns count updated.
    iFlow + Qwen accounts sharing the same email share one proxy slot.
    """
    with _lock:
        d = _read()
        pool = d.get("proxy_pool", [])
        max_pp = d.get("proxy_pool_settings", {}).get("max_per_proxy", 1)

        for p in pool:
            p["assigned"] = 0
        for a in d["accounts"]:
            a["proxy"] = ""

        updated = 0
        pool_idx = 0
        assigned_by_email: dict[str, str] = {}

        def _next_proxy() -> str:
            nonlocal pool_idx
            while pool_idx < len(pool):
                p = pool[pool_idx]
                if p.get("assigned", 0) < max_pp:
                    proxy_str = _build_proxy_str(p)
                    p["assigned"] = p.get("assigned", 0) + 1
                    return proxy_str
                pool_idx += 1
            return ""

        for a in d["accounts"]:
            qwen_email = a.get("qwen_email", "")
            name = a.get("name", "")
            email_key = qwen_email if qwen_email else name

            if email_key in assigned_by_email:
                a["proxy"] = assigned_by_email[email_key]
                updated += 1
                continue

            proxy_str = _next_proxy()
            if proxy_str:
                a["proxy"] = proxy_str
                assigned_by_email[email_key] = proxy_str
                if qwen_email:
                    assigned_by_email[qwen_email.split("@")[0]] = proxy_str
                else:
                    assigned_by_email[name] = proxy_str
                updated += 1

        _write(d)
        return updated
