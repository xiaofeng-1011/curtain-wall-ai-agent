"""DEV04：确定性计算（T01 T02 T03 T06 T10 T11 T17 T18 T19 T20 T21 T26 T33 T37 计算部分）。

参考值来自 data/demo/expected_results.json（独立十进制手算录入），不由被测函数生成。
"""
from decimal import Decimal

import pytest

from calculation.comparison import compare_schemes
from calculation.cost import calculate_cost
from calculation.geometry import calculate_geometry
from calculation.lengths import calculate_lengths
from calculation.mass import calculate_mass
from calculation.pipeline import run_scheme
from domain.contracts import dec
from tests.helpers import EXPECTED, demo_package, mdisp, mutate, mval, snap

PKG = demo_package()


def results(s):
    return {k: v.data for k, v in run_scheme(s).items()}


def _same_display(actual: str, expected: str) -> bool:
    return Decimal(actual) == Decimal(expected) and \
        len(actual.split(".")[-1]) == len(expected.split(".")[-1]) if "." in expected else Decimal(actual) == Decimal(expected)


@pytest.mark.parametrize("sid", ["S0", "S1", "S2", "S3", "S4", "S5"])
def test_reference_schemes(sid):
    r = run_scheme(snap(PKG, sid))
    assert all(v.status == "ok" for v in r.values()), {k: v.issues for k, v in r.items()}
    allm = {}
    for resp in r.values():
        allm.update(resp.data["metrics"])
    for key, val in EXPECTED["schemes"][sid].items():
        if key == "rules":
            continue
        m = allm[key]
        # 展示值按约定位数 ROUND_HALF_UP 后与参考值精确一致
        assert _same_display(m["display_value"], val), (sid, key, m["display_value"], val)
        # 未舍入值与参考值之差不超过参考值的舍入半单位
        places = len(val.split(".")[-1]) if "." in val else 0
        assert abs(dec(m["value"]) - dec(val)) <= Decimal(5).scaleb(-(places + 1)), (sid, key)


def test_exact_values_unrounded():
    r = run_scheme(snap(PKG, "S1"))
    assert mval(r["cost"], "total_cost_cny") == "7776"
    assert mval(r["mass"], "beam_mass_per_m2").startswith("0.93333333333333")
    assert mdisp(r["mass"], "beam_mass_per_m2") == "0.9333"


def test_t01_comparison_s1_vs_s0():
    s0, s1 = snap(PKG, "S0"), snap(PKG, "S1")
    resp = compare_schemes(s0, s1, results(s0), results(s1))
    assert resp.status == "ok"
    exp = EXPECTED["comparisons"]["S1_vs_S0"]
    d = resp.data["delta_metrics"]
    for k, v in exp.items():
        if k.startswith("delta_"):
            assert d[k]["display_value"] == v, (k, d[k]["display_value"])
    assert resp.data["percent_metrics"]["percent_total_cost"]["display_value"] == exp["percent_total_cost"]
    assert [c["field"] for c in resp.data["changed_fields"]] == ["grid_width_m"]


def test_t02_comparison_s2_vs_s0_lists_profile_changes():
    s0, s2 = snap(PKG, "S0"), snap(PKG, "S2")
    resp = compare_schemes(s0, s2, results(s0), results(s2))
    exp = EXPECTED["comparisons"]["S2_vs_S0"]
    assert resp.data["delta_metrics"]["delta_total_cost_cny"]["display_value"] == exp["delta_total_cost_cny"]
    assert resp.data["percent_metrics"]["percent_total_cost"]["display_value"] == exp["percent_total_cost"]
    fields = {c["field"] for c in resp.data["changed_fields"]}
    assert {"grid_width_m", "column_profile_id", "beam_profile_id", "column.unit_price"} <= fields


def test_t03_unit_equivalence():
    a = snap(PKG, "S0", [("grid_width_m", "set", "1200", "mm")])
    b = snap(PKG, "S0", [("grid_width_m", "set", "1.2", "m")])
    c = snap(PKG, "S0", [("grid_width_m", "set", "120", "cm")])
    ra, rb, rc = run_scheme(a), run_scheme(b), run_scheme(c)
    assert mval(ra["cost"], "total_cost_cny") == mval(rb["cost"], "total_cost_cny") == mval(rc["cost"], "total_cost_cny")
    assert dec(a["parameters"]["grid_width_m"]["value"]) == Decimal("1.2")
    assert a["parameters"]["grid_width_m"]["raw_value"] == "1200"


def test_t03_missing_unit_blocks():
    s = snap(PKG, "S0", [("grid_width_m", "set", "1200", None)])
    assert s["parameters"]["grid_width_m"]["value"] is None
    assert s["parameters"]["grid_width_m"]["issues"][0]["code"] == "UNIT_REQUIRED"
    r = run_scheme(s)
    assert r["geometry"].status == "partial"  # 立面面积可算，板块面积与格数不可算
    assert mval(r["geometry"], "facade_area_m2") == "108"
    assert r["length"].status == "blocked" and r["cost"].status == "blocked"


def test_t06_non_divisible_grid_blocked_no_rounding():
    s = snap(PKG, "S0", [("grid_width_m", "set", "1.4", "m")])
    r = run_scheme(s)
    assert any(i.code == "NON_DIVISIBLE_GRID" for i in r["geometry"].issues)
    assert "nx" not in r["geometry"].data["metrics"]
    assert mval(r["geometry"], "panel_area_m2") is not None  # 板块面积不依赖整除
    assert r["length"].status == "blocked" and mval(r["length"], "column_length_m") is None


def test_t20_zero_dimensions_blocked():
    for f in ("width_m", "height_m", "grid_width_m", "grid_height_m"):
        r = run_scheme(snap(PKG, "S0", [(f, "set", "0", "m")]))
        assert any(i.code == "VALUE_INVALID" for i in r["geometry"].issues), f
        for mod in ("length", "mass", "cost"):
            assert r[mod].status == "blocked", (f, mod)


def test_t21_negative_mass_and_price():
    r = run_scheme(snap(PKG, "S0", [("column_mass_override", "set", "-2.4", "kg/m")]))
    assert r["length"].status == "ok" and r["mass"].status == "blocked"
    assert mval(r["mass"], "total_mass_kg") is None and r["cost"].status == "blocked"
    r = run_scheme(snap(PKG, "S0", [("beam_price_override", "set", "-27", "元/kg")]))
    assert r["mass"].status == "ok" and r["cost"].status == "blocked"
    assert mval(r["cost"], "total_cost_cny") is None


def test_t26_missing_beam_price_no_fake_total():
    pkg = mutate(PKG, lambda p: p["prices"]["DEMO-PRICE-001::BEAM-A"].__setitem__("unit_price", None))
    r = run_scheme(snap(pkg, "S1"))
    assert mdisp(r["mass"], "total_mass_kg") == "295.200"
    assert r["cost"].status == "blocked"
    assert mval(r["cost"], "column_cost_cny") is None and mval(r["cost"], "total_cost_cny") is None
    assert any(i.code == "PRICE_MISSING" for i in r["cost"].issues)


def test_t11_zero_price_requires_ack_and_zero_baseline():
    edits = [("column_price_override", "set", "0", "元/kg"), ("beam_price_override", "set", "0", "元/kg")]
    r = run_scheme(snap(PKG, "S0", edits))
    assert any(i.code == "ZERO_PRICE_UNCONFIRMED" for i in r["cost"].issues)
    s0 = snap(PKG, "S0", edits, zero_acks=["column", "beam"])
    r0 = run_scheme(s0)
    assert mval(r0["cost"], "total_cost_cny") == "0"
    s1 = snap(PKG, "S1")
    cmp = compare_schemes(s0, s1, {k: v.data for k, v in r0.items()}, results(s1))
    assert cmp.status == "ok"
    pct = cmp.data["percent_metrics"]["percent_total_cost"]
    assert pct["value"] is None and pct["reason_code"] == "ZERO_BASELINE"
    assert cmp.data["delta_metrics"]["delta_total_cost_cny"]["display_value"] == "7776.00"


def test_t10_incompatible_comparison():
    s0 = snap(PKG, "S0")
    s_w = snap(PKG, "S1", [("width_m", "set", "15", "m")])
    resp = compare_schemes(s0, s_w, results(s0), results(s_w))
    assert resp.status == "blocked" and resp.data["delta_metrics"] == {}
    assert any(i.code == "COMPARISON_INCOMPATIBLE" for i in resp.issues)
    s_area = snap(PKG, "S1", [("reported_area_m2", "set", "100", "m²")])
    resp = compare_schemes(s0, s_area, results(s0), results(s_area))
    assert resp.status == "blocked"


def test_t37_price_replacement():
    edits = [("column_price_override", "set", "30", "元/kg"), ("beam_price_override", "set", "31", "元/kg")]
    r = run_scheme(snap(PKG, "S1", edits))
    assert mdisp(r["cost"], "total_cost_cny") == "8956.80"
    assert EXPECTED["price_replacement_T37"]["S1_total_cost_cny_with_COL-A_30_BEAM-A_31"] == "8956.80"


def test_t33_tampered_upstream_rejected():
    s1 = snap(PKG, "S1")
    geo = calculate_geometry(s1)
    ln = calculate_lengths(s1, geo.data)
    tampered = dict(ln.data, metrics=dict(ln.data["metrics"]))
    tampered["metrics"]["column_length_m"] = dict(tampered["metrics"]["column_length_m"], value="1")
    m = calculate_mass(s1, tampered, geo.data)
    assert m.status == "blocked" and m.issues[0].code == "INPUT_HASH_MISMATCH"
    # 拼接其他快照的上游结果
    s0 = snap(PKG, "S0")
    m2 = calculate_mass(s1, calculate_lengths(s0, calculate_geometry(s0).data).data, geo.data)
    assert m2.issues[0].code == "INPUT_HASH_MISMATCH"
    c = calculate_cost(s1, None, geo.data)
    assert c.status == "blocked"


def test_traceability_column_cost_chain():
    r = run_scheme(snap(PKG, "S1"))
    steps = [s for resp in r.values() for s in resp.data["steps"]]
    cc = r["cost"].data["metrics"]["column_cost_cny"]
    assert any("resolved_prices.column.unit_price" in x for x in cc["input_refs"])
    exprs = [s["expression"] for s in steps]
    assert "Lc = Nc × H = 9 × 9" in exprs
    assert any(e.startswith("Mc = Lc × μc = 81 × 2.4") for e in exprs)
    assert any(e.startswith("Cc = Mc × pc = 194.4") for e in exprs)
    assert cc["source_statuses"] == ["synthetic"] and cc["generation_method"] == "program_derived"
    assert cc["review_status"] == "pending_professional_review"


def test_deterministic_and_no_float():
    a = run_scheme(snap(PKG, "S2"))
    b = run_scheme(snap(PKG, "S2"))
    assert a["cost"].data["result_hash"] == b["cost"].data["result_hash"]
    with pytest.raises(ValueError):
        dec(1.2)
