"""TL12 resolve_checklist：冻结本次运行的必查清单。

清单中的每一项都必须执行并记录状态，不依据“本次修改了哪些字段”或关键词召回结果删减。
"""
from __future__ import annotations

import copy

from domain.contracts import Code, Issue, ToolResponse, now_utc, sha256_of


def resolve_checklist(package: dict, case_id: str) -> ToolResponse:
    started = now_utc()
    case = package["cases"][case_id]
    rs_id = case.get("required_rule_set_id")
    rs = package["rule_sets"].get(rs_id)
    issues: list[Issue] = []
    if rs is None:
        issues.append(Issue(Code.EVIDENCE_NOT_FOUND, f"必查清单 {rs_id} 不存在", None, "rule_check"))
        return ToolResponse("TL12", "blocked", None, issues, {"started_at": started, "ended_at": now_utc()})
    items = []
    for m in sorted(rs["members"], key=lambda x: x.get("display_order", 0)):
        rule = package["rules"].get(m["rule_id"])
        items.append({
            "item_id": m["item_id"], "rule_id": m["rule_id"], "display_order": m.get("display_order"),
            "required": True,
            # 冻结规则全文，旧报告不随后续数据包变化
            "rule": copy.deepcopy(rule) if rule else None,
        })
    data = {
        "rule_set_id": rs_id, "rule_set_version": rs.get("rule_set_version"), "name": rs.get("name"),
        "coverage_notes": rs.get("coverage_notes"), "items": items,
        "required_count": len(items),
        "formal_clause_count": sum(1 for r in package["rules"].values() if r["collection"] == "formal_clauses"),
        "rule_set_provenance": copy.deepcopy(package["provenance"].get(rs.get("provenance_id"))),
        "provenance": {pid: copy.deepcopy(package["provenance"][pid])
                       for pid in {(it["rule"] or {}).get("provenance_id") for it in items}
                       if pid in package["provenance"]},
    }
    data["checklist_hash"] = sha256_of(data)
    return ToolResponse("TL12", "ok", data, issues, {"started_at": started, "ended_at": now_utc()},
                        input_hash=sha256_of({"case": case_id, "rs": rs_id, "pkg": package.get("content_hash")}))
