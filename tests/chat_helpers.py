"""对话测试辅助：每个测试一个独立空白工作区；默认本地解析（不访问网络模型）。"""
import json
from pathlib import Path

from agent.assistant import Assistant
from reporting.report import all_metrics, fmt
from storage.workspace import Workspace

EXPECTED = json.loads((Path(__file__).parent / "fixtures" / "manual_expected.json").read_text(encoding="utf-8"))


class Chat:
    def __init__(self, root):
        self.ws = Workspace(root)
        self.a = Assistant(self.ws)
        self.conv = None

    def new(self, project_id=None):
        self.conv = self.a.new_conversation(project_id)
        return self

    def say(self, text, files=None):
        if self.conv is None:
            self.new()
        self.conv = self.a.send(self.conv["conversation_id"], text, files)
        return self.conv

    def click(self, label):
        for m in reversed(self.conv["messages"]):
            for x in m.get("actions") or []:
                if x["label"] == label and not m.get("consumed"):
                    r = self.a.act(self.conv["conversation_id"], m["id"], x["action"], x.get("payload"))
                    if r.get("_deleted"):
                        return r
                    self.conv = r
                    return r
        raise AssertionError(f"没有按钮：{label}；最近消息：{[m['text'][:40] for m in self.conv['messages'][-3:]]}")

    def last(self, kind=None):
        for m in reversed(self.conv["messages"]):
            if m["role"] == "assistant" and (kind is None or m["kind"] == kind):
                return m
        return None

    def run(self):
        m = self.last("result")
        return self.ws.get_run(m["card"]["run_id"]) if m else None

    def project(self):
        return self.a.project_of(self.ws.get_conversation(self.conv["conversation_id"]))

    def build_t10(self, b="1.2", confirm=True):
        self.say(f"手工测试甲，构件式玻璃幕墙，宽18m、高6m，分格宽{b}m、高1.5m。本次数据为 synthetic 演示数据。")
        self.say("立柱 2.7kg/m、25 元/kg，横梁 1.3kg/m、28 元/kg，算材料金额")
        if confirm:
            self.click("确认并计算")
        return self


def disp(run, role, key):
    m = all_metrics((run.get("results") or {}).get(role)).get(key)
    return None if not m or m.get("value") is None else fmt(m, key)
