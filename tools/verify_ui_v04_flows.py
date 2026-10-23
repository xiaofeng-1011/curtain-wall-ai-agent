"""扩展 UI 验收，仅允许向系统临时目录中的隔离工作区写入测试数据。"""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["MW_FORCE_FALLBACK"] = "1"

from playwright.sync_api import sync_playwright, expect
from tests.chat_helpers import Chat


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--url", required=True)
    args = parser.parse_args()
    workspace = Path(args.workspace).resolve()
    if not workspace.is_relative_to(Path(tempfile.gettempdir()).resolve()):
        raise ValueError("验收数据只能写入系统临时目录")
    out = Path("evidence/ui-v04")
    out.mkdir(parents=True, exist_ok=True)
    history = Chat(workspace).new()
    unique_query = "首批之外的唯一检索目标" + str(len(history.ws.list_conversations()))
    history.say(unique_query)
    oldest_title = history.conv["title"]
    for i in range(102):
        c = history.a.new_conversation()
        c["title"] = f"验收历史 {i:03d} · 中文 English 立柱横梁方案"
        c["messages"].append(history.a._msg(c, "user", f"隔离测试消息 {i}"))
        history.ws.save_conversation(c, c["rev"])
    calc = Chat(workspace).build_t10()
    calc.say("把分格宽改为1.5m，与原方案比较")
    cid = calc.conv["conversation_id"]
    baseline_name = calc.project()["name"]
    facts = {"history_count": len(history.ws.list_conversations()), "checks": []}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width":1440,"height":900})
        page.goto(args.url + "?c=" + cid, wait_until="networkidle")
        expect(page.locator(".mw-result-summary").last).to_contain_text("1,215.00")
        page.locator(".mw-result-summary").last.scroll_into_view_if_needed()
        page.screenshot(path=str(out / "comparison-1440.png"))
        facts["checks"].append("比较结果显示 1,215.00 元变化，未改变计算内核")
        search = page.get_by_role("textbox", name="搜索对话", exact=True)
        search.fill(unique_query)
        search.press("Enter")
        expect(page.locator('.st-key-sidebar-history [class*="st-key-cv-"] button:visible')).to_have_count(1)
        expect(page.locator('.st-key-sidebar-history [class*="st-key-cv-"] button:visible')).to_contain_text(oldest_title)
        facts["checks"].append("可搜索首批 40 条之外的历史，底部工具仍可达")
        page.screenshot(path=str(out / "history-search.png"))
        search.fill("")
        search.press("Enter")
        menu = page.locator('[class*="st-key-history-current-"] [data-testid="stPopoverButton"]:visible')
        menu.click()
        expect(page.get_by_role("textbox", name="重命名对话", exact=True)).to_be_visible()
        assert "c=" + cid in page.url
        special = "# 中文 <特殊> [标题] & English"
        page.get_by_role("textbox", name="重命名对话", exact=True).fill(special)
        page.get_by_role("button", name="保存名称", exact=True).click()
        expect(page.locator(".mw-title")).to_have_text(special)
        assert calc.ws.get_project(calc.project()["project_id"])["name"] == baseline_name
        expect(page).to_have_title(special + " · 幕墙方案助手")
        facts["checks"].append("菜单不切换对话；特殊标题按纯文本显示，改名不改项目")
        page.get_by_role("button", name="方案参数").click()
        expect(page.get_by_role("dialog")).to_be_visible()
        page.get_by_role("tab", name="原方案", exact=True).click()
        page.get_by_role("textbox", name="幕墙总宽", exact=True).first.fill("24")
        page.get_by_role("button", name="保存参数", exact=True).first.click()
        expect(page.locator(".mw-subtitle")).to_contain_text("参数已修改，结果待更新")
        expect(page.get_by_role("button", name="按当前参数重新计算").first).to_be_visible()
        expect(page.get_by_role("button", name="下载此历史版本").first).to_be_visible()
        page.locator(".mw-stale").last.scroll_into_view_if_needed()
        page.screenshot(path=str(out / "historical-result.png"))
        facts["checks"].append("参数保存后历史卡保持旧值，顶部及下载均标历史状态")
        page.locator('.st-key-nav-settings button').click()
        expect(page.get_by_role("dialog")).to_be_visible()
        page.get_by_role("switch", name="降低透明度", exact=True).focus()
        page.get_by_role("switch", name="降低透明度", exact=True).press("Space")
        expect(page.get_by_role("switch", name="降低透明度", exact=True)).to_be_checked()
        page.get_by_role("button", name="应用阅读偏好", exact=True).click()
        expect(page.locator('.st-key-conversation-header')).to_have_css("backdrop-filter", "none")
        page.locator('.st-key-nav-settings button').click()
        expect(page.get_by_role("switch", name="降低透明度", exact=True)).to_be_checked()
        page.screenshot(path=str(out / "settings-1440.png"))
        facts["checks"].append("降低透明度生效，关闭并重开设置后偏好仍保留")
        page.keyboard.press("Escape")
        for zoom in (1, 1.25, 1.5, 2):
            page.evaluate("scale => document.documentElement.style.zoom = scale", str(zoom))
            page.locator('.mw-title').scroll_into_view_if_needed()
            bounds = page.locator('.mw-title').bounding_box()
            facts.setdefault("css_zoom_layout_checks", []).append({"scale":zoom,"title":bounds,
                "horizontal_overflow": page.evaluate("document.documentElement.scrollWidth > innerWidth")})
        page.evaluate("document.documentElement.style.zoom = '1'")
        assert not page.locator('[data-testid="stException"]').count()
        facts["browser"] = browser.version
        browser.close()
    (out / "flow-checks.json").write_text(json.dumps(facts,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(facts,ensure_ascii=False))


if __name__ == "__main__":
    main()
