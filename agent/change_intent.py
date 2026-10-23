"""理解方案修改意图：“把分格加大”“减少立柱”“换一种型材”等没有给出数值的修改。

只识别意图、说明需要补充的关键条件和可能影响；不补造任何工程参数、截面性能或项目条件。
待确认的修改不写入方案，与已生效参数分开显示；用户给出数值后才走原有的参数确认和计算流程。
"""
from __future__ import annotations

import json
import re

from domain.contracts import dec
from domain.fields import USER_FIELDS
from domain.units import parse_quantity

from . import projects as P

UP = r"加大|增大|放大|变大|大一点|大一些|调大|加宽|加高|放宽"
DOWN = r"加密|减小|缩小|变小|小一点|小一些|调小|改小|变窄"

IMPACTS = {
    "grid_up": {"成本": "立柱或横梁数量减少，理论型材用量和材料金额通常下降（以重新计算为准）；玻璃单块变大，玻璃及加工费用未计入本软件。",
                "构造": "单根立柱或横梁受荷宽度增大，玻璃面板变大，可能需要更大截面或更厚的玻璃。",
                "施工": "板块变大后加工、运输、吊装难度增加，需核对原片和设备限制。",
                "维护": "单块玻璃更大，日后更换成本和难度更高。",
                "规范核查": "需复核玻璃强度与挠度、立柱与横梁强度和挠度、连接承载力。"},
    "grid_down": {"成本": "构件数量增加，型材用量和材料金额通常上升（以重新计算为准）。",
                  "构造": "单根构件受荷减小，但节点数量增多。",
                  "施工": "安装工作量和节点数量增加。",
                  "维护": "板块较小，单块更换较容易，但密封胶缝更长。",
                  "规范核查": "需按新分格复核面板和构件验算，以及立面分格设计要求。"},
    "material": {"成本": "需要新型材的线重和未税单价才能重新计算材料金额。",
                 "构造": "截面形式、壁厚、材质会影响承载力和连接构造；线重不能代表截面性能。",
                 "施工": "需确认配套连接件、加工和供货条件。",
                 "维护": "需确认表面处理、耐久性和配件通用性。",
                 "规范核查": "需提供截面参数和材质牌号，复核型材壁厚等构造要求和强度、挠度。"},
}


def _role(project: dict) -> str:
    return "candidate" if P.has_candidate(project) else "baseline"


def _current(project, role, field):
    v, unit = P.user_value(project, role, field)
    return "未填写" if v is None else f"{P._fmt(v)} {unit or ''}".strip()


def detect(text: str) -> dict | None:
    t = re.sub(r"\s+", "", text or "")
    comp = "column" if "立柱" in t and "横梁" not in t else "beam" if "横梁" in t and "立柱" not in t else None
    if re.search(r"(换|更换|改用|换成|替换|改成)(一种|一下|别的|其他|另一种|更轻的|更便宜的|新的|另外的)?(铝)?(型材|规格|截面|材料)", t) \
            or re.search(r"(立柱|横梁)(型材|规格|截面)?.{0,3}(换|更换|改用|替换)", t):
        fields = [f"{c}_linear_mass" for c in ([comp] if comp else ["column", "beam"])] + \
                 [f"{c}_unit_price" for c in ([comp] if comp else ["column", "beam"])]
        return {"id": "material", "label": f"更换{P.COMP_NAME.get(comp, '立柱、横梁')}型材", "fields": fields,
                "impact": "material", "component": comp}
    if re.search(rf"(减少|少用|去掉|精简|少几根|少一些)立柱|立柱.{{0,4}}(减少|少一些|少几根|少一点)", t):
        return {"id": "fewer_columns", "label": "减少立柱（加大立柱间距，即加大分格宽）", "fields": ["grid_width_m"],
                "impact": "grid_up", "direction": "up"}
    if re.search(rf"(增加|多加|加密)立柱|立柱.{{0,4}}(增加|多一些|加密)", t):
        return {"id": "more_columns", "label": "增加立柱（减小分格宽）", "fields": ["grid_width_m"], "impact": "grid_down",
                "direction": "down"}
    if re.search(rf"(减少|少用|去掉|精简)横梁|横梁.{{0,4}}(减少|少一些|少几道)", t):
        return {"id": "fewer_beams", "label": "减少横梁（加大分格高）", "fields": ["grid_height_m"], "impact": "grid_up",
                "direction": "up"}
    m = re.search(rf"(分格|板块|格子)(宽|高|宽度|高度|尺寸)?.{{0,4}}({UP}|{DOWN})", t)
    if m and not re.search(r"\d", t):
        up = bool(re.search(UP, m.group(3)))
        which = m.group(2) or ""
        fields = ["grid_height_m"] if which.startswith("高") else ["grid_width_m"] if which.startswith("宽") else ["grid_width_m"]
        word = "加大" if up else "减小"
        label = f"{word}分格{'高' if fields == ['grid_height_m'] else '宽'}" + ("（只说“分格”时按分格宽理解）" if not which else "")
        return {"id": "grid_" + ("up" if up else "down"), "label": label, "fields": fields,
                "impact": "grid_up" if up else "grid_down", "direction": "up" if up else "down"}
    return None


def contradiction(text: str, project: dict, role: str, patches: list[dict]) -> list[str]:
    """用户说“加大”却给了更小的值（或相反）时提醒确认。"""
    t = re.sub(r"\s+", "", text or "")
    up, down = bool(re.search(UP + r"|增加到|加到", t)), bool(re.search(DOWN + r"|减少到|减到", t))
    if up == down:
        return []
    out = []
    for x in patches:
        if x.get("op") != "set" or not x["field"].endswith(("_m", "linear_mass")):
            continue
        old, _ = P.user_value(project, role, x["field"])
        try:
            new, _, issues = parse_quantity(x.get("value"), x.get("unit"), "length" if x["field"].endswith("_m") else "linear_mass", x["field"])
        except (ValueError, TypeError):
            continue
        if issues or old is None or new is None:
            continue
        if (up and new < dec(old)) or (down and new > dec(old)):
            out.append(f"你说“{'加大' if up else '减小'}”，但{USER_FIELDS[x['field']]['label']} {P._fmt(old)} → {P._fmt(new)} "
                       f"实际是{'减小' if up else '加大'}，请确认数值是否正确（可点“撤销”）。")
    return out


ADVISOR_SCHEMA = {"type": "object", "properties": {
    "understanding": {"type": "string"},
    "questions": {"type": "array", "items": {"type": "string"}},
    "impacts": {"type": "object", "properties": {k: {"type": "string"} for k in ("成本", "构造", "施工", "维护", "规范核查")},
                "required": ["成本", "构造", "施工", "维护", "规范核查"], "additionalProperties": False},
    "contradictions": {"type": "array", "items": {"type": "string"}}},
    "required": ["understanding", "questions", "impacts", "contradictions"], "additionalProperties": False}
ADVISOR_SYSTEM = """你是幕墙方案修改助手。用户提出了没有给出具体数值的方案修改。根据当前项目和两个方案的参数：
1. understanding：用一句话复述你理解的修改意图，以及它对应哪个参数（用中文参数名，不写英文字段名）。
2. questions：只提出真正影响当前判断的关键追问（最多2个），例如新的分格宽是多少、新型材的线重和单价；
   追问要写明当前值，便于用户回答。
3. impacts：分别用一句话说明对成本、构造、施工、维护、规范核查的可能影响，说明是“可能”而非结论。
4. contradictions：指出用户说法与现有参数之间的矛盾（没有就为空）。
不要给出或猜测任何新的参数数值、截面性能或项目条件；不要计算金额；不要判断合规或结构安全。只返回 JSON。"""


def advise(text: str, project: dict, intent: dict, use_model: bool) -> dict:
    """返回 {understanding, questions, impacts, contradictions, source, model_record}。"""
    from agent import extraction
    from agent.claim_check import _num_set, check_text
    role = _role(project)
    current = {USER_FIELDS[f]["label"]: _current(project, role, f) for f in intent["fields"]}
    question = "；".join(f"{label}改为多少？（当前{P.ROLE_NAME[role]}：{value}）" for label, value in current.items())
    if intent["id"] == "material":
        question = "新型材的名称、线重（kg/m）和未税单价是多少？如需规范核查，还需截面壁厚和材质牌号。"
    base = {"understanding": f"我理解你想{intent['label']}。", "questions": [question],
            "impacts": dict(IMPACTS[intent["impact"]]), "contradictions": [], "source": "template",
            "model_record": {"attempted": False, "reason": "未配置模型或已关闭，使用本地规则"}}
    if not use_model:
        return base
    context = {"user_text": text, "intent": intent["label"], "fields": intent["fields"],
               "schemes": {P.ROLE_NAME[r]: dict(P.summary_rows(project, r)) for r in project["schemes"]}}
    raw, record = extraction.call_with_record("change_advice", ADVISOR_SYSTEM, json.dumps(context, ensure_ascii=False),
                                              ADVISOR_SCHEMA, max_tokens=1500, timeout=extraction.MODEL_TIMEOUT_S * 2)
    base["model_record"] = record
    if raw is None:
        return base
    allowed = _num_set(json.dumps(context, ensure_ascii=False))

    def ok(s):
        return not check_text(s, allowed_numbers=allowed, allowed_codes=set())["problems"]

    def plain(s):
        # 模型偶尔带出内部字段名；显示时换成中文参数名。
        for f, d in USER_FIELDS.items():
            s = re.sub(rf"[（(]?{f}[)）]?", d["label"] if d["label"] not in s else "", s)
        return s

    raw = {k: ([plain(x) for x in v] if isinstance(v, list) else plain(v) if isinstance(v, str) else v) for k, v in raw.items()}
    out = dict(base, source="model")
    if ok(raw["understanding"]):
        out["understanding"] = raw["understanding"]
    qs = [q for q in raw["questions"][:2] if ok(q)]
    out["questions"] = qs or base["questions"]
    # 影响说明沿用经整理的专业要点；模型负责理解意图、追问和指出矛盾。
    out["impacts"] = base["impacts"]
    out["contradictions"] = [c for c in raw["contradictions"][:3] if ok(c)]
    return out


def render(advice: dict, intent: dict) -> str:
    lines = [f"**修改理解（待确认，尚未生效）**：{advice['understanding']}"]
    lines.append("\n**需要你确认**：\n" + "\n".join(f"- {q}" for q in advice["questions"]))
    if advice.get("contradictions"):
        lines.append("\n**注意**：\n" + "\n".join(f"- {c}" for c in advice["contradictions"]))
    lines.append("\n**可能的影响**（给出数值并重新计算后，会自动生成两个方案的对比分析和规范核查）：\n"
                 + "\n".join(f"- {k}：{v}" for k, v in advice["impacts"].items()))
    return "\n".join(lines)
