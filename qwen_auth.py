"""
Qwen OAuth Device Flow Module
Authenticates with Qwen via OAuth 2.0 Device Authorization Grant (RFC 8628) with PKCE.

Endpoints:
  POST https://chat.qwen.ai/api/v1/oauth2/device/code  (initiate device flow)
  POST https://chat.qwen.ai/api/v1/oauth2/token         (poll / refresh)

API:
  https://portal.qwen.ai/v1/chat/completions             (default inference)
  https://{resource_url}/v1/chat/completions              (if resource_url present)
"""
import json
import time
import secrets
import hashlib
import base64
import asyncio
import logging
from pathlib import Path

import httpx

logger = logging.getLogger("qwen_auth")

# ── OAuth Constants ───────────────────────────────────────────────────────
QWEN_CLIENT_ID = "f0304373b74a44d2b584a3fb70ca9e56"
QWEN_SCOPE = "openid profile email model.completion"
QWEN_DEVICE_URL = "https://chat.qwen.ai/api/v1/oauth2/device/code"
QWEN_TOKEN_URL = "https://chat.qwen.ai/api/v1/oauth2/token"
QWEN_DEFAULT_UPSTREAM = "https://portal.qwen.ai/v1/chat/completions"

QWEN_CREDS_DIR = Path.home() / ".cli-proxy-api"
QWEN_REFRESH_LEAD_S = 3 * 3600  # refresh 3 hours before expiry


# ── PKCE ──────────────────────────────────────────────────────────────────
def _pkce_pair() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256)."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# ── Credential File I/O ──────────────────────────────────────────────────
def creds_path_for(email: str) -> Path:
    return QWEN_CREDS_DIR / f"qwen-{email}.json"


def read_qwen_creds(email: str) -> dict:
    try:
        return json.loads(creds_path_for(email).read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_qwen_creds(email: str, data: dict):
    try:
        path = creds_path_for(email)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning(f"Could not write Qwen creds for {email}: {e}")


def list_qwen_creds() -> list[dict]:
    """Return all ~/.cli-proxy-api/qwen-*.json as list of dicts."""
    results = []
    if not QWEN_CREDS_DIR.exists():
        return results
    for p in QWEN_CREDS_DIR.glob("qwen-*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            email = p.stem.removeprefix("qwen-")
            d.setdefault("email", email)
            results.append(d)
        except Exception:
            pass
    return results


# ── Token Expiry ──────────────────────────────────────────────────────────
def is_qwen_token_expiring(creds: dict) -> bool:
    """Returns True if access_token expires within QWEN_REFRESH_LEAD_S."""
    expiry = creds.get("expiry_date", 0)
    if not expiry:
        return True
    return (expiry - QWEN_REFRESH_LEAD_S * 1000) <= int(time.time() * 1000)


# ── Device Flow: Initiate ─────────────────────────────────────────────────
async def start_device_flow() -> dict:
    """
    POST device/code endpoint.
    Returns {device_code, user_code, verification_uri, verification_uri_complete,
             expires_in, interval, verifier}.
    """
    verifier, challenge = _pkce_pair()
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.post(
            QWEN_DEVICE_URL,
            data={
                "client_id": QWEN_CLIENT_ID,
                "scope": QWEN_SCOPE,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": "QwenCode/0.10.3",
            },
        )
        logger.info(f"Device code response: status={resp.status_code}, content-type={resp.headers.get('content-type')}, body={resp.text[:500]}")
        resp.raise_for_status()
        try:
            data = resp.json()
        except Exception:
            raise RuntimeError(f"Qwen device/code returned non-JSON (HTTP {resp.status_code}): {resp.text[:200]}")
    data["verifier"] = verifier
    logger.info(f"Device flow started, user_code={data.get('user_code')}")
    return data


# ── Device Flow: Poll for Token ───────────────────────────────────────────
async def poll_device_token(
    device_code: str,
    verifier: str,
    interval: int = 5,
    max_attempts: int = 60,
) -> dict:
    """
    Poll token endpoint until user authorizes or timeout.
    Returns token dict {access_token, refresh_token, resource_url, expires_in, ...}.
    """
    current_interval = interval
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        for _ in range(max_attempts):
            await asyncio.sleep(current_interval)
            resp = await client.post(
                QWEN_TOKEN_URL,
                data={
                    "client_id": QWEN_CLIENT_ID,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": device_code,
                    "code_verifier": verifier,
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                    "User-Agent": "QwenCode/0.10.3",
                },
            )
            try:
                body = resp.json()
            except Exception:
                logger.warning(f"Qwen token poll: non-JSON response (HTTP {resp.status_code}): {resp.text[:200]}")
                body = {}
            if resp.status_code == 200 and body.get("access_token"):
                logger.info("Device flow completed, got access_token")
                return body
            error = body.get("error", "")
            if error == "authorization_pending":
                continue
            elif error == "slow_down":
                current_interval = min(current_interval * 1.5, 10)
                continue
            elif error in ("expired_token", "access_denied"):
                raise RuntimeError(f"Qwen device flow: {error}")
            elif not error:
                # Empty body or no error field — treat as pending, keep polling
                logger.warning(f"Qwen token poll: unexpected response (HTTP {resp.status_code}): {body}")
                continue
            else:
                raise RuntimeError(f"Qwen token poll error: {body}")
    raise TimeoutError("Qwen device flow timed out (max attempts reached)")


# ── Token Refresh ─────────────────────────────────────────────────────────
async def refresh_qwen_token(refresh_token: str) -> dict:
    """Refresh access_token using refresh_token."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            QWEN_TOKEN_URL,
            data={
                "client_id": QWEN_CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": "QwenCode/0.10.3 (darwin; arm64)",
                "X-Dashscope-Useragent": "QwenCode/0.10.3 (darwin; arm64)",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        logger.info(f"Qwen token refreshed, expires_in={data.get('expires_in')}s")
        return data


# ── Ensure Valid Token ────────────────────────────────────────────────────
async def ensure_valid_access_token(email: str) -> str | None:
    """Read creds, refresh if expiring, return current access_token."""
    creds = read_qwen_creds(email)
    if not creds:
        logger.warning(f"No Qwen creds for {email}")
        return None

    if not is_qwen_token_expiring(creds):
        return creds.get("access_token")

    rt = creds.get("refresh_token")
    if not rt:
        return creds.get("access_token")

    try:
        new_tok = await refresh_qwen_token(rt)
        creds["access_token"] = new_tok["access_token"]
        creds["refresh_token"] = new_tok.get("refresh_token", rt)
        expires_in = new_tok.get("expires_in", 3600)
        creds["expiry_date"] = int(time.time() * 1000) + expires_in * 1000
        if "resource_url" in new_tok:
            creds["resource_url"] = new_tok["resource_url"]
        write_qwen_creds(email, creds)
        logger.info(f"Qwen token refreshed for {email}")
    except Exception as e:
        logger.error(f"Qwen token refresh failed for {email}: {e}")

    return creds.get("access_token")


# ── Background Auto-Refresh ──────────────────────────────────────────────
class QwenAutoRefresh:
    """Periodically refreshes Qwen tokens and updates the proxy store."""

    def __init__(self, store_module, interval_seconds: int = 300):
        self.store = store_module
        self.interval = interval_seconds
        self._task: asyncio.Task | None = None

    def start(self):
        self._task = asyncio.create_task(self._loop())
        logger.info(f"Qwen auto-refresh started (interval={self.interval}s)")

    def stop(self):
        if self._task:
            self._task.cancel()

    async def _loop(self):
        while True:
            try:
                await self._refresh_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"QwenAutoRefresh error: {e}")
            await asyncio.sleep(self.interval)

    async def _refresh_once(self):
        accounts = self.store.get_accounts()
        for acc in accounts:
            if acc.get("provider") != "qwen":
                continue
            email = acc.get("qwen_email", "")
            if not email:
                continue
            token = await ensure_valid_access_token(email)
            if token and acc.get("api_key") != token:
                self.store.update_account(acc["id"], api_key=token)
                logger.info(f"Updated Qwen account '{acc['name']}' with refreshed token")
