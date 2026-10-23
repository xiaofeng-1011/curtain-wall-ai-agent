"""v0.4 状态与名称回归；只使用隔离工作区，不访问在线模型。"""
import pytest

from agent import extraction
from agent.assistant import AssistantError
from storage.workspace import Workspace
from tests.chat_helpers import Chat


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv("MW_FORCE_FALLBACK", "1")


def test_title_and_user_message_are_durable_before_understanding(tmp_path, monkeypatch):
    chat = Chat(tmp_path).new()
    original = extraction.understand

    def observed(text, *args):
        saved = chat.ws.get_conversation(chat.conv["conversation_id"])
        assert saved["title"] == "玻璃幕墙立柱截面厚度要求"
        assert saved["messages"][0]["text"] == text
        return original(text, *args)

    monkeypatch.setattr(extraction, "understand", observed)
    chat.say("玻璃幕墙立柱截面厚度要求")
    assert chat.last()["kind"] == "query"
    assert chat.project() is None


def test_manual_title_survives_project_creation_and_both_renames(tmp_path):
    chat = Chat(tmp_path).new()
    cid = chat.conv["conversation_id"]
    chat.a.rename_conversation(cid, "新对话")  # 手动名称即使等于默认名，也必须保留。
    chat.say("幕墙宽18m、高6m，分格宽1.2m、高1.5m")
    assert chat.conv["title"] == "新对话"
    name = chat.project()["name"]
    chat.a.rename_conversation(cid, "# 中文 <特殊> [标题]")
    assert chat.project()["name"] == name
    chat.a.rename_project(chat.project()["project_id"], "项目独立名称")
    assert chat.ws.get_conversation(cid)["title"] == "# 中文 <特殊> [标题]"
    for invalid in ("   ", "长" * 41):
        with pytest.raises(AssistantError):
            chat.a.rename_conversation(cid, invalid)


def test_auto_title_stays_stable_and_duplicate_request_is_ignored(tmp_path):
    chat = Chat(tmp_path).new()
    cid = chat.conv["conversation_id"]
    chat.conv = chat.a.send(cid, "玻璃幕墙立柱截面厚度要求", request_id="one")
    count = len(chat.conv["messages"])
    assert len(chat.a.send(cid, "玻璃幕墙立柱截面厚度要求", request_id="one")["messages"]) == count
    chat.say("建立项目，宽18m、高6m")
    assert chat.conv["title"] == "玻璃幕墙立柱截面厚度要求"


def test_failed_understanding_keeps_first_input(tmp_path, monkeypatch):
    chat = Chat(tmp_path).new()
    def fail(*args):
        raise TimeoutError("offline test")
    monkeypatch.setattr(extraction, "understand", fail)
    with pytest.raises(TimeoutError):
        chat.say("玻璃幕墙立柱截面厚度要求")
    saved = chat.ws.get_conversation(chat.conv["conversation_id"])
    assert saved["title"] == "玻璃幕墙立柱截面厚度要求"
    assert len(saved["messages"]) == 1


def test_attachment_title_and_safe_legacy_backfill(tmp_path):
    chat = Chat(tmp_path).new()
    chat.say("", files=[("幕墙要求.md", ("资料文字。" * 30).encode())])
    assert chat.conv["title"] == "幕墙要求 · 查阅"
    old = chat.conv.copy()
    old.update(title="新对话", title_source="default")
    old = chat.ws.save_conversation(old, old["rev"])
    reopened = Workspace(tmp_path).get_conversation(old["conversation_id"])
    assert reopened["title"] == "幕墙要求 · 查阅"
    assert reopened["messages"] == old["messages"]
    assert reopened["updated_at"] == old["updated_at"]


def test_requirement_query_with_sources_and_safety_boundary(tmp_path):
    chat = Chat(tmp_path).new()
    chat.say("", files=[("测试说明.md", ("立柱截面厚度要求。本段仅用于界面测试，不代表任何规范条文。" * 12).encode())])
    chat.say("玻璃幕墙立柱截面厚度要求")
    assert chat.last()["kind"] == "query"
    assert chat.last()["card"]["type"] == "query"
    chat.say("帮我验算立柱结构是否安全")
    assert chat.last()["kind"] == "scope"
    chat.click("这是资料查询")
    assert chat.last()["kind"] == "query"
    assert chat.project() is None


def test_history_order_is_stable_when_timestamps_tie(tmp_path, monkeypatch):
    monkeypatch.setattr("storage.workspace.now_utc", lambda: "2026-10-12T10:00:00+00:00")
    chat = Chat(tmp_path)
    first = chat.a.new_conversation()
    second = chat.a.new_conversation()
    assert [c["conversation_id"] for c in chat.ws.list_conversations()] == [second["conversation_id"], first["conversation_id"]]
