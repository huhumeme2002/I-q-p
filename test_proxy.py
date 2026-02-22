import httpx
import time
import asyncio
import json

import uuid as _uuid

API_KEY = "sk-39901292f8ac61222790a63c0514452e"
UPSTREAM = "https://apis.iflow.cn/v1/chat/completions"

def make_headers():
    return {
        "accept": "*/*",
        "authorization": f"Bearer {API_KEY}",
        "content-type": "application/json",
        "user-agent": "iFlow-Cli",
        "conversation-id": _uuid.uuid4().hex,
        "session-id": f"session-{_uuid.uuid4()}",
    }

MODELS_TO_TEST = ["glm-4.7", "deepseek-v3.2-chat"]

# Replicate exact proxy request format
BODY_TEMPLATE = {
    "messages": [{"role": "user", "content": "Say hi in 3 words"}],
    "max_new_tokens": 32000,
    "stream": False,
    "temperature": 1,
    "top_p": 0.95,
    "chat_template_kwargs": {"enable_thinking": True},
}

PROXIES = [
    ("Proxy 1 :52060", "socks5://u_USER_YFPmKO:0VlCB57YBwPR@dc-t3.proxyvt.com:52060"),
    ("Proxy 2 :42943", "socks5://u_USER_YFPmKO:0VlCB57YBwPR@dc-t3.proxyvt.com:42943"),
    ("No proxy (direct)", None),
]


async def test_model(client, model):
    body = {**BODY_TEMPLATE, "model": model}
    t0 = time.time()
    try:
        r = await client.post(UPSTREAM, headers=make_headers(), json=body)
        elapsed = time.time() - t0
        data = r.json()
        if "choices" in data:
            text = data["choices"][0]["message"]["content"][:50]
            return f"[OK] {model}: {elapsed:.2f}s | {text}"
        else:
            msg = data.get("msg", json.dumps(data)[:80])
            return f"[ERR] {model}: {elapsed:.2f}s | {msg}"
    except Exception as e:
        elapsed = time.time() - t0
        return f"[FAIL] {model}: {elapsed:.2f}s | {type(e).__name__}: {str(e)[:60]}"


async def test_proxy(name, proxy_url):
    print(f"\n[{name}]")
    kwargs = {"timeout": 20.0}
    if proxy_url:
        kwargs["proxy"] = proxy_url
        kwargs["http2"] = False
    else:
        kwargs["http2"] = True  # same as global proxy client
    try:
        async with httpx.AsyncClient(**kwargs) as c:
            for model in MODELS_TO_TEST:
                result = await test_model(c, model)
                print(f"  {result}")
    except Exception as e:
        print(f"  Connection FAIL: {type(e).__name__}: {str(e)[:100]}")


async def main():
    print("=" * 60)
    print("  Testing SOCKS5 proxies + models with iFlow API")
    print("=" * 60)
    for name, proxy in PROXIES:
        await test_proxy(name, proxy)
    print("\n" + "=" * 60)


asyncio.run(main())
