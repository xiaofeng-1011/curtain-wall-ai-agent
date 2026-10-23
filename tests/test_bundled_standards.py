import importlib.util
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
import threading
from unittest.mock import patch

from storage.standard_library import StandardLibrary, sha256_file, stat_signature
from evidence.standard_retrieval import StandardRetriever


class BundleAvailabilityTest(unittest.TestCase):
    def test_bundle_installer_available(self):
        self.assertIsNotNone(importlib.util.find_spec('storage.bundled_standards'))


class BundleBehaviorTest(unittest.TestCase):
    def setUp(self):
        from tools.build_standard_bundle import build_bundle
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source, self.package = self.root / 'originals', self.root / 'ocr'
        self.source.mkdir()
        self.package.mkdir()
        self.pdf = self.source / 'standard.pdf'
        self.pdf.write_bytes(b'%PDF-preserved-original-fixture')
        self.doc_id = sha256_file(self.pdf)
        (self.package / 'text.txt').write_text('1.1 幕墙应满足安全要求。', encoding='utf-8')
        source_library = StandardLibrary(self.root / 'source-library')
        self.original_id = source_library.ensure_collection('Example', self.source, self.package)
        self.add_doc(source_library, self.original_id)
        source_library.update_collection(self.original_id, import_complete=True,
            stats={'documents': 1, 'pages': 1}, source_manifest=[
                {'root': 'source_root', 'relative_path': 'standard.pdf', 'signature': stat_signature(self.pdf)},
                {'root': 'package_root', 'relative_path': 'text.txt', 'signature': stat_signature(self.package / 'text.txt')},
                {'root': 'package_root', 'relative_path': 'missing.json', 'signature': None}])
        self.bundle = self.root / 'bundle'
        # Build while the source WAL connection remains open: backup must include
        # committed data that is not present in the main database alone.
        self.manifest = build_bundle(source_library.directory / 'library.db', self.source, self.package, self.bundle)
        source_library.db.close()
        self.env = patch.dict(os.environ, {'MW_STANDARD_BUNDLE': str(self.bundle)})
        self.env.start()
        self.library = StandardLibrary(self.root / 'user')

    def tearDown(self):
        self.library.db.close()
        self.env.stop()
        self.temp.cleanup()

    def add_doc(self, library, cid):
        library.upsert_document(cid, {'doc_id': self.doc_id, 'sha256': self.doc_id,
            'relative_path': 'standard.pdf', 'filename': 'standard.pdf', 'page_count': 1,
            'status': 'processed', 'standard_code': 'GB 12345-2024', 'title': '安全规范',
            'completion_record': {'source': str(self.pdf)}},
            [{'page': 1, 'text': '1.1 幕墙应满足安全要求。', 'quality': 'reference', 'issues': []}])

    def test_installs_prebuilt_index_and_preserves_custom_collection(self):
        from storage.bundled_standards import initialize_builtin, BUILTIN_COLLECTION_ID
        custom = self.library.ensure_collection('Personal', self.source, self.package)
        self.add_doc(self.library, custom)
        self.library.update_collection(custom, import_complete=True)
        retriever = StandardRetriever(self.library)
        retriever.build(custom)
        before = retriever.db.execute('SELECT * FROM chunks WHERE collection_id=?', (custom,)).fetchall()
        retriever.close()
        with patch.object(StandardRetriever, 'build', side_effect=AssertionError('startup must not build index')):
            result = initialize_builtin(self.library)
            self.assertTrue(result['ok'], result)
            self.assertTrue(result['deployed'])
            self.assertTrue(initialize_builtin(self.library, repair=True)['ok'])
        retriever = StandardRetriever(self.library)
        try:
            self.assertEqual(before, retriever.db.execute('SELECT * FROM chunks WHERE collection_id=?', (custom,)).fetchall())
            self.assertTrue(retriever.search('幕墙安全', [BUILTIN_COLLECTION_ID])['evidence'])
        finally:
            retriever.close()
        self.assertEqual(self.library.pdf_bytes(BUILTIN_COLLECTION_ID, self.doc_id), self.pdf.read_bytes())

    def test_resource_relocation_and_timestamps_are_portable(self):
        from storage.bundled_standards import initialize_builtin, BUILTIN_COLLECTION_ID
        self.assertTrue(initialize_builtin(self.library)['ok'])
        moved = self.root / 'new-installation'
        self.bundle.rename(moved)
        os.environ['MW_STANDARD_BUNDLE'] = str(moved)
        os.utime(moved / 'source' / 'standard.pdf', (1234567890, 1234567890))
        result = initialize_builtin(self.library)
        self.assertTrue(result['ok'], result)
        self.assertFalse(result['deployed'])
        self.assertFalse(self.library.source_status(BUILTIN_COLLECTION_ID)['stale'])
        raw = self.library.db.execute('SELECT json FROM collections WHERE collection_id=?', (BUILTIN_COLLECTION_ID,)).fetchone()[0]
        self.assertIn('resource://', raw)
        self.assertNotIn(str(moved), raw)

    def test_interrupted_transaction_retries_without_partial_builtin(self):
        from storage.bundled_standards import initialize_builtin, BUILTIN_COLLECTION_ID
        self.library.db.execute("CREATE TRIGGER interrupt_deploy BEFORE INSERT ON standard_pages BEGIN SELECT RAISE(ABORT, 'simulated interruption'); END")
        result = initialize_builtin(self.library)
        self.assertFalse(result['ok'])
        self.assertIsNone(self.library.get_collection(BUILTIN_COLLECTION_ID))
        self.assertTrue((self.library.directory / 'builtin-deployment.pending').exists())
        self.library.db.execute('DROP TRIGGER interrupt_deploy')
        self.assertTrue(initialize_builtin(self.library)['ok'])
        self.assertFalse((self.library.directory / 'builtin-deployment.pending').exists())

    def test_corrupt_original_is_blocked_even_with_preserved_mtime(self):
        from storage.bundled_standards import initialize_builtin, BUILTIN_COLLECTION_ID
        self.assertTrue(initialize_builtin(self.library)['ok'])
        self.assertFalse(self.library.source_status(BUILTIN_COLLECTION_ID)['stale'])
        pdf = self.bundle / 'source' / 'standard.pdf'
        stamp = pdf.stat()
        data = pdf.read_bytes()
        pdf.write_bytes(b'X' + data[1:])
        os.utime(pdf, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        self.assertTrue(self.library.source_status(BUILTIN_COLLECTION_ID)['stale'])
        self.assertFalse(self.library.verify_asset(BUILTIN_COLLECTION_ID, self.doc_id)['ok'])
        self.assertFalse(initialize_builtin(self.library, repair=True)['ok'])

    def test_disable_persists_and_explicit_repair_restores(self):
        from storage.bundled_standards import initialize_builtin, remove_builtin, BUILTIN_COLLECTION_ID
        self.assertTrue(initialize_builtin(self.library)['ok'])
        custom = self.library.ensure_collection('Personal', self.source, self.package)
        self.assertTrue(remove_builtin(self.library)['ok'])
        self.assertIsNone(self.library.get_collection(BUILTIN_COLLECTION_ID))
        self.assertIsNotNone(self.library.get_collection(custom))
        self.assertEqual('disabled', initialize_builtin(self.library)['status'])
        self.assertTrue(initialize_builtin(self.library, repair=True)['ok'])

    def test_missing_index_is_restored_on_new_session(self):
        from storage.bundled_standards import initialize_builtin
        self.assertTrue(initialize_builtin(self.library)['ok'])
        self.library.db.close()
        (self.root / 'user' / 'standards' / 'search.db').unlink()
        self.library = StandardLibrary(self.root / 'user')
        result = initialize_builtin(self.library)
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['deployed'])

    def test_new_release_upgrades_only_builtin_and_reuses_prebuilt_index(self):
        from storage.bundled_standards import initialize_builtin, BUILTIN_COLLECTION_ID
        from storage.standard_library import canonical
        self.assertTrue(initialize_builtin(self.library)['ok'])
        custom = self.library.ensure_collection('Personal', self.source, self.package)
        self.add_doc(self.library, custom)
        before = self.library.documents(custom)
        manifest = json.loads((self.bundle / 'manifest.json').read_text(encoding='utf-8'))
        manifest.pop('bundle_version')
        manifest['release_note'] = 'Version two'
        manifest['bundle_version'] = hashlib.sha256(canonical(manifest).encode()).hexdigest()
        (self.bundle / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        with patch.object(StandardRetriever, 'build', side_effect=AssertionError('must not rebuild')):
            result = initialize_builtin(self.library)
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['deployed'])
        self.assertEqual(self.library.documents(custom), before)
        self.assertEqual(self.library.get_collection(BUILTIN_COLLECTION_ID)['bundle_version'], manifest['bundle_version'])

    def test_missing_ocr_dependency_is_reported_and_blocks_builtin(self):
        from storage.bundled_standards import initialize_builtin, BUILTIN_COLLECTION_ID
        self.assertTrue(initialize_builtin(self.library)['ok'])
        (self.bundle / 'package' / 'text.txt').unlink()
        result = initialize_builtin(self.library, repair=True)
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'error')
        self.assertTrue(self.library.source_status(BUILTIN_COLLECTION_ID)['stale'])

    def test_two_library_instances_initialize_serially(self):
        from storage.bundled_standards import initialize_builtin, BUILTIN_COLLECTION_ID
        other = StandardLibrary(self.root / 'user')
        barrier = threading.Barrier(2)
        def start(library):
            barrier.wait()
            return initialize_builtin(library)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(start, [self.library, other]))
            self.assertTrue(all(result['ok'] for result in results), results)
            self.assertEqual(sum(result['deployed'] for result in results), 1, results)
            self.assertTrue(self.library.get_collection(BUILTIN_COLLECTION_ID)['import_complete'])
            self.assertTrue((self.library.directory / 'builtin-deployment.lock').exists())
        finally:
            other.db.close()

    def test_same_size_ocr_tampering_with_restored_mtime_is_detected(self):
        from storage.bundled_standards import initialize_builtin, BUILTIN_COLLECTION_ID
        self.assertTrue(initialize_builtin(self.library)['ok'])
        self.assertFalse(self.library.source_status(BUILTIN_COLLECTION_ID)['stale'])
        path = self.bundle / 'package' / 'text.txt'
        before = path.stat()
        raw = path.read_bytes()
        path.write_bytes(b'9' + raw[1:])
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertTrue(self.library.source_status(BUILTIN_COLLECTION_ID)['stale'])

    def test_relative_workspace_root_deploys_successfully(self):
        from storage.bundled_standards import initialize_builtin
        previous = Path.cwd()
        relative_library = None
        try:
            os.chdir(self.root)
            relative_library = StandardLibrary('relative user workspace')
            result = initialize_builtin(relative_library)
            self.assertTrue(result['ok'], result)
            self.assertTrue(relative_library.root.is_absolute())
        finally:
            if relative_library:
                relative_library.db.close()
            os.chdir(previous)


if __name__ == '__main__':
    unittest.main()
