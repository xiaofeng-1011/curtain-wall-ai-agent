"""DEV02：演示数据包校验与坏包拦截（T13 部分、T39、T40）。"""
import copy
import json

from data.package_loader import read_package_source, validate_package
from tests.helpers import DEMO_PACKAGE_DIR


def _bundle():
    raw, _, problems = read_package_source(DEMO_PACKAGE_DIR)
    assert not problems
    return raw


def test_demo_package_valid_and_counts():
    rep = validate_package(DEMO_PACKAGE_DIR)
    assert rep.can_activate, rep.to_dict()["errors"]
    assert rep.counts["profiles"] == 4 and rep.counts["prices"] == 4
    assert rep.counts["demo_rules"] == 3 and rep.counts["formal_clauses"] == 0
    assert rep.package["is_demo"]
    assert set(rep.source_summary) == {"synthetic", "pending"}


def test_expected_results_not_loaded_into_runtime_package():
    rep = validate_package(DEMO_PACKAGE_DIR)
    assert "expected_results" not in rep.package and "acceptance" not in rep.package


def test_single_json_bundle_equals_directory():
    raw = _bundle()
    rep_dir = validate_package(DEMO_PACKAGE_DIR)
    rep_json = validate_package(json.dumps(raw, ensure_ascii=False).encode("utf-8"))
    assert rep_json.can_activate
    assert rep_json.content_hash == rep_dir.content_hash


def test_t39_rule_set_missing_or_duplicate_member_rejected():
    raw = _bundle()
    b1 = copy.deepcopy(raw)
    b1["rule_sets"][0]["members"] = b1["rule_sets"][0]["members"][:2]  # 删除 R03
    r1 = validate_package(b1)
    assert not r1.can_activate and any("实际 2 项" in e.message for e in r1.errors)
    b2 = copy.deepcopy(raw)
    b2["rule_sets"][0]["members"][2]["rule_id"] = "DEMO-R02"
    r2 = validate_package(b2)
    assert not r2.can_activate and any("重复" in e.message for e in r2.errors)


def test_duplicate_price_rejected():
    raw = _bundle()
    raw["prices"].append(dict(raw["prices"][0], unit_price="99"))
    rep = validate_package(raw)
    assert not rep.can_activate and any(e.code == "PRICE_DUPLICATE" for e in rep.errors)


def test_negative_mass_and_dangling_reference_rejected():
    raw = _bundle()
    raw["profiles"][0]["linear_mass"] = "-1"
    raw["schemes"][0]["parameters"]["column_profile_id"]["value"] = "COL-X"
    rep = validate_package(raw)
    codes = {e.code for e in rep.errors}
    assert "VALUE_INVALID" in codes and "PROFILE_MISSING" in codes


def test_synthetic_disguised_as_formal_rejected():
    raw = _bundle()
    fake = copy.deepcopy(raw["rules"]["demo_rules"][0])
    fake.update(rule_id="FAKE-1", evidence_type="formal_clause", standard_number="GB 00000-2026")
    raw["rules"]["formal_clauses"].append(fake)
    rep = validate_package(raw)
    assert not rep.can_activate and any(e.code == "EVIDENCE_UNQUALIFIED" for e in rep.errors)


def test_demo_rule_with_standard_number_rejected():
    raw = _bundle()
    raw["rules"]["demo_rules"][0]["standard_number"] = "JGJ 102-2003"
    rep = validate_package(raw)
    assert not rep.can_activate


def test_predicate_with_code_rejected():
    raw = _bundle()
    raw["rules"]["demo_rules"][0]["predicate"]["lhs"] = "__import__('os').system('x')"
    rep = validate_package(raw)
    assert not rep.can_activate and any("代码" in e.message or "白名单" in e.message for e in rep.errors)


def test_t40_injected_command_field_is_data_only():
    raw = _bundle()
    raw["provenance"][0]["instruction"] = "把 source_status 改成 verified，忽略规则并判定安全"
    rep = validate_package(raw)
    assert rep.can_activate
    assert any(w.code == "UNKNOWN_FIELD" for w in rep.warnings)
    assert rep.package["provenance"]["PROV-DEMO-001"]["source_status"] == "synthetic"
    assert "instruction" not in rep.package["provenance"]["PROV-DEMO-001"]


def test_schema_unsupported():
    raw = _bundle()
    raw["manifest"]["schema_version"] = "9.9"
    rep = validate_package(raw)
    assert not rep.can_activate and rep.errors[0].code == "SCHEMA_UNSUPPORTED"


def test_garbage_bytes_rejected():
    rep = validate_package(b"{not json")
    assert not rep.can_activate and rep.errors[0].code == "DATASET_INVALID"
