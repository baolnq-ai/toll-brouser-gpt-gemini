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