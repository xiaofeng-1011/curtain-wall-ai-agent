from agent.assistant import Assistant
from storage.workspace import Workspace


def collection(ws, tmp_path, name):
    return ws.standard_library().ensure_collection(name, tmp_path/name, tmp_path/(name+'-ocr'))


def test_qa_defaults_and_persists_user_scope(tmp_path):
    ws = Workspace(tmp_path/'user')
    one = collection(ws, tmp_path, 'one')
    two = collection(ws, tmp_path, 'two')
    a = Assistant(ws)
    c = a.new_conversation(mode='standard_qa')
    assert c['qa_options']['collection_ids'] == [one]
    a.set_qa_options(c['conversation_id'], dict(c['qa_options'], collection_ids=[two]))
    assert a.new_conversation(mode='standard_qa')['qa_options']['collection_ids'] == [two]
    a.set_qa_options(c['conversation_id'], dict(c['qa_options'], collection_ids=[]))
    assert Assistant(Workspace(ws.root)).new_conversation(mode='standard_qa')['qa_options']['collection_ids'] == []


def test_empty_session_gets_new_collection_unless_user_chose_empty(tmp_path):
    ws = Workspace(tmp_path/'user'); a = Assistant(ws)
    c = a.new_conversation(mode='standard_qa')
    assert not c['qa_options']['collection_ids']
    one = collection(ws, tmp_path, 'one')
    c = a.ensure_qa_selection(c['conversation_id'])
    assert c['qa_options']['collection_ids'] == [one]
    a.set_qa_options(c['conversation_id'], dict(c['qa_options'], collection_ids=[]))
    assert a.ensure_qa_selection(c['conversation_id'])['qa_options']['collection_ids'] == []


def test_clear_business_preserves_standard_registration(tmp_path):
    ws = Workspace(tmp_path/'user')
    cid = collection(ws, tmp_path, 'one')
    Assistant(ws).new_conversation(mode='standard_qa')
    ws.clear_workspace()
    assert ws.counts()['conversations'] == 0
    assert ws.standard_library().get_collection(cid)


def test_rendering_missing_collection_does_not_overwrite_choice(tmp_path):
    from streamlit.testing.v1 import AppTest
    ws = Workspace(tmp_path/'user'); a = Assistant(ws)
    cid = collection(ws,tmp_path,'one')
    c = a.new_conversation(mode='standard_qa')
    a.set_qa_options(c['conversation_id'], dict(c['qa_options'],collection_ids=[cid]))
    ws.standard_library().clear()
    script = '''
from storage.workspace import Workspace
from agent.assistant import Assistant
from ui.standards import controls
ws = Workspace(ROOT)
a = Assistant(ws)
controls(ws,a,ws.get_conversation(CONV))
'''.replace('ROOT',repr(str(ws.root))).replace('CONV',repr(c['conversation_id']))
    app=AppTest.from_string(script).run()
    assert not app.exception
    assert ws.get_setting('standard_collection_selection') == [cid]
    assert a.new_conversation(mode='standard_qa')['qa_options']['collection_ids'] == [cid]


def test_home_read_documents_opens_standard_qa_without_empty_upload_hint(tmp_path, monkeypatch):
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    from storage import workspace
    from ui.chat import services
    monkeypatch.setenv('MW_STANDARD_BUNDLE', str(tmp_path / 'no-release-resources'))
    monkeypatch.setenv('MW_FORCE_FALLBACK', '1')
    monkeypatch.setattr(workspace, 'DEFAULT_DIR', tmp_path / 'user')
    services.clear()
    ws, _ = services()
    cid = collection(ws, tmp_path, 'available-reference')
    ws.standard_library().update_collection(cid, import_complete=True)
    try:
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'app.py'), default_timeout=15).run()
        assert not app.exception
        next(button for button in app.button if button.label == '查阅资料').click().run()
        assert not app.exception
        conv = ws.get_conversation(app.session_state['conv_id'])
        assert conv['mode'] == 'standard_qa'
        assert conv['qa_options']['collection_ids'] == [cid]
        assert any(widget.label == '资料集' for widget in app.multiselect)
        assert not any('暂无可检索资料' in caption.value for caption in app.caption)
        assert not any(widget.label == 'API Key' for widget in app.text_input)
    finally:
        ws.standard_library().db.close()
        ws.db.close()
        services.clear()
