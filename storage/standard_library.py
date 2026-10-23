"""Independent, explicitly imported standards collection; never writes source assets."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def sha256_file(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def contained_path(root, relative):
    """Reject absolute paths, traversal, and symlinks escaping the registered root."""
    root = Path(root).resolve()
    rel = Path(str(relative).replace('\\', '/'))
    if rel.anchor or rel.is_absolute() or '..' in rel.parts or ':' in str(rel):
        raise AssetError('资料路径不在登记根目录内')
    candidate = root / rel
    target = candidate.resolve()
    if not _comparison_path(target).is_relative_to(_comparison_path(root)):
        raise AssetError('资料路径不在登记根目录内')
    # Keep the caller's relative spelling for manifest round-tripping. Windows
    # resolve() may add an extended-path prefix and trim a directory's trailing
    # spaces even when the final artifact does not exist. Containment above still
    # checks the fully resolved destination, including symlinks/junctions.
    return candidate


def _comparison_path(path):
    value = str(path)
    if os.name == 'nt':
        if value.startswith('\\\\?\\UNC\\'):
            value = '\\\\' + value[8:]
        elif value.startswith('\\\\?\\'):
            value = value[4:]
    return Path(value)


def stat_signature(path):
    try:
        s = Path(path).stat()
        return [s.st_size, s.st_mtime_ns]
    except OSError:
        return None


class AssetError(ValueError):
    pass


class StandardLibrary:
    def __init__(self, root):
        self.root = Path(root)
        self.directory = self.root / 'standards'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.directory / 'library.db', check_same_thread=False, isolation_level=None)
        self.db.execute('PRAGMA journal_mode=WAL')
        self._lock = threading.RLock()
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS collections(collection_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS standard_documents(collection_id TEXT NOT NULL,doc_id TEXT NOT NULL,fingerprint TEXT NOT NULL,json TEXT NOT NULL,PRIMARY KEY(collection_id,doc_id));
            CREATE TABLE IF NOT EXISTS standard_pages(collection_id TEXT NOT NULL,doc_id TEXT NOT NULL,page INTEGER NOT NULL,json TEXT NOT NULL,PRIMARY KEY(collection_id,doc_id,page));
            CREATE TABLE IF NOT EXISTS standard_index_state(collection_id TEXT PRIMARY KEY,json TEXT NOT NULL);
        ''')

    @contextmanager
    def tx(self):
        with self._lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise

    def ensure_collection(self, name, source_root, package_root):
        source_root, package_root = str(Path(source_root).resolve()), str(Path(package_root).resolve())
        cid = hashlib.sha256(canonical([source_root, package_root]).encode()).hexdigest()[:24]
        with self.tx():
            self.db.execute('INSERT OR IGNORE INTO collections VALUES(?,0,?)',
                            (cid, canonical(dict(collection_id=cid, name=name, source_root=source_root, package_root=package_root, import_complete=False))))
        return cid

    def get_collection(self, collection_id):
        row = self.db.execute('SELECT revision,json FROM collections WHERE collection_id=?', (collection_id,)).fetchone()
        return dict(json.loads(row[1]), revision=row[0]) if row else None

    def list_collections(self):
        return [dict(json.loads(row[1]), revision=row[0]) for row in self.db.execute('SELECT revision,json FROM collections ORDER BY rowid')]

    def revision(self, collection_id):
        collection = self.get_collection(collection_id)
        return collection['revision'] if collection else None

    def update_collection(self, collection_id, **values):
        with self.tx():
            collection = self.get_collection(collection_id)
            if collection is None:
                raise KeyError(collection_id)
            collection.pop('revision', None)
            collection.update(values)
            self.db.execute('UPDATE collections SET json=? WHERE collection_id=?', (canonical(collection), collection_id))

    def upsert_document(self, collection_id, document, pages):
        """Commit one complete document and its pages atomically, no-op on equal content."""
        document = dict(document)
        doc_id = document['doc_id']
        fingerprint = hashlib.sha256(canonical([document, pages]).encode()).hexdigest()
        with self.tx():
            old = self.db.execute('SELECT fingerprint FROM standard_documents WHERE collection_id=? AND doc_id=?', (collection_id, doc_id)).fetchone()
            if old and old[0] == fingerprint:
                return False
            document['fingerprint'] = fingerprint
            self.db.execute('INSERT OR REPLACE INTO standard_documents VALUES(?,?,?,?)', (collection_id, doc_id, fingerprint, canonical(document)))
            self.db.execute('DELETE FROM standard_pages WHERE collection_id=? AND doc_id=?', (collection_id, doc_id))
            self.db.executemany('INSERT INTO standard_pages VALUES(?,?,?,?)', [(collection_id, doc_id, p['page'], canonical(dict(p, doc_id=doc_id, collection_id=collection_id))) for p in pages])
            self.db.execute('UPDATE collections SET revision=revision+1 WHERE collection_id=?', (collection_id,))
        return True

    def remove_absent_documents(self, collection_id, keep_ids):
        """Only remove rows absent from a fully read current manifest, never source files."""
        with self.tx():
            absent = [d['doc_id'] for d in self.documents(collection_id) if d['doc_id'] not in keep_ids]
            for did in absent:
                self.db.execute('DELETE FROM standard_documents WHERE collection_id=? AND doc_id=?', (collection_id, did))
                self.db.execute('DELETE FROM standard_pages WHERE collection_id=? AND doc_id=?', (collection_id, did))
            if absent:
                self.db.execute('UPDATE collections SET revision=revision+1 WHERE collection_id=?', (collection_id,))

    def documents(self, collection_id):
        return [json.loads(r[0]) for r in self.db.execute('SELECT json FROM standard_documents WHERE collection_id=? ORDER BY rowid', (collection_id,))]

    def get_document(self, collection_id, doc_id):
        row = self.db.execute('SELECT json FROM standard_documents WHERE collection_id=? AND doc_id=?', (collection_id, doc_id)).fetchone()
        return json.loads(row[0]) if row else None

    def pages(self, collection_id, doc_id=None):
        sql, args = 'SELECT json FROM standard_pages WHERE collection_id=?', [collection_id]
        if doc_id is not None:
            sql += ' AND doc_id=?'
            args.append(doc_id)
        return [json.loads(r[0]) for r in self.db.execute(sql + ' ORDER BY doc_id,page', args)]

    def get_page(self, collection_id, doc_id, page):
        row = self.db.execute('SELECT json FROM standard_pages WHERE collection_id=? AND doc_id=? AND page=?', (collection_id, doc_id, page)).fetchone()
        return json.loads(row[0]) if row else None

    def source_status(self, collection_id):
        collection = self.get_collection(collection_id)
        if not collection:
            return {'stale': True, 'changes': ['资料集不存在']}
        changes = []
        if not collection.get('import_complete'):
            changes.append('导入尚未完整结束')
        for item in collection.get('source_manifest', []):
            try:
                path = contained_path(collection[item['root']], item['relative_path'])
                if stat_signature(path) != item['signature']:
                    changes.append(item['relative_path'])
            except (OSError, AssetError):
                changes.append(item['relative_path'])
        return {'stale': bool(changes), 'changes': changes, 'revision': collection['revision']}

    def verify_asset(self, collection_id, doc_id):
        doc, collection = self.get_document(collection_id, doc_id), self.get_collection(collection_id)
        if not doc or not collection:
            return {'ok': False, 'reason': '资料未登记或不属于该资料集'}
        try:
            path = contained_path(collection['source_root'], doc['relative_path'])
            if sha256_file(path) != doc['sha256']:
                raise AssetError('原始文件SHA-256已变化，请重新导入')
            return {'ok': True, 'sha256': doc['sha256']}
        except (OSError, AssetError) as e:
            return {'ok': False, 'reason': str(e)}

    def pdf_bytes(self, collection_id, doc_id):
        doc, collection = self.get_document(collection_id, doc_id), self.get_collection(collection_id)
        if not doc or not collection:
            raise AssetError('资料未登记或不属于该资料集')
        try:
            data = contained_path(collection['source_root'], doc['relative_path']).read_bytes()
        except OSError as e:
            raise AssetError(str(e)) from e
        if hashlib.sha256(data).hexdigest() != doc['sha256']:
            raise AssetError('原始文件SHA-256已变化，请重新导入')
        return data

    def page_png(self, collection_id, doc_id, page, *, dpi=110):
        import fitz
        if not isinstance(page, int) or page < 1 or not self.get_page(collection_id, doc_id, page):
            raise AssetError('原始PDF页码未登记')
        with fitz.open(stream=self.pdf_bytes(collection_id, doc_id), filetype='pdf') as pdf:
            if page > len(pdf):
                raise AssetError('原始PDF页码超出范围')
            return pdf[page - 1].get_pixmap(dpi=max(36, min(180, int(dpi))), alpha=False).tobytes('png')

    def get_index_state(self, collection_id):
        row = self.db.execute('SELECT json FROM standard_index_state WHERE collection_id=?', (collection_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_index_state(self, collection_id, state):
        with self.tx():
            self.db.execute('INSERT OR REPLACE INTO standard_index_state VALUES(?,?)', (collection_id, canonical(state)))

    def index_stale(self, collection_id, index_version):
        state = self.get_index_state(collection_id) or {}
        return state.get('revision') != self.revision(collection_id) or state.get('index_version') != index_version or self.source_status(collection_id)['stale']

    def clear(self):
        """Clear local registrations and indexes only; never follow source roots."""
        before = {key: self.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                  for key, table in (('collections', 'collections'), ('documents', 'standard_documents'), ('pages', 'standard_pages'))}
        # Clear the existing FTS database through SQL, including when UI readers
        # still hold a connection. Do not unlink live SQLite/WAL files on Windows.
        search_path = self.directory / 'search.db'
        if search_path.is_file():
            connection = sqlite3.connect(search_path, timeout=30)
            try:
                with connection:
                    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    for table in ('search', 'chunks', 'states'):
                        if table in tables:
                            connection.execute(f'DELETE FROM {table}')
            finally:
                connection.close()
        with self.tx():
            for table in ('standard_pages', 'standard_documents', 'standard_index_state', 'collections'):
                self.db.execute(f'DELETE FROM {table}')
        problems = []
        # Fixed local artifact names only; no recursion and no source-derived paths.
        for name in ('import_report.json', 'import_report.json.tmp', 'import_cli.log', 'quality_report.json', 'quality_report.md'):
            path = self.directory / name
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                problems.append(f'{name}: {exc}')
        return dict(before, problems=problems)
