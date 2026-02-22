# iFlow Proxy — Fix Checklist

## ✅ Đã hoàn thành (Round 1)

### 🔴 Critical (Security & Data Integrity)
- [x] 1. Fix `/debug/raw` endpoint không có auth protection
- [x] 2. Fix OAuth credentials bị duplicate trong reg_iflow.py
- [x] 3. Fix race condition trong store.py (lock không bao toàn bộ read-modify-write)
- [x] 4. Fix admin password lưu plain text + cookie chứa password trực tiếp
- [x] 16. Fix `admin.html` — JS overwrite session cookie bằng plaintext password sau khi set password
- [x] 17. Fix `store.get_active_provider()` không tồn tại → AttributeError runtime tại `GET /api/provider`
- [x] 18. Fix `pick_pool_proxy()` + `inc_pool_proxy_assigned()` không atomic → race condition khi nhiều workers

### 🟠 High (Performance & Reliability)
- [x] 5. Fix disk I/O bottleneck (3-4 lần đọc/ghi JSON mỗi request)
- [x] 6. Fix `_rr_index` không thread-safe
- [x] 7. Fix vision cache FIFO → LRU thực sự (dùng OrderedDict + move_to_end)
- [x] 8. Fix proxy health check phụ thuộc httpbin.org
- [x] 9. Fix `process_vision_in_messages` không handle lỗi từng image
- [x] 19. Fix `asyncio.get_event_loop()` deprecated → `get_running_loop()`
- [x] 20. Fix `store._read()` silent data loss khi `data.json` bị corrupt
- [x] 21. Fix non-streaming Qwen 401 refresh không track `error_count`
- [x] 22. Fix `reg_iflow.py` dùng `threading.Lock()` trong async context
- [x] 23. Fix `accounts.txt` chứa plaintext passwords không bị xóa sau registration

### 🟡 Medium (Code Quality)
- [x] 10. Fix `messages()` quá lớn — extract helpers
- [x] 11. Fix `count_tokens` inaccurate
- [x] 12. Fix `delete_account` proxy pairing logic fragile
- [x] 24. Fix `_csrf_tokens.pop()` xóa token ngẫu nhiên → dùng deque
- [x] 25. Fix vision cache key collision với ảnh cùng base64 header
- [x] 26. Fix log hiển thị model từ request body thay vì model thực sự forward
- [x] 27. Fix Qwen auto-import không lưu `resource_url`

### 🔵 Low (Minor)
- [x] 13. Fix CSRF protection trên admin API
- [x] 14. Fix `saveHeadless()` dùng localStorage → persist lên server
- [x] 15. Fix logging handler configuration
- [x] 28. Fix `reg_iflow.py` email interpolated vào JS string → injection

---

## 🆕 Round 2 — Bugs & Improvements

### 🔴 Critical (Bugs)
- [x] 29. Fix non-streaming iFlow 401 không gọi `finalize_request` → error bị drop khỏi log/stats (`proxy.py:~1183`)
- [x] 30. Fix `data.json` ghi bằng `write_text` không atomic trên Windows → corrupt nếu crash giữa chừng (`store.py:81`) — dùng write-to-temp + `os.replace()`
- [x] 31. ~~Fix `aiohttp` dùng trong `reg_iflow.py` nhưng không có trong `requirements.txt`~~ — `aiohttp` đã có sẵn trong requirements.txt, không phải bug

### 🟠 High (Reliability & Security)
- [x] 32. Fix `inc_account_request` gọi trước khi request thành công → account bị tính sai khi retry sang account khác (`proxy.py:~1082`)
- [x] 33. ~~Fix `stream_anthropic_sse` emit empty text block khi response chỉ có tool calls~~ — không phải bug: điều kiện `not tool_block_anthropic_index` đã ngăn emit khi có tool blocks
- [x] 34. Fix CSRF token không có TTL → token cũ valid mãi mãi, chỉ evict theo số lượng
- [x] 35. Fix `ADMIN_PASSWORD` env var so sánh bằng `==` thay vì `hmac.compare_digest()` → timing attack (`store.py:~568`)
- [x] 36. Fix `_admin_sessions` dict không bị cleanup → memory leak dài hạn
- [x] 37. Fix `reg_proxies.json` không bị xóa sau registration (chỉ `accounts.txt` được xóa)
- [x] 38. Fix per-request SOCKS5 `httpx.AsyncClient` không có connection pooling → mỗi request mở TCP mới (`proxy.py:~1084`)

### 🟡 Medium (Features & UX)
- [ ] 39. Add per-account model override cho iFlow accounts (Qwen đã có `qwen_model`, iFlow thì không)
- [ ] 40. Fix `proxy_url` global không expose trong admin UI Settings — chỉ sửa được bằng tay trong `data.json`
- [ ] 41. Fix dashboard hiển thị `ANTHROPIC_AUTH_TOKEN` sai → đúng phải là `ANTHROPIC_API_KEY` (`admin.html:~493`)
- [ ] 42. Fix dedup khi add reg accounts — có thể queue cùng email 2 lần
- [ ] 43. Fix Log auto-refresh 5s kể cả khi idle → tốn tài nguyên không cần thiết
- [ ] 44. Fix `EventSource` log stream không gửi được auth header → live log fail nếu có password

### 🔵 Low (Minor)
- [ ] 45. Add export/backup `data.json` từ admin UI
- [ ] 46. Fix thay đổi port không có warning "cần restart" trong UI
- [ ] 47. Fix password trong reg queue bị mask ở UI nhưng vẫn là plaintext trong DOM/API response
- [ ] 48. Add hiển thị `last_used` trong bảng API Keys để biết account nào đang được dùng
- [ ] 49. Optimize SHA-256 hash toàn bộ base64 image trên mỗi cache lookup — dùng hash first+last N bytes thay vì toàn bộ
