"""TL07 calculate_geometry：立面面积、板块面积、格数（GEO-AREA/PANEL/GRID-v1）。"""
from __future__ import annotations

from domain.contracts import Code, Issue, ToolResponse, calc, dec, merge_statuses

from .common import Calc

ALL_METRICS = ("facade_area", "panel_area", "grid_counts")


def calculate_geometry(snapshot: dict, requested_metrics=ALL_METRICS) -> ToolResponse:
    c = Calc("TL07", snapshot)
    requested_keys: list[str] = []
    mod = "geometry"

    if "facade_area" in requested_metrics:
        requested_keys.append("facade_area_m2")
        W, H = c.param("width_m", module=mod), c.param("height_m", module=mod)
        if W and H:
            A = calc("*", W[0], H[0])
            s = c.step("facade_area_m2", "GEO-AREA-v1", "*", [W[:2], H[:2]], A, f"A = W × H = {W[0]} × {H[0]}")
            c.metric("facade_area_m2", A, "GEO-AREA-v1", [W[1], H[1]], merge_statuses(W[2], H[2]), [s])
            rep = (snapshot.get("parameters") or {}).get("reported_area_m2") or {}
            if rep.get("value") not in (None, ""):
                if dec(rep["value"]) != A:
                    c.issues.append(Issue(Code.AREA_CONFLICT,
                                          f"申报面积 {rep['value']} m² 与 W×H={A} m² 不一致；单位面积指标将不计算",
                                          "reported_area_m2", mod))

    if "panel_area" in requested_metrics:
        requested_keys.append("panel_area_m2")
        b, h = c.param("grid_width_m", module=mod), c.param("grid_height_m", module=mod)
        if b and h:
            a = calc("*", b[0], h[0])
            s = c.step("panel_area_m2", "GEO-PANEL-v1", "*", [b[:2], h[:2]], a, f"a = b × h = {b[0]} × {h[0]}")
            c.metric("panel_area_m2", a, "GEO-PANEL-v1", [b[1], h[1]], merge_statuses(b[2], h[2]), [s])

    if "grid_counts" in requested_metrics:
        requested_keys += ["nx", "ny"]
        vals = {f: c.param(f, module=mod) for f in ("width_m", "height_m", "grid_width_m", "grid_height_m")}
        if all(vals.values()):
            for key, num, den in (("nx", "width_m", "grid_width_m"), ("ny", "height_m", "grid_height_m")):
                n, d = vals[num], vals[den]
                q = calc("/", n[0], d[0])
                if q != q.to_integral_value() or q <= 0:
                    c.issues.append(Issue(
                        Code.NON_DIVISIBLE_GRID,
                        f"{num}/{den} = {n[0]}/{d[0]} 不是正整数；本版只支持等距整除分格，不取整、不生成边跨",
                        den, mod, required_action="改为可整除的分格尺寸或交由人工处理"))
                    continue
                q = q.to_integral_value()
                s = c.step(key, "GEO-GRID-v1", "/", [n[:2], d[:2]], q, f"{key} = {n[0]} / {d[0]}")
                c.metric(key, q, "GEO-GRID-v1", [n[1], d[1]], merge_statuses(n[2], d[2]), [s])

    # 同一字段在多个指标中缺失时只保留一条 Issue
    uniq, seen = [], set()
    for i in c.issues:
        k = (i.code, i.field_path)
        if k not in seen:
            seen.add(k)
            uniq.append(i)
    c.issues = uniq
    return c.response(requested_keys, {"snapshot_hash": snapshot.get("content_hash"),
                                       "requested": sorted(requested_metrics)})
