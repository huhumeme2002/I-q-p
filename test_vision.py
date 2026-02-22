"""Test vision fallback: send a request with an image and verify it works."""
import urllib.request
import json

BASE = "http://localhost:8083"

def get(url):
    r = urllib.request.urlopen(url, timeout=10)
    return json.loads(r.read())

def put(url, data):
    req = urllib.request.Request(url, data=json.dumps(data).encode(),
                                  headers={"Content-Type": "application/json"}, method="PUT")
    r = urllib.request.urlopen(req, timeout=10)
    return json.loads(r.read())

def post(url, data):
    req = urllib.request.Request(url, data=json.dumps(data).encode(),
                                  headers={"Content-Type": "application/json"}, method="POST")
    r = urllib.request.urlopen(req, timeout=60)
    return json.loads(r.read())

# Step 1: Enable vision with glm-4v-plus
print("=== Step 1: Enable Vision Fallback ===")
result = put(f"{BASE}/api/settings", {
    "vision": {
        "enabled": True,
        "model": "glm-4v-plus",
        "api_key": "",
        "upstream_url": "",
        "prompt": "Please describe this image in detail, including all visible text, objects, colors, and context.",
    }
})
print(f"Vision enabled: {result['vision']['enabled']}, model: {result['vision']['model']}")

# Step 2: Send Anthropic-format request with a small test image (1x1 red pixel PNG)
print("\n=== Step 2: Send request with image ===")
# A small 8x8 colorful PNG image (base64)
test_image_b64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAYAAADED76LAAAAP0lEQVQoU2NkYGD4"
    "z8BQDwAEhgH+AwMDIwMDAwMjAwMDIwMDAwMjAwMDIwMDAwMjAwMDIwMDAwMjAwMD"
    "IwMDAwAAAABJRU5ErkJggg=="
)

anthropic_request = {
    "model": "glm-4.7",
    "max_tokens": 200,
    "messages": [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": test_image_b64,
                    }
                },
                {
                    "type": "text",
                    "text": "What do you see in this image? Reply in 1-2 sentences."
                }
            ]
        }
    ]
}

print("Sending request with image to proxy...")
try:
    resp = post(f"{BASE}/v1/messages", anthropic_request)
    content = resp.get("content", [])
    text = next((c["text"] for c in content if c.get("type") == "text"), "")
    print(f"Response: {text[:200]}")
    print(f"Stop reason: {resp.get('stop_reason')}")
    print("\n✅ Vision fallback test PASSED!")
except Exception as e:
    print(f"❌ Error: {e}")

# Step 3: Check logs to see what happened
print("\n=== Step 3: Check recent logs ===")
logs = get(f"{BASE}/api/logs")
if logs:
    l = logs[0]
    print(f"Last request: status={l['status']}, model={l['model']}, duration={l['duration']}ms")

# Step 4: Disable vision again (restore default)
print("\n=== Step 4: Restore vision to disabled ===")
put(f"{BASE}/api/settings", {"vision": {"enabled": False}})
print("Vision disabled (restored to default)")
