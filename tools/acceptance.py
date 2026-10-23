"""v0.3 验收：运行全部自动化测试，对照改版方案 §9 的 T01–T38 生成《验收记录.md》，并实测性能。

用法：python tools/acceptance.py                   （自动化测试均在本地解析模式下运行，不访问网络）
      DEEPSEEK_API_KEY=... python tools/acceptance.py （另外实测在线理解 5 次的耗时）
"""
from __future__ import annotations

import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CASES = {
    "T01": ("全新目录启动、刷新、重启保持空白", ["test_t01_"]),
    "T02": ("新对话只查资料不要求几何价格", ["test_t02_t33"]),
    "T03": ("从零建项目，不依赖预设编号", ["test_t03_"]),
    "T04": ("两个项目交替不串数据", ["test_t04_"]),
    "T05": ("删除项目后重启不复活", ["test_t05_t06"]),
    "T06": ("恢复回收站项目，不覆盖同名新项目", ["test_t05_t06"]),
    "T07": ("彻底清理项目记录、快照、导出", ["test_t07_"]),
    "T08": ("删除项目时保留个人材料库", ["test_t08_"]),
    "T09": ("旧安装迁移选择（保留／回收站／暂不处理）", ["test_t09_"]),
    "T10": ("T10 手工算例数值由工具给出", ["test_t10_"]),
    "T11": ("1.2→1.5 比较与反向比较", ["test_t11_"]),
    "T12": ("缺单价时给出完整质量", ["test_t12_"]),
    "T13": ("只有几何时显示面积并说明缺项", ["test_t13_"]),
    "T14": ("连续更正不产生无意义冲突、可撤销", ["test_t14_"]),
    "T15": ("沿用价格只取本项目", ["test_t15_"]),
    "T16": ("整除判断由工具执行", ["test_t16_t17"]),
    "T17": ("非整除不取整，可保留真实尺寸", ["test_t16_t17"]),
    "T18": ("零、负值阻断且不写入", ["test_t18_"]),
    "T19": ("元/吨换算；含税不直接比金额", ["test_t19_"]),
    "T20": ("自定义型材名称", ["test_t20_"]),
    "T21": ("price_basis_id + profile_id 唯一定位", ["test_t21_"]),
    "T22": ("普通 XLSX 预览映射后导入、可追溯", ["test_t22_"]),
    "T23": ("缺单价的 XLSX 仍导入并可算质量", ["test_t23_"]),
    "T24": ("含义不明的列只问选择", ["test_t24_"]),
    "T25": ("中文数字与相对修改由工具运算", ["test_t25_"]),
    "T26": ("“为什么”引用真实计算步骤", ["test_t26_"]),
    "T27": ("检索用户上传的资料", ["test_t27_"]),
    "T28": ("超出范围不给伪精确结论", ["test_t28_"]),
    "T29": ("改参后旧结果标为旧版本", ["test_t29_", "test_correct_baseline"]),
    "T30": ("模型超时／非法 JSON 时保留输入并降级", ["test_t30_", "test_failures_degrade"]),
    "T31": ("演示规则逐项执行", ["test_t31_"]),
    "T32": ("零规则／零正式条款不显示为通过", ["test_t32_", "test_t31_t32"]),
    "T33": ("只做规范查询不要求价格", ["test_t02_t33"]),
    "T34": ("缺价格仍可执行规则核查", ["test_t34_"]),
    "T35": ("正式条件明确不满足的状态", ["test_t22_formal_condition_not_met"]),
    "T36": ("逐字段来源、不自动升级 verified", ["test_t36_"]),
    "T37": ("结果追溯到版本、来源、工具与公式", ["test_t37_"]),
    "T38": ("导出 Excel 比选表与技术追溯包", ["test_t38_"]),
}


def run_pytest() -> dict:
    xml = Path(tempfile.mkdtemp()) / "junit.xml"
    subprocess.run([sys.executable, "-m", "pytest", "-o", "addopts=", "-q", f"--junitxml={xml}"], cwd=ROOT,
                   env=dict(os.environ, MW_FORCE_FALLBACK="1"))
    out = {}
    for tc in ET.parse(xml).getroot().iter("testcase"):
        failed = tc.find("failure") is not None or tc.find("error") is not None
        out[f"{tc.get('classname')}::{tc.get('name')}"] = "failed" if failed else "passed"
    return out


def perf_local(n: int = 10) -> list[float]:
    os.environ["MW_FORCE_FALLBACK"] = "1"
    from tests.chat_helpers import Chat
    times = []
    for _ in range(n):
        c = Chat(tempfile.mkdtemp()).build_t10()
        t = time.perf_counter()
        c.say("把分格宽改为1.5m，与原方案比较")
        times.append(time.perf_counter() - t)
    return times


def perf_model(n: int = 5):
    if not os.environ.get("DEEPSEEK_API_KEY"):
        return None
    os.environ.pop("MW_FORCE_FALLBACK", None)
    import importlib

    from agent import extraction
    importlib.reload(extraction)
    times, ok = [], 0
    for _ in range(n):
        t = time.perf_counter()
        r = extraction.understand("把分格宽改为1.5m，其他不变，与原方案比较", {"last_field": "grid_width_m"})
        times.append(time.perf_counter() - t)
        ok += r["model_mode"] == "online"
    return times, ok, extraction.model_label()


def main() -> None:
    started = datetime.now(timezone.utc).astimezone()
    res = run_pytest()
    local = perf_local()
    model = perf_model()
    passed = sum(1 for v in res.values() if v == "passed")
    rows = []
    for tid, (title, pats) in CASES.items():
        hits = {k: v for k, v in res.items() if any(p in k for p in pats)}
        if not hits:
            rows.append((tid, title, "未执行", "无对应自动化测试"))
            continue
        fails = [k.split("::")[-1] for k, v in hits.items() if v != "passed"]
        rows.append((tid, title, "通过" if not fails else "未通过",
                     f"{len(hits) - len(fails)}/{len(hits)} 项测试通过" + (f"；失败：{', '.join(fails)}" if fails else "")))
    L = ["# v0.3 验收记录（自动生成）", "",
         f"- 生成时间：{started.isoformat(timespec='seconds')}",
         f"- 环境：{platform.platform()}；Python {platform.python_version()}",
         "- 依据：《幕墙智能体：聊天式交互与空白启动改版方案 v0.3》§9、§12",
         "- 自动化测试在本地解析模式下运行（不访问网络）；每个测试使用独立的空白工作区，不使用演示包或预设种子",
         f"- 自动化测试：{passed}/{len(res)} 通过", "",
         "| 用例 | 场景 | 结论 | 实际结果 |", "|---|---|---|---|"]
    L += [f"| {a} | {b} | {c} | {d} |" for a, b, c, d in rows]
    L += ["", "## 性能实测（只代表本机、本数据规模和当时网络）", "",
          f"- 本地解析 + 确定性计算（一轮“改参并比较”，10 次）：最大 {max(local):.3f}s，中位 {statistics.median(local):.3f}s"]
    if model:
        times, ok, label = model
        L.append(f"- 在线理解（{label}，5 次）：最大 {max(times):.2f}s，中位 {statistics.median(times):.2f}s，在线成功 {ok}/5")
    else:
        L.append("- 在线理解：未执行（未设置 DEEPSEEK_API_KEY）")
    L += ["", "## 说明", "",
          "- T10/T11 等参考值来自 tests/fixtures/manual_expected.json（按 §9.5 公式手算录入），被测代码不读取。",
          "- 改版方案 T11 写“相对 T10 增加 14.22%”，独立复算为 1215/8541 = 14.2255…%，按 ROUND_HALF_UP 应为 14.23%。",
          "- 页面层（首页空白、单一输入框、结果卡、详情弹窗、左栏删除恢复、1366×768 无横向滚动）另做了浏览器走查，不在本表自动化范围内。",
          "- §12 要求至少一名未参与实现的人员从空白工作区完成 T10 主算例，此项需由团队另行完成并记录。",
          "- 全部通过只证明空白建案、对话流程、确定性计算、追溯与删除符合设计，不证明真实工程适用性、正式规范覆盖或结构安全。"]
    out = ROOT / "验收记录.md"
    out.write_text("\n".join(L), encoding="utf-8")
    print(f"tests {passed}/{len(res)}; local_max={max(local):.3f}s; model={'n/a' if not model else f'{max(model[0]):.2f}s'}")


if __name__ == "__main__":
    main()
