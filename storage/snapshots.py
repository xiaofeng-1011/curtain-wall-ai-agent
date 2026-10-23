"""不可变方案快照的构建（纯函数）。持久化由 repository 负责。

快照冻结：参数、解析后的线重与两条报价、所用来源记录、确认凭据中的零价确认。
content_hash 只覆盖业务内容，不含时间与版本号，便于判断同内容复用。
"""
from __future__ import annotations

import copy

from domain.contracts import now_utc, sha256_of
from validation.parameters import resolve_materials


def dataset_version_id(package: dict) -> str:
    return f"{package['dataset_id']}@{package['dataset_version']}"


def build_snapshot(package: dict, case_id: str, scheme_id: str, params: dict, *, label: str = "",
                   change_reason: str = "", parent_snapshot_id: str | None = None,
                   draft_id: str | None = None, draft_revision: int | None = None,
                   zero_price_acknowledgements: list[str] | None = None,
                   accepted_assumptions: list[str] | None = None,
                   extra_provenance: dict | None = None) -> dict:
    params = copy.deepcopy(params)
    for fv in params.values():
        fv["confirmation_status"] = "confirmed"  # 仅表示“本次使用确认”，不是专业审核
    profiles, prices, _ = resolve_materials(package, params)
    case = package["cases"][case_id]
    used_prov = set()
    for fv in params.values():
        used_prov.update(fv.get("provenance_ids") or [])
    for d in list(profiles.values()) + list(prices.values()):
        used_prov.update(d.get("provenance_ids") or [])
    for k in ("case_name", "curtain_wall_type", "design_stage"):
        used_prov.update((case.get(k) or {}).get("provenance_ids") or [])
    all_prov = {**package["provenance"], **(extra_provenance or {})}
    provenance = {p: all_prov[p] for p in sorted(used_prov) if p in all_prov}
    body = {
        "scheme_id": scheme_id,
        "dataset_version_id": dataset_version_id(package),
        "dataset_content_hash": package.get("content_hash"),
        "case_id": case_id,
        "curtain_wall_type": (case.get("curtain_wall_type") or {}).get("value"),
        "design_stage": (case.get("design_stage") or {}).get("value"),
        "parameters": params,
        "resolved_profiles": profiles,
        "resolved_prices": prices,
        "provenance": provenance,
        "confirmation_record": {
            "zero_price_acknowledgements": sorted(zero_price_acknowledgements or []),
            "accepted_assumptions": sorted(accepted_assumptions or []),
            "meaning": "本次使用确认，不等于专业复核或工程签审",
        },
    }
    content_hash = sha256_of(body)
    return {
        **body,
        "snapshot_id": f"{scheme_id}-{content_hash[:10]}",
        "label": label or scheme_id,
        "change_reason": change_reason,
        "parent_snapshot_id": parent_snapshot_id,
        "draft_id": draft_id, "draft_revision": draft_revision,
        "content_hash": content_hash,
        "created_at": now_utc(),
        "version": None,  # 由仓库在持久化时分配
    }


def verify_snapshot(snapshot: dict) -> bool:
    body_keys = ("scheme_id", "dataset_version_id", "dataset_content_hash", "case_id", "curtain_wall_type",
                 "design_stage", "parameters", "resolved_profiles", "resolved_prices", "provenance",
                 "confirmation_record")
    return sha256_of({k: snapshot.get(k) for k in body_keys}) == snapshot.get("content_hash")
