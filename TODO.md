# Qwen Tool Call Error Fixes

## Steps

- [x] 1. `store.py` — Add `qwen_system_prompt` to DEFAULT_DATA + getter + update_settings
- [x] 2. `proxy.py` — Add `_repair_tool_json()` function (5-pass repair for malformed JSON from Qwen)
- [x] 3. `proxy.py` — Add `_apply_qwen_system_prompt()` function
- [x] 4. `proxy.py` — Wire `_apply_qwen_system_prompt()` into `anthropic_to_openai()` for Qwen provider
- [x] 5. `proxy.py` — Use `_repair_tool_json()` in `openai_to_anthropic()` instead of bare try/except
- [x] 6. `proxy.py` — Add UTF-8 surrogatepass normalization for tool arg chunks in `stream_anthropic_sse()`
- [x] 7. `static/admin.html` — Add "Qwen Tool Guidance" card UI + loadCfg/saveQwenPrompt/resetQwenPrompt JS

## ✅ COMPLETE

## Design Notes
- `_repair_tool_json`: 5-pass repair: direct parse → truncate at last `}`/`]` → strip control chars → remove trailing commas → combined
- `_apply_qwen_system_prompt`: prepend Qwen guidance to existing system prompt (only when provider=qwen)
- Default prompt guides: PowerShell syntax (`;` not `&&`), UTF-8 file encoding, exact search/replace matching, valid JSON tool args
- Admin UI: textarea to customize Qwen system prompt, toggle to enable/disable, "Reset to Default" button
- Encoding normalization: `encode("utf-8", errors="surrogatepass").decode("utf-8", errors="replace")` on streamed tool arg chunks
