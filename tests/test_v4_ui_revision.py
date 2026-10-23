"""UI revision: frozen explanations and explicit selection persistence."""
from copy import deepcopy

from streamlit.testing.v1 import AppTest

from agent.assistant import Assistant
from reporting.report import template_explanation
from storage.workspace import Workspace
from tests.chat_helpers import Chat


def render_chat(chat):
    return AppTest.from_string('''
from storage.workspace import Workspace
from agent.assistant import Assistant
from ui.chat import render_message
ws = Workspace(ROOT)
a = Assistant(ws)
c = ws.get_conversation(CID)
p = a.project_of(c)
for msg in c['messages']:
    if msg.get('kind') == 'explain':
        render_message(ws,a,c,p,msg,set())
'''.replace('ROOT', repr(str(chat.ws.root))).replace('CID', repr(chat.conv['conversation_id']))).run()


def test_structured_explanation_preserves_frozen_run_and_legacy(tmp_path, monkeypatch):
    monkeypatch.setenv('MW_FORCE_FALLBACK','1')
    chat = Chat(tmp_path/'user').new().build_t10()
    chat.say('为什么金额减少？')
    msg = deepcopy(chat.last('explain'))
    blocks = msg['card']['blocks']
    run = chat.ws.get_run(msg['card']['run_id'])
    assert blocks['main'] + blocks['limits'] == template_explanation(run)
    assert blocks['main'] in msg['text'] and blocks['limits'] in msg['text']
    assert blocks['steps'] and all(step in msg['text'] for step in blocks['steps'])
    app = render_chat(chat)
    assert not app.exception
    assert blocks['main'] in [m.value for m in app.markdown]
    assert blocks['limits'] in [m.value for m in app.caption]
    chat.say('原方案分格宽改为1.5m')
    assert chat.last('explain') == msg
    app = render_chat(chat)
    assert any('历史结果' in w.value for w in app.warning)
    assert chat.ws.get_run(msg['card']['run_id']) == run
    saved = chat.ws.get_conversation(chat.conv['conversation_id'])
    legacy = next(m for m in saved['messages'] if m['id'] == msg['id'])
    del legacy['card']['blocks']
    chat.ws.save_conversation(saved,saved['rev'])
    app = render_chat(chat)
    assert msg['text'] in [m.value for m in app.markdown]
    assert any('历史结果' in w.value for w in app.warning)


def test_missing_run_keeps_text_and_disables_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv('MW_FORCE_FALLBACK','1')
    chat = Chat(tmp_path/'user').new().build_t10()
    chat.say('为什么金额减少？')
    saved = chat.ws.get_conversation(chat.conv['conversation_id'])
    msg = next(m for m in saved['messages'] if m.get('kind') == 'explain')
    msg['card']['run_id'] = 'missing-run'
    chat.ws.save_conversation(saved,saved['rev'])
    app = render_chat(chat)
    assert not app.exception
    assert any('原计算记录已不可用' in w.value for w in app.warning)
    assert next(b for b in app.button if b.label == '查看完整计算依据').disabled


def test_native_clear_persists_only_current_and_new_conversations(tmp_path):
    ws = Workspace(tmp_path/'user'); a = Assistant(ws)
    lib = ws.standard_library()
    one = lib.ensure_collection('one',tmp_path/'one',tmp_path/'one-ocr')
    c = a.new_conversation(mode='standard_qa')
    other = a.new_conversation(mode='standard_qa')
    options = dict(c['qa_options'],standards=['TEMP-2026'],versions=['2026'],categories=['暂不可用'])
    a.set_qa_options(c['conversation_id'],options)
    script = '''
from storage.workspace import Workspace
from agent.assistant import Assistant
from ui.standards import controls
ws=Workspace(ROOT)
a=Assistant(ws)
controls(ws,a,ws.get_conversation(CID))
'''.replace('ROOT',repr(str(ws.root))).replace('CID',repr(c['conversation_id']))
    app=AppTest.from_string(script).run()
    assert not app.exception
    assert ws.get_conversation(c['conversation_id'])['qa_options'] == options
    app.multiselect[0].set_value([]).run()
    assert not app.exception
    assert ws.get_conversation(c['conversation_id'])['qa_options']['collection_ids'] == []
    assert ws.get_conversation(other['conversation_id'])['qa_options']['collection_ids'] == [one]
    assert Assistant(Workspace(ws.root)).new_conversation(mode='standard_qa')['qa_options']['collection_ids'] == []
    assert lib.get_collection(one)
    app.run()
    assert app.multiselect[0].value == []
    # Reopening a session also preserves the explicit empty selection.
    assert AppTest.from_string(script).run().multiselect[0].value == []


def test_unavailable_then_restored_does_not_erase_selection(tmp_path):
    ws = Workspace(tmp_path/'user'); a = Assistant(ws)
    lib = ws.standard_library()
    one = lib.ensure_collection('one',tmp_path/'one',tmp_path/'one-ocr')
    c = a.new_conversation(mode='standard_qa')
    script = '''
from storage.workspace import Workspace
from agent.assistant import Assistant
from ui.standards import controls
ws=Workspace(ROOT)
a=Assistant(ws)
controls(ws,a,ws.get_conversation(CID))
'''.replace('ROOT',repr(str(ws.root))).replace('CID',repr(c['conversation_id']))
    app=AppTest.from_string(script).run()
    assert app.multiselect[0].value == [one]
    lib.clear()
    app.run()
    assert not app.exception
    assert app.multiselect[0].value == []
    assert ws.get_conversation(c['conversation_id'])['qa_options']['collection_ids'] == [one]
    assert lib.ensure_collection('one',tmp_path/'one',tmp_path/'one-ocr') == one
    app.run()
    assert app.multiselect[0].value == [one]
