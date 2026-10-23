"""对冻结程序（便携目录或安装后的程序）做真实浏览器端到端验收。

python packaging/verify_v5_frozen.py --exe <幕墙方案助手.exe> --out <记录目录> --prefix portable
- 工作区与用户设置目录均为临时目录；模型配置从本机 config.toml 读到子进程环境变量，不写入任何文件。
- --workspace 可指定已有工作区（升级验收）；--keep-workspace 结束后保留工作区供检查。
- 检查：内置库就绪、AI 解读实际调用并显示出处、原 PDF 打开、修改意图追问、计算后自动分析、
  历史分析不被改写、调用记录、关闭模型的选择被记住、V4 界面样式未退化。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import time
import tomllib
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ap = argparse.ArgumentParser()
ap.add_argument("--exe", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--prefix", default="frozen")
ap.add_argument("--workspace")
ap.add_argument("--keep-workspace", action="store_true")
ap.add_argument("--no-model", action="store_true")
ap.add_argument("--upgrade-check", action="store_true", help="只验证已有工作区数据与设置在新版中保留")
args = ap.parse_args()
out = Path(args.out)
out.mkdir(parents=True, exist_ok=True)
temp = Path(tempfile.mkdtemp(prefix="mw-v5-frozen-"))
workspace = Path(args.workspace) if args.workspace else temp / "用户 工作区"
env = dict(os.environ, MW_RUNTIME_DIR=str(workspace), LOCALAPPDATA=str(temp / "隔离用户"), MW_NO_BROWSER="1")
env["PATH"] = os.pathsep.join([str(Path(os.environ["SystemRoot"]) / "System32"), os.environ["SystemRoot"]])
for k in list(env):
    if k.endswith("_API_KEY") or k in ("MW_PROVIDER", "MW_MODEL", "MW_BASE_URL"):
        env.pop(k)
config = Path(os.environ["LOCALAPPDATA"]) / "MqHelper" / "config.toml"
if config.exists() and not args.no_model:
    for k, v in tomllib.loads(config.read_text(encoding="utf-8")).items():
        env[k] = str(v)
secret_values = [v for k, v in env.items() if k.endswith("_API_KEY")]
report = {"exe": args.exe, "prefix": args.prefix, "model_env": not args.no_model and bool(secret_values),
          "workspace_isolated": not args.workspace, "checks": [], "started": time.strftime("%Y-%m-%d %H:%M:%S")}
child = subprocess.Popen([args.exe], env=env)


def check(name, ok, detail=""):
    report["checks"].append({"name": name, "ok": bool(ok), "detail": str(detail)[:600]})
    print(("PASS " if ok else "FAIL ") + name, str(detail)[:160], flush=True)
    return ok


try:
    deadline = time.monotonic() + 180
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = None
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError("服务启动失败；查看工作区 launcher.log")
        try:
            state = json.loads((workspace / "launcher-state.json").read_text())
            with opener.open(state["url"] + "/_stcore/health", timeout=2) as r:
                if r.status == 200:
                    url = state["url"]
                    break
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(1)
    check("frozen service health", url, url)

    pw = sync_playwright().start()
    if True:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.set_default_timeout(30000)

        def idle(timeout=240000):
            page.wait_for_timeout(600)
            page.wait_for_function('document.querySelector("[data-testid=stApp]")?.getAttribute("data-test-script-state") === "notRunning"',
                                   timeout=timeout)

        def shot(name):
            page.screenshot(path=str(out / f"{args.prefix}-{name}.png"), full_page=False)

        def say(text, wait_text=None, timeout=300000):
            """发送后等待：用户消息出现、其后出现新的助手回复、脚本空闲且内容稳定。"""
            users = page.locator('[aria-label="Chat message from user"]').count()
            box = page.get_by_test_id("stChatInputTextArea")
            box.fill(text)
            box.press("Enter")
            page.wait_for_function(f'document.querySelectorAll("[aria-label=\\"Chat message from user\\"]").length > {users}',
                                   timeout=timeout)
            deadline = time.monotonic() + timeout / 1000
            last, stable = None, 0
            while time.monotonic() < deadline:
                idle(timeout)
                texts = page.locator('[data-testid="stChatMessage"]').all_inner_texts()
                after_user = bool(texts) and "person" not in texts[-1][:12]
                ready = after_user and (not wait_text or wait_text in texts[-1])
                stable = stable + 1 if ready and texts == last else 0
                last = texts
                if stable >= 2:
                    return
                page.wait_for_timeout(1500)
            raise TimeoutError(f"等待回复超时：{text}")

        def exceptions():
            return page.get_by_test_id("stException").count()

        def assistant_texts():
            return page.locator('[aria-label="Chat message from assistant"]').all_inner_texts()

        page.goto(url)
        page.get_by_test_id("stChatInput").wait_for(timeout=240000)
        idle()
        check("home loads without exception", exceptions() == 0)

        if args.upgrade_check:
            history = page.locator('[class*="st-key-history-"] button').all_inner_texts()
            report["history"] = history
            check("upgraded history visible", any("升级前项目" in h for h in history), history[:6])
            page.get_by_text("升级前项目", exact=False).first.click()
            idle()
            texts = "\n".join(assistant_texts())
            check("old messages intact", "已覆盖型材材料金额" in texts, texts[:200])
            page.get_by_role("button", name="规范问答").first.click()
            idle()
            model_box = page.get_by_role("checkbox", name="AI 解读使用已配置模型")
            check("old explicit model-off preference kept", not model_box.is_checked())
            shot("upgrade-standards")
            raise SystemExit(0)

        # ---------------- 规范 AI 解读
        page.get_by_role("button", name="规范问答").first.click()
        idle()
        check("AI 解读 selected by default", page.get_by_role("radio", name="AI 解读", exact=True).is_checked())
        model_box = page.get_by_role("checkbox", name="AI 解读使用已配置模型")
        check("model enabled by default when configured", model_box.is_checked())
        check("caption says model will be called", page.get_by_text("提问时将实际调用").count() > 0 or args.no_model)
        shot("standards-default")
        started = time.time()
        say("隐框玻璃幕墙的硅酮结构密封胶有什么要求？现场能不能打注？")
        page.get_by_text("备注 · 依据出处").last.wait_for(timeout=240000) if not args.no_model else None
        answer = assistant_texts()[-1]
        report["qa_seconds"] = round(time.time() - started, 1)
        report["qa_answer"] = answer[:4000]
        if args.no_model:
            check("no-model fallback is local excerpt", "本地" in answer or "模型" in answer, answer[:200])
        else:
            check("AI answer actually called model", "实际调用" in answer, answer[:200])
            check("AI answer has summary and sections", "直接回答" in answer and "规范明确要求" in answer)
            check("citations numbered and sources generated", "[1]" in answer and "原PDF文件第" in answer and "现行状态：待核实" in answer)
        page.locator('[aria-label="Chat message from assistant"]').last.scroll_into_view_if_needed()
        shot("standards-ai-answer")
        page.get_by_text("展开原文与原PDF入口").first.click() if not args.no_model else page.locator("details summary").last.click()
        page.get_by_role("button", name="查看原始PDF第").first.click()
        page.get_by_text("原始PDF复核").wait_for(timeout=60000)
        page.locator('[role="dialog"] img').first.wait_for(timeout=60000)
        check("original PDF opens from citation", page.locator('[role="dialog"] img').count() > 0)
        shot("original-pdf")
        page.keyboard.press("Escape")
        page.locator('[role="dialog"]').wait_for(state="detached", timeout=15000)
        idle()

        # ---------------- 方案修改与自动分析
        page.get_by_role("button", name="新对话").first.click()
        idle()
        say("手工测试甲，构件式玻璃幕墙，宽18m、高6m，分格宽1.2m、高1.5m。本次数据为 synthetic 演示数据。")
        say("立柱 2.7kg/m、25 元/kg，横梁 1.3kg/m、28 元/kg，算材料金额")
        page.get_by_role("button", name="确认参数并计算").last.click()
        idle()
        say("我更看重外观通透，其次才是成本")
        say("把分格加大", "待确认，尚未生效")
        texts = assistant_texts()
        check("vague change asks for value without applying", "待确认，尚未生效" in texts[-1] and "分格宽" in texts[-1], texts[-1][:200])
        started = time.time()
        say("1.5米", "目前倾向")
        report["analysis_seconds"] = round(time.time() - started, 1)
        analysis = assistant_texts()[-1]
        report["analysis_text"] = analysis[:5000]
        for part in ("本次修改", "计算结果变化", "原方案更合适的情况", "修改方案更合适的情况", "目前倾向", "进一步决策还需要"):
            check(f"analysis contains {part}", part in analysis)
        check("analysis source shown", ("模型分析" in analysis and "实际调用" in analysis) if not args.no_model else "本地模板分析" in analysis,
              analysis[-300:])
        check("material scope kept", "不是工程总造价" in analysis)
        page.locator('[aria-label="Chat message from assistant"]').last.scroll_into_view_if_needed()
        shot("analysis")
        page.get_by_text("变更核查逐项结果与依据").last.click()
        page.wait_for_timeout(800)
        check("check table shows statuses", page.get_by_text("存在潜在冲突").count() > 0)
        page.get_by_text("变更核查逐项结果与依据").last.scroll_into_view_if_needed()
        shot("change-check")
        say("把分格宽改为1.8m", "目前倾向")
        stale = page.locator(".mw-stale").all_inner_texts()
        check("old analysis marked historical, not rewritten", any("历史分析" in s and "未按当前参数重写" in s for s in stale), stale[-3:])
        check("no exception after scheme flow", exceptions() == 0)
        shot("analysis-history")

        # ---------------- V4 界面样式保持
        bubble_bg = page.locator('[aria-label="Chat message from assistant"] [class*="st-key-answer-bubble-"]').last.evaluate(
            "e=>getComputedStyle(e).backgroundColor")
        check("answer bubble keeps light-blue style", bubble_bg not in ("rgba(0, 0, 0, 0)", "rgb(255, 255, 255)"), bubble_bg)
        border = page.locator('.st-key-conv-search [data-testid="stTextInputRootElement"]').first.evaluate("e=>getComputedStyle(e).borderWidth")
        check("search single 2px border", border == "2px", border)
        composer = page.locator('[data-testid="stChatInput"]').first.evaluate("e=>getComputedStyle(e).borderWidth")
        check("composer single 2px border", composer == "2px", composer)
        shadow = page.locator('[class*="st-key-history-current-"]').first.evaluate("e=>getComputedStyle(e).boxShadow")
        check("history selection without stripe", shadow == "none", shadow)
        check("no horizontal overflow", page.evaluate("document.documentElement.scrollWidth <= innerWidth"))

        # ---------------- 调用记录
        page.get_by_role("button", name="设置").first.click()
        page.get_by_role("tab", name="模型连接").click()
        page.get_by_text("最近的模型调用记录").click()
        page.wait_for_timeout(800)
        log = page.locator('[role="dialog"]').inner_text()
        report["call_log"] = log[-1500:]
        if not args.no_model:
            check("call log shows real calls", "规范解读 · 成功" in log and "方案分析 · 成功" in log, log[-400:])
        check("call log has no key", not any(s in log for s in secret_values))
        shot("call-log")
        page.keyboard.press("Escape")
        page.locator('[role="dialog"]').wait_for(state="detached", timeout=15000)
        idle()

        # ---------------- 关闭模型：尊重并记住
        page.get_by_role("button", name="规范问答").first.click()
        idle()
        box = page.get_by_role("checkbox", name="AI 解读使用已配置模型")
        if box.is_checked():
            page.locator('[data-testid="stCheckbox"]').filter(has_text="AI 解读使用已配置模型").locator("label").click()
            idle()
        check("model switch turned off by user", not box.is_checked())
        say("JGJ102-2003 5.5.4 有什么要求")
        note = assistant_texts()[-1]
        check("explicit off returns local excerpt", "已关闭模型" in note, note[:200])
        page.get_by_role("button", name="规范问答").first.click()
        idle()
        check("explicit off remembered for new conversation", not page.get_by_role("checkbox", name="AI 解读使用已配置模型").is_checked())
        shot("model-off")
        browser.close()
except SystemExit:
    pass
except Exception as exc:  # noqa: BLE001
    check("unexpected error", False, f"{type(exc).__name__}: {exc}")
    try:
        page.screenshot(path=str(out / f"{args.prefix}-error.png"), full_page=True)
        (out / f"{args.prefix}-error.txt").write_text(page.inner_text("body")[-6000:], encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
finally:
    try:
        browser.close()
        pw.stop()
    except Exception:  # noqa: BLE001
        pass
    child.terminate()
    try:
        child.wait(timeout=15)
    except subprocess.TimeoutExpired:
        child.kill()
    events = ""
    db = workspace / "workspace.db"
    if db.exists():
        import sqlite3
        con = sqlite3.connect(db)
        events = json.dumps(con.execute("SELECT json FROM events").fetchall(), ensure_ascii=False)
        events += json.dumps(con.execute("SELECT json FROM analyses").fetchall(), ensure_ascii=False) if \
            con.execute("SELECT name FROM sqlite_master WHERE name='analyses'").fetchone() else ""
        con.close()
    log = (workspace / "launcher.log").read_text(encoding="utf-8", errors="replace") if (workspace / "launcher.log").exists() else ""
    report["key_in_workspace_records"] = any(s in events or s in log for s in secret_values)
    check("API key not in workspace records or log", not report["key_in_workspace_records"])
    report["ok"] = all(c["ok"] for c in report["checks"])
    report["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    text = json.dumps(report, ensure_ascii=False, indent=2)
    for s in secret_values:
        text = text.replace(s, "***")
    (out / f"{args.prefix}-acceptance.json").write_text(text, encoding="utf-8")
    if not args.keep_workspace and not args.workspace:
        shutil.rmtree(temp, ignore_errors=True)
    print("OK" if report["ok"] else "FAILED", flush=True)
