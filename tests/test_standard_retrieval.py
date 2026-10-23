import copy
from pathlib import Path

import pytest


class LibraryFixture:
    def __init__(self, root):
        self.root = Path(root)
        self.rev = 1
        self.docs = [dict(doc_id='a'*64, sha256='a'*64, filename='玻璃.pdf', title='玻璃幕墙',
                          standard_code='JGJ102', version='2003', category='幕墙', status='processed', page_count=4),
                     dict(doc_id='b'*64, sha256='b'*64, filename='新版.pdf', title='玻璃幕墙',
                          standard_code='JGJ102', version='2025', category='幕墙', status='processed', page_count=1)]
        self.pgs = [self.pg('a'*64, 1, '1.1.1 玻璃幕墙应防止雨水渗漏。\n1.1.2 幕墙连接应符合下列规定：'),
                    self.pg('a'*64, 2, '特殊情况下，不应省略连接检查。\n1.1.3 防火材料不得使用易燃材料。'),
                    self.pg('a'*64, 3, '5.2.1 玻璃强度按表5.2.1采用。\n表5.2.1 强度 N/mm²\n玻璃\t28.0\n注：应核对厚度。', quality='locate'),
                    self.pg('a'*64, 4, '', regions=[dict(region='left', text='6.1.1 左页密封胶应连续。', bbox=[0,0,100,100]),
                                                   dict(region='right', text='6.1.2 右页开启扇不得脱落。', bbox=[100,0,200,100])]),
                    self.pg('b'*64, 1, '1.1.1 玻璃幕墙新版要求。')]

    @staticmethod
    def pg(doc, page, text, quality='reference', regions=None):
        return dict(doc_id=doc, page=page, original_page=page, processed_page=page, text=text,
                    quality=quality, issues=['渲染差异'] if quality=='locate' else [], numeric_risk=False,
                    regions=regions or [dict(region='full', text=text, bbox=None)], printed_candidate='候选', printed_status='未确认')

    def documents(self, cid): return copy.deepcopy(self.docs)
    def pages(self, cid, doc_id=None): return copy.deepcopy([p for p in self.pgs if not doc_id or p['doc_id']==doc_id])
    def get_document(self, cid, doc): return next((d for d in self.documents(cid) if d['doc_id']==doc), None)
    def get_page(self, cid, doc, page): return next((p for p in self.pages(cid, doc) if p['page']==page), None)
    def get_collection(self, cid): return dict(collection_id=cid, revision=self.rev)
    def source_changes(self, cid): return []


@pytest.fixture
def indexed(tmp_path):
    from evidence.standard_retrieval import StandardRetriever
    lib=LibraryFixture(tmp_path)
    retriever=StandardRetriever(lib)
    retriever.build('c')
    return lib,retriever


def test_exact_version_clause_and_short_text(indexed):
    _,r=indexed
    hits=r.search('JGJ102-2003 1.1.1', ['c'])['evidence']
    assert hits and all(h['version']=='2003' for h in hits)
    assert any('防止雨水渗漏' in h['text'] for h in hits)
    assert not r.search('JGJ102-1999 1.1.1', ['c'])['evidence']


def test_chinese_context_crosspage_and_double_spread(indexed):
    _,r=indexed
    hits=r.search('JGJ102-2003 1.1.2', ['c'])['evidence']
    hit=next(h for h in hits if h['clause_candidate']=='1.1.2')
    assert {s['page'] for s in hit['sources']}=={1,2}
    assert '不应省略' in hit['text']
    assert r.search('防火材料', ['c'])['evidence']
    hit=r.search('JGJ102-2003 6.1.1', ['c'])['evidence'][0]
    assert '右页' not in hit['text']
    assert hit['sources'][0]['region']=='left'


def test_quality_unrelated_scope_and_stale(indexed):
    lib,r=indexed
    assert not r.search('火星咖啡烹饪食谱', ['c'])['evidence']
    hit=r.search('JGJ102-2003 表5.2.1', ['c'])['evidence'][0]
    assert not hit['answer_eligible']
    assert not r.search('防火材料', [])['evidence']
    assert not r.search('防火材料', ['c'], versions=['2025'])['evidence']
    lib.rev+=1
    stale=r.search('防火材料', ['c'])
    assert not stale['evidence'] and stale['stale']
    r.build('c')
    assert r.search('防火材料', ['c'])['evidence']


def test_stable_citation_and_tampering(indexed):
    from evidence.standard_retrieval import validate_evidence
    lib,r=indexed
    h=r.search('防火材料', ['c'])['evidence'][0]
    assert validate_evidence(lib,h,['c'])
    assert not validate_evidence(lib,h,['other'])
    changed=copy.deepcopy(h); changed['sources'][0]['page']=999
    assert not validate_evidence(lib,changed,['c'])
    changed=copy.deepcopy(h); changed['text']='伪造原文'
    assert not validate_evidence(lib,changed,['c'])
    r.build('c')
    assert r.search('防火材料', ['c'])['evidence'][0]['evidence_id']==h['evidence_id']


@pytest.mark.parametrize('raw,expected',[
    ('GB-T 3098.2-2025',('GB/T3098.2','2025')),
    ('JG／T475-2015',('JG/T475','2015')),
    ('DG/TJ08-22-2013',('DG/TJ08-22','2013')),
    ('GB14683--2017',('GB14683','2017')),
    ('GB93-87',('GB93','87')),
])
def test_standard_identity_punctuation(raw,expected):
    from evidence.standard_retrieval import standard_parts
    assert standard_parts(raw)==expected


@pytest.mark.parametrize('body', ['5.3 稳压气源：气压可达700 kPa。', '5.4 秒表：分度值0.1 s。',
                                 '1.1.1 建筑高度超过150m时应核对。', '1.1.1 见表 A.1。',
                                 '1.1.1 表5. 5. 1 参数\n28.0\t17.0'])
def test_unreviewed_numeric_units_and_tables_not_claims(body):
    from evidence.standard_retrieval import make_chunks
    chunks=make_chunks({},[LibraryFixture.pg('a',1,body)])
    assert chunks and not any(c['answer_eligible'] for c in chunks)


def test_decimal_measurement_not_clause_and_spaced_ocr(indexed):
    lib,r=indexed
    lib.pgs=[lib.pg('a'*64,1,'2.5 防火材料不得使用易燃材料。\n3.1 立柱壁厚不得小于2.5mm。\n5. 5. 1 热工性能应符合规定。')]
    lib.rev+=1; r.build('c')
    hits=r.search('立柱壁厚是否至少2.5mm',['c'])['evidence']
    assert hits and hits[0]['clause_candidate']=='3.1'
    assert r.search('JGJ102-2003 5.5.1',['c'])['evidence'][0]['clause_candidate']=='5.5.1'


@pytest.mark.parametrize('heading', ['前言', '编制说明', '条文说明'])
def test_frontmatter_and_commentary_are_locator_only(heading):
    from evidence.standard_retrieval import make_chunks
    chunks=make_chunks({},[LibraryFixture.pg('a',1,heading+'\n本规范适用于玻璃幕墙。')])
    assert chunks and all(not c['answer_eligible'] for c in chunks)


def test_numbered_conditions_stay_with_clause():
    from evidence.standard_retrieval import make_chunks
    chunks=make_chunks({},[LibraryFixture.pg('a',1,'3.0.3 应符合下列规定：\n1 具有资质\n2 具有设备\n3 应留存记录。')])
    assert len(chunks)==1
    assert '具有资质' in chunks[0]['text'] and '应留存记录' in chunks[0]['text']


def test_clause_resets_front_matter_section():
    from evidence.standard_retrieval import make_chunks
    chunks=make_chunks({},[LibraryFixture.pg('a',1,'前言\n本标准编制说明。\n1.1 玻璃幕墙应符合规定。')])
    assert chunks[-1]['section']=='正文候选'


def test_commentary_stays_separate_through_its_own_preface_and_contents():
    """JGJ102-2003 physical pages 51-53 have a second preface and contents."""
    from evidence.standard_retrieval import make_chunks
    pages = [
        LibraryFixture.pg('a', 50, '5.1.4 幕墙内力和变形可按弹性方法计算。'),
        LibraryFixture.pg('a', 51, '中华人民共和国行业标准\n玻璃幕墙工程技术规范\nJGJ 102—2003\n条文说明'),
        LibraryFixture.pg('a', 52, '', regions=[
            dict(region='left', text='前言\n本规范的条文说明，供使用者参考。', bbox=None),
            dict(region='right', text='目\t次\n1 总则……94', bbox=None)]),
        LibraryFixture.pg('a', 53, '1总\t则\n1.0.1 由玻璃面板与支承结构体系组成。'),
        LibraryFixture.pg('a', 64, '5.1.4 目前，结构抗震设计的标准是小震下保持弹性。'),
    ]
    chunks = make_chunks({}, pages)
    assert chunks[0]['section'] == '正文候选'
    explanations = [c for c in chunks if c['sources'][0]['page'] >= 52]
    assert explanations and all(c['section'] == '条文说明' for c in explanations)
    assert all(not c['answer_eligible'] for c in explanations)


@pytest.mark.parametrize('heading', ['条\n文\n说\n明', '条文\n说明', '条 文 说 明'])
def test_split_commentary_heading_preserves_original_source(heading):
    from evidence.standard_retrieval import make_chunks
    body = '封面文字\n' + heading + '\n1.0.1 本条解释玻璃幕墙。'
    chunks = make_chunks({}, [LibraryFixture.pg('a', 1, body)])
    assert chunks[-1]['section'] == '条文说明'
    assert not chunks[-1]['answer_eligible']
    assert any(c['text'] == heading for c in chunks)


def test_commentary_mention_in_clause_does_not_change_section():
    from evidence.standard_retrieval import make_chunks
    body = '1.0.1 本规范附有条文说明。\n1.0.2 玻璃幕墙应符合规定。'
    chunks = make_chunks({}, [LibraryFixture.pg('a', 1, body)])
    assert all(c['section'] == '正文候选' for c in chunks)


@pytest.mark.parametrize('label', ['15', 'A.1', '5.2.1'])
def test_exact_table_selector_keeps_table_references_not_same_numbered_clause(indexed, label):
    lib, r = indexed
    lib.pgs = [lib.pg('a'*64, 1, '2.1 材料强度按表' + label + '采用。\n表' + label + ' 材料强度\n普通玻璃 28'),
               lib.pg('a'*64, 2, '5.2.1 另一条文不含所查表格。')]
    lib.rev += 1; r.build('c')
    hits = r.search('JGJ102-2003 请查表' + label, ['c'])['evidence']
    assert hits and all(label in h['table_candidates'] for h in hits)
    assert all(h['clause_candidate'] != '5.2.1' for h in hits)


def test_table_word_in_topic_and_multiple_explicit_years(indexed):
    lib, r = indexed
    for d in lib.docs:
        d['standard_code'] = 'GB/T3098.2'
    lib.docs[0]['version'] = '2015'
    lib.pgs = [lib.pg(d['doc_id'], 1, '1.1.1 表面积应核对。\n2.1 表15 材料强度\n普通玻璃28') for d in lib.docs]
    lib.rev += 1; r.build('c')
    assert r.search('表面积', ['c'])['evidence'][0]['clause_candidate'] == '1.1.1'
    hits = r.search('GB/T3098.2 2015和2025 表15', ['c'])['evidence']
    assert {h['version'] for h in hits} == {'2015', '2025'}
