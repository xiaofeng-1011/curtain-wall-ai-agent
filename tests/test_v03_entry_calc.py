"""改版方案 §9.2：手工录入与计算（T10–T21）。参考值来自 fixtures/manual_expected.json（独立手算）。"""
import pytest

from agent import projects as P
from agent.engine import is_current
from tests.chat_helpers import EXPECTED, Chat, disp


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv("MW_FORCE_FALLBACK", "1")


def test_t10_single_scheme_values(tmp_path):
    c = Chat(tmp_path).build_t10(b="1.5")
    r = c.run()
    exp = EXPECTED["b_1.5"]
    for k, v in exp.items():
        assert disp(r, "baseline", k) == v, k
    steps = [s["expression"] for mod in ("length", "mass", "cost") for s in r["results"]["baseline"][mod]["steps"]]
    assert "Lc = Nc × H = 13 × 6" in steps and any(s.startswith("Cc = Mc × pc = 210.6 × 25") for s in steps)


def test_t11_compare_and_reverse(tmp_path):
    c = Chat(tmp_path).build_t10(b="1.2")
    assert disp(c.run(), "baseline", "total_cost_cny") == EXPECTED["b_1.2"]["total_cost_cny"]
    c.say("把分格宽改为1.5m，与原方案比较")
    r = c.run()
    cmp = r["comparison"]
    assert cmp["delta_metrics"]["delta_total_cost_cny"]["display_value"] == "-1215.00"
    assert cmp["percent_metrics"]["percent_total_cost"]["display_value"] == EXPECTED["compare_1.2_to_1.5"]["percent_total_cost"]
    assert "减少 1,215.00 元，下降 12.45%" in c.last("result")["text"]
    # 反向：以 1.5 m 为原方案，候选改回 1.2 m
    d = Chat(tmp_path).build_t10(b="1.5")
    d.say("把分格宽改为1.2m，与原方案比较")
    pct = d.run()["comparison"]["percent_metrics"]["percent_total_cost"]["display_value"]
    assert pct == EXPECTED["compare_1.5_to_1.2"]["percent_total_cost"]


def test_t12_missing_price_gives_mass(tmp_path):
    c = Chat(tmp_path).new()
    c.say("宽18m、高6m，分格宽1.2m、高1.5m，立柱线重2.7kg/m，横梁线重1.3kg/m，算材料金额")
    c.click("确认并计算")
    r = c.run()
    assert disp(r, "baseline", "total_mass_kg") == "376.200"
    assert disp(r, "baseline", "total_cost_cny") is None and r["execution_state"] == "PARTIAL"
    assert c.conv["pending"]["field"] == "column_unit_price"  # 接着只问缺的单价，不填 0


def test_t13_geometry_only(tmp_path):
    c = Chat(tmp_path).new()
    c.say("幕墙宽18米，高6米，算一下")
    c.click("确认并计算")
    r = c.run()
    assert disp(r, "baseline", "facade_area_m2") == "108.00"
    assert r["module_statuses"]["length"] == "blocked"
    assert c.conv["pending"]["field"] == "grid_width_m"


def test_t14_followup_correction_no_conflict(tmp_path):
    c = Chat(tmp_path).build_t10()
    c.say("把分格宽改为1.5m，与原方案比较")
    c.say("再改成2m")
    m = [x for x in c.conv["messages"] if x["kind"] == "change"][-1]
    assert "分格宽 1.5 → 2 m" in m["text"] and "冲突" not in m["text"]
    assert disp(c.run(), "candidate", "total_cost_cny") == EXPECTED["b_2.0"]["total_cost_cny"]
    c.click("撤销")
    assert P.user_value(c.project(), "candidate", "grid_width_m")[0] == "1.5"


def test_t15_reuse_price_only_within_project(tmp_path):
    other = Chat(tmp_path).build_t10()
    c = Chat(tmp_path).new()
    c.say("项目丙，宽18m、高6m，分格宽1.2m、高1.5m，立柱2.7kg/m，横梁1.3kg/m")
    c.say("其他不变，沿用刚才的价格")
    assert P.user_value(c.project(), "baseline", "column_unit_price")[0] is None  # 不从其他项目取值
    assert "没有可沿用" in c.last("change")["text"]
    assert other.project()["project_id"] != c.project()["project_id"]


def test_t15_reuse_price_inside_project(tmp_path):
    c = Chat(tmp_path).build_t10()
    c.say("调整方案立柱线重改为3.2kg/m")
    assert P.user_value(c.project(), "candidate", "column_unit_price")[0] is None  # 换型材不自动沿用旧价
    c.say("沿用刚才的价格")
    assert P.user_value(c.project(), "candidate", "column_unit_price")[0] == "25"
    assert P.user_value(c.project(), "baseline", "column_linear_mass")[0] == "2.7"  # 原方案未被改动


def test_t16_t17_divisibility(tmp_path):
    c = Chat(tmp_path).new()
    c.say("宽18m、高6m，分格宽2m、高1.5m，算长度")
    c.click("确认并计算")
    assert disp(c.run(), "baseline", "column_length_m") == "60.000"
    d = Chat(tmp_path).new()
    d.say("宽15m、高6m，分格宽1.2m、高1.5m，算型材用量")
    q = d.conv["pending"]
    assert q["code"] == "NON_DIVISIBLE_GRID" and "不取整" in q["text"]
    assert P.user_value(d.project(), "baseline", "width_m")[0] == "15"  # 保留真实尺寸，不强迫改数
    d.click("保留真实尺寸，先看面积")
    d.click("确认并计算")
    assert disp(d.run(), "baseline", "facade_area_m2") == "90.00"
    assert disp(d.run(), "baseline", "column_length_m") is None


@pytest.mark.parametrize("text,label", [("宽0m、高6m", "幕墙总宽"), ("宽18m、高-6m", "幕墙总高")])
def test_t18_zero_negative_geometry(tmp_path, text, label):
    c = Chat(tmp_path).new()
    c.say(text + "，分格宽1.2m、高1.5m，算一下")
    assert c.conv["pending"] is None or c.conv["pending"]["type"] == "question"
    assert c.ws.counts()["runs"] == 0  # 不形成可运行结果


def test_t18_negative_material_not_written(tmp_path):
    c = Chat(tmp_path).build_t10()
    c.say("立柱线重改为-2.7kg/m，横梁单价-28元/kg")
    m = c.last("change")
    assert "立柱线重必须大于 0" in m["text"] and "横梁单价不能为负" in m["text"]
    p = c.project()
    assert P.user_value(p, "baseline", "column_linear_mass")[0] == "2.7"
    assert P.user_value(p, "baseline", "beam_unit_price")[0] == "28"


def test_t19_ton_price_and_tax(tmp_path):
    c = Chat(tmp_path).new()
    c.say("宽18m、高6m，分格宽1.2m、高1.5m，立柱2.7kg/m 26000元/吨，横梁1.3kg/m 28元/kg，算金额")
    assert "26000 元/吨 → 26 元/kg" in [m for m in c.conv["messages"] if m["kind"] == "change"][-1]["text"]
    c.click("确认并计算")
    assert disp(c.run(), "baseline", "total_cost_cny") == EXPECTED["price_26000_per_t"]["b_1.2_total_cost_cny"]
    c.say("这些单价都是含税的")
    c.say("算金额")
    r = c.run()
    assert disp(r, "baseline", "total_cost_cny") is None and disp(r, "baseline", "total_mass_kg") == "376.200"
    assert any("含税" in i["message"] for i in r["module_issues"]["cost"])


def test_t19_price_without_unit_asks(tmp_path):
    c = Chat(tmp_path).new()
    c.say("宽18m、高6m，分格宽1.2m、高1.5m，立柱2.7kg/m，单价26元")
    assert "计价单位" in c.last("change")["text"]
    assert P.user_value(c.project(), "baseline", "column_unit_price")[0] is None


def test_t20_custom_names(tmp_path):
    c = Chat(tmp_path).new()
    c.say("宽18m、高6m，分格宽1.5m、高1.5m")
    c.say("立柱叫试用立柱甲，线重 2.7kg/m，25 元/kg；横梁叫试用横梁乙，线重 1.3kg/m，28 元/kg。都是我构造的未税演示数值。算金额")
    c.click("确认并计算")
    p = c.project()
    assert P.material_of(p, "baseline", "column")["profile_name"] == "试用立柱甲"
    assert "承载" in P.material_of(p, "baseline", "column")["applicability_notes"]
    assert disp(c.run(), "baseline", "total_cost_cny") == "8,541.00"


def test_t21_price_basis_isolation(tmp_path):
    c = Chat(tmp_path).build_t10()
    c.say("调整方案立柱单价改为30元/kg，算金额")
    p = c.project()
    assert P.basis_of(p, "baseline")["price_basis_id"] != P.basis_of(p, "candidate")["price_basis_id"]
    assert P.material_of(p, "baseline", "column")["profile_id"] == P.material_of(p, "candidate", "column")["profile_id"]
    assert P.price_of(p, "baseline", "column")["unit_price"] == "25"
    assert P.price_of(p, "candidate", "column")["unit_price"] == "30"
    r = c.run()
    assert disp(r, "candidate", "column_cost_cny") == "7,776.00"  # 259.2 × 30
    assert disp(r, "baseline", "column_cost_cny") == "6,480.00"


def test_correct_baseline_syncs_candidate(tmp_path):
    c = Chat(tmp_path).build_t10()
    c.say("把分格宽改为1.5m，与原方案比较")
    old_run = c.run()
    c.say("原方案高度填错了，应该是 9 米")
    p = c.project()
    assert P.user_value(p, "baseline", "height_m")[0] == "9"
    assert P.user_value(p, "candidate", "height_m")[0] == "9"  # 继承字段同步
    assert not is_current(p, old_run)  # 旧结果标为对应旧版本
    snap = c.ws.get_snapshot(old_run["snapshots"]["baseline"]["snapshot_id"])
    assert snap["parameters"]["height_m"]["value"] == "6"  # 旧快照不可变
    c.say("比较两个方案")
    assert disp(c.run(), "baseline", "total_cost_cny") == "14,306.40"
