"""Persistent local FTS5/BM25 retrieval; never build an index during a question.

Clause identifiers and printed pages remain OCR candidates. No engineering rules
are created here. Each text range retains its original physical PDF page/region.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from pathlib import Path

INDEX_VERSION = 'standard-fts5-v1.5'
STANDARD = re.compile(r'(?i)(?:(?:DG\s*[/_—-]?\s*TJ)\s*\d+\s*[-—–－]\s*\d+(?:\s*[-—–－]\s*\d{4})?|(?:GB\s*[/_／-]?\s*T|JGJ\s*[/_／-]?\s*T|JG\s*[/_／-]?\s*T|JC\s*[/_／-]?\s*T|DB\d+\s*[/_／-]?\s*T|T\s*[/_／]\s*CECS|GB|JGJ|JG|JC|CECS)\s*\d+(?:\.\d+)*(?:\s*[-—–－]{1,2}\s*(?:\d{4}|\d{2})(?!\d))?)')
CLAUSE = re.compile(r'^\s*((?:[A-Z]\s*\.\s*)?\d{1,2}\s*\.\s*\d{1,2}(?:\s*\.\s*\d{1,3})?)(?![\d.])\s*(?=[\u4e00-\u9fffA-Za-z])')
QUERY_CLAUSE = re.compile(r'(?<![\d.])(?:[A-Z]\s*\.\s*)?\d{1,2}\s*\.\s*\d{1,2}(?:\s*\.\s*\d{1,3})?(?![\d.])')
TABLE_LABEL = re.compile(r'表\s*((?:[A-Z]\s*\.\s*)?\d+(?:\s*\.\s*\d+)*)(?![\d.])')
COMMENTARY_HEADING = re.compile(r'^[^\S\r\n]*条\s*文\s*说\s*明[^\S\r\n]*$', re.M)
STOP = {'什么','哪些','如何','怎么','多少','是否','要求','规定','规范','条文','查找','查询','请问','资料','依据','有关','相关','根据','基于','回答','原文','可以','中的','对于','一个','进行','需要','应当','不得','不应'}


def norm(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(text or '')))


def standard_parts(value):
    match = STANDARD.search(unicodedata.normalize('NFKC', value or ''))
    if not match:
        return '', ''
    s = norm(match.group()).upper().replace('_', '/').replace('—', '-').replace('–', '-').replace('－', '-')
    s = re.sub(r'^(GB|JGJ|JG|JC)T', r'\1/T', s)
    s = re.sub(r'^(GB|JGJ|JG|JC)-T', r'\1/T', s)
    s = re.sub(r'^DG[/-]?TJ', 'DG/TJ', s)
    s = re.sub(r'-{2,}', '-', s)
    found = re.search(r'-(\d{4})$' if s.startswith('DG/TJ') else r'-(\d{4}|\d{2})$', s)
    return (s[:found.start()], found[1]) if found else (s, '')


def tokens(text):
    text = unicodedata.normalize('NFKC', text).lower()
    result = re.findall(r'[a-z]+|\d+(?:\.\d+)*', text)
    for seg in re.findall(r'[\u4e00-\u9fff]+', text):
        result.extend(seg[i:i+2] for i in range(len(seg)-1) if seg[i:i+2] not in STOP)
    return result


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _section(text, previous):
    # A commentary appendix often has its own preface, contents and numbered
    # chapters. Those inner headings must never reset it to normative body text.
    if previous == '条文说明' or COMMENTARY_HEADING.match(text):
        return '条文说明'
    compact = norm(text.splitlines()[0])
    if compact.startswith('条文说明') or re.fullmatch(r'[\d.]*条文说明', compact):
        return '条文说明'
    if compact.startswith(('前言', '编制说明', '公告', '目次')):
        return '前置说明'
    if compact == '目录':
        return '目录'
    if previous not in ('条文说明', '目录') and CLAUSE.match(text):
        return '正文候选'
    if re.match(r'^(?:1|1\.0)\s*总\s*则', text) and previous == '目录':
        return '正文候选'
    return previous


def make_chunks(doc, pages):
    """Follow physical page order, then left/right order. Keep tables intact.

    Continuations before the next detected heading join the preceding clause.
    Unknown identifiers stay null. Long chunks remain locators, not truncated claims.
    """
    chunks, active, section = [], None, '正文候选'
    for page in sorted(pages, key=lambda p: p['page']):
        regions = page.get('regions') or [{'region': 'full', 'text': page.get('text', ''), 'bbox': None}]
        for region in regions:
            text = region.get('text', '').strip()
            if not text:
                active = None  # do not silently bridge missing text
                continue
            lines = text.splitlines(keepends=True)
            commentary_starts = {m.start() for m in COMMENTARY_HEADING.finditer(text)}
            blocks, block, clause = [], '', None
            offset = 0
            for line in lines:
                m = CLAUSE.match(line)
                heading = re.match(r'^\s*\d{1,2}(?:\.\d{1,2})?\s+(?:总则|术语|基本规定|材料|设计|施工|安装|验收|附录)[：:]?\s*$', line)
                boundary = (m or heading or offset in commentary_starts
                            or norm(line) in ('条文说明', '目录', '前言', '编制说明', '公告', '目次'))
                if boundary and block:
                    blocks.append((block.strip(), clause)); block = ''
                if boundary:
                    clause = norm(m.group(1)) if m else None
                block += line
                offset += len(line)
            if block.strip():
                blocks.append((block.strip(), clause))
            for i, (body, candidate) in enumerate(blocks):
                next_section = _section(body, section)
                source = {'page': page['page'], 'processed_page': page.get('processed_page'),
                          'region': region.get('region', 'full'), 'bbox': region.get('bbox'),
                          'printed_candidate': region.get('printed_candidate') or page.get('printed_candidate'),
                          'printed_status': page.get('printed_status', '未确认'), 'text': body,
                          'quality': page.get('quality', 'locate'), 'issues': page.get('issues', []),
                          'numeric_risk': page.get('numeric_risk', False)}
                # Only join the first continuation block; never infer a new clause number.
                continuation = (i == 0 and candidate is None and active and active.get('clause_candidate')
                                and next_section == section and len(active['text']) < 3200
                                and not re.match(r'^\s*\d{1,2}\s+\D', body)
                                and norm(body.splitlines()[0]) not in ('条文说明', '目录'))
                section = next_section
                if continuation:
                    active['sources'].append(source)
                    active['text'] += '\n' + body
                else:
                    active = {'text': body, 'clause_candidate': candidate, 'section': section, 'sources': [source]}
                    chunks.append(active)
    for i, chunk in enumerate(chunks):
        chunk['ordinal'] = i
        chunk['table_candidates'] = list(dict.fromkeys(norm(x) for x in TABLE_LABEL.findall(chunk['text'])))
        # Numeric/table/formula content has no audited structure, even on otherwise usable pages.
        stripped = CLAUSE.sub('', chunk['text'], count=1)
        # OCR units can themselves be corrupted; a closed unit whitelist misses
        # kPa, seconds, dimensions and decimal-only table cells. Conservatively
        # keep every unreviewed numeric body as a locator (clause ID excepted).
        chunk['numeric_risk'] = bool(chunk['table_candidates'] or re.search(r'\d|[≤≥±=∑]|式中|公式|表\s*[A-Z\d]', stripped, re.I))
        chunk['quality'] = 'reference' if all(s['quality'] == 'reference' for s in chunk['sources']) else 'locate'
        chunk['answer_eligible'] = (chunk['quality'] == 'reference' and not chunk['numeric_risk']
                                    and chunk['section'] not in ('目录', '条文说明', '前置说明')
                                    and len(chunk['text']) <= 1800)
        chunk['issues'] = list(dict.fromkeys(str(x) for s in chunk['sources'] for x in s['issues']))
        if chunk['numeric_risk']:
            chunk['issues'].append('数值、表格或公式结构未逐项核对；仅供原页定位，不生成确定数值结论')
        chunk['content_hash'] = digest(chunk['sources'])
    return chunks


class StandardRetriever:
    def __init__(self, library):
        self.library = library
        path = Path(library.root) / 'standards' / 'search.db'
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS states(collection_id TEXT PRIMARY KEY,revision TEXT,version TEXT);
        CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY,collection_id TEXT,doc_id TEXT,ordinal INTEGER,json TEXT);
        CREATE INDEX IF NOT EXISTS chunk_scope ON chunks(collection_id,doc_id,ordinal);
        CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(terms);
        ''')

    def close(self):
        self.db.close()

    def build(self, collection_id):
        collection = self.library.get_collection(collection_id)
        if not collection:
            raise ValueError('资料集不存在')
        count = 0
        eligible_chunks, eligible_pages, eligible_docs = 0, set(), set()
        indexed_pages, indexed_docs = set(), set()
        with self.db:
            ids = [r[0] for r in self.db.execute('SELECT id FROM chunks WHERE collection_id=?', (collection_id,))]
            self.db.executemany('DELETE FROM search WHERE rowid=?', [(i,) for i in ids])
            self.db.execute('DELETE FROM chunks WHERE collection_id=?', (collection_id,))
            all_pages = {}
            for page in self.library.pages(collection_id):
                all_pages.setdefault(page['doc_id'], []).append(page)
            for doc in self.library.documents(collection_id):
                if doc.get('status') == 'failed':
                    continue
                code, year = standard_parts(doc.get('standard_code') or doc.get('standard_number') or '')
                version = str(doc.get('version') or year or '')
                for chunk in make_chunks(doc, all_pages.get(doc['doc_id'], [])):
                    indexed_docs.add(doc['doc_id'])
                    indexed_pages.update((doc['doc_id'], s['page']) for s in chunk['sources'])
                    if chunk['answer_eligible']:
                        eligible_chunks += 1
                        eligible_docs.add(doc['doc_id'])
                        eligible_pages.update((doc['doc_id'], s['page']) for s in chunk['sources'])
                    chunk.update(collection_id=collection_id, doc_id=doc['doc_id'], filename=doc['filename'],
                                 title=doc.get('title', doc['filename']), standard_code=code, version=version,
                                 category=doc.get('category', ''), revision=collection['revision'], index_version=INDEX_VERSION,
                                 review_status=doc.get('review_status', '未复核'), sample_review_status=doc.get('sample_review_status', '未抽查'),
                                 current_status=doc.get('current_status', '待核实'), effect_status=doc.get('current_status', '待核实'))
                    chunk['evidence_id'] = 'E-' + digest([collection_id, doc['doc_id'], chunk['content_hash']])[:16]
                    cur = self.db.execute('INSERT INTO chunks(collection_id,doc_id,ordinal,json) VALUES(?,?,?,?)',
                                          (collection_id, doc['doc_id'], chunk['ordinal'], json.dumps(chunk, ensure_ascii=False)))
                    self.db.execute('INSERT INTO search(rowid,terms) VALUES(?,?)', (cur.lastrowid, ' '.join(tokens(chunk['text']))))
                    count += 1
            self.db.execute('INSERT OR REPLACE INTO states VALUES(?,?,?)', (collection_id, str(collection['revision']), INDEX_VERSION))
        state = {'collection_id': collection_id, 'chunks': count, 'index_version': INDEX_VERSION,
                 'revision': collection['revision'], 'answer_eligible_chunks': eligible_chunks,
                 'answer_eligible_pages': len(eligible_pages), 'answer_eligible_documents': len(eligible_docs),
                 'indexed_pages': len(indexed_pages), 'indexed_documents': len(indexed_docs),
                 'locator_only_pages': len(indexed_pages - eligible_pages),
                 'locator_only_documents': len(indexed_docs - eligible_docs)}
        if hasattr(self.library, 'set_index_state'):
            self.library.set_index_state(collection_id, state)
        return state

    def stale(self, collection_id):
        c = self.library.get_collection(collection_id)
        state = self.db.execute('SELECT revision,version FROM states WHERE collection_id=?', (collection_id,)).fetchone()
        return not c or not state or state != (str(c['revision']), INDEX_VERSION)

    def search(self, query, collection_ids, *, standards=None, versions=None, categories=None, top_k=6):
        collection_ids = list(dict.fromkeys(collection_ids or []))
        stale = [c for c in collection_ids if self.stale(c)]
        for c in collection_ids:
            if hasattr(self.library, 'source_status') and self.library.source_status(c)['stale']:
                stale.append(c)
        result = {'evidence': [], 'stale': list(dict.fromkeys(stale)), 'index_version': INDEX_VERSION,
                  'query': query, 'collection_ids': collection_ids, 'gaps': []}
        if stale or not collection_ids:
            result['gaps'] = ['资料索引过期或源文件已变化，请在管理规范库中更新。' if stale else '请先选择资料集。']
            return result
        query = str(query or '')[:2000]
        requested = [standard_parts(m.group()) for m in STANDARD.finditer(query)]
        remainder = STANDARD.sub(' ', query)
        exact_tables = list(dict.fromkeys(norm(x) for x in TABLE_LABEL.findall(remainder)))
        remainder = TABLE_LABEL.sub(' ', remainder)
        exact = []
        for match in QUERY_CLAUSE.finditer(remainder):
            after = remainder[match.end():].lstrip()
            before = remainder[max(0,match.start()-6):match.start()]
            measure = re.match(r'(?:mm|cm|m|kPa|MPa|Pa|kN|N|s|h|d|kg|g|%|℃|毫米|厘米|米|秒|小时|倍)(?![a-z])', after, re.I)
            value = norm(match.group())
            if not measure and (value.count('.') >= 2 or re.search(r'条|表|章节|第', before) or after.startswith(('条','节'))
                                or bool(requested) or norm(remainder) == value):
                exact.append(value)
        exact = list(dict.fromkeys(exact))
        years = set(re.findall(r'(?<!\d)(?:19|20)\d{2}(?!\d)', remainder))
        remainder = QUERY_CLAUSE.sub(' ', remainder)
        remainder = re.sub(r'(?<!\d)(?:19|20)\d{2}(?!\d)', '', remainder)
        qt = list(dict.fromkeys(t for t in tokens(remainder) if not t.isdigit()))
        # Exact clause/standard queries do not depend on incidental filler words.
        topic = qt if not (exact or exact_tables) else []
        marks = ','.join('?' for _ in collection_ids)
        if topic:
            expression = ' OR '.join('"' + t.replace('"', '""') + '"' for t in topic[:80])
            sql = f'SELECT c.json,bm25(search) FROM search JOIN chunks c ON c.id=search.rowid WHERE search MATCH ? AND c.collection_id IN ({marks}) ORDER BY bm25(search) LIMIT 2000'
            rows = self.db.execute(sql, [expression] + collection_ids).fetchall()
        elif exact or exact_tables or requested or years:
            rows = self.db.execute(f'SELECT json,0 FROM chunks WHERE collection_id IN ({marks})', collection_ids).fetchall()
        else:
            rows = []
        candidates = []
        for raw, rank in rows:
            hit = json.loads(raw)
            if requested and not any(hit['standard_code'] == code and (not year or hit['version'] == year) for code, year in requested):
                continue
            if years and hit['version'] not in years:
                continue
            if standards and hit['standard_code'] not in standards:
                continue
            if versions and hit['version'] not in versions:
                continue
            if categories and hit['category'] not in categories:
                continue
            if exact_tables and not any(v in hit['table_candidates'] for v in exact_tables):
                continue
            if exact and not any(v == hit['clause_candidate'] or v in hit['table_candidates'] for v in exact):
                continue
            body = norm(hit['text']).lower()
            matched = sum(t in body for t in topic)
            if topic and (matched < min(2, len(topic)) or matched / len(topic) < .30):
                continue
            hit['score'] = round(-rank, 5)
            hit['matched_tokens'] = matched
            candidates.append(hit)
        candidates.sort(key=lambda h: (h['section'] == '目录', -h['matched_tokens'], -h['score'], h['doc_id'], h['ordinal']))
        # Diversify near-duplicate clauses while retaining each version requested.
        for hit in candidates:
            if any(h['doc_id']==hit['doc_id'] and h['content_hash']==hit['content_hash'] for h in result['evidence']):
                continue
            neighbours = self.db.execute('SELECT json FROM chunks WHERE collection_id=? AND doc_id=? AND ordinal IN (?,?) ORDER BY ordinal',
                                         (hit['collection_id'], hit['doc_id'], hit['ordinal']-1, hit['ordinal']+1)).fetchall()
            hit['context'] = [json.loads(r[0]) for r in neighbours if json.loads(r[0])['section'] == hit['section']]
            # Context is separate evidence, never merged into the detected requirement.
            result['evidence'].append(hit)
            if len(result['evidence']) >= top_k:
                break
        if not result['evidence']:
            result['gaps'].append('未找到足够依据；不表示规范没有相关要求。明确指定的版本不会替换为其他版本。')
        return result


def validate_evidence(library, evidence, selected):
    """Verify scope, immutable page text ranges and original physical page bounds."""
    try:
        if evidence['collection_id'] not in selected or evidence.get('index_version') != INDEX_VERSION:
            return False
        collection = library.get_collection(evidence['collection_id'])
        if not collection or collection['revision'] != evidence['revision']:
            return False
        doc = library.get_document(evidence['collection_id'], evidence['doc_id'])
        if not doc or doc.get('status') == 'failed':
            return False
        sources = evidence['sources']
        if not sources or digest(sources) != evidence['content_hash']:
            return False
        if norm(evidence['text']) != norm('\n'.join(s['text'] for s in sources)):
            return False
        if evidence['evidence_id'] != 'E-' + digest([evidence['collection_id'], evidence['doc_id'], evidence['content_hash']])[:16]:
            return False
        for source in sources:
            if not 1 <= source['page'] <= doc['page_count']:
                return False
            page = library.get_page(evidence['collection_id'], evidence['doc_id'], source['page'])
            if not page:
                return False
            regions = page.get('regions') or [{'region':'full', 'text':page.get('text','')}]
            region = next((r for r in regions if r['region']==source['region']), None)
            if not region or not norm(source['text']) or norm(source['text']) not in norm(region['text']):
                return False
            if source['quality'] != page['quality']:
                return False
        return True
    except (KeyError, TypeError, ValueError):
        return False
