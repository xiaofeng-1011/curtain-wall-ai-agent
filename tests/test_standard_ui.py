from pathlib import Path

from streamlit.testing.v1 import AppTest
from tests.test_ocr_library import package


def test_standard_entry_renders_without_project(tmp_path, monkeypatch):
    monkeypatch.setenv('MW_RUNTIME_DIR',str(tmp_path))
    monkeypatch.setenv('MW_FORCE_FALLBACK','1')
    from storage import workspace
    monkeypatch.setattr(workspace,'DEFAULT_DIR',tmp_path)
    from ui.chat import services
    services.clear()
    app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app.py')).run()
    assert not app.exception
    next(b for b in app.button if b.label=='规范问答').click().run()
    assert not app.exception
    assert app.multiselect[0].label=='资料集'
    assert any(r.label=='问答方式' for r in app.radio)
    assert not any(b.label=='方案参数' for b in app.button)


def test_citation_opens_registered_original(tmp_path, monkeypatch, package):
    from storage import workspace
    from ui.chat import services
    from agent.ocr_import import import_ocr_package
    from evidence.standard_retrieval import StandardRetriever
    from agent.assistant import Assistant
    root=tmp_path/'app_workspace'
    monkeypatch.setattr(workspace,'DEFAULT_DIR',root)
    monkeypatch.setenv('MW_FORCE_FALLBACK','1')
    services.clear()
    ws,asst=services(); lib=ws.standard_library()
    source,output,_=package
    cid=import_ocr_package(lib,source,output)['collection_id']
    r=StandardRetriever(lib);r.build(cid);r.close()
    conv=asst.new_conversation(mode='standard_qa')
    asst.set_qa_options(conv['conversation_id'],dict(conv['qa_options'],collection_ids=[cid]))
    asst.send(conv['conversation_id'],'玻璃幕墙')
    app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app.py'), default_timeout=20)
    app.session_state['conv_id']=conv['conversation_id'];app.run()
    assert not app.exception
    next(b for b in app.button if b.label=='查看原始PDF第 1 页').click().run()
    assert not app.exception
    assert any(n.label=='原始PDF页码' and n.value==1 for n in app.number_input)
    assert app.get('imgs') or app.get('image')
