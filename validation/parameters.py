"""参数草稿（FieldValue）、材料解析、运行门槛与可运行模块计划（TL04 核心）。

- null 是缺失，不等于零；显式清空记为 binding_state=cleared，不回填基准值。
- 本模块只做确定性校验，不计算金额。
"""
from __future__ import annotations

import copy

from domain.contracts import (
    MEASUREMENT_MODEL, Code, GenerationMethod, Issue, dec, merge_statuses,
)
from domain.fields import CURTAIN_WALL_TYPES, DESIGN_STAGES, FIELD_DEFS, GEOMETRY_FIELDS, NUMERIC_KINDS
from domain.units import parse_quantity

TASKS = {
    "query": {"label": "规范查询", "modules": ["query"]},
    "rule_check": {"label": "辅助核查", "modules": ["geometry", "rule_check"]},
    "length": {"label": "理论长度", "modules": ["geometry", "length", "rule_check"]},
    "mass": {"label": "型材质量", "modules": ["geometry", "length", "mass", "rule_check"]},
    "cost_compare": {"label": "成本比选", "modules": ["geometry", "length", "mass", "cost", "comparison", "rule_check"]},
}
MODULE_ORDER = ["query", "geometry", "length", "mass", "cost", "comparison", "rule_check", "report"]
MODULE_FIELDS = {
    "geometry": GEOMETRY_FIELDS,
    "length": GEOMETRY_FIELDS + ["measurement_model_id"],
    "mass": ["column_profile_id", "beam_profile_id", "column_mass_override", "beam_mass_override"],
    "cost": ["price_basis_id", "column_price_override", "beam_price_override"],
}
MODULE_DEPS = {"length": ["geometry"], "mass": ["length"], "cost": ["mass"], "comparison": ["cost"]}


# ------------------------------------------------------------------ FieldValue
def field_value(value=None, unit=None, *, provenance_ids=None, binding_state="missing", raw_value=None,
                raw_unit=None, inherited_from=None, generation_method=GenerationMethod.PROVIDED.value,
                confirmation_status="unconfirmed") -> dict:
    return {
        "value": value, "unit": unit, "raw_value": raw_value, "raw_unit": raw_unit,
        "provenance_ids": list(provenance_ids or []), "binding_state": binding_state,
        "confirmation_status": confirmation_status, "inherited_from": inherited_from,
        "generation_method": generation_method, "issues": [],
    }


def params_from_seed(resolved_seed: dict, baseline_ref: str | None, *, inherit_mode: bool) -> dict:
    """由种子参数生成参数字典。inherit_mode=True 时标为继承（用于候选草稿）。"""
    out = {}
    for f, fdef in FIELD_DEFS.items():
        sv = resolved_seed.get(f) or {}
        v = sv.get("value")
        if v is None:
            state = "missing"
        elif inherit_mode:
            state = "inherited"
        else:
            state = "modified" if sv.get("origin_seed") else "missing"
        out[f] = field_value(v, sv.get("unit") or fdef["unit"], provenance_ids=sv.get("provenance_ids"),
                             binding_state=state, raw_value=v, raw_unit=sv.get("unit"),
                             inherited_from=(f"{baseline_ref}#parameters.{f}" if inherit_mode and v is not None
                                             else None))
    return out


def inherit_from_snapshot(snapshot: dict) -> dict:
    out = {}
    for f, fdef in FIELD_DEFS.items():
        sv = (snapshot.get("parameters") or {}).get(f) or {}
        v = sv.get("value")
        out[f] = field_value(v, sv.get("unit") or fdef["unit"], provenance_ids=sv.get("provenance_ids"),
                             binding_state="inherited" if v is not None else "missing",
                             raw_value=sv.get("raw_value", v), raw_unit=sv.get("raw_unit", sv.get("unit")),
                             inherited_from=(f"snapshot:{snapshot['snapshot_id']}#parameters.{f}"
                                             if v is not None else None),
                             generation_method=sv.get("generation_method", GenerationMethod.PROVIDED.value))
    return out


def apply_edit(params: dict, field: str, operation: str, raw_value=None, raw_unit=None, *,
               provenance_ids=None, generation_method=GenerationMethod.PROVIDED.value) -> dict:
    """set / clear 一个字段，返回新的参数字典（不修改原对象）。数值统一解析归一。"""
    if field not in FIELD_DEFS:
        raise KeyError(f"未知字段 {field}")
    fdef = FIELD_DEFS[field]
    new = copy.deepcopy(params)
    old = new.get(field) or field_value(unit=fdef["unit"])
    if operation == "clear":
        new[field] = field_value(None, fdef["unit"], provenance_ids=provenance_ids or old["provenance_ids"],
                                 binding_state="cleared", generation_method=generation_method)
        return new
    fv = field_value(None, fdef["unit"], provenance_ids=provenance_ids or old["provenance_ids"],
                     binding_state="modified", raw_value=raw_value, raw_unit=raw_unit,
                     generation_method=generation_method)
    if fdef["kind"] in NUMERIC_KINDS:
        val, unit, issues = parse_quantity(raw_value, raw_unit, fdef["kind"], field)
        fv["value"] = None if val is None else str(val)
        fv["unit"] = unit or fdef["unit"]
        fv["issues"] = [i.to_dict() for i in issues]
    else:
        fv["value"] = None if raw_value in (None, "") else str(raw_value).strip()
    new[field] = fv
    return new


# ------------------------------------------------------------------ 材料解析
def resolve_materials(package: dict, params: dict) -> tuple[dict, dict, list[Issue]]:
    """按方案绑定的价格基准 + 各自 profile_id 解析线重与报价；不猜规格、不以 0 补价。"""
    issues: list[Issue] = []
    profiles, prices = {}, {}
    basis_id = (params.get("price_basis_id") or {}).get("value")
    basis = package["price_bases"].get(basis_id) if basis_id else None
    if basis_id and basis is None:
        issues.append(Issue(Code.PRICE_MISSING, f"价格基准 {basis_id} 不存在", "price_basis_id", "cost"))
    for comp, label in (("column", "立柱"), ("beam", "横梁")):
        pid = (params.get(f"{comp}_profile_id") or {}).get("value")
        rec = package["profiles"].get(pid) if pid else None
        if pid and rec is None:
            issues.append(Issue(Code.PROFILE_MISSING, f"{label}规格 {pid} 不存在", f"{comp}_profile_id", "mass"))
        elif rec is not None and rec.get("component_type") != comp:
            issues.append(Issue(Code.PROFILE_TYPE_MISMATCH, f"{label}规格 {pid} 是 {rec.get('component_type')} 型材",
                                f"{comp}_profile_id", "mass"))
            rec = None
        if rec is not None:
            ov = params.get(f"{comp}_mass_override") or {}
            if ov.get("value") is not None:
                lm, src, pids = ov["value"], "override", ov.get("provenance_ids") or []
            else:
                lm, src, pids = rec.get("linear_mass"), "profile", [rec.get("provenance_id")]
            profiles[comp] = {"profile_id": pid, "profile_name": rec.get("profile_name"),
                              "component_type": rec.get("component_type"), "linear_mass": lm, "unit": "kg/m",
                              "linear_mass_source": src, "catalog_linear_mass": rec.get("linear_mass"),
                              "provenance_ids": pids, "applicability_notes": rec.get("applicability_notes")}
        if basis is not None and pid:
            pr = package["prices"].get(f"{basis_id}::{pid}")
            ov = params.get(f"{comp}_price_override") or {}
            entry = {"price_basis_id": basis_id, "price_basis_version": basis.get("price_basis_version"),
                     "profile_id": pid, "currency": basis.get("currency"), "price_unit": basis.get("price_unit"),
                     "tax_basis": basis.get("tax_basis"), "cost_scope": basis.get("cost_scope"),
                     "catalog_unit_price": pr.get("unit_price") if pr else None,
                     "override_unit_price": ov.get("value"), "price_source": None}
            if ov.get("value") is not None:
                entry.update(unit_price=ov["value"], price_source="override",
                             provenance_ids=ov.get("provenance_ids") or [])
            elif pr is not None and pr.get("unit_price") is not None:
                entry.update(unit_price=pr["unit_price"], price_source="catalog",
                             provenance_ids=[pr.get("provenance_id")])
            else:
                entry.update(unit_price=None, provenance_ids=[pr.get("provenance_id")] if pr else [])
            prices[comp] = entry
    return profiles, prices, issues


# ------------------------------------------------------------------ 范围检查
def scope_check(package: dict, case_id: str) -> list[Issue]:
    case = package["cases"][case_id]
    cwt = (case.get("curtain_wall_type") or {}).get("value")
    stage = (case.get("design_stage") or {}).get("value")
    if cwt not in CURTAIN_WALL_TYPES or stage not in DESIGN_STAGES:
        return [Issue(Code.SCOPE_UNSUPPORTED, f"案例类型 {cwt}/{stage} 超出当前覆盖范围（仅构件式玻璃幕墙方案阶段）",
                      "curtain_wall_type", None)]
    return []


# ------------------------------------------------------------------ 字段校验
def field_issues(package: dict, params: dict, scheme_label: str) -> dict[str, list[Issue]]:
    """逐字段返回问题（缺失、非法、单位、整除、引用）。"""
    out: dict[str, list[Issue]] = {}

    def add(f, issue):
        issue.message = f"{scheme_label}：{issue.message}" if scheme_label else issue.message
        out.setdefault(f, []).append(issue)

    for f, fdef in FIELD_DEFS.items():
        fv = params.get(f) or {}
        for i in fv.get("issues") or []:
            add(f, Issue(**{k: i.get(k) for k in Issue.__dataclass_fields__ if k in i}))
        if fv.get("issues"):
            continue
        v = fv.get("value")
        if v is None:
            if fdef["required"]:
                reason = "显式清空，不继承基准值" if fv.get("binding_state") == "cleared" else "缺失"
                add(f, Issue(Code.FIELD_MISSING, f"{fdef['label']}{reason}", f))
            continue
        if fdef["kind"] in NUMERIC_KINDS:
            d = dec(v)
            if fdef["kind"] == "price":
                if d < 0:
                    add(f, Issue(Code.VALUE_INVALID, f"{fdef['label']}不能为负（{v}）", f))
            elif d <= 0:
                add(f, Issue(Code.VALUE_INVALID, f"{fdef['label']}必须大于 0（当前 {v} {fdef['unit']}）", f))
    if (params.get("measurement_model_id") or {}).get("value") not in (None, MEASUREMENT_MODEL):
        add("measurement_model_id", Issue(Code.MODEL_UNSUPPORTED, "仅支持计量模型 rect_uniform_axis_v1",
                                          "measurement_model_id"))
    # 整除
    vals = {}
    for f in GEOMETRY_FIELDS:
        if f in out:
            continue
        v = (params.get(f) or {}).get("value")
        if v is not None:
            vals[f] = dec(v)
    for num, den in (("width_m", "grid_width_m"), ("height_m", "grid_height_m")):
        if num in vals and den in vals:
            q = vals[num] / vals[den]
            if q != q.to_integral_value():
                add(den, Issue(Code.NON_DIVISIBLE_GRID,
                               f"{FIELD_DEFS[num]['label']} {vals[num]} m 不能被{FIELD_DEFS[den]['label']} "
                               f"{vals[den]} m 整除；本版只支持等距整除分格，不取整、不生成边跨", den,
                               required_action="改为可整除的分格尺寸，或交由人工处理"))
    # 面积一致性
    ra = (params.get("reported_area_m2") or {}).get("value")
    if ra is not None and "width_m" in vals and "height_m" in vals and dec(ra) != vals["width_m"] * vals["height_m"]:
        add("reported_area_m2", Issue(Code.AREA_CONFLICT,
                                      f"申报面积 {ra} m² 与 W×H={vals['width_m'] * vals['height_m']} m² 不一致",
                                      "reported_area_m2", blocking=False))
    _, _, mat = resolve_materials(package, params)
    for i in mat:
        add(i.field_path, i)
    return out


def module_readiness(package: dict, params: dict, issues: dict[str, list[Issue]],
                     zero_acks: list[str] | None = None) -> dict[str, list[Issue]]:
    """返回每个材料模块的阻断原因列表（空列表即就绪）。"""
    zero_acks = zero_acks or []
    blockers: dict[str, list[Issue]] = {}
    hard = {f: [i for i in lst if i.blocking] for f, lst in issues.items()}
    blockers["geometry"] = [i for f in GEOMETRY_FIELDS for i in hard.get(f, [])
                            if i.code != Code.NON_DIVISIBLE_GRID]
    blockers["length"] = blockers["geometry"] + [i for f in MODULE_FIELDS["length"] for i in hard.get(f, [])
                                                 if i not in blockers["geometry"]]
    profiles, prices, _ = resolve_materials(package, params)
    mass_b = [i for f in MODULE_FIELDS["mass"] for i in hard.get(f, [])]
    for comp, label in (("column", "立柱"), ("beam", "横梁")):
        p = profiles.get(comp)
        if p is None and not hard.get(f"{comp}_profile_id"):
            mass_b.append(Issue(Code.FIELD_MISSING, f"{label}规格缺失", f"{comp}_profile_id"))
        elif p is not None and p.get("linear_mass") in (None, ""):
            mass_b.append(Issue(Code.MASS_MISSING, f"{label}线重为空（规格 {p['profile_id']}）",
                                f"{comp}_mass_override"))
        elif p is not None and dec(p["linear_mass"]) <= 0:
            mass_b.append(Issue(Code.VALUE_INVALID, f"{label}线重必须大于 0", f"{comp}_mass_override"))
    blockers["mass"] = blockers["length"] + mass_b
    cost_b = [i for f in MODULE_FIELDS["cost"] for i in hard.get(f, [])]
    if not hard.get("price_basis_id") and (params.get("price_basis_id") or {}).get("value"):
        for comp, label in (("column", "立柱"), ("beam", "横梁")):
            pr = prices.get(comp)
            if profiles.get(comp) is None:
                continue
            if pr is None or pr.get("unit_price") is None:
                cost_b.append(Issue(Code.PRICE_MISSING, f"缺少{label}报价", f"{comp}_price_override",
                                    required_action=f"补充{label}单价（CNY/kg，未税）"))
            elif dec(pr["unit_price"]) < 0:
                cost_b.append(Issue(Code.VALUE_INVALID, f"{label}单价不能为负", f"{comp}_price_override"))
            elif dec(pr["unit_price"]) == 0 and comp not in zero_acks:
                cost_b.append(Issue(Code.ZERO_PRICE_UNCONFIRMED, f"{label}单价为 0，需要确认是否确为零价",
                                    f"{comp}_price_override"))
    blockers["cost"] = blockers["mass"] + cost_b
    return blockers


QUESTION_PRIORITY = [
    # 先冲突与单位，再几何，再规格、线重、价格，最后规则所需条件
    [Code.PARAMETER_CONFLICT, Code.AMBIGUOUS_FIELD, Code.UNIT_REQUIRED, Code.UNIT_UNSUPPORTED, Code.VALUE_INVALID,
     Code.AREA_CONFLICT, Code.NON_DIVISIBLE_GRID],
]
FIELD_PRIORITY = (GEOMETRY_FIELDS + ["measurement_model_id", "column_profile_id", "beam_profile_id",
                                     "column_mass_override", "beam_mass_override", "price_basis_id",
                                     "column_price_override", "beam_price_override", "floor_height_m",
                                     "reported_area_m2", "region", "check_date"])
