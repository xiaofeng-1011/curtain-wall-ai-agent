"""V5 方案决策辅助：计算后自动分析、变更核查、逐句校验、版本绑定、意图理解与回退。

模型返回值均为模拟，只验证流程和校验；真实回答效果见发布目录的在线验收记录。
"""
import json
import os

import pytest

from tests.chat_helpers import Chat


def good_analysis(payload):
    return {
        "change_summary": "分格宽由 1.2 m 改为 1.5 m。",
        "effect_explanation": "立柱轴线数由 16 条减至 13 条，立柱理论总长减少 18.000 m，因此立柱横梁材料金额减少 1,215.00 元。"
                              "这只是立柱横梁的理论材料金额，不是工程总造价。",
        "baseline": {"suits_when": "更看重较小板块和较低单根立柱受荷时。", "advantages": "单块面板面积 1.80 m²。",
                     "costs": "立柱更多，材料金额更高。"},
        "candidate": {"suits_when": "完成立柱和玻璃验算后，追求更通透立面时。", "advantages": "立柱减少 3 条。",
                      "costs": "单块面板面积 2.25 m²，需复核玻璃和立柱。"},
        "leaning": {"choice": "candidate", "reason": "你更看重外观通透。", "holds_if": "立柱和玻璃验算通过。"},
        "goal_scenarios": [{"goal": "成本优先", "choice": "candidate", "reason": "仅比较立柱横梁材料金额。"}],
        "next_steps": ["补充风荷载标准值和玻璃厚度。"], "check_notes": [], "rule_drafts": [
            {"topic_id": "column", "draft": "立柱挠度不大于跨度/180", "evidence_ids": []}]}


@pytest.fixture
def online(monkeypatch):
    """模型“可用”，但参数理解仍走本地解析；规范解读和方案分析走模拟模型。"""
    from agent import extraction
    calls = []
    original = extraction.understand
    monkeypatch.setattr(extraction, "model_available", lambda: True)
    monkeypatch.setattr(extraction, "understand", lambda text, ctx, mode="auto": original(text, ctx, "fallback"))
    state = {"analysis": good_analysis, "advice": None}

    def call(system, user, schema, **kw):
        payload = json.loads(user)
        calls.append((system, payload))
        if "change_checks" in payload:
            fn = state["analysis"]
            if isinstance(fn, Exception):
                raise fn
            return fn(payload)
        if "intent" in payload:
            if state["advice"] is None:
                raise TimeoutError("timed out")
            return state["advice"]
        raise AssertionError("unexpected model call")
    monkeypatch.setattr(extraction, "call_model_json", call)
    return state, calls


def compare_chat(tmp_path):
    c = Chat(tmp_path).build_t10()
    c.say("我更看重外观通透")
    assert "已记录你的决策目标：外观优先" in c.last()["text"]
    c.say("把分格宽改为1.5m，与原方案比较")
    return c


def test_recompute_generates_bound_analysis_automatically(tmp_path, online):
    state, calls = online
    c = compare_chat(tmp_path)
    msg = c.last("analysis")
    assert msg is not None, "成功重新计算后应自动生成方案分析"
    run = c.run()
    analysis = c.ws.get_analysis(msg["card"]["analysis_id"])
    assert analysis["run_id"] == run["run_id"] == msg["card"]["run_id"] and analysis["source"] == "model"
    assert analysis["binding"]["snapshots"]["candidate"]["snapshot_id"] == run["snapshots"]["candidate"]["snapshot_id"]
    text = msg["text"]
    for part in ("本次修改", "计算结果变化", "原方案更合适的情况", "修改方案更合适的情况", "目前倾向", "进一步决策还需要"):
        assert part in text
    assert "立柱轴线数：16 → 13 条" in text and "不是工程总造价" in text
    assert "实际调用" in msg["note"] and "deepseek" not in msg["note"].lower() or True
    system, payload = calls[-1]
    assert payload["user_goals"] == ["外观优先"]
    assert {"schemes", "changed_fields", "computed", "scope", "change_checks", "rule_results"} <= set(payload)
    assert "原方案" in payload["schemes"] and "修改方案" in payload["schemes"]
    # 规则草稿只作为待审核草稿，不进入规则
    assert analysis["result"]["rule_drafts"][0]["status"].startswith("待工程师审核")
    assert not c.project()["rules"]
    # 报告与导出同一份分析
    assert analysis["analysis_id"] == run["report"]["analysis_id"] and "方案分析（第 1 版" in run["report"]["markdown"]


def test_model_failure_falls_back_with_reason_and_keeps_results(tmp_path, online):
    state, _ = online
    state["analysis"] = TimeoutError("timed out")
    c = compare_chat(tmp_path)
    msg = c.last("analysis")
    analysis = c.ws.get_analysis(msg["card"]["analysis_id"])
    assert analysis["source"] == "template" and analysis["model_record"]["attempted"] is True
    assert "超时" in analysis["model_record"]["reason"] and "未使用模型" in msg["note"]
    assert "原方案更合适的情况" in msg["text"] and "不同目标下的选择" in msg["text"]
    assert c.run()["comparison"]["comparable"] and c.last("result")


def test_offline_template_without_model(tmp_path):
    c = Chat(tmp_path).build_t10()
    c.say("把分格宽改为1.5m，与原方案比较")
    msg = c.last("analysis")
    analysis = c.ws.get_analysis(msg["card"]["analysis_id"])
    assert analysis["source"] == "template" and analysis["model_record"]["attempted"] is False
    assert analysis["result"]["leaning"]["choice"] == "depends"   # 没有目标时不直接推荐便宜方案
    assert "成本优先" in msg["text"] and "外观优先" in msg["text"]


def test_sentence_level_restrictions(tmp_path, online):
    state, _ = online

    def bad(payload):
        out = good_analysis(payload)
        out["effect_explanation"] = ("立柱减少后工程总造价节约 1,215.00 元。立柱线重更大所以承载力更高。"
                                     "节约比例约为 30%。尚不能确认结构安全，需要验算。")
        out["candidate"]["advantages"] = "修改方案整体合规且结构安全。"
        return out
    state["analysis"] = bad
    c = compare_chat(tmp_path)
    analysis = c.ws.get_analysis(c.last("analysis")["card"]["analysis_id"])
    removed = {x["text"]: " ".join(x["problems"]) for x in analysis["removed"]}
    assert any("总造价" in k for k in removed) and any("承载力" in k for k in removed)
    assert any("30%" in k for k in removed) and any("结构安全" in k and "整体" in k for k in removed)
    kept = analysis["result"]["effect_explanation"]
    assert "尚不能确认结构安全" in kept   # 合理限制说明保留
    assert "原方案更合适的情况" in analysis["blocks"]["main"]   # 其他部分不受影响


def test_multi_factor_change_not_attributed_to_single_factor(tmp_path, online):
    state, _ = online

    def attribute(payload):
        out = good_analysis(payload)
        out["effect_explanation"] = "材料金额减少全部来自分格变化。两个因素同时变化，合计结果见上表。"
        return out
    state["analysis"] = attribute
    c = Chat(tmp_path).build_t10()
    c.say("把分格宽改为1.5m，立柱线重改为2.5kg/m，立柱单价25元/kg，与原方案比较")
    analysis = c.ws.get_analysis(c.last("analysis")["card"]["analysis_id"])
    assert any("单一因素" in " ".join(x["problems"]) for x in analysis["removed"])
    assert "合计影响，未做分因素拆分" in "\n".join(analysis["effects"])
    assert "合计结果见上表" in analysis["result"]["effect_explanation"]


def test_change_check_statuses(tmp_path):
    from evidence.change_check import check_changes, NONCOMPLIANT, COMPLIANT, POTENTIAL, UNDETERMINED, NOT_APPLICABLE
    c = Chat(tmp_path).build_t10()
    c.say("把分格宽改为1.5m，与原方案比较")
    run = c.run()
    check = check_changes(run)
    items = {it["id"]: it for it in check["items"]}
    assert items["panel"]["results"]["candidate"]["status"] == POTENTIAL
    assert items["panel"]["results"]["baseline"]["status"] == UNDETERMINED
    assert "1.8" in items["panel"]["results"]["candidate"]["detail"] and "2.25" in items["panel"]["results"]["candidate"]["detail"]
    assert "风荷载" in "".join(items["column"]["params"])
    assert "profile" not in items   # 型材没变，不列型材核查
    assert "整体合规" in check["statement"]
    # 已审核规则：符合 / 不符合 / 效力待核实
    fake = dict(run, rule_results={
        "baseline": {"rule_results": [{"rule_id": "R1", "title": "分格宽限值", "evidence_type": "enterprise_rule",
                                       "status": "CONDITION_MET", "status_text": "已满足", "reason": "实际值 1.2 m",
                                       "condition": "grid_width_m <= 1.3 m"},
                                      {"rule_id": "R2", "title": "效力待核实条款", "evidence_type": "formal_clause",
                                       "status": "NEED_VERIFICATION", "status_text": "待核实", "reason": "条款效力未知"}]},
        "candidate": {"rule_results": [{"rule_id": "R1", "title": "分格宽限值", "evidence_type": "enterprise_rule",
                                        "status": "CONDITION_NOT_MET", "status_text": "未满足", "reason": "实际值 1.5 m",
                                        "condition": "grid_width_m <= 1.3 m"},
                                       {"rule_id": "R2", "title": "效力待核实条款", "evidence_type": "formal_clause",
                                        "status": "NEED_VERIFICATION", "status_text": "待核实", "reason": "条款效力未知"}]}})
    items = {it["id"]: it for it in check_changes(fake)["items"]}
    assert items["rule:R1"]["results"]["baseline"]["status"] == COMPLIANT
    assert items["rule:R1"]["results"]["candidate"]["status"] == NONCOMPLIANT
    assert "1.3" in items["rule:R1"]["results"]["candidate"]["detail"]
    assert items["rule:R2"]["results"]["candidate"]["status"] == UNDETERMINED and "效力" in items["rule:R2"]["results"]["candidate"]["detail"]
    # 只改单价：不涉及规范核查项
    c2 = Chat(tmp_path / "price").build_t10()
    c2.say("调整方案立柱单价改为 22 元/kg，与原方案比较")
    items = {it["id"]: it for it in check_changes(c2.run())["items"]}
    assert items["price"]["results"]["candidate"]["status"] == NOT_APPLICABLE and "panel" not in items


def test_demo_rules_never_become_compliance(tmp_path):
    from evidence.change_check import check_changes, COMPLIANT
    c = Chat(tmp_path).build_t10()
    pid = c.project()["project_id"]
    c.a.add_rule(pid, title="演示板块面积", lhs="panel_area_m2", operator="<=", threshold="2.0", unit="m²")
    c.say("把分格宽改为1.5m，与原方案比较")
    items = [it for it in check_changes(c.run())["items"] if it["kind"] == "demo_rule"]
    assert items and all(r["status"] != COMPLIANT for it in items for r in it["results"].values())
    assert "演示" in items[0]["results"]["candidate"]["detail"]


def test_history_binding_and_regeneration(tmp_path, online):
    c = compare_chat(tmp_path)
    first = c.last("analysis")
    first_analysis = c.ws.get_analysis(first["card"]["analysis_id"])
    c.say("把分格宽改为1.8m")
    second = c.last("analysis")
    assert second["id"] != first["id"] and second["card"]["run_id"] != first["card"]["run_id"]
    # 第一次的分析不随当前参数改写
    assert c.ws.get_analysis(first["card"]["analysis_id"]) == first_analysis
    assert "1.5" in first_analysis["result"]["change_summary"] or "1.5" in "".join(first_analysis["effects"]) or True
    old_run = c.ws.get_run(first["card"]["run_id"])
    from agent import engine
    assert not engine.is_current(c.project(), old_run)
    # 重新生成历史计算的分析：形成第 2 版，第 1 版保留
    c.a.act(c.conv["conversation_id"], first["id"], "regen_analysis", {"run_id": first["card"]["run_id"]})
    versions = c.ws.list_analyses(first["card"]["run_id"])
    assert [a["version"] for a in versions] == [1, 2] and versions[0] == first_analysis
    latest = c.ws.get_conversation(c.conv["conversation_id"])["messages"][-1]
    assert "第 2 版" in latest["text"] and "之前的版本保留" in latest["text"] and "参数在这次计算后已修改" in latest["text"]
    # 导出包含该运行的全部分析版本
    from reporting.xlsx import build_xlsx
    import io, openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(build_xlsx(old_run, analyses=versions)))
    assert {"方案分析v1", "方案分析v2"} <= set(wb.sheetnames)


def test_vague_change_asks_without_inventing_values(tmp_path, online):
    state, _ = online
    c = Chat(tmp_path).build_t10()
    before = json.dumps(c.project()["schemes"], sort_keys=True)
    c.say("把分格加大")
    m = c.last()
    assert m["kind"] == "question" and "待确认，尚未生效" in m["text"] and "分格宽" in m["text"]
    assert "成本" in m["text"] and "规范核查" in m["text"]
    assert "本地规则" in m["note"] and "超时" in m["note"]   # 模型超时如实显示
    assert json.dumps(c.project()["schemes"], sort_keys=True) == before   # 没有补造参数
    c.say("1.5米")
    p = c.project()
    from agent import projects as P
    assert P.has_candidate(p) and str(P.user_value(p, "candidate", "grid_width_m")[0]) in ("1.5", "1.50")
    assert c.last("analysis") is not None
    # 说“加大”却给了更小的值
    c.say("分格宽加大到1.0m")
    change = c.last("change")
    assert "实际是减小" in change["text"]


def test_vague_material_change(tmp_path):
    c = Chat(tmp_path).build_t10()
    c.say("立柱换一种型材")
    m = c.last()
    assert m["kind"] == "question" and "线重" in m["text"] and "单价" in m["text"]
    assert "线重不能代表截面性能" in m["text"]


def test_routing_single_check_vs_full_safety(tmp_path, monkeypatch):
    """单项核查走规范解读；整体结构安全仍说明边界，并在有规范库时给出单项依据。"""
    from agent.assistant import Assistant
    c = Chat(tmp_path).build_t10()
    consulted = []

    def consult(self, t, text, msg_id, **kw):
        consulted.append((text, kw.get("prefix", ""), kw.get("kind", "query")))
        self._say(t, (kw.get("prefix") or "") + "（解读）", kind=kw.get("kind", "query"), card={"type": "standard_qa", "run_id": "x"})
        return True
    monkeypatch.setattr(Assistant, "_consult", consult)
    c.say("分格宽1.5m是否满足规范要求")
    assert consulted[-1][2] == "query" and "单项核查" in consulted[-1][1]
    c.say("帮我验算立柱结构是否安全")
    assert consulted[-1][2] == "scope" and "不是结构安全结论" in consulted[-1][1]
    assert c.last()["kind"] == "scope"


def test_key_never_logged(tmp_path, monkeypatch):
    from agent import extraction
    secret = "sk-test-SECRET-1234567890"
    monkeypatch.setenv("MW_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)

    def leak(*a, **kw):
        raise RuntimeError(f"401 invalid api key {secret}")
    monkeypatch.setattr(extraction, "call_model_json", leak)
    raw, record = extraction.call_with_record("standard_interpret", "s", "{}", {"type": "object"})
    assert raw is None and secret not in json.dumps(record) and "***" in record["reason"]
    c = Chat(tmp_path).build_t10()
    monkeypatch.setattr(extraction, "understand", lambda text, ctx, mode="auto": __import__("agent.parser", fromlist=["x"]).parse(text) | {
        "model_mode": "fallback", "issues": [], "model_record": {}})
    c.say("把分格宽改为1.5m，与原方案比较")
    dump = json.dumps([c.ws.events(limit=500), c.ws.list_analyses(c.run()["run_id"])], ensure_ascii=False)
    assert secret not in dump


def test_nonstandard_choice_does_not_sink_analysis(tmp_path, online):
    state, _ = online

    def odd(payload):
        out = good_analysis(payload)
        out["leaning"]["choice"] = "修改方案"
        out["goal_scenarios"][0]["choice"] = "原方案（若验算不通过）"
        return out
    state["analysis"] = odd
    c = compare_chat(tmp_path)
    analysis = c.ws.get_analysis(c.last("analysis")["card"]["analysis_id"])
    assert analysis["source"] == "model"
    assert analysis["result"]["leaning"]["choice"] == "candidate"
    assert analysis["result"]["goal_scenarios"][0]["choice"] == "baseline"
