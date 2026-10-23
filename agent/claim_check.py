"""按结论逐条校验模型输出，只限制有问题的那一条。

校验能证明的：引用的依据确实在本次提供的资料中；支撑语句能在原文中定位；数字、单位来自原文或
程序计算；应/宜/可/不得等要求强度和否定关系没有被改写；原文中的前提、例外有没有被交代；
没有提及未提供资料的标准编号；没有从少量资料推出“整体合规”“结构安全”或标准现行有效。

校验不能证明的：结论在语义上完全正确。因此通过校验的状态叫“来源与原文定位已校验”，
不叫“事实正确”。模型自评不计入校验。
"""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation

from evidence.standard_retrieval import STANDARD, norm, standard_parts

STRENGTH = [('严禁', 3, True), ('禁止', 3, True), ('必须', 3, False), ('不得', 2, True), ('不应', 2, True),
            ('不允许', 2, True), ('不能', 2, True), ('不宜', 1, True), ('不可', 1, True), ('应', 2, False), ('宜', 1, False),
            ('可', 0, False)]
# “应”“可”等字在普通词里出现（应力、相应、可能……）时不是要求用语。
NOT_MODAL = re.compile(r'应力|应变|相应|对应|反应|供应|适应|响应|效应|感应|应用|应急|顺应|回应|理应|答应|'
                       r'可能|可靠|可见|可视|可燃|可调|可拆|可开启|可操作|可检修|可更换|可再生|许可|认可|可行|宜居')
CONDITION = re.compile(r'当[^。；]{1,40}时|除[^。；]{1,40}外|对于[^。；，]{1,30}|在[^。；]{1,30}情况下|但[^。；]{2,40}|'
                       r'如[^。；，]{1,20}时|有条件时|必要时|不能满足[^。；]{0,20}时')
GLOBAL = re.compile(r'(整体|全部|全面|完全|所有方面|整个工程|该工程|本工程|整个项目|该项目|本项目|方案)'
                    r'[^。；，]{0,6}(合规|符合规范|满足规范|符合所有|满足所有|均符合|均满足|没有问题)'
                    r'|结构(是|均|已|完全)?(安全|可靠)(的|无虞)?'
                    r'|(承载力|承载能力|强度|刚度|挠度)[^。；，]{0,6}(足够|满足要求|没有问题|安全)')
NEGATE = re.compile(r'(不能|无法|尚不能|尚无法|不可|未能|未|不|难以|没有|并非|不等于|不代表|不能据此|无从|待)[^。；]{0,10}$')
STATUS = re.compile(r'(现行有效|现行版本|仍然有效|已经?废止|已作废|已被替代|已失效)')
STATUS_HEDGE = re.compile(r'待核实|未核实|需核实|无法确认|不能确认|尚不能确定')
NUMBER = re.compile(r'(?<![A-Za-z\d.])(\d+(?:\.\d+)?)\s*(N/mm2|N/mm²|kN/m2|kN/m²|mm|cm|m²|m2|m|kN|N|MPa|kPa|Pa|%|‰|°C|℃|°|h|min|s|kg/m|kg|年|层|个|倍|次)?')
CITE = re.compile(r'\[(\d{1,2})\]')


def _nfkc(text) -> str:
    return unicodedata.normalize('NFKC', str(text or ''))


COMPARE = r'小于|少于|低于|短于|窄于|薄于|大于|多于|高于|超过|超出|长于|宽于|厚于'


def _fold(text: str) -> str:
    """“不应小于”的否定属于比较关系：改写成“应不小于”，再分别比较要求强度和比较方向。"""
    return re.sub(rf'不(应|宜|得|可|能)({COMPARE})', r'\1不\2', NOT_MODAL.sub('', norm(text)))


def directions(text: str) -> set[str]:
    t = _fold(text)
    out = set()
    for m in re.finditer(rf'(不)?({COMPARE})', t):
        smaller = m.group(2) in ('小于', '少于', '低于', '短于', '窄于', '薄于')
        out.add(('>=' if smaller else '<=') if m.group(1) else ('<' if smaller else '>'))
    if re.search(r'至少|以上|及以上|不止', t):
        out.add('>=')
    if re.search(r'至多|最多|以内|以下|及以下', t):
        out.add('<=')
    return out


def modals(text: str) -> list[tuple[str, int, bool]]:
    t = re.sub(rf'不({COMPARE})', '', _fold(text))
    found = []
    for word, level, negative in STRENGTH:
        if word in t:
            found.append((word, level, negative))
            t = t.replace(word, '')
    return found


def numbers(text: str) -> list[tuple[str, str]]:
    t = re.sub(r'(?<=\d),(?=\d{3}(?!\d))', '', CITE.sub(' ', _nfkc(text)))
    t = re.sub(r'(?:GB|JGJ|JG|JC|DB\d*|CECS|DG/?TJ)\s*/?\s*T?\s*\d+(?:\.\d+)*(?:\s*-\s*\d{2,4})?', ' ', t, flags=re.I)
    # 条文号、表号不是工程数值（例如“第5.5.4条”“表6.2.1”）。
    t = re.sub(r'第\s*[A-Z]?[\d.]+\s*[条款章节]|[表图式]\s*[A-Z]?\d+(?:\.\d+)+|(?<![\d.])\d+(?:\.\d+){2,}(?![\d.])', ' ', t)
    return [(m.group(1), m.group(2) or '') for m in NUMBER.finditer(t)]


def _num_set(text: str) -> set[str]:
    out = set()
    for value, _ in numbers(text):
        out.add(value)
        try:
            out.add(format(Decimal(value).normalize(), 'f'))
        except InvalidOperation:
            pass
    return out


def _canon(value: str) -> str:
    try:
        return format(Decimal(value).normalize(), 'f')
    except InvalidOperation:
        return value


def sentence_with(text: str, basis: str) -> str:
    """原文中包含支撑语句的整句（用于检查前提和例外）。"""
    for sentence in re.split(r'(?<=[。；;])', _nfkc(text)):
        if norm(basis)[:12] and norm(basis)[:12] in norm(sentence):
            return sentence.strip()
    return ''


def global_claims(text: str) -> list[str]:
    """找出未被否定的整体合规、结构安全断言。“尚不能确认结构安全”不算。"""
    hits = []
    t = _nfkc(text)
    for m in GLOBAL.finditer(t):
        before = t[max(0, m.start() - 12):m.start()]
        after = t[m.end():m.end() + 6]
        if NEGATE.search(before) or re.match(r'[^。；]{0,4}(待|需|仍需|尚需|还需|与否|性)', after) or '是否' in before[-4:]                 or re.search(r'是否|能否|可否|与否|有没有|是不是', m.group(0)):
            continue
        # “确保主体结构安全”“与主体结构可靠连接”“安全裕度可能更高”“验算通过后……满足要求”是目标、做法、
        # 条件或推测，不是安全结论。
        wide = t[max(0, m.start() - 40):m.start()]
        if re.search(r'(若|如果|假如|只要|前提是|在[^。；]{1,20}前提下|待[^。；]{1,20}后|(齐全|完成|补充|提供|通过)后)[^。；]*$', wide):
            continue
        if re.search(r'(确保|保证|保障|使其|以便|从而)[^。；]{0,12}$', t[max(0, m.start() - 16):m.start()]) \
                and not m.group(0).startswith(('整体', '全部', '全面', '整个', '该工程', '本工程', '该项目', '本项目')):
            continue
        if m.group(0).startswith('结构') and (re.search(r'(确保|保证|保障|影响|涉及|关系到|关乎|危及|验算|判断|核查|评估|确认|复核|论证|考虑|关注|有利于|不利于)[^。；]{0,8}$', before)
                                             or re.match(r'(连接|固定|锚固|裕度|余量|储备|系数|等级|措施|要求|方面|问题)', after)):
            continue
        if re.search(r'可能|或许|也许|未必|不一定', before[-6:] + after):
            continue
        hits.append(m.group(0))
    return hits


def unknown_standards(text: str, allowed_codes: set[str]) -> list[str]:
    bad = []
    for m in STANDARD.finditer(_nfkc(text)):
        code, _ = standard_parts(m.group())
        if code and code not in allowed_codes:
            bad.append(m.group().strip())
    return bad


def check_text(text: str, *, allowed_numbers: set[str], allowed_codes: set[str], risky_numbers: set[str] = frozenset()) -> dict:
    """不依赖单条证据的通用检查（总结、缺项说明、方案分析句子）。"""
    problems, flags = [], []
    for value, unit in numbers(text):
        if _canon(value) not in allowed_numbers and value not in allowed_numbers:
            problems.append(f'数值“{value}{unit}”没有出现在所给原文或程序计算结果中')
        elif _canon(value) in risky_numbers:
            flags.append(f'数值“{value}{unit}”来自未逐项核对的OCR文本，请对照原PDF页')
    for code in unknown_standards(text, allowed_codes):
        problems.append(f'提及了本次未提供资料的标准编号“{code}”')
    for g in global_claims(text):
        problems.append(f'“{g}”属于整体合规或结构安全断言，现有资料和计算不能支持')
    m = STATUS.search(_nfkc(text))
    if m and not STATUS_HEDGE.search(_nfkc(text)):
        problems.append(f'“{m.group(0)}”：标准现行状态未核实，不能断言')
    return {'problems': list(dict.fromkeys(problems)), 'flags': list(dict.fromkeys(flags))}


REQUIREMENT_KINDS = {'requirement', 'condition', 'exception', 'relation'}


def check_claim(claim: dict, evidence: dict[str, dict], *, project_numbers: set[str], allowed_codes: set[str],
                sibling_claims: list[dict] = ()) -> dict:
    """返回 dict(status, problems, flags, condition_text)。status：verified_source / flagged / restricted。"""
    kind = claim.get('kind')
    text = str(claim.get('text') or '').strip()
    ids = [i for i in claim.get('evidence_ids') or [] if i in evidence]
    unknown_ids = [i for i in claim.get('evidence_ids') or [] if i not in evidence]
    problems, flags, condition_text = [], [], ''
    if not text:
        return {'status': 'restricted', 'problems': ['结论为空'], 'flags': [], 'condition_text': ''}
    if unknown_ids:
        problems.append('引用了本次未提供的依据编号：' + '、'.join(unknown_ids[:3]))
    if kind in REQUIREMENT_KINDS and not ids:
        problems.append('规范要求类结论没有关联本次提供的依据')
    cited = [evidence[i] for i in ids]
    cited_text = '\n'.join(h['text'] for h in cited)
    basis = str(claim.get('basis') or '').strip()
    # 模型常用“……”连接同一条文中的几段原文；每一段都必须能在所引原文中找到。
    segments = [norm(x) for x in re.split(r'…+|\.{3,}|。{2,}', basis) if len(norm(x)) >= 4]

    def located(texts):
        return bool(segments) and all(any(seg in norm(t) for t in texts) for seg in segments)

    basis_ok = located([h['text'] for h in cited])
    if basis and not basis_ok and sum(len(x) for x in segments) >= 8:
        # 支撑语句在本次另一条依据中能找到：改为关联那条依据，并说明引用编号已更正。
        other = [i for i, h in evidence.items() if i not in ids and located([h['text']])]
        if other:
            ids = other[:1] + ids
            cited = [evidence[i] for i in ids]
            cited_text = '\n'.join(h['text'] for h in cited)
            claim['evidence_ids'] = ids
            basis_ok = True
            flags.append('模型标注的依据编号与支撑原文不一致，程序已改为能定位该原文的依据')
    if kind in REQUIREMENT_KINDS:
        if not basis:
            problems.append('没有给出可在原文中定位的支撑语句')
        elif not basis_ok:
            problems.append('支撑语句不能在所引原文中定位（可能改写了原文或引错条文）')
    elif basis and not basis_ok:
        flags.append('所附支撑语句与原文不完全一致，此项按分析或建议理解')

    # 数字与单位
    risky = set()
    for h in cited:
        if h.get('numeric_risk') or h.get('quality') != 'reference':
            risky |= _num_set(h['text'])
    allowed = _num_set(cited_text) | set(project_numbers)
    for value, unit in numbers(text):
        canon = _canon(value)
        if canon not in allowed and value not in allowed:
            problems.append(f'数值“{value}{unit}”没有出现在所引原文或项目条件中')
            continue
        if unit and canon in _num_set(cited_text) and canon not in project_numbers:
            near = re.findall(re.escape(value) + r'\s*([A-Za-z%‰°℃²/]+|毫米|米|层|个|倍|年|次)', _nfkc(cited_text))
            stem = lambda u: (re.match(r'[A-Za-z%‰°℃]+|毫米|米|层|个|倍|年|次', norm(u)) or [norm(u)])[0]
            if near and stem(unit) not in [stem(x) for x in near]:
                problems.append(f'数值“{value}”的单位“{unit}”与原文单位“{near[0]}”不一致')
        if canon in risky and canon not in project_numbers:
            flags.append(f'数值“{value}{unit}”来自含表格/公式或OCR质量风险的原文，未逐项核对，请查看原PDF页')

    # 要求强度与否定
    if kind in REQUIREMENT_KINDS and basis_ok:
        source_sentence = '。'.join(x for x in (sentence_with(cited_text, seg) for seg in segments) if x) or basis
        b, c = modals(source_sentence), modals(text)
        b_neg, c_neg = any(x[2] for x in b), any(x[2] for x in c)
        b_words, c_words = {x[0] for x in b}, {x[0] for x in c}
        # 一句话概括多条原文时会出现多个要求用语；原文用语都保留下来就不算改写强度。
        kept = bool(b_words) and b_words <= c_words
        b_dir, c_dir = directions(source_sentence), directions(text)
        same_direction = bool(b_dir) and bool(c_dir) and bool(b_dir & c_dir)
        if b_dir and c_dir and not (b_dir & c_dir):
            problems.append('数值比较方向与原文相反（例如把“不小于”写成“小于”）')
        elif b and c and b_neg != c_neg and not kept and not same_direction:
            problems.append('否定或禁止关系与原文不一致（原文' + ('是' if b_neg else '不是') + '否定/禁止要求）')
        elif b and c and not kept:
            b_level, c_level = max(x[1] for x in b), max(x[1] for x in c)
            if c_level > b_level:
                problems.append(f'把原文“{b[0][0]}”写成了更强的“{c[0][0]}”')
            elif c_level < b_level:
                flags.append(f'表述强度弱于原文“{b[0][0]}”，以原文为准')
        elif b and not c and kind == 'requirement':
            flags.append(f'原文用语为“{"、".join(x[0] for x in b)}”，本句未保留要求强度，以原文为准')
        # 前提与例外
        cond = CONDITION.search(source_sentence)
        if cond:
            core = norm(cond.group(0))[1:7]
            covered = core and core in norm(text)
            covered = covered or any(s.get('kind') in ('condition', 'exception') and set(s.get('evidence_ids') or []) & set(ids)
                                     for s in sibling_claims if s is not claim)
            if not covered:
                condition_text = cond.group(0)
                flags.append(f'原文带有前提或例外“{cond.group(0)}”，已附在本条后，适用时须同时满足')
    if kind in ('advice', 'analysis') and re.search(r'必须|严禁|规范(明确)?要求', _nfkc(text)) and not ids:
        flags.append('这是一般工程建议或分析，不是规范强制要求')
    if any(h.get('section') == '条文说明' for h in cited) and kind == 'requirement' and all(h.get('section') == '条文说明' for h in cited):
        flags.append('依据仅为条文说明，属于解释性内容，不构成条文要求')
    if cited and all(h.get('quality') != 'reference' for h in cited):
        flags.append('所引原文OCR质量未达参考门槛，请查看原PDF页核对')

    common = check_text(text, allowed_numbers=allowed | {_canon(x) for x in allowed}, allowed_codes=allowed_codes)
    problems += [p for p in common['problems'] if not p.startswith('数值')]
    problems = list(dict.fromkeys(problems))
    flags = list(dict.fromkeys(flags))
    status = 'restricted' if problems else 'flagged' if flags else 'verified_source'
    return {'status': status, 'problems': problems, 'flags': flags, 'condition_text': condition_text}


STATUS_TEXT = {'verified_source': '来源与原文定位已校验', 'flagged': '已校验，附注意事项',
               'restricted': '未通过校验，已从正文移除'}
