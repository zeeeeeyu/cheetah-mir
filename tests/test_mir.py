import csv
import json
import socket
import sqlite3
from pathlib import Path

import pytest

from cheetah_mir.core import Workspace, ConflictError, timestamp, digest
from cheetah_mir.adapters import AkshareAdapter, RemoteAdapter, NetworkDisabled, ingest_feed, ingest_pdf, build_graph, parse_pdf_native
from cheetah_mir.cli import main, export, status

AT = '2026-11-05T10:00:00+08:00'
PUB = '2026-11-04T08:00:00+08:00'
FIXTURES = Path(__file__).parent/'fixtures'
QUOTE = 'If market breadth improves for three sessions, the rebound may persist for five trading days.'

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*a, **kw):
        raise AssertionError('external socket attempted')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket, 'create_connection', deny)

@pytest.fixture
def chain(tmp_path):
    w = Workspace(tmp_path/'mir', initialize=True)
    s = w.source('Fixture', 'https://example.invalid', 'B', collected_at=AT)
    doc = w.ingest_file(FIXTURES/'article.md', s, collected_at=AT, published_at=PUB)
    person = w.entity('MOCK Trader A', 'PERSON', collected_at=AT, document_id=doc)
    ev = w.evidence(doc, QUOTE, 'paragraph 1', collected_at=AT)
    return w, s, doc, person, ev

def bar_args(w, s):
    doc = w.ingest_file(FIXTURES/'market.csv', s, collected_at=AT)
    asset = w.asset('MOCK_INDEX', 'MOCK', 'INDEX', collected_at=AT, document_id=doc)
    with (FIXTURES/'market.csv').open(encoding='utf-8') as f:
        row = next(csv.DictReader(f))
    return doc, asset, row

def test_archive_is_independent_of_original(chain, tmp_path):
    w, s, *_ = chain
    p = tmp_path/'original.txt'; p.write_text('one', encoding='utf-8')
    doc = w.ingest_file(p, s, collected_at=AT)
    p.write_text('two', encoding='utf-8')
    raw = w.safe_path(w.get(doc)['payload']['raw_path'])
    assert raw.read_text(encoding='utf-8') == 'one'
    assert digest(raw.read_bytes()) == w.get(doc)['payload']['raw_sha256']

def test_duplicate_does_not_add_records_or_audit_noise(chain):
    w, s, doc, *_ = chain
    with w.db() as c: before = c.execute('SELECT count(*) FROM audit').fetchone()[0]
    assert w.ingest_file(FIXTURES/'article.md', s, collected_at='2026-11-06T10:00:00+08:00', published_at=PUB) == doc
    with w.db() as c: assert c.execute('SELECT count(*) FROM audit').fetchone()[0] == before

def test_same_content_across_sources_keeps_both_provenances(chain):
    w, _, doc, *_ = chain
    s2 = w.source('Other publisher', 'https://example.invalid/other', 'A', collected_at=AT)
    other = w.ingest_file(FIXTURES/'article.md', s2, collected_at=AT, published_at=PUB)
    assert other != doc
    assert w.get(doc)['payload']['duplicate_group'] == w.get(other)['payload']['duplicate_group']
    assert w.get(doc)['source_id'] != w.get(other)['source_id']

def test_timezone_conversion_preserves_original():
    ts = timestamp(PUB)
    assert ts['utc'] == '2026-11-04T00:00:00+00:00'
    assert ts['original'] == PUB and ts['timezone']

@pytest.mark.parametrize('value', ['2026-11-04T08:00:00', 'not a date', None])
def test_naive_or_bad_time_rejected(value):
    with pytest.raises(ValueError): timestamp(value)

def test_pit_requires_publication_and_actual_capture(chain):
    w, _, doc, *_ = chain
    assert w.get(doc)['available_at'] == timestamp(AT)['utc']
    assert not w.list('document', as_of='2026-11-04T23:59:00+08:00')
    assert w.list('document', as_of=AT)

def test_future_publication_not_visible_early(chain):
    w, s, *_ = chain
    doc = w.ingest_file(FIXTURES/'feed.xml', s, collected_at=AT, published_at='2026-11-07T12:00:00+08:00')
    assert doc not in {r['id'] for r in w.list(as_of=AT)}

def test_hash_tampering_is_detected(chain):
    w, _, doc, *_ = chain
    w.safe_path(w.get(doc)['payload']['raw_path']).write_bytes(b'tampered')
    assert not w.verify()['ok']

def test_invalid_tier_rejected(chain):
    w, *_ = chain
    with pytest.raises(ValueError): w.source('X', None, 'C', collected_at=AT)

def test_unknown_source_rejected_before_archive_write(chain):
    w, *_ = chain
    with pytest.raises(KeyError): w.ingest_file(FIXTURES/'article.md', 'missing', collected_at=AT)

def test_fact_conflict_keeps_old_and_logs_exception(chain):
    w, s, *_ = chain
    args = dict(collected_at=AT, source_id=s)
    rid = w.put('event', 'event-1', {'name': 'old'}, **args)
    with pytest.raises(ConflictError): w.put('event', 'event-1', {'name': 'new'}, **args)
    assert w.get(rid)['payload']['name'] == 'old'
    with w.db() as c: assert c.execute('SELECT count(*) FROM conflicts').fetchone()[0] == 1

def test_revision_is_append_only(chain):
    w, s, *_ = chain
    old = w.put('event', 'ev', {'name': 'first'}, collected_at=AT, source_id=s)
    new = w.revise(old, {'name': 'revised'}, collected_at=AT)
    assert w.get(new)['payload']['previous_version_id'] == old
    assert w.get(old)['payload']['name'] == 'first'
    with w.db() as c, pytest.raises(sqlite3.IntegrityError): c.execute('DELETE FROM records WHERE id=?', (old,))

def test_quote_cannot_be_invented(chain):
    w, _, doc, *_ = chain
    with pytest.raises(ValueError, match='not present'): w.evidence(doc, 'invented quote', 'p1', collected_at=AT)

def test_opinion_is_linked_to_person_and_original_quote(chain):
    w, _, doc, person, ev = chain
    rid = w.opinion(person, ev, opinion_state='FORECAST', target='MOCK', horizon='T+5', collected_at=AT, published_at=PUB)
    value = w.get(rid)
    assert value['document_id'] == doc and value['payload']['quote'] == QUOTE
    assert value['payload']['attribution'] == 'AUTHOR_EXPLICIT_VIEW'

def test_company_cannot_be_misattributed_as_person(chain):
    w, _, doc, _, ev = chain
    company = w.entity('Company', 'COMPANY', collected_at=AT, document_id=doc)
    with pytest.raises(ValueError): w.opinion(company, ev, opinion_state='FORECAST', target='X', horizon='T+5', collected_at=AT, published_at=PUB)

@pytest.mark.parametrize('state', ['PREDICTION', 'VERIFIED', ''])
def test_invalid_opinion_enums_rejected(chain, state):
    w, _, _, person, ev = chain
    with pytest.raises(ValueError): w.opinion(person, ev, opinion_state=state, target='MOCK', horizon=None, collected_at=AT, published_at=PUB)

def test_unlisted_company_cannot_get_fake_code(chain):
    w, _, doc, *_ = chain
    with pytest.raises(ValueError): w.asset('999999', 'SSE', 'EQUITY', collected_at=AT, document_id=doc, listing_status='UNLISTED')

def test_equity_etf_exact_identity_is_checked(chain):
    w, s, *_ = chain
    doc, asset, row = bar_args(w, s)
    row['asset_type'] = 'ETF'
    with pytest.raises(ValueError, match='identity mismatch'): w.market_bar(asset, row, document_id=doc, collected_at=AT)

def test_nav_cannot_enter_ohlc(chain):
    w, s, *_ = chain
    doc, _, row = bar_args(w, s)
    nav = w.asset('000001', 'FUND', 'FUND_NAV', collected_at=AT, document_id=doc)
    with pytest.raises(ValueError, match='NAV'): w.market_bar(nav, row, document_id=doc, collected_at=AT)

@pytest.mark.parametrize('bad', ['NaN', 'inf', '-1'])
def test_nonfinite_and_negative_market_values_rejected(chain, bad):
    w, s, *_ = chain
    doc, asset, row = bar_args(w, s); row['close'] = bad
    with pytest.raises(ValueError): w.market_bar(asset, row, document_id=doc, collected_at=AT)

def test_missing_amount_stays_null(chain):
    w, s, *_ = chain
    doc, asset, row = bar_args(w, s); row['amount'] = ''
    bar = w.market_bar(asset, row, document_id=doc, collected_at=AT)
    assert w.get(bar)['payload']['amount'] is None
    assert w.get(bar)['payload']['quality'] == 'PARTIAL'

def test_ohlc_bounds_rejected(chain):
    w, s, *_ = chain
    doc, asset, row = bar_args(w, s); row['high'] = '98'
    with pytest.raises(ValueError): w.market_bar(asset, row, document_id=doc, collected_at=AT)

def test_market_has_document_and_source(chain):
    w, s, *_ = chain
    doc, asset, row = bar_args(w, s)
    bar = w.market_bar(asset, row, document_id=doc, collected_at=AT)
    assert w.get(bar)['source_id'] == s and w.get(bar)['document_id'] == doc

def test_typed_graph_and_relation_evidence(chain):
    w, s, doc, person, ev = chain
    event = w.put('event', 'e', {'name': 'MOCK'}, collected_at=AT, source_id=s, document_id=doc)
    rel = w.relation(person, event, 'REFERENCES', ev, collected_at=AT, valid_from=PUB, confidence=.8)
    graph = build_graph(w)
    assert graph.edges[person, event, rel]['evidence_id'] == ev
    assert graph.edges[person, event, rel]['review_status'] == 'UNVERIFIED'
    assert build_graph(w, '2026-11-04T23:59:00+08:00').number_of_edges() == 0

def test_unverified_relation_cannot_be_auto_promoted(chain):
    w, _, doc, person, ev = chain
    with pytest.raises(ValueError, match='auto-verify'): w.relation(person, doc, 'PUBLISHED', ev, collected_at=AT, valid_from=PUB, review_status='VERIFIED')

def test_relationship_requires_evidence_not_document(chain):
    w, _, doc, person, _ = chain
    with pytest.raises(ValueError): w.relation(person, doc, 'PUBLISHED', doc, collected_at=AT, valid_from=PUB)

def test_network_disabled_before_any_provider_call():
    with pytest.raises(NetworkDisabled): AkshareAdapter().fetch('000001')
    with pytest.raises(NetworkDisabled): RemoteAdapter().fetch('https://example.invalid')

def test_rss_fixture_summary_is_not_full_article(chain):
    w, s, *_ = chain
    ids = ingest_feed(w, FIXTURES/'feed.xml', s, collected_at=AT)
    assert len(ids) == 1 and w.get(ids[0])['payload']['full_text_available'] is False
    assert w.get(ids[0])['document_id']

def test_broken_feed_is_logged_and_does_not_silently_fallback(chain, tmp_path):
    w, s, *_ = chain
    p = tmp_path/'bad.xml'; p.write_text('<invalid>', encoding='utf-8')
    with pytest.raises(ValueError): ingest_feed(w, p, s, collected_at=AT)
    with w.db() as c: assert c.execute("SELECT count(*) FROM failures WHERE adapter='rss'").fetchone()[0] == 1

def test_failed_pdf_is_archived_and_logged(chain, tmp_path):
    w, s, *_ = chain
    p = tmp_path/'bad.pdf'; p.write_bytes(b'not a PDF')
    with pytest.raises(Exception): ingest_pdf(w, p, s, collected_at=AT)
    with w.db() as c: assert c.execute("SELECT count(*) FROM failures WHERE adapter='docling'").fetchone()[0] == 1

@pytest.mark.parametrize('name', ['quant_v4_6', 'Cheetah_UI', 'Cheetah_V4.2_fixed', 'Quant_V4.5.1_fixed'])
def test_protected_project_root_rejected(tmp_path, name):
    with pytest.raises(ValueError): Workspace(tmp_path/name/'research_lab'/'..'/'escape', initialize=True)

def test_resolved_path_escape_rejected(chain):
    w, *_ = chain
    with pytest.raises(ValueError): w.safe_path('../account.db')

def test_exports_are_deterministic_and_do_not_mutate_raw(chain):
    w, *_ = chain
    assert w.verify()['ok']; export(w)
    first = w.safe_path('reports/export.json').read_bytes()
    export(w)
    assert w.safe_path('reports/export.json').read_bytes() == first and w.verify()['ok']

def test_akshare_shaped_dataframe_is_usable_without_endpoint():
    import pandas as pd
    frame = pd.DataFrame([{'日期':'2026-11-04','开盘':100,'最高':102,'最低':99,'收盘':101,'成交量':1000,'成交额':100000}])
    result = AkshareAdapter().normalize(frame, code='MOCK_INDEX', exchange='MOCK', asset_type='INDEX', adjustment='NONE', market_as_of='2026-11-04T15:00:00+08:00')
    assert result[0]['close'] == 101 and result[0]['code'] == 'MOCK_INDEX'

def test_cli_init_sources_and_integrity(tmp_path, capsys):
    root = tmp_path/'cli'
    assert main(['--root', str(root), 'init']) == 0
    assert main(['--root', str(root), 'sources', 'validate']) == 0
    assert main(['--root', str(root), 'provenance', 'verify']) == 0
    assert '"ok": true' in capsys.readouterr().out

def test_native_pdf_has_page_provenance_offline(tmp_path):
    pytest.importorskip('docling')
    from reportlab.pdfgen.canvas import Canvas

    pdf = tmp_path / 'synthetic-evidence.pdf'
    label = 'Synthetic closing evidence for provenance testing.'
    canvas = Canvas(str(pdf))
    canvas.drawString(72, 720, label)
    canvas.save()
    result = parse_pdf_native(pdf)
    assert label in result['text']
    assert any(p['page_no'] == 1 for b in result['blocks'] for p in b['provenance'])

def test_fixture_chain_end_to_end_is_repeatable(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location('demo_chain', Path(__file__).parents[1]/'tools/demo_chain.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    first = module.run(tmp_path/'chain'); second = module.run(tmp_path/'chain')
    assert first == second
    assert second['graph_edges'] == 4 and second['status']['provenance']['ok']

def test_same_bytes_different_filename_are_deduplicated(chain, tmp_path):
    w, s, doc, *_ = chain
    copy = tmp_path/'renamed.md'; copy.write_bytes((FIXTURES/'article.md').read_bytes())
    assert w.ingest_file(copy, s, collected_at=AT, published_at=PUB) == doc

def test_publication_change_requires_revision_not_silent_hindsight_edit(chain):
    w, s, doc, *_ = chain
    with pytest.raises(ConflictError):
        w.ingest_file(FIXTURES/'article.md', s, collected_at=AT, published_at='2026-11-03T08:00:00+08:00')
    assert w.get(doc)['payload']['published_time']['original'] == PUB

def test_revision_cannot_upgrade_evidence(chain):
    w, _, _, _, ev = chain
    with pytest.raises(ValueError): w.revise(ev, {'fact_status':'VERIFIED'}, collected_at=AT)

def test_company_and_person_views_do_not_mix(chain):
    w, _, doc, *_ = chain
    w.entity('Company', 'COMPANY', collected_at=AT, document_id=doc)
    with w.db() as c:
        assert c.execute('SELECT count(*) FROM company_profiles').fetchone()[0] == 1
        assert c.execute('SELECT count(*) FROM trader_profiles').fetchone()[0] == 1

def test_market_as_of_matches_trade_date(chain):
    w, s, *_ = chain
    doc, asset, row = bar_args(w, s); row['market_as_of'] = '2026-11-05T15:00:00+08:00'
    with pytest.raises(ValueError): w.market_bar(asset, row, document_id=doc, collected_at=AT)

def test_fact_record_requires_source(chain):
    w, *_ = chain
    with pytest.raises(ValueError): w.put('event', 'anonymous', {'name':'unsupported'}, collected_at=AT)
