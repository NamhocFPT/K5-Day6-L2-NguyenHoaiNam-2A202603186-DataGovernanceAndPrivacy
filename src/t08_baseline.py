"""T08 baseline (cố tình ngây thơ): xoá đúng 1 file raw theo filename chứa subject_id.

Không tìm frame/crop/embedding/backup/export, không kiểm legal hold/ACL -> dùng để chứng minh
"chỉ xoá raw" là không đủ (completeness thấp, có thể xoá nhầm dữ liệu đang legal hold).
Chạy: python -m src.main --track t08 --pipeline src.t08_baseline --output out/baseline --run-name baseline --offline
"""
from __future__ import annotations

import json

import pandas as pd

from .common.context import RunContext
from .common.decision import Decision, write_decision
from .pipeline import load


def run(ctx: RunContext) -> None:
    data = load(ctx)
    raws = [a for a in data["graph"]["artefacts"] if a["type"] == "raw_video"]
    rows = []
    for r in sorted(data["requests"], key=lambda x: x["request_id"]):
        for a in raws:
            if r["subject_id"] in (a.get("filename") or ""):
                rows.append({"request_id": r["request_id"], "artefact_id": a["artefact_id"], "action": "delete"})
                ctx.audit.log(actor="baseline", action="delete", item_id=a["artefact_id"], request_id=r["request_id"])
    df = pd.DataFrame(rows, columns=["request_id", "artefact_id", "action"])
    df.to_csv(ctx.out("actions.csv"), index=False)
    ctx.stats["n_items"] = len(df)
    write_decision(Decision("GO", "BASELINE_RAW_ONLY", "Baseline: xoá raw theo filename, không truy lineage",
                            ctx.config.get("owner", "TODO-owner"), ctx.track), ctx.out("decision.json"), ctx.out("decision_log.csv"))
