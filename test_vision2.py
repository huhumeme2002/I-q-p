"""Test vision with GPT models at http://157.66.100.102/"""
import urllib.request
import urllib.error
import json
import base64

API_URL = "http://157.66.100.102/v1/chat/completions"
API_KEY = "sk-cliproxy-key-1"

def call_vision(model, image_input, question, timeout=45):
    """image_input: either {'url': '...'} or {'base64': '...', 'media_type': 'image/jpeg'}"""
    if "url" in image_input:
        img_content = {"type": "image_url", "image_url": {"url": image_input["url"]}}
    else:
        data_url = f"data:{image_input['media_type']};base64,{image_input['base64']}"
        img_content = {"type": "image_url", "image_url": {"url": data_url}}

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": [
            img_content,
            {"type": "text", "text": question}
        ]}],
        "max_tokens": 200
    }
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"},
        method="POST"
    )
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        resp = json.loads(r.read())
        text = (resp.get("choices") or [{}])[0].get("message", {}).get("content", "")
        return "OK", text
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:200]
        return f"HTTP {e.code}", body
    except Exception as e:
        return type(e).__name__, str(e)[:100]


# Download a real image and encode as base64
print("=== Downloading test image ===")
# Use Baidu logo (accessible from China)
test_urls = [
    "https://www.baidu.com/img/flexible/logo/pc/result.png",
    "https://img.alicdn.com/imgextra/i1/O1CN01bHdrZE1rSpt9E5nXz_!!6000000005630-2-tps-200-200.png",
    "https://gw.alipayobjects.com/zos/rmsportal/KDpgvguMpGfqaHPjicRK.svg",
]

img_b64 = None
img_type = "image/png"
for test_url in test_urls:
    try:
        req = urllib.request.Request(test_url, headers={"User-Agent": "Mozilla/5.0"})
        img_data = urllib.request.urlopen(req, timeout=10).read()
        img_b64 = base64.b64encode(img_data).decode()
        print(f"Downloaded: {test_url} ({len(img_data)} bytes)")
        break
    except Exception as e:
        print(f"Failed {test_url}: {e}")

if not img_b64:
    print("Using fallback: hardcoded small PNG")
    # A valid 16x16 red PNG
    img_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAHklEQVQ4T2P8z8BQDwAEgAF/"
        "QualIQAAAABJRU5ErkJggg=="
    )
    img_type = "image/png"

print(f"\nBase64 length: {len(img_b64)} chars")

# Test models
models_to_test = ["gpt-5", "gpt-5.1", "gpt-5.2", "gpt-5.1-codex", "gpt-5.3-codex"]
question = "Describe what you see in this image in 1-2 sentences."

print("\n=== Testing vision with base64 image ===")
for model in models_to_test:
    status, text = call_vision(model, {"base64": img_b64, "media_type": img_type}, question)
    print(f"\n[{model}] Status: {status}")
    if status == "OK":
        print(f"  Response: {text[:200]}")
    else:
        print(f"  Error: {text[:150]}")
