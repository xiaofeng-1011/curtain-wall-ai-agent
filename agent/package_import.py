"""维护用途：把技术 JSON 数据包（v0.2 契约）中的案例转换为项目。只在用户主动选择时执行。"""
from __future__ import annotations

import copy

from data.package_loader import resolve_seed_parameters, validate_package
from validation.parameters import field_value

from .projects import new_project


def import_package(ws, source) -> dict:
    rep = validate_package(source)
    if not rep.can_activate:
        return {"ok": False, "errors": [e.to_dict() for e in rep.errors][:20]}
    pkg = rep.package
    created = []
    for cid, case in pkg["cases"].items():
        p = new_project(f"{case['case_name']['value']}（数据包导入）")
        p["provenance"] = copy.deepcopy(pkg["provenance"])
        if all(v["source_status"] == "synthetic" for v in pkg["provenance"].values()
               if v["provenance_id"] != "PROV-PENDING-001"):
            p["default_source"] = "synthetic"
        resolved = resolve_seed_parameters(pkg, case["baseline_seed_id"])
        params = {}
        for f, sv in resolved.items():
            params[f] = field_value(sv["value"], sv["unit"], provenance_ids=sv["provenance_ids"],
                                    binding_state="modified" if sv["value"] is not None else "missing",
                                    raw_value=sv["value"], raw_unit=sv["unit"])
        p["schemes"]["baseline"]["params"] = params
        for mid, m in pkg["profiles"].items():
            p["materials"][mid] = dict(copy.deepcopy(m), created_for="baseline", derived_from=None)
        for bid, b in pkg["price_bases"].items():
            p["price_bases"][bid] = dict(copy.deepcopy(b), created_for="baseline")
        for k, pr in pkg["prices"].items():
            p["prices"][k] = dict(copy.deepcopy(pr), tax_basis=pr.get("tax_basis") or "exclusive")
        rs = pkg["rule_sets"].get(case.get("required_rule_set_id"))
        for m in (rs or {}).get("members", []):
            r = pkg["rules"].get(m["rule_id"])
            if r and r.get("evidence_type") == "demo_rule":
                p["rules"][r["rule_id"]] = copy.deepcopy(r)
        ws.create_project(p)
        created.append(p["name"])
    return {"ok": True, "projects": created, "warnings": len(rep.warnings)}
