"""Import existing OCR deliverables without running OCR or changing source files."""
from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from storage.standard_library import AssetError, canonical, contained_path, sha256_file, stat_signature

TEXT_VERSION = 'ocr-delivery-parser-1'
QUALITY_VERSION = 'page-quality-1'
GENERIC_NUMERIC = '数字/单位/公式或表格页：行列、上下标、不等号及脚注需对照原页，未逐项专业复核'


def _csv(path):
    if not path.exists():
        return []
    with path.open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def _json(path):
    return json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}


def _rel(value):
    return str(value).replace('\\', '/')


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _text(raw):
    parts = re.split(r'^---以下[^\r\n]*---\s*$', raw, maxsplit=1, flags=re.M)
    if len(parts) != 2:
        # A missing delimiter must not turn provenance headers into searchable clauses.
        return raw.strip(), '', False
    return parts[0].strip(), parts[1].strip(), True


def _regions(body, mapping, page_mapping):
    matches = list(re.finditer(r'\[原始双页扫描：([左右])页区域\]', body))
    if matches:
        regions = []
        for i, match in enumerate(matches):
            side = match.group(1)
            coords = mapping.get(side + '页区域', '')
            try:
                bbox = [float(x) for x in coords.split(',')] if coords else None
            except ValueError:
                bbox = None
            regions.append(dict(region='left' if side == '左' else 'right',
                                text=body[match.end():matches[i + 1].start() if i + 1 < len(matches) else len(body)].strip(),
                                bbox=bbox, printed_candidate=mapping.get(side + '区印刷页候选', ''),
                                printed_status='候选，未人工确认'))
        return regions
    return [dict(region='full', text=body, bbox=None, printed_candidate=page_mapping.get('印刷页码候选', ''), printed_status='候选，未人工确认')]


def _quality(text, issues, failed, render):
    if failed or not text.strip():
        return 'failed'
    diagnostic = ';'.join(str(i.get('问题', '')) for i in issues).replace(GENERIC_NUMERIC, '')
    if render is False or re.search(r'渲染差异|未识别出文字|OCR失败|识别失败|漏字|漏识|误识|错误|多处差异|未可靠结构化|复杂公式|禁止当作|文字缺失|空白候选|识别为|OCR出现|未完整|不一致|不可靠|不能视为可靠|不得据OCR|否定词', diagnostic):
        return 'locate'
    return 'reference'


def _page_quality_counts(pages):
    return dict(reference_pages=sum(p['quality'] == 'reference' for p in pages),
                locate_pages=sum(p['quality'] == 'locate' for p in pages),
                failed_pages=sum(p['quality'] == 'failed' for p in pages),
                no_text_pages=sum(not p['text'].strip() and not p.get('quarantined_text') for p in pages),
                blank_candidate_pages=sum('空白候选' in p.get('mapping', {}).get('处理前分类', '') for p in pages),
                render_difference_pages=sum(p.get('render_identical') is False or any('渲染差异' in i.get('问题', '') for i in p['issues']) for p in pages))


def import_ocr_package(library, source_root, package_root, collection_name='幕墙规范资料', progress=None):
    """Each document is atomic; interrupted runs resume safely on the next explicit call."""
    source_root, package_root = Path(source_root).resolve(), Path(package_root).resolve()
    files_path = package_root / '检查清单/逐文件清单.csv'
    maps_path = package_root / '检查清单/逐页分类与映射汇总.csv'
    issues_path = package_root / '待人工核对/问题清单.csv'
    if not all(p.is_file() for p in (files_path, maps_path, issues_path)):
        raise ValueError('缺少逐文件清单、逐页分类与映射汇总或问题清单；未执行导入')
    files, mappings, issues = _csv(files_path), _csv(maps_path), _csv(issues_path)
    cid = library.ensure_collection(collection_name, source_root, package_root)
    library.update_collection(cid, import_complete=False)
    manifest, manifest_keys = [], set()

    def record(root_key, path):
        root = source_root if root_key == 'source_root' else package_root
        path = Path(path)
        relative = path.relative_to(root).as_posix()
        key = (root_key, relative)
        if key not in manifest_keys:
            manifest_keys.add(key)
            manifest.append(dict(root=root_key, relative_path=relative, signature=stat_signature(path)))

    for p in (files_path, maps_path, issues_path):
        record('package_root', p)
    manual_path = package_root / '工作缓存/manual_issues.json'
    record('package_root', manual_path)
    manual_issues = _json(manual_path) if manual_path.exists() else []
    for row in manual_issues:
        if row not in issues:
            issues.append(row)
    double_path = package_root / '页码映射/002_双页扫描区域映射.csv'
    record('package_root', double_path)
    double = {(_rel(r['原始相对路径']), _int(r['原始PDF页'])): r for r in _csv(double_path)}
    by_mapping, by_issue = defaultdict(dict), defaultdict(list)
    for row in mappings:
        by_mapping[_rel(row['原始相对路径'])][_int(row['原始PDF页'])] = row
    for row in issues:
        by_issue[_rel(row['原始相对路径'])].append(row)
    excluded = [r for r in files if '排除' in r.get('处理状态', '')]
    aliases = [r for r in files if '重复别名' in r.get('处理状态', '')]
    main = [r for r in files if r not in excluded and r not in aliases]
    gaps, keep, changed, unchanged = [], set(), 0, 0
    verified_aliases = defaultdict(list)
    main_by_path = {_rel(r['原始相对路径']): r for r in main}
    for row in aliases:
        relative, target = _rel(row['原始相对路径']), _rel(row.get('重复主文件', ''))
        try:
            alias_path = contained_path(source_root, relative)
            record('source_root', alias_path)
            target_row = main_by_path.get(target)
            if not target_row or sha256_file(alias_path) != target_row['原始SHA256'] or row['原始SHA256'] != target_row['原始SHA256']:
                raise AssetError('别名与主文件SHA-256不一致，不合并')
            target_path = contained_path(source_root, target)
            if sha256_file(target_path) != target_row['原始SHA256']:
                raise AssetError('别名目标原件SHA-256不一致，不合并')
            verified_aliases[target].append(dict(relative_path=relative, sha256=row['原始SHA256'], source_row=row))
        except (OSError, AssetError) as e:
            gaps.append(dict(relative_path=relative, reason=str(e), kind='alias'))

    for pos, row in enumerate(main, 1):
        relative, digest = _rel(row['原始相对路径']), row['原始SHA256']
        if not re.fullmatch('[0-9a-fA-F]{64}', digest):
            gaps.append(dict(relative_path=relative, reason='清单SHA-256格式无效', kind='source'))
            continue
        try:
            original = contained_path(source_root, relative)
        except AssetError as e:
            gaps.append(dict(relative_path=relative, reason=str(e), kind='source'))
            continue
        record('source_root', original)
        try:
            # Do not trust paths or the claimed hash from the CSV.
            verified = sha256_file(original) == digest
        except (OSError, AssetError) as e:
            gaps.append(dict(relative_path=relative, reason=str(e), kind='source'))
            verified = False
        failure = row.get('处理状态') != '已处理' or not verified
        if not verified:
            gaps.append(dict(relative_path=relative, reason='原件与登记SHA-256不一致', kind='source'))
        done_path = package_root / '工作缓存/完成状态' / (digest + '.json')
        check_path = package_root / '待人工核对' / digest[:12] / '程序检查.json'
        file_map_path = contained_path(package_root, '页码映射/' + str(Path(relative).with_suffix('.csv')).replace('\\', '/'))
        for p in (done_path, check_path, file_map_path):
            record('package_root', p)
        done, check = _json(done_path), _json(check_path)
        page_mapping = dict(by_mapping.get(relative, {}))
        per_file = _csv(file_map_path)
        for entry in per_file:
            n = _int(entry.get('原始PDF页'))
            # Summary is the delivery authority; retain per-file evidence separately.
            page_mapping.setdefault(n, entry)
        per_file_by_page = {_int(x.get('原始PDF页')): x for x in per_file}
        all_issues = list(by_issue.get(relative, []))
        for item in done.get('issues', []):
            if item not in all_issues:
                all_issues.append(item)
        render = {int(r['page']): r.get('render_identical_36dpi') for r in check.get('render', [])}
        count = _int(row.get('总页数'))
        page_records = []
        for page in range(1, count + 1):
            mapping = page_mapping.get(page, {})
            text_path = contained_path(package_root, '逐页文本/' + str(Path(relative).with_suffix('')).replace('\\', '/') + f'/原始PDF页_{page:04}.txt')
            record('package_root', text_path)
            raw = text_path.read_text(encoding='utf-8-sig') if text_path.exists() else ''
            header, body, delimiter = _text(raw)
            page_issues = [i for i in all_issues if _int(i.get('原始PDF页')) == page]
            parsed_header = dict(re.findall(r'^([^：\n]+)：([^\n]*)$', header, flags=re.M))
            provenance_error = (
                ('原始SHA-256' in parsed_header and parsed_header['原始SHA-256'].strip().lower() != digest.lower())
                or ('来源相对路径' in parsed_header and _rel(parsed_header['来源相对路径'].strip()) != relative)
                or ('原始PDF页码' in parsed_header and _int(parsed_header['原始PDF页码']) != page)
                or (mapping.get('原始SHA256') and mapping['原始SHA256'].lower() != digest.lower())
                or (mapping.get('原始相对路径') and _rel(mapping['原始相对路径']) != relative))
            quarantined_text = body if provenance_error else ''
            if provenance_error:
                page_issues.append(dict(问题='逐页文本或映射来源校验失败（SHA-256/相对路径/原始页码）；正文隔离，不进入检索', 状态='待人工核对'))
                body = ''
            if mapping.get('待人工核对') and not any(mapping['待人工核对'] == x.get('问题') for x in page_issues):
                page_issues.append(dict(问题=mapping['待人工核对'], 状态='待人工核对', 原始PDF页=str(page), 来源='逐页映射'))
            if not delimiter:
                page_issues.append(dict(问题='逐页文本缺失或缺少正文分隔符', 状态='待人工核对'))
            if not mapping:
                page_issues.append(dict(问题='页码映射缺失', 状态='待人工核对'))
            if render.get(page) is False:
                page_issues.append(dict(问题='处理前后渲染差异（程序检查）', 状态='待人工核对'))
            quality = _quality(body, page_issues, failure, render.get(page))
            if quality == 'reference' and not mapping:
                quality = 'locate'
            regions = _regions(body, double.get((relative, page), {}), mapping)
            if (relative, page) in double and ({r['region'] for r in regions} != {'left', 'right'} or any(r['bbox'] is None for r in regions)):
                page_issues.append(dict(问题='双页扫描区域标记或坐标缺失，不能确认左右条文读序', 状态='待人工核对'))
                if quality != 'failed':
                    quality = 'locate'
            page_records.append(dict(doc_id=digest, page=page, original_page=page,
                                     processed_page=_int(mapping.get('处理PDF页'), None), text=body,
                                     metadata_header=header, metadata=parsed_header, regions=regions, quality=quality,
                                     quarantined_text=quarantined_text, provenance_verified=not provenance_error,
                                     issues=page_issues, numeric_risk=bool(re.search(r'\d|[±≤≥<>=]|数字|公式|表格', body + canonical(page_issues))),
                                     printed_candidate=mapping.get('印刷页码候选', ''), printed_status=mapping.get('印刷页码状态', '未人工确认'),
                                     mapping=mapping, file_mapping=per_file_by_page.get(page), region_mapping=double.get((relative, page)),
                                     render_identical=render.get(page), text_sha256=hashlib.sha256(raw.encode()).hexdigest()))
        from evidence.standard_retrieval import STANDARD, standard_parts
        raw_number = row.get('标准编号候选（文件内）', '')
        filename_number, filename_year = standard_parts(Path(relative).stem)
        candidates = list(dict.fromkeys(standard_parts(m.group()) for m in STANDARD.finditer(raw_number)))
        selected = next((c for c in candidates if c[0] == filename_number and c[1]), None)
        if selected is None and len(candidates) == 1 and candidates[0][1]:
            selected = candidates[0]
        identity_status = 'internal_candidate'
        if selected is None:
            selected = (filename_number, filename_year)
            identity_status = 'filename_candidate' if filename_number else 'unresolved'
        standard_code, version = selected
        document = dict(doc_id=digest, sha256=digest, relative_path=relative, filename=Path(relative).name,
                        title=row.get('标准名称候选（文件内）', '') or Path(relative).stem,
                        standard_number=raw_number, standard_code=standard_code, version=version,
                        standard_candidates_raw=raw_number, standard_identity_status=identity_status,
                        edition_evidence=row.get('版次（明确字样）', ''),
                        category=relative.split('/')[0] if '/' in relative else '未分类', page_count=count,
                        status='failed' if failure else 'processed', quality='failed' if failure else ('reference' if any(p['quality'] == 'reference' for p in page_records) else 'locate'),
                        source_verified=verified, aliases=verified_aliases[relative], source_row=row,
                        processing_version=done.get('version', row.get('处理方式', '未提供')),
                        text_version=TEXT_VERSION, quality_version=QUALITY_VERSION,
                        review_status=row.get('专业审核状态', '未复核'), sample_review_status=row.get('抽查状态', '未抽查'),
                        sample_review=row.get('抽查记录', ''), current_status=row.get('现行状态', '待核实'),
                        issues=all_issues, completion_record=done, program_checks=check,
                        quality_counts=_page_quality_counts(page_records))
        keep.add(digest)
        if library.upsert_document(cid, document, page_records):
            changed += 1
        else:
            unchanged += 1
        if failure:
            gaps.append(dict(relative_path=relative, reason='处理失败/未完成；残留文本不计入成功资料', kind='processing', pages=count))
        if progress:
            progress(pos, len(main), relative)
    library.remove_absent_documents(cid, keep)
    docs, pages = library.documents(cid), library.pages(cid)
    grouped_pages = defaultdict(list)
    for page in pages:
        grouped_pages[page['doc_id']].append(page)
    searchable = lambda p: bool(p['text'].strip()) and p['quality'] in ('reference', 'locate')
    stats = dict(manifest_rows=len(files), documents=len(docs), target_documents=len(main),
                 processed_documents=sum(d['status'] == 'processed' for d in docs),
                 failed_documents=sum(d['status'] == 'failed' for d in docs),
                 alias_documents=sum(len(d['aliases']) for d in docs), excluded_documents=len(excluded),
                 pages=len(pages), reference_pages=sum(p['quality'] == 'reference' for p in pages),
                 locate_pages=sum(p['quality'] == 'locate' for p in pages), failed_pages=sum(p['quality'] == 'failed' for p in pages),
                 blank_pages=sum(not p['text'].strip() and not p.get('quarantined_text') for p in pages),
                 searchable_pages=sum(searchable(p) for p in pages),
                 searchable_documents=sum(any(searchable(p) for p in grouped_pages[d['doc_id']]) for d in docs),
                 reference_documents=sum(any(p['quality'] == 'reference' for p in grouped_pages[d['doc_id']]) for d in docs),
                 locator_only_documents=sum(any(p['quality'] == 'locate' for p in grouped_pages[d['doc_id']]) and not any(p['quality'] == 'reference' for p in grouped_pages[d['doc_id']]) for d in docs),
                 mixed_documents=sum(any(p['quality'] == 'locate' for p in grouped_pages[d['doc_id']]) and any(p['quality'] == 'reference' for p in grouped_pages[d['doc_id']]) for d in docs),
                 changed_documents=changed, unchanged_documents=unchanged)
    stats.update(_page_quality_counts(pages))
    stats.update(failed_document_pages=sum(len(grouped_pages[d['doc_id']]) for d in docs if d['status'] == 'failed'),
                 nonsearchable_processed_pages=sum(p['quality'] == 'failed' for d in docs if d['status'] == 'processed' for p in grouped_pages[d['doc_id']]),
                 render_difference_documents=sum(d['quality_counts']['render_difference_pages'] > 0 for d in docs),
                 sampled_documents=sum('已抽查' in d.get('sample_review_status', '') for d in docs),
                 review_status_counts=dict(Counter(d.get('review_status') or '未复核' for d in docs)),
                 sample_review_status_counts=dict(Counter(d.get('sample_review_status') or '未抽查' for d in docs)),
                 current_status_counts=dict(Counter(d.get('current_status') or '待核实' for d in docs)),
                 blank_pages_definition='兼容字段：无可检索正文页数；不代表原页已确认空白',
                 blank_candidate_pages_definition='仅处理前分类标记为空白候选；未确认真正空白')
    library.update_collection(cid, source_manifest=manifest, import_complete=True, stats=stats, gaps=gaps,
                              excluded_rows=excluded, alias_rows=aliases, text_version=TEXT_VERSION, quality_version=QUALITY_VERSION)
    return dict(collection_id=cid, stats=stats, gaps=gaps)
