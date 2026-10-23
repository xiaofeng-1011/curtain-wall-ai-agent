"""Standards controls and registered original-PDF citations, using the existing UI."""
from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from agent import extraction
from agent.ocr_import import import_ocr_package
from evidence.standard_retrieval import StandardRetriever, standard_parts, validate_evidence
from storage.standard_library import AssetError


def controls(ws, asst, conv):
    library = ws.standard_library()
    collections = library.list_collections()
    options = conv.get('qa_options') or {}
    cid = conv['conversation_id']
    st.subheader('规范问答')
    st.caption('查阅本地资料，无需建立工程项目。可检索、可参考回答与专业审核是不同状态。')
    choices = {c['collection_id']: c['name'] for c in collections}
    selected = st.multiselect('资料集', list(choices), default=[c for c in options.get('collection_ids',[]) if c in choices],
                              format_func=lambda x: choices[x], key='qa-collections-'+cid)
    a,b = st.columns([3,1])
    mode = a.radio('问答方式', ['查找原文','基于资料回答'], index=int(options.get('answer_mode')=='answer'),
                   horizontal=True, key='qa-mode-'+cid)
    if b.button('管理规范库', icon=':material/library_books:', key='qa-manage-'+cid):
        st.session_state['dialog'] = ('standards', {})
        st.rerun()
    docs = [d for c in selected for d in library.documents(c)]
    standards = sorted({standard_parts(d.get('standard_code') or d.get('standard_number',''))[0] for d in docs} - {''})
    versions = sorted({str(d.get('version','')) for d in docs} - {''})
    categories = sorted({d.get('category','未分类') for d in docs})
    with st.expander('筛选标准、版本与专业类别'):
        st.caption('标准身份为资料候选信息；年份不代表现行有效。问题中明确指定的版本也会严格筛选。')
        chosen_standards = st.multiselect('标准编号候选', standards, default=[x for x in options.get('standards',[]) if x in standards], key='qa-std-'+cid)
        chosen_versions = st.multiselect('版本年份候选', versions, default=[x for x in options.get('versions',[]) if x in versions], key='qa-ver-'+cid)
        chosen_categories = st.multiselect('专业类别（按资料目录）', categories, default=[x for x in options.get('categories',[]) if x in categories], key='qa-cat-'+cid)
    allow_model = st.checkbox('使用已配置模型组织本次所需证据摘录', value=options.get('allow_model',False),
                              disabled=mode=='查找原文', key='qa-model-'+cid,
                              help='开启后仅向现有模型发送问题和至多4个证据片段，不发送整套PDF。配置沿用应用设置。')
    if allow_model and mode!='查找原文':
        st.caption(extraction.model_label() + (' · 已配置' if extraction.model_available() else ' · 未配置，将返回本地摘录'))
    updated = dict(collection_ids=selected, standards=chosen_standards, versions=chosen_versions,
                   categories=chosen_categories, answer_mode='answer' if mode=='基于资料回答' else 'source', allow_model=allow_model)
    if updated != options:
        conv = asst.set_qa_options(cid, updated)
    if not collections:
        st.info('尚未导入规范资料。请打开管理规范库，导入已有OCR成果包。')
    return conv


def citation_card(ws, run):
    response = run['answer']
    st.caption(response['limits'])
    for gap in response.get('gaps',[]):
        st.caption(gap)
    for hit in response['evidence']:
        pages = '、'.join(str(p) for p in dict.fromkeys(s['page'] for s in hit['sources']))
        label = f"[{hit['evidence_id']}] {hit['filename']} · 原PDF {pages} 页"
        with st.expander(label):
            st.caption(f"标准候选：{hit['standard_code'] or '未识别'} · 版本候选：{hit['version'] or '未识别'} · {hit['section']}"
                       + f" · 条文号候选：{hit['clause_candidate'] or '未识别'}")
            st.caption('可参与参考摘录' if hit['answer_eligible'] else '仅供检索定位；不能据此确认数值或要求')
            st.text(hit['text'])
            st.caption('已补充相邻条件与例外候选；相邻段落不自动视为同一条要求。')
            for neighbour in hit.get('context',[]):
                st.text(f"相邻原文 [{neighbour['evidence_id']}]（{neighbour['section']}）\n{neighbour['text']}")
                st.caption('原PDF页：' + '、'.join(str(s['page']) for s in neighbour['sources']))
            for i, source in enumerate(hit['sources']):
                st.caption(f"原PDF第 {source['page']} 页 · 处理页 {source.get('processed_page')} · 区域 {source['region']} · "
                           f"印刷页码候选：{source.get('printed_candidate') or '未识别'}（未人工确认）")
                if st.button(f"查看原始PDF第 {source['page']} 页", key=f"original-{run['run_id']}-{hit['evidence_id']}-{i}", icon=':material/picture_as_pdf:'):
                    st.session_state['dialog'] = ('standard_original', dict(run_id=run['run_id'], evidence_id=hit['evidence_id'], page=source['page']))
                    st.rerun()
            for issue in hit.get('issues',[]):
                st.caption(str(issue))
            st.caption(f"专业审核：{hit.get('review_status') or '未复核'} · 图像抽查：{hit.get('sample_review_status') or '未抽查'} · "
                       f"现行状态：{hit.get('current_status') or '待核实'} · 引用校验不等于专业复核")


@st.dialog('原始PDF复核', width='large')
def original_dialog(ws, run_id, evidence_id, page):
    library = ws.standard_library()
    run = ws.get_run(run_id)
    if not run or run.get('requested_task')!='standard_qa':
        st.error('引用运行记录不存在'); return
    hit = next((h for h in run['answer']['evidence'] if h['evidence_id']==evidence_id),None)
    if not hit or not validate_evidence(library,hit,run['options'].get('collection_ids',[])):
        st.error('引用已失效或资料已更新，请重新检索。'); return
    if page not in {s['page'] for s in hit['sources']}:
        st.error('页码不属于该引用'); return
    doc = library.get_document(hit['collection_id'],hit['doc_id'])
    st.write(doc['filename'])
    selected_page = int(st.number_input('原始PDF页码', min_value=1, max_value=doc['page_count'], value=int(page), step=1,
                                        key='original-page-'+evidence_id))
    try:
        data = library.pdf_bytes(hit['collection_id'],hit['doc_id'])
        st.image(library.page_png(hit['collection_id'],hit['doc_id'],selected_page), caption=f'原始PDF第 {selected_page} 页（非OCR处理副本）', width='stretch')
        st.download_button('下载原始PDF', data, file_name=doc['filename'], mime='application/pdf', key='download-original-'+evidence_id)
        st.caption('已按登记SHA-256核对原件。页码按PDF物理页序，从1开始。')
    except (AssetError,OSError,ValueError) as exc:
        st.error(str(exc))


@st.dialog('管理规范库', width='large')
def library_dialog(ws):
    library = ws.standard_library()
    st.caption('专用导入只读取逐页文本、页码映射和质量清单，不重跑OCR，不修改原件或处理成果。')
    base = Path(__file__).resolve().parents[2]
    source = st.text_input('原始PDF目录（只读）', str(base/'幕墙规范资料'))
    package = st.text_input('OCR成果包目录（只读）', str(base/'幕墙规范资料_处理成果'))
    name = st.text_input('资料集名称', '幕墙规范资料')
    if st.button('导入或增量更新成果包', icon=':material/sync:', type='primary'):
        progress = st.progress(0, text='核对成果包清单…')
        try:
            report = import_ocr_package(library, source, package, name,
                                        progress=lambda n,total,path: progress.progress(n/total, text=f'{n}/{total} {Path(path).name}'))
            retriever = StandardRetriever(library)
            try:
                report['index'] = retriever.build(report['collection_id'])
            finally:
                retriever.close()
            write_report(library,report)
            st.success('导入与本地索引更新完成；详细覆盖和质量问题见下方。')
        except Exception as exc:
            st.error(f'导入未完成（{type(exc).__name__}）：{exc}。已完成文件保留，可再次执行恢复。')
    for collection in library.list_collections():
        cid = collection['collection_id']
        st.subheader(collection['name'])
        retriever = StandardRetriever(library)
        try:
            stale = retriever.stale(cid) or library.source_status(cid)['stale']
        finally:
            retriever.close()
        if stale:
            st.warning('索引或源资料已变化，请执行增量更新；过期索引不参与回答。')
        stats = collection.get('stats',{})
        index = library.get_index_state(cid) or {}
        st.write(f"已登记 {stats.get('documents',0)} 份 / {stats.get('pages',0)} 页；"
                 f"可检索 {stats.get('searchable_documents',0)} 份 / {stats.get('searchable_pages',0)} 页；"
                 f"处理失败 {stats.get('failed_documents',0)} 份 / {stats.get('failed_document_pages',0)} 页。")
        st.caption(f"通过片段质量门槛、可参与参考摘录：{index.get('answer_eligible_documents',0)} 份中的 "
                   f"{index.get('answer_eligible_pages',0)} 页；其余可检索页仅供定位。未专业审核。")
        with st.expander('索引与导入统计明细'):
            st.json({'导入':stats,'索引':index})
        docs = library.documents(cid)
        st.dataframe([{'文件':d['filename'],'标准候选':d.get('standard_code') or d.get('standard_number'),
                       '版本候选':d.get('version'), '页数':d['page_count'],
                       '使用状态':{'reference':'可参考问答（仅无风险片段）','locate':'仅可定位','failed':'失败'}.get(d['quality'],d['quality']),
                       '专业审核':d['review_status'], '图像抽查':d.get('sample_review_status'),
                       '重复别名':len(d.get('aliases',[]))} for d in docs], hide_index=True)
        with st.expander('覆盖缺口与文件级问题'):
            for gap in collection.get('gaps',[]):
                st.write(gap)
            st.caption(f"明确排除的文件：{len(collection.get('excluded_rows',[]))}；失败资料不计入成功数量。")
        st.download_button('下载质量与覆盖报告', json.dumps(full_report(library,cid),ensure_ascii=False,indent=2),
                           file_name='规范库_质量与覆盖报告.json', mime='application/json', key='quality-report-'+cid)


def full_report(library,cid):
    collection = library.get_collection(cid)
    def public_issue(value):
        if not isinstance(value, dict):
            return value
        out = dict(value)
        if out.get('截图/定位'):
            out['截图/定位'] = str(out['截图/定位']).replace('\\', '/').rsplit('/', 1)[-1]
        return out
    def public_doc(d):
        out = {k:v for k,v in d.items() if k not in ('completion_record','program_checks','source_row')}
        out['issues'] = [public_issue(x) for x in out.get('issues', [])]
        out['aliases'] = [{k:v for k,v in a.items() if k != 'source_row'} for a in out.get('aliases', [])]
        return out
    # Avoid distributing absolute paths in the user-facing report.
    return {'collection_id':cid,'name':collection['name'],'revision':collection['revision'],
            'stats':collection.get('stats',{}),'index':library.get_index_state(cid), 'gaps':collection.get('gaps',[]),
            'documents':[public_doc(d) for d in library.documents(cid)],
            'page_quality':[{'doc_id':p['doc_id'],'page':p['page'],'quality':p['quality'],
                             'issues':[public_issue(x) for x in p['issues']], 'printed_candidate':p.get('printed_candidate'),'printed_status':p.get('printed_status')}
                            for p in library.pages(cid) if p['issues'] or p['quality']!='reference']}


def write_report(library, report):
    target = Path(library.root)/'standards'/'import_report.json'
    target.write_text(json.dumps(dict(report,quality=full_report(library,report['collection_id'])),ensure_ascii=False,indent=2), encoding='utf-8')
