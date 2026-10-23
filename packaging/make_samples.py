"""由在线验收记录生成《修改前后回答样例.md》：V4 与 V5 用同一问题、同一模型配置的真实输出。"""
import json
import sys
from pathlib import Path

rec = Path(sys.argv[1])
v4 = json.loads((rec / "online-v4.json").read_text(encoding="utf-8"))
v5 = json.loads((rec / "online-v5.json").read_text(encoding="utf-8"))
L = ["# 修改前后回答样例（真实在线模型输出）", "",
     f"- 模型：{v5['provider']} / {v5['model']}（本机已保存的配置；记录中不含 API Key）",
     f"- V4（修改前）运行：{v4['started']}；V5（修改后）运行：{v5['started']}",
     "- 两版在各自的临时隔离工作区运行，使用同一份内置规范库、同样的问题和同样的方案操作。",
     "- 以下正文为程序实际显示给用户的文字，未经人工改写；V5 备注中的出处由程序按登记信息生成。", ""]
L += ["## 一、规范问答", ""]
for a, b in zip(v4["qa"], v5["qa"]):
    L += [f"### 问题：{b['question']}", ""]
    if b.get("project"):
        L += [f"V5 同时提供了项目条件：{b['project'].get('说明')}（V4 不支持结合项目条件）", ""]
    L += ["**修改前（V4，“基于资料回答 + 使用模型”）**", "", f"> 状态：{a.get('notice')}", "", "```text", a["text"].strip(), "```", ""]
    L += ["**修改后（V5，AI 解读）**", "", f"> 状态：{b.get('notice')}", "", b["text"].strip(), ""]
    if b.get("notes"):
        L += ["备注（程序生成的出处）：", ""] + [f"- {n}" for n in b["notes"]] + [""]
    restricted = [c for c in b.get("claims", []) if c["status"] == "restricted"]
    if restricted:
        L += ["程序校验移除的结论（不显示在正文，附原因）：", ""]
        L += [f"- “{c['text']}” —— {'；'.join(c['problems'])}" for c in restricted] + [""]
L += ["## 二、方案修改与比选", ""]


def replies(rec_list, user):
    for s in rec_list:
        if s["user"] == user:
            return s
    return None


for user in [s["user"] for s in v5["scheme"]]:
    a, b = replies(v4["scheme"], user), replies(v5["scheme"], user)
    L += [f"### 用户：{user}", ""]
    for title, s in (("修改前（V4）", a), ("修改后（V5）", b)):
        L += [f"**{title}**", ""]
        if not s:
            L += ["（无）", ""]
            continue
        for r in s["replies"]:
            if r["kind"] == "result":
                L += [f"- 结果卡：{r['text']}"]
                continue
            L += [f"- 类型：{r['kind']}" + (f" ｜ {r['note']}" if r.get("note") else ""), "", r["text"].strip(), ""]
        L += [""]
v4why = replies(v4["scheme"], "为什么金额减少？")
if v4why:
    L += ["### V4 需要用户追问“为什么”才给出的解释", "", v4why["replies"][0]["text"].strip(), "",
          "V5 在重新计算成功后自动给出上面的方案分析，不需要追问。", ""]
h = v5.get("history") or {}
L += ["## 三、历史分析绑定", "",
      f"- 第一次比较的运行：{h.get('first_run')}；该运行的分析版本：{h.get('versions')}；重新生成后第 1 版是否原样保留：{h.get('v1_unchanged')}",
      f"- 重新生成时的提示：{(h.get('regen_reply') or '').splitlines()[0] if h.get('regen_reply') else ''}", ""]
f = v5.get("failure") or {}
L += ["## 四、模型超时（人为把超时设为 0.001 秒）", "",
      f"- 规范问答：{f.get('qa_notice')}；仍返回 {f.get('qa_evidence')} 处本地检索依据。",
      f"- 方案分析：{f.get('analysis_note')}；计算结果正常：{f.get('calc_ok')}", ""]
(rec.parent / "修改前后回答样例.md").write_text("\n".join(L), encoding="utf-8")
print("ok")
