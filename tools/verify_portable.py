"""Offline acceptance from a relocated release; records actual checks only."""
from pathlib import Path
import argparse
import json
import os
import socket
import sys
import time

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--deny-root', action='append', default=[])
    args = parser.parse_args()
    output = Path(args.output); output.mkdir(parents=True, exist_ok=True)
    denied = [os.path.normcase(os.path.abspath(p)) for p in args.deny_root]
    def audit(event, values):
        if event in ('open', 'os.listdir', 'os.scandir') and values and isinstance(values[0], (str, bytes, os.PathLike)):
            path = os.path.normcase(os.path.abspath(os.fsdecode(values[0])))
            if any(path == p or path.startswith(p+os.sep) for p in denied):
                raise PermissionError('Acceptance blocks developer directory access')
        if event == 'socket.connect':
            address = values[1]
            if isinstance(address, tuple) and address[0] not in ('127.0.0.1','::1','localhost'):
                raise PermissionError('Offline acceptance blocks external network')
    sys.addaudithook(audit)
    for key in list(os.environ):
        if key.endswith('API_KEY'):
            os.environ.pop(key)
    os.environ['MW_FORCE_FALLBACK'] = '1'
    os.environ['MW_RUNTIME_DIR'] = args.workspace
    from storage.workspace import Workspace
    from storage.bundled_standards import initialize_builtin, BUILTIN_COLLECTION_ID
    from evidence.standard_retrieval import StandardRetriever, validate_evidence
    from agent.assistant import Assistant
    from agent.standard_qa import answer
    ws=Workspace(args.workspace); lib=ws.standard_library()
    start=time.perf_counter()
    status=initialize_builtin(lib)
    assert status['ok'],status
    report={'initialization':status,'initialization_seconds':round(time.perf_counter()-start,2),
            'developer_paths_blocked':args.deny_root,'external_network_blocked':True,'checks':[]}
    cid=BUILTIN_COLLECTION_ID
    a=Assistant(ws); conv=a.new_conversation(mode='standard_qa')
    assert conv['qa_options']['collection_ids']==[cid]
    assert ws.counts()['projects']==0
    report['checks'].append({'name':'fresh workspace default selection without project','status':'passed'})
    retriever=StandardRetriever(lib)
    questions=[('JGJ102-2003 5.5.4',True),('立柱与主体结构连接',True),
               ('JGJ102-2003 5.1.7',True),('JGJ102-2003 5.5.8',True),
               ('JGJ102-2003 表5.2.1',True),('GB/T3098.2-2015 螺母',True),
               ('GB/T3098.2-2025 螺母',True),('JGJ102-1999 5.1.7',False),('火星咖啡烹饪食谱',False)]
    for question, expected in questions:
        result=retriever.search(question,[cid]); hits=result['evidence']
        assert not result['stale'],result['stale']
        assert bool(hits)==expected,question
        assert all(validate_evidence(lib,h,[cid]) for h in hits)
        if '3098.2' in question:
            assert {h['version'] for h in hits}=={question.split('-')[1][:4]}
        if '5.5.4' in question:
            hit=next(h for h in hits if h['section']=='正文候选')
            pages=sorted({s['page'] for s in hit['sources']})
            for page in pages:
                (output/f'JGJ102-2003_5.5.4_original_{page}.png').write_bytes(lib.page_png(cid,hit['doc_id'],page,dpi=130))
            assert lib.pdf_bytes(cid,hit['doc_id']).startswith(b'%PDF')
        if '表5.2.1' in question:
            assert all(not h['answer_eligible'] for h in hits)
        if '5.5.8' in question:
            assert any(len({(s['page'],s['region']) for s in h['sources']})>1 for h in hits)
        if '5.1.7' in question and expected:
            assert any(len(h['text'])<80 and h['sources'][0]['region']=='left' for h in hits)
            fallback=answer(lib,result,mode='answer',allow_model=True)
            assert fallback['mode']=='local'
        report['checks'].append({'name':question,'status':'passed','hits':[{'file':h['filename'],
            'citation':h['evidence_id'],'pages':[s['page'] for s in h['sources']], 'version':h['version'],
            'answer_eligible':h['answer_eligible']} for h in hits]})
        print(question,'passed',flush=True)
    pages=lib.pages(cid)
    affected=[p for p in pages if any('渲染差异' in i.get('问题','') for i in p['issues'])]
    assert len(affected)==510 and all(p['quality']!='reference' for p in affected)
    report['checks'].append({'name':'510 render-difference pages restricted','status':'passed'})
    report['statistics']=lib.get_collection(cid)['stats']
    report['index']=lib.get_index_state(cid)
    a.send(conv['conversation_id'],'JGJ102-2003 5.5.4')
    assert ws.counts()['projects']==0
    from streamlit.testing.v1 import AppTest
    from ui.chat import services
    services.clear()
    app=AppTest.from_file(str(APP/'app.py'),default_timeout=90).run()
    assert not app.exception,[e.message for e in app.exception]
    next(b for b in app.button if b.label=='规范问答').click().run()
    assert not app.exception,[e.message for e in app.exception]
    assert app.multiselect[0].value==[cid]
    app.chat_input[0].set_value('JGJ102-2003 5.5.4').run()
    assert not app.exception,[e.message for e in app.exception]
    next(b for b in app.button if b.label.startswith('查看原始PDF第')).click().run()
    assert not app.exception,[e.message for e in app.exception]
    assert any(n.label=='原始PDF页码' for n in app.number_input)
    assert app.get('imgs') or app.get('image')
    report['checks'].append({'name':'UI default -> query -> original PDF image dialog','status':'passed'})
    retriever.close()
    reopened=Workspace(args.workspace).standard_library()
    restart=initialize_builtin(reopened)
    assert restart['ok'] and not restart['deployed']
    assert len(reopened.documents(cid))==91
    report['checks'].append({'name':'restart no duplicate import/index build','status':'passed'})
    report['result']='passed'
    (output/'portable_acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'result':'passed','checks':len(report['checks'])},ensure_ascii=False))


if __name__=='__main__':
    main()
