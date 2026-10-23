"""DeepSeek 接入：请求参数、上下文、结构检查与降级（假客户端，不访问网络）。"""
import json
import sys
import types

import pytest

from agent import extraction


class FakeCompletions:
    def __init__(self, content, finish_reason="stop", exc=None):
        self.content, self.finish_reason, self.exc = content, finish_reason, exc
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if self.exc:
            raise self.exc
        msg = types.SimpleNamespace(content=self.content)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg, finish_reason=self.finish_reason)])


@pytest.fixture()
def fake(monkeypatch):
    holder = {}

    def install(content, finish_reason="stop", exc=None):
        comp = FakeCompletions(content, finish_reason, exc)

        class FakeOpenAI:
            def __init__(self, **kw):
                holder["client_kwargs"] = kw
                self.chat = types.SimpleNamespace(completions=comp)

        monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=FakeOpenAI))
        holder["comp"] = comp
        return holder

    monkeypatch.setenv("MW_PROVIDER", "deepseek")
    monkeypatch.setenv("MW_MODEL", "deepseek-v4-pro")
    monkeypatch.delenv("MW_BASE_URL", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.delenv("MW_FORCE_FALLBACK", raising=False)
    return install


def good(**over):
    base = {"intents": ["modify", "compute"], "task": "compare", "target": None,
            "patches": [{"field": "grid_width_m", "op": "set", "value": "1.5", "unit": "m",
                         "evidence": "分格宽改为1.5m", "claimed_previous_value": None}],
            "project_name": None, "create_project": False, "source_decl": None, "tax": None, "reuse_price": [],
            "skip": False, "confirm": False, "query_text": None, "rename_to": None, "scope_hints": [], "ambiguous": []}
    base.update(over)
    return base


CTX = {"active_field": None, "last_field": "grid_width_m",
       "model_context": {"project": "手工测试甲", "schemes": {"原方案": {"分格宽": "1.2 m"}},
                         "recent_messages": [{"role": "user", "text": "宽18m"}]}}


def test_request_shape_and_context(fake):
    h = fake(json.dumps(good(), ensure_ascii=False))
    r = extraction.understand("把分格宽改为1.5m，与原方案比较", CTX)
    assert r["model_mode"] == "online" and r["patches"][0]["value"] == "1.5"
    kw = h["comp"].kwargs
    assert kw["model"] == "deepseek-v4-pro" and kw["response_format"] == {"type": "json_object"}
    assert kw["extra_body"] == {"thinking": {"type": "disabled"}}
    user = kw["messages"][1]["content"]
    assert "手工测试甲" in user and "上次修改的字段：grid_width_m" in user and "宽18m" in user
    assert h["client_kwargs"]["base_url"] == "https://api.deepseek.com"


def test_free_text_scope_hint_does_not_block(fake):
    fake(json.dumps(good(scope_hints=["构件式玻璃幕墙方案阶段"]), ensure_ascii=False))
    r = extraction.understand("把分格宽改为1.5m，与原方案比较", CTX)
    assert r["scope_hints"] == []


def test_real_scope_detected(fake):
    fake(json.dumps(good(patches=[], intents=["other"], scope_hints=["结构安全判断"]), ensure_ascii=False))
    r = extraction.understand("请判断单元式幕墙结构是否安全", CTX)
    assert "单元式幕墙" in r["scope_hints"] and "完整结构计算" in r["scope_hints"]


def test_invented_value_dropped(fake):
    bad = good()
    bad["patches"][0]["value"] = "1.8"
    fake(json.dumps(bad))
    r = extraction.understand("把分格宽改为1.5m", CTX)
    # 模型的值不在原文中被丢弃，本地解析结果补位
    assert [p["value"] for p in r["patches"]] == ["1.5"]
    assert any("未出现在原文" in i["message"] for i in r["issues"])


def test_chinese_number_value_accepted(fake):
    g = good()
    g["patches"][0].update(value="1.5", unit="米", evidence="一米五")
    fake(json.dumps(g, ensure_ascii=False))
    r = extraction.understand("分格宽改成一米五", CTX)
    assert r["model_mode"] == "online" and r["patches"][0]["value"] == "1.5"


@pytest.mark.parametrize("kw", [{"content": "not json"}, {"content": ""}, {"content": "{}", "finish_reason": "length"},
                                {"content": None, "exc": TimeoutError("timed out")},
                                {"content": json.dumps({"intents": ["approve"]})}])
def test_failures_degrade(fake, kw):
    fake(**kw)
    r = extraction.understand("把分格宽改为1.5m", CTX)
    assert r["model_mode"] == "fallback" and r["patches"][0]["field"] == "grid_width_m"
    assert r["model_record"]["ok"] is False


def test_short_replies_skip_model(fake):
    h = fake(json.dumps(good()))
    assert extraction.understand("确认", CTX)["confirm"] is True
    assert h["comp"].kwargs is None


def test_no_key_fallback(monkeypatch):
    monkeypatch.setenv("MW_PROVIDER", "deepseek")
    monkeypatch.delenv("MW_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("MW_FORCE_FALLBACK", raising=False)
    assert extraction.model_available() is False
    assert extraction.understand("层高 3 米", {})["model_mode"] == "fallback"
