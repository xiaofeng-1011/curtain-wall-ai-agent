"""Standards controls and registered original-PDF citations, using the existing UI."""
from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from agent import extraction
from agent.ocr_import import import_ocr_package
from evidence.standard_retrieval import StandardRetriever, standard_parts, validate_evidence
from storage.standard_library import AssetError


def prepare_builtin(ws, *, repair=False):
    """Deploy prepared snapshots once per process; never import/OCR in chat."""
    if getattr(ws, '_builtin_prepared', False) and not repair:
        return getattr(ws, '_builtin_status', {})
    from storage.bundled_standards import initialize_builtin
    progress = st.empty()
    try:
        result = initialize_builtin(ws.standard_library(),
            progress=lambda n,total,text: progress.progress(min(1.0,n/max(1,total)), text=text), repair=repair)
        if result.get('status') == 'error':
            raise AssetError(result.get('message', '规范包校验失败'))
        if result.get('status') == 'absent':
            st.warning('未找到内置规范包。请用包含规范资料的完整安装包修复安装；可在管理规范库中恢复。')
        ws._builtin_status = result
        ws._builtin_prepared = True
        return result
    except Exception as exc:
        st.error(f'内置规范库暂不可用：{exc}')
        st.caption('可点击下方重新校验恢复。若安装资源缺失或损坏，请用完整安装包覆盖安装；已有项目、聊天和自有资料会保留。')
        if st.button('重新校验并恢复内置规范库', key='builtin-startup-repair'):
            ws._builtin_prepared = False
            st.rerun()
        return {'status':'error'}
    finally:
        progress.empty()


def controls(ws, asst, conv):
    library = ws.standard_library()
    collections = library.list_collections()
    before = (conv.get('qa_options') or {}).get('collection_ids', [])
    conv = asst.ensure_qa_selection(conv['conversation_id'])
    options = conv.get('qa_options') or {}
    cid = conv['conversation_id']
    if before != options.get('collection_ids', []):
        st.session_state.pop('qa-collections-'+cid, None)
    st.subheader('规范问答')
    st.caption('查阅本地资料，无需建立工程项目。可检索、可参考回答与专业审核是不同状态。')
    choices = {c['collection_id']: c['name'] for c in collections}
    def save_option(field, key):
        # Read the latest revision and persist only the widget the user changed.
        saved = ws.get_conversation(cid)
        value = st.session_state[key]
        if field == 'answer_mode':
            value = 'answer' if value == 'AI 解读' else 'source'
        if field == 'project_id':
            value = value or None
        asst.set_qa_options(cid, dict(saved.get('qa_options') or {}, **{field:value}),
                            user_selection=field in ('collection_ids', 'allow_model', 'answer_mode'), changed=field)

    def reconcile_choices(key, available):
        # Option loss is not a user decision. Restore saved intent when it returns.
        signature = tuple(available)
        if st.session_state.get(key+'-available') != signature:
            st.session_state.pop(key, None)
            st.session_state[key+'-available'] = signature

    reconcile_choices('qa-collections-'+cid, choices)
    selected = st.multiselect('资料集', list(choices), default=[c for c in options.get('collection_ids',[]) if c in choices],
                              format_func=lambda x: choices[x], key='qa-collections-'+cid,
                              on_change=save_option, args=('collection_ids','qa-collections-'+cid))
    unavailable = len(set(options.get('collection_ids', [])) - set(choices))
    st.caption(f'已选 {len(selected)} 项' + (f' · 另有 {unavailable} 项暂不可用，已保留选择' if unavailable else '')
               + (' · 未选择资料集，不会检索全部资料' if not selected else ''))
    a,b = st.columns([3,1])
    mode = a.radio('问答方式', ['查找原文','AI 解读'], index=int(options.get('answer_mode')=='answer'),
                   horizontal=True, key='qa-mode-'+cid,
                   on_change=save_option, args=('answer_mode','qa-mode-'+cid),
                   help='查找原文：只做本地检索。AI 解读：模型阅读检索到的条文片段后总结、解释并给出建议，出处由程序生成。')
    if b.button('管理规范库', icon=':material/library_books:', key='qa-manage-'+cid):
        st.session_state['dialog'] = ('standards', {})
        st.rerun()
    docs = [d for c in selected for d in library.documents(c)]
    standards = sorted({standard_parts(d.get('standard_code') or d.get('standard_number',''))[0] for d in docs} - {''})
    versions = sorted({str(d.get('version','')) for d in docs} - {''})
    categories = sorted({d.get('category','未分类') for d in docs})
    with st.expander('筛选标准、版本与专业类别'):
        st.caption('标准身份为资料候选信息；年份不代表现行有效。问题中明确指定的版本也会严格筛选。')
        for label, field, prefix, available in (
            ('标准编号候选', 'standards', 'qa-std-', standards),
            ('版本年份候选', 'versions', 'qa-ver-', versions),
            ('专业类别（按资料目录）', 'categories', 'qa-cat-', categories),
        ):
            key = prefix+cid
            reconcile_choices(key, available)
            st.multiselect(label, available, default=[x for x in options.get(field,[]) if x in available],
                           key=key, on_change=save_option, args=(field,key))
    effective = asst.effective_qa_options(conv)
    allow_model = st.checkbox('AI 解读使用已配置模型', value=effective['allow_model'],
                              disabled=mode=='查找原文', key='qa-model-'+cid,
                              on_change=save_option, args=('allow_model','qa-model-'+cid),
                              help='开启后向设置中的模型发送问题、项目条件和本次检索到的相关条文片段（含相邻条文、适用范围和被引用条文），'
                                   '不发送整份PDF。关闭后只显示本地摘录；关闭选择会被记住。')
    if mode!='查找原文':
        if allow_model:
            st.caption(extraction.model_label() + (' · 已配置，提问时将实际调用' if extraction.model_available()
                                                   else ' · 未配置，将返回本地摘录'))
        else:
            st.caption('已关闭模型：AI 解读只返回本地检索摘录。')
        projects = {p['project_id']: p['name'] for p in ws.list_projects()}
        if projects:
            current = options.get('project_id') if options.get('project_id') in projects else ''
            st.selectbox('结合项目条件（可选）', [''] + list(projects), index=([''] + list(projects)).index(current),
                         format_func=lambda x: projects.get(x, '不结合项目'), key='qa-project-'+cid,
                         on_change=save_option, args=('project_id','qa-project-'+cid),
                         help='只读取该项目已录入的参数作为解读条件，不会修改项目。')
    if not collections:
        st.info('规范库暂未启用。请在“管理规范库”中恢复内置规范库；自行增加资料也可在该入口操作。')
    return conv


def _evidence_block(run_id, hit, key_prefix, *, label=None):
    pages = '、'.join(str(p) for p in dict.fromkeys(s['page'] for s in hit['sources']))
    with st.expander(label or f"[{hit['evidence_id']}] {hit['filename']} · 原PDF {pages} 页"):
        st.caption(f"标准候选：{hit['standard_code'] or '未识别'} · 版本候选：{hit['version'] or '未识别'} · {hit['section']}"
                   + f" · 条文号候选：{hit['clause_candidate'] or '未识别'}")
        st.caption('可参与参考摘录' if hit.get('answer_eligible') else '含数值/表格/公式或OCR质量风险：定性内容可参考，具体数值以原PDF页为准')
        st.text(hit['text'])
        if hit.get('context'):
            st.caption('已补充相邻条件与例外候选；相邻段落不自动视为同一条要求。')
        for neighbour in hit.get('context',[]):
            st.text(f"相邻原文 [{neighbour['evidence_id']}]（{neighbour['section']}）\n{neighbour['text']}")
            st.caption('原PDF页：' + '、'.join(str(s['page']) for s in neighbour['sources']))
        for i, source in enumerate(hit['sources']):
            st.caption(f"原PDF文件第 {source['page']} 页 · 处理页 {source.get('processed_page')} · 区域 {source['region']} · "
                       f"印刷页码候选：{source.get('printed_candidate') or '未识别'}（未人工确认）")
            if st.button(f"查看原始PDF第 {source['page']} 页", key=f"original-{key_prefix}-{hit['evidence_id']}-{i}", icon=':material/picture_as_pdf:'):
                st.session_state['dialog'] = ('standard_original', dict(run_id=run_id, evidence_id=hit['evidence_id'], page=source['page']))
                st.rerun()
        for issue in hit.get('issues',[]):
            st.caption(str(issue))
        st.caption(f"专业审核：{hit.get('review_status') or '未复核'} · 图像抽查：{hit.get('sample_review_status') or '未抽查'} · "
                   f"现行状态：{hit.get('current_status') or '待核实'} · 引用校验不等于专业复核")


def model_record_text(record):
    record = record or {}
    if not record.get('attempted'):
        return '未调用模型：' + (record.get('reason') or '本次不需要模型')
    head = f"已实际调用 {record.get('provider_label') or record.get('provider')}（{record.get('model')}，{record.get('endpoint') or '默认地址'}）"
    if record.get('ok'):
        return head + f" · 成功 · 用时 {record.get('duration_ms', 0) / 1000:.1f} 秒" + (
            f" · 阅读 {record['evidence_count']} 个片段" if record.get('evidence_count') else '')
    return head + ' · 失败并回退：' + (record.get('reason') or '未知原因')


def citation_card(ws, run):
    from agent.standard_qa import KIND_TEXT
    from agent.claim_check import STATUS_TEXT
    response = run['answer']
    interp = response.get('interpretation') if response.get('mode') == 'model' else None
    st.caption(response['limits'])
    for gap in response.get('gaps',[]):
        st.caption(gap)
    if interp:
        by_id = {h['evidence_id']: h for h in response['evidence'] + response.get('context_evidence', [])}
        st.markdown('**备注 · 依据出处**（由程序按资料登记信息生成）')
        for note in interp.get('notes', []):
            hit = by_id.get(note['evidence_id'])
            st.caption(f"[{note['n']}] {note['standard_code']}-{note['version']}《{note['title'][:40]}》第 {note['clause']} 条（候选）· "
                       f"{note['pages']} · {note['level']}，{note['nature']} · {note['force']} · 现行状态：{note['current_status']}")
            if hit:
                _evidence_block(run['run_id'], hit, run['run_id'] + '-n', label=f"[{note['n']}] 展开原文与原PDF入口 · {hit['filename']}")
        flagged = [c for c in interp['claims'] if c['status'] != 'verified_source']
        with st.expander(f"校验说明 · {response['validation'].get('shown', 0)} 条显示，{response['validation'].get('restricted', 0)} 条移除，"
                         f"{response['validation'].get('flagged', 0)} 条附注意事项"):
            st.caption(response['validation'].get('method', ''))
            st.caption('“来源与原文定位已校验”只说明依据编号、原文和页码对得上，不代表解释在语义上完全正确；模型自评不计入校验。')
            for c in flagged:
                st.markdown(f"- **{STATUS_TEXT[c['status']]}** · {KIND_TEXT.get(c['kind'], c['kind'])}：{c['text']}")
                for x in c.get('problems', []) + c.get('flags', []):
                    st.caption('　' + x)
            if interp.get('summary_problems'):
                st.caption('总结未通过：' + '；'.join(interp['summary_problems']))
        others = [h for h in response['evidence'] if h['evidence_id'] not in interp.get('citation_order', [])]
        if others:
            with st.expander(f'其他检索候选（{len(others)} 处，本次解读未引用）'):
                for hit in others:
                    pages = '、'.join(str(p) for p in dict.fromkeys(s['page'] for s in hit['sources']))
                    st.caption(f"{hit['filename']} · 第 {hit.get('clause_candidate') or '未识别'} 条 · 原PDF {pages} 页")
    else:
        for hit in response['evidence']:
            _evidence_block(run['run_id'], hit, run['run_id'])
    st.caption('模型状态：' + model_record_text(response.get('model_record')))


@st.dialog('原始PDF复核', width='large')
def original_dialog(ws, run_id, evidence_id, page):
    library = ws.standard_library()
    run = ws.get_run(run_id)
    if not run and str(run_id).startswith('ANA'):
        analysis = ws.get_analysis(run_id)
        if analysis:
            hits = [h for it in analysis['check'].get('items', []) for h in it.get('evidence', [])]
            run = {'requested_task': 'standard_qa', 'answer': {'evidence': hits},
                   'options': {'collection_ids': analysis['check'].get('evidence_version', {}).get('collections', [])}}
    if not run or run.get('requested_task')!='standard_qa':
        st.error('引用运行记录不存在'); return
    answer = run['answer']
    hit = next((h for h in answer['evidence'] + answer.get('context_evidence', []) if h['evidence_id']==evidence_id),None)
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
    st.caption('内置规范库随安装提供，可离线检索和查看原始PDF。可检索不代表可可靠回答，也不代表已经专业审核。')
    with st.expander('内置规范库维护与恢复'):
        st.caption('修复会校验安装资源并重新部署预建索引，不重跑OCR。升级安装会更新内置包，保留项目、聊天和自有规范资料。')
        if st.button('校验并恢复内置规范库', key='builtin-repair'):
            result = prepare_builtin(ws, repair=True)
            if result.get('status') not in ('error','absent'):
                st.success('内置规范库已恢复。')
            elif result.get('status')=='absent':
                st.error('未找到随安装提供的规范包，请使用完整安装包覆盖安装。')
        st.caption('停用仅移除当前用户的内置库登记与索引；安装资源保留，可随时恢复。清空项目或聊天不会执行此操作。')
        remove = st.checkbox('确认停用当前用户的内置规范库', key='builtin-remove-confirm')
        if st.button('停用内置规范库', disabled=not remove, key='builtin-remove'):
            from storage.bundled_standards import remove_builtin
            remove_builtin(library)
            ws._builtin_prepared = False
            st.rerun()
    with st.expander('高级：增加或更新自己的规范资料'):
        st.caption('专用导入只读取逐页文本、页码映射和质量清单，不重跑OCR，不修改原件或处理成果。普通文件库上传不能代替规范库导入。')
        source = st.text_input('原始PDF目录（只读）', '')
        package = st.text_input('OCR成果包目录（只读）', '')
        name = st.text_input('资料集名称', '我的规范资料')
        import_clicked = st.button('导入或增量更新成果包', icon=':material/sync:', disabled=not source.strip() or not package.strip())
    if import_clicked:
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
        if collection.get('bundle_version'):
            st.caption('内置规范包版本：'+str(collection['bundle_version']))
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
