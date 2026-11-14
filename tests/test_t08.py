"""Tests của nhóm cho T08 (success + failure/edge). Không cần gt/."""
import json
import shutil

import pandas as pd

from src import main as runner
from src.pipeline import plan_actions


def _run(out, *extra):
    return runner.main(["--config", "configs/lab.yaml", "--track", "t08", "--output", str(out),
                        "--offline", "--seed", "42", *extra])


def _graph(*arts):
    base = {"subject_ids": [], "parent_ids": [], "acl": ["data-platform"], "legal_hold": None, "origin_known": True,
            "owner": "data-platform", "uri": ""}
    return {"artefacts": [base | a for a in arts]}


# ---------------------------------------------------------------- success
def test_t08_end_to_end_and_idempotent(tmp_path):
    out = tmp_path / "poc"
    assert _run(out) == 0
    a1 = pd.read_csv(out / "actions.csv")
    assert {"request_id", "artefact_id", "action"} <= set(a1.columns)
    assert set(a1.action) <= {"delete", "retain_legal_hold", "escalate"}
    assert not a1.duplicated(["request_id", "artefact_id"]).any()
    # không bao giờ xoá artefact đang legal hold / thiếu quyền / không rõ nguồn / nhiều subject
    dels = a1[a1.action == "delete"]
    assert (dels.legal_hold.fillna("") == "").all()
    assert not dels.reason_code.isin(["NO_DELETE_PERMISSION", "UNKNOWN_ORIGIN", "MULTI_SUBJECT"]).any()
    # PoC truy được bản sao + dẫn xuất, không chỉ raw
    assert {"backup", "embedding", "face_crop", "frame"} <= set(dels.type)
    # mọi action đều có audit entry
    logged = {json.loads(l)["item_id"] for l in (out / "audit_log.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()}
    assert set(a1.artefact_id) <= logged
    assert json.loads((out / "selfcheck.json").read_text(encoding="utf-8"))["n_violations"] == 0
    # idempotency: chạy lần 2 cùng thư mục -> cùng kết quả
    assert _run(out) == 0
    a2 = pd.read_csv(out / "actions.csv")
    pd.testing.assert_frame_equal(a1, a2)


def test_t08_policy_precedence_unit():
    g = _graph(
        {"artefact_id": "r1", "type": "raw_video", "subject_ids": ["s1"], "legal_hold": "LH-1"},
        {"artefact_id": "f1", "type": "frame", "subject_ids": ["s1"], "parent_ids": ["r1"]},           # kế thừa hold
        {"artefact_id": "r2", "type": "raw_video", "subject_ids": ["s1"]},
        {"artefact_id": "b2", "type": "backup", "subject_ids": ["s1"], "parent_ids": ["r2"], "acl": ["vendor-x"]},
        {"artefact_id": "e2", "type": "embedding", "subject_ids": ["s1"], "parent_ids": ["r2"], "origin_known": False},
        {"artefact_id": "m2", "type": "frame", "subject_ids": ["s1", "s2"], "parent_ids": ["r2"]},
        {"artefact_id": "c2", "type": "face_crop", "subject_ids": ["s1"], "parent_ids": ["m2"]},
        {"artefact_id": "x9", "type": "raw_video", "subject_ids": ["s2"]},                              # ngoài phạm vi
    )
    df = plan_actions(g, [{"request_id": "R1", "subject_id": "s1", "identity_verified": True}]).set_index("artefact_id")
    assert "x9" not in df.index
    assert df.loc["r1", "action"] == df.loc["f1", "action"] == "retain_legal_hold"
    assert df.loc["b2", "reason_code"] == "NO_DELETE_PERMISSION"
    assert df.loc["e2", "reason_code"] == "UNKNOWN_ORIGIN"
    assert df.loc["m2", "reason_code"] == "MULTI_SUBJECT"
    assert df.loc["r2", "action"] == df.loc["c2", "action"] == "delete"
    # yêu cầu chưa xác minh danh tính -> không xoá gì
    un = plan_actions(g, [{"request_id": "R2", "subject_id": "s1", "identity_verified": False}])
    assert "delete" not in set(un.action)


# ---------------------------------------------------------------- failure / edge
def test_t08_review_without_reviewer_fails_loudly(tmp_path):
    out = tmp_path / "poc"
    assert _run(out) == 0
    q = pd.read_csv(out / "review_queue.csv", dtype=str).fillna("")
    q.loc[0, "reviewer_decision"] = "approve"          # có quyết định nhưng thiếu reviewer
    q.to_csv(out / "review_queue_reviewed.csv", index=False)
    assert _run(out) == 2


def test_t08_review_override_and_legal_hold_cannot_be_deleted(tmp_path):
    out = tmp_path / "poc"
    assert _run(out) == 0
    q = pd.read_csv(out / "review_queue.csv", dtype=str).fillna("")
    hold = q.index[q.reason == "LEGAL_HOLD"][0]
    esc = q.index[q.reason == "MULTI_SUBJECT"][0]
    q.loc[[hold, esc], "reviewer"] = "Nguyen Hoai Nam"
    q.loc[[hold, esc], "reviewer_decision"] = "edit"
    q.loc[[hold, esc], "override_value"] = "delete"
    q.to_csv(out / "review_queue_reviewed.csv", index=False)
    assert _run(out) == 0
    a = pd.read_csv(out / "actions.csv").set_index("item_id")
    assert a.loc[q.loc[hold, "item_id"], "action"] == "retain_legal_hold"   # bị chặn
    assert a.loc[q.loc[esc, "item_id"], "action"] == "delete"               # người duyệt cho phép
    log = (out / "audit_log.jsonl").read_text(encoding="utf-8")
    assert "override_blocked" in log and "review:edit" in log


def test_t08_tampered_input_is_rejected(tmp_path):
    src = tmp_path / "t08_deletion"
    shutil.copytree("fixtures/t08_deletion", src, ignore=shutil.ignore_patterns("gt"))
    with open(src / "requests.json", "ab") as f:
        f.write(b" ")
    rc = runner.main(["--config", "configs/lab.yaml", "--track", "t08", "--input", str(src),
                      "--output", str(tmp_path / "out"), "--offline"])
    assert rc == 2
