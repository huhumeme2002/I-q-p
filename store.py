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
import threading
from pathlib import Path
from datetime import datetime

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
    except Exception:
        return json.loads(json.dumps(DEFAULT_DATA))


def _write(data: dict):
    with _lock:
        DATA_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def init_data():
    """Ensure data.json exists with all required keys."""
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
                provider: str = "iflow", qwen_email: str = "") -> dict:
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
        "upstream_url": upstream_url,
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
    d = _read()
    for a in d["accounts"]:
        if a["id"] == account_id:
            for k, v in kwargs.items():
                if k in ("name", "api_key", "upstream_url", "proxy", "provider", "qwen_email", "qwen_model",
                         "refresh_token", "access_token", "expiry_date"):
                    a[k] = v if k != "proxy" else str(v).strip()
            _write(d)
            return a
    return None


def delete_account(account_id: str) -> bool:
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
        # For qwen accounts: only release slot if paired iflow account is also gone
        # For iflow accounts: only release slot if paired qwen account is also gone
        # (iflow + qwen sharing same email = 1 slot)
        paired_still_exists = any(
            a.get("proxy") == proxy and a.get("provider") != provider
            and (
                # iflow paired with qwen by name prefix matching
                (provider == "qwen" and qwen_email and
                 a.get("name") == qwen_email.split("@")[0]) or
                (provider == "iflow" and
                 a.get("qwen_email", "").split("@")[0] == acc.get("name", ""))
            )
            for a in d["accounts"]
        )
        if not paired_still_exists:
            # Find proxy in pool by matching proxy string and decrement assigned
            pool = d.get("proxy_pool", [])
            for p in pool:
                if _build_proxy_str(p) == proxy and p.get("assigned", 0) > 0:
                    p["assigned"] -= 1
                    break

    _write(d)
    return True


def toggle_account(account_id: str) -> dict | None:
    d = _read()
    for a in d["accounts"]:
        if a["id"] == account_id:
            a["enabled"] = not a["enabled"]
            _write(d)
            return a
    return None


def disable_account(account_id: str) -> dict | None:
    d = _read()
    for a in d["accounts"]:
        if a["id"] == account_id:
            a["enabled"] = False
            _write(d)
            return a
    return None


def inc_account_request(account_id: str):
    d = _read()
    for a in d["accounts"]:
        if a["id"] == account_id:
            a["request_count"] = a.get("request_count", 0) + 1
            a["last_used"] = datetime.utcnow().isoformat()
            break
    d["stats"]["total_requests"] = d["stats"].get("total_requests", 0) + 1
    _write(d)


def inc_account_error(account_id: str):
    d = _read()
    for a in d["accounts"]:
        if a["id"] == account_id:
            a["error_count"] = a.get("error_count", 0) + 1
            break
    d["stats"]["total_errors"] = d["stats"].get("total_errors", 0) + 1
    _write(d)


def inc_account_tokens(account_id: str, input_tokens: int, output_tokens: int):
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
    d = _read()
    for a in d["accounts"]:
        if a["id"] == account_id:
            a["input_tokens"] = 0
            a["output_tokens"] = 0
            _write(d)
            return True
    return False


def reset_all_tokens():
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
    d = _read()
    d["qwen_default_model"] = model
    _write(d)


_DEFAULT_QWEN_MODELS = ["qwen3-coder-plus", "qwen3-coder-flash"]


def get_qwen_models() -> list:
    d = _read()
    return d.get("qwen_models", _DEFAULT_QWEN_MODELS)


def add_qwen_model(model: str) -> bool:
    d = _read()
    if "qwen_models" not in d:
        d["qwen_models"] = list(_DEFAULT_QWEN_MODELS)
    if model not in d["qwen_models"]:
        d["qwen_models"].append(model)
        _write(d)
        return True
    return False


def delete_qwen_model(model: str) -> bool:
    d = _read()
    if "qwen_models" not in d:
        return False
    if model in d["qwen_models"]:
        d["qwen_models"].remove(model)
        _write(d)
        return True
    return False


def get_active_provider() -> str:
    return _read().get("active_provider", "iflow")


def set_active_provider(provider: str):
    d = _read()
    d["active_provider"] = provider
    _write(d)


def add_model(model: str) -> bool:
    d = _read()
    if model not in d["models"]:
        d["models"].append(model)
        _write(d)
        return True
    return False


def delete_model(model: str) -> bool:
    d = _read()
    if model in d["models"]:
        d["models"].remove(model)
        if d["default_model"] == model and d["models"]:
            d["default_model"] = d["models"][0]
        _write(d)
        return True
    return False


def set_default_model(model: str) -> bool:
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
    _write(d)
    return d["settings"]


def get_admin_password() -> str:
    """Return admin password from env var or data.json. Empty string = no auth."""
    env_pw = os.environ.get("ADMIN_PASSWORD", "").strip()
    if env_pw:
        return env_pw
    return _read().get("admin_password", "")


def set_admin_password(password: str):
    d = _read()
    d["admin_password"] = password.strip()
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


# ============================================================
# STATS & LOGS
# ============================================================

def get_stats() -> dict:
    return _read().get("stats", {"total_requests": 0, "total_errors": 0})


def get_logs() -> list:
    return _read().get("logs", [])


def add_log(account_name: str, model: str, status: str, preview: str = "", duration: int = 0):
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


def clear_logs():
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
    d = _read()
    d["reg_accounts"] = []
    _write(d)


def get_reg_results() -> list:
    return _read().get("reg_results", [])


def add_reg_result(result: dict):
    d = _read()
    if "reg_results" not in d:
        d["reg_results"] = []
    d["reg_results"].insert(0, result)
    _write(d)


def clear_reg_results():
    d = _read()
    d["reg_results"] = []
    _write(d)


def get_reg_status() -> dict:
    return _read().get("reg_status", {"running": False, "use_proxy": False, "headless": True})


def set_reg_status(running: bool, use_proxy: bool = False, headless: bool | None = None):
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
    d = _read()
    before = len(d.get("reg_proxies", []))
    d["reg_proxies"] = [p for p in d.get("reg_proxies", []) if p["id"] != proxy_id]
    if len(d["reg_proxies"]) < before:
        _write(d)
        return True
    return False


def clear_reg_proxies():
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
    d = _read()
    if "proxy_pool" not in d:
        d["proxy_pool"] = []
    added = 0
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Detect optional scheme prefix: http:... or socks5:...
        scheme = "socks5"
        if line.lower().startswith("http:") and not line.lower().startswith("http://"):
            scheme = "http"
            line = line[5:]  # strip "http:"
        elif line.lower().startswith("socks5:") and not line.lower().startswith("socks5://"):
            scheme = "socks5"
            line = line[7:]  # strip "socks5:"
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
    d = _read()
    p = next((x for x in d.get("proxy_pool", []) if x["id"] == proxy_id), None)
    if not p:
        return False
    proxy_str = _build_proxy_str(p)
    d["proxy_pool"] = [x for x in d["proxy_pool"] if x["id"] != proxy_id]
    # Clear proxy from any accounts using it
    for a in d.get("accounts", []):
        if a.get("proxy", "").strip() == proxy_str:
            a["proxy"] = ""
    _write(d)
    return True


def clear_proxy_pool():
    d = _read()
    # Build set of all proxy strings in pool before clearing
    proxy_strs = {_build_proxy_str(p) for p in d.get("proxy_pool", [])}
    d["proxy_pool"] = []
    # Clear proxy from any accounts using a pool proxy
    for a in d.get("accounts", []):
        if a.get("proxy", "").strip() in proxy_strs:
            a["proxy"] = ""
    _write(d)


def pick_pool_proxy() -> dict | None:
    """Pick a proxy from pool that hasn't reached max_per_proxy. Returns proxy dict or None."""
    d = _read()
    pool = d.get("proxy_pool", [])
    max_pp = d.get("proxy_pool_settings", {}).get("max_per_proxy", 1)
    for p in pool:
        if p.get("assigned", 0) < max_pp:
            return p
    return None


def inc_pool_proxy_assigned(proxy_id: str):
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
    """
    proxy_str = ""

    if pair_email:
        # Try to reuse proxy from the paired iflow account (same email, different provider)
        existing = [a for a in get_accounts()
                    if a.get("provider") == "iflow" and a.get("name") == pair_email.split("@")[0]
                    and a.get("proxy")]
        if existing:
            proxy_str = existing[0]["proxy"]

    if not proxy_str:
        proxy = pick_pool_proxy()
        if proxy:
            proxy_str = _build_proxy_str(proxy)
            inc_pool_proxy_assigned(proxy["id"])

    acc = add_account(name=name, api_key=api_key, proxy=proxy_str, provider=provider, qwen_email=qwen_email)
    return acc


# ============================================================
# PROXY REASSIGNMENT
# ============================================================

def reassign_account_proxy(account_id: str, proxy_str: str = "") -> dict | None:
    """Reassign proxy for a single account. If proxy_str is empty, pick from pool."""
    d = _read()
    acc = None
    for a in d["accounts"]:
        if a["id"] == account_id:
            acc = a
            break
    if not acc:
        return None

    old_proxy = acc.get("proxy", "").strip()

    # Release old proxy slot in pool
    if old_proxy:
        for p in d.get("proxy_pool", []):
            if _build_proxy_str(p) == old_proxy and p.get("assigned", 0) > 0:
                p["assigned"] -= 1
                break

    # Assign new proxy
    if not proxy_str:
        max_pp = d.get("proxy_pool_settings", {}).get("max_per_proxy", 1)
        for p in d.get("proxy_pool", []):
            if p.get("assigned", 0) < max_pp:
                proxy_str = _build_proxy_str(p)
                p["assigned"] = p.get("assigned", 0) + 1
                break

    acc["proxy"] = proxy_str
    _write(d)
    return acc


def reassign_all_proxies() -> int:
    """Reassign proxies from pool to all accounts. Returns count updated.
    iFlow + Qwen accounts sharing the same email share one proxy slot.
    """
    d = _read()
    pool = d.get("proxy_pool", [])
    max_pp = d.get("proxy_pool_settings", {}).get("max_per_proxy", 1)

    # Reset all pool assigned counts and clear all account proxies
    for p in pool:
        p["assigned"] = 0
    for a in d["accounts"]:
        a["proxy"] = ""

    updated = 0
    pool_idx = 0
    # email_key -> proxy_str: tracks proxy already assigned to an email group
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
        # Build a pairing key: qwen accounts use qwen_email; iflow accounts use name
        # They share a slot when iflow.name == qwen.qwen_email.split("@")[0]
        qwen_email = a.get("qwen_email", "")
        name = a.get("name", "")
        if qwen_email:
            email_key = qwen_email
        else:
            # For iflow accounts, check if there's a qwen pair by matching name to email prefix
            email_key = name

        if email_key in assigned_by_email:
            a["proxy"] = assigned_by_email[email_key]
            updated += 1
            continue

        proxy_str = _next_proxy()
        if proxy_str:
            a["proxy"] = proxy_str
            assigned_by_email[email_key] = proxy_str
            # Also register the paired key so the partner shares the same proxy
            if qwen_email:
                # qwen account: register by email prefix so iflow partner matches
                assigned_by_email[qwen_email.split("@")[0]] = proxy_str
            else:
                # iflow account: register by name so qwen partner (qwen_email prefix) matches
                assigned_by_email[name] = proxy_str
            updated += 1

    _write(d)
    return updated
