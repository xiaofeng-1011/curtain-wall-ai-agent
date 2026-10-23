"""TL09 calculate_mass：Mc=Lc×μc、Mt=Lt×μt、M=Mc+Mt 及单位面积质量（MASS-v1）。

线重只取快照中解析好的规格记录或显式覆盖值，不由规格名称猜测。
"""
from __future__ import annotations

from domain.contracts import Code, Issue, ToolResponse, calc, dec, merge_statuses

from .common import Calc, check_upstream, upstream_metric

KEYS = ["column_mass_kg", "beam_mass_kg", "total_mass_kg",
        "column_mass_per_m2", "beam_mass_per_m2", "total_mass_per_m2"]


def resolve_linear_mass(c: Calc, component: str, module: str):
    prof = (c.snapshot.get("resolved_profiles") or {}).get(component)
    label = "立柱" if component == "column" else "横梁"
    if not prof:
        c.issues.append(Issue(Code.PROFILE_MISSING, f"{label}规格缺失或不存在", f"{component}_profile_id", module))
        return None
    if prof.get("component_type") != component:
        c.issues.append(Issue(Code.PROFILE_TYPE_MISMATCH, f"{label}引用了 {prof.get('component_type')} 型材",
                              f"{component}_profile_id", module))
        return None
    lm = prof.get("linear_mass")
    if lm in (None, ""):
        c.issues.append(Issue(Code.MASS_MISSING, f"{label}线重为空", f"{component}_profile_id", module))
        return None
    d = dec(lm)
    if d <= 0:
        c.issues.append(Issue(Code.VALUE_INVALID, f"{label}线重必须大于 0，当前 {lm}",
                              f"{component}_mass_override" if prof.get("linear_mass_source") == "override"
                              else f"{component}_profile_id", module))
        return None
    ref = f"snapshot:{c.snapshot.get('snapshot_id')}#resolved_profiles.{component}.linear_mass"
    return d, ref, c.statuses_for(prof.get("provenance_ids"))


def calculate_mass(snapshot: dict, length_result: dict | None, geometry_result: dict | None) -> ToolResponse:
    c = Calc("TL09", snapshot)
    mod = "mass"
    payload = {"snapshot_hash": snapshot.get("content_hash"),
               "length_result_hash": (length_result or {}).get("result_hash"),
               "geometry_result_hash": (geometry_result or {}).get("result_hash")}
    for up, name in ((length_result, "TL08"), (geometry_result, "TL07")):
        bad = check_upstream(snapshot, up, name, mod)
        if bad:
            c.issues.append(bad)
            return c.response(KEYS, payload)
    Lc, Lt = upstream_metric(length_result, "column_length_m"), upstream_metric(length_result, "beam_length_m")
    if not (Lc and Lt):
        c.issues.append(Issue(Code.UPSTREAM_NOT_DONE, "理论长度未完成，不计算质量", None, mod))
        return c.response(KEYS, payload)
    mu_c, mu_t = resolve_linear_mass(c, "column", mod), resolve_linear_mass(c, "beam", mod)
    if not (mu_c and mu_t):
        # 不生成不完整的总质量
        return c.response(KEYS, payload)
    Mc = calc("*", Lc[0], mu_c[0])
    s1 = c.step("column_mass_kg", "MASS-v1", "*", [Lc[:2], mu_c[:2]], Mc, f"Mc = Lc × μc = {Lc[0]} × {mu_c[0]}")
    c.metric("column_mass_kg", Mc, "MASS-v1", [Lc[1], mu_c[1]], merge_statuses(Lc[2], mu_c[2]), [s1])
    Mt = calc("*", Lt[0], mu_t[0])
    s2 = c.step("beam_mass_kg", "MASS-v1", "*", [Lt[:2], mu_t[:2]], Mt, f"Mt = Lt × μt = {Lt[0]} × {mu_t[0]}")
    c.metric("beam_mass_kg", Mt, "MASS-v1", [Lt[1], mu_t[1]], merge_statuses(Lt[2], mu_t[2]), [s2])
    M = calc("+", Mc, Mt)
    s3 = c.step("total_mass_kg", "MASS-v1", "+", [(Mc, s1), (Mt, s2)], M, f"M = Mc + Mt = {Mc} + {Mt}")
    st_all = merge_statuses(c.metrics["column_mass_kg"].source_statuses, c.metrics["beam_mass_kg"].source_statuses)
    c.metric("total_mass_kg", M, "MASS-v1", [s1, s2], st_all, [s1, s2, s3])

    A = upstream_metric(geometry_result, "facade_area_m2")
    area_conflict = any(i.get("code") == Code.AREA_CONFLICT for i in geometry_result.get("issues", []))
    if A and A[0] > 0 and not area_conflict:
        for key, val, sid, sts in (("column_mass_per_m2", Mc, s1, c.metrics["column_mass_kg"].source_statuses),
                                   ("beam_mass_per_m2", Mt, s2, c.metrics["beam_mass_kg"].source_statuses),
                                   ("total_mass_per_m2", M, s3, st_all)):
            v = calc("/", val, A[0])
            s = c.step(key, "MASS-v1", "/", [(val, sid), A[:2]], v, f"{key} = {val} / {A[0]}")
            c.metric(key, v, "MASS-v1", [sid, A[1]], merge_statuses(sts, A[2]), [sid, s])
    else:
        c.issues.append(Issue(Code.AREA_CONFLICT if area_conflict else Code.UPSTREAM_NOT_DONE,
                              "立面面积不可用或存在冲突，单位面积质量未计算", "reported_area_m2", mod, blocking=False))
    return c.response(KEYS[:3], payload)
