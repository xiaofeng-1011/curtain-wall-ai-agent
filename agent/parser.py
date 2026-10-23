"""有限的本地语言解析（模型不可用时的降级路径，也用于“确认／跳过”等短回复）。

只做“说了什么”的识别：字段、数值、原始单位、操作（设置／清空／增加／减少）、意图、指代对象与来源声明；
不做单位换算（交给 domain.units）、不做任何计算。
"""
from __future__ import annotations

import re

from domain.fields import USER_FIELDS
from domain.units import normalize_text

DIM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(mm|毫米|cm|厘米|m|米)?\s*[x*乘]\s*(\d+(?:\.\d+)?)\s*(mm|毫米|cm|厘米|m|米)?")


def _compatible(field: str | None, kind: str | None) -> bool:
    if not field or field not in USER_FIELDS:
        return False
    return kind is None or USER_FIELDS[field]["kind"] == kind


def _expand_dims(c: str) -> str:
    """“18x6米”→“宽18米、高6米”（分格语境下为分格宽／高）。"""
    def rep(m):
        u1, u2 = m.group(2) or m.group(4) or "", m.group(4) or m.group(2) or ""
        return f"宽{m.group(1)}{u1}、高{m.group(3)}{u2}"
    return DIM_RE.sub(rep, c)

# 超出范围（固定说明模板见 SCOPE_TEXT）
SCOPE_PATTERNS = [
    (r"单元式", "单元式幕墙"),
    (r"石材幕墙|石材", "石材幕墙"),
    (r"金属板幕墙|铝板幕墙", "金属板幕墙"),
    (r"玻璃厚度|玻璃选型|玻璃.*(推荐|选多厚|多厚)", "玻璃厚度推荐"),
    (r"结构(是否)?安全|安全吗|承载力|挠度|结构验算|结构计算|强度验算|连接.*验算|锚固设计|安全验算", "完整结构计算"),
    (r"风荷载(计算|取值|多少)", "风荷载计算"),
    (r"施工图审查|签审|审图|安全鉴定", "施工图审查／签审"),
    (r"\bCAD\b|BIM|dwg|图纸识别|读取图纸|识别图纸", "CAD/BIM 识别"),
    (r"曲面|(?<!没有)(?<!无)(?<!不含)(?<!不带)(?<!没)洞口|转角|非等距|异形", "复杂立面"),
    (r"玻璃(成本|价格|单价|用量)|工程总价|总造价|安装费|安装造价", "玻璃或完整造价"),
]
SCOPE_TEXT = {
    "单元式幕墙": "当前版本只覆盖构件式玻璃幕墙的矩形等距立面。单元式幕墙的构件和计量方式未实现，不能套用当前模型给出用量或成本结论。我可以先记录你的需求和已有参数，具体计量模型需由工程师确定。",
    "完整结构计算": "当前版本不进行承载力、挠度、连接或锚固验算，不能判断结构是否安全，也不会给出估算数字。我可以整理已有参数和待确认事项，验算需要专业工程师使用经验证的工具完成。",
    "风荷载计算": "当前版本不进行风荷载计算，型材用量计算也不使用风荷载。需要时请由专业工程师使用经验证的工具完成。",
    "玻璃厚度推荐": "当前版本不推荐玻璃厚度，也不计算玻璃成本；玻璃选型涉及安全判断，需要专业工程师确认。",
    "CAD/BIM 识别": "当前版本不读取 CAD/BIM 图纸。可以把立面尺寸、分格和材料信息直接告诉我，或上传简单的 Excel 参数表。",
    "复杂立面": "当前版本只支持单个矩形、等距分格、无洞口的立面，不能套用当前模型计算洞口、转角、曲面或非等距分格。可以保存已有资料，交由工程师处理。",
    "玻璃或完整造价": "当前版本只计算立柱、横梁的理论材料金额，不包含玻璃、五金、加工、损耗、运输、安装或税费，不能给出工程总价。",
}
DEFAULT_SCOPE_TEXT = "这超出了当前版本的覆盖范围（矩形等距构件式玻璃幕墙、立柱横梁理论用量与材料金额）。当前没有可靠依据，需人工核实。"

NUM = r"[-+]?\d+(?:\.\d+)?"
UNIT = r"mm|毫米|cm|厘米|m²|m2|平方米|㎡|kg/m|元/kg|元/吨|元/t|元|m(?![a-z])|米|%"
QTY_RE = re.compile(rf"({NUM})\s*({UNIT})?", re.IGNORECASE)
SET_WORDS = r"(改成|改为|改到|调整为|调整到|调到|变为|变成|增加到|增大到|减少到|减小到|减到|加到|设为|设置为|定为|应为|应该是|为|是|=|:|到|取)"
CLAUSE_SPLIT = re.compile(r"[，,；;。！!？?\n]|并且|然后|另外|同时")
COMP_RE = re.compile(r"立柱|横梁")
NAME_RE = re.compile(r"(立柱|横梁)(?:型材)?(?:叫做?|名称(?:为|是)?|名叫|命名为|型号(?:为|是)?)\s*[“\"「]?([^\s，,；;。、\"”」\d]{1,16})")
PROJECT_NAME_RES = [
    re.compile(r"(?:新建|创建|建立?|做)(?:一个|个|一份)?(?:名为|叫做?|叫)?[“\"「]?([^，,。；;“”\"」\d]{1,20}?)[”\"」]?(案例|项目)"),
    re.compile(r"项目(?:名称?|名字)(?:叫|为|是)?[“\"「]?([^，,。；;“”\"」]{1,20})[”\"」]?"),
]
RENAME_RE = re.compile(r"(?:项目|这个项目|对话)?(?:改名|重命名|名称改)(?:为|成)?\s*[“\"「]?([^，,。；;“”\"」]{1,20})")


def _comps_in(s: str) -> list[str]:
    out = []
    if "立柱" in s or "柱" in s and "柱梁" in s:
        out.append("column")
    if "横梁" in s or "柱梁" in s:
        out.append("beam")
    return out


def classify_scope(text: str) -> list[str]:
    t = normalize_text(text)
    return list(dict.fromkeys(label for pat, label in SCOPE_PATTERNS if re.search(pat, t, re.IGNORECASE)))


def scope_message(hints: list[str]) -> str:
    msgs = [SCOPE_TEXT.get(h, DEFAULT_SCOPE_TEXT) for h in hints] or [DEFAULT_SCOPE_TEXT]
    return "\n\n".join(dict.fromkeys(msgs))


def is_safety_request(text: str) -> bool:
    return bool(re.search(r"安全吗|是否安全|是否满足|能否满足|是否合格|能不能用|是否可用|帮我验算|验算我的|进行.*验算|判断.*(安全|满足)|算一下.*(承载|挠度|强度)", text))


def is_reference_query(text: str) -> bool:
    return bool(re.search(r"规范|条文|条款|资料|规定|手册|厚度要求|截面.{0,8}要求|壁厚.{0,8}要求", text)) and not is_safety_request(text)


def _intents(t: str, has_patches: bool) -> tuple[set, str | None]:
    it = set()
    task = None
    if re.search(r"为什么|为何|原因|怎么算|如何算|怎么来的|怎么得|解释一下|说明一下", t):
        it.add("explain")
    if re.search(r"导出|下载|比选表|报表|excel|xlsx|报告", t, re.IGNORECASE):
        it.add("export")
    if re.search(r"撤销|撤回|回退|改回来|恢复上一步|还原上一步|撤消", t):
        it.add("undo")
    if re.search(r"删除(?:这个|本|当前|该)?项目|项目.{0,4}删(掉|除)", t):
        it.add("delete_project")
    elif re.search(r"删除(?:这个|本|当前|该)?(对话|会话)", t):
        it.add("delete_conversation")
    if re.search(r"清空(?:工作区|所有|全部)|全部清除|彻底清理", t):
        it.add("clear_workspace")
    if re.search(r"你能做什么|怎么用|如何使用|帮助|使用说明|有哪些功能", t):
        it.add("help")
    if re.search(r"规范|条文|条款|标准|资料|依据|规定|文献|手册|(有什么|有哪些|什么)要求|要求是什么|如何规定|怎么规定|"
                 r"是否允许|应符合|限值|厚度要求|截面.{0,8}要求|壁厚.{0,8}要求", t) and not has_patches and not re.search(r"(算|计算|比较|对比)", t):
        it.add("query")
    if re.search(r"(查|找|搜|检索)(一下|找|询)?.{0,6}(资料|规范|条文|标准|依据)", t):
        it.add("query")
    if re.search(r"算|计算|求出|求解|比较|对比|比一下|多少(?!种)|看看|看一下|出结果|核算|核查|检查|评估", t):
        it.add("compute")
    if re.search(r"比较|对比|差别|差异|相比|比一下|差多少|区别|与原方案|跟原方案|和原方案|比原方案", t):
        task = "compare"
    elif re.search(r"金额|成本|造价|费用|多少钱|材料价|价格是多少|算.{0,4}钱", t):
        task = "cost"
    elif re.search(r"质量|重量|多重|用量|型材量", t) and not re.search(r"每米多重", t):
        task = "mass"
    elif re.search(r"长度|多长|轴线", t):
        task = "length"
    elif re.search(r"面积", t) and re.search(r"算|计算|多少|求", t):
        task = "area"
    elif re.search(r"核查|规则|检查", t) and "compute" in it:
        task = "rule_check"
    return it, task


def _target(t: str) -> str | None:
    if re.search(r"(修正|更正|改|调整|修改)(一下)?原方案|原方案.{0,10}(填错|写错|错了|有误|应该是|应为|改成|改为|改一下|改)|"
                 r"(基准|调整前|原来的方案|旧方案).{0,8}(改|错|应为|应该是)", t):
        return "baseline"
    if re.search(r"调整方案|候选方案|新方案|调整后|另一个方案|对比方案", t):
        return "candidate"
    return None


def _source(t: str) -> str | None:
    if re.search(r"真实|实际项目|实际工程|现场实测|厂家报价|项目实测|真实项目", t):
        return "real"
    if re.search(r"测试|演示|虚构|编造|构造|假设|虚拟|示例|synthetic|模拟|假的|随便编|随便填", t, re.IGNORECASE):
        return "synthetic"
    return None


def _tax(t: str) -> str | None:
    if re.search(r"不含税|未税|税前|除税", t):
        return "exclusive"
    if re.search(r"含税", t):
        return "inclusive"
    return None


def _length_field(seg: str, grid_ctx: bool, last_kind: str | None) -> str | None:
    if "层高" in seg:
        return "floor_height_m"
    if re.search(r"申报面积|立面面积|面积", seg):
        return "reported_area_m2"
    is_grid = grid_ctx or re.search(r"分格|板块", seg)
    if re.search(r"宽", seg):
        return "grid_width_m" if is_grid else "width_m"
    if re.search(r"高", seg):
        return "grid_height_m" if is_grid else "height_m"
    if re.search(r"分格|板块", seg):
        return "grid_width_m"  # 只说“分格”时按分格宽理解，并在回显中写明
    return None


def parse(text: str, *, active_field: str | None = None, last_field: str | None = None) -> dict:
    raw = text
    t = normalize_text(text).strip()
    out = {"intents": set(), "task": None, "target": _target(t), "patches": [], "project_name": None,
           "source_decl": _source(t), "tax": _tax(t), "reuse_price": [], "skip": False, "confirm": False,
           "query_text": None, "rename_to": None, "scope_hints": classify_scope(t), "ambiguous": [],
           "create_project": False, "notes": []}
    short = re.sub(r"[。！!.\s]", "", t)
    if re.fullmatch(r"(确认|确定|没问题|可以|好的?|对的?|是的?|ok|OK|开始计算|确认并计算|继续|行)", short):
        out["confirm"] = True
        return _finish(out)
    if re.fullmatch(r"(不知道|不清楚|跳过|暂时没有|先不填|稍后|以后再说|没有|不确定|暂缺|先跳过)", short):
        out["skip"] = True
        return _finish(out)
    m = RENAME_RE.search(t)
    if m:
        out["rename_to"] = m.group(1).strip()
        out["intents"].add("rename")
    for rx in PROJECT_NAME_RES:
        m = rx.search(t)
        if m:
            name = m.group(1).strip()
            if len(m.groups()) > 1 and m.group(2) and not name.endswith(m.group(2)):
                name += m.group(2)
            if name and not re.fullmatch(r"(新|一个|个)?(方案|案例|项目)?", name):
                out["project_name"] = name
            out["create_project"] = True
            break
    if re.search(r"(新建|创建|建立?|做)(一个|个)?(新)?(方案|项目|案例)", t):
        out["create_project"] = True

    clauses = [c for c in CLAUSE_SPLIT.split(t) if c.strip()]
    # 首句无数字、像名称（如“手工测试甲，构件式玻璃幕墙，宽18m……”）
    if not out["project_name"] and len(clauses) >= 2 and not re.search(r"\d", clauses[0]) and len(clauses[0]) <= 16 \
            and re.search(r"\d", t) and not re.search(
            r"幕墙|构件|玻璃|分格|宽|高|立柱|横梁|算|比较|改|把|请|帮|方案$|我|你|查|资料|规范|沿用|其他", clauses[0]):
        out["project_name"] = clauses[0].strip()
        out["create_project"] = True

    comp_ctx: list[str] = []
    pending_field: str | None = None
    n_clauses = len(clauses)
    for clause in clauses:
        c = _expand_dims(clause.strip())
        comps_here = _comps_in(c)
        if comps_here:
            comp_ctx = comps_here
        grid_ctx = bool(re.search(r"分格|板块", c))
        # 名称
        for nm in NAME_RE.finditer(c):
            comp = "column" if nm.group(1) == "立柱" else "beam"
            out["patches"].append(_p(f"{comp}_name", "set", nm.group(2), None, c))
        # 沿用价格
        if re.search(r"沿用.{0,6}(价格|单价|报价)|(价格|单价|报价).{0,3}(不变|照旧|一样)", c):
            out["reuse_price"] = comp_ctx or ["column", "beam"]
            continue
        # 清空
        if re.search(r"清空|删除|去掉|不要|置空|删掉", c) and not re.search(r"项目|对话|会话|工作区", c):
            for f in _fields_named(c, comp_ctx, grid_ctx):
                out["patches"].append(_p(f, "clear", None, None, c))
            continue
        # 数值
        prev_end, prev = 0, None
        for m in QTY_RE.finditer(c):
            num, unit = m.group(1), (m.group(2) or None)
            seg = c[prev_end:m.start()]
            prev_end = m.end()
            if unit == "%":
                continue
            kind = _kind_by_unit(unit)
            field = None
            op = "set"
            rel = re.search(r"(再)?(增加|加大|加长|加|多|放大)(?!到)\s*$", seg) or re.search(r"(增加|加)了?\s*$", seg)
            dec_ = re.search(r"(再)?(减少|减小|缩小|减|少)(?!到)\s*$", seg)
            if rel:
                op = "add"
            elif dec_:
                op = "sub"
            # 指代之前数字（“从 A 改为 B”“18x6”）
            if prev is not None and re.fullmatch(rf"\s*{SET_WORDS}?\s*", seg.replace("再", "")) is not None and \
                    re.search(r"从|由", c[:m.start()]) and prev.get("from_marker"):
                prev_patch = prev
                prev_patch["claimed_previous_value"] = prev_patch.pop("value")
                prev_patch["claimed_previous_unit"] = prev_patch.pop("unit")
                prev_patch.pop("from_marker", None)
                prev_patch.update(value=num, unit=unit or prev_patch.get("claimed_previous_unit"), op="set")
                prev = prev_patch
                continue
            if prev is not None and re.fullmatch(r"\s*[x*乘]\s*", seg):
                field = {"width_m": "height_m", "grid_width_m": "grid_height_m"}.get(prev["field"])
                if field and not unit and prev.get("unit"):
                    unit = prev["unit"]
                if field is None:
                    continue
            if field is None:
                if kind == "linear_mass" or re.search(r"线重|每米", seg):
                    field = "linear_mass"
                elif kind == "price" or re.search(r"单价|价格|报价", seg):
                    field = "unit_price"
                elif kind in ("length", "area", None):
                    field = _length_field(seg, grid_ctx, None)
            if field is None and not re.search(r"尺寸|大小|规格", seg):
                bare = not re.sub(rf"\s|{SET_WORDS}|再|把|它|这个|那个|就", "", seg)
                if _compatible(pending_field, kind):
                    field = pending_field
                elif (op in ("add", "sub") or re.search(r"再|改回|改成|改为|调整|改到", c)) and _compatible(last_field, kind):
                    field = last_field
                elif active_field and bare and n_clauses == 1 and _compatible(active_field, kind):
                    field = active_field
            if field in USER_FIELDS and field.startswith(("column_", "beam_")):
                out["patches"].append(_p(field, op, num, unit, c))
                prev = out["patches"][-1]
                pending_field = None
                continue
            if field in ("linear_mass", "unit_price"):
                comps = comp_ctx or ([active_field.split("_")[0]] if active_field and active_field.startswith(
                    ("column", "beam")) else [])
                if not comps:
                    out["ambiguous"].append({"text": c, "reason": f"未说明是立柱还是横梁的{'线重' if field == 'linear_mass' else '单价'}"})
                    continue
                for comp in comps:
                    out["patches"].append(_p(f"{comp}_{field}", op, num, unit, c))
                prev = out["patches"][-1]
                continue
            if field is None:
                if re.search(r"尺寸|规格|大小", seg):
                    out["ambiguous"].append({"text": c, "reason": "“尺寸”未说明是幕墙总宽／总高还是分格宽／高"})
                elif kind in ("length", None) and not re.search(r"\d", seg) and "compute" not in seg:
                    out["ambiguous"].append({"text": c, "reason": "没有说明这个数值对应哪个参数"})
                continue
            p = _p(field, op, num, unit, c)
            if re.search(r"(从|由)\s*$", seg):
                p["from_marker"] = True
            out["patches"].append(p)
            prev = p
            pending_field = None
        if prev_end == 0:  # 本句没有数值：记住它提到的字段，供下一句的数值使用（“原方案高度填错了，应该是 9 米”）
            nf = _length_field(c, grid_ctx, None)
            if re.search(r"线重", c) and len(comp_ctx) == 1:
                nf = f"{comp_ctx[0]}_linear_mass"
            elif re.search(r"单价|价格|报价", c) and len(comp_ctx) == 1:
                nf = f"{comp_ctx[0]}_unit_price"
            pending_field = nf or pending_field
    for p in out["patches"]:
        p.pop("from_marker", None)
    # 同一字段只保留最后一次（“改成 1.5，不，改成 2”）
    seen = {}
    for p in out["patches"]:
        seen[p["field"]] = p
    out["patches"] = list(seen.values())
    # 只给出数值的回答
    if active_field and not out["patches"] and re.fullmatch(rf"\s*({NUM})\s*({UNIT})?\s*", t):
        m = re.fullmatch(rf"\s*({NUM})\s*({UNIT})?\s*", t)
        out["patches"].append(_p(active_field, "set", m.group(1), m.group(2), raw))
        out["ambiguous"] = []
    intents, task = _intents(t, bool(out["patches"]))
    out["intents"] |= intents
    out["task"] = task
    if "query" in out["intents"]:
        out["query_text"] = raw.strip()
    if out["patches"] or out["reuse_price"]:
        out["intents"].add("modify")
        out["intents"].discard("query")
    return _finish(out)


def _p(field, op, value, unit, evidence):
    return {"field": field, "op": op, "value": value, "unit": unit, "evidence": evidence.strip(),
            "claimed_previous_value": None, "claimed_previous_unit": None}


def _kind_by_unit(unit: str | None) -> str | None:
    if not unit:
        return None
    u = unit.lower()
    if u in ("kg/m",):
        return "linear_mass"
    if u.startswith("元"):
        return "price"
    if u in ("m²", "m2", "平方米", "㎡"):
        return "area"
    return "length"


def _fields_named(c: str, comps: list[str], grid_ctx: bool) -> list[str]:
    out = []
    if re.search(r"单价|价格|报价", c):
        out += [f"{x}_unit_price" for x in (comps or ["column", "beam"])]
    if re.search(r"线重", c):
        out += [f"{x}_linear_mass" for x in (comps or ["column", "beam"])]
    f = _length_field(re.sub(r"单价|价格|报价|线重", "", c), grid_ctx, None)
    if f and not out:
        out.append(f)
    return out


def _finish(out: dict) -> dict:
    out["intents"] = sorted(out["intents"])
    return out
