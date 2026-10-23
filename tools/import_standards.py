"""Explicit one-time/local refresh command; reads deliverables, never reruns OCR."""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.ocr_import import import_ocr_package
from storage.standard_library import StandardLibrary
from storage.workspace import DEFAULT_DIR


def quality_report(library, collection_id):
    c = library.get_collection(collection_id)
    def issue_public(value):
        if not isinstance(value, dict):
            return value
        out = dict(value)
        if out.get('截图/定位'):
            out['截图/定位'] = str(out['截图/定位']).replace('\\', '/').rsplit('/', 1)[-1]
        return out
    def doc_public(d):
        out = {k: v for k, v in d.items() if k not in ('completion_record', 'program_checks', 'source_row')}
        out['issues'] = [issue_public(x) for x in out.get('issues', [])]
        out['aliases'] = [{k: v for k, v in a.items() if k != 'source_row'} for a in out.get('aliases', [])]
        return out
    return {
        'collection_id': collection_id, 'name': c['name'], 'revision': c['revision'],
        'stats': c.get('stats', {}), 'gaps': c.get('gaps', []),
        'documents': [doc_public(d) for d in library.documents(collection_id)],
        'page_quality': [
            {'doc_id': p['doc_id'], 'page': p['page'], 'quality': p['quality'],
             'issues': [issue_public(x) for x in p.get('issues', [])], 'printed_candidate': p.get('printed_candidate'),
             'printed_status': p.get('printed_status')}
            for p in library.pages(collection_id) if p.get('issues') or p.get('quality') != 'reference'
        ],
    }


def main():
    parser = argparse.ArgumentParser(description='将现有OCR处理成果导入独立规范资料库')
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--package', required=True, type=Path)
    parser.add_argument('--workspace', type=Path, default=DEFAULT_DIR)
    parser.add_argument('--name', default='幕墙规范资料')
    args = parser.parse_args()
    library = StandardLibrary(args.workspace)
    result = import_ocr_package(library, args.source, args.package, args.name,
                                progress=lambda n, total, path: print(f'{n}/{total} {path}', flush=True))
    from evidence.standard_retrieval import StandardRetriever
    retriever = StandardRetriever(library)
    try:
        result['index'] = retriever.build(result['collection_id'])
    finally:
        retriever.close()
    result['quality'] = quality_report(library, result['collection_id'])
    report_path = library.directory / 'import_report.json'
    report_text = json.dumps(result, ensure_ascii=False, indent=2)
    temporary_report = report_path.with_suffix('.json.tmp')
    temporary_report.write_text(report_text, encoding='utf-8')
    temporary_report.replace(report_path)
    print(json.dumps({k:v for k,v in result.items() if k != 'quality'}, ensure_ascii=False, indent=2))
    print(f'详细质量报告：{report_path}')


if __name__ == '__main__':
    main()
