"""Release upgrades and interrupted recovery must preserve all user data."""
import hashlib
import json
import socket
from unittest.mock import patch

import pytest

from agent.documents import add_document
from evidence.standard_retrieval import StandardRetriever
from storage.bundled_standards import BUILTIN_COLLECTION_ID, initialize_builtin
from storage.standard_library import canonical
from tests import test_bundled_standards as bundle_fixtures
from tests.chat_helpers import Chat


@pytest.fixture
def populated_user(monkeypatch):
    monkeypatch.setenv('MW_FORCE_FALLBACK', '1')

    def deny_network(*args, **kwargs):
        raise AssertionError('Upgrade preservation tests must remain offline')

    monkeypatch.setattr(socket.socket, 'connect', deny_network)
    fixture = bundle_fixtures.BundleBehaviorTest()
    fixture.setUp()
    chat = Chat(fixture.root / 'user')
    chat.ws._standard_library = fixture.library
    try:
        chat.build_t10()
        ordinary = add_document(chat.ws, '用户项目说明.txt', '这是用户自己的工程说明，需要保留。'.encode())
        assert ordinary['ok']
        attachment = chat.ws.root / 'attachments' / chat.project()['project_id'] / '用户附件.txt'
        attachment.parent.mkdir(parents=True, exist_ok=True)
        attachment.write_bytes(b'user attachment must remain byte-identical')
        custom = fixture.library.ensure_collection('我的规范', fixture.source, fixture.package)
        fixture.add_doc(fixture.library, custom)
        fixture.library.update_collection(custom, import_complete=True)
        retriever = StandardRetriever(fixture.library)
        try:
            retriever.build(custom)
        finally:
            retriever.close()
        assert initialize_builtin(fixture.library)['ok']
        yield fixture, chat, custom, attachment
    finally:
        chat.ws.db.close()
        fixture.tearDown()


def user_state(fixture, chat, custom, attachment):
    # Compare persisted rows, including projects, conversations, runs, ordinary
    # document page text, settings, provenance snapshots, and event history.
    tables = [row[0] for row in chat.ws.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    business = {table: chat.ws.db.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
                for table in tables}
    standards = {table: fixture.library.db.execute(
        f'SELECT * FROM {table} WHERE collection_id=? ORDER BY rowid', (custom,)).fetchall()
        for table in ('collections', 'standard_documents', 'standard_pages', 'standard_index_state')}
    retriever = StandardRetriever(fixture.library)
    try:
        chunks = retriever.db.execute('SELECT * FROM chunks WHERE collection_id=? ORDER BY id', (custom,)).fetchall()
        terms = retriever.db.execute(
            'SELECT s.rowid,s.terms FROM search s JOIN chunks c ON c.id=s.rowid '
            'WHERE c.collection_id=? ORDER BY s.rowid', (custom,)).fetchall()
    finally:
        retriever.close()
    return business, standards, chunks, terms, attachment.read_bytes()


@pytest.mark.parametrize('interrupted', [False, True], ids=['upgrade', 'interrupted-upgrade-recovery'])
def test_upgrade_preserves_project_chat_documents_and_custom_library(populated_user, interrupted):
    fixture, chat, custom, attachment = populated_user
    before = user_state(*populated_user)
    assert before[0]['projects'] and before[0]['conversations'] and before[0]['documents']
    assert before[0]['runs'] and before[0]['snapshots']
    manifest_path = fixture.bundle / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest.pop('bundle_version')
    manifest['release_note'] = 'Upgrade retention test release'
    manifest['bundle_version'] = hashlib.sha256(canonical(manifest).encode()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    if interrupted:
        fixture.library.db.execute(
            "CREATE TRIGGER interrupt_builtin BEFORE INSERT ON standard_pages "
            f"WHEN NEW.collection_id='{BUILTIN_COLLECTION_ID}' "
            "BEGIN SELECT RAISE(ABORT, 'simulated upgrade interruption'); END")
        result = initialize_builtin(fixture.library)
        assert not result['ok']
        assert (fixture.library.directory / 'builtin-deployment.pending').exists()
        assert user_state(*populated_user) == before
        fixture.library.db.execute('DROP TRIGGER interrupt_builtin')
    with patch.object(StandardRetriever, 'build', side_effect=AssertionError('Upgrade must reuse the prebuilt index')):
        result = initialize_builtin(fixture.library)
    assert result['ok'] and result['deployed'], result
    assert result['bundle_version'] == manifest['bundle_version']
    assert not (fixture.library.directory / 'builtin-deployment.pending').exists()
    assert user_state(*populated_user) == before
    assert fixture.library.get_collection(BUILTIN_COLLECTION_ID)['import_complete']


def test_builtin_qa_works_without_model_keys_or_network(populated_user, monkeypatch):
    from agent import extraction
    fixture, chat, _, _ = populated_user
    for key in ('MW_FORCE_FALLBACK', 'MW_API_KEY', 'MW_MODEL_ENABLE', 'ANTHROPIC_AUTH_TOKEN',
                'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'ZHIPU_API_KEY', 'DEEPSEEK_API_KEY', 'MOONSHOT_API_KEY'):
        monkeypatch.delenv(key, raising=False)
    assert not extraction.model_available()
    conv = chat.a.new_conversation(mode='standard_qa')
    assert conv['qa_options']['collection_ids'] == [BUILTIN_COLLECTION_ID]
    conv = chat.a.send(conv['conversation_id'], '幕墙安全')
    run = chat.ws.get_run(conv['last_run_id'])
    assert run['answer']['mode'] == 'local' and run['answer']['evidence']
    options = dict(conv['qa_options'], answer_mode='answer', allow_model=True)
    chat.a.set_qa_options(conv['conversation_id'], options)
    conv = chat.a.send(conv['conversation_id'], '幕墙安全要求')
    run = chat.ws.get_run(conv['last_run_id'])
    assert run['answer']['mode'] == 'local' and run['answer']['items']
    assert '模型未配置' in run['answer']['notice']
