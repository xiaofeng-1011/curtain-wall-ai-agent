"""Read-only acceptance against the explicitly imported real local corpus.

Original image observations are documented separately; these assertions do not
pretend comparing OCR with itself is an accuracy check.
"""
from pathlib import Path
import json
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.standard_qa import answer
from evidence.standard_retrieval import StandardRetriever, validate_evidence
from storage.workspace import Workspace


def main():
    ws = Workspace()
    lib = ws.standard_library()
    collections = lib.list_collections()
    if not collections:
        raise SystemExit('请先导入OCR成果包')
    cid = collections[0]['collection_id']
    retriever = StandardRetriever(lib)
    docs = lib.documents(cid)
    results = []

    def query(question, expect=None):
        started = time.perf_counter()
        response = retriever.search(question, [cid])
        assert not response['stale'], response['stale']
        hits = response['evidence']
        assert all(validate_evidence(lib, h, [cid]) for h in hits)
        if expect is not None:
            assert bool(hits) == expect, question
        results.append({'question': question, 'seconds': round(time.perf_counter()-started,3),
                        'hits': [{'citation':h['evidence_id'],'standard':h['standard_code'], 'version':h['version'],
                                  'clause_candidate':h['clause_candidate'],'section':h['section'],
                                  'pages':[s['page'] for s in h['sources']],
                                  'regions':[s['region'] for s in h['sources']],
                                  'answer_eligible':h['answer_eligible']} for h in hits]})
        return response

    q = query('JGJ102-2003 5.1.7', True)
    short = next(h for h in q['evidence'] if h['section']=='正文候选')
    assert len(short['text']) < 80 and short['sources'][0]['page']==16
    assert short['sources'][0]['region']=='left' and short['answer_eligible']
    assert any(h['section']=='条文说明' and not h['answer_eligible'] for h in q['evidence'])
    q = query('JGJ102-2003 表5.2.1', True)
    assert all(not h['answer_eligible'] for h in q['evidence'])
    assert any('N/mm' in h['text'] and '注' in h['text'] for h in q['evidence'])
    q = query('JGJ102-2003 5.5.8', True)
    assert any(len({(s['page'],s['region']) for s in h['sources']}) > 1 for h in q['evidence'])
    query('玻璃幕墙立柱与主体结构连接',True)
    query('单元式幕墙 密封',True)
    for question in ('DB33/T1240-2021 3.5.3','JGJ336-2016 6.4.3','GB/T13477.4-2017 5.3'):
        q=query(question,True)
        assert all(not h['answer_eligible'] for h in q['evidence'])
    # Versions are derived from distinct registered PDFs, not the query text.
    versions=[d for d in docs if d.get('standard_code')=='GB/T3098.2']
    assert {'2015','2025'}.issubset({d['version'] for d in versions})
    for year in ('2015','2025'):
        q=query(f'GB/T3098.2-{year} 螺母',True)
        assert {h['version'] for h in q['evidence']}=={year}
    query('JGJ102-1999 5.1.7',False)
    query('火星咖啡烹饪食谱',False)
    q=query('JGJ102-2003 5.1.7',True)
    fallback=answer(lib,q,mode='answer',allow_model=False)
    assert fallback['mode']=='local' and fallback['items']
    assert sum(d['status']=='failed' for d in docs)==2
    all_pages=lib.pages(cid)
    render_pages=[p for p in all_pages if any('渲染差异' in i.get('问题','') for i in p['issues'])]
    assert len(render_pages)==510 and all(p['quality']!='reference' for p in render_pages)
    asset=lib.pdf_bytes(cid,short['doc_id'])
    assert asset.startswith(b'%PDF') and lib.page_png(cid,short['doc_id'],16).startswith(b'\x89PNG')
    assert lib.source_status(cid)['stale'] is False
    report={'collection_id':cid,'stats':lib.get_collection(cid)['stats'], 'index':lib.get_index_state(cid),
            'queries':results,'original_asset_hash_verified':True,
            'known_render_difference_pages_restricted':len(render_pages),
            'visual_check_record':'规范问答使用与验收.md；原图保存在 output/standard_qa_validation/',
            'live_model_tested':False,'result':'passed'}
    output=Path(__file__).resolve().parents[1]/'output/standard_qa_validation/acceptance.json'
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    retriever.close()
    print(json.dumps({'result':'passed','queries':len(results),'report':str(output)},ensure_ascii=False))


if __name__=='__main__':
    main()
