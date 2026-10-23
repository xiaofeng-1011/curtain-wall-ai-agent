"""TL08 calculate_lengths：rect_uniform_axis_v1 理论轴线长度（QTY-COLUMN/BEAM-v1）。

左右边柱、顶底横梁均计入；横梁按分格轴线计长，不扣柱宽或连接间隙；不计损耗与接头。
"""
from __future__ import annotations

from decimal import Decimal

from domain.contracts import MEASUREMENT_MODEL, Code, Issue, ToolResponse, calc, merge_statuses

from .common import Calc, check_upstream, upstream_metric

KEYS = ["column_count", "beam_rows", "beam_segments", "column_length_m", "beam_length_m"]


def calculate_lengths(snapshot: dict, geometry_result: dict | None) -> ToolResponse:
    c = Calc("TL08", snapshot)
    mod = "length"
    payload = {"snapshot_hash": snapshot.get("content_hash"),
               "geometry_result_hash": (geometry_result or {}).get("result_hash")}
    bad = check_upstream(snapshot, geometry_result, "TL07", mod)
    if bad:
        c.issues.append(bad)
        return c.response(KEYS, payload)
    model = ((snapshot.get("parameters") or {}).get("measurement_model_id") or {}).get("value")
    if model != MEASUREMENT_MODEL:
        c.issues.append(Issue(Code.MODEL_UNSUPPORTED, f"计量模型 {model or '未指定'} 不受支持，仅支持 {MEASUREMENT_MODEL}",
                              "measurement_model_id", mod))
        return c.response(KEYS, payload)
    nx, ny = upstream_metric(geometry_result, "nx"), upstream_metric(geometry_result, "ny")
    W, H = c.param("width_m", module=mod), c.param("height_m", module=mod)
    if not (nx and ny):
        c.issues.append(Issue(Code.UPSTREAM_NOT_DONE, "格数未计算（几何缺参或非整除），不计算长度", None, mod))
        return c.response(KEYS, payload)
    if not (W and H):
        return c.response(KEYS, payload)
    one = (Decimal(1), "const:1")

    Nc = calc("+", nx[0], Decimal(1))
    s1 = c.step("column_count", "QTY-COLUMN-v1", "+", [nx[:2], one], Nc, f"Nc = nx + 1 = {nx[0]} + 1")
    c.metric("column_count", Nc, "QTY-COLUMN-v1", [nx[1]], nx[2], [s1])
    Lc = calc("*", Nc, H[0])
    s2 = c.step("column_length_m", "QTY-COLUMN-v1", "*", [(Nc, s1), H[:2]], Lc, f"Lc = Nc × H = {Nc} × {H[0]}")
    c.metric("column_length_m", Lc, "QTY-COLUMN-v1", [s1, H[1]], merge_statuses(nx[2], H[2]), [s1, s2])

    Rt = calc("+", ny[0], Decimal(1))
    s3 = c.step("beam_rows", "QTY-BEAM-v1", "+", [ny[:2], one], Rt, f"Rt = ny + 1 = {ny[0]} + 1")
    c.metric("beam_rows", Rt, "QTY-BEAM-v1", [ny[1]], ny[2], [s3])
    Nt = calc("*", nx[0], Rt)
    s4 = c.step("beam_segments", "QTY-BEAM-v1", "*", [nx[:2], (Rt, s3)], Nt, f"Nt = nx × Rt = {nx[0]} × {Rt}")
    c.metric("beam_segments", Nt, "QTY-BEAM-v1", [nx[1], s3], merge_statuses(nx[2], ny[2]), [s3, s4])
    Lt = calc("*", Rt, W[0])
    s5 = c.step("beam_length_m", "QTY-BEAM-v1", "*", [(Rt, s3), W[:2]], Lt, f"Lt = Rt × W = {Rt} × {W[0]}（= Nt × b）")
    c.metric("beam_length_m", Lt, "QTY-BEAM-v1", [s3, W[1]], merge_statuses(ny[2], W[2]), [s3, s5])
    return c.response(KEYS, payload)
