"""
iFlow Account Registration Tool
Automates: Google Sign-In → iFlow → Create API Key → Qwen OAuth
Usage:
    1. Put Google accounts in accounts.txt (email|password per line)
    2. python reg_iflow.py                          (no proxy, 1 worker)
    3. python reg_iflow.py --proxy                  (with proxies from reg_proxies.json)
    4. python reg_iflow.py --proxy --workers 3      (3 concurrent workers)
"""
import asyncio
import json
import re
import sys
from pathlib import Path
from datetime import datetime
from playwright.async_api import async_playwright, Browser, BrowserContext
import httpx

IFLOW_URL = "https://platform.iflow.cn/profile?tab=apiKey"
OUTPUT_FILE = Path(__file__).parent / "reg_results.json"
ACCOUNTS_FILE = Path(__file__).parent / "accounts.txt"
PROXIES_FILE = Path(__file__).parent / "reg_proxies.json"

# Timeouts (ms)
NAV_TIMEOUT = 30_000
ACTION_TIMEOUT = 15_000

# Import store and qwen_auth for saving results
try:
    import store as _store
except ImportError:
    _store = None

try:
    import qwen_auth as _qwen_auth
except ImportError:
    _qwen_auth = None

try:
    from playwright_stealth import stealth_async as _stealth_async
except ImportError:
    _stealth_async = None

# Thread-safe lock for file writes
import threading
_file_lock = threading.Lock()


# ── Helpers ──

def load_accounts() -> list[dict]:
    if not ACCOUNTS_FILE.exists():
        print(f"[!] {ACCOUNTS_FILE} not found")
        sys.exit(1)
    accounts = []
    for line in ACCOUNTS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        sep = "|" if "|" in line else ":"
        parts = line.split(sep, 1)
        if len(parts) == 2:
            accounts.append({"email": parts[0].strip(), "password": parts[1].strip()})
    return accounts


def load_proxies() -> list[dict]:
    if PROXIES_FILE.exists():
        try:
            return json.loads(PROXIES_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def save_result(result: dict):
    with _file_lock:
        results = []
        if OUTPUT_FILE.exists():
            try:
                results = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
            except Exception:
                results = []
        results.append(result)
        OUTPUT_FILE.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    if _store:
        _store.add_reg_result(result)


async def rotate_proxy(rotate_url: str, worker_id: str) -> bool:
    if not rotate_url:
        return True
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(rotate_url)
            print(f"  [{worker_id}][proxy] Rotate: {resp.status_code} - {resp.text[:80]}")
            return resp.status_code == 200
    except Exception as e:
        print(f"  [{worker_id}][proxy] Rotate failed: {e}")
        return False


# ── Browser automation ──

async def google_sign_in(context: BrowserContext, email: str, password: str, tag: str = "") -> bool:
    page = await context.new_page()
    if _stealth_async:
        await _stealth_async(page)
    try:
        print(f"  [{tag}][1] Opening iFlow...")
        await page.goto(IFLOW_URL, timeout=NAV_TIMEOUT, wait_until="networkidle")
        await page.wait_for_timeout(3000)

        print(f"  [{tag}][2] Clicking Sign in with Google...")
        google_page = None
        popup_future = asyncio.get_event_loop().create_future()

        def on_popup(new_page):
            if not popup_future.done():
                popup_future.set_result(new_page)
        context.on("page", on_popup)

        clicked = False
        for frame in page.frames:
            try:
                btn = frame.locator("text=Sign in with Google").first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            for sel in ["text=Sign in with Google", ".nsm7Bb-HzV7m-LgbsSe-bN97Pc-sM5MNb",
                         "[data-provider='google']", "div[role='button']:has-text('Google')"]:
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible(timeout=2000):
                        await btn.click()
                        clicked = True
                        break
                except Exception:
                    continue
        if not clicked:
            print(f"  [{tag}][!] Could not find Google button")
            await page.close()
            return False

        try:
            google_page = await asyncio.wait_for(popup_future, timeout=10)
            await google_page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT)
        except asyncio.TimeoutError:
            if "accounts.google.com" in page.url:
                google_page = page
            else:
                print(f"  [{tag}][!] No popup/redirect. URL: {page.url}")
                await page.close()
                return False
        context.remove_listener("page", on_popup)

        # Email
        print(f"  [{tag}][3] Entering email...")
        await google_page.wait_for_selector("#identifierId", timeout=ACTION_TIMEOUT)
        await google_page.fill("#identifierId", email)
        await google_page.wait_for_timeout(500)
        for t in ["Tiếp theo", "Next"]:
            try:
                b = google_page.locator(f"button:has-text('{t}'), span:has-text('{t}')").first
                if await b.is_visible(timeout=2000):
                    await b.click(); break
            except Exception: continue
        await google_page.wait_for_timeout(3000)

        # Password
        print(f"  [{tag}][4] Entering password...")
        await google_page.wait_for_selector("input[name='Passwd']", timeout=ACTION_TIMEOUT)
        await google_page.fill("input[name='Passwd']", password)
        await google_page.wait_for_timeout(500)
        for t in ["Tiếp theo", "Next"]:
            try:
                b = google_page.locator(f"button:has-text('{t}'), span:has-text('{t}')").first
                if await b.is_visible(timeout=2000):
                    await b.click(); break
            except Exception: continue
        await google_page.wait_for_timeout(4000)

        # Consent screens
        print(f"  [{tag}][5] Consent screens...")
        for sel in ["input[name='confirm']", "button:has-text('Tôi hiểu')", "button:has-text('I understand')"]:
            try:
                el = google_page.locator(sel).first
                if await el.is_visible(timeout=3000):
                    await el.click(); await google_page.wait_for_timeout(2000); break
            except Exception: continue
        for t in ["Continue", "Tiếp tục", "Allow"]:
            try:
                b = google_page.locator(f"button:has-text('{t}')").first
                if await b.is_visible(timeout=3000):
                    await b.click(); await google_page.wait_for_timeout(3000); break
            except Exception: continue

        print(f"  [{tag}][6] Waiting for iFlow...")
        if google_page != page:
            await page.wait_for_timeout(3000)
            await page.reload(wait_until="networkidle", timeout=NAV_TIMEOUT)
        else:
            try: await page.wait_for_url("**/platform.iflow.cn/**", timeout=NAV_TIMEOUT)
            except Exception: await page.goto(IFLOW_URL, timeout=NAV_TIMEOUT, wait_until="networkidle")
        await page.wait_for_timeout(2000)

    except Exception as e:
        print(f"  [{tag}][!] Error: {e}")
        try: await page.close()
        except Exception: pass
        return False
    return True
# PLACEHOLDER_CREATEKEY


async def create_api_key(context: BrowserContext, tag: str = "") -> str | None:
    pages = context.pages
    page = pages[-1] if pages else await context.new_page()
    try:
        print(f"  [{tag}][7] Opening API key page...")
        await page.goto(IFLOW_URL, timeout=NAV_TIMEOUT, wait_until="networkidle")
        await page.wait_for_timeout(2000)

        print(f"  [{tag}][8] Create API Key...")
        await page.locator("text=创建API密钥").first.click(timeout=ACTION_TIMEOUT)
        await page.wait_for_timeout(1500)

        print(f"  [{tag}][9] Agree and generate...")
        await page.locator("text=同意协议并生成").first.click(timeout=ACTION_TIMEOUT)
        await page.wait_for_timeout(2000)

        print(f"  [{tag}][10] Extracting API key...")
        key_el = page.locator("[class*='value']").filter(has_text="sk-").first
        if not await key_el.is_visible(timeout=5000):
            content = await page.content()
            match = re.search(r"sk-[a-zA-Z0-9]{16,}", content)
            if match:
                return match.group(0)
            # Screenshot for debugging
            try:
                await page.screenshot(path=str(Path(__file__).parent / f"iflow_key_fail_{tag}.png"))
                print(f"  [{tag}][!] Screenshot saved: iflow_key_fail_{tag}.png")
            except Exception:
                pass
            return None
        api_key = (await key_el.text_content()).strip()
        return api_key if api_key.startswith("sk-") else None
    except Exception as e:
        print(f"  [{tag}][!] Create key error: {e}")
        try:
            pages = context.pages
            if pages:
                await pages[-1].screenshot(path=str(Path(__file__).parent / f"iflow_key_fail_{tag}.png"))
                print(f"  [{tag}][!] Screenshot saved: iflow_key_fail_{tag}.png")
        except Exception:
            pass
        return None


IFLOW_CLIENT_ID     = "10009311001"
IFLOW_CLIENT_SECRET = "4Z3YjXycVsQvyGF1etiNlIBB4RsqSDtW"
IFLOW_TOKEN_URL     = "https://iflow.cn/oauth/token"
IFLOW_USERINFO_URL  = "https://iflow.cn/api/oauth/getUserInfo"


async def extract_oauth_tokens(context: BrowserContext, tag: str = "") -> dict | None:
    """Use the existing logged-in browser context to run OAuth flow and get refresh_token + apiKey."""
    import secrets as _secrets
    import base64 as _b64
    import time as _time
    from urllib.parse import quote as _quote
    from aiohttp import web as _web

    port = 54300 + hash(tag) % 200  # unique port per worker
    redirect_uri = f"http://localhost:{port}/oauth2callback"

    # Find a free port if the calculated one is in use
    import socket as _socket
    for _p in range(port, port + 20):
        try:
            _s = _socket.socket()
            _s.bind(('127.0.0.1', _p))
            _s.close()
            port = _p
            break
        except OSError:
            continue
    redirect_uri = f"http://localhost:{port}/oauth2callback"
    state = _secrets.token_urlsafe(16)

    code_received = asyncio.Event()
    auth_code: dict = {}

    async def _cb(request):
        code = request.rel_url.query.get("code")
        if code:
            auth_code["code"] = code
            code_received.set()
        return _web.Response(text="OK")

    app = _web.Application()
    app.router.add_get("/oauth2callback", _cb)
    runner = _web.AppRunner(app)
    await runner.setup()
    await _web.TCPSite(runner, "127.0.0.1", port).start()

    try:
        page = await context.new_page()
        auth_url = (
            f"https://iflow.cn/oauth?loginMethod=phone&type=phone"
            f"&redirect={_quote(redirect_uri)}&state={state}&client_id={IFLOW_CLIENT_ID}"
        )
        print(f"  [{tag}][OAuth] Starting token extraction...")
        await page.goto(auth_url, wait_until="networkidle", timeout=20000)
        await page.wait_for_timeout(2000)

        # Click the account row (skip "使用其他账号")
        clicked = await page.evaluate("""() => {
            const els = document.querySelectorAll("div, li");
            for (const el of els) {
                const t = (el.innerText || "").trim();
                if (t && t.length < 50 && !t.includes("使用其他") && !t.includes("账号")) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 50 && r.height > 20) { el.click(); return t; }
                }
            }
            return null;
        }""")
        if clicked:
            print(f"  [{tag}][OAuth] Clicked account: {clicked}")
        await page.wait_for_timeout(3000)
        await page.close()

        await asyncio.wait_for(code_received.wait(), timeout=10)

        basic = _b64.b64encode(f"{IFLOW_CLIENT_ID}:{IFLOW_CLIENT_SECRET}".encode()).decode()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                IFLOW_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": auth_code["code"],
                    "redirect_uri": redirect_uri,
                    "client_id": IFLOW_CLIENT_ID,
                    "client_secret": IFLOW_CLIENT_SECRET,
                },
                headers={
                    "Authorization": f"Basic {basic}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            tokens = resp.json()
            if not tokens.get("access_token"):
                print(f"  [{tag}][OAuth] Token exchange failed: {tokens}")
                return None

            info_resp = await client.get(f"{IFLOW_USERINFO_URL}?accessToken={tokens['access_token']}")
            info = info_resp.json().get("data", {})

        result = {
            "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token", ""),
            "expiry_date": int(_time.time() * 1000) + tokens.get("expires_in", 172800) * 1000,
            "apiKey": info.get("apiKey"),
            "email": info.get("email") or info.get("phone", ""),
            "userName": info.get("userName", ""),
        }
        print(f"  [{tag}][OAuth] Got refresh_token + apiKey for {result['userName']}")
        return result

    except asyncio.TimeoutError:
        print(f"  [{tag}][OAuth] Timeout waiting for OAuth code")
        return None
    except Exception as e:
        print(f"  [{tag}][OAuth] Error: {e}")
        return None
    finally:
        await runner.cleanup()


# ── Qwen OAuth Device Flow (browser automation) ──

async def authorize_qwen(context: BrowserContext, email: str, tag: str = "") -> bool:
    """
    Automate Qwen OAuth device flow in an existing browser context
    (Google already logged in from iFlow registration).
    Returns True if authorization succeeded.
    """
    if not _qwen_auth:
        print(f"  [{tag}][QWEN] qwen_auth module not available, skipping")
        return False

    # Step 1: Start device flow
    print(f"  [{tag}][QWEN][1] Starting device flow...")
    try:
        flow = await _qwen_auth.start_device_flow()
    except Exception as e:
        print(f"  [{tag}][QWEN][!] Device flow start failed: {e}")
        return False

    device_code = flow["device_code"]
    verifier = flow["verifier"]
    interval = flow.get("interval", 5)
    verify_url = flow.get("verification_uri_complete") or flow.get("verification_uri", "")
    print(f"  [{tag}][QWEN][2] Got user_code={flow.get('user_code')}, opening {verify_url[:60]}...")

    # Step 2: Start token polling in background
    poll_task = asyncio.create_task(
        _qwen_auth.poll_device_token(device_code, verifier, interval, max_attempts=60)
    )

    # Step 3: Browser automation — authorize via Google
    page = await context.new_page()
    authorized = False
    try:
        await page.goto(verify_url, timeout=NAV_TIMEOUT, wait_until="networkidle")
        await page.wait_for_timeout(3000)

        # Click "Continue with Google"
        print(f"  [{tag}][QWEN][3] Clicking Continue with Google...")
        google_clicked = False
        for sel in [
            "text=Continue with Google",
            "text=Sign in with Google",
            ".qwenchat-auth-pc-other-login-text",
            "span:has-text('Google')",
        ]:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=3000):
                    await btn.click()
                    google_clicked = True
                    break
            except Exception:
                continue

        if not google_clicked:
            print(f"  [{tag}][QWEN][!] Could not find Google login button")
            await page.close()
            poll_task.cancel()
            return False

        # Wait for Google account chooser page to actually load
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        await page.wait_for_timeout(2000)

        # Click on the Google account (already logged in)
        print(f"  [{tag}][QWEN][4] Selecting Google account...")
        account_clicked = False

        # First wait for the account chooser to appear
        try:
            await page.locator(
                "[data-authuser], .VV3oRb, [data-identifier], "
                "li[data-identifier], div[data-authuser], "
                ".XraQ3b, .nCP5yc, [jsname='tJHJj'], "
                "li.MnOvKe, div.MnOvKe"
            ).first.wait_for(state="visible", timeout=15000)
        except Exception:
            print(f"  [{tag}][QWEN][4] Account chooser not found, taking screenshot...")
            try:
                url = page.url
                title = await page.title()
                print(f"  [{tag}][QWEN][4] Current page: {url} — {title}")
            except Exception:
                pass

        # Try to find the account by email first, then fallback
        for sel in [
            f"[data-identifier='{email}']",
            f"li[data-identifier='{email}']",
            f"[data-authuser] [data-identifier='{email}']",
            "[data-authuser='0']",
            "li[data-authuser='0']",
            ".VV3oRb",
            # New Google account chooser UI (WebLiteSignIn / AccountChooser)
            "li.MnOvKe",
            "div.MnOvKe",
            "[jsname='tJHJj']",
            ".XraQ3b",
            # Generic: any list item in the account chooser
            "ul li:first-child",
            "[data-authuser]",
        ]:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=3000):
                    await el.click()
                    account_clicked = True
                    print(f"  [{tag}][QWEN][4] Clicked account via selector: {sel}")
                    break
            except Exception:
                continue

        if not account_clicked:
            # Last resort: JS click on any element containing the email
            try:
                clicked_text = await page.evaluate(f"""() => {{
                    const email = '{email}';
                    const all = document.querySelectorAll('li, div[role="button"], div[tabindex]');
                    for (const el of all) {{
                        if (el.innerText && el.innerText.includes(email)) {{
                            el.click();
                            return el.innerText.trim().slice(0, 50);
                        }}
                    }}
                    // fallback: click first li or div[role=button] in account chooser
                    const first = document.querySelector('li[data-authuser], li.MnOvKe, div[data-authuser]');
                    if (first) {{ first.click(); return 'first-item'; }}
                    return null;
                }}""")
                if clicked_text:
                    account_clicked = True
                    print(f"  [{tag}][QWEN][4] JS-clicked account: {clicked_text}")
            except Exception:
                pass

        if not account_clicked:
            print(f"  [{tag}][QWEN][!] Could not select Google account")
            try:
                url = page.url
                content = await page.content()
                print(f"  [{tag}][QWEN][!] Page URL: {url}")
                print(f"  [{tag}][QWEN][!] Page snippet: {content[:500]}")
            except Exception:
                pass
            await page.close()
            poll_task.cancel()
            return False

        # Wait for page to navigate away from account chooser
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        await page.wait_for_timeout(3000)

        # Click Continue/Allow on Google consent (may appear multiple times)
        print(f"  [{tag}][QWEN][5] Google consent...")
        for _ in range(3):
            clicked = False
            for t in ["Continue", "Tiếp tục", "Allow", "Cho phép"]:
                try:
                    b = page.locator(f"button:has-text('{t}')").first
                    if await b.is_visible(timeout=4000):
                        await b.click()
                        await page.wait_for_timeout(3000)
                        clicked = True
                        print(f"  [{tag}][QWEN][5] Clicked: {t}")
                        break
                except Exception:
                    continue
            if not clicked:
                break
            # Google may show consent page again after clicking Continue
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            await page.wait_for_timeout(2000)

        await page.wait_for_timeout(2000)

        # Click Confirm on Qwen authorization page (may appear multiple times)
        print(f"  [{tag}][QWEN][6] Qwen confirm...")
        await page.wait_for_timeout(3000)
        for attempt in range(4):
            clicked = False
            for sel in [
                "button:has-text('Confirm')",
                "button:has-text('确认')",
                "button:has-text('授权')",
                ".qwen-confirm-btn",
                ".qwenchat-auth-pc-callback-button",
                "button[type='submit']",
                "button.ant-btn-primary",
            ]:
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible(timeout=3000):
                        await btn.click()
                        await page.wait_for_timeout(3000)
                        clicked = True
                        print(f"  [{tag}][QWEN][6] attempt {attempt+1} clicked via: {sel}")
                        break
                except Exception:
                    continue
            if not clicked:
                # No confirm button found, log and stop
                try:
                    url = page.url
                    print(f"  [{tag}][QWEN][6] attempt {attempt+1} - no confirm btn, url={url}")
                    await page.screenshot(path=str(Path(__file__).parent / f"qwen_confirm_{tag}_{attempt}.png"))
                except Exception:
                    pass
                break
            # Wait for next page/button after clicking
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            await page.wait_for_timeout(2000)

        # Wait for success message
        print(f"  [{tag}][QWEN][7] Waiting for success...")
        try:
            await page.locator("text=Authentication successful").wait_for(timeout=15000)
            authorized = True
            print(f"  [{tag}][QWEN][OK] Browser authorization successful")
        except Exception:
            # Check if page shows success in any form
            content = await page.content()
            if "Authentication successful" in content or "success" in content.lower():
                authorized = True
                print(f"  [{tag}][QWEN][OK] Authorization detected in page content")
            else:
                print(f"  [{tag}][QWEN][!] Did not see success message")

    except Exception as e:
        print(f"  [{tag}][QWEN][!] Browser automation error: {e}")
    finally:
        try:
            await page.close()
        except Exception:
            pass

    # Step 4: Wait for token from polling
    if authorized:
        print(f"  [{tag}][QWEN][8] Waiting for token...")
        try:
            token = await asyncio.wait_for(poll_task, timeout=30)
            expires_in = token.get("expires_in", 3600)
            import time as _time
            token["expiry_date"] = int(_time.time() * 1000) + expires_in * 1000
            token["email"] = email
            _qwen_auth.write_qwen_creds(email, token)

            # Add to store
            if _store:
                access_token = token["access_token"]
                resource_url = token.get("resource_url", "")
                upstream = f"https://{resource_url}/v1/chat/completions" if resource_url else ""
                existing = [a for a in _store.get_accounts()
                            if a.get("provider") == "qwen" and a.get("qwen_email") == email]
                if existing:
                    _store.update_account(existing[0]["id"], api_key=access_token, upstream_url=upstream)
                else:
                    _store.auto_add_account_with_proxy(
                        api_key=access_token,
                        name=f"Qwen ({email.split('@')[0]})",
                        provider="qwen",
                        qwen_email=email,
                        pair_email=email,
                    )
                print(f"  [{tag}][QWEN][OK] Token saved, account added to store")
            return True
        except asyncio.TimeoutError:
            print(f"  [{tag}][QWEN][!] Token poll timed out after browser success")
        except Exception as e:
            print(f"  [{tag}][QWEN][!] Token poll error: {e}")
    else:
        poll_task.cancel()

    return False


async def process_account(browser: Browser, email: str, password: str, tag: str = "") -> dict:
    result = {
        "email": email, "api_key": None, "status": "failed",
        "error": None, "timestamp": datetime.utcnow().isoformat(),
    }
    context = await browser.new_context(
        viewport={"width": 1280, "height": 800},
        locale="vi-VN",
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
    )
    # Hide automation fingerprints
    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
        Object.defineProperty(navigator, 'languages', { get: () => ['vi-VN', 'vi', 'en-US', 'en'] });
        window.chrome = { runtime: {} };
    """)
    try:
        ok = await google_sign_in(context, email, password, tag)
        if not ok:
            result["error"] = "Google sign-in failed"
            return result
        api_key = await create_api_key(context, tag)
        if api_key:
            result["api_key"] = api_key
            result["status"] = "success"
            print(f"  [{tag}][OK] {api_key[:16]}...")

            # Extract OAuth tokens (refresh_token) using the live session
            oauth = await extract_oauth_tokens(context, tag)
            if oauth:
                result["refresh_token"] = oauth.get("refresh_token", "")
                result["access_token"] = oauth.get("access_token", "")
                result["expiry_date"] = oauth.get("expiry_date", 0)

            # Auto-add to proxy account list with SOCKS5 from pool
            if _store:
                acc = _store.auto_add_account_with_proxy(api_key, email.split("@")[0])
                if acc:
                    proxy_info = acc.get("proxy", "direct")
                    print(f"  [{tag}][AUTO] Added to accounts → {proxy_info}")
                    # Save refresh_token into the account
                    if oauth and oauth.get("refresh_token"):
                        _store.update_account(acc["id"], refresh_token=oauth["refresh_token"],
                                              access_token=oauth["access_token"],
                                              expiry_date=oauth["expiry_date"])
                        print(f"  [{tag}][AUTO] Saved refresh_token for {acc['name']}")
                else:
                    print(f"  [{tag}][AUTO] Added to accounts (no proxy available)")

            # Qwen OAuth: reuse Google session to authorize Qwen
            if _qwen_auth:
                qwen_ok = await authorize_qwen(context, email, tag)
                result["qwen_authorized"] = qwen_ok
        else:
            result["error"] = "Could not extract API key"
    except Exception as e:
        result["error"] = str(e)
    finally:
        await context.close()
    return result


# ── Worker & Main ──

async def worker(worker_id: str, queue: asyncio.Queue, playwright, proxy_cfg: dict | None, rotate_url: str, headless: bool = False):
    """Worker coroutine: pulls accounts from queue, processes them one by one."""
    while True:
        try:
            idx, total, acc = queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        tag = f"W{worker_id}"
        email, password = acc["email"], acc["password"]
        print(f"\n[{tag}][{idx}/{total}] {email}")

        # Rotate proxy before each account
        if proxy_cfg and rotate_url:
            await rotate_proxy(rotate_url, tag)
            await asyncio.sleep(3)

        browser = await playwright.chromium.launch(
            headless=headless,
            proxy=proxy_cfg,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-infobars",
                "--window-size=1280,800",
            ],
        )
        result = await process_account(browser, email, password, tag)
        save_result(result)
        await browser.close()

        status = "OK" if result["status"] == "success" else f"FAIL: {result['error']}"
        print(f"  [{tag}] {status}")

        queue.task_done()
        await asyncio.sleep(2)


async def main():
    use_proxy = "--proxy" in sys.argv
    use_headless = "--headless" in sys.argv

    # Parse --workers N
    num_workers = 1
    if "--workers" in sys.argv:
        wi = sys.argv.index("--workers")
        if wi + 1 < len(sys.argv):
            num_workers = max(1, int(sys.argv[wi + 1]))

    accounts = load_accounts()
    if not accounts:
        print("[!] No accounts in accounts.txt")
        return

    proxies = load_proxies() if use_proxy else []

    print(f"[*] Accounts : {len(accounts)}")
    print(f"[*] Proxies  : {len(proxies) if proxies else 'OFF'}")
    print(f"[*] Workers  : {num_workers}")
    print(f"[*] Headless : {use_headless}")
    print(f"[*] Output   : {OUTPUT_FILE}")
    print()

    # Build proxy configs for workers
    # If we have proxies, assign them round-robin to workers
    # If no proxies, all workers run without proxy
    worker_proxies: list[tuple[dict | None, str]] = []
    if proxies:
        for i in range(num_workers):
            px = proxies[i % len(proxies)]
            cfg = {
                "server": f"http://{px['host']}:{px['port']}",
                "username": px.get("username", ""),
                "password": px.get("password", ""),
            }
            # Skip auth if no username
            if not cfg["username"]:
                cfg = {"server": cfg["server"]}
            worker_proxies.append((cfg, px.get("rotate_url", "")))
    else:
        worker_proxies = [(None, "")] * num_workers

    # Fill queue
    queue: asyncio.Queue = asyncio.Queue()
    for i, acc in enumerate(accounts, 1):
        queue.put_nowait((i, len(accounts), acc))

    async with async_playwright() as p:
        tasks = []
        for i in range(num_workers):
            proxy_cfg, rotate_url = worker_proxies[i]
            tasks.append(asyncio.create_task(
                worker(str(i + 1), queue, p, proxy_cfg, rotate_url, use_headless)
            ))
        await asyncio.gather(*tasks)

    # Summary
    if OUTPUT_FILE.exists():
        results = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
        success = sum(1 for r in results if r["status"] == "success")
        print(f"\n[*] Done: {success}/{len(results)} successful")
    print(f"[*] Results saved to {OUTPUT_FILE}")

    if _store:
        _store.clear_reg_accounts()
        _store.set_reg_status(False)


if __name__ == "__main__":
    asyncio.run(main())
