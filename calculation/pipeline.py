"""按依赖顺序串联单方案确定性计算：几何 → 长度 → 质量 → 成本。

缺少某一步的输入时保留前一步结果，后续步骤返回 blocked，不估算。
"""
from __future__ import annotations

from .cost import calculate_cost
from .geometry import calculate_geometry
from .lengths import calculate_lengths
from .mass import calculate_mass

ORDER = ["geometry", "length", "mass", "cost"]


def run_scheme(snapshot: dict, modules=("geometry", "length", "mass", "cost")) -> dict:
    """返回 {module: ToolResponse}；未请求的模块不出现。"""
    out = {}
    geo = calculate_geometry(snapshot)
    out["geometry"] = geo
    if "length" in modules:
        out["length"] = calculate_lengths(snapshot, geo.data)
    if "mass" in modules:
        out["mass"] = calculate_mass(snapshot, out["length"].data, geo.data)
    if "cost" in modules:
        out["cost"] = calculate_cost(snapshot, out["mass"].data, geo.data)
    return out
