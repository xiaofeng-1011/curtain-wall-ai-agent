"""TL14 evaluate_rules：受控规则引擎，输出九种单项核查状态之一。

判定顺序：清单项存在性 → 证据来源资格 → 已知适用范围 → 适用性所需字段 → 效力确认
→ 可机判性 → 数值输入与依赖 → 条件比较。前置条件未知时不跳到真假比较。
只执行白名单操作符，不执行数据文件中的任何代码。
"""
from __future__ import annotations

from domain.contracts import (
    BLOCKING_RULE_STATUSES, DETERMINATE_RULE_STATUSES, RULE_STATUS_TEXT, Code, Issue, RuleStatus, ToolResponse,
    dec, now_utc, sha256_of,
)
from domain.fields import PREDICATE_METRICS, PREDICATE_OPERATORS, PREDICATE_PARAMS

OPS = {
    "<=": lambda a, b: a <= b, "<": lambda a, b: a < b, ">=": lambda a, b: a >= b,
    ">": lambda a, b: a > b, "==": lambda a, b: a == b,
}
APPROVED = {"technically_reviewed"}


def _qualified_formal(rule: dict, prov: dict | None) -> tuple[bool, str]:
    """正式／企业依据是否具备机判资格。返回 (是否合格, 不合格原因)。"""
    if not prov:
        return False, "来源记录缺失"
    if prov.get("source_status") not in ("public", "verified"):
        return False, f"来源状态为 {prov.get('source_status')}，不能作正式判断"
    if rule.get("evidence_type") == "formal_clause":
        for f in ("standard_name", "standard_number", "clause_number"):
            if not rule.get(f):
                return False, f"缺少 {f}"
    if not rule.get("exact_text") or not rule.get("source_ref"):
        return False, "缺少原文或出处"
    return True, ""


def _applicable(rule: dict, snapshot: dict) -> tuple[bool | None, str]:
    app = rule.get("applicability") or {}
    cwt, stage = snapshot.get("curtain_wall_type"), snapshot.get("design_stage")
    if app.get("curtain_wall_type") and cwt not in app["curtain_wall_type"]:
        return False, f"幕墙类型 {cwt} 不在适用范围 {app['curtain_wall_type']}"
    if app.get("design_stage") and stage not in app["design_stage"]:
        return False, f"设计阶段 {stage} 不在适用范围 {app['design_stage']}"
    params = snapshot.get("parameters") or {}
    if app.get("region"):
        region = (params.get("region") or {}).get("value")
        if region is None:
            return None, "适用性依赖地区，但地区未提供"
        if region not in app["region"]:
            return False, f"地区 {region} 不在适用范围"
    if app.get("check_date_required") and not (params.get("check_date") or {}).get("value"):
        return None, "适用性依赖规范核查日期，但未提供"
    return True, ""


def _lhs_value(lhs: str, snapshot: dict, geometry: dict | None):
    """返回 (Decimal | None, 实际值引用, 缺失原因)。"""
    if lhs in PREDICATE_PARAMS:
        fv = (snapshot.get("parameters") or {}).get(lhs) or {}
        ref = f"snapshot:{snapshot.get('snapshot_id')}#parameters.{lhs}"
        if fv.get("issues"):
            return None, ref, f"{lhs} 输入非法：{fv['issues'][0].get('message')}"
        if fv.get("value") is None:
            return None, ref, f"{lhs} 缺失"
        v = dec(fv["value"])
        if v <= 0:
            return None, ref, f"{lhs} 非法（必须大于 0）"
        return v, ref, None
    if lhs in PREDICATE_METRICS:
        m = ((geometry or {}).get("metrics") or {}).get(lhs) or {}
        ref = f"result:{(geometry or {}).get('result_hash', '')[:12]}#{lhs}"
        if m.get("value") is None:
            return None, ref, f"{lhs} 未由几何工具算出（依赖参数缺失或非法）"
        return dec(m["value"]), ref, None
    return None, "", f"不受支持的左值 {lhs}"


def evaluate_item(item: dict, snapshot: dict, geometry: dict | None, provenance: dict) -> dict:
    rule = item.get("rule")
    base = {"item_id": item["item_id"], "rule_id": item["rule_id"], "snapshot_id": snapshot.get("snapshot_id"),
            "rule_version": (rule or {}).get("rule_version"), "evidence_type": (rule or {}).get("evidence_type"),
            "title": (rule or {}).get("title"), "execution_status": "done", "actual_values": {},
            "condition": None, "input_refs": [], "review_items": [], "reason_code": None,
            "evidence_ref": f"rule:{item['rule_id']}@{(rule or {}).get('rule_version')}" if rule else None}

    def out(status: RuleStatus, reason: str, code: str | None = None, review: list | None = None):
        base.update(status=status.value, status_text=RULE_STATUS_TEXT[status], reason=reason, reason_code=code)
        if review:
            base["review_items"] = review
        return base

    try:
        # 1 清单项存在性
        if rule is None:
            return out(RuleStatus.NOT_COVERED, "清单引用的规则不在知识库中，当前知识库无可靠依据，需人工核实",
                       Code.EVIDENCE_NOT_FOUND)
        is_demo = rule.get("evidence_type") == "demo_rule"
        prov = provenance.get(rule.get("provenance_id"))
        # 2 证据来源资格
        if is_demo:
            if not prov or prov.get("source_status") != "synthetic" or rule.get("standard_number"):
                return out(RuleStatus.NEED_VERIFICATION, "演示规则来源身份异常", Code.EVIDENCE_UNQUALIFIED)
        else:
            ok, why = _qualified_formal(rule, prov)
            if not ok:
                return out(RuleStatus.NEED_VERIFICATION, f"依据资格不足：{why}", Code.EVIDENCE_UNQUALIFIED,
                           ["由工程师核实依据来源与审核状态"])
        # 3/4 适用范围及其所需字段
        app, why = _applicable(rule, snapshot)
        if app is False:
            return out(RuleStatus.NOT_APPLICABLE, why)
        if app is None:
            return out(RuleStatus.NEED_PARAMETERS, why, Code.FIELD_MISSING)
        # 5 效力确认（演示规则 not_applicable 效力字段）
        if not is_demo:
            if rule.get("effect_status") != "current":
                msg = {"repealed": "该条款已废止，只能作为历史资料，不参与当前判断",
                       "replaced": "该条款已被替代，替代条款未在本库确认，存在依据缺口",
                       "partially_replaced": "该条款部分被替代，需人工确认现行部分",
                       "unknown": "条款效力未知"}.get(rule.get("effect_status"), "效力不明确")
                if rule.get("replaced_by"):
                    msg += f"；替代引用：{', '.join(rule['replaced_by'])}（未入库则需补充）"
                return out(RuleStatus.NEED_VERIFICATION, msg, Code.EVIDENCE_UNQUALIFIED, ["由工程师核实条款效力"])
            if (rule.get("effect_review") or {}).get("review_status") not in APPROVED or \
                    (rule.get("mapping_review") or {}).get("review_status") not in APPROVED:
                return out(RuleStatus.NEED_VERIFICATION, "效力审核或规则映射审核未完成", Code.EVIDENCE_UNQUALIFIED,
                           ["完成效力与规则映射审核"])
        # 6 可机判性
        if not rule.get("machine_checkable"):
            return out(RuleStatus.NEED_ENGINEER, "该条款需要专业判断或未实现的计算，不由软件判定",
                       review=["由工程师判断"])
        pred = rule.get("predicate") or {}
        if pred.get("operator") not in PREDICATE_OPERATORS or \
                pred.get("lhs") not in PREDICATE_PARAMS | set(PREDICATE_METRICS):
            return out(RuleStatus.NEED_VERIFICATION, "规则条件不在受控白名单内", Code.EVIDENCE_UNQUALIFIED)
        # 7 数值输入与依赖
        missing = []
        for p in rule.get("required_parameters") or []:
            if p in PREDICATE_PARAMS:
                v, ref, why = _lhs_value(p, snapshot, geometry)
                if why:
                    missing.append(why)
                else:
                    base["input_refs"].append(ref)
        lhs_val, lhs_ref, why = _lhs_value(pred["lhs"], snapshot, geometry)
        if why and why not in missing:
            missing.append(why)
        if missing:
            return out(RuleStatus.NEED_PARAMETERS, "；".join(missing), Code.FIELD_MISSING)
        th = dec(str(pred["threshold"]["value"]))
        unit = pred["threshold"].get("unit", "")
        base["input_refs"].append(lhs_ref)
        base["actual_values"] = {pred["lhs"]: {"value": str(lhs_val), "unit": unit}}
        base["condition"] = f"{pred['lhs']} {pred['operator']} {pred['threshold']['value']} {unit}"
        # 8 条件比较
        truth = OPS[pred["operator"]](lhs_val, th)
        if is_demo:
            if truth:
                return out(RuleStatus.DEMO_MET, f"实际值 {lhs_val} {unit}，演示条件成立（仅表示演示条件成立）")
            return out(RuleStatus.DEMO_ATTENTION,
                       f"实际值 {lhs_val} {unit}，{rule.get('attention_text') or '演示关注项触发'}（不是规范违反）",
                       review=[rule.get("attention_text") or "需工程师确认"])
        if truth:
            return out(RuleStatus.CONDITION_MET, f"实际值 {lhs_val} {unit}，仅表示该条款可机判条件满足")
        return out(RuleStatus.CONDITION_NOT_MET,
                   f"实际值 {lhs_val} {unit}，仅表示该条款可机判条件未满足，不推导整个工程不合规",
                   review=["由工程师确认处理方式"])
    except Exception as exc:  # noqa: BLE001 - 执行失败不能伪装成业务状态
        base.update(status=None, status_text="核查执行失败", execution_status="error",
                    reason=f"{type(exc).__name__}: {exc}", reason_code=Code.TOOL_FAILED)
        return base


def coverage(results: list[dict], checklist: dict) -> dict:
    cnt = {s.value: 0 for s in RuleStatus}
    errors = 0
    for r in results:
        if r.get("execution_status") == "error":
            errors += 1
        else:
            cnt[r["status"]] += 1
    det = sum(cnt[s.value] for s in DETERMINATE_RULE_STATUSES)
    return {
        "rule_set_ref": f"{checklist['rule_set_id']}@{checklist['rule_set_version']}",
        "required_count": checklist["required_count"], "result_count": len(results),
        "determinate_count": det, "status_counts": cnt, "execution_errors": errors,
        "demo_rule_count": sum(1 for it in checklist["items"] if (it.get("rule") or {}).get("evidence_type") == "demo_rule"),
        "formal_clause_count": checklist.get("formal_clause_count", 0),
        "formal_assessment": "not_covered" if checklist.get("formal_clause_count", 0) == 0 else "partial",
    }


def evaluate_rules(checklist: dict, snapshot: dict, geometry: dict | None) -> ToolResponse:
    started = now_utc()
    prov = dict(checklist.get("provenance") or {})
    results = [evaluate_item(it, snapshot, geometry, prov) for it in checklist["items"]]
    cov = coverage(results, checklist)
    issues = []
    if cov["result_count"] != cov["required_count"]:
        issues.append(Issue(Code.TOOL_FAILED, "核查结果数量与必查清单不一致", None, "rule_check"))
    if any(r.get("execution_status") == "error" for r in results):
        status = "error" if all(r.get("execution_status") == "error" for r in results) else "partial"
    elif any(r["status"] in {s.value for s in BLOCKING_RULE_STATUSES} for r in results):
        status = "partial"
    else:
        status = "ok"
    for r in results:
        if r.get("status") in {s.value for s in BLOCKING_RULE_STATUSES}:
            issues.append(Issue(r.get("reason_code") or Code.FIELD_MISSING, f"{r['rule_id']}：{r['reason']}",
                                None, "rule_check", blocking=True))
    data = {"rule_results": results, "coverage": cov, "checklist_hash": checklist.get("checklist_hash"),
            "snapshot_id": snapshot.get("snapshot_id"), "snapshot_hash": snapshot.get("content_hash"),
            "review_items": sorted({x for r in results for x in r.get("review_items", [])})}
    data["result_hash"] = sha256_of(data)
    return ToolResponse("TL14", status, data, issues, {"started_at": started, "ended_at": now_utc()},
                        input_hash=sha256_of({"cl": checklist.get("checklist_hash"),
                                              "snap": snapshot.get("content_hash"),
                                              "geo": (geometry or {}).get("result_hash")}))
