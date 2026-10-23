"""Build a relocatable standards-only release from a consistent SQLite snapshot.

Usage: python tools/build_standard_bundle.py --library .../standards/library.db
       --source-root ... --package-root ... --output resources/standards
All inputs are read-only. Only files explicitly referenced by source_manifest
are copied; chat history, credentials and unrelated workspace data are excluded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from storage.standard_library import StandardLibrary, AssetError, canonical, contained_path, sha256_file, stat_signature
from storage.bundled_standards import BUILTIN_COLLECTION_ID, RESOURCE_PREFIX, FORMAT_VERSION, verify_bundle
from evidence.standard_retrieval import StandardRetriever, INDEX_VERSION


def sanitize(value, roots):
    """Recursively replace provenance machine paths, including embedded strings."""
    if isinstance(value, dict):
        return {sanitize(k, roots): sanitize(v, roots) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v, roots) for v in value]
    if isinstance(value, str):
        text = value
        for root, replacement in sorted(roots, key=lambda p: -len(p[0])):
            for spelling in (root, root.replace('\\', '/'), root.replace('/', '\\')):
                text = text.replace(spelling, replacement)
        # Remaining absolute developer paths (logs/tool commands/cache entries)
        # are provenance only, never runtime resources. Remove the whole token.
        text = re.sub(r'(?i)(?<![a-z0-9_])(?:[a-z]:[\\/]|\\\\)[^\r\n\t"<>|]*', '[构建环境路径已移除]', text)
        return text
    return value


def _snapshot(source, target):
    original = sqlite3.connect(Path(source).resolve().as_uri() + '?mode=ro', uri=True)
    output = sqlite3.connect(target)
    try:
        original.backup(output)
    finally:
        output.close()
        original.close()


def build_bundle(library_path, source_root, package_root, output, progress=None):
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('输出目录已存在，请使用新的输出目录以保留已有发布包')
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.standards-build-', dir=output.parent))
    library = retriever = snapshot = None
    try:
        snapshot_path = stage / 'source-snapshot.db'
        _snapshot(library_path, snapshot_path)
        snapshot = sqlite3.connect(snapshot_path)
        rows = snapshot.execute('SELECT collection_id,revision,json FROM collections').fetchall()
        if len(rows) != 1:
            raise ValueError('构建源必须恰有一个规范集合，避免误带用户资料')
        old_id, revision, raw = rows[0]
        collection = json.loads(raw)
        if not collection.get('import_complete'):
            raise ValueError('源规范库导入未完成')
        roots = [(str(Path(source_root).resolve()), RESOURCE_PREFIX + 'source'),
                 (str(Path(package_root).resolve()), RESOURCE_PREFIX + 'package'),
                 (collection['source_root'], RESOURCE_PREFIX + 'source'),
                 (collection['package_root'], RESOURCE_PREFIX + 'package')]
        resource_roots = {'source_root': Path(source_root).resolve(), 'package_root': Path(package_root).resolve()}
        resource_dirs = {'source_root': 'source', 'package_root': 'package'}
        assets, source_manifest = [], []
        references = collection.get('source_manifest', [])
        if not references:
            raise ValueError('源规范库缺少依赖清单')
        for number, item in enumerate(references, 1):
            root_key, relative = item['root'], item['relative_path']
            source = contained_path(resource_roots[root_key], relative)
            if root_key == 'package_root':
                # Legacy imports recorded dependency stats; reject changed OCR
                # instead of combining an old index with newly edited evidence.
                if 'sha256' in item:
                    actual = sha256_file(source) if source.is_file() else None
                    unchanged = actual == item['sha256']
                else:
                    unchanged = stat_signature(source) == item.get('signature')
                if not unchanged:
                    raise AssetError('OCR依赖已变化，请先重新导入源库：' + relative)
            record = {'root': root_key, 'relative_path': relative}
            if not source.is_file():
                if item.get('signature') is not None or item.get('sha256') is not None:
                    raise AssetError('依赖文件缺失：' + relative)
                record.update(sha256=None, size=None)
            else:
                destination = contained_path(stage, resource_dirs[root_key] + '/' + relative)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if root_key == 'package_root' and source.suffix.lower() in ('.json', '.txt', '.csv', '.md'):
                    text = source.read_text(encoding='utf-8-sig')
                    if source.suffix.lower() == '.json':
                        destination.write_text(json.dumps(sanitize(json.loads(text), roots), ensure_ascii=False, indent=2), encoding='utf-8')
                    else:
                        destination.write_text(sanitize(text, roots), encoding='utf-8')
                else:
                    shutil.copyfile(source, destination)
                digest = sha256_file(destination)
                if root_key == 'source_root' and digest != sha256_file(source):
                    raise AssetError('原始PDF复制校验失败：' + relative)
                record.update(sha256=digest, size=destination.stat().st_size)
                assets.append({'path': destination.relative_to(stage).as_posix(), 'size': destination.stat().st_size, 'sha256': digest})
            source_manifest.append(record)
            if progress and (number == 1 or number % 100 == 0 or number == len(references)):
                progress(number, len(references), '复制规范依赖')
        library = StandardLibrary(stage / 'working')
        new_collection = sanitize(collection, roots)
        new_collection.update(collection_id=BUILTIN_COLLECTION_ID, name='内置幕墙规范库',
                              source_root=RESOURCE_PREFIX + 'source', package_root=RESOURCE_PREFIX + 'package',
                              builtin=True, source_manifest=source_manifest, import_complete=True)
        with library.tx():
            library.db.execute('INSERT INTO collections VALUES(?,?,?)', (BUILTIN_COLLECTION_ID, revision, canonical(new_collection)))
            for doc_id, fingerprint, raw in snapshot.execute('SELECT doc_id,fingerprint,json FROM standard_documents WHERE collection_id=?', (old_id,)):
                doc = sanitize(json.loads(raw), roots)
                original = contained_path(stage / 'source', doc['relative_path'])
                if sha256_file(original) != doc['sha256']:
                    raise AssetError('原件与库内登记SHA-256不一致：' + doc['relative_path'])
                # Fingerprints describe the portable published document content.
                doc.pop('fingerprint', None)
                fingerprint = hashlib.sha256(canonical(doc).encode()).hexdigest()
                doc['fingerprint'] = fingerprint
                library.db.execute('INSERT INTO standard_documents VALUES(?,?,?,?)', (BUILTIN_COLLECTION_ID, doc_id, fingerprint, canonical(doc)))
            for doc_id, page, raw in snapshot.execute('SELECT doc_id,page,json FROM standard_pages WHERE collection_id=?', (old_id,)):
                record = sanitize(json.loads(raw), roots)
                record['collection_id'] = BUILTIN_COLLECTION_ID
                # text_sha256 is a derivative checksum, so track the sanitized
                # release text; original PDF SHA-256 remains strictly unchanged.
                doc_path = library.get_document(BUILTIN_COLLECTION_ID, doc_id)['relative_path']
                text_path = stage / 'package' / '逐页文本' / Path(doc_path).with_suffix('') / f'原始PDF页_{page:04}.txt'
                if text_path.is_file():
                    record['text_sha256'] = hashlib.sha256(text_path.read_text(encoding='utf-8-sig').encode()).hexdigest()
                library.db.execute('INSERT INTO standard_pages VALUES(?,?,?,?)', (BUILTIN_COLLECTION_ID, doc_id, page, canonical(record)))
        snapshot.close()
        snapshot = None
        snapshot_path.unlink()
        # Exactly one index construction, at release time; consumer startup only
        # installs the indexed rows and never re-runs parsing or chunking.
        retriever = StandardRetriever(library)
        index_state = retriever.build(BUILTIN_COLLECTION_ID)
        retriever.close()
        retriever = None
        library.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        library.db.close()
        library = None
        for name in ('library.db', 'search.db'):
            _snapshot(stage / 'working' / 'standards' / name, stage / name)
            db = sqlite3.connect(stage / name)
            try:
                db.execute('PRAGMA journal_mode=DELETE')
                if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise AssetError('发布数据库完整性检查失败：' + name)
            finally:
                db.close()
            assets.append({'path': name, 'size': (stage / name).stat().st_size, 'sha256': sha256_file(stage / name)})
        # Only this validated staging subtree is removed; no input path is moved.
        shutil.rmtree(stage / 'working')
        manifest = {'format_version': FORMAT_VERSION, 'collection_id': BUILTIN_COLLECTION_ID,
                    'index_version': INDEX_VERSION, 'counts': {'documents': new_collection['stats']['documents'],
                    'pages': new_collection['stats']['pages'], 'chunks': index_state['chunks']},
                    'files': sorted(assets, key=lambda x: x['path'])}
        manifest['bundle_version'] = hashlib.sha256(canonical(manifest).encode()).hexdigest()
        (stage / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        verify_bundle(stage, progress)
        stage.rename(output)
        return manifest
    finally:
        if retriever:
            retriever.close()
        if library:
            library.db.close()
        if snapshot:
            snapshot.close()
        # Keep failed staging output for diagnosis; a later build uses a fresh
        # unique stage and the last complete release remains untouched.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', required=True)
    parser.add_argument('--source-root', required=True)
    parser.add_argument('--package-root', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    result = build_bundle(args.library, args.source_root, args.package_root, args.output,
                          lambda n, total, text: print(f'{text}: {n}/{total}', flush=True))
    print(json.dumps({'bundle_version': result['bundle_version'], 'counts': result['counts']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
