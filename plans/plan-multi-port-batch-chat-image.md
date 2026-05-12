# Plan: Multi-port Open + Batch Chat/Image + Port Failover Queue

## 1) Muc tieu task
- Ho tro mo nhieu CDP port trong endpoint open bang cach gui list port, va tu dong mo san Gemini/GPT URL theo cau hinh `.env.example`.
- Dat timeout mac dinh cho endpoint chat/image la 10 phut (600s).
- Ho tro gui nhieu prompt trong 1 request cho endpoint chat/image, xu ly song song theo so port da mo.
- Co co che chuyen port khi gap rate limit/quota, kem hang doi de tranh spam vao port dang ban.
- Endpoint image ho tro tra ve 1 anh binary truc tiep (khong chi base64).
- Test ky voi request that, khong smoke test qua loa.

## 2) Pham vi code
- File backend chinh: `examples/apps/gemini-use/server.py`.
- Runtime env va startup: `.env.example`, `setup.sh`.
- Test: bo sung test trong `tests/` cho scheduler/failover/batch behavior.
- Documentation task: `docs/`.
- Logging task process: `logs/`.

## 3) Skills can dung va cach ap dung
- `backend-skill`:
  - Refactor API schema, service orchestration, queue + failover, error taxonomy.
  - Dam bao clean code, dat ten ro rang, logic de bao tri.
- `testing-skill` (khong thay file skill rieng trong repo):
  - Ap dung bo tieu chi test nghiem ngat thay the: unit + integration request, concurrent run, failover, timeout, image binary.
  - Yeu cau pass truoc khi sang phase tiep theo.
- `documentation-skill`:
  - Ghi tong ket implementation va validation ket qua vao `docs/`.
- `logging-skill`:
  - Ghi log tien trinh theo moc thoi gian vao `logs/`.
- `frontend-skill`:
  - Khong ap dung truc tiep vi task backend API.

## 4) Phases thuc hien (lam lan luot, pass test moi qua phase)

### Phase A - Design va state model (du kien 45 phut)
- Doc lai backend-skill + plan constraints.
- Thiet ke state quan ly pool port:
  - Port registry (opened/active/cooldown/in-flight count).
  - Per-port lock de dam bao 1 session thao tac UI tai 1 thoi diem.
  - Cooldown queue khi port bi limit.
- Thiet ke request schema moi:
  - Open endpoint nhan list ports.
  - Chat/Image endpoint nhan `prompt` hoac `prompts`.
  - Image endpoint them che do tra binary.
- Deliverable: design duoc coding trong server.py, co comments ngan gon cho block kho.

### Phase B - Implement open multi-port + defaults (du kien 60 phut)
- Sua endpoint open de ho tro list ports va auto open URL cho ca Gemini/GPT dua tren `.env.example`.
- Dam bao backend ghi nhan danh sach port da open.
- Dat default timeout chat/image = 600s.
- Update `.env.example` de co bien URL/port list ro rang.
- Update `setup.sh` neu can de tuong thich bien moi.
- Testing phase:
  - Open 3 port 9222/9223/9224 that su, kiem tra ping/status active.

### Phase C - Implement batch parallel + scheduler failover (du kien 120 phut)
- Tao scheduler phan bo prompt theo danh sach port active.
- Chay song song theo muc do song song = so port kha dung.
- Khi gap rate limit/429:
  - Danh dau port cooldown trong 1 khoang thoi gian.
  - Thu lai prompt tren port khac con trong.
- Co hang doi de tranh dồn request vao port vua ranh ngay lap tuc.
- Dam bao khong fallback bay ba:
  - Retry co gioi han.
  - Khong nuot loi; bao cao ro prompt nao fail.
- Testing phase:
  - Batch chat voi nhieu prompts.
  - Gia lap/thu that truong hop rate limit tren mot port va verify rotate port.

### Phase D - Image binary response + strict validation (du kien 75 phut)
- Them endpoint/image mode tra binary 1 anh:
  - Chon anh dau tien thanh cong.
  - Tra ve `image/*` bytes kem headers metadata.
- Van giu JSON mode cho batch/nhieu anh.
- Testing phase:
  - Goi tao anh dang binary va verify file mo duoc.
  - Goi batch image prompts va verify ket qua tung prompt.

### Phase E - Test nghiem ngat + hardening (du kien 120 phut)
- Viet bo test tu dong cho scheduler/failover/queue parsing.
- Chay test command day du (unit + integration lien quan).
- Chay test request that bang server local (khong chi smoke):
  - open multi-port
  - batch chat
  - batch image
  - image binary
  - timeout behavior
- Sửa loi den khi on dinh.

### Phase F - Documentation + Logging + closeout (du kien 45 phut)
- Cap nhat doc task vao `docs/`:
  - Kien truc moi, API contract moi, env vars, huong test.
  - Kho khan va cach xu ly.
- Ghi log implementation theo thoi gian vao `logs/`.
- Quet lai CI/test impact va tong hop trang thai san sang.

## 5) Tai nguyen can thiet
- Chrome da login san tren ports: 9222, 9223, 9224.
- Python env/uv dependencies va FastAPI server runtime.
- Quyen ghi files: `plans/`, `docs/`, `logs/`, `tests/`.

## 6) Tieu chi hoan thanh
- Endpoint open nhan list ports va provider, ghi nhan active ports dung.
- Chat/Image ho tro 1 hoac nhieu prompt; batch chay song song theo pool port.
- Failover khi limit hoat dong, co cooldown queue chong spam port.
- Timeout default 600s.
- Image endpoint co mode tra binary 1 anh.
- Test that va test tu dong pass, khong fallback sai logic.
- Co file documentation + log task day du theo skill.