"""分任务输入门槛、部分结果与单项追问。

层级：area 面积 → length 长度（需分格且整除）→ mass 质量（需两类线重）→ cost 金额（需两类未税单价）→ comparison 比较。
缺某层输入只阻断该层及以后；规则核查独立，缺参只影响对应规则，不阻断材料计算。
"""
from __future__ import annotations

from domain.contracts import Code
from domain.fields import USER_FIELDS
from validation.parameters import field_issues, module_readiness

from .projects import ROLE_NAME, basis_of, effective_params, has_candidate, material_of, price_of, project_package

LEVELS = ["area", "length", "mass", "cost"]
LEVEL_TEXT = {"area": "面积", "length": "立柱横梁长度", "mass": "型材质量", "cost": "材料金额", "comparison": "两方案比较",
              "rule_check": "规则核查"}
TASK_LEVEL = {"area": "area", "length": "length", "mass": "mass", "cost": "cost", "compare": "cost",
              "rule_check": "area", "auto": "cost"}
TASK_TEXT = {"area": "计算面积", "length": "计算立柱横梁长度", "mass": "计算型材用量", "cost": "计算材料金额",
             "compare": "比较两个方案", "rule_check": "执行规则核查", "auto": "计算"}
CODE_RANK = {Code.PARAMETER_CONFLICT: 0, Code.UNIT_REQUIRED: 0, Code.UNIT_UNSUPPORTED: 0, Code.VALUE_INVALID: 0,
             Code.NON_DIVISIBLE_GRID: 1, Code.AREA_CONFLICT: 1, Code.FIELD_MISSING: 2, Code.MASS_MISSING: 2,
             Code.PRICE_MISSING: 2, Code.PRICE_BASIS_MISMATCH: 2, Code.ZERO_PRICE_UNCONFIRMED: 3}
LEVEL_RANK = {"geometry": 0, "grid": 1, "mass": 2, "cost": 3, "rule": 4, "check": 5, "label": 6}


def _user_field(issue: dict) -> str | None:
    f = issue.get("field_path") or ""
    if f in USER_FIELDS:
        return f
    for comp in ("column", "beam"):
        if f.startswith(comp):
            if f.endswith("price_override") or issue.get("code") in (Code.PRICE_MISSING, Code.ZERO_PRICE_UNCONFIRMED):
                return f"{comp}_unit_price"
            return f"{comp}_linear_mass"
    if f == "price_basis_id":
        return "column_unit_price"
    return None


def scheme_issues(project: dict, role: str) -> dict:
    """返回 {level: [issue]}，issue 含 user_field、code、message、role。"""
    pkg = project_package(project)
    params = effective_params(project, role)
    fi = field_issues(pkg, params, "")
    zero = (project.get("zero_acks") or {}).get(role, [])
    mr = module_readiness(pkg, params, fi, zero)
    out = {lvl: [] for lvl in LEVELS}

    def add(level, issue):
        uf = _user_field(issue)
        if issue.get("code") == Code.MODEL_UNSUPPORTED:
            uf = None
        item = {"user_field": uf, "code": issue.get("code"), "message": issue.get("message"), "role": role,
                "blocking": issue.get("blocking", True)}
        if item not in out[level]:
            out[level].append(item)

    for f in ("width_m", "height_m"):
        for i in fi.get(f, []):
            if i.blocking:
                add("area", i.to_dict())
    for i in mr["length"]:
        add("length", i.to_dict())
    for i in mr["mass"]:
        if i.field_path not in ("width_m", "height_m", "grid_width_m", "grid_height_m", "measurement_model_id"):
            add("mass", i.to_dict())
    for i in mr["cost"]:
        if _user_field(i.to_dict()) and _user_field(i.to_dict()).endswith("unit_price"):
            add("cost", i.to_dict())
    # 未建立价格基准 → 两类单价都缺
    basis = basis_of(project, role)
    for comp in ("column", "beam"):
        if material_of(project, role, comp) is None or basis is None or price_of(project, role, comp) is None:
            uf = f"{comp}_unit_price"
            if not any(x["user_field"] == uf for x in out["cost"]):
                out["cost"].append({"user_field": uf, "code": Code.PRICE_MISSING, "role": role, "blocking": True,
                                    "message": f"缺少{'立柱' if comp == 'column' else '横梁'}单价"})
    if basis is not None:
        tax = pkg["price_bases"][basis["price_basis_id"]]["tax_basis"]
        if tax != "exclusive":
            out["cost"].append({"user_field": "column_unit_price", "code": Code.PRICE_BASIS_MISMATCH, "role": role,
                                "blocking": True,
                                "message": "报价为含税价或含税／未税混用；本版按未税材料金额比较，请提供未税单价"})
    # 面积冲突（非阻断，但会影响单位面积指标与比较）
    for i in fi.get("reported_area_m2", []):
        out["area"].append({"user_field": "reported_area_m2", "code": i.code, "message": i.message, "role": role,
                            "blocking": False})
    return out


def level_ready(issues: dict) -> dict:
    ready, ok = {}, True
    for lvl in LEVELS:
        ok = ok and not any(i["blocking"] for i in issues[lvl])
        ready[lvl] = ok
    return ready


def plan(project: dict | None, task: str) -> dict:
    """返回 {roles, ready: {role: {level: bool}}, issues: {role: {...}}, target_level, ready_all, comparable}"""
    if project is None:
        return {"roles": [], "ready": {}, "issues": {}, "target_level": TASK_LEVEL.get(task, "cost"),
                "ready_all": False}
    roles = ["baseline"] + (["candidate"] if has_candidate(project) else [])
    if task in ("area", "length", "mass", "cost", "rule_check") and has_candidate(project):
        run_roles = roles
    else:
        run_roles = roles
    issues = {r: scheme_issues(project, r) for r in run_roles}
    ready = {r: level_ready(issues[r]) for r in run_roles}
    tl = TASK_LEVEL.get(task, "cost")
    ready_all = all(ready[r][tl] for r in run_roles)
    if task == "compare" and not has_candidate(project):
        ready_all = False
    return {"roles": run_roles, "ready": ready, "issues": issues, "target_level": tl, "ready_all": ready_all}


def gaps_text(p: dict, role: str) -> str:
    """“长度还缺分格尺寸；质量还缺立柱线重”这类说明。"""
    parts = []
    iss = p["issues"][role]
    for lvl in LEVELS:
        blocking = [i for i in iss[lvl] if i["blocking"]]
        if not blocking:
            continue
        labels = []
        for i in blocking:
            uf = i.get("user_field")
            if i["code"] == Code.NON_DIVISIBLE_GRID:
                labels.append("可整除的分格（当前尺寸不能整除）")
            elif i["code"] in (Code.VALUE_INVALID, Code.UNIT_REQUIRED, Code.UNIT_UNSUPPORTED):
                labels.append(f"{USER_FIELDS.get(uf, {}).get('label', uf)}（{i['message']}）")
            elif uf:
                labels.append(USER_FIELDS[uf]["label"])
        if labels:
            parts.append(f"{LEVEL_TEXT[lvl]}还缺{'、'.join(dict.fromkeys(labels))}")
    return "；".join(parts)


def pick_question(p: dict, skipped: list[str], *, task: str) -> dict | None:
    """只挑一个最影响当前任务结果的问题。"""
    tl = TASK_LEVEL.get(task, "cost")
    upto = LEVELS[:LEVELS.index(tl) + 1]
    cands = []
    multi = len(p["roles"]) > 1
    for role in p["roles"]:
        for lvl in upto:
            for i in p["issues"][role][lvl]:
                uf = i.get("user_field")
                if not uf or not i["blocking"] or f"{role}:{uf}" in skipped:
                    continue
                cands.append((CODE_RANK.get(i["code"], 2), LEVEL_RANK[USER_FIELDS[uf]["level"]],
                              0 if role == "baseline" else 1, list(USER_FIELDS).index(uf), role, uf, i))
    if not cands:
        return None
    cands.sort(key=lambda x: x[:4])
    *_, role, uf, issue = cands[0]
    # 调整方案只是继承了原方案的缺项时，问原方案
    prefix = f"{ROLE_NAME[role]}的" if multi else ""
    d = USER_FIELDS[uf]
    code = issue["code"]
    options = None
    if code == Code.ZERO_PRICE_UNCONFIRMED:
        text = f"{prefix}{d['label']}为 0，是否确认确为零价？（空值不会被当作 0）"
        options = ["确认为零价", "不是，我重新输入"]
    elif code == Code.PRICE_BASIS_MISMATCH:
        text = f"{prefix}报价是含税价（或含税／未税混用）。本版按未税材料金额计算，请提供未税单价（元/kg 或 元/吨）。"
    elif code == Code.NON_DIVISIBLE_GRID:
        text = (f"{issue['message']}。你可以保留真实尺寸（只计算面积等可算部分）、提供可整除的分格，"
                "或交由工程师按边跨处理。要修改分格吗？")
    elif code in (Code.UNIT_REQUIRED, Code.UNIT_UNSUPPORTED, Code.VALUE_INVALID, Code.PARAMETER_CONFLICT):
        text = f"{prefix}{d['label']}：{issue['message']}。请重新输入数值和单位。"
    else:
        text = f"{prefix}{d.get('question') or d['label'] + '是多少？请输入数值和单位。'}"
        if not d.get("question") and d["unit"]:
            text = f"{prefix}{d['label']}是多少？请输入数值和单位（{d['unit']}）。"
    return {"field": uf, "role": role, "code": code, "text": text, "options": options,
            "reason": issue["message"], "task": task}
