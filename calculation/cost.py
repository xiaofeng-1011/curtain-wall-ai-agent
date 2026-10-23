"""TL10 calculate_cost：Cc=Mc×pc、Ct=Mt×pt、C=Cc+Ct、单位面积金额（COST-v1）。

报价由 (price_basis_id, profile_id) 在冻结快照中唯一定位；缺任一报价不返回完整总成本，
零价必须有显式确认，null 不等于 0。
"""
from __future__ import annotations

from domain.contracts import Code, Issue, ToolResponse, calc, dec, merge_statuses

from .common import Calc, check_upstream, upstream_metric

KEYS = ["column_cost_cny", "beam_cost_cny", "total_cost_cny", "cost_per_m2"]


def _price(c: Calc, component: str, module: str):
    label = "立柱" if component == "column" else "横梁"
    pr = (c.snapshot.get("resolved_prices") or {}).get(component)
    if not pr or pr.get("unit_price") in (None, ""):
        c.issues.append(Issue(Code.PRICE_MISSING, f"缺少{label}报价", f"{component}_price_override", module,
                              required_action=f"补充{label}报价或检查价格基准"))
        return None
    p = dec(pr["unit_price"])
    if p < 0:
        c.issues.append(Issue(Code.VALUE_INVALID, f"{label}单价不能为负：{pr['unit_price']}",
                              f"{component}_price_override", module))
        return None
    acks = (c.snapshot.get("confirmation_record") or {}).get("zero_price_acknowledgements") or []
    if p == 0 and component not in acks:
        c.issues.append(Issue(Code.ZERO_PRICE_UNCONFIRMED, f"{label}单价为 0 但未确认确为零价",
                              f"{component}_price_override", module))
        return None
    ref = f"snapshot:{c.snapshot.get('snapshot_id')}#resolved_prices.{component}.unit_price"
    return p, ref, c.statuses_for(pr.get("provenance_ids")), pr


def calculate_cost(snapshot: dict, mass_result: dict | None, geometry_result: dict | None) -> ToolResponse:
    c = Calc("TL10", snapshot)
    mod = "cost"
    payload = {"snapshot_hash": snapshot.get("content_hash"),
               "mass_result_hash": (mass_result or {}).get("result_hash"),
               "geometry_result_hash": (geometry_result or {}).get("result_hash")}
    for up, name in ((mass_result, "TL09"), (geometry_result, "TL07")):
        bad = check_upstream(snapshot, up, name, mod)
        if bad:
            c.issues.append(bad)
            return c.response(KEYS, payload)
    Mc, Mt = upstream_metric(mass_result, "column_mass_kg"), upstream_metric(mass_result, "beam_mass_kg")
    if not (Mc and Mt):
        c.issues.append(Issue(Code.UPSTREAM_NOT_DONE, "型材质量未完成，不计算成本", None, mod))
        return c.response(KEYS, payload)
    pc, pt = _price(c, "column", mod), _price(c, "beam", mod)
    if not (pc and pt):
        return c.response(KEYS, payload)
    basis_keys = ("price_basis_id", "price_basis_version", "currency", "price_unit", "tax_basis", "cost_scope")
    if any(pc[3].get(k) != pt[3].get(k) for k in basis_keys):
        c.issues.append(Issue(Code.PRICE_BASIS_MISMATCH, "立柱与横梁报价口径不一致，拒绝合计", "price_basis_id", mod))
        return c.response(KEYS, payload)
    if pc[3].get("tax_basis") != "exclusive":
        # 本版按未税材料金额计算；含税价不默认换算为未税价
        c.issues.append(Issue(Code.PRICE_BASIS_MISMATCH, "报价为含税价或含税／未税混用，本版只按未税材料金额计算；"
                                                         "请提供未税单价", "price_basis_id", mod))
        return c.response(KEYS, payload)

    Cc = calc("*", Mc[0], pc[0])
    s1 = c.step("column_cost_cny", "COST-v1", "*", [Mc[:2], pc[:2]], Cc, f"Cc = Mc × pc = {Mc[0]} × {pc[0]}")
    c.metric("column_cost_cny", Cc, "COST-v1", [Mc[1], pc[1]], merge_statuses(Mc[2], pc[2]), [s1])
    Ct = calc("*", Mt[0], pt[0])
    s2 = c.step("beam_cost_cny", "COST-v1", "*", [Mt[:2], pt[:2]], Ct, f"Ct = Mt × pt = {Mt[0]} × {pt[0]}")
    c.metric("beam_cost_cny", Ct, "COST-v1", [Mt[1], pt[1]], merge_statuses(Mt[2], pt[2]), [s2])
    C = calc("+", Cc, Ct)
    st = merge_statuses(c.metrics["column_cost_cny"].source_statuses, c.metrics["beam_cost_cny"].source_statuses)
    s3 = c.step("total_cost_cny", "COST-v1", "+", [(Cc, s1), (Ct, s2)], C, f"C = Cc + Ct = {Cc} + {Ct}")
    c.metric("total_cost_cny", C, "COST-v1", [s1, s2], st, [s1, s2, s3])

    A = upstream_metric(geometry_result, "facade_area_m2")
    area_conflict = any(i.get("code") == Code.AREA_CONFLICT for i in geometry_result.get("issues", []))
    if A and A[0] > 0 and not area_conflict:
        v = calc("/", C, A[0])
        s4 = c.step("cost_per_m2", "COST-v1", "/", [(C, s3), A[:2]], v, f"c = C / A = {C} / {A[0]}")
        c.metric("cost_per_m2", v, "COST-v1", [s3, A[1]], merge_statuses(st, A[2]), [s3, s4])
    else:
        c.issues.append(Issue(Code.AREA_CONFLICT if area_conflict else Code.UPSTREAM_NOT_DONE,
                              "立面面积不可用或存在冲突，单位面积金额未计算", "reported_area_m2", mod, blocking=False))
    return c.response(KEYS[:3], payload)
