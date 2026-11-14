# Metric contract (khai báo bắt buộc — chốt ở phút 30, trước khi chạy eval)

> Các metric chuẩn đã được cài trong `src/metrics/tracks.py`. File này là nơi nhóm **cam kết** cách dùng chúng.
> Không đổi testset, threshold hay trọng số sau khi đã thấy kết quả eval.

| Mục | Khai báo của nhóm |
|---|---|
| Track | T08 · Retention & Delete Request Simulator |
| Primary metric (tên đúng như evaluator in ra) | `Deletion Completeness` |
| Tăng tốt hay giảm tốt? | higher |
| Đơn vị đánh giá | cặp (request_id, artefact_id) |
| Công thức + numerator / denominator | số cặp GT `expected_action = delete` mà pred `action = delete` / tổng số cặp GT `expected_action = delete` |
| Cách ghép prediction ↔ GT | join chính xác theo (request_id, artefact_id); cặp GT không có trong pred = không xoá |
| Candidate pool cố định | 212 artefact trong `data_graph.json` (mock-graph-1) × 10 request trong `requests.json`; sha256 kiểm trong `manifest.csv` |
| Review budget B / K | B = 40 dòng/run (`params.budget`), ưu tiên escalate > legal hold > delete |
| Seed | 42 (pipeline deterministic, không dùng random) |
| Threshold policy (chọn trên dev như thế nào) | Không có threshold số; policy là thứ tự ưu tiên P1–P7 (`POLICY_VERSION = t08-policy-v1`), chốt dựa trên handout + đọc `data_graph.json`, không nhìn GT |
| Eval split (không dùng để tune) | toàn bộ 10 request (fixture không chia split); không có gt/ trong repo học viên |
| Chỉ số lỗi xấu | **False-delete count** (xoá artefact thuộc exception hoặc ngoài phạm vi) — mục tiêu 0, ưu tiên hơn completeness; phụ: Exception handling accuracy, Audit trace completeness |
| Slices sẽ báo cáo (≥2) | theo `expected_action` (delete / retain_legal_hold / escalate — evaluator in sẵn); theo `type` artefact (raw_video, backup, frame, face_crop, embedding, annotation, training_export) |
| Cách báo độ bất định khi N nhỏ | Wilson 95% CI cho Deletion Completeness; báo thêm từng request (`request_decisions.csv`) |
| Kiểm tra không cần nhãn (tự kiểm) | `selfcheck.json`: 0 delete dưới legal hold, 0 delete artefact nhiều subject chưa duyệt, 0 delete ngoài subject tag, 0 dòng trùng; idempotency (chạy 2 lần cùng actions.csv) |
| Ngày giờ chốt + người chốt | 2026-11-14 15:30 (UTC+7) · Nguyễn Hoài Nam |

Nếu baseline = 0 thì ghi rõ "baseline = 0" thay vì phần trăm cải thiện.
