# Auto-Continue Feature - Debug & Improvement

## Steps

- [x] 1. `store.py` — Auto_continue settings (DEFAULT_DATA, getter, update_settings)
- [x] 2. `proxy.py` — Helper functions (_should_auto_continue, _build_continue_body)
- [x] 3. `proxy.py` — Streaming buffer-based auto-continue logic
- [x] 4. `proxy.py` — Non-streaming auto-continue retry loop
- [x] 5. `static/admin.html` — Auto-Continue settings UI card
- [x] 6. `data.json` — auto_continue enabled
- [x] 7. `proxy.py` — Add diagnostic logging to _should_auto_continue()
- [x] 8. `proxy.py` — Add always-on logging after stream completes
- [x] 9. `proxy.py` — Improve token counting for iFlow streaming (effective_tokens)
- [ ] 10. Restart proxy and test with real coding agent session

## How to debug

After restarting proxy, watch logs for these entries:
- `[AutoContinue:CHECK]` — Appears after EVERY request (stream & non-stream)
- `[AutoContinue:SKIP]` — Shows exactly WHY auto-continue didn't trigger
- `[AutoContinue:TRIGGER]` — Shows when auto-continue IS triggered
- `[AutoContinue] Lazy response detected` — Confirms retry is happening

Set `DEBUG_REQUESTS=1` env var for even more detail.
