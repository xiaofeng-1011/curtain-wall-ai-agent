"""Portable, verified built-in standards; never modify installed resources.

The durable pending marker makes a multi-database WAL commit recoverable after
power loss. Deployment replaces only the reserved collection, in a transaction;
custom collections and their FTS rows are never deleted or overwritten.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from storage.standard_library import AssetError, canonical, contained_path, sha256_file

BUILTIN_COLLECTION_ID = 'builtin-curtain-standards'
RESOURCE_PREFIX = 'resource://builtin-standards/'
FORMAT_VERSION = 1


@contextmanager
def _deployment_lock(library, progress=None, timeout=120):
    """Persistent OS lock, shared by all app sessions and processes."""
    path = library.directory / 'builtin-deployment.lock'
    with path.open('a+b') as handle:
        if path.stat().st_size == 0:
            handle.write(b'0')
            handle.flush()
        started, announced, acquired = time.monotonic(), False, False
        while not acquired:
            handle.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as exc:
                if time.monotonic() - started >= timeout:
                    raise TimeoutError('另一窗口正在部署规范库，请稍后重试。') from exc
                if progress and not announced:
                    progress(0, 1, '等待另一窗口完成规范库部署')
                    announced = True
                time.sleep(0.1)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def find_bundle():
    override = os.environ.get('MW_STANDARD_BUNDLE')
    return Path(override).resolve() if override else Path(__file__).resolve().parents[1] / 'resources' / 'standards'


def resolve_resource(value):
    if not str(value).startswith(RESOURCE_PREFIX):
        raise AssetError('无法识别的内置资料资源路径')
    return contained_path(find_bundle(), str(value)[len(RESOURCE_PREFIX):])


def _read_manifest(bundle):
    manifest = json.loads((bundle / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('format_version') != FORMAT_VERSION or manifest.get('collection_id') != BUILTIN_COLLECTION_ID:
        raise AssetError('内置规范包格式不兼容')
    expected = manifest.get('bundle_version')
    payload = {k: v for k, v in manifest.items() if k != 'bundle_version'}
    if expected != hashlib.sha256(canonical(payload).encode()).hexdigest():
        raise AssetError('内置规范包清单校验失败')
    if not {'library.db', 'search.db'}.issubset({i['path'] for i in manifest['files']}):
        raise AssetError('内置规范包缺少数据库')
    return manifest


def verify_bundle(bundle=None, progress=None):
    bundle = Path(bundle or find_bundle())
    manifest = _read_manifest(bundle)
    for number, item in enumerate(manifest['files'], 1):
        path = contained_path(bundle, item['path'])
        if not path.is_file() or path.stat().st_size != item['size'] or sha256_file(path) != item['sha256']:
            raise AssetError('内置规范资源校验失败：' + item['path'])
        if progress and (number == 1 or number % 100 == 0 or number == len(manifest['files'])):
            progress(number, len(manifest['files']), '校验内置规范资料')
    return manifest


def _installed_ok(library, manifest):
    cid = BUILTIN_COLLECTION_ID
    raw = library.db.execute('SELECT json FROM collections WHERE collection_id=?', (cid,)).fetchone()
    if not raw or not json.loads(raw[0]).get('import_complete') or json.loads(raw[0]).get('bundle_version') != manifest['bundle_version']:
        return False
    counts = manifest['counts']
    for table, key in [('standard_documents', 'documents'), ('standard_pages', 'pages')]:
        if library.db.execute(f'SELECT count(*) FROM {table} WHERE collection_id=?', (cid,)).fetchone()[0] != counts[key]:
            return False
    path = library.directory / 'search.db'
    if not path.exists():
        return False
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        state = db.execute('SELECT revision,version FROM states WHERE collection_id=?', (cid,)).fetchone()
        expected = (str(library.revision(cid)), manifest['index_version'])
        return (state == expected and db.execute('SELECT count(*) FROM chunks WHERE collection_id=?', (cid,)).fetchone()[0] == counts['chunks']
                and db.execute('SELECT count(*) FROM chunks c JOIN search s ON s.rowid=c.id WHERE c.collection_id=?', (cid,)).fetchone()[0] == counts['chunks'])
    except sqlite3.DatabaseError:
        return False
    finally:
        db.close()


def initialize_builtin(library, progress=None, repair=False):
    # Acquire the file lock before checking versions or pending markers. A
    # second process must observe the first process's completed transaction.
    try:
        with library._lock, _deployment_lock(library, progress):
            return _initialize_builtin_locked(library, progress, repair)
    except (OSError, TimeoutError) as exc:
        return {'ok': False, 'status': 'error', 'collection_id': BUILTIN_COLLECTION_ID, 'message': str(exc)}


def _initialize_builtin_locked(library, progress=None, repair=False):
    """Return ready/missing/error status; failed deployment is retried next start.

    Call before creating retriever connections. Resources are verified on each
    new library session; repeated calls reuse the successful session result.
    """
    bundle = find_bundle()
    disabled = library.directory / 'builtin-disabled.json'
    if disabled.exists() and not repair:
        if library.get_collection(BUILTIN_COLLECTION_ID):
            library.update_collection(BUILTIN_COLLECTION_ID, import_complete=False)
        return {'ok': False, 'status': 'disabled', 'message': '内置规范库已停用，可在管理规范库中恢复。', 'collection_id': BUILTIN_COLLECTION_ID}
    if not (bundle / 'manifest.json').is_file():
        if library.get_collection(BUILTIN_COLLECTION_ID):
            library.update_collection(BUILTIN_COLLECTION_ID, import_complete=False)
        return {'ok': False, 'status': 'absent', 'message': '安装包未包含内置规范库，请修复安装。', 'collection_id': BUILTIN_COLLECTION_ID}
    with library._lock:
        attached = []
        try:
            manifest = _read_manifest(bundle)
            key = (str(bundle), manifest['bundle_version'])
            pending = library.directory / 'builtin-deployment.pending'
            if not repair and not pending.exists() and getattr(library, '_builtin_verified', None) == key and _installed_ok(library, manifest):
                return dict(library._builtin_result)
            verify_bundle(bundle, progress)
            deployed = repair or pending.exists() or not _installed_ok(library, manifest)
            if deployed:
                from evidence.standard_retrieval import StandardRetriever
                retriever = StandardRetriever(library)
                retriever.close()
                # fsync before any data mutation: if WAL databases commit only
                # partly during power loss, the next run reapplies both tables.
                with pending.open('w', encoding='utf-8') as stream:
                    stream.write(manifest['bundle_version'])
                    stream.flush()
                    os.fsync(stream.fileno())
                for name, path, readonly in [('builtin_source', bundle / 'library.db', True), ('builtin_search', bundle / 'search.db', True), ('local_search', library.directory / 'search.db', False)]:
                    target = path.resolve().as_uri() + '?mode=ro' if readonly else str(path)
                    library.db.execute(f'ATTACH DATABASE ? AS {name}', (target,))
                    attached.append(name)
                cid = BUILTIN_COLLECTION_ID
                with library.tx():
                    library.db.execute('DELETE FROM local_search.search WHERE rowid IN (SELECT id FROM local_search.chunks WHERE collection_id=?)', (cid,))
                    library.db.execute('DELETE FROM local_search.chunks WHERE collection_id=?', (cid,))
                    library.db.execute('DELETE FROM local_search.states WHERE collection_id=?', (cid,))
                    for table in ('collections', 'standard_documents', 'standard_pages', 'standard_index_state'):
                        library.db.execute(f'DELETE FROM main.{table} WHERE collection_id=?', (cid,))
                        library.db.execute(f'INSERT INTO main.{table} SELECT * FROM builtin_source.{table} WHERE collection_id=?', (cid,))
                    raw = library.db.execute('SELECT json FROM collections WHERE collection_id=?', (cid,)).fetchone()
                    if not raw:
                        raise AssetError('规范包缺少内置资料集')
                    collection = json.loads(raw[0])
                    collection['bundle_version'] = manifest['bundle_version']
                    library.db.execute('UPDATE collections SET json=? WHERE collection_id=?', (canonical(collection), cid))
                    # Shift IDs beyond every surviving user FTS row.
                    offset = library.db.execute('SELECT coalesce(max(id),0) FROM local_search.chunks').fetchone()[0]
                    library.db.execute('INSERT INTO local_search.chunks SELECT id+?,collection_id,doc_id,ordinal,json FROM builtin_search.chunks WHERE collection_id=?', (offset, cid))
                    library.db.execute('INSERT INTO local_search.search(rowid,terms) SELECT s.rowid+?,s.terms FROM builtin_search.search s JOIN builtin_search.chunks c ON c.id=s.rowid WHERE c.collection_id=?', (offset, cid))
                    library.db.execute('INSERT INTO local_search.states SELECT * FROM builtin_search.states WHERE collection_id=?', (cid,))
                for name in reversed(attached):
                    library.db.execute(f'DETACH DATABASE {name}')
                attached.clear()
                if not _installed_ok(library, manifest):
                    raise AssetError('内置规范部署后校验失败，将在下次启动重试')
                pending.unlink()
            library._asset_hash_cache.clear()
            if repair:
                disabled.unlink(missing_ok=True)
            result = {'ok': True, 'status': 'ready', 'deployed': deployed, 'collection_id': BUILTIN_COLLECTION_ID,
                      'bundle_version': manifest['bundle_version'], 'counts': manifest['counts'], 'message': '内置规范库已就绪'}
            library._builtin_verified, library._builtin_result = key, result
            return result
        except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
            # Prevent retrieval from a partially committed or damaged built-in
            # collection, while leaving all custom registrations available.
            if library.db.in_transaction:
                library.db.rollback()
            raw = library.db.execute('SELECT json FROM collections WHERE collection_id=?', (BUILTIN_COLLECTION_ID,)).fetchone()
            if raw:
                library.update_collection(BUILTIN_COLLECTION_ID, import_complete=False)
            return {'ok': False, 'status': 'error', 'collection_id': BUILTIN_COLLECTION_ID, 'message': str(exc)}
        finally:
            for name in reversed(attached):
                try:
                    library.db.execute(f'DETACH DATABASE {name}')
                except sqlite3.Error:
                    pass


def default_collection_ids(library):
    return [c['collection_id'] for c in library.list_collections() if c.get('import_complete')]


def remove_builtin(library):
    try:
        with library._lock, _deployment_lock(library):
            return _remove_builtin_locked(library)
    except (OSError, TimeoutError) as exc:
        return {'ok': False, 'status': 'error', 'collection_id': BUILTIN_COLLECTION_ID, 'message': str(exc)}


def _remove_builtin_locked(library):
    """Disable automatic reinstall and remove only built-in local registrations."""
    cid = BUILTIN_COLLECTION_ID
    with library._lock:
        marker = library.directory / 'builtin-disabled.json'
        with marker.open('w', encoding='utf-8') as stream:
            stream.write(canonical({'disabled': True, 'collection_id': cid}))
            stream.flush()
            os.fsync(stream.fileno())
        library._builtin_verified = None
        if library.get_collection(cid):
            library.update_collection(cid, import_complete=False)
        search = library.directory / 'search.db'
        attached = False
        try:
            if search.exists():
                library.db.execute('ATTACH DATABASE ? AS builtin_remove_search', (str(search),))
                attached = True
            with library.tx():
                if attached:
                    library.db.execute('DELETE FROM builtin_remove_search.search WHERE rowid IN (SELECT id FROM builtin_remove_search.chunks WHERE collection_id=?)', (cid,))
                    library.db.execute('DELETE FROM builtin_remove_search.chunks WHERE collection_id=?', (cid,))
                    library.db.execute('DELETE FROM builtin_remove_search.states WHERE collection_id=?', (cid,))
                for table in ('standard_pages', 'standard_documents', 'standard_index_state', 'collections'):
                    library.db.execute(f'DELETE FROM {table} WHERE collection_id=?', (cid,))
        finally:
            if attached:
                library.db.execute('DETACH DATABASE builtin_remove_search')
        return {'ok': True, 'status': 'disabled', 'collection_id': cid, 'message': '已移除本机内置规范索引，个人规范库已保留。'}
