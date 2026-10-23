"""确定性单位解析与归一（单位换算由程序完成，不交给模型）。

- 长度统一为 m；线重统一为 kg/m；单价统一为 CNY/kg（元/吨按 /1000 换算并保留原文）。
- 不支持或缺失的单位不默默换算，返回 UNIT_REQUIRED / UNIT_UNSUPPORTED。
- normalize_text() 把常见中文数字与口语单位写法（“一米五”“每米 2.7 公斤”“26000 元一吨”）归一成阿拉伯数字。
"""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal

from .contracts import Code, Issue, calc, dec

LENGTH_UNITS = {
    "m": Decimal(1), "米": Decimal(1),
    "cm": Decimal("0.01"), "厘米": Decimal("0.01"),
    "mm": Decimal("0.001"), "毫米": Decimal("0.001"),
}
AREA_UNITS = {"m2": Decimal(1), "m²": Decimal(1), "平方米": Decimal(1), "㎡": Decimal(1)}
MASS_UNITS = {"kg/m": Decimal(1), "千克/米": Decimal(1), "公斤/米": Decimal(1), "kg/米": Decimal(1),
              "千克/m": Decimal(1), "公斤/m": Decimal(1)}
PRICE_UNITS = {"元/kg": Decimal(1), "元/千克": Decimal(1), "元/公斤": Decimal(1), "cny/kg": Decimal(1),
               "元/吨": Decimal("0.001"), "元/t": Decimal("0.001"), "cny/t": Decimal("0.001")}
CANONICAL = {"length": "m", "area": "m²", "linear_mass": "kg/m", "price": "CNY/kg"}
TABLES = {"length": LENGTH_UNITS, "area": AREA_UNITS, "linear_mass": MASS_UNITS, "price": PRICE_UNITS}
UNIT_HINT = {"length": "m、mm 或 cm", "area": "m²", "linear_mass": "kg/m", "price": "元/kg 或 元/吨"}


def _lookup(kind: str, unit: str):
    u = unit.strip().replace("／", "/").replace(" ", "")
    table = TABLES[kind]
    return table.get(u) if u in table else table.get(u.lower())


def parse_quantity(raw_value, raw_unit: str | None, kind: str, field_path: str | None = None):
    """返回 (Decimal 归一值 | None, 规范单位 | None, issues)。raw_value 可以是 "1200 mm" 这类合写文本。"""
    if raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()):
        return None, None, [Issue(Code.FIELD_MISSING, "缺少数值", field_path)]
    text = normalize_text(str(raw_value)).strip().replace(",", "")
    unit = (raw_unit or "").strip() or None
    m = re.fullmatch(r"\s*([-+]?\d+(?:\.\d+)?)\s*(\S*)\s*", text)
    if not m:
        return None, None, [Issue(Code.VALUE_INVALID, f"无法解析数值“{raw_value}”", field_path)]
    num_s, inline_unit = m.group(1), m.group(2) or None
    if inline_unit and unit and _lookup(kind, inline_unit) != _lookup(kind, unit):
        return None, None, [Issue(Code.PARAMETER_CONFLICT, f"数值自带单位“{inline_unit}”与所选单位“{unit}”不一致",
                                  field_path)]
    unit = inline_unit or unit
    if "." in num_s and len(num_s.split(".")[-1]) > 6:
        return None, None, [Issue(Code.VALUE_INVALID, "输入最多 6 位小数", field_path)]
    value = dec(num_s)
    if unit is None or unit in ("元", "块"):
        what = "报价请说明计价单位（元/kg 或 元/吨）" if kind == "price" else f"请补充单位（{UNIT_HINT[kind]}）"
        return None, None, [Issue(Code.UNIT_REQUIRED, f"数值“{num_s}”没有单位：{what}", field_path,
                                  required_action="补充单位")]
    factor = _lookup(kind, unit)
    if factor is None:
        return None, None, [Issue(Code.UNIT_UNSUPPORTED, f"不支持的单位“{unit}”，可用 {UNIT_HINT[kind]}", field_path)]
    return calc("*", value, factor), CANONICAL[kind], []


def conversion_note(raw_value, raw_unit, kind: str) -> str | None:
    """原始表达与换算后不同则给出说明，例如“26000 元/吨 → 26 元/kg”。"""
    v, u, iss = parse_quantity(raw_value, raw_unit, kind)
    if v is None or not raw_unit:
        return None
    factor = _lookup(kind, raw_unit)
    if factor is None or factor == 1:
        return None
    shown = {"CNY/kg": "元/kg"}.get(u, u)
    return f"{raw_value} {raw_unit} → {format(v.normalize(), 'f')} {shown}"


# ---------------------------------------------------------------- 中文数字与口语单位
_CN_DIGIT = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000, "万": 10000}
_CN_CHARS = "零〇一二两三四五六七八九十百千万"


def cn_to_int(s: str) -> int | None:
    if not s or any(ch not in _CN_CHARS for ch in s):
        return None
    if all(ch in _CN_DIGIT for ch in s) and len(s) > 1:  # “一二零零” 逐位
        return int("".join(str(_CN_DIGIT[ch]) for ch in s))
    total, section, num = 0, 0, 0
    last_unit = 1
    for ch in s:
        if ch in _CN_DIGIT:
            num = _CN_DIGIT[ch]
        else:
            u = _CN_UNIT[ch]
            if u == 10000:
                total += (section + num) * u
                section, num = 0, 0
            else:
                section += (num or 1) * u
                num = 0
            last_unit = u
    if num and last_unit >= 100 and s[-1] in _CN_DIGIT and len(s) >= 2 and s[-2] in "百千":
        num *= last_unit // 10  # “一千五” = 1500，“一百二” = 120
    return total + section + num


_UNIT_AFTER = r"(?=\s*(?:毫米|厘米|米|公斤|千克|吨|元|块|kg|mm|cm|m\b|%|个点))"


def normalize_text(text: str) -> str:
    t = unicodedata.normalize("NFKC", text)
    t = t.replace("×", "x").replace("＊", "*")
    # 一米五 / 两米四 / 1米5 → 1.5米
    t = re.sub(r"([零〇一二两三四五六七八九十]+|\d+)米([零〇一二三四五六七八九]|\d)(?![\d零〇一二三四五六七八九十百千万米])",
               lambda m: f"{cn_to_int(m.group(1)) if not m.group(1).isdigit() else m.group(1)}."
                         f"{_CN_DIGIT.get(m.group(2), m.group(2))}米", t)
    # 一点五 → 1.5
    t = re.sub(r"([零〇一二两三四五六七八九十百千万]+)点([零〇一二三四五六七八九]+)",
               lambda m: f"{cn_to_int(m.group(1))}." + "".join(str(_CN_DIGIT[c]) for c in m.group(2)), t)
    # 其余后接单位的中文整数
    t = re.sub(r"([零〇一二两三四五六七八九十百千万]+)" + _UNIT_AFTER,
               lambda m: str(cn_to_int(m.group(1))) if cn_to_int(m.group(1)) is not None else m.group(1), t)
    num = r"(\d+(?:\.\d+)?)"
    # 线重：每米 2.7 公斤 / 2.7公斤每米 / 2.7 kg 每米
    t = re.sub(r"每米(?:重)?\s*" + num + r"\s*(?:公斤|千克|kg)", r"\1kg/m", t)
    t = re.sub(num + r"\s*(?:公斤|千克|kg)\s*(?:每米|/米|一米|1米)", r"\1kg/m", t)
    t = re.sub(num + r"\s*(?:公斤|千克)\s*/\s*m\b", r"\1kg/m", t)
    # 单价：25 元一公斤 / 每公斤 25 元 / 26000 元一吨 / 每吨 26000 元
    t = re.sub(r"每(?:公斤|千克|kg)\s*" + num + r"\s*(?:元|块)", r"\1元/kg", t)
    t = re.sub(num + r"\s*(?:元|块)(?:钱)?\s*(?:一|1|每|/)\s*(?:公斤|千克|kg)", r"\1元/kg", t)
    t = re.sub(r"每吨\s*" + num + r"\s*(?:元|块)", r"\1元/吨", t)
    t = re.sub(num + r"\s*(?:元|块)(?:钱)?\s*(?:一|1|每|/)\s*(?:吨|t\b)", r"\1元/吨", t)
    t = re.sub(num + r"\s*块(?:钱)?", r"\1元", t)
    return t
