"""计算工具共用：读取快照字段、来源集合、步骤与指标构造、上游一致性检查。

计算模块只接收已确认快照与上游结果，不读取界面状态、模型或 expected_results。
"""
from __future__ import annotations

from decimal import Decimal

from domain.contracts import (
    TOOL_VERSIONS, Code, Issue, Metric, Step, ToolResponse, dec, display, dstr, merge_statuses, now_utc,
    sha256_of,
)

METRIC_META = {
    "facade_area_m2": ("m²", "area", "立面面积 A"),
    "panel_area_m2": ("m²", "area", "板块面积 a"),
    "nx": ("格", "count", "横向格数 nx"),
    "ny": ("格", "count", "竖向格数 ny"),
    "column_count": ("条", "count", "立柱轴线数 Nc"),
    "beam_rows": ("道", "count", "横梁道数 Rt"),
    "beam_segments": ("段", "count", "横梁理论分段数 Nt"),
    "column_length_m": ("m", "length", "立柱理论总长 Lc"),
    "beam_length_m": ("m", "length", "横梁理论总长 Lt"),
    "column_linear_mass": ("kg/m", "mass_per_area", "立柱线重 μc"),
    "beam_linear_mass": ("kg/m", "mass_per_area", "横梁线重 μt"),
    "column_mass_kg": ("kg", "mass", "立柱质量 Mc"),
    "beam_mass_kg": ("kg", "mass", "横梁质量 Mt"),
    "total_mass_kg": ("kg", "mass", "型材总质量 M"),
    "column_mass_per_m2": ("kg/m²", "mass_per_area", "立柱质量／面积"),
    "beam_mass_per_m2": ("kg/m²", "mass_per_area", "横梁质量／面积"),
    "total_mass_per_m2": ("kg/m²", "mass_per_area", "型材总质量／面积"),
    "column_unit_price": ("CNY/kg", "money", "立柱单价 pc"),
    "beam_unit_price": ("CNY/kg", "money", "横梁单价 pt"),
    "column_cost_cny": ("CNY", "money", "立柱材料金额 Cc"),
    "beam_cost_cny": ("CNY", "money", "横梁材料金额 Ct"),
    "total_cost_cny": ("CNY", "money", "型材材料总金额 C"),
    "cost_per_m2": ("CNY/m²", "money_per_area", "型材材料金额／面积"),
    "percent_total_cost": ("%", "percent", "总金额变化率"),
    "percent_total_mass": ("%", "percent", "总质量变化率"),
}


def metric_label(key: str) -> str:
    base = key[6:] if key.startswith("delta_") else key
    label = METRIC_META.get(base, ("", "", base))[2]
    return f"Δ {label}" if key.startswith("delta_") else label


def metric_kind(key: str) -> str:
    base = key[6:] if key.startswith("delta_") else key
    return METRIC_META.get(base, ("", "length", key))[1]


def metric_unit(key: str) -> str:
    base = key[6:] if key.startswith("delta_") else key
    return METRIC_META.get(base, ("", "", key))[0]


class Calc:
    """单个工具执行时的步骤与指标收集器。"""

    def __init__(self, tool_id: str, snapshot: dict, prefix: str | None = None):
        self.tool_id = tool_id
        self.version = TOOL_VERSIONS[tool_id]
        self.snapshot = snapshot
        self.prefix = prefix or snapshot.get("snapshot_id", "snap")
        self.steps: list[Step] = []
        self.metrics: dict[str, Metric] = {}
        self.issues: list[Issue] = []
        self.started = now_utc()

    # ---- 输入
    def statuses_for(self, provenance_ids) -> list[str]:
        prov = self.snapshot.get("provenance", {})
        return merge_statuses([prov.get(p, {}).get("source_status", "pending") for p in provenance_ids or []])

    def param(self, field: str, *, module: str, positive: bool = True, allow_zero: bool = False):
        """返回 (Decimal, ref, statuses) 或在缺失／非法时登记 Issue 并返回 None。"""
        fv = (self.snapshot.get("parameters") or {}).get(field)
        ref = f"snapshot:{self.snapshot.get('snapshot_id')}#parameters.{field}"
        if fv is None or fv.get("value") in (None, ""):
            self.issues.append(Issue(Code.FIELD_MISSING, f"缺少 {field}", field, module))
            return None
        if fv.get("confirmation_status") not in (None, "confirmed"):
            self.issues.append(Issue(Code.UNCONFIRMED_INPUT, f"{field} 未经本次使用确认", field, module))
            return None
        try:
            d = dec(fv["value"])
        except ValueError:
            self.issues.append(Issue(Code.VALUE_INVALID, f"{field} 数值非法", field, module))
            return None
        if positive and (d < 0 or (d == 0 and not allow_zero)):
            self.issues.append(Issue(Code.VALUE_INVALID, f"{field} 必须大于 0，当前 {fv['value']}", field, module))
            return None
        return d, ref, self.statuses_for(fv.get("provenance_ids"))

    # ---- 输出
    def step(self, key: str, formula_id: str, operator: str, operands: list[tuple[Decimal, str]],
             value: Decimal, expression: str) -> str:
        step_id = f"{self.prefix}:{self.tool_id}:{key}"
        self.steps.append(Step(step_id, formula_id, operator, [r for _, r in operands], expression,
                               dstr(value), metric_unit(key), display(value, metric_kind(key))))
        return step_id

    def metric(self, key: str, value: Decimal | None, formula_id: str, input_refs: list[str],
               statuses: list[str], step_refs: list[str], reason_code: str | None = None) -> Metric:
        m = Metric(key, dstr(value), metric_unit(key),
                   None if value is None else display(value, metric_kind(key)),
                   input_refs, statuses, formula_id, self.version, step_refs, reason_code)
        self.metrics[key] = m
        return m

    def response(self, requested: list[str], input_payload: dict) -> ToolResponse:
        done = [k for k in requested if k in self.metrics and self.metrics[k].value is not None]
        if len(done) == len(requested) and not any(i.blocking for i in self.issues):
            status = "ok"
        elif done:
            status = "partial"
        else:
            status = "blocked"
        data = {
            "snapshot_id": self.snapshot.get("snapshot_id"),
            "snapshot_hash": self.snapshot.get("content_hash"),
            "metrics": {k: m.to_dict() for k, m in self.metrics.items()},
            "steps": [s.to_dict() for s in self.steps],
            "issues": [i.to_dict() for i in self.issues],
        }
        data["result_hash"] = sha256_of(data)
        input_hash = sha256_of({"tool": self.tool_id, "version": self.version, **input_payload})
        return ToolResponse(self.tool_id, status, data, self.issues,
                            {"input_refs": sorted({r for m in self.metrics.values() for r in m.input_refs}),
                             "started_at": self.started, "ended_at": now_utc(),
                             "step_refs": [s.step_id for s in self.steps]},
                            input_hash=input_hash)


def check_upstream(snapshot: dict, upstream: dict | None, tool_name: str, module: str) -> Issue | None:
    """上游结果必须属于同一快照（哈希一致）且自身未被篡改。"""
    if upstream is None:
        return Issue(Code.UPSTREAM_NOT_DONE, f"缺少上游 {tool_name} 结果", None, module)
    if upstream.get("snapshot_hash") != snapshot.get("content_hash") or \
            upstream.get("snapshot_id") != snapshot.get("snapshot_id"):
        return Issue(Code.INPUT_HASH_MISMATCH, f"上游 {tool_name} 结果不属于当前快照，拒绝拼接", None, module)
    body = {k: v for k, v in upstream.items() if k != "result_hash"}
    if upstream.get("result_hash") != sha256_of(body):
        return Issue(Code.INPUT_HASH_MISMATCH, f"上游 {tool_name} 结果内容与其哈希不一致（疑似被修改）", None, module)
    return None


def upstream_metric(upstream: dict, key: str):
    m = (upstream.get("metrics") or {}).get(key)
    if not m or m.get("value") is None:
        return None
    ref = f"result:{upstream.get('result_hash', '')[:12]}#{key}"
    return dec(m["value"]), ref, m.get("source_statuses", [])
