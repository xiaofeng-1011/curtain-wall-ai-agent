"""项目领域数据：方案、材料、报价、规则、来源与撤销。

- 项目由用户建立；方案只有“原方案”（baseline）和“调整方案”（candidate）两个角色，ID 由系统生成，
  不依赖任何预设编号。调整方案只保存与原方案不同的字段，其余字段实时继承原方案（修正原方案会自动同步）。
- 材料（型材）与报价按需在项目内建立：调整方案改线重时复制一份新材料，改单价时复制一份新的价格基准，
  从而不会改动原方案的取值。
- project_package() 把项目转换为计算／校验／规则内核使用的“数据包视图”。
"""
from __future__ import annotations

import copy

from domain.contracts import GenerationMethod, ReviewStatus, SourceStatus, SourceType, new_id, now_utc, sha256_of
from domain.fields import FIELD_DEFS, USER_FIELDS
from validation.parameters import apply_edit, field_value

ROLES = ("baseline", "candidate")
ROLE_NAME = {"baseline": "原方案", "candidate": "调整方案"}
COMP_NAME = {"column": "立柱", "beam": "横梁"}
UNDO_LIMIT = 30


# ------------------------------------------------------------------ 创建
def new_project(name: str) -> dict:
    return {
        "project_id": new_id("PRJ"), "name": name or "未命名项目",
        "curtain_wall_type": "component_glass", "design_stage": "scheme_design",
        "default_source": None, "measurement_confirmed": False, "data_rev": 1,
        "schemes": {"baseline": new_scheme("baseline")}, "materials": {}, "price_bases": {}, "prices": {},
        "rules": {}, "provenance": {}, "undo": [], "zero_acks": {"baseline": [], "candidate": []},
        "seq": 0,
    }


def new_scheme(role: str) -> dict:
    s = {"scheme_id": role, "role": role, "name": ROLE_NAME[role], "rev": 1}
    if role == "baseline":
        s["params"] = {f: field_value(None, d["unit"]) for f, d in FIELD_DEFS.items()}
        s["params"]["measurement_model_id"] = field_value("rect_uniform_axis_v1", None, binding_state="modified",
                                                          provenance_ids=["PROV-SYSTEM"],
                                                          generation_method=GenerationMethod.PROVIDED.value)
    else:
        s["overrides"] = {}
    return s


def _seq(project: dict, prefix: str) -> str:
    project["seq"] = project.get("seq", 0) + 1
    return f"{prefix}{project['seq']:03d}"


SYSTEM_PROV = {"provenance_id": "PROV-SYSTEM", "source_status": SourceStatus.PENDING.value,
               "source_type": SourceType.UNKNOWN.value, "review_status": ReviewStatus.UNREVIEWED.value,
               "generation_method": GenerationMethod.PROVIDED.value,
               "source_ref": "系统默认计量模型 rect_uniform_axis_v1（需工程师确认口径）"}


def add_provenance(project: dict, *, declared: str | None, source_ref: str, location: str | None,
                   generation_method: str = GenerationMethod.PROVIDED.value, notes: str | None = None) -> str:
    """按实际输入记录来源：声明测试／演示 → synthetic；未声明 → pending（保留“用户提供”的具体出处）。"""
    status = declared or project.get("default_source") or SourceStatus.PENDING.value
    if status not in (SourceStatus.SYNTHETIC.value, SourceStatus.PENDING.value):
        status = SourceStatus.PENDING.value  # 用户不能直接把输入标为 verified/public
    pid = _seq(project, "PROV-")
    project["provenance"][pid] = {
        "provenance_id": pid, "source_status": status,
        "source_type": SourceType.TEAM_CONSTRUCTED.value if status == "synthetic" else SourceType.UNKNOWN.value,
        "review_status": ReviewStatus.UNREVIEWED.value, "generation_method": generation_method,
        "source_ref": source_ref, "location": location, "captured_at": now_utc(),
        "notes": notes or ("用户声明为测试／演示数据" if status == "synthetic"
                           else "用户提供，出处待核实；本次使用确认不等于专业复核"),
    }
    return pid


# ------------------------------------------------------------------ 读取
def has_candidate(project: dict) -> bool:
    return "candidate" in project["schemes"]


def effective_params(project: dict, role: str) -> dict:
    base = project["schemes"]["baseline"]["params"]
    if role == "baseline":
        return copy.deepcopy(base)
    ov = project["schemes"]["candidate"].get("overrides", {})
    out = {}
    for f in FIELD_DEFS:
        if f in ov:
            out[f] = copy.deepcopy(ov[f])
        else:
            fv = copy.deepcopy(base[f])
            if fv.get("value") is not None:
                fv.update(binding_state="inherited", inherited_from=f"baseline#parameters.{f}")
            out[f] = fv
    return out


def material_of(project: dict, role: str, comp: str) -> dict | None:
    mid = effective_params(project, role)[f"{comp}_profile_id"].get("value")
    return project["materials"].get(mid) if mid else None


def price_of(project: dict, role: str, comp: str) -> dict | None:
    eff = effective_params(project, role)
    pb, mid = eff["price_basis_id"].get("value"), eff[f"{comp}_profile_id"].get("value")
    return project["prices"].get(f"{pb}::{mid}") if pb and mid else None


def basis_of(project: dict, role: str) -> dict | None:
    pb = effective_params(project, role)["price_basis_id"].get("value")
    return project["price_bases"].get(pb) if pb else None


def user_value(project: dict, role: str, ufield: str):
    """返回面向用户的当前值（字符串）与单位；不存在返回 (None, unit)。"""
    d = USER_FIELDS[ufield]
    if "component" not in d:
        fv = effective_params(project, role)[ufield]
        return fv.get("value"), d["unit"]
    comp, attr = d["component"], d["attr"]
    if attr == "unit_price":
        p = price_of(project, role, comp)
        return (p or {}).get("unit_price"), "元/kg"
    m = material_of(project, role, comp)
    if attr == "linear_mass":
        return (m or {}).get("linear_mass"), "kg/m"
    return (m or {}).get("profile_name"), None


def field_state(project: dict, role: str, ufield: str) -> str:
    """inherited / modified / cleared / missing（调整方案中材料类字段按引用判断继承）。"""
    d = USER_FIELDS[ufield]
    if role == "baseline":
        v, _ = user_value(project, role, ufield)
        return "modified" if v is not None else "missing"
    if "component" not in d:
        return effective_params(project, role)[ufield].get("binding_state", "missing")
    comp, attr = d["component"], d["attr"]
    ov = project["schemes"]["candidate"].get("overrides", {})
    key = "price_basis_id" if attr == "unit_price" else f"{comp}_profile_id"
    if key not in ov:
        return "inherited" if user_value(project, role, ufield)[0] is not None else "missing"
    same = user_value(project, "baseline", ufield)[0] == user_value(project, role, ufield)[0]
    return "inherited" if same else ("modified" if user_value(project, role, ufield)[0] is not None else "missing")


# ------------------------------------------------------------------ 撤销
def push_undo(project: dict, label: str) -> None:
    state = {k: copy.deepcopy(project[k]) for k in ("schemes", "materials", "price_bases", "prices", "zero_acks",
                                                    "rules")}
    project["undo"] = (project.get("undo") or [])[-(UNDO_LIMIT - 1):] + [{"label": label, "at": now_utc(),
                                                                          "state": state}]


def undo(project: dict) -> str | None:
    if not project.get("undo"):
        return None
    last = project["undo"].pop()
    for k, v in last["state"].items():
        project[k] = v
    project["data_rev"] += 1
    return last["label"]


# ------------------------------------------------------------------ 修改
def ensure_candidate(project: dict) -> bool:
    if has_candidate(project):
        return False
    project["schemes"]["candidate"] = new_scheme("candidate")
    project["zero_acks"]["candidate"] = list(project["zero_acks"].get("baseline", []))
    return True


def _set_param(project: dict, role: str, field: str, op: str, raw_value=None, raw_unit=None, *, prov: str,
               gm: str) -> None:
    s = project["schemes"][role]
    if role == "baseline":
        s["params"] = apply_edit(s["params"], field, op, raw_value, raw_unit, provenance_ids=[prov],
                                 generation_method=gm)
    else:
        cur = effective_params(project, "candidate")
        new = apply_edit(cur, field, op, raw_value, raw_unit, provenance_ids=[prov], generation_method=gm)
        base_v = project["schemes"]["baseline"]["params"][field].get("value")
        if op == "set" and new[field].get("value") == base_v and not new[field].get("issues") and base_v is not None:
            s["overrides"].pop(field, None)  # 改回与原方案相同 → 恢复继承
        else:
            s["overrides"][field] = new[field]
    s["rev"] += 1


def set_geometry(project: dict, role: str, field: str, op: str, raw_value, raw_unit, *, prov: str, gm: str) -> None:
    _set_param(project, role, field, op, raw_value, raw_unit, prov=prov, gm=gm)


def _material_for_write(project: dict, role: str, comp: str) -> dict:
    """取得本方案可写的材料；若材料属于另一个方案（继承而来），复制一份新材料（copy-on-write）。"""
    mid = effective_params(project, role)[f"{comp}_profile_id"].get("value")
    mat = project["materials"].get(mid) if mid else None
    if mat is not None and mat.get("created_for") == role:
        return mat
    new_mid = _seq(project, "MAT-")
    if mat is None:
        mat = {"profile_id": new_mid, "component_type": comp,
               "profile_name": f"{ROLE_NAME[role]}{COMP_NAME[comp]}", "linear_mass": None, "unit": "kg/m",
               "provenance_id": None, "created_for": role, "derived_from": None,
               "applicability_notes": "用户录入的材料记录；工程适用性未确认，不由名称或线重推断承载性能"}
    else:
        mat = dict(copy.deepcopy(mat), profile_id=new_mid, created_for=role, derived_from=mat["profile_id"],
                   profile_name=f"{ROLE_NAME[role]}{COMP_NAME[comp]}")
    project["materials"][new_mid] = mat
    _set_param(project, role, f"{comp}_profile_id", "set", new_mid, None, prov="PROV-SYSTEM",
               gm=GenerationMethod.PROGRAM_DERIVED.value)
    # 新材料在当前价格基准下暂无报价：不自动沿用旧价格（不同型材价格可能不同）
    return mat


def _basis_for_write(project: dict, role: str, tax: str | None) -> dict:
    pb_id = effective_params(project, role)["price_basis_id"].get("value")
    pb = project["price_bases"].get(pb_id) if pb_id else None
    if pb is not None and pb.get("created_for") == role:
        return pb
    new_pb = _seq(project, "PB-")
    nb = {"price_basis_id": new_pb, "price_basis_version": "1", "name": f"{ROLE_NAME[role]}报价",
          "currency": "CNY", "price_unit": "CNY/kg", "tax_basis": "exclusive", "cost_scope": "aluminum_material_only",
          "created_for": role, "provenance_id": "PROV-SYSTEM", "quote_date": None}
    project["price_bases"][new_pb] = nb
    if pb is not None:  # 复制原有报价，再修改本方案的单价
        for key, pr in list(project["prices"].items()):
            if key.startswith(f"{pb_id}::"):
                project["prices"][f"{new_pb}::{pr['profile_id']}"] = dict(copy.deepcopy(pr), price_basis_id=new_pb)
    _set_param(project, role, "price_basis_id", "set", new_pb, None, prov="PROV-SYSTEM",
               gm=GenerationMethod.PROGRAM_DERIVED.value)
    return nb


def set_material(project: dict, role: str, ufield: str, op: str, value: str | None, *, prov: str,
                 raw: str | None = None, tax: str | None = None) -> None:
    """value 已由单位工具归一（kg/m、元/kg）；raw 为原始表达。"""
    d = USER_FIELDS[ufield]
    comp, attr = d["component"], d["attr"]
    if attr in ("linear_mass", "name"):
        mat = _material_for_write(project, role, comp)
        if attr == "linear_mass":
            mat["linear_mass"] = None if op == "clear" else value
            mat["provenance_id"] = prov
            mat["raw"] = raw
        else:
            mat["profile_name"] = value or mat["profile_name"]
    else:
        pb = _basis_for_write(project, role, tax)
        mat = material_of(project, role, comp)
        if mat is None:
            mat = _material_for_write(project, role, comp)
        key = f"{pb['price_basis_id']}::{mat['profile_id']}"
        if op == "clear":
            project["prices"].pop(key, None)
        else:
            project["prices"][key] = {"price_basis_id": pb["price_basis_id"], "profile_id": mat["profile_id"],
                                      "unit_price": value, "currency": "CNY", "unit": "CNY/kg",
                                      "tax_basis": tax or "exclusive", "price_basis_version": "1",
                                      "provenance_id": prov, "raw": raw}
    project["schemes"][role]["rev"] += 1


def reuse_price(project: dict, role: str, comp: str, *, prov: str) -> bool:
    """“沿用刚才／原来的价格”：只从本项目中该材料的来源材料或原方案同类构件取价。"""
    mat = material_of(project, role, comp)
    candidates = []
    if mat and mat.get("derived_from"):
        candidates.append(mat["derived_from"])
    other = "baseline" if role == "candidate" else None
    if other:
        om = material_of(project, other, comp)
        if om:
            candidates.append(om["profile_id"])
    for src in candidates:
        for key, pr in project["prices"].items():
            if pr["profile_id"] == src:
                set_material(project, role, f"{comp}_unit_price", "set", pr["unit_price"], prov=prov,
                             raw=f"沿用 {pr['unit_price']} 元/kg", tax=pr.get("tax_basis"))
                return True
    return False


def bump(project: dict) -> None:
    project["data_rev"] = project.get("data_rev", 1) + 1


# ------------------------------------------------------------------ 规则（用户主动建立的演示规则）
def add_demo_rule(project: dict, *, title: str, lhs: str, operator: str, threshold: str, unit: str) -> str:
    rid = _seq(project, "RULE-")
    prov = add_provenance(project, declared="synthetic", source_ref="用户在项目中建立的演示规则", location=rid,
                          notes="演示规则：只验证流程，不是正式规范条款")
    req = ["grid_width_m", "grid_height_m"] if lhs == "panel_area_m2" else (
        ["width_m", "height_m"] if lhs == "facade_area_m2" else [lhs])
    project["rules"][rid] = {
        "rule_id": rid, "rule_version": "1", "evidence_type": "demo_rule", "collection": "demo_rules",
        "title": title, "exact_text": f"【用户建立的演示规则】{title}：{lhs} {operator} {threshold} {unit}",
        "source_ref": "用户建立的演示规则", "location": f"项目 {project['name']}",
        "standard_name": None, "standard_number": None, "edition": None, "clause_number": None,
        "effect_status": "not_applicable", "replaced_by": [],
        "applicability": {"curtain_wall_type": ["component_glass"], "design_stage": ["scheme_design"]},
        "required_parameters": req, "machine_checkable": True,
        "predicate": {"lhs": lhs, "operator": operator, "threshold": {"value": threshold, "unit": unit}},
        "required_tools": ["TL07"] if lhs.endswith("_m2") else [], "attention_text": "演示关注项触发，需工程师确认",
        "provenance_id": prov, "effect_review": {"review_status": "unreviewed"},
        "mapping_review": {"review_status": "unreviewed"},
    }
    return rid


# ------------------------------------------------------------------ 数据包视图
def _basis_tax(project: dict, pb_id: str) -> str:
    taxes = {pr.get("tax_basis") or "exclusive" for k, pr in project["prices"].items() if k.startswith(f"{pb_id}::")}
    if not taxes:
        return project["price_bases"][pb_id].get("tax_basis", "exclusive")
    return taxes.pop() if len(taxes) == 1 else "mixed"


def project_package(project: dict) -> dict:
    prov = {"PROV-SYSTEM": SYSTEM_PROV, **copy.deepcopy(project["provenance"])}
    bases = {}
    for pb_id, pb in project["price_bases"].items():
        bases[pb_id] = dict(pb, tax_basis=_basis_tax(project, pb_id))
    prices = {k: dict(v, tax_basis=bases[v["price_basis_id"]]["tax_basis"]) for k, v in project["prices"].items()
              if v["price_basis_id"] in bases}
    profiles = {mid: {k: v for k, v in m.items() if k != "raw"} | {"provenance_id": m.get("provenance_id")
                                                                       or "PROV-SYSTEM"}
                for mid, m in project["materials"].items()}
    rules = copy.deepcopy(project.get("rules") or {})
    rule_sets = {}
    if rules:
        rule_sets["RULES"] = {"rule_set_id": "RULES", "rule_set_version": str(project["data_rev"]),
                              "name": f"项目演示规则（{len(rules)} 条，正式条款 0 条）", "required_count": len(rules),
                              "members": [{"item_id": f"ITEM-{i + 1}", "rule_id": rid, "display_order": i + 1,
                                           "required": True} for i, rid in enumerate(rules)],
                              "provenance_id": "PROV-SYSTEM",
                              "coverage_notes": "用户建立的演示规则；不代表正式规范核查"}
    pkg = {
        "dataset_id": project["project_id"], "dataset_version": str(project["data_rev"]), "schema_version": "0.2",
        "name": project["name"], "is_demo": False, "default_case_id": project["project_id"],
        "provenance": prov,
        "cases": {project["project_id"]: {
            "case_id": project["project_id"], "case_name": {"value": project["name"], "provenance_ids": []},
            "curtain_wall_type": {"value": project["curtain_wall_type"], "provenance_ids": []},
            "design_stage": {"value": project["design_stage"], "provenance_ids": []},
            "required_rule_set_id": "RULES" if rules else None}},
        "profiles": profiles, "price_bases": bases, "prices": prices, "rules": rules, "rule_sets": rule_sets,
        "seeds": {},
    }
    pkg["content_hash"] = sha256_of({k: pkg[k] for k in ("profiles", "price_bases", "prices", "rules",
                                                         "provenance")})
    return pkg


def summary_rows(project: dict, role: str) -> list[tuple[str, str]]:
    """面向用户的参数摘要（字段名、值与单位）。"""
    rows = []
    for uf, d in USER_FIELDS.items():
        v, unit = user_value(project, role, uf)
        if v is None and uf in ("floor_height_m", "reported_area_m2", "column_name", "beam_name"):
            continue
        rows.append((d["label"], "未填写" if v is None else f"{_fmt(v)} {unit or ''}".strip()))
    return rows


def _fmt(v) -> str:
    try:
        from decimal import Decimal
        return format(Decimal(str(v)).normalize(), "f")
    except Exception:  # noqa: BLE001
        return str(v)
