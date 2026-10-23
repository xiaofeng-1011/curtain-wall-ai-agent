"""TL11 compare_schemes：差值 = 候选 − 基准；变化率 = 差值 / 基准 × 100%（DELTA-v1）。

先做同口径检查：W/H/A、计量模型、货币、税口径、费用范围必须一致，否则不返回降本指标。
规格或价格变化允许比较，但只列出变更因素，P0 不做多因素贡献分解。
"""
from __future__ import annotations

from decimal import Decimal

from domain.contracts import (
    TOOL_VERSIONS, Code, Issue, Metric, Step, ToolResponse, calc, dec, display, dstr, merge_statuses, now_utc,
    sha256_of,
)
from domain.fields import FIELD_DEFS

from .common import check_upstream, metric_kind, metric_unit

DELTA_KEYS = [
    "nx", "ny", "column_count", "beam_rows", "beam_segments",
    "column_length_m", "beam_length_m",
    "column_mass_kg", "beam_mass_kg", "total_mass_kg",
    "column_mass_per_m2", "beam_mass_per_m2", "total_mass_per_m2",
    "column_cost_cny", "beam_cost_cny", "total_cost_cny", "cost_per_m2",
]
PERCENT_KEYS = {"percent_total_cost": "total_cost_cny", "percent_total_mass": "total_mass_kg"}


def _all_metrics(results: dict) -> dict:
    out = {}
    for mod in ("geometry", "length", "mass", "cost"):
        r = results.get(mod)
        if r:
            for k, m in (r.get("metrics") or {}).items():
                out[k] = (m, r)
    return out


def _param(snapshot: dict, field: str):
    return ((snapshot.get("parameters") or {}).get(field) or {}).get("value")


def comparability(base_snap: dict, cand_snap: dict, base_res: dict, cand_res: dict) -> list[Issue]:
    issues: list[Issue] = []
    mod = "comparison"
    for f in ("width_m", "height_m"):
        a, b = _param(base_snap, f), _param(cand_snap, f)
        if a is None or b is None or dec(a) != dec(b):
            issues.append(Issue(Code.COMPARISON_INCOMPATIBLE, f"{FIELD_DEFS[f]['label']}不同（{a} vs {b}），不是同一立面",
                                f, mod))
    ga = ((base_res.get("geometry") or {}).get("metrics") or {}).get("facade_area_m2", {}).get("value")
    gb = ((cand_res.get("geometry") or {}).get("metrics") or {}).get("facade_area_m2", {}).get("value")
    if ga is None or gb is None or dec(ga) != dec(gb):
        issues.append(Issue(Code.COMPARISON_INCOMPATIBLE, f"立面面积不同（{ga} vs {gb}）", "facade_area_m2", mod))
    for snap, res in ((base_snap, base_res), (cand_snap, cand_res)):
        if any(i.get("code") == Code.AREA_CONFLICT for i in (res.get("geometry") or {}).get("issues", [])):
            issues.append(Issue(Code.COMPARISON_INCOMPATIBLE, f"{snap.get('scheme_id')} 申报面积与 W×H 冲突未解决",
                                "reported_area_m2", mod))
    ma, mb = _param(base_snap, "measurement_model_id"), _param(cand_snap, "measurement_model_id")
    if ma != mb:
        issues.append(Issue(Code.COMPARISON_INCOMPATIBLE, f"计量模型不同（{ma} vs {mb}）", "measurement_model_id", mod))
    for comp in ("column", "beam"):
        pa = (base_snap.get("resolved_prices") or {}).get(comp) or {}
        pb = (cand_snap.get("resolved_prices") or {}).get(comp) or {}
        for k, label in (("currency", "货币"), ("tax_basis", "税口径"), ("cost_scope", "费用范围"),
                         ("price_unit", "价格单位")):
            if pa.get(k) != pb.get(k):
                issues.append(Issue(Code.COMPARISON_INCOMPATIBLE,
                                    f"{label}不一致（{pa.get(k)} vs {pb.get(k)}）", "price_basis_id", mod))
    # 去重
    uniq, seen = [], set()
    for i in issues:
        if i.message not in seen:
            seen.add(i.message)
            uniq.append(i)
    return uniq


def changed_fields(base_snap: dict, cand_snap: dict) -> list[dict]:
    out = []
    for f, fdef in FIELD_DEFS.items():
        a, b = _param(base_snap, f), _param(cand_snap, f)
        if a != b and not (a is not None and b is not None and fdef["kind"] in ("length", "price", "linear_mass")
                           and dec(a) == dec(b)):
            out.append({"field": f, "label": fdef["label"], "baseline": a, "candidate": b})
    for comp, label in (("column", "立柱"), ("beam", "横梁")):
        for k, what in (("linear_mass", "线重"),):
            a = ((base_snap.get("resolved_profiles") or {}).get(comp) or {}).get(k)
            b = ((cand_snap.get("resolved_profiles") or {}).get(comp) or {}).get(k)
            if a != b:
                out.append({"field": f"{comp}.{k}", "label": f"{label}{what}", "baseline": a, "candidate": b})
        a = ((base_snap.get("resolved_prices") or {}).get(comp) or {}).get("unit_price")
        b = ((cand_snap.get("resolved_prices") or {}).get(comp) or {}).get("unit_price")
        if a != b:
            out.append({"field": f"{comp}.unit_price", "label": f"{label}单价", "baseline": a, "candidate": b})
    return out


def compare_schemes(base_snap: dict, cand_snap: dict, base_res: dict, cand_res: dict) -> ToolResponse:
    tool = "TL11"
    version = TOOL_VERSIONS[tool]
    started = now_utc()
    issues: list[Issue] = []
    payload = {"base": base_snap.get("content_hash"), "cand": cand_snap.get("content_hash"),
               "base_results": {k: (v or {}).get("result_hash") for k, v in base_res.items()},
               "cand_results": {k: (v or {}).get("result_hash") for k, v in cand_res.items()}}
    input_hash = sha256_of({"tool": tool, "version": version, **payload})

    for snap, res in ((base_snap, base_res), (cand_snap, cand_res)):
        for mod, tl in (("geometry", "TL07"), ("length", "TL08"), ("mass", "TL09"), ("cost", "TL10")):
            bad = check_upstream(snap, res.get(mod), tl, "comparison")
            if bad:
                bad.message = f"{snap.get('scheme_id')}：{bad.message}"
                issues.append(bad)
    if not issues:
        for snap, res in ((base_snap, base_res), (cand_snap, cand_res)):
            tc = ((res.get("cost") or {}).get("metrics") or {}).get("total_cost_cny") or {}
            if tc.get("value") is None:
                issues.append(Issue(Code.UPSTREAM_NOT_DONE, f"{snap.get('scheme_id')} 成本未完成，不能比较",
                                    None, "comparison"))
    if issues:
        return ToolResponse(tool, "blocked", None, issues, {"started_at": started, "ended_at": now_utc()},
                            input_hash=input_hash)
    incompat = comparability(base_snap, cand_snap, base_res, cand_res)
    changes = changed_fields(base_snap, cand_snap)
    if incompat:
        data = {"comparable": False, "changed_fields": changes, "delta_metrics": {}, "percent_metrics": {},
                "steps": [], "comparison_scope": None}
        data["result_hash"] = sha256_of(data)
        return ToolResponse(tool, "blocked", data, incompat, {"started_at": started, "ended_at": now_utc()},
                            input_hash=input_hash)

    bm, cm = _all_metrics(base_res), _all_metrics(cand_res)
    steps: list[Step] = []
    deltas: dict = {}
    prefix = f"{base_snap.get('snapshot_id')}->{cand_snap.get('snapshot_id')}"
    for key in DELTA_KEYS:
        if key not in bm or key not in cm or bm[key][0].get("value") is None or cm[key][0].get("value") is None:
            continue
        a, b = dec(bm[key][0]["value"]), dec(cm[key][0]["value"])
        d = calc("-", b, a)
        dk = f"delta_{key}"
        sid = f"{prefix}:TL11:{dk}"
        refs = [f"result:{bm[key][1].get('result_hash', '')[:12]}#{key}",
                f"result:{cm[key][1].get('result_hash', '')[:12]}#{key}"]
        steps.append(Step(sid, "DELTA-v1", "-", refs, f"Δ = {b} − {a}", dstr(d), metric_unit(key),
                          display(d, metric_kind(key))))
        deltas[dk] = Metric(dk, dstr(d), metric_unit(key), display(d, metric_kind(key)), refs,
                            merge_statuses(bm[key][0]["source_statuses"], cm[key][0]["source_statuses"]),
                            "DELTA-v1", version, [sid]).to_dict()
    percents: dict = {}
    for pk, base_key in PERCENT_KEYS.items():
        dk = f"delta_{base_key}"
        if dk not in deltas:
            continue
        a = dec(bm[base_key][0]["value"])
        sid = f"{prefix}:TL11:{pk}"
        if a == 0:
            percents[pk] = Metric(pk, None, "%", None, [dk], deltas[dk]["source_statuses"], "DELTA-v1", version,
                                  [], reason_code=Code.ZERO_BASELINE).to_dict()
            continue
        r = calc("*", calc("/", dec(deltas[dk]["value"]), a), Decimal(100))
        steps.append(Step(sid, "DELTA-v1", "/", [deltas[dk]["step_refs"][0]],
                          f"r = {deltas[dk]['value']} / {a} × 100%", dstr(r), "%", display(r, "percent")))
        percents[pk] = Metric(pk, dstr(r), "%", display(r, "percent"), [deltas[dk]["step_refs"][0]],
                              deltas[dk]["source_statuses"], "DELTA-v1", version, [sid]).to_dict()
    data = {
        "comparable": True,
        "baseline_snapshot_id": base_snap.get("snapshot_id"), "candidate_snapshot_id": cand_snap.get("snapshot_id"),
        "delta_metrics": deltas, "percent_metrics": percents,
        "changed_fields": changes,
        "comparison_scope": "同一立面、同一计量模型（rect_uniform_axis_v1）、CNY 未税、仅铝型材材料；"
                            "差值=候选−基准，负号表示该指标减少，不等于工程优化",
        "steps": [s.to_dict() for s in steps],
    }
    data["result_hash"] = sha256_of(data)
    return ToolResponse(tool, "ok", data, [], {"started_at": started, "ended_at": now_utc(),
                                               "step_refs": [s.step_id for s in steps]}, input_hash=input_hash)
