"""v0.2 → v0.3 一次性迁移选择（不自动删除任何旧数据）。

旧版数据位于 data/runtime/app.db。迁移前先复制一份备份；用户选择：
- keep：把旧运行中的方案转换为项目，继续使用；
- trash：同样转换，但直接放入回收站（从空白开始，需要时可恢复）；
- ignore：不转换，旧文件原样保留。
无法识别的记录不转换，保留在旧文件中。
"""
from __future__ import annotations

import copy
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from domain.contracts import new_id, now_utc
from domain.fields import FIELD_DEFS

from .projects import new_project, new_scheme

OLD_DB = Path(__file__).resolve().parents[1] / "data" / "runtime" / "app.db"


def pending(ws) -> dict | None:
    if ws.get_setting("migration_v03") or not OLD_DB.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{OLD_DB}?mode=ro", uri=True)
        runs = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        drafts = con.execute("SELECT COUNT(*) FROM drafts").fetchone()[0]
        con.close()
    except sqlite3.Error:
        return {"runs": 0, "drafts": 0, "readable": False, "path": str(OLD_DB)}
    if runs == 0 and drafts == 0:
        return None
    return {"runs": runs, "drafts": drafts, "readable": True, "path": str(OLD_DB)}


def _project_from_run(run: dict) -> dict | None:
    snaps = run.get("snapshots") or {}
    base = snaps.get("baseline") or snaps.get("candidate")
    if not base:
        return None
    label = (snaps.get("candidate") or base).get("label") or run.get("case_id") or "旧方案"
    p = new_project(f"旧版：{label}")
    p["measurement_confirmed"] = True
    p["provenance"] = copy.deepcopy(base.get("provenance") or {})
    for s in snaps.values():
        p["provenance"].update(copy.deepcopy(s.get("provenance") or {}))

    def import_materials(snap, role):
        out = {}
        for comp in ("column", "beam"):
            prof = (snap.get("resolved_profiles") or {}).get(comp)
            if prof:
                mid = f"MAT-{role[:1].upper()}{comp[:1].upper()}"
                p["materials"][mid] = {"profile_id": mid, "component_type": comp,
                                       "profile_name": prof.get("profile_name") or comp,
                                       "linear_mass": prof.get("linear_mass"), "unit": "kg/m",
                                       "provenance_id": (prof.get("provenance_ids") or [None])[0],
                                       "created_for": role, "derived_from": None,
                                       "applicability_notes": "由旧版数据迁移；工程适用性未确认"}
                out[comp] = mid
        pb = f"PB-{role[:1].upper()}"
        any_price = False
        for comp, mid in out.items():
            pr = (snap.get("resolved_prices") or {}).get(comp)
            if pr and pr.get("unit_price") is not None:
                any_price = True
                p["prices"][f"{pb}::{mid}"] = {"price_basis_id": pb, "profile_id": mid,
                                               "unit_price": pr["unit_price"], "currency": "CNY", "unit": "CNY/kg",
                                               "tax_basis": pr.get("tax_basis") or "exclusive",
                                               "price_basis_version": "1",
                                               "provenance_id": (pr.get("provenance_ids") or [None])[0]}
        if any_price:
            p["price_bases"][pb] = {"price_basis_id": pb, "price_basis_version": "1", "name": "旧版报价",
                                    "currency": "CNY", "price_unit": "CNY/kg", "tax_basis": "exclusive",
                                    "cost_scope": "aluminum_material_only", "created_for": role,
                                    "provenance_id": "PROV-SYSTEM"}
        return out, (pb if any_price else None)

    def params_of(snap, role):
        mats, pb = import_materials(snap, role)
        params = {}
        for f in FIELD_DEFS:
            fv = copy.deepcopy((snap.get("parameters") or {}).get(f) or {})
            fv.setdefault("value", None)
            params[f] = fv
        for comp, mid in mats.items():
            params[f"{comp}_profile_id"] = dict(params[f"{comp}_profile_id"], value=mid)
        for comp in ("column", "beam"):
            for k in (f"{comp}_mass_override", f"{comp}_price_override"):
                params[k] = dict(params[k], value=None)
        params["price_basis_id"] = dict(params["price_basis_id"], value=pb)
        return params

    p["schemes"]["baseline"]["params"] = params_of(base, "baseline")
    cand = snaps.get("candidate") if snaps.get("baseline") else None
    if cand:
        p["schemes"]["candidate"] = new_scheme("candidate")
        cparams = params_of(cand, "candidate")
        bparams = p["schemes"]["baseline"]["params"]
        for f, fv in cparams.items():
            if fv.get("value") != bparams[f].get("value"):
                p["schemes"]["candidate"]["overrides"][f] = dict(fv, binding_state="modified")
    return p


def migrate(ws, choice: str) -> dict:
    if choice == "ignore":
        ws.set_setting("migration_v03", {"choice": "ignore", "at": now_utc()})
        return {"converted": 0}
    backup = OLD_DB.with_name(f"app.db.bak-{datetime.now().strftime('%Y%m%d%H%M%S')}")
    shutil.copy2(OLD_DB, backup)
    con = sqlite3.connect(f"file:{OLD_DB}?mode=ro", uri=True)
    runs = [json.loads(r[0]) for r in con.execute("SELECT json FROM runs ORDER BY created_at")]
    con.close()
    latest_by_draft = {}
    for r in runs:
        if r.get("requested_task") == "query" or not r.get("snapshots"):
            continue
        latest_by_draft[r.get("draft_id") or r["run_id"]] = r
    converted, skipped = 0, 0
    for r in latest_by_draft.values():
        proj = _project_from_run(r)
        if proj is None:
            skipped += 1
            continue
        ws.create_project(proj)
        conv = {"conversation_id": new_id("CONV"), "project_id": proj["project_id"], "title": proj["name"],
                "messages": [{"id": "m1", "role": "assistant", "kind": "info", "at": now_utc(),
                              "text": f"该项目由旧版数据迁移而来（原运行 {r['run_id']}）。旧结果保留在旧版文件中，"
                                      "需要时请在这里重新计算。"}],
                "pending": None, "task": None, "skipped": [], "last_field": None, "last_run_id": None}
        ws.create_conversation(conv)
        if choice == "trash":
            ws.trash_project(proj["project_id"])
        converted += 1
    ws.set_setting("migration_v03", {"choice": choice, "at": now_utc(), "converted": converted, "skipped": skipped,
                                     "backup": str(backup)})
    return {"converted": converted, "skipped": skipped, "backup": str(backup)}
