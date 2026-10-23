"""规范出处与效力标注：全部由程序依据已登记元数据生成，模型不得编写出处。

- 标准层级按编号前缀识别（国家 / 行业 / 地方 / 团体），只是编号规则推断，标为“按编号识别”。
- 推荐性（/T）与强制性标准、强制性条文是不同概念；条文是否为强制性条文只认资料公告页的明确文字。
- 现行状态没有核实就写“待核实”；PDF 文件页与印刷页码分开写，印刷页码只是未确认候选。
"""
from __future__ import annotations

import re

UNKNOWN = '未识别'


def standard_level(code: str) -> dict:
    """按标准编号前缀给出层级与性质（推断，非效力核实）。"""
    c = (code or '').upper().replace(' ', '')
    if not c:
        return {'level': UNKNOWN, 'nature': UNKNOWN, 'basis': '编号未识别'}
    if c.startswith(('T/', 'CECS')) or c.startswith('T/CECS'):
        return {'level': '团体标准', 'nature': '自愿采用', 'basis': '按编号识别'}
    if c.startswith(('DB', 'DG/TJ', 'DGJ')):
        return {'level': '地方标准', 'nature': '推荐性' if '/T' in c or c.startswith('DG/TJ') else '地方标准（性质待核实）',
                'basis': '按编号识别'}
    if c.startswith('GB55'):
        return {'level': '国家标准', 'nature': '强制性工程建设规范（全文强制）', 'basis': '按编号识别'}
    if c.startswith('GB'):
        return {'level': '国家标准', 'nature': '推荐性' if '/T' in c else '国家标准（含强制性条文时以公告为准）',
                'basis': '按编号识别'}
    if c.startswith(('JGJ', 'JG', 'JC')):
        return {'level': '行业标准', 'nature': '推荐性' if '/T' in c else '行业标准（含强制性条文时以公告为准）',
                'basis': '按编号识别'}
    return {'level': UNKNOWN, 'nature': UNKNOWN, 'basis': '编号规则未覆盖'}


def clause_force(hit: dict, mandatory: dict | None) -> str:
    """该条文的强制程度说明；只有公告页明确列出时才说是强制性条文。"""
    clause = hit.get('clause_candidate')
    if hit.get('section') == '条文说明':
        return '条文说明（解释性内容，不是条文本身）'
    if not mandatory or mandatory.get('status') != 'announced':
        return '是否强制性条文：资料中未识别到公告，待核实'
    if mandatory.get('whole_document'):
        return f"资料公告页（原PDF第{mandatory.get('source_page')}页）称本规范全部条文必须严格执行（OCR识别，现行状态待核实）"
    if clause and clause in mandatory.get('clauses', []):
        return f"资料公告页（原PDF第{mandatory.get('source_page')}页）列为强制性条文（OCR识别，现行状态待核实）"
    if clause:
        return f"未列入资料公告页的强制性条文清单（原PDF第{mandatory.get('source_page')}页），按一般条文理解"
    return '条文号未识别，强制程度待核实'


def reliability(hit: dict) -> tuple[str, str]:
    """(等级, 说明)。等级：reference / numeric / locate / commentary。"""
    if hit.get('section') == '条文说明':
        return 'commentary', '条文说明，可帮助理解，但不是条文要求'
    if hit.get('quality') != 'reference':
        return 'locate', 'OCR质量未达参考门槛，内容可能有误，结论需查看原PDF页'
    if hit.get('numeric_risk'):
        return 'numeric', '含数值、表格或公式，OCR未逐项核对；定性内容可解释，具体数值以原PDF页为准'
    return 'reference', '通过片段质量门槛（未专业复核）'


def page_text(hit: dict) -> str:
    pages = list(dict.fromkeys(s['page'] for s in hit.get('sources', [])))
    printed = []
    for s in hit.get('sources', []):
        cand = s.get('printed_candidate')
        if cand and not str(cand).startswith('未识别'):
            printed.append(str(cand))
    file_pages = '、'.join(str(p) for p in pages) or UNKNOWN
    printed_text = ('印刷页码候选 ' + '；'.join(dict.fromkeys(printed)) + '（未人工确认）') if printed else '印刷页码未识别'
    return f'原PDF文件第 {file_pages} 页 · {printed_text}'


def clean_title(value) -> str:
    """登记标题常含重复段和书号等页眉文字；只做去重和去噪，不改写名称。"""
    parts = [re.sub(r'\s+', ' ', x).strip() for x in re.split(r'\s+/\s+', str(value or ''))]
    parts = [x for x in parts if x and not re.search(r'统一书号|定\s*价|出版社|全文信息系统|工程建设标准$|^未明确识别', x)]
    out = list(dict.fromkeys(parts))
    return ' / '.join(out) if out else UNKNOWN


def citation_note(n: int, hit: dict, mandatory: dict | None = None) -> dict:
    """备注中的一条出处。只使用已校验证据上的登记字段。"""
    level = standard_level(hit.get('standard_code', ''))
    title = clean_title(hit.get('title'))
    return {
        'n': n, 'evidence_id': hit['evidence_id'],
        'title': title, 'filename': hit.get('filename') or UNKNOWN,
        'standard_code': hit.get('standard_code') or UNKNOWN, 'version': hit.get('version') or UNKNOWN,
        'clause': hit.get('clause_candidate') or UNKNOWN, 'section': hit.get('section') or UNKNOWN,
        'pages': page_text(hit), 'file_pages': list(dict.fromkeys(s['page'] for s in hit.get('sources', []))),
        'level': level['level'], 'nature': level['nature'], 'level_basis': level['basis'],
        'force': clause_force(hit, mandatory),
        'current_status': hit.get('current_status') or '待核实',
        'review_status': hit.get('review_status') or '未复核',
        'reliability': reliability(hit)[1],
        'identity_note': '标准编号、版本、条文号均为资料识别候选',
    }


def note_line(note: dict) -> str:
    return (f"[{note['n']}] {note['standard_code']}-{note['version']}《{note['title'][:40]}》 第 {note['clause']} 条（候选）· "
            f"{note['pages']} · {note['level']}，{note['nature']}（{note['level_basis']}）· {note['force']} · "
            f"现行状态：{note['current_status']} · 专业审核：{note['review_status']} · {note['reliability']}")
