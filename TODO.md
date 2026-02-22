# Auto-Continue Feature Implementation

## Steps

- [ ] 1. `store.py` — Add auto_continue settings (DEFAULT_DATA, getter, update_settings)
- [ ] 2. `proxy.py` — Add helper functions (_should_auto_continue, _build_continue_body)
- [ ] 3. `proxy.py` — Modify `stream_anthropic_sse()` to accumulate response_text in usage_out
- [ ] 4. `proxy.py` — Implement auto-continue for NON-STREAMING path
- [ ] 5. `proxy.py` — Implement auto-continue for STREAMING path (threshold-based buffering)
- [ ] 6. `proxy.py` — Add API endpoint for auto_continue settings
- [ ] 7. `static/admin.html` — Add Auto-Continue settings UI card
- [ ] 8. Test & verify
