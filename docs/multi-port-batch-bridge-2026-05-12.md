# Multi-port Batch Bridge - 2026-05-12

## Tong quan
Task da hoan thanh cho bridge API o `examples/apps/gemini-use/server.py` voi cac thay doi chinh:
- Open endpoint ho tro nhan list ports va mo dong thoi Gemini/GPT URL (theo `.env.example`).
- Chat/Image endpoint ho tro `prompt` dang list de batch trong 1 request.
- Batch chay song song theo pool port dang available.
- Co scheduler theo port: per-port lock + cooldown queue khi gap rate-limit.
- Them failover co kiem soat cho nhom loi transient (timeout/CDP) de tang on dinh runtime.
- Timeout mac dinh doi thanh 600s cho Gemini/GPT.
- Image endpoint ho tro `response_format=binary` de tra bytes anh thay vi chi base64.

## API cap nhat
### 1) `POST /v1/web/open`
Payload mo rong:
- `providers`: `['gemini','gpt']` (optional, mac dinh mo ca 2)
- `port` hoac `ports`
- `url` (optional, neu bo trong dung URL trong env)

Response mo rong:
- `results`: danh sach ket qua theo tung `(provider, port)`
- `active_ports`: cac port dang active sau khi open

### 2) `POST /v1/chat/{provider}`
Payload:
- `prompt` (bat buoc la list chuoi)
- `timeout_s`

Behavior:
- Batch prompts duoc xu ly song song theo so port kha dung.
- Khi 429/rate-limit: danh dau cooldown port va doi port khac.
- Queue lock per-port de tranh spam cung port dang ban.

### 3) `POST /v1/image/{provider}`
Payload:
- `prompt` (bat buoc la list chuoi)
- `response_format`: `json` | `binary`
- `max_images`, `timeout_s`

Behavior:
- `json`: tra ket qua anh (co `base64_data`).
- `binary`: tra truc tiep bytes cua 1 anh thanh cong dau tien.

## Env va runtime cap nhat
`.env.example` da bo sung:
- `CHAT_BRIDGE_DISCOVERY_PORTS=9222,9223,9224`
- `GEMINI_OPEN_URL=https://gemini.google.com/app`
- `GPT_OPEN_URL=https://chatgpt.com/`
- `GEMINI_DEFAULT_TIMEOUT_S=600`
- `GPT_DEFAULT_TIMEOUT_S=600`
- `CHAT_BRIDGE_RATE_LIMIT_COOLDOWN_S=45`
- `CHAT_BRIDGE_MAX_BATCH_PROMPTS=24`

`setup.sh` cap nhat phan usage de hien thi cac bien moi.

## Testing da thuc hien
### Tu dong
Command:
- `uv run pytest -q tests/ci/test_gemini_bridge_batch.py`

Ket qua:
- 4 tests passed
- Cover: prompt parsing, open multi-port, batch rate-limit failover, image binary response

### Request that (live)
Server chay tren `GEMINI_API_PORT=8010`.
Da goi thanh cong:
- `POST /v1/web/open` voi ports `[9222,9223,9224]` va providers `['gemini','gpt']`
- `POST /v1/chat/gemini` voi 3 prompts (phan bo qua 3 ports)
- `POST /v1/chat/gpt` voi 2 prompts (song song, thanh cong)
- `POST /v1/image/gemini` voi `response_format=binary` (tra `image/png` bytes)

## Kho khan gap phai va cach xu ly
1. Loi bind port 8008 do da co process khac
- Xu ly: chay runtime test o `8010`.

2. 1 truong hop GPT timeout tren test that truoc khi harden
- Xu ly: bo sung failover co kiem soat cho nhom loi transient (`REQUEST_TIMEOUT`, `CDP_*`) de retry sang port khac.

3. Refactor lon de bo lock toan cuc ma van tranh spam port
- Xu ly: tao `PortScheduler` (per-port lock + cooldown + round-robin selection).

## Luu y compatibility
- Van giu duong dan endpoint cu.
- Van chap nhan mode single prompt (`prompt`) nhu truoc.
- Da them cac field moi (`results`, `used_port`) de observable hon khi batch.
