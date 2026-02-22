"""
iFlow OAuth Auto-Refresh Module
Tự động refresh access_token và lấy apiKey từ iFlow OAuth.

Credentials hardcoded trong iFlow CLI bundle:
  client_id     = 10009311001
  client_secret = 4Z3YjXycVsQvyGF1etiNlIBB4RsqSDtW

Endpoints:
  POST https://iflow.cn/oauth/token          (refresh token)
  GET  https://iflow.cn/api/oauth/getUserInfo (get apiKey)
"""
import json
import time
import base64
import asyncio
import logging
from pathlib import Path

import httpx

logger = logging.getLogger("iflow_auth")

# ── OAuth App Credentials (from iFlow CLI bundle) ──────────────────────────
IFLOW_CLIENT_ID     = "10009311001"
IFLOW_CLIENT_SECRET = "4Z3YjXycVsQvyGF1etiNlIBB4RsqSDtW"
IFLOW_TOKEN_URL     = "https://iflow.cn/oauth/token"
IFLOW_USERINFO_URL  = "https://iflow.cn/api/oauth/getUserInfo"

# Default oauth_creds.json path (same as iFlow CLI)
DEFAULT_CREDS_PATH = Path.home() / ".iflow" / "oauth_creds.json"

# ── Basic Auth header ───────────────────────────────────────────────────────
def _basic_auth() -> str:
    raw = f"{IFLOW_CLIENT_ID}:{IFLOW_CLIENT_SECRET}"
    return "Basic " + base64.b64encode(raw.encode()).decode()


# ── Read / Write local creds file ──────────────────────────────────────────
def read_creds(path: Path = DEFAULT_CREDS_PATH) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_creds(data: dict, path: Path = DEFAULT_CREDS_PATH):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning(f"Could not write creds: {e}")


# ── Token expiry check ─────────────────────────────────────────────────────
def is_token_expiring(creds: dict, buffer_ms: int = 5 * 60 * 1000) -> bool:
    """Returns True if access_token expires within buffer_ms milliseconds."""
    expiry = creds.get("expiry_date", 0)
    if not expiry:
        return True
    return (expiry - buffer_ms) <= int(time.time() * 1000)


# ── Refresh access_token ───────────────────────────────────────────────────
async def refresh_access_token(refresh_token: str) -> dict:
    """
    POST https://iflow.cn/oauth/token
    grant_type=refresh_token
    Returns new token dict or raises.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            IFLOW_TOKEN_URL,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": _basic_auth(),
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": IFLOW_CLIENT_ID,
                "client_secret": IFLOW_CLIENT_SECRET,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        logger.info(f"Token refreshed, expires_in={data.get('expires_in')}s")
        return data


# ── Get apiKey from access_token ───────────────────────────────────────────
async def get_user_info(access_token: str) -> dict:
    """
    GET https://iflow.cn/api/oauth/getUserInfo?accessToken=<token>
    Returns {apiKey, userId, userName, avatar, email, ...}
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            IFLOW_USERINFO_URL,
            params={"accessToken": access_token},
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("success") and body.get("data"):
            return body["data"]
        raise ValueError(f"getUserInfo failed: {body}")


# ── Main: ensure valid apiKey ──────────────────────────────────────────────
async def ensure_valid_api_key(creds_path: Path = DEFAULT_CREDS_PATH) -> str | None:
    """
    Reads oauth_creds.json, refreshes if needed, returns current apiKey.
    Also updates the creds file with new tokens.
    """
    creds = read_creds(creds_path)
    if not creds:
        logger.warning("No oauth_creds.json found")
        return None

    refresh_token = creds.get("refresh_token")
    if not refresh_token:
        logger.warning("No refresh_token in creds")
        return creds.get("apiKey")

    # Refresh if expiring soon
    if is_token_expiring(creds):
        logger.info("Access token expiring, refreshing...")
        try:
            new_token = await refresh_access_token(refresh_token)
            # Merge new token data
            creds["access_token"]  = new_token.get("access_token", creds.get("access_token"))
            creds["refresh_token"] = new_token.get("refresh_token", refresh_token)
            creds["expiry_date"]   = int(time.time() * 1000) + new_token.get("expires_in", 3600) * 1000
            creds["token_type"]    = new_token.get("token_type", "bearer")
            creds["scope"]         = new_token.get("scope", creds.get("scope", ""))

            # Get updated apiKey
            user_info = await get_user_info(creds["access_token"])
            if user_info.get("apiKey"):
                creds["apiKey"]    = user_info["apiKey"]
                creds["userId"]    = user_info.get("userId", creds.get("userId"))
                creds["userName"]  = user_info.get("userName", creds.get("userName"))
                creds["email"]     = user_info.get("email", creds.get("email"))
                logger.info(f"Got new apiKey for user {creds.get('userName')}")

            write_creds(creds, creds_path)
        except Exception as e:
            logger.error(f"Token refresh failed: {e}")
            # Fall back to cached apiKey
    else:
        logger.debug("Token still valid")

    return creds.get("apiKey")


# ── Background auto-refresh task ───────────────────────────────────────────
class IFlowAutoRefresh:
    """
    Background task that periodically refreshes the iFlow API key
    and updates the proxy store.
    """
    def __init__(self, store_module, interval_seconds: int = 300):
        self.store = store_module
        self.interval = interval_seconds
        self._task: asyncio.Task | None = None

    def start(self):
        self._task = asyncio.create_task(self._loop())
        logger.info(f"Auto-refresh started (interval={self.interval}s)")

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
                logger.error(f"Auto-refresh error: {e}")
            await asyncio.sleep(self.interval)

    async def _refresh_once(self):
        accounts = self.store.get_accounts()
        updated = 0
        for acc in accounts:
            if acc.get("provider", "iflow") != "iflow":
                continue
            refresh_token = acc.get("refresh_token", "")
            if not refresh_token:
                # Fallback: try shared ~/.iflow/oauth_creds.json
                api_key = await ensure_valid_api_key()
                if api_key and acc.get("api_key") != api_key:
                    self.store.update_account(acc["id"], api_key=api_key)
                    updated += 1
                continue

            # Check if token is expiring soon
            expiry = acc.get("expiry_date", 0)
            if expiry and (expiry - 5 * 60 * 1000) > int(time.time() * 1000):
                continue  # still valid

            try:
                new_tok = await refresh_access_token(refresh_token)
                new_access = new_tok["access_token"]
                new_refresh = new_tok.get("refresh_token", refresh_token)
                new_expiry = int(time.time() * 1000) + new_tok.get("expires_in", 172800) * 1000

                user_info = await get_user_info(new_access)
                new_api_key = user_info.get("apiKey") or acc.get("api_key")

                self.store.update_account(acc["id"],
                    api_key=new_api_key,
                    access_token=new_access,
                    refresh_token=new_refresh,
                    expiry_date=new_expiry,
                )
                updated += 1
                logger.info(f"Refreshed token for account '{acc['name']}' (expires in {new_tok.get('expires_in')}s)")
            except Exception as e:
                logger.error(f"Failed to refresh token for account '{acc['name']}': {e}")

        if updated:
            logger.info(f"Refreshed {updated} iFlow account(s)")


# ── CLI test ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import asyncio
    logging.basicConfig(level=logging.INFO)

    async def main():
        print("Reading creds from:", DEFAULT_CREDS_PATH)
        creds = read_creds()
        print(f"  userId   : {creds.get('userId')}")
        print(f"  userName : {creds.get('userName')}")
        print(f"  email    : {creds.get('email')}")
        print(f"  apiKey   : {creds.get('apiKey')}")
        expiry = creds.get('expiry_date', 0)
        remaining = (expiry - int(time.time() * 1000)) / 1000
        print(f"  expires  : {remaining:.0f}s from now")
        print(f"  expiring : {is_token_expiring(creds)}")
        print()

        print("Ensuring valid API key via shared creds file...")
        key = await ensure_valid_api_key()
        print(f"  apiKey   : {key}")

        # Test per-account refresh if refresh_token present in creds
        rt = creds.get("refresh_token")
        if rt:
            print("\nTesting per-account token refresh...")
            try:
                new_tok = await refresh_access_token(rt)
                print(f"  new access_token : {new_tok.get('access_token', '')[:20]}...")
                print(f"  expires_in       : {new_tok.get('expires_in')}s")
                user_info = await get_user_info(new_tok["access_token"])
                print(f"  apiKey           : {user_info.get('apiKey')}")
            except Exception as e:
                print(f"  refresh failed: {e}")
        else:
            print("\nNo refresh_token in creds file — skipping per-account refresh test")

    asyncio.run(main())
