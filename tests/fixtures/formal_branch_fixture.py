"""隔离的正式条款分支测试替身（D07）。

仅存在于 tests 目录；不含任何真实规范原文、标准编号或条号，不进入业务知识库，不增加正式覆盖数。
"""
import copy

FIXTURE_PROV = {
    "provenance_id": "TEST-PROV-FORMAL", "source_status": "verified", "source_type": "enterprise_record",
    "review_status": "technically_reviewed", "generation_method": "provided",
    "source_ref": "测试夹具（非真实资料）", "reviewer": "测试", "reviewed_at": "2026-01-01T00:00:00+00:00",
    "review_scope": "仅规则引擎单元测试",
}

BASE_RULE = {
    "rule_id": "TEST-FORMAL-01", "rule_version": "1", "evidence_type": "enterprise_rule",
    "title": "测试夹具：可机判条件", "exact_text": "测试夹具文本（非真实规范原文）",
    "source_ref": "测试夹具", "location": "tests/fixtures", "standard_name": None, "standard_number": None,
    "edition": None, "clause_number": None, "effect_status": "current", "replaced_by": [],
    "applicability": {"curtain_wall_type": ["component_glass"], "design_stage": ["scheme_design"]},
    "required_parameters": ["grid_width_m"], "machine_checkable": True,
    "predicate": {"lhs": "grid_width_m", "operator": "<=", "threshold": {"value": "1.00", "unit": "m"}},
    "required_tools": [], "provenance_id": "TEST-PROV-FORMAL", "collection": "formal_clauses",
    "effect_review": {"review_status": "technically_reviewed"},
    "mapping_review": {"review_status": "technically_reviewed"},
}


def checklist_with(rule_overrides: dict | None = None, prov_overrides: dict | None = None) -> dict:
    rule = copy.deepcopy(BASE_RULE)
    rule.update(rule_overrides or {})
    prov = dict(FIXTURE_PROV, **(prov_overrides or {}))
    return {"rule_set_id": "TEST-RS", "rule_set_version": "1", "required_count": 1, "formal_clause_count": 0,
            "items": [{"item_id": "T-1", "rule_id": rule["rule_id"], "rule": rule}],
            "provenance": {prov["provenance_id"]: prov}, "checklist_hash": "test"}
