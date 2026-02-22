import urllib.request, json

BASE = "http://localhost:8083"

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

# Step 1: Update vision settings to use the new API
print("=== Step 1: Configure Vision with gpt-5-codex-mini ===")
result = put(BASE + "/api/settings", {
    "vision": {
        "enabled": True,
        "model": "gpt-5-codex-mini",
        "api_key": "sk-cliproxy-key-1",
        "upstream_url": "http://157.66.100.102/v1/chat/completions",
        "prompt": "Please describe this image in detail, including all visible text, objects, colors, and context.",
    }
})
v = result["vision"]
print("enabled:", v["enabled"])
print("model:", v["model"])
print("upstream:", v["upstream_url"])

# Step 2: Test with a real image URL (a simple public image)
print("\n=== Step 2: Test vision with image URL ===")
anthropic_request = {
    "model": "glm-4.7",
    "max_tokens": 300,
    "messages": [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "url",
                        "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/4/47/PNG_transparency_demonstration_1.png/280px-PNG_transparency_demonstration_1.png",
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
    resp = post(BASE + "/v1/messages", anthropic_request)
    content = resp.get("content", [])
    text = next((c["text"] for c in content if c.get("type") == "text"), "")
    print("Response:", text[:300])
    print("Stop reason:", resp.get("stop_reason"))
    print("\nVision test PASSED!")
except Exception as e:
    print("Error:", e)

# Step 3: Disable vision (restore)
print("\n=== Step 3: Disable vision (restore default) ===")
put(BASE + "/api/settings", {"vision": {"enabled": False}})
print("Vision disabled.")
