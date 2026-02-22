# Auto-Continue Feature — Bug Fix

## Root Cause
Model (GLM-4.7) at 86+ messages returns `completion_tokens=18` but `response_text=''` (empty).
The 18 tokens are thinking/overhead tokens, NOT actual text content.
Retrying with empty assistant message (`content_len=0`) only makes context longer and model more stuck.

## Fixes Applied

- [x] 1. **Skip auto-continue when response_text is empty** (streaming path)
  - `[AutoContinue:STUCK]` log when model returns tokens but no text
  - Flushes buffered response to client instead of retrying
  - File: `proxy.py` — streaming `gen()` function

- [x] 2. **Skip auto-continue when response_text is empty** (non-streaming path)
  - Same `[AutoContinue:STUCK]` detection for non-streaming responses
  - Passes through to client instead of retrying
  - File: `proxy.py` — non-streaming while loop

- [x] 3. **Don't append empty assistant messages**
  - `_build_continue_body()` now skips assistant message if `assistant_text` is empty
  - Prevents confusing the model with `{"role": "assistant", "content": ""}`

- [x] 4. **Escalating nudge messages**
  - 3 levels: gentle → strong → forceful
  - Each retry uses progressively stronger language
  - Custom message from settings overrides escalation

- [x] 5. **Pass attempt number to `_build_continue_body()`**
  - Both streaming and non-streaming paths now pass `attempt=ac_attempt`

## Log Messages to Watch
- `[AutoContinue:STUCK]` — Model is truly stuck (context too long), no retry
- `[AutoContinue:TRIGGER]` — Lazy response detected, will retry
- `[AutoContinue:SKIP]` — Conditions not met for auto-continue
- `[AutoContinue:CHECK]` — Diagnostic info after each response

## Next Steps
- [ ] Restart proxy server to pick up changes
- [ ] Test with `DEBUG_REQUESTS=1` to verify STUCK detection works
- [ ] Consider adding context compaction for long conversations (future)
