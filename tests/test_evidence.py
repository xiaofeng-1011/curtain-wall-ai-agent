"""DEV06：必查清单、规则状态、证据资格与检索（T07 T08 T18 T19 T22 T23 T29 T30 T24 部分）。"""
import copy

import pytest

from calculation.geometry import calculate_geometry
from evidence.checklist import resolve_checklist
from evidence.evaluation import evaluate_rules
from evidence.retrieval import retrieve_evidence, tokenize
from tests.fixtures.formal_branch_fixture import checklist_with
from tests.helpers import EXPECTED, demo_package, snap

PKG = demo_package()
CL = resolve_checklist(PKG, "DEMO-CW-001").data


def run_rules(s, checklist=CL):
    geo = calculate_geometry(s).data
    return evaluate_rules(checklist, s, geo)


def statuses(resp):
    return {r["rule_id"]: r["status"] for r in resp.data["rule_results"]}


@pytest.mark.parametrize("sid", ["S0", "S1", "S2", "S3", "S4", "S5"])
def test_t07_t18_t19_demo_rule_statuses(sid):
    resp = run_rules(snap(PKG, sid))
    assert statuses(resp) == EXPECTED["schemes"][sid]["rules"]
    cov = resp.data["coverage"]
    assert cov["required_count"] == cov["result_count"] == 3
    assert cov["formal_clause_count"] == 0 and cov["formal_assessment"] == "not_covered"
    assert resp.status == "ok"  # 关注项触发是正常业务输出，不是工具错误


def test_demo_attention_text_not_violation():
    r = {x["rule_id"]: x for x in run_rules(snap(PKG, "S1")).data["rule_results"]}["DEMO-R02"]
    assert r["status_text"] == "演示关注项触发"
    assert "不是规范违反" in r["reason"]
    assert r["actual_values"]["panel_area_m2"]["value"] == "2.25"


def test_t23_all_items_executed_with_evidence_refs():
    resp = run_rules(snap(PKG, "S1"))
    rr = resp.data["rule_results"]
    assert [r["rule_id"] for r in rr] == ["DEMO-R01", "DEMO-R02", "DEMO-R03"]
    assert all(r["evidence_ref"] and r["input_refs"] for r in rr)
    # 关键词召回不改变清单
    q = retrieve_evidence(PKG, {"query_text": "分格宽度", "collection": "demo"},
                          pinned_rule_ids=[i["rule_id"] for i in CL["items"]], top_k=1)
    assert {p["evidence_id"] for p in q.data["pinned"]} == {"DEMO-R01", "DEMO-R02", "DEMO-R03"}


def test_t30_missing_h_only_affects_r02_and_r01_continues():
    s = snap(PKG, "S1", [("grid_height_m", "clear")])
    st = statuses(run_rules(s))
    assert st == {"DEMO-R01": "DEMO_MET", "DEMO-R02": "NEED_PARAMETERS", "DEMO-R03": "DEMO_MET"}


def test_t20_zero_input_gives_need_parameters():
    s = snap(PKG, "S0", [("grid_width_m", "set", "0", "m")])
    st = statuses(run_rules(s))
    assert st["DEMO-R01"] == "NEED_PARAMETERS" and st["DEMO-R02"] == "NEED_PARAMETERS"
    assert st["DEMO-R03"] == "DEMO_MET"


def test_t29_one_item_unverified_others_continue():
    cl = copy.deepcopy(CL)
    cl["provenance"]["PROV-DEMO-RULE-001"] = dict(cl["provenance"]["PROV-DEMO-RULE-001"])
    # 只让 R02 指向待核实来源
    cl["items"][1]["rule"]["provenance_id"] = "PROV-X"
    cl["provenance"]["PROV-X"] = {"source_status": "pending"}
    resp = run_rules(snap(PKG, "S0"), cl)
    st = statuses(resp)
    assert st == {"DEMO-R01": "DEMO_MET", "DEMO-R02": "NEED_VERIFICATION", "DEMO-R03": "DEMO_MET"}
    assert resp.status == "partial" and resp.data["coverage"]["result_count"] == 3


def test_missing_rule_in_checklist_not_covered():
    cl = copy.deepcopy(CL)
    cl["items"][2]["rule"] = None
    st = statuses(run_rules(snap(PKG, "S0"), cl))
    assert st["DEMO-R03"] == "NOT_COVERED"


def test_execution_error_is_not_business_status():
    cl = copy.deepcopy(CL)
    cl["items"][0]["rule"]["predicate"]["threshold"]["value"] = "abc"
    rr = run_rules(snap(PKG, "S0"), cl).data["rule_results"]
    assert rr[0]["execution_status"] == "error" and rr[0]["status"] is None
    assert rr[1]["status"] == "DEMO_MET"


# ---------------- 隔离的正式分支（T22、T08）
def test_t22_formal_condition_not_met():
    resp = run_rules(snap(PKG, "S1"), checklist_with())
    r = resp.data["rule_results"][0]
    assert r["status"] == "CONDITION_NOT_MET" and r["status_text"] == "未满足该条款可机判条件"
    assert "不推导整个工程不合规" in r["reason"]


def test_formal_condition_met():
    r = run_rules(snap(PKG, "S0"), checklist_with({"predicate": {"lhs": "grid_width_m", "operator": "<=",
                                                                  "threshold": {"value": "1.5", "unit": "m"}}}))
    assert r.data["rule_results"][0]["status"] == "CONDITION_MET"


@pytest.mark.parametrize("effect,expected", [("unknown", "NEED_VERIFICATION"), ("repealed", "NEED_VERIFICATION"),
                                             ("replaced", "NEED_VERIFICATION")])
def test_t08_unknown_or_repealed_not_current_basis(effect, expected):
    r = run_rules(snap(PKG, "S1"), checklist_with({"effect_status": effect, "replaced_by": ["(未入库)"]}))
    rr = r.data["rule_results"][0]
    assert rr["status"] == expected
    if effect == "replaced":
        assert "替代" in rr["reason"]


def test_formal_unreviewed_mapping_or_pending_source():
    r = run_rules(snap(PKG, "S1"), checklist_with({"mapping_review": {"review_status": "unreviewed"}}))
    assert r.data["rule_results"][0]["status"] == "NEED_VERIFICATION"
    r = run_rules(snap(PKG, "S1"), checklist_with(prov_overrides={"source_status": "pending"}))
    assert r.data["rule_results"][0]["status"] == "NEED_VERIFICATION"


def test_formal_not_machine_checkable_goes_to_engineer():
    r = run_rules(snap(PKG, "S1"), checklist_with({"machine_checkable": False}))
    assert r.data["rule_results"][0]["status"] == "NEED_ENGINEER"


def test_formal_not_applicable_and_region_required():
    r = run_rules(snap(PKG, "S1"), checklist_with({"applicability": {"curtain_wall_type": ["unitized"]}}))
    assert r.data["rule_results"][0]["status"] == "NOT_APPLICABLE"
    r = run_rules(snap(PKG, "S1"), checklist_with({"applicability": {"region": ["浙江"]}}))
    assert r.data["rule_results"][0]["status"] == "NEED_PARAMETERS"


def test_fixture_does_not_enter_business_library():
    assert all(r["collection"] != "formal_clauses" for r in PKG["rules"].values())
    assert "TEST-FORMAL-01" not in PKG["rules"]


# ---------------- 检索
def test_t24_query_formal_empty_returns_gap():
    q = retrieve_evidence(PKG, {"query_text": "玻璃幕墙立柱", "collection": "formal"})
    assert q.status == "ok" and q.data["candidates"] == []
    assert any("正式条款覆盖 0" in g for g in q.data["coverage_gaps"])
    assert q.data["statement"] == "检索结果是候选资料，尚未对方案判定。"


def test_query_demo_rules():
    q = retrieve_evidence(PKG, {"query_text": "板块面积", "collection": "demo"})
    assert q.data["candidates"][0]["evidence_id"] == "DEMO-R02"
    assert q.data["candidates"][0]["source_status"] == "synthetic"


def test_reference_collection_marked_unreviewed():
    idx = {"documents": [{"doc_id": "D1", "title": "测试资料", "file": "x.pdf", "needs_ocr": False,
                          "standard_number_from_filename": None, "sha256": "h"}],
           "pages": [{"doc_id": "D1", "page": 3, "text": "幕墙立柱应采用铝合金型材，横梁与立柱连接……" * 3}]}
    q = retrieve_evidence(PKG, {"query_text": "立柱 横梁", "collection": "reference"}, reference_index=idx)
    c = q.data["candidates"][0]
    assert c["collection"] == "reference" and c["usable_for_determination"] is False
    assert c["source_status"] == "pending" and c["standard_number"] is None
    assert q.data["formal_clause_count"] == 0


def test_tokenize():
    assert "分格" in tokenize("分格宽度是多少") and "的" not in "".join(tokenize("的"))
