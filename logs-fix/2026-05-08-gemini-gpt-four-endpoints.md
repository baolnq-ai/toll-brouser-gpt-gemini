# 2026-05-08 Gemini GPT Four Endpoints

## Issue

Backend bridge had extra public routes, image delivery depended on a static file endpoint, and image persistence could block runtime behavior during generation/download.

## Fix

- reduced public API to exactly 4 endpoints:
  - `POST /v1/chat/gemini`
  - `POST /v1/chat/gpt`
  - `POST /v1/image/gemini`
  - `POST /v1/image/gpt`
- removed public `/health`, `/health/gpt`, `/docs`, `/openapi.json`, `/v1/chat`, `/v1/image`, and `/generated-assets/*`
- changed image responses to return inline `base64_data` instead of requiring a static download route
- kept deterministic Chrome/CDP automation and request locking
- kept multi-image handling and dedupe logic in the image pipeline

## Validation

- `./.venv/Scripts/python.exe -m py_compile examples/apps/gemini-use/server.py`
- smoke test via repo-local `.venv` against running server:
  - `POST /v1/chat/gemini` -> `200`, answer `OK`
  - `POST /v1/chat/gpt` -> `200`, answer `OK`
  - `POST /v1/image/gemini` -> `200`, inline base64 image returned
  - `POST /v1/image/gpt` -> `200`, inline base64 image returned
- confirmed legacy public routes return `404`

## Docs Regression Fix

- issue: FastAPI docs were unreachable because `docs_url` and `openapi_url` were explicitly disabled during the public-surface cleanup.
- root cause: app config in `examples/apps/gemini-use/server.py` set both routes to `None`, so `/docs` and `/openapi.json` always returned `404`.
- fix: re-enabled `/docs` and `/openapi.json` while keeping the four Gemini/GPT business endpoints unchanged.
- validation:
  - `./.venv/Scripts/python.exe -m py_compile examples/apps/gemini-use/server.py`
  - runtime check: `GET /docs` -> `200`
  - runtime check: `GET /openapi.json` -> `200`
  - re-smoke the 4 business endpoints after restart

## 2026-05-08 Extra Hardening (Quota/Limit Case)

- expanded `RATE_LIMIT_KEYWORDS` in `examples/apps/gemini-use/server.py` to catch more real-world quota texts, including image-limit specific phrases like:
  - `image creation limit`
  - `upgrade to plus`
  - `exceeded your current quota`
  - `resource has been exhausted`
- this ensures GPT/Gemini image limit failures map to structured `*_RATE_LIMIT` with HTTP `429` instead of generic error paths.

### Live Smoke (real requests, port 8010)

- `POST /v1/chat/gemini` -> `200`, `success=true`
- `POST /v1/chat/gpt` -> `504`, `error_code=GPT_RESPONSE_TIMEOUT` (handled timeout response, no crash)
- `POST /v1/image/gemini` -> `200`, `success=true`
- `POST /v1/image/gpt` -> `429`, `error_code=GPT_RATE_LIMIT` (limit reached path handled correctly)

### Notes

- ChatGPT text endpoint can still timeout depending on active tab/session state, but the failure is returned as structured JSON (`GPT_RESPONSE_TIMEOUT`) and server remains stable.
- GPT image-limit scenario is now reliably surfaced as `GPT_RATE_LIMIT` with HTTP `429`.

## 2026-05-08 Timeout Hardening (stream/image wait)

- strengthened stream wait logic in `examples/apps/gemini-use/server.py`:
  - snapshot now includes `responseTextsTail` (last 4 assistant texts) instead of relying only on one last text.
  - `_wait_for_answer` now tracks response signatures (last 1-2 messages), supports multi-answer flows, and applies a short post-stream grace window (`STREAM_SETTLE_GRACE_SECONDS`) to avoid false timeout right after stream stops.
  - if timeout boundary is reached but a stable/usable answer was already observed, it returns that answer instead of failing hard.

- strengthened image wait logic:
  - `_wait_for_images` now receives baseline candidates and `desired_count` (`max_images`) and filters only newly generated images versus baseline.
  - supports waiting for `2` images when requested (`max_images=2`) while still allowing graceful completion if stream ended with fewer images.
  - applies the same post-stream grace window to reduce race-condition timeouts.

- quota/limit handling remains explicit (`*_RATE_LIMIT`, HTTP `429`) for both chat/image flows when UI surfaces limit text.
