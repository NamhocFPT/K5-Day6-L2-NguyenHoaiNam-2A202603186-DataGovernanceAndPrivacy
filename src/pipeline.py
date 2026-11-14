"""T08 · Retention & Delete Request Simulator — PoC pipeline (owner: Nguyễn Hoài Nam).

5 bước (contract của lab):
    01 load     kiểm sha256 manifest, đọc data_graph.json + requests.json
    02 process  truy lineage (BFS raw -> frame -> crop -> embedding/annotation, backup, export)
                + áp policy theo thứ tự ưu tiên -> 1 action cho mỗi (request, artefact)
    03 rank     review queue top-B: exception (escalate / legal hold) lên trước
    04 review   đọc out/<run>/review_queue_reviewed.csv -> override có kiểm soát + audit log
    05 decide   GO / HOLD / REWORK (+ reason_code + owner); dry-run plan hoặc mock-delete receipts

Policy (precedence, cao -> thấp; freeze trước khi eval, version ở POLICY_VERSION):
    P1 LEGAL_HOLD            artefact hoặc tổ tiên đang legal hold     -> retain_legal_hold
    P2 IDENTITY_UNVERIFIED   yêu cầu chưa xác minh danh tính           -> escalate
    P3 NO_DELETE_PERMISSION  ACL không có operator_role                 -> escalate
    P4 UNKNOWN_ORIGIN        origin_known = false                       -> escalate
    P5 NOT_SUBJECT_TAGGED    là con cháu nhưng không gắn subject        -> escalate
    P6 MULTI_SUBJECT         artefact chứa dữ liệu của người khác       -> escalate
    P7 DELETE                còn lại                                    -> delete
Không đọc fixtures/*/gt/, không gọi network, mọi thứ deterministic (không dùng random).
"""
from __future__ import annotations

import json
from collections import deque
from pathlib import Path

import pandas as pd

from .common.context import RunContext
from .common.decision import Decision, write_decision
from .common.io import write_json
from .common.manifest import verify_manifest
from .common.review import load_review_decisions, make_review_queue, write_review_queue
from .metrics.tracks import get_track

POLICY_VERSION = "t08-policy-v1 (2026-11-14)"
ACTIONS = {"delete", "retain_legal_hold", "escalate"}
RULES = [  # (rule_id, reason_code, action, review priority score, legal/ops basis — cần DPO xác nhận)
    ("P1", "LEGAL_HOLD", "retain_legal_hold", 0.50, "Ngoại lệ quyền xoá khi có nghĩa vụ pháp lý lưu giữ (Luật 91/2025; GDPR Art.17(3)(b)(e))"),
    ("P2", "IDENTITY_UNVERIFIED", "escalate", 1.00, "Phải xác minh chủ thể trước khi xoá (chống yêu cầu giả mạo)"),
    ("P3", "NO_DELETE_PERMISSION", "escalate", 0.95, "Operator không có quyền ACL -> chủ sở hữu/vendor phải xử lý"),
    ("P4", "UNKNOWN_ORIGIN", "escalate", 0.90, "Không rõ nguồn gốc -> không chắc thuộc phạm vi, cần data owner xác nhận"),
    ("P5", "NOT_SUBJECT_TAGGED", "escalate", 0.60, "Con cháu lineage không gắn subject -> cần kiểm tra thủ công"),
    ("P6", "MULTI_SUBJECT", "escalate", 0.70, "Chứa dữ liệu người khác -> redact/tách thay vì xoá cả artefact"),
    ("P7", "DELETE", "delete", 0.10, "Quyền xoá của chủ thể (Luật 91/2025; GDPR Art.17(1))"),
]
RULE = {r[1]: r for r in RULES}


# ---------------------------------------------------------------- 01 load
def load(ctx: RunContext) -> dict:
    manifest = ctx.input_dir / "manifest.csv"
    problems = verify_manifest(manifest) if manifest.exists() else [{"problem": "no_manifest"}]
    if problems:
        ctx.audit.log(actor="pipeline", action="provenance_problems", item_id=str(manifest), after=problems[:20])
        if ctx.config.get("strict_provenance", True):
            raise ValueError(f"provenance check failed ({len(problems)} problems), first: {problems[:3]}")
    graph = json.loads((ctx.input_dir / "data_graph.json").read_text(encoding="utf-8"))
    requests = json.loads((ctx.input_dir / "requests.json").read_text(encoding="utf-8"))
    ctx.audit.log(actor="pipeline", action="load", item_id="input",
                  after={"graph_version": graph.get("version"), "n_artefacts": len(graph["artefacts"]),
                         "n_requests": len(requests), "policy": POLICY_VERSION})
    return {"graph": graph, "requests": requests}


# ---------------------------------------------------------------- 02 process (pure, testable)
def build_index(artefacts: list[dict]) -> tuple[dict, dict]:
    by_id = {a["artefact_id"]: a for a in artefacts}
    children: dict[str, list[str]] = {k: [] for k in by_id}
    for a in artefacts:
        for p in a.get("parent_ids") or []:
            children.setdefault(p, []).append(a["artefact_id"])
    for k in children:
        children[k].sort()
    return by_id, children


def lineage_scope(subject: str, by_id: dict, children: dict) -> dict[str, str]:
    """BFS: seeds = artefact gắn subject; mở rộng xuống mọi con cháu. Trả {artefact_id: via}."""
    seeds = sorted(k for k, a in by_id.items() if subject in (a.get("subject_ids") or []))
    scope = {s: "tagged" for s in seeds}
    q = deque(seeds)
    while q:
        cur = q.popleft()
        for c in children.get(cur, []):
            if c not in scope:
                scope[c] = f"descendant_of:{cur}"
                q.append(c)
    return scope


def held_by(aid: str, by_id: dict, _memo: dict) -> str | None:
    """legal_hold của chính artefact hoặc gần nhất trên các nhánh tổ tiên (hold áp cho cả bản dẫn xuất)."""
    if aid in _memo:
        return _memo[aid]
    _memo[aid] = None  # chặn vòng lặp nếu graph lỗi
    a = by_id.get(aid)
    if a is None:
        return None
    hold = a.get("legal_hold")
    if not hold:
        for p in sorted(a.get("parent_ids") or []):
            hold = held_by(p, by_id, _memo)
            if hold:
                break
    _memo[aid] = hold or None
    return _memo[aid]


def classify(a: dict, subject: str, request: dict, hold: str | None, operator_role: str) -> str:
    if hold:
        return "LEGAL_HOLD"
    if not request.get("identity_verified", False):
        return "IDENTITY_UNVERIFIED"
    if operator_role not in (a.get("acl") or []):
        return "NO_DELETE_PERMISSION"
    if not a.get("origin_known", True):
        return "UNKNOWN_ORIGIN"
    subs = a.get("subject_ids") or []
    if subject not in subs:
        return "NOT_SUBJECT_TAGGED"
    if len(subs) > 1:
        return "MULTI_SUBJECT"
    return "DELETE"


def plan_actions(graph: dict, requests: list[dict], operator_role: str = "data-platform") -> pd.DataFrame:
    by_id, children = build_index(graph["artefacts"])
    memo: dict = {}
    rows = []
    for r in sorted(requests, key=lambda x: x["request_id"]):
        scope = lineage_scope(r["subject_id"], by_id, children)
        for aid in sorted(scope):
            a = by_id[aid]
            hold = held_by(aid, by_id, memo)
            code = classify(a, r["subject_id"], r, hold, operator_role)
            rid, _, action, score, basis = RULE[code]
            rows.append({
                "request_id": r["request_id"], "artefact_id": aid, "action": action,
                "item_id": f"{r['request_id']}|{aid}", "subject_id": r["subject_id"], "type": a["type"],
                "subject_ids": ";".join(a.get("subject_ids") or []), "uri": a.get("uri", ""),
                "via": scope[aid], "legal_hold": hold or "", "rule_id": rid, "reason_code": code,
                "reason": basis, "score": score, "suggested_action": action, "deadline": r.get("deadline", ""),
            })
    return pd.DataFrame(rows)


def process(ctx: RunContext, data: dict) -> pd.DataFrame:
    df = plan_actions(data["graph"], data["requests"], str(ctx.param("operator_role", "data-platform")))
    for req, g in df.groupby("request_id", sort=True):
        ctx.audit.log(actor="pipeline", action="lineage_trace", item_id=req,
                      after={"n_in_scope": len(g), "by_type": g["type"].value_counts().to_dict()})
    return df


# ---------------------------------------------------------------- 03 rank
def rank(ctx: RunContext, scored: pd.DataFrame) -> pd.DataFrame:
    budget = int(ctx.param("budget", 40))
    base = scored.sort_values(["item_id"], kind="mergesort")
    q = make_review_queue(base, id_col="item_id", score_col="score", budget=budget,
                          reason_col="reason_code", action_col="suggested_action", slice_col="type", evidence_col="uri")
    write_review_queue(q, ctx.out("review_queue.csv"))
    ctx.stats["review_budget"] = budget
    ctx.stats["n_exceptions"] = int((scored.action != "delete").sum())
    return q


# ---------------------------------------------------------------- 04 review
def review(ctx: RunContext, scored: pd.DataFrame, queue: pd.DataFrame) -> pd.DataFrame:
    final = scored.copy()
    final["reviewed_by"] = ""
    final["review_decision"] = ""
    reviewed_path = ctx.output_dir / "review_queue_reviewed.csv"
    if not reviewed_path.exists():
        ctx.stats["n_reviewed"] = 0
        return final
    reviewed = load_review_decisions(reviewed_path)   # thiếu reviewer -> ValueError -> exit 2
    ctx.stats["n_reviewed"] = len(reviewed)
    pos = {k: i for i, k in enumerate(final["item_id"])}
    for r in reviewed.itertuples(index=False):
        if r.item_id not in pos:
            raise ValueError(f"review row {r.item_id!r} không có trong kế hoạch xoá")
        i = final.index[pos[r.item_id]]
        before = final.at[i, "action"]
        after = before
        if r.reviewer_decision in {"edit", "reject"} and str(r.override_value).strip():
            after = str(r.override_value).strip().lower()
            if after not in ACTIONS:
                raise ValueError(f"override_value {after!r} không hợp lệ cho {r.item_id}; dùng {sorted(ACTIONS)}")
            if final.at[i, "reason_code"] == "LEGAL_HOLD" and after == "delete":
                ctx.audit.log(actor=r.reviewer, action="override_blocked", item_id=final.at[i, "artefact_id"],
                              before=before, after=after, reason="legal hold không thể bị override thành delete",
                              request_id=final.at[i, "request_id"])
                after = before
        final.at[i, "action"] = after
        final.at[i, "reviewed_by"] = r.reviewer
        final.at[i, "review_decision"] = r.reviewer_decision
        ctx.audit.log(actor=r.reviewer, action=f"review:{r.reviewer_decision}", item_id=final.at[i, "artefact_id"],
                      before=before, after=after, reason=r.review_note, request_id=final.at[i, "request_id"])
    return final


# ---------------------------------------------------------------- 05 decide
def self_check(final: pd.DataFrame) -> dict:
    """Kiểm tra không cần nhãn: invariant của policy."""
    dels = final[final.action == "delete"]
    viol = {
        "delete_under_legal_hold": int((dels.legal_hold != "").sum()),
        "delete_without_subject_tag": int((~dels.apply(lambda r: r.subject_id in r.subject_ids.split(";"), axis=1)).sum()) if len(dels) else 0,
        "delete_multi_subject_unreviewed": int(((dels.subject_ids.str.count(";") > 0) & (dels.reviewed_by == "")).sum()),
        "duplicate_rows": int(final.duplicated(["request_id", "artefact_id"]).sum()),
    }
    return {"n_rows": len(final), "actions": final.action.value_counts().to_dict(),
            "by_reason": final.reason_code.value_counts().to_dict(), "violations": viol,
            "n_violations": int(sum(viol.values()))}


def decide(ctx: RunContext, final: pd.DataFrame, check: dict) -> tuple[Decision, pd.DataFrame]:
    owner = ctx.config.get("owner", "TODO-owner")
    per_req = []
    for req, g in final.groupby("request_id", sort=True):
        pending = int(((g.action == "escalate") & (g.reviewed_by == "")).sum())
        retained = int((g.action == "retain_legal_hold").sum())
        status = "HOLD" if pending else "GO"
        n_esc = int((g.action == "escalate").sum())
        code = ("ESCALATIONS_PENDING_REVIEW" if pending else "GO_PARTIAL_LEGAL_HOLD" if retained
                else "GO_EXCEPTIONS_REVIEWED" if n_esc else "GO_ALL_DELETED")
        per_req.append({"request_id": req, "subject_id": g.subject_id.iloc[0], "deadline": g.deadline.iloc[0],
                        "status": status, "reason_code": code, "n_delete": int((g.action == "delete").sum()),
                        "n_retain_legal_hold": retained, "n_escalate": int((g.action == "escalate").sum()),
                        "n_escalate_pending": pending, "owner": owner})
    per_req = pd.DataFrame(per_req)
    if check["n_violations"]:
        d = Decision("REWORK", "POLICY_INVARIANT_VIOLATION", f"{check['n_violations']} vi phạm invariant: {check['violations']}", owner, ctx.track)
    elif (per_req.status == "HOLD").any():
        n = int((per_req.status == "HOLD").sum())
        d = Decision("HOLD", "ESCALATIONS_PENDING_REVIEW",
                     f"{n}/{len(per_req)} request còn escalation chưa có người duyệt; chỉ thực thi xoá cho request GO",
                     owner, ctx.track)
    else:
        d = Decision("GO", "DELETION_PLAN_APPROVED", "Mọi exception đã được người duyệt; legal hold được giữ và hẹn xoá khi hết hold",
                     owner, ctx.track)
    d.evidence = [str(ctx.out(f)) for f in ("actions.csv", "audit_log.jsonl", "review_queue.csv", "request_decisions.csv", "selfcheck.json")]
    d.metrics = {"actions": check["actions"], "n_violations": check["n_violations"], "policy": POLICY_VERSION}
    return d, per_req


def execute(ctx: RunContext, final: pd.DataFrame, per_req: pd.DataFrame) -> pd.DataFrame:
    """Dry-run (mặc định) chỉ ghi kế hoạch; không dry-run thì mock-delete cho request GO + ghi receipt."""
    dry = bool(ctx.param("dry_run", True))
    go = set(per_req.loc[per_req.status == "GO", "request_id"])
    final = final.copy()
    final["executed"] = (~pd.Series([dry] * len(final), index=final.index)) & (final.action == "delete") & final.request_id.isin(go)
    final[final.action == "delete"][["request_id", "artefact_id", "type", "uri", "executed"]].to_csv(ctx.out("deletion_plan.csv"), index=False)
    if not dry:
        rec = final[final.executed][["request_id", "artefact_id", "uri"]].assign(tombstone=lambda d: "mock-deleted://" + d.artefact_id)
        rec.to_csv(ctx.out("deletion_receipts.csv"), index=False)
    for r in final.itertuples(index=False):
        ctx.audit.log(actor="pipeline", action=f"{'dry_run' if dry else 'execute'}:{r.action}", item_id=r.artefact_id,
                      after={"executed": bool(r.executed), "rule": r.rule_id}, reason=r.reason_code,
                      request_id=r.request_id, legal_hold=r.legal_hold)
    ctx.stats["dry_run"] = dry
    return final


def run(ctx: RunContext) -> None:
    spec = get_track(ctx.track)
    if spec.track != "t08":
        raise NotImplementedError(f"pipeline này chỉ cài cho t08; {spec.track} ({spec.title}) chưa được cài")
    data = load(ctx)
    scored = process(ctx, data)
    ctx.stats["n_items"] = len(scored)
    queue = rank(ctx, scored)
    final = review(ctx, scored, queue)
    check = self_check(final)
    write_json(ctx.out("selfcheck.json"), check)
    decision, per_req = decide(ctx, final, check)
    per_req.to_csv(ctx.out("request_decisions.csv"), index=False)
    final = execute(ctx, final, per_req)
    cols = spec.pred_columns + [c for c in final.columns if c not in spec.pred_columns]
    final[cols].to_csv(ctx.out(spec.pred_file), index=False)
    write_decision(decision, ctx.out("decision.json"), ctx.out("decision_log.csv"))
