"""本地视觉验收：仅连接指定的隔离测试服务，保存截图与视口测量。"""
from pathlib import Path
import json
import argparse

from playwright.sync_api import sync_playwright, expect


def main():
    args = argparse.ArgumentParser()
    args.add_argument("--url", default="http://127.0.0.1:8514")
    args.add_argument("--out", default="evidence/ui-v04")
    opts = args.parse_args()
    out = Path(opts.out)
    out.mkdir(parents=True, exist_ok=True)
    findings = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
        page.goto(opts.url, wait_until="networkidle")
        expect(page.get_by_role("button", name="录入方案")).to_be_visible()
        expect(page.get_by_role("button", name="空白模板")).to_be_visible()
        expect(page.get_by_role("button", name="Stop", exact=True)).not_to_be_visible()
        page.screenshot(path=str(out / "home-1440.png"), full_page=True)
        page.locator('[data-testid="stChatInputTextArea"]').fill("玻璃幕墙立柱截面厚度要求")
        page.locator('[data-testid="stChatInputTextArea"]').press("Enter")
        expect(page.get_by_role("button", name="上传资料", exact=True)).to_be_visible(timeout=20000)
        expect(page.locator(".mw-title")).to_have_text("玻璃幕墙立柱截面厚度要求")
        expect(page).to_have_title("玻璃幕墙立柱截面厚度要求 · 幕墙方案助手")
        page.screenshot(path=str(out / "query-1440.png"), full_page=True)
        for width, height in ((1366,768),(1440,900),(1920,1080),(768,1024),(390,844)):
            page.set_viewport_size({"width":width,"height":height})
            if width < 600:
                sidebar = page.locator('[data-testid="stSidebar"]')
                if sidebar.get_attribute("aria-expanded") == "true":
                    page.locator('[data-testid="stSidebarCollapseButton"] button').click()
                expect(sidebar).to_have_attribute("aria-expanded", "false")
            page.screenshot(path=str(out / f"query-{width}.png"),full_page=True)
            findings.append({"viewport":[width,height],"horizontal_overflow":page.evaluate("document.documentElement.scrollWidth > innerWidth"),
                             "title":page.locator('.mw-title').bounding_box()})
        page.set_viewport_size({"width":1440,"height":900})
        page.get_by_role("button",name="上传资料",exact=True).click()
        expect(page.get_by_role("dialog")).to_be_visible()
        page.screenshot(path=str(out / "documents-1440.png"),full_page=True)
        page.keyboard.press("Escape")
        page.goto(opts.url, wait_until="networkidle")
        page.get_by_role("button",name="录入方案").click()
        expect(page.get_by_role("dialog")).to_be_visible()
        expect(page.get_by_role("button", name="保存参数", exact=True)).to_be_visible()
        page.screenshot(path=str(out / "parameters-1440.png"),full_page=True)
        findings.append({"browser":browser.version,"exception_count":page.locator('[data-testid="stException"]').count()})
        browser.close()
    (out / "browser-checks.json").write_text(json.dumps(findings,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(findings,ensure_ascii=False))


if __name__ == "__main__":
    main()
