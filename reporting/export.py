"""TL16 export_run：只导出指定 run 的冻结数据——Markdown 报告、CSV 对比表、JSON 快照与带哈希的清单。"""
from __future__ import annotations

import csv
import io
import json
import zipfile
from pathlib import Path

from domain.contracts import Code, Issue, ToolResponse, new_id, now_utc, sha256_of


def build_artifacts(run: dict) -> dict[str, bytes]:
    rep = run["report"]
    files: dict[str, bytes] = {}
    files["report.md"] = rep["markdown"].encode("utf-8")
    buf = io.StringIO()
    rows = rep["structured_tables"]["comparison"]
    w = csv.writer(buf)
    w.writerow(["metric_key", "指标", "单位", "原方案", "调整方案", "差值"])
    for r in rows:
        w.writerow([r["metric_key"], r["指标"], r["单位"], r.get("原方案") or "", r.get("调整方案") or "", r.get("差值") or ""])
    w.writerow([])
    w.writerow(["# 数据来源", rep["banner"]])
    w.writerow(["# 运行", run["run_id"], run["dataset_version_id"], run["input_hash"]])
    files["comparison.csv"] = ("﻿" + buf.getvalue()).encode("utf-8")
    snapshot = {k: run.get(k) for k in ("run_id", "request_id", "input_hash", "requested_task", "target_modules",
                                         "confirmed_modules", "case_id", "project_id", "project_name", "dataset_version_id", "snapshots",
                                         "query_context", "tool_versions", "rounding_policy", "measurement_model",
                                         "module_statuses", "module_issues", "results", "comparison", "checklist",
                                         "rule_results", "coverage", "retrieval", "execution_state", "review_items",
                                         "started_at", "ended_at", "model_mode", "confirmation")}
    files["run_snapshot.json"] = json.dumps(snapshot, ensure_ascii=False, indent=1).encode("utf-8")
    return files


def export_run(run: dict, out_root: Path, request_id: str, allow_partial: bool) -> ToolResponse:
    started = now_utc()
    if run.get("report") is None:
        return ToolResponse("TL16", "error", None, [Issue(Code.TOOL_FAILED, "运行没有报告，无法导出")],
                            {"started_at": started})
    partial = run["execution_state"] != "COMPLETE"
    if partial and not allow_partial:
        return ToolResponse("TL16", "blocked", None, [Issue(
            Code.PARTIAL_EXPORT_NOT_CONFIRMED, "该运行为部分结果；请勾选“包含未完成事项”后再导出部分报告", None, "export",
            required_action="确认导出部分结果")], {"started_at": started})
    export_id = new_id("EXP")
    files = build_artifacts(run)
    manifest = {
        "export_id": export_id, "run_id": run["run_id"], "created_at": now_utc(),
        "completeness": "partial" if partial else "complete",
        "title": "部分结果" if partial else "完整结果",
        "dataset_version_id": run["dataset_version_id"], "input_hash": run["input_hash"],
        "source_banner": run["report"]["banner"],
        "files": [{"path": k, "sha256": sha256_of(v), "bytes": len(v)} for k, v in files.items()],
        "note": "导出内容只取自该 run 的冻结数据，不读取当前可变草稿；演示数据为 synthetic。",
    }
    files["manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8")
    out_dir = Path(out_root) / export_id
    out_dir.mkdir(parents=True, exist_ok=False)
    for name, data in files.items():
        (out_dir / name).write_bytes(data)
    zpath = out_dir.with_suffix(".zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    data = {"export_id": export_id, "run_id": run["run_id"], "request_id": request_id, "created_at": manifest["created_at"],
            "completeness": manifest["completeness"], "artifact_manifest": manifest, "dir": str(out_dir),
            "zip": str(zpath)}
    return ToolResponse("TL16", "ok", data, [], {"started_at": started, "ended_at": now_utc()})
