"""V5 代表性在线验收：使用本机已配置的真实模型，在隔离工作区运行规范解读、方案修改与分析、失败回退。

用法：python packaging/verify_v5_online.py --app <业务代码目录> --out <记录目录> [--label v5|v4]
- 模型配置从 %LOCALAPPDATA%/MqHelper/config.toml 读入当前进程环境，不复制、不写入任何记录。
- 工作区为临时目录，不读写用户的真实工程、聊天和资料。
- --label v4 时用 V4 业务代码跑同样的问题，作为“修改前”样例。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

QA = [
    ("预埋件", "玻璃幕墙立柱与主体结构如何连接？部分楼层预埋件漏埋了怎么办？",
     {"说明": "框支承隐框玻璃幕墙，主体为钢筋混凝土框架，部分楼层预埋件漏埋"}),
    ("结构胶", "隐框玻璃幕墙的硅酮结构密封胶有什么要求？现场能不能打注？", None),
    ("防火", "幕墙与楼板之间的缝隙防火封堵有什么要求？", {"说明": "高层办公楼，构件式玻璃幕墙，层间有楼板"}),
]
SCHEME = ["我更看重外观通透，其次才是成本", "把分格加大", "1.5米",
          "立柱线重改为2.5kg/m，立柱单价26元/kg",
          "分格宽1.5m时玻璃面板是否满足规范要求？",
          "帮我验算立柱结构是否安全"]


def load_model_env():
    import tomllib
    path = Path(os.environ.get("LOCALAPPDATA", "")) / "MqHelper" / "config.toml"
    if path.exists():
        for k, v in tomllib.loads(path.read_text(encoding="utf-8")).items():
            os.environ.setdefault(k, str(v).strip())


def secrets():
    return [v for k, v in os.environ.items() if k.endswith("_API_KEY") and len(v) > 8]


def scrub(obj):
    text = json.dumps(obj, ensure_ascii=False, default=str)
    for s in secrets():
        text = text.replace(s, "***")
    return json.loads(text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--label", default="v5")
    ap.add_argument("--skip-failure", action="store_true")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    load_model_env()
    work = Path(tempfile.mkdtemp(prefix=f"mw-{args.label}-online-"))
    os.environ["MW_RUNTIME_DIR"] = str(work / "workspace")
    os.environ["LOCALAPPDATA"] = str(work / "profile")   # 隔离设置文件写入
    sys.path.insert(0, args.app)
    os.chdir(args.app)
    from agent import extraction
    from agent.assistant import Assistant
    from storage.workspace import Workspace
    from storage.bundled_standards import initialize_builtin
    ws = Workspace(work / "workspace")
    status = initialize_builtin(ws.standard_library())
    record = {"label": args.label, "app": args.app, "provider": extraction._provider(), "model": extraction.model_id(),
              "model_available": extraction.model_available(), "builtin": status.get("status"),
              "started": time.strftime("%Y-%m-%d %H:%M:%S"), "qa": [], "scheme": [], "failure": {}}
    asst = Assistant(ws)

    def last_messages(conv, n):
        return [{"kind": m["kind"], "text": m["text"], "note": m.get("note"), "card": m.get("card")}
                for m in conv["messages"][-n:] if m["role"] == "assistant"]

    # ---------------------------------------------------------- 规范问答
    for name, question, ctx in QA:
        conv = asst.new_conversation(mode="standard_qa")
        options = dict(conv["qa_options"], answer_mode="answer", allow_model=True)
        conv = asst.set_qa_options(conv["conversation_id"], options)
        if ctx and args.label == "v5":
            from agent import engine
            started = time.time()
            run = engine.run_standard_query(ws, query_text=question, conversation_id=conv["conversation_id"],
                                           request_id=f"accept:{name}", options=asst.effective_qa_options(conv),
                                           project_context=ctx)
            item = {"name": name, "question": question, "project": ctx, "seconds": round(time.time() - started, 1),
                    "text": run["answer"]["text"], "notice": run["answer"]["notice"]}
        else:
            started = time.time()
            conv = asst.send(conv["conversation_id"], question)
            msg = conv["messages"][-1]
            run = ws.get_run(msg["card"]["run_id"])
            item = {"name": name, "question": question, "project": None, "seconds": round(time.time() - started, 1),
                    "text": msg["text"], "notice": msg.get("note")}
        answer = run["answer"]
        item.update(mode=answer.get("mode"), validation=answer.get("validation"), model_record=answer.get("model_record"))
        if answer.get("interpretation"):
            from agent.standard_qa import notes_text
            item["notes"] = notes_text(answer)
            item["claims"] = [{k: c.get(k) for k in ("kind", "text", "status", "problems", "flags")}
                              for c in answer["interpretation"]["claims"]]
        record["qa"].append(item)
        print("QA", name, item["mode"], item["seconds"], "s", flush=True)

    # ---------------------------------------------------------- 方案修改与分析
    conv = asst.new_conversation()
    cid = conv["conversation_id"]
    for text in ["手工测试甲，构件式玻璃幕墙，宽18m、高6m，分格宽1.2m、高1.5m。本次数据为 synthetic 演示数据。",
                 "立柱 2.7kg/m、25 元/kg，横梁 1.3kg/m、28 元/kg，算材料金额"]:
        conv = asst.send(cid, text)
    confirm = next(m for m in reversed(conv["messages"]) if any(a["action"] == "confirm_run" for a in m.get("actions") or []))
    conv = asst.act(cid, confirm["id"], "confirm_run", {"task": "cost"})
    for text in SCHEME:
        n = len(conv["messages"])
        started = time.time()
        conv = asst.send(cid, text)
        record["scheme"].append({"user": text, "seconds": round(time.time() - started, 1),
                                 "replies": last_messages(conv, len(conv["messages"]) - n - 1)})
        print("SCHEME", text, round(time.time() - started, 1), "s", flush=True)
    analyses = [m for m in conv["messages"] if m["kind"] == "analysis"]
    if analyses and args.label == "v5":
        first = analyses[0]
        before = ws.get_analysis(first["card"]["analysis_id"])
        conv = asst.act(cid, first["id"], "regen_analysis", {"run_id": first["card"]["run_id"]})
        versions = ws.list_analyses(first["card"]["run_id"])
        record["history"] = {"first_run": first["card"]["run_id"], "versions": [a["version"] for a in versions],
                             "v1_unchanged": versions[0] == before, "regen_reply": conv["messages"][-1]["text"][:400],
                             "first_analysis_still_bound_to": versions[0]["binding"]["snapshots"]}
        record["analyses"] = [{k: a.get(k) for k in ("analysis_id", "run_id", "version", "source", "model_record", "removed",
                                                      "program_notes", "goals", "effects")}
                              for m in analyses for a in [ws.get_analysis(m["card"]["analysis_id"])]]
    if args.label == "v4":
        conv = asst.send(cid, "为什么金额减少？")
        record["scheme"].append({"user": "为什么金额减少？", "replies": last_messages(conv, 1)})

    # ---------------------------------------------------------- 模型超时：如实回退，本地功能可用
    if not args.skip_failure and args.label == "v5":
        extraction.ANALYSIS_TIMEOUT_S = 0.001
        extraction.MODEL_TIMEOUT_S = 0.001
        conv = asst.new_conversation(mode="standard_qa")
        conv = asst.send(conv["conversation_id"], QA[1][1])
        qa = ws.get_run(conv["messages"][-1]["card"]["run_id"])["answer"]
        conv2 = asst.send(cid, "把分格宽改为1.8m")
        analysis = next((m for m in reversed(conv2["messages"]) if m["kind"] == "analysis"), None)
        a = ws.get_analysis(analysis["card"]["analysis_id"]) if analysis else None
        record["failure"] = {"qa_mode": qa["mode"], "qa_notice": qa["notice"], "qa_evidence": len(qa["evidence"]),
                             "qa_text": qa["text"][:300], "analysis_source": (a or {}).get("source"),
                             "analysis_note": (analysis or {}).get("note"),
                             "calc_ok": bool(ws.get_run(analysis["card"]["run_id"])["comparison"]["comparable"]) if analysis else False}
    record["events_with_key"] = any(s in json.dumps(ws.events(limit=1000), ensure_ascii=False) for s in secrets())
    record["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    (out / f"online-{args.label}.json").write_text(json.dumps(scrub(record), ensure_ascii=False, indent=2), encoding="utf-8")
    ws.db.close()
    shutil.rmtree(work, ignore_errors=True)
    print(json.dumps({"qa": [(q["name"], q["mode"]) for q in record["qa"]], "failure": record["failure"],
                      "events_with_key": record["events_with_key"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
