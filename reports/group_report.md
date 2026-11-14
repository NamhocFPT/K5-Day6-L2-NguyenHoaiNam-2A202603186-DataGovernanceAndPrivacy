# Group Report — Day 06 Data Lab (Field-to-Release PoC)

> Số liệu dưới đây lấy từ file do script sinh ra (`out/*/actions.csv`, `out/poc/selfcheck.json`,
> `out/poc/request_decisions.csv`, `out/*/run_info.json`). Repo học viên **không có `gt/`** nên
> `reports/metrics_*.json` (Deletion Completeness chính thức) chỉ sinh được khi giảng viên chạy overlay —
> các con số "proxy" bên dưới là tự kiểm không cần nhãn và được ghi rõ là proxy.

## 00 · Header

| Mục | Giá trị |
| --- | --- |
| Group ID | G-2A202603186 (nhóm 1 người) |
| Challenge | T08 · Retention & Delete Request Simulator |
| Thành viên + vai trò | 1. Nguyễn Hoài Nam — Lead/Integrator, Data, Core algorithm, QA/metric, Review + report (làm một mình) |
| Git URL | <https://github.com/NamhocFPT/K5-Day6-L2-NguyenHoaiNam-2A202603186-DataGovernanceAndPrivacy> |
| Tag `demo-180m` (commit) | `87a0720` (2026-11-14 16:15 +07) |
| Tag `final-24h` (commit) | commit cuối khi nộp (xem `git show final-24h`) |
| Deadline (giờ VN) | 18h ( 2026-11-14) |

## 01 · Problem statement

- **Ai bị ảnh hưởng:** chủ thể dữ liệu (người lái/hành khách xuất hiện trong video) — nếu xoá thiếu, mặt và embedding nhận dạng của họ vẫn tồn tại; bên thứ ba có mặt chung frame — nếu xoá thừa, dữ liệu của họ bị mất; công ty — vi phạm nghĩa vụ lưu giữ khi xoá dữ liệu đang **legal hold**.
- **Điểm nghẽn:** một video gốc sinh ra backup, frame, face_crop, embedding, annotation và training_export. Xoá theo tên file chỉ chạm được raw.
- **Đầu vào → đầu ra:** `data_graph.json` (212 artefact mock) + `requests.json` (10 yêu cầu xoá) → `actions.csv` (request, artefact, action ∈ {delete, retain_legal_hold, escalate}) + `audit_log.jsonl` + `review_queue.csv` + `request_decisions.csv` + `decision.json`.
- **Tiêu chí GO / HOLD / REWORK (chốt trước khi chạy):**
  - **REWORK** nếu `selfcheck.json` có ≥1 vi phạm invariant (xoá dưới legal hold, xoá artefact nhiều subject chưa duyệt, xoá ngoài subject tag, dòng trùng).
  - **HOLD** nếu còn ≥1 dòng `escalate` chưa có người duyệt (chỉ request GO mới được thực thi).
  - **GO** khi mọi escalation đã được người duyệt; legal hold được giữ và hẹn xoá khi hết hold.
- **Giả định (cần DPO/legal xác nhận):** legal hold áp cho toàn bộ dẫn xuất của artefact bị hold; artefact chứa nhiều người không được xoá nguyên khối mà phải redact; dữ liệu không rõ nguồn gốc không tự xoá. Đây là tình huống học tập, **không** tuyên bố tuân thủ GDPR / Luật 91/2025.

## 02 · Data & provenance

| Split | N | Nguồn / link | License | Synthetic? |
|---|---|---|---|---|
| toàn bộ (fixture không chia split) | 212 artefact × 10 request → 164 cặp trong phạm vi | `fixtures/t08_deletion/` (synthetic: tools/make_fixtures.py, seed 42) | CC0-1.0 | có (mock, không có dữ liệu thật) |

- **Schema:** artefact có `type, subject_ids, parent_ids, uri, owner, acl, legal_hold, origin_known`; 7 loại: raw_video 15, backup 15, frame 45, face_crop 45, embedding 45, annotation 45, training_export 2. Trường hợp khó có sẵn: 2 raw legal hold (`LH-2026-03`, `LH-2026-07`), 3 embedding `origin_known=false`, 1 backup ACL `vendor-x`, 10 artefact nhiều subject.
- **Seed:** 42 (pipeline không dùng random — deterministic).
- **`manifest.csv` verified (sha256):** có — `python -m tools.verify_manifests`; pipeline dừng exit 2 nếu hash lệch (`strict_provenance: true`).
- **Sự cố provenance thật gặp phải:** clone trên Windows với `core.autocrlf` đổi LF→CRLF khiến 170 file text lệch hash ở cả 14 track; đã chuyển lại LF (chỉ khi hash khớp đúng giá trị gốc) và thêm `.gitattributes` (`fixtures/** -text`). Đây chính là ví dụ guard provenance hoạt động đúng.
- Dữ liệu thật: không dùng (đề yêu cầu không xoá dữ liệu thực).

## 03 · Research & design

- **Kỹ thuật chọn:** duyệt đồ thị lineage (BFS từ artefact gắn subject xuống mọi con cháu) + policy engine theo thứ tự ưu tiên. Lý do: quyền xoá phải bao phủ mọi bản sao và dẫn xuất (GDPR Art.17(1)), nhưng có ngoại lệ (Art.17(3); Luật 91/2025) — nên cần cả "tìm hết" lẫn "biết dừng".
- **Policy (version `t08-policy-v1`, trong `src/pipeline.py::RULES`, mỗi rule có căn cứ):**

| Ưu tiên | Rule | Điều kiện | Action |
| --- | --- | --- | --- |
| P1 | LEGAL_HOLD | artefact hoặc tổ tiên có `legal_hold` | retain_legal_hold |
| P2 | IDENTITY_UNVERIFIED | `identity_verified = false` | escalate |
| P3 | NO_DELETE_PERMISSION | ACL không có `data-platform` | escalate |
| P4 | UNKNOWN_ORIGIN | `origin_known = false` | escalate |
| P5 | NOT_SUBJECT_TAGGED | con cháu không gắn subject yêu cầu | escalate |
| P6 | MULTI_SUBJECT | chứa >1 subject | escalate |
| P7 | DELETE | còn lại | delete |

- **Data flow:** load (sha256) → lineage BFS + policy → review queue top-B=40 (escalate > legal hold) → reviewer CSV → override có kiểm soát + audit → self-check invariant → GO/HOLD/REWORK theo request và toàn run → dry-run plan / mock-delete receipts.
- **Thành phần offline:** chỉ pandas/json; không model, không network.
- **Trade-offs:** (1) giữ hold cho cả dẫn xuất → an toàn pháp lý nhưng có thể giảm completeness; (2) escalate artefact nhiều subject thay vì xoá → không xoá nhầm người khác nhưng đẩy việc redact cho người; (3) ưu tiên **false-delete = 0** hơn completeness vì xoá nhầm không đảo ngược được.

## 04 · Baseline vs PoC

- **Baseline** (`src/t08_baseline.py`): xoá đúng 1 raw_video có filename chứa `subject_id`; không truy lineage, không kiểm hold/ACL.
- **PoC** (`src/pipeline.py`, policy `t08-policy-v1`, `configs/lab.yaml`).
- Cùng input / seed 42 / offline: xác nhận.

**Bảng 1 · Baseline vs PoC (proxy không cần nhãn — số chính thức chờ `make eval-baseline eval-poc compare`)**

| Metric (unit) | Baseline | PoC | Δ & chú thích |
| --- | --- | --- | --- |
| Số artefact bị xoá (cặp request–artefact) | 10 | 102 | PoC phủ backup, frame, face_crop, embedding, annotation |
| Proxy completeness = baseline∩PoC-delete / PoC-delete | 8/102 = 0.078 | 102/102 (tham chiếu) | baseline bỏ sót 94 bản sao/dẫn xuất |
| Xoá artefact đang legal hold (false-delete chắc chắn) | **2** (DR-002 a0043 `LH-2026-03`, DR-005 a0099 `LH-2026-07`) | **0** | higher = tệ; mục tiêu 0 |
| Loại artefact bị xoá | raw_video 10 | face_crop 24, frame 21, embedding 21, annotation 21, raw_video 8, backup 7 | |
| Invariant violations (`selfcheck.json`) | n/a | 0 | |
| Runtime toàn run | 0.045 s (10 dòng) | 0.21 s (164 dòng, Windows 11, Intel i-series, Python 3.13) | |

## 05 · Evaluation protocol

- Primary: **Deletion Completeness** = cặp GT `delete` được pred `delete` / cặp GT `delete`; ghép theo (request_id, artefact_id). False-delete = exception bị xoá + xoá ngoài phạm vi GT. Xem `reports/metric_contract.md`.
- Không có threshold số; policy P1–P7 chốt 2026-11-14 trước khi có bất kỳ kết quả eval nào.
- Độ bất định: báo Wilson 95% CI cho completeness và báo theo từng request (N=10 request).
- Lệnh: `python -m src.evaluate --track t08 --pred out/poc/actions.csv --run-name poc --out reports/metrics_poc.json` (cần overlay gt/).

## 06 · Result slices

| Slice (type) | N trong phạm vi | delete | escalate | retain_legal_hold |
| --- | --- | --- | --- | --- |
| raw_video | 10 | 8 | 0 | 2 |
| backup | 10 | 7 | 1 | 2 |
| frame | 33 | 21 | 6 | 6 |
| face_crop | 33 | 24 | 3 | 6 |
| embedding | 33 | 21 | 6 | 6 |
| annotation | 33 | 21 | 6 | 6 |
| training_export | 12 | 0 | 0 | 12 |

Theo request (`request_decisions.csv`): DR-002 (subj_03) và DR-005 (subj_07) giữ toàn bộ 15/16 artefact do legal hold; DR-004 (subj_05) có nhiều exception nhất (7 escalate: 3 UNKNOWN_ORIGIN, 2 MULTI_SUBJECT, 2 NOT_SUBJECT_TAGGED).

- **Failure case 1 — xoá nhầm dữ liệu legal hold (baseline):** baseline xoá `a0043` (`drive_subj_03_003.mp4`, hold `LH-2026-03`) và `a0099` (`LH-2026-07`). Root cause: chỉ khớp filename, không đọc metadata `legal_hold`. PoC: P1 giữ lại + hold lan xuống mọi dẫn xuất.
- **Failure case 2 — chỉ xoá raw, còn bản sao và embedding định danh:** với DR-001 (subj_01) baseline chỉ xoá raw `a0015`, để lại backup `a0016` và các face_crop/embedding. Root cause: không truy lineage. PoC: BFS qua `parent_ids`.
- **Failure case 3 — giới hạn của PoC:** 3 embedding `a0075/a0079/a0083` (subj_05) có `origin_known=false` → PoC không xoá; nếu thực tế chúng đúng là của subj_05 thì completeness giảm. Đánh đổi có chủ đích để giữ false-delete = 0.
- **Failure case 4 — training_export:** 2 bộ export gom nhiều subject, có tổ tiên đang hold → bị giữ cho mọi request. Cần quy trình rebuild export không có chủ thể yêu cầu xoá.

## 07 · Human & decision

- **Review queue:** `out/poc/review_queue.csv`, B=40, ưu tiên theo score: NO_DELETE_PERMISSION 0.95 > UNKNOWN_ORIGIN 0.90 > MULTI_SUBJECT 0.70 > NOT_SUBJECT_TAGGED 0.60 > LEGAL_HOLD 0.50 > DELETE 0.10. Cả 22 escalation nằm trong queue; 18/40 legal hold được xác nhận (22 còn lại tự động giữ).
- **Reviewer:** Nguyễn Hoài Nam, 40/40 dòng `approve` (xác nhận *không xoá* các ngoại lệ) kèm ghi chú lý do theo nhóm. Không dòng nào cần `edit` vì mọi đề xuất của máy ở đây đều là hướng an toàn (không xoá). Hạn chế: chỉ có 1 người duyệt — thực tế nên có người duyệt thứ hai cho các ca MULTI_SUBJECT và UNKNOWN_ORIGIN.
- **Override + audit:** `edit/reject` + `override_value` đổi action; mọi thay đổi ghi `out/poc/audit_log.jsonl` (actor, before/after, lý do). Legal hold **không thể** bị override thành delete → ghi `override_blocked` (có test). Thiếu `reviewer` → pipeline dừng exit 2 (có test).
- **Decision:** trước review: **HOLD** `ESCALATIONS_PENDING_REVIEW` (7/10 request còn escalation); sau review: **GO** `DELETION_PLAN_APPROVED`, owner Nguyễn Hoài Nam (Lead / Data Owner). Per-request: 9 `GO_PARTIAL_LEGAL_HOLD`, 1 `GO_EXCEPTIONS_REVIEWED`.
- **Khi nào không tự động:** còn escalation chưa duyệt (HOLD), vi phạm invariant (REWORK); mặc định `dry_run: true` — chỉ ghi `deletion_plan.csv`, muốn mock-delete phải đặt `dry_run: false`.

## 08 · Reproduce & tests

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m src.main --track t08 --pipeline src.t08_baseline --output out/baseline --run-name baseline --offline --seed 42
.venv\Scripts\python -m src.main --config configs/lab.yaml --track t08 --output out/poc --run-name poc --offline --seed 42
.venv\Scripts\python -m pytest -q
```

- Tests của nhóm (`tests/test_t08.py`, 5 test): E2E + idempotency (chạy 2 lần cùng `actions.csv`), precedence unit test, review thiếu reviewer → exit 2, legal hold không bị override, input bị sửa → exit 2.
- `pytest -q`: 0 failed (các test cần gt/ bị skip).
- Máy: Windows 11 (10.0.26200), Intel64 Family 6 Model 154, Python 3.13.17 · git sha: `87a0720` (demo), commit cuối = tag `final-24h`

## 09 · Limitations & ethics

- Mock data, không xoá dữ liệu thật; không tuyên bố đạt GDPR / Luật 91/2025 / NĐ 356/2025. Cần DPO/legal xác nhận: phạm vi lan của legal hold, cách xử lý artefact nhiều chủ thể (redact vs xoá), thời hạn phản hồi.
- Chưa biết: GT có coi dẫn xuất của artefact bị hold là `retain_legal_hold` hay `delete` không — nếu là `delete`, completeness của PoC giảm ở DR-002/DR-005.
- Scale 10×: BFS O(V+E) vẫn nhanh; nút thắt là hàng đợi review (22 → ~220 escalation) → cần ưu tiên theo deadline và tự động hoá redact.
- Cải tiến: SLA dashboard theo deadline, resume job bị ngắt, tách hold theo phạm vi vụ việc, rebuild training_export, gửi yêu cầu xoá tới vendor (giống mô hình California DROP).

## 10 · Team contribution

| Học viên | Git author/email | Module / vai trò | Commit SHA / diff chứng minh | Trình bày? |
|---|---|---|---|---|
| Nguyễn Hoài Nam | Nguyen Hoai Nam <hoainguyennam10@gmail.com> | Toàn bộ: config, pipeline, baseline, tests, review, report | `87a0720` (pipeline/baseline/tests/report) + commit hoàn thiện report | có |

## 11 · References

| URL | Ngày xem | Ý nghĩa |
| --- | --- | --- |
| <https://eur-lex.europa.eu/eli/reg/2016/679/oj> | 2026-11-14 | GDPR Art.17: quyền xoá và các ngoại lệ (nghĩa vụ pháp lý, khiếu nại) → cơ sở cho P1 và P7 |
| docs/tracks/T08.md (handout) | 2026-11-14 | Gợi ý BFS lineage, precedence legal hold > permission > multi-subject > delete |
| Luật BVDLCN 91/2025, NĐ 356/2025 (Research Shelf trong handout) | 2026-11-14 | Ngoại lệ quyền xoá theo pháp luật VN — cần đối chiếu điều khoản gốc |

---

**Bảng 3 · Checklist chốt khi nộp**

- [x] Link repo và tag cuối cùng trong README; input mẫu không chứa PII thật.
- [x] Đúng 1 lệnh run, 1 lệnh evaluate, 1 lệnh tests; không cần download khi chạy.
- [x] Có baseline/PoC (proxy), ≥1 failure case, reviewer evidence, decision log.
- [x] Có license/source provenance, seed, offline environment, N.
- [x] Mỗi thành viên có ≥1 commit thực chất và được nêu minh bạch trong bảng trên.
