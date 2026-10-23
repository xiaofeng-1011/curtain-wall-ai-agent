import csv
import hashlib
import json

import fitz
import pytest


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(dict.fromkeys(k for r in rows for k in r)))
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def package(tmp_path):
    source, output = tmp_path / 'source', tmp_path / 'output'
    source.mkdir()
    pdf = fitz.open()
    for i in range(3):
        pdf.new_page().insert_text((30, 30), f'Page {i + 1}')
    data = pdf.tobytes()
    pdf.close()
    (source / 'main.pdf').write_bytes(data)
    (source / 'alias.pdf').write_bytes(data)
    failed = fitz.open()
    failed.new_page().insert_text((20, 20), 'FAILED')
    failed_data = failed.tobytes()
    failed.close()
    (source / 'failed.pdf').write_bytes(failed_data)
    digest = hashlib.sha256(data).hexdigest()
    rows = [dict(原始相对路径='main.pdf', 原始SHA256=digest, 总页数='3', 处理状态='已处理',
                 **{'标准编号候选（文件内）': 'JGJ 102-2003', '标准名称候选（文件内）': '玻璃幕墙',
                    '专业审核状态': '未复核', '现行状态': '待核实'}),
            dict(原始相对路径='alias.pdf', 原始SHA256=digest, 总页数='3', 处理状态='完全重复别名，使用主副本', 重复主文件='main.pdf'),
            dict(原始相对路径='failed.pdf', 原始SHA256=hashlib.sha256(failed_data).hexdigest(), 总页数='1', 处理状态='未处理/失败'),
            dict(原始相对路径='excluded.pdf', 原始SHA256='x', 总页数='1', 处理状态='排除（按用户指定目录）')]
    write_csv(output / '检查清单/逐文件清单.csv', rows)
    mappings = [dict(原始相对路径='main.pdf', 原始PDF页=str(i), 处理PDF页=str(i), 印刷页码候选=str(i+30),
                     印刷页码状态='未人工确认', 待人工核对='数字/单位/公式或表格页：行列、上下标、不等号及脚注需对照原页，未逐项专业复核') for i in range(1,4)]
    write_csv(output / '检查清单/逐页分类与映射汇总.csv', mappings)
    write_csv(output / '待人工核对/问题清单.csv', [dict(原始相对路径='main.pdf', 原始PDF页='2', 问题='处理前后36dpi渲染差异', 状态='待人工核对')])
    texts = output / '逐页文本/main'
    texts.mkdir(parents=True)
    for i, body in enumerate(['短条文：玻璃幕墙。', '[原始双页扫描：左页区域]\n左区条款\n[原始双页扫描：右页区域]\n右区条款', ''], 1):
        (texts / f'原始PDF页_{i:04}.txt').write_text('来源相对路径：main.pdf\n质量状态：待人工核对\n---以下为原层提取或OCR文本；不补写跨页内容---\n' + body, encoding='utf-8')
    failed_text = output / '逐页文本/failed'
    failed_text.mkdir(parents=True)
    (failed_text / '原始PDF页_0001.txt').write_text('---以下为OCR文本---\n不可算完成的残留文本', encoding='utf-8')
    write_csv(output / '页码映射/002_双页扫描区域映射.csv', [dict(原始相对路径='main.pdf', 原始PDF页='2', 左页区域='0,0,100,200', 左区印刷页候选='4', 右页区域='100,0,200,200', 右区印刷页候选='5')])
    return source, output, digest


def test_import_preserves_page_quality_alias_and_failed_status(tmp_path, package):
    from storage.standard_library import StandardLibrary
    from agent.ocr_import import import_ocr_package
    source, output, digest = package
    lib = StandardLibrary(tmp_path / 'workspace')
    report = import_ocr_package(lib, source, output)
    cid = report['collection_id']
    assert len(lib.documents(cid)) == 2
    doc = lib.get_document(cid, digest)
    assert doc['aliases'][0]['relative_path'] == 'alias.pdf'
    assert doc['standard_number'] == 'JGJ 102-2003'
    assert doc['current_status'] == '待核实'
    pages = lib.pages(cid, digest)
    assert len(pages) == 3
    assert pages[0]['text'] == '短条文：玻璃幕墙。'
    assert '来源相对路径' not in pages[0]['text']
    assert pages[0]['quality'] == 'reference'
    assert pages[0]['numeric_risk'] is True
    assert pages[1]['quality'] == 'locate'
    assert [r['text'] for r in pages[1]['regions']] == ['左区条款', '右区条款']
    assert pages[1]['regions'][1]['bbox'] == [100., 0., 200., 200.]
    assert pages[1]['regions'][1]['printed_candidate'] == '5'
    assert pages[2]['quality'] == 'failed'
    failed = next(d for d in lib.documents(cid) if d['status'] == 'failed')
    assert lib.get_page(cid, failed['doc_id'], 1)['quality'] == 'failed'
    assert report['stats']['processed_documents'] == 1
    assert report['stats']['failed_documents'] == 1
    assert report['stats']['excluded_documents'] == 1


def test_idempotence_changes_staleness_and_controlled_assets(tmp_path, package):
    from storage.standard_library import StandardLibrary, AssetError
    from agent.ocr_import import import_ocr_package
    source, output, digest = package
    lib = StandardLibrary(tmp_path / 'workspace')
    cid = import_ocr_package(lib, source, output)['collection_id']
    revision = lib.revision(cid)
    import_ocr_package(lib, source, output)
    assert lib.revision(cid) == revision
    assert lib.source_status(cid)['stale'] is False
    assert lib.verify_asset(cid, digest)['ok'] is True
    assert lib.pdf_bytes(cid, digest).startswith(b'%PDF')
    assert lib.page_png(cid, digest, 1).startswith(b'\x89PNG')
    with pytest.raises(AssetError):
        lib.pdf_bytes(cid, '../main.pdf')
    with pytest.raises(AssetError):
        lib.page_png(cid, digest, 0)
    text = output / '逐页文本/main/原始PDF页_0001.txt'
    text.write_text(text.read_text(encoding='utf-8') + '更新', encoding='utf-8')
    assert lib.source_status(cid)['stale'] is True
    import_ocr_package(lib, source, output)
    assert lib.revision(cid) > revision
    assert lib.source_status(cid)['stale'] is False
    (source / 'main.pdf').write_bytes(b'changed')
    assert lib.verify_asset(cid, digest)['ok'] is False
    with pytest.raises(AssetError):
        lib.pdf_bytes(cid, digest)


def test_rejects_unverified_alias_and_traversal(tmp_path, package):
    from storage.standard_library import StandardLibrary
    from agent.ocr_import import import_ocr_package
    source, output, digest = package
    (source / 'alias.pdf').write_bytes(b'not same')
    lib = StandardLibrary(tmp_path / 'workspace')
    report = import_ocr_package(lib, source, output)
    assert lib.get_document(report['collection_id'], digest)['aliases'] == []
    assert any('alias.pdf' in str(g) for g in report['gaps'])
    rows = [dict(原始相对路径='../outside.pdf', 原始SHA256=digest, 总页数='1', 处理状态='已处理')]
    write_csv(output / '检查清单/逐文件清单.csv', rows)
    report = import_ocr_package(lib, source, output)
    assert report['gaps']


def test_program_checks_downgrade_only_affected_page_and_refresh(tmp_path, package):
    from storage.standard_library import StandardLibrary
    from agent.ocr_import import import_ocr_package
    source, output, digest = package
    lib = StandardLibrary(tmp_path / 'workspace')
    cid = import_ocr_package(lib, source, output)['collection_id']
    check = output / '待人工核对' / digest[:12] / '程序检查.json'
    check.parent.mkdir(parents=True)
    check.write_text(json.dumps({'render': [{'page': 1, 'render_identical_36dpi': False}]}), encoding='utf-8')
    assert lib.source_status(cid)['stale']
    before = lib.revision(cid)
    import_ocr_package(lib, source, output)
    assert lib.revision(cid) > before
    assert lib.get_page(cid, digest, 1)['quality'] == 'locate'
    assert lib.get_page(cid, digest, 3)['quality'] == 'failed'
    assert not lib.source_status(cid)['stale']


def test_atomic_replacement_rollback_and_index_state(tmp_path, package):
    from storage.standard_library import StandardLibrary
    from agent.ocr_import import import_ocr_package
    source, output, digest = package
    lib = StandardLibrary(tmp_path / 'workspace')
    cid = import_ocr_package(lib, source, output)['collection_id']
    doc, pages = lib.get_document(cid, digest), lib.pages(cid, digest)
    before = lib.revision(cid)
    with pytest.raises(Exception):
        lib.upsert_document(cid, dict(doc, title='Must rollback'), [pages[0], pages[0]])
    assert lib.get_document(cid, digest)['title'] == doc['title']
    assert len(lib.pages(cid, digest)) == 3
    assert lib.revision(cid) == before
    lib.set_index_state(cid, {'revision': before, 'index_version': 'test'})
    assert not lib.index_stale(cid, 'test')
    assert lib.index_stale(cid, 'new')


def test_known_numeric_error_is_locate_but_generic_numeric_risk_is_reference(tmp_path, package):
    from storage.standard_library import StandardLibrary
    from agent.ocr_import import import_ocr_package
    source, output, digest = package
    write_csv(output / '待人工核对/问题清单.csv', [dict(原始相对路径='main.pdf', 原始PDF页='1', 问题='独立OCR与原层条文号/小数存在多处差异，需逐项核对', 状态='待人工核对')])
    lib = StandardLibrary(tmp_path / 'workspace')
    cid = import_ocr_package(lib, source, output)['collection_id']
    assert lib.get_page(cid, digest, 1)['quality'] == 'locate'
    assert lib.get_page(cid, digest, 2)['quality'] == 'reference'


def test_successful_reimport_reconciles_removed_or_excluded_documents(tmp_path, package):
    from storage.standard_library import StandardLibrary
    from agent.ocr_import import import_ocr_package
    source, output, digest = package
    lib = StandardLibrary(tmp_path / 'workspace')
    cid = import_ocr_package(lib, source, output)['collection_id']
    rows = list(csv.DictReader((output / '检查清单/逐文件清单.csv').open(encoding='utf-8-sig')))
    rows = [r for r in rows if r['原始相对路径'] != 'failed.pdf']
    write_csv(output / '检查清单/逐文件清单.csv', rows)
    import_ocr_package(lib, source, output)
    assert len(lib.documents(cid)) == 1
    assert len(lib.pages(cid)) == 3


def test_double_page_missing_marker_must_not_merge_left_and_right(tmp_path, package):
    from storage.standard_library import StandardLibrary
    from agent.ocr_import import import_ocr_package
    source, output, digest = package
    text = output / '逐页文本/main/原始PDF页_0002.txt'
    text.write_text('---以下为OCR文本---\n左区与右区混在一起', encoding='utf-8')
    lib = StandardLibrary(tmp_path / 'workspace')
    cid = import_ocr_package(lib, source, output)['collection_id']
    page = lib.get_page(cid, digest, 2)
    assert page['quality'] == 'locate'
    assert any('区域' in i['问题'] for i in page['issues'])


def test_wrong_text_provenance_is_never_reference(tmp_path, package):
    from storage.standard_library import StandardLibrary
    from agent.ocr_import import import_ocr_package
    source, output, digest = package
    text = output / '逐页文本/main/原始PDF页_0001.txt'
    text.write_text('来源相对路径：wrong.pdf\n原始PDF页码：19\n原始SHA-256：' + 'f' * 64 + '\n---以下为OCR文本---\n其它文档条款', encoding='utf-8')
    lib = StandardLibrary(tmp_path / 'workspace')
    cid = import_ocr_package(lib, source, output)['collection_id']
    page = lib.get_page(cid, digest, 1)
    assert page['quality'] == 'failed'
    assert not page['text']
    assert any('来源校验' in i['问题'] for i in page['issues'])


def test_manual_supplement_is_page_specific_and_missing_original_is_registered(tmp_path, package):
    from storage.standard_library import StandardLibrary
    from agent.ocr_import import import_ocr_package
    source, output, digest = package
    manual = output / '工作缓存/manual_issues.json'
    manual.parent.mkdir(parents=True)
    manual.write_text(json.dumps([dict(原始相对路径='main.pdf', 原始PDF页=1, 问题='尺寸10未完整进入OCR；上下标形态不一致。')]), encoding='utf-8')
    (source / 'failed.pdf').unlink()
    lib = StandardLibrary(tmp_path / 'workspace')
    result = import_ocr_package(lib, source, output)
    cid = result['collection_id']
    assert len(lib.documents(cid)) == 2
    assert lib.get_page(cid, digest, 1)['quality'] == 'locate'
    assert len(lib.pages(cid)) == 4
    assert result['stats']['failed_documents'] == 1
    assert result['stats']['blank_pages'] == 1
    assert result['stats']['searchable_documents'] == 1
    assert result['stats']['reference_documents'] == 0
    assert result['stats']['locator_only_documents'] == 1


def test_missing_artifact_under_windows_trailing_space_stem_is_recorded(tmp_path, package):
    from storage.standard_library import StandardLibrary, contained_path, AssetError
    from agent.ocr_import import import_ocr_package
    source, output, digest = package
    rows = list(csv.DictReader((output / '检查清单/逐文件清单.csv').open(encoding='utf-8-sig')))
    row = next(r for r in rows if r['原始相对路径'] == 'failed.pdf')
    row['原始相对路径'] = '051 failed .pdf'
    (source / 'failed.pdf').rename(source / row['原始相对路径'])
    write_csv(output / '检查清单/逐文件清单.csv', rows)
    (output / '逐页文本/051 failed').mkdir(parents=True)
    target = contained_path(output, '逐页文本/051 failed /原始PDF页_0001.txt')
    assert not target.exists()
    lib = StandardLibrary(tmp_path / 'workspace')
    result = import_ocr_package(lib, source, output)
    assert result['stats']['documents'] == 2
    assert result['stats']['failed_documents'] == 1
    assert lib.get_page(result['collection_id'], row['原始SHA256'], 1)['quality'] == 'failed'
    assert not lib.source_status(result['collection_id'])['stale']
    with pytest.raises(AssetError):
        contained_path(output, '../outside.txt')
    outside = tmp_path / 'outside'
    outside.mkdir()
    link = output / 'escape'
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        return  # Windows without the symlink privilege still covers the trailing-space regression.
    with pytest.raises(AssetError):
        contained_path(output, 'escape/file.txt')


def test_clear_removes_library_search_and_reports_but_never_source(tmp_path, package):
    from storage.standard_library import StandardLibrary
    from agent.ocr_import import import_ocr_package
    from evidence.standard_retrieval import StandardRetriever
    source, output, digest = package
    lib = StandardLibrary(tmp_path / 'workspace')
    cid = import_ocr_package(lib, source, output)['collection_id']
    retriever = StandardRetriever(lib)
    retriever.build(cid)
    lib.set_index_state(cid, {'revision': lib.revision(cid)})
    (lib.directory / 'import_report.json').write_text('{}')
    result = lib.clear()
    assert result['documents'] == 2
    assert not lib.list_collections()
    assert not lib.pages(cid)
    assert lib.get_index_state(cid) is None
    assert retriever.db.execute('SELECT COUNT(*) FROM chunks').fetchone()[0] == 0
    assert retriever.db.execute('SELECT COUNT(*) FROM states').fetchone()[0] == 0
    assert retriever.db.execute('SELECT COUNT(*) FROM search').fetchone()[0] == 0
    assert not (lib.directory / 'import_report.json').exists()
    assert (source / 'main.pdf').exists()
    assert (output / '逐页文本/main/原始PDF页_0001.txt').exists()
    assert not StandardLibrary(tmp_path / 'workspace').list_collections()


def test_import_stats_distinguish_failure_missing_text_and_blank_candidates(tmp_path, package):
    from storage.standard_library import StandardLibrary
    from agent.ocr_import import import_ocr_package
    source, output, digest = package
    maps = output / '检查清单/逐页分类与映射汇总.csv'
    rows = list(csv.DictReader(maps.open(encoding='utf-8-sig')))
    rows[2]['处理前分类'] = '5_空白候选'
    write_csv(maps, rows)
    files = output / '检查清单/逐文件清单.csv'
    rows = list(csv.DictReader(files.open(encoding='utf-8-sig')))
    rows[0]['抽查状态'] = '已抽查（有限页面；未专业复核）'
    write_csv(files, rows)
    lib = StandardLibrary(tmp_path / 'workspace')
    report = import_ocr_package(lib, source, output)
    stats = report['stats']
    assert stats['failed_document_pages'] == 1
    assert stats['nonsearchable_processed_pages'] == 1
    assert stats['blank_candidate_pages'] == 1
    assert stats['no_text_pages'] == 1
    assert stats['render_difference_pages'] == 1
    assert stats['render_difference_documents'] == 1
    assert stats['sampled_documents'] == 1
    assert stats['review_status_counts']['未复核'] == 2
    doc = lib.get_document(report['collection_id'], digest)
    assert doc['quality_counts']['render_difference_pages'] == 1
