# Context Window Management Feature

## Steps

- [x] 1. `store.py` — Add context_window settings (DEFAULT_DATA, getter, update_settings)
- [x] 2. `proxy.py` — Add `_truncate_context(body)` function with smart message trimming
- [x] 3. `proxy.py` — Wire `_truncate_context()` into `/v1/messages` endpoint
- [x] 4. `static/admin.html` — Add Context Window settings UI card
- [x] 5. `data.json` — Enable context_window with defaults
- [x] 6. Git push

## Design Notes
- Always keep: system prompt + last 20 messages
- Trim oldest messages first, keeping tool_use/tool_result pairs together
- Insert truncation notice at cut point
- Fast path: skip if total tokens < max_tokens (no deepcopy)
- Log [ContextWindow] with before/after stats
