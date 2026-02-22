# Context Window Management Feature

## Steps

- [x] 1. `store.py` — Add context_window settings (DEFAULT_DATA, getter, update_settings)
- [x] 2. `proxy.py` — Add `_truncate_context(body)` function with smart message trimming + DEEP_TRIM
- [x] 3. `proxy.py` — Wire `_truncate_context()` into `/v1/messages` endpoint
- [x] 4. `static/admin.html` — Add Context Window settings UI card
- [x] 5. `data.json` — Enable context_window (max_tokens=16000, keep_recent_messages=10)
- [x] 6. Git push

## Design Notes
- Always keep: system prompt + last 10 messages (lowered from 20 — 20 msgs with tool calls easily exceed 16k)
- Trim oldest messages first, keeping tool_use/tool_result pairs together
- Insert truncation notice at cut point
- Fast path: skip if total tokens < max_tokens (no deepcopy)
- DEEP_TRIM: when protected messages + tools still exceed limit, trim protected from oldest (MIN_KEEP=4)
- Log [ContextWindow] with before/after stats, [ContextWindow:DEEP_TRIM] for aggressive trimming
- Auto-Continue: STUCK detection skips retry when model returns tokens but empty text
- Escalating nudge messages (gentle → strong → forceful) with attempt number
