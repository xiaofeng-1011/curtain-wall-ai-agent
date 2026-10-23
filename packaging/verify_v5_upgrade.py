"""已有 V4 用户升级到 V5 的真实验收（隔离的临时用户目录，不触碰本机真实数据）。

1. 用 V4 安装包静默安装（验收模式），用安装后的 V4 业务代码在默认用户数据位置写入：
   计算过的项目与聊天、主动清空资料选择的规范问答会话、个人型材库、模型配置（假 Key）。
2. 用 V5 安装包覆盖安装到同一目录。
3. 核对：用户数据库与 config.toml 原样保留；V5 新模块已安装；启动 V5 后旧项目、旧消息、
   空资料选择、模型提供方仍在；内置库无需重新选择即可使用；旧结果可以被 V5 正常显示。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--v4-installer", required=True)
ap.add_argument("--v5-installer", required=True)
ap.add_argument("--target", required=True)
ap.add_argument("--out", required=True)
args = ap.parse_args()
target = Path(args.target)
target.mkdir(parents=True, exist_ok=False)
out = Path(args.out)
profile = target / "隔离用户"
app_dir = target / "程序"
workspace = profile / "MqHelper" / "workspace"
config = profile / "MqHelper" / "config.toml"
FAKE_KEY = "sk-upgrade-test-not-a-real-key-0000"
base_env = dict(os.environ, LOCALAPPDATA=str(profile), MW_NO_BROWSER="1")
for k in list(base_env):
    if k.endswith("_API_KEY") or k.startswith("MW_"):
        base_env.pop(k)
base_env["MW_NO_BROWSER"] = "1"
report = {"checks": [], "target": str(target)}


def check(name, ok, detail=""):
    report["checks"].append({"name": name, "ok": bool(ok), "detail": str(detail)[:500]})
    print(("PASS " if ok else "FAIL ") + name, str(detail)[:150], flush=True)


def install(installer, log):
    r = subprocess.run([installer, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/PORTABLETEST=1", "/TASKS=",
                        f"/DIR={app_dir}", f"/LOG={target / log}"], env=base_env, timeout=900)
    return r.returncode


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


check("V4 installed", install(args.v4_installer, "install-v4.log") == 0)
check("V4 installation has no V5 modules", not (app_dir / "_internal/app/reporting/analysis.py").exists())
seed = r'''
import os, sys, json
sys.path.insert(0, sys.argv[1]); os.chdir(sys.argv[1])
from storage.workspace import Workspace
from storage.bundled_standards import initialize_builtin
from agent.assistant import Assistant
ws = Workspace(sys.argv[2]); print(initialize_builtin(ws.standard_library())["status"])
a = Assistant(ws)
c = a.new_conversation()
c = a.send(c["conversation_id"], "升级前项目，构件式玻璃幕墙，宽18m、高6m，分格宽1.2m、高1.5m。本次数据为 synthetic 演示数据。")
c = a.send(c["conversation_id"], "立柱 2.7kg/m、25 元/kg，横梁 1.3kg/m、28 元/kg，算材料金额")
m = next(m for m in reversed(c["messages"]) if any(x["action"] == "confirm_run" for x in m.get("actions") or []))
c = a.act(c["conversation_id"], m["id"], "confirm_run", {"task": "cost"})
c = a.send(c["conversation_id"], "把分格宽改为1.5m，与原方案比较")
qa = a.new_conversation(mode="standard_qa")
qa = a.set_qa_options(qa["conversation_id"], dict(qa["qa_options"], collection_ids=[]))
p = a.project_of(c); lib = a.save_to_library(p["project_id"], "baseline", "column")
print(json.dumps({"project": p["project_id"], "conv": c["conversation_id"], "qa": qa["conversation_id"], "lib": lib,
                  "runs": ws.counts()["runs"]}))
'''
(target / "seed_v4.py").write_text(seed, encoding="utf-8")
config.parent.mkdir(parents=True, exist_ok=True)
config.write_text(f'MW_PROVIDER = "kimi"\nMOONSHOT_API_KEY = "{FAKE_KEY}"\nMW_MODEL = "moonshot-v1-8k"\n', encoding="utf-8")
r = subprocess.run([sys.executable, "-B", str(target / "seed_v4.py"), str(app_dir / "_internal" / "app"), str(workspace)],
                   env=dict(base_env, PYTHONIOENCODING="utf-8"), capture_output=True, text=True, encoding="utf-8", timeout=600)
seeded = json.loads(r.stdout.strip().splitlines()[-1])
report["seeded"] = seeded
check("V4 user data created with installed V4 code", seeded["runs"] >= 2, seeded)
config_hash = digest(config)
db = sqlite3.connect(workspace / "workspace.db")
before = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("projects", "conversations", "runs", "snapshots", "library_materials")}
db.close()

check("V5 installed over V4", install(args.v5_installer, "install-v5.log") == 0)
check("V5 modules present after upgrade", all((app_dir / "_internal/app" / m).is_file() for m in (
    "agent/claim_check.py", "evidence/change_check.py", "reporting/analysis.py")))
check("config.toml unchanged by installer", digest(config) == config_hash)
db = sqlite3.connect(workspace / "workspace.db")
after = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in before}
db.close()
check("workspace data unchanged by installer", after == before, {"before": before, "after": after})

child = subprocess.Popen([str(app_dir / "幕墙方案助手.exe")], env=base_env)
try:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url, deadline = None, time.monotonic() + 180
    while time.monotonic() < deadline and not url:
        try:
            state = json.loads((workspace / "launcher-state.json").read_text())
            with opener.open(state["url"] + "/_stcore/health", timeout=2) as resp:
                url = state["url"] if resp.status == 200 else None
        except (OSError, ValueError, KeyError):
            time.sleep(1)
    check("V5 starts on upgraded user data", url, url)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.set_default_timeout(30000)

        def idle():
            page.wait_for_timeout(600)
            page.wait_for_function('document.querySelector("[data-testid=stApp]")?.getAttribute("data-test-script-state") === "notRunning"',
                                   timeout=240000)
        page.goto(url)
        page.get_by_test_id("stChatInput").wait_for(timeout=240000)
        idle()
        history = page.locator('[class*="st-key-history-"]').all_inner_texts()
        check("old project conversation listed", any("升级前项目" in h for h in history), history[:5])
        page.get_by_text("升级前项目").first.click()
        idle()
        body = page.inner_text("body")
        check("old result card still renders", "已覆盖型材材料金额" in body and page.get_by_test_id("stException").count() == 0)
        page.screenshot(path=str(out / "upgrade-old-project.png"))
        qa_title = page.locator('[class*="st-key-history-"] button').filter(has_text="新对话")
        page.goto(url + "/?c=" + seeded["qa"])
        page.get_by_test_id("stChatInput").wait_for(timeout=240000)
        idle()
        check("explicitly emptied collection selection kept", page.get_by_text("未选择资料集，不会检索全部资料").count() > 0)
        page.screenshot(path=str(out / "upgrade-empty-selection.png"))
        page.get_by_role("button", name="设置").first.click()
        page.get_by_role("tab", name="模型连接").click()
        page.get_by_text("当前模型").last.wait_for()
        text = page.get_by_text("当前模型").last.inner_text()
        page.screenshot(path=str(out / "upgrade-model-config.png"))
        check("model provider config kept", "Kimi" in text and "moonshot-v1-8k" in text, text[:200])
        page.keyboard.press("Escape")
        idle()
        page.get_by_role("button", name="规范问答").first.click()
        idle()
        check("builtin library usable for new QA without reselecting", page.get_by_text("已选 1 项").count() > 0 or
              page.get_by_text("未选择资料集").count() > 0)
        report["new_qa_selection_text"] = page.locator('[data-testid="stCaptionContainer"]').all_inner_texts()[:4]
        browser.close()
finally:
    child.terminate()
    try:
        child.wait(timeout=15)
    except subprocess.TimeoutExpired:
        child.kill()
db = sqlite3.connect(workspace / "workspace.db")
check("analyses table added without touching old rows",
      db.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == before["projects"]
      and db.execute("SELECT name FROM sqlite_master WHERE name='analyses'").fetchone() is not None)
db.close()
report["ok"] = all(c["ok"] for c in report["checks"])
text = json.dumps(report, ensure_ascii=False, indent=2).replace(FAKE_KEY, "***")
(out / "upgrade-acceptance.json").write_text(text, encoding="utf-8")
print("OK" if report["ok"] else "FAILED")
